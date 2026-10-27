#!/usr/bin/env python3
"""Steering items C-F of feat/simple-backup-import: half-created pipelines, UDF lookup, zip slip, missing tables,
plus per-table target schemas (A) and fan-in warnings (B). All synthetic: no real backup, no network."""

import argparse
import io
import logging
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import yaml

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import import_options  # noqa: E402
import create_all_entities  # noqa: E402
import fan_in  # noqa: E402
import table_target_schema  # noqa: E402
from fake_corehub import FakeCoreHub, column, run_create_entities  # noqa: E402

V23 = "2.3.0.0 - build test"

SOURCE = {("src", "ORDERS"): [column("ID", 1, "int", is_pk=True, nullable=False), column("NOTE", 2)]}
TARGET = {("tgt", "orders"): [column("id", 101, "int", is_pk=True), column("note", 102)],
          ("tgt", "ghost"): [column("id", 201, "int", is_pk=True), column("label", 202)]}

GHOST_YAML = """
src:
  target: tgt
  tables:
    custom:
      ORDERS:
        name: orders
        keys: [ID]
      GHOST:
        name: ghost
        keys: [ID]
        columns:
          - {source: ID, target: id, type: int, isPK: true}
          - {source: LABEL, target: label, type: varchar, dataLength: 40}
"""

UNTYPED_YAML = """
src:
  target: tgt
  tables:
    custom:
      ORDERS:
        name: orders
      UNTYPED:
        name: ghost
        columns:
          - {source: ID, target: id, type: int}
          - {source: LABEL, target: label}
"""


def _run(yaml_text, options):
    hub = FakeCoreHub(SOURCE, TARGET, version=V23)
    yaml_config = yaml.safe_load(yaml_text)
    with import_options.use(options):
        with mock.patch.object(create_all_entities, "get_agent_tables", side_effect=hub.agent_tables):
            tables = create_all_entities._source_tables_or_offline("t", "p", "source-agent", "src", yaml_config)
        result = run_create_entities(hub, yaml_text, tables)
    return hub, tables, result


class AllowMissingTablesTests(unittest.TestCase):
    def test_flag_and_env(self):
        parser = argparse.ArgumentParser()
        import_options.add_cli_arguments(parser)
        self.assertTrue(import_options.ImportOptions.from_args(parser.parse_args(["--allow-missing-tables"]))
                        .allow_missing_tables)
        self.assertFalse(import_options.ImportOptions.from_args(parser.parse_args([])).allow_missing_tables)
        with mock.patch.dict("os.environ", {"GS_IMPORT_ALLOW_MISSING_TABLES": "true"}):
            self.assertTrue(import_options.from_env_if_requested().allow_missing_tables)

    def test_default_ignores_a_table_missing_from_the_source(self):
        hub, tables, _result = _run(GHOST_YAML, import_options.ImportOptions())
        self.assertEqual([t["name"] for t in tables], ["ORDERS"])
        self.assertNotIn("src.GHOST", [e["entityName"] for e in hub.saved])

    def test_missing_table_entity_is_built_from_the_yaml(self):
        options = import_options.ImportOptions(allow_missing_tables=True)
        hub, tables, result = _run(GHOST_YAML, options)
        self.assertEqual(sorted(t["name"] for t in tables), ["GHOST", "ORDERS"])
        self.assertEqual(options.report.missing_source_tables, ["src.GHOST"])
        self.assertEqual(result["failed"], 0, result)
        self.assertEqual(len(hub.saved), 2)
        self.assertIn("src.GHOST", options.report.render())

    def test_untyped_column_skips_the_table_with_a_warning(self):
        options = import_options.ImportOptions(allow_missing_tables=True)
        hub, _tables, _result = _run(UNTYPED_YAML, options)
        self.assertEqual(len(hub.saved), 1)
        self.assertTrue(any("LABEL" in w and "never guessed" in w for w in options.report.warnings))
        self.assertEqual(options.report.missing_source_tables, [])

    def test_unreachable_source_uses_the_yaml_tables(self):
        options = import_options.ImportOptions(allow_missing_tables=True)
        with import_options.use(options):
            with mock.patch.object(create_all_entities, "get_agent_tables", side_effect=ConnectionError("down")):
                tables = create_all_entities._source_tables_or_offline(
                    "t", "p", "a", "src", yaml.safe_load(GHOST_YAML))
        self.assertEqual(sorted(t["name"] for t in tables), ["GHOST", "ORDERS"])

    def test_unreachable_source_still_raises_by_default(self):
        with import_options.use(import_options.ImportOptions()):
            with mock.patch.object(create_all_entities, "get_agent_tables", side_effect=ConnectionError("down")):
                config = yaml.safe_load(GHOST_YAML)
                with self.assertRaises(ConnectionError):
                    create_all_entities._source_tables_or_offline("t", "p", "a", "src", config)


class UdfLookupTests(unittest.TestCase):
    def setUp(self):
        import create_user_defined_functions as udf
        self.udf = udf
        self.old = udf.UDF_PATH
        self.addCleanup(setattr, udf, "UDF_PATH", self.old)

    def test_udf_path_is_the_only_folder_searched(self):
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as session:
            (Path(work) / "old-session").mkdir()
            (Path(work) / "old-session" / "UDF_X.java").write_text("stale")
            self.udf.UDF_PATH = session
            with mock.patch.object(self.udf, "get_automator_workspace_root", return_value=Path(work)):
                self.assertIsNone(self.udf.find_udf_definition_in_path("UDF_X", self.udf.UdfFunctionType.java))

    def test_ambiguous_workspace_match_fails_loudly(self):
        with tempfile.TemporaryDirectory() as work:
            for session in ("a", "b"):
                (Path(work) / session).mkdir()
                (Path(work) / session / "UDF_X.java").write_text("x")
            self.udf.UDF_PATH = None
            with mock.patch.object(self.udf, "get_automator_workspace_root", return_value=Path(work)):
                with self.assertRaises(FileNotFoundError):
                    self.udf.find_udf_definition_in_path("UDF_X", self.udf.UdfFunctionType.java)


class ZipSlipTests(unittest.TestCase):
    def _zip(self, name):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr(name, "x")
        return zipfile.ZipFile(io.BytesIO(buffer.getvalue()))

    def test_udf_member_outside_root_is_refused(self):
        from automator_app import app
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("udf-a/../../evil.java", "/abs/udf-a/evil.java", "C:/udf-a/evil.java"):
                archive_file, dest = self._zip(bad), Path(tmp)
                with self.assertRaises(ValueError, msg=bad):
                    app._extract_udf_source(archive_file, bad, dest)
            app._extract_udf_source(self._zip("udf-a/ok.java"), "udf-a/ok.java", Path(tmp))
            self.assertTrue((Path(tmp) / "udf-a" / "ok.java").is_file())

    def test_function_collection_member_stays_under_root(self):
        import function_collection
        with tempfile.TemporaryDirectory() as tmp:
            zf = self._zip("function-collection/../../../evil.java")
            function_collection.extract_function_collection_members(zf, Path(tmp))
            self.assertFalse((Path(tmp).parent / "evil.java").exists())
            for written in Path(tmp).rglob("*"):
                self.assertTrue(written.resolve().is_relative_to(Path(tmp).resolve()))


class PipelineImportTests(unittest.TestCase):
    CONFIG = {"pipelineName": "demo", "agents": [{"agentType": "SOURCE", "agentTag": "s"},
                                                 {"agentType": "TARGET", "agentTag": "t"}]}

    def _import(self, fetch, **kwargs):
        from automator_app import corehub
        with mock.patch.object(corehub, "configure_core_hub"), \
                mock.patch.object(corehub, "fetch_core_hub", side_effect=fetch), \
                mock.patch.object(corehub, "_add_agent_to_pipeline_via_corehub",
                                  side_effect=lambda **k: f"id-{k['agent_user_tag']}"):
            return corehub.import_pipeline_config_only(token="t", base_url="https://hub", use_ssl=True,
                                                       skip_verify=True, config=dict(self.CONFIG), **kwargs)

    def test_existing_name_gets_a_suffix_by_default(self):
        calls = []

        def fetch(path, method="GET", **kwargs):
            calls.append((method, path))
            if path == "/pipelines" and method == "GET":
                return [{"name": "demo"}]
            if path == "/pipelines":
                return {"pipelineId": "p1"}
            return {}
        self.assertEqual(self._import(fetch)["pipelineName"], "demo (restored)")

    def test_existing_name_fail_creates_nothing(self):
        calls = []

        def fetch(path, method="GET", **kwargs):
            calls.append((method, path))
            return [{"name": "demo"}]
        with self.assertRaises(RuntimeError):
            self._import(fetch, on_existing_name="fail")
        self.assertEqual(calls, [("GET", "/pipelines")])

    def test_failure_after_creation_names_the_half_created_pipeline(self):
        from automator_app import corehub
        calls = []

        def fetch(path, method="GET", **kwargs):
            calls.append((method, path))
            if path == "/pipelines" and method == "GET":
                return []
            if path == "/pipelines":
                return {"pipelineId": "p9"}
            if path.endswith("/agents/id-t"):
                raise RuntimeError("bind refused")
            return {}
        with self.assertRaises(corehub.PartialPipelineImportError) as caught:
            self._import(fetch)
        self.assertEqual(caught.exception.pipeline_id, "p9")
        self.assertEqual(len(caught.exception.agents), 1)
        self.assertIn("LEFT IN PLACE", str(caught.exception))
        self.assertNotIn(("DELETE", "/pipelines/p9"), calls)

    def test_cleanup_on_failure_deletes_it(self):
        from automator_app import corehub
        calls = []

        def fetch(path, method="GET", **kwargs):
            calls.append((method, path))
            if path == "/pipelines" and method == "GET":
                return []
            if path == "/pipelines":
                return {"pipelineId": "p9"}
            if "/agents/" in path:
                raise RuntimeError("bind refused")
            return {}
        with self.assertRaises(corehub.PartialPipelineImportError) as caught:
            self._import(fetch, cleanup_on_failure=True)
        self.assertTrue(caught.exception.cleaned_up)
        self.assertIn(("DELETE", "/pipelines/p9"), calls)


PER_TABLE_YAML = {
    "SCH19": {"target": "TGT01", "tables": {"whitelist": ["A", "B"], "custom": {
        "A": {"name": "a", "targetSchema": "TGT02",
              "customProperties": {"target": {"preSnapshotCommand": "DELETE FROM TGT01.a"}}},
        "B": {"name": "b"}}}},
}


class PerTableTargetSchemaTests(unittest.TestCase):
    def test_split_one_pass_per_target_schema(self):
        passes = table_target_schema.split_by_target_schema(PER_TABLE_YAML, "SCH19", "TGT01")
        self.assertEqual([t for t, _ in passes], ["TGT01", "TGT02"])
        self.assertEqual(list(passes[1][1]["SCH19"]["tables"]["custom"]), ["A"])

    def test_old_yaml_without_target_schema_is_one_pass(self):
        old = {"S": {"target": "T", "tables": {"custom": {"A": {}}}}}
        self.assertEqual(table_target_schema.split_by_target_schema(old, "S", "T"), [("T", old)])

    def test_snapshot_command_on_another_schema_warns(self):
        warnings = table_target_schema.snapshot_command_warnings(PER_TABLE_YAML, "SCH19", "TGT01")
        self.assertEqual(len(warnings), 1)
        self.assertIn("TGT01", warnings[0])


class FanInTests(unittest.TestCase):
    YAML = {
        "S1": {"target": "T", "tables": {"custom": {"X": {"name": "ALL", "columns": [
            {"source": "ID", "target": "ID", "type": "int"}, {"source": "A", "target": "A", "type": "varchar"}]}}}},
        "S2": {"target": "T", "tables": {"custom": {"X": {"name": "ALL", "columns": [
            {"source": "ID", "target": "ID", "type": "datetime2"}, {"source": "B", "target": "B", "type": "int"}]}}}},
    }

    def test_type_conflict_and_missing_union_columns(self):
        warnings = fan_in.fan_in_warnings(self.YAML, "S1", "T", lambda s, t: ["ID", "A"])
        self.assertTrue(any("conflicting types" in w and "id" in w.lower() for w in warnings), warnings)
        self.assertTrue(any("lacks union column" in w and " b;" in w.lower() for w in warnings), warnings)

    def test_widenable_types_are_no_conflict(self):
        yaml_config = {
            "S1": {"target": "T", "tables": {"custom": {"X": {"name": "ALL", "columns": [
                {"source": "N", "target": "N", "type": "varchar", "dataLength": 9},
                {"source": "I", "target": "I", "type": "int"}]}}}},
            "S2": {"target": "T", "tables": {"custom": {"X": {"name": "ALL", "columns": [
                {"source": "N", "target": "N", "type": "varchar", "dataLength": 10},
                {"source": "I", "target": "I", "type": "bigint"}]}}}},
        }
        self.assertEqual(fan_in.fan_in_warnings(yaml_config, "S1", "T", None), [])

    def test_merge_types(self):
        self.assertEqual(fan_in.merge_types([("varchar", 9, None, 0), ("varchar", 10, None, 0)])[0][:2], ("varchar", 10))
        self.assertEqual(fan_in.merge_types([("char", 9, None, 0), ("nvarchar", 5, None, 0)])[0][:2], ("nvarchar", 9))
        self.assertEqual(fan_in.merge_types([("numeric", 0, 7, 2), ("numeric", 0, 9, 0)])[0], ("numeric", None, 11, 2))
        self.assertEqual(fan_in.merge_types([("int", None, None, 0), ("numeric", None, 5, 2)])[0], ("numeric", None, 12, 2))
        self.assertIsNone(fan_in.merge_types([("numeric", None, 9, 0), ("varchar", 9, None, 0)])[0])
        self.assertIsNone(fan_in.merge_types([("numeric", None, 7, 0), ("datetime2", None, None, 0)])[0])

    def test_single_feeder_is_quiet(self):
        one = {"S1": self.YAML["S1"]}
        self.assertEqual(fan_in.fan_in_warnings(one, "S1", "T", lambda s, t: []), [])


    def test_same_type_different_params_takes_the_wider(self):
        self.assertEqual(fan_in.merge_types([("float", None, 24, 0), ("float", None, 53, 0)])[0][:3],
                         ("float", None, 53))
        self.assertEqual(fan_in.merge_types([("real", None, None, 0), ("float", None, 53, 0)])[0][0], "float")
        self.assertEqual(fan_in.merge_types([("real", None, None, 0), ("double", None, None, 0)])[0][0], "double")
        self.assertEqual(fan_in.merge_types([("money", None, 10, 4), ("money", None, 19, 4)])[0][2], 19)
        self.assertEqual(fan_in.merge_types([("datetime2", None, 3, 0), ("datetime2", None, 7, 0)])[0][2], 7)

    def test_max_wins(self):
        self.assertEqual(fan_in.merge_types([("varchar", 10, None, 0), ("varchar", -1, None, 0)])[0][1], -1)


class FanInCreationTests(unittest.TestCase):
    """Review #9: nullability merged across feeders, VARCHAR(MAX) kept through the merge."""

    @staticmethod
    def _columns(*rows):
        import create_all_tables as cat
        out = []
        for name, kind, length, nullable, pk in rows:
            raw = length
            col = cat.Column(name=name, dataType=kind, id=0, tableId=0, position=len(out) + 1, isPK=pk,
                             isNullable=nullable, charMaxLength=cat._normalize_data_length(kind, length))
            out.append(cat._mark_max(raw, col))
        return out

    @staticmethod
    def _peer(*rows):
        return {"columns": [{"source": n, "target": n, "targetDataType": k, "targetDataLength": l,
                             "isNullable": nl} for n, k, l, nl in rows]}

    def _apply(self, own, peers):
        import create_all_tables as cat
        with mock.patch.object(cat, "_exported_target_type", side_effect=lambda row, *_a: row["targetDataType"]):
            return {c.name: c for c in cat._apply_fan_in(own, {"columns": []}, peers, "T.ALL", cat._KeyLookup(["ID"]),
                                                         None, None)}

    def test_not_null_relaxed_when_another_feeder_lacks_or_nulls_it(self):
        own = self._columns(("ID", "int", 0, False, True), ("X", "varchar", 10, False, False),
                            ("Y", "varchar", 10, False, False), ("Z", "varchar", 10, False, False))
        peers = [self._peer(("ID", "int", 0, False), ("Y", "varchar", 10, True), ("Z", "varchar", 10, False))]
        result = self._apply(own, peers)
        self.assertTrue(result["X"].isNullable)   # the peer lacks X
        self.assertTrue(result["Y"].isNullable)   # the peer has Y nullable
        self.assertFalse(result["Z"].isNullable)  # NOT NULL everywhere
        self.assertFalse(result["ID"].isNullable)

    def test_varchar_max_survives_the_merge(self):
        own = self._columns(("ID", "int", 0, False, True), ("X", "varchar", 10, True, False))
        peers = [self._peer(("ID", "int", 0, False), ("X", "varchar", -1, True))]
        self.assertEqual(self._apply(own, peers)["X"].charMaxLength, -1)
        own = self._columns(("ID", "int", 0, False, True), ("X", "varchar", -1, True, False))
        peers = [self._peer(("ID", "int", 0, False), ("X", "varchar", 2000, True))]
        self.assertEqual(self._apply(own, peers)["X"].charMaxLength, -1)

    def test_widest_finite_length_without_max(self):
        own = self._columns(("ID", "int", 0, False, True), ("X", "varchar", 10, True, False))
        peers = [self._peer(("ID", "int", 0, False), ("X", "varchar", 2000, True))]
        self.assertEqual(self._apply(own, peers)["X"].charMaxLength, 2000)


if __name__ == "__main__":
    unittest.main()
