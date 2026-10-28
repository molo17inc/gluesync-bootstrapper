"""the two synthetic backups (Synth1: 24 schemas / 58 entities into TGT01 and TGT02; Synth2: 30 entities into
TGT03) imported against a mocked Core Hub 2.3.1 with no target table yet: every entity is created in the right
target schema, with the YAML's keys, and every target table is created once with the union of its feeders' columns.

The fixtures under fixtures/synthetic are the backups with secrets stripped (credentials and hosts are placeholders).
Run as ``main`` does: target schemas inferred from snapshot commands, then one pass per target schema."""

import contextlib
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_corehub import FakeCoreHub, FakeFunctionCollections, column, patched, SOURCE_AGENT, TARGET_AGENT  # noqa: E402

import create_all_entities  # noqa: E402
import create_all_tables  # noqa: E402
import fan_in  # noqa: E402
import table_target_schema  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "synthetic"
FUNCTIONS = ("fnHelper01", "fnExtra01", "fnExtra02", "fnHelper03", "fnHelper02",
             "fnExtra03", "fnExtra04", "fnExtra05", "fnExtra06", "fnExtra07",
             "fnHelper05", "fnExtra08", "fnHelper04", "fnHelper06", "fnExtra09",
             "fnExtra10", "fnExtra11")
TGT02_TABLES = {"TAB039", "TAB047", "TAB042", "TAB043", "TAB041@@3", "TAB044@@2", "TAB045", "TAB040",
               "TAB046"}


def load(tag):
    return yaml.safe_load((FIXTURES / f"{tag}_entities.yaml").read_text())


def _names(value):
    return [value] if isinstance(value, str) else [v for v in (value or []) if isinstance(v, str)]


def source_discovery(config):
    """The source tables the YAML describes, with the types, lengths and keys its rows give."""
    tables = {}
    for schema, entry in config["schemas"].items():
        for key, cfg in entry["tables"]["custom"].items():
            cols = tables.setdefault((schema, cfg.get("sourceTableName") or key.split("@@")[0]), [])
            _add_row_columns(cols, cfg.get("columns") or [])
    return tables


def _add_row_columns(cols, rows):
    """Add to ``cols`` a discovered column per source column of the YAML ``rows`` not in it yet."""
    seen = {c["name"] for c in cols}
    for row in rows:
        for name in _names(row.get("source")):
            if name not in seen:
                seen.add(name)
                cols.append(_row_column(row, name, len(cols) + 1))


def _row_column(row, name, position):
    return column(name, position, row.get("type") or "varchar",
                  is_pk=bool(row.get("isPK")), charMaxLength=row.get("dataLength") or 0,
                  numPrec=row.get("numericPrecision") or 0,
                  numScale=row.get("numericScale") or 0)


class _Import:
    """One mocked Import All of a backup: entities saved and target tables created."""

    def __init__(self, tag):
        logging.disable(logging.CRITICAL)
        config = load(tag)
        self.config, self.inferred = table_target_schema.apply_inferred_target_schemas(config, None, True)
        self.hub = FakeCoreHub(source_discovery(self.config), {}, version="2.3.1.0 - build test",
                               function_collection=FakeFunctionCollections.exporting(*FUNCTIONS))
        self.created = {}  # (schema, table) -> [Column]
        self.results = {}

        def exists(pipeline_id, schema_name, table_name, token):
            return (schema_name, table_name) in self.created

        def statement(pipeline_id, schema_name, table_name, token, table_data):
            self.created[(schema_name, table_name)] = list(table_data.columns)
            return f"CREATE TABLE {schema_name}.{table_name}"

        with contextlib.ExitStack() as stack:
            stack.enter_context(patched(self.hub))
            for target, name, value in (
                    (create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", True),
                    (create_all_tables, "CREATE_TABLE_IF_NOT_EXISTS", True)):
                stack.enter_context(mock.patch.object(target, name, value))
            stack.enter_context(mock.patch.object(create_all_tables, "table_exists", side_effect=exists))
            stack.enter_context(mock.patch.object(create_all_tables, "generate_create_table_statement",
                                                  side_effect=statement))
            stack.enter_context(mock.patch.object(create_all_tables, "create_target_table", return_value=None))
            for schema, entry in self.config["schemas"].items():
                tables = sorted({t for (s, t) in self.hub.source_tables if s == schema})
                for pass_target, pass_yaml in table_target_schema.split_by_target_schema(
                        self.config, schema, entry["target"]):
                    self.results[(schema, pass_target)] = create_all_entities.create_entities(
                        token="token", pipeline_id="pipeline", source_schema=schema, target_schema=pass_target,
                        tables=tables, source_agent_id=SOURCE_AGENT, target_agent_id=TARGET_AGENT,
                        source_type="RDBMS", target_type="RDBMS", yaml_config=pass_yaml, skip_errors=True,
                        chunk_size=50)
        logging.disable(logging.NOTSET)

    def expected(self):
        """``{entityName: (target schema, target table, keys, target column names)}`` from the YAML."""
        out = {}
        for schema, entry in self.config["schemas"].items():
            for key, cfg in entry["tables"]["custom"].items():
                out[cfg.get("entityName") or key] = (cfg.get("targetSchema") or entry["target"],
                                                     cfg.get("name") or key.split("@@")[0], cfg)
        return out


_CACHE = {}


def imported(tag):
    if tag not in _CACHE:
        _CACHE[tag] = _Import(tag)
    return _CACHE[tag]


def _entity_target(entity):
    tgt = entity.get("targetEntity") or entity.get("target") or {}
    return tgt


class SyntheticBackupImportTests(unittest.TestCase):
    def check_backup(self, tag, count):
        run = imported(tag)
        failures = {k: v.get("errors") for k, v in run.results.items() if v.get("failed")}
        self.assertEqual(failures, {})
        saved = {e.get("entityName"): e for e in run.hub.saved}
        expected = run.expected()
        self.assertEqual(len(expected), count)
        self.assertEqual(set(saved), set(expected))
        for name, (schema, table, cfg) in expected.items():
            with self.subTest(entity=name):
                self.assertIn((schema, table), run.created, f"{name}: target table {schema}.{table} not created")
                created_cols = {c.name.casefold() for c in run.created[(schema, table)]}
                for row in cfg.get("columns") or []:
                    for target in _names(row.get("target")):
                        self.assertIn(target.casefold(), created_cols, f"{name}: column {target}")
                text = repr(saved[name])
                self.assertIn(repr(schema), text, f"{name}: entity not in target schema {schema}")
                for key in cfg.get("keys") or []:
                    self.assertIn(repr(str(key).split(":")[0].strip()), text, f"{name}: key {key}")
        return run

    def test_synth1_all_58_entities_and_tables(self):
        run = self.check_backup("synth1", 58)
        tgt02 = {k for s, e in run.config["schemas"].items() for k, c in e["tables"]["custom"].items()
                if c.get("targetSchema") == "TGT02"}
        self.assertEqual({k for k in tgt02}, TGT02_TABLES)
        self.assertEqual(len(run.inferred), 9)
        feeders = fan_in.target_feeders(run.config)
        # The TAB050 collision is gone: one table per target, the inferred one in TGT02
        self.assertEqual({k: len(v) for k, v in feeders.items() if len(v) > 1},
                         {("tgt01", "tab002"): 20, ("tgt01", "tab037"): 2})
        self.assertIn(("tgt02", "tab050"), feeders)
        self.assertEqual(len(feeders[("tgt01", "tab002")]), 20)
        student = {c.name: c for c in run.created[("TGT01", "TAB002")]}
        self.assertEqual(student["COL0013"].dataType.lower(), "varchar")
        self.assertEqual(student["COL0013"].charMaxLength, 10)
        union = fan_in.union_columns(feeders[("tgt01", "tab002")])
        self.assertEqual({n.casefold() for n in student}, set(union))

    def test_synth2_all_30_entities_and_tables(self):
        self.check_backup("synth2", 30)

    def test_explicit_target_schema_wins_and_inference_can_be_disabled(self):
        config = load("synth1")
        cfg = config["schemas"]["SCH19"]["tables"]["custom"]["TAB039"]
        cfg["targetSchema"] = "ELSEWHERE"
        inferred, notes = table_target_schema.apply_inferred_target_schemas(config, None, True)
        self.assertEqual(inferred["schemas"]["SCH19"]["tables"]["custom"]["TAB039"]["targetSchema"],
                         "ELSEWHERE")
        self.assertEqual(len(notes), 8)
        same, none = table_target_schema.apply_inferred_target_schemas(load("synth1"), None, False)
        self.assertEqual(none, [])
        with mock.patch.dict("os.environ", {"GS_IMPORT_INFER_TARGET_SCHEMA": "false"}):
            self.assertEqual(table_target_schema.apply_inferred_target_schemas(load("synth1"))[1], [])


class FanInCreationTests(unittest.TestCase):
    def test_union_and_widest_type(self):
        from create_all_tables import Column, _KeyLookup, _apply_fan_in
        own_cfg = {"columns": [{"source": "SN", "target": "SN", "type": "varchar", "dataLength": 9}]}
        peer = {"columns": [{"source": "SN", "target": "SN", "type": "varchar", "dataLength": 10},
                            {"source": "X", "target": "EXTRA", "type": "numeric", "numericPrecision": 7}]}
        own = [Column(name="SN", dataType="varchar", id=1, tableId=0, position=1, isPK=True, isNullable=False,
                      charMaxLength=9, numPrec=0, numScale=0)]
        info = {"category": "RDBMS", "dataTypesMatrix": []}
        with mock.patch.object(create_all_tables, "canonicalize_target_type", side_effect=lambda t, i: t), \
                mock.patch.object(create_all_tables, "map_data_type", side_effect=lambda t, *a: t):
            cols = {c.name: c for c in _apply_fan_in(own, own_cfg, [own_cfg, peer], "T", _KeyLookup([]), info, info)}
        self.assertEqual((cols["SN"].dataType, cols["SN"].charMaxLength, cols["SN"].isPK), ("varchar", 10, True))
        self.assertEqual((cols["EXTRA"].dataType, cols["EXTRA"].numPrec, cols["EXTRA"].isNullable),
                         ("numeric", 7, True))


class InferenceRuleTests(unittest.TestCase):
    def cfg(self, *commands):
        return {"preSnapshotCommand": list(commands)} if len(commands) > 1 else {"preSnapshotCommand": commands[0]}

    def test_rules(self):
        infer = table_target_schema.inferred_target_schema
        self.assertEqual(infer(self.cfg("DELETE FROM [TGT02].[T]"), "TGT01"), "TGT02")
        self.assertIsNone(infer(self.cfg("DELETE FROM [TGT01].[T]"), "TGT01"))
        self.assertIsNone(infer(self.cfg("DELETE FROM T"), "TGT01"))
        self.assertIsNone(infer({"targetSchema": "X", "preSnapshotCommand": "DELETE FROM [TGT02].[T]"}, "TGT01"))
        self.assertIsNone(infer({}, "TGT01"))


class CliTests(unittest.TestCase):
    def parse(self, *argv, env=None):
        from automator_app import import_backup
        with mock.patch.dict("os.environ", env or {}, clear=False):
            return import_backup._parse_args(["backup.zip", *argv])  # pylint: disable=protected-access

    def test_ssl_follows_the_base_url_unless_set(self):
        self.assertTrue(self.parse("--base-url", "https://hub:1717").use_ssl)
        self.assertFalse(self.parse("--base-url", "http://hub:1717").use_ssl)
        self.assertFalse(self.parse("--base-url", "https://hub:1717", "--no-use-ssl").use_ssl)
        self.assertTrue(self.parse("--base-url", "http://hub:1717", "--use-ssl").use_ssl)

    def test_resume_from_pipeline_needs_a_resume_pipeline_id(self):
        import io
        err = io.StringIO()
        with mock.patch.dict("os.environ", {"GS_IMPORT_RESUME_PIPELINE_ID": ""}), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            self.parse("--resume-from-pipeline", "old")
        self.assertIn("--resume-from-pipeline needs --resume-pipeline-id", err.getvalue())
        self.assertEqual(self.parse("--resume-from-pipeline", "old", "--resume-pipeline-id", "new")
                         .resume_from_pipeline, "old")


if __name__ == "__main__":
    unittest.main()
