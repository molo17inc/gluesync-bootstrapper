#!/usr/bin/env python3
"""
Regression tests for pipeline identity resolution in backup export/import.

Covers the bug where the import-all flow extracted the pipeline ID from the
backup filename using rsplit("_", 1), which broke once timestamps were added
to filenames (backup_<name>_<pipelineId>_<YYYYMMDD>_<HHMMSS>.yaml). The wrong
ID (the time segment) failed the per-pipeline agent mapping lookup, causing
ALL agents from agents-config.yaml to be attached to EVERY imported pipeline.

Covers:
- _extract_pipeline_id_from_backup_stem (filename parsing, all formats)
- _extract_export_metadata_from_yaml_text (embedded exportMetadata parsing)
- export_pipeline_yaml embeds exportMetadata as a first-class YAML key
- /api/import/all resolves identity from exportMetadata and binds only the
  agents belonging to each pipeline (full endpoint regression test)
- exportMetadata is never mistaken for a schema by YAML schema extraction
"""

import io
import logging
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import yaml

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# _extract_pipeline_id_from_backup_stem
# ---------------------------------------------------------------------------
class ExtractPipelineIdFromBackupStemTests(unittest.TestCase):
    """Filename-based pipeline ID extraction (legacy fallback path)."""

    @classmethod
    def setUpClass(cls):
        import automator_app.app as app_module

        cls.extract = staticmethod(app_module._extract_pipeline_id_from_backup_stem)

    # -- current export format: backup_<name>_<id>_<YYYYMMDD>_<HHMMSS> ------
    def test_current_format_with_timestamp(self):
        self.assertEqual(
            self.extract("backup_n8n Backoffice to Jira issues_58f2fe4b_20260610_112414"),
            "58f2fe4b",
        )

    def test_current_format_simple_name(self):
        self.assertEqual(
            self.extract("backup_1st pipeline_2d5eb205_20260610_112407"),
            "2d5eb205",
        )

    def test_current_format_name_with_underscores(self):
        self.assertEqual(
            self.extract("backup_my_pipe_line_abc12345_20260610_112414"),
            "abc12345",
        )

    def test_current_format_numeric_pipeline_id(self):
        # An all-numeric pipeline ID that is NOT 8 digits must survive
        self.assertEqual(
            self.extract("backup_pipe_1234567_20260610_112414"),
            "1234567",
        )

    def test_regression_does_not_return_time_segment(self):
        """The original bug: rsplit('_', 1) returned the HHMMSS segment."""
        result = self.extract("backup_n8n Backoffice to Jira issues_58f2fe4b_20260610_112414")
        self.assertNotEqual(result, "112414")
        self.assertNotEqual(result, "20260610")

    # -- legacy formats ------------------------------------------------------
    def test_legacy_format_name_and_id(self):
        self.assertEqual(self.extract("backup_My Pipe_abc12345"), "abc12345")

    def test_legacy_format_id_only(self):
        self.assertEqual(self.extract("backup_abc12345"), "abc12345")

    def test_legacy_six_digit_numeric_id_not_stripped(self):
        # Only two segments: cannot be a timestamp pair, keep as ID
        self.assertEqual(self.extract("backup_name_123456"), "123456")

    def test_legacy_eight_digit_numeric_id_not_stripped(self):
        # 8-digit last segment alone is not a timestamp pair
        self.assertEqual(self.extract("backup_name_12345678"), "12345678")

    # -- timestamp detection edge cases --------------------------------------
    def test_timestamp_requires_both_date_and_time_segments(self):
        # date-like segment followed by a non-time segment: not stripped
        self.assertEqual(self.extract("backup_x_20260610_abc123"), "abc123")
        # time-like last segment without date-like before it: not stripped
        self.assertEqual(self.extract("backup_x_abcdefgh_112414"), "112414")

    def test_timestamp_stripped_only_once(self):
        # Name itself contains date-like segments; only trailing pair stripped
        self.assertEqual(
            self.extract("backup_20260101_123456_abc12345_20260610_112414"),
            "abc12345",
        )

    # -- invalid inputs -------------------------------------------------------
    def test_non_backup_prefix_returns_none(self):
        self.assertIsNone(self.extract("export_foo_bar"))
        self.assertIsNone(self.extract("agents-config"))
        self.assertIsNone(self.extract("global-config"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(self.extract(""))

    def test_bare_backup_prefix_returns_none(self):
        self.assertIsNone(self.extract("backup_"))


# ---------------------------------------------------------------------------
# _extract_export_metadata_from_yaml_text
# ---------------------------------------------------------------------------
class ExtractExportMetadataFromYamlTextTests(unittest.TestCase):
    """Embedded exportMetadata parsing (preferred identity source)."""

    @classmethod
    def setUpClass(cls):
        import automator_app.app as app_module

        cls.extract = staticmethod(app_module._extract_export_metadata_from_yaml_text)

    def test_full_metadata(self):
        text = (
            "exportMetadata:\n"
            "  pipelineId: 58f2fe4b\n"
            "  pipelineName: n8n Backoffice to Jira issues\n"
            "dbo:\n"
            "  target: public\n"
        )
        self.assertEqual(
            self.extract(text), ("58f2fe4b", "n8n Backoffice to Jira issues")
        )

    def test_metadata_with_comment_header(self):
        text = (
            "# ---------------------------------------------------------------\n"
            "# Gluesync Automator – Pipeline Configuration Export\n"
            "# Pipeline ID     : 58f2fe4b\n"
            "# ---------------------------------------------------------------\n"
            "exportMetadata:\n"
            "  pipelineId: 58f2fe4b\n"
            "  pipelineName: My Pipe\n"
        )
        self.assertEqual(self.extract(text), ("58f2fe4b", "My Pipe"))

    def test_partial_metadata_id_only(self):
        text = "exportMetadata:\n  pipelineId: abc123\n"
        self.assertEqual(self.extract(text), ("abc123", None))

    def test_partial_metadata_name_only(self):
        text = "exportMetadata:\n  pipelineName: Only Name\n"
        self.assertEqual(self.extract(text), (None, "Only Name"))

    def test_numeric_pipeline_id_coerced_to_string(self):
        text = "exportMetadata:\n  pipelineId: 12345678\n  pipelineName: N\n"
        pipeline_id, _ = self.extract(text)
        self.assertEqual(pipeline_id, "12345678")
        self.assertIsInstance(pipeline_id, str)

    def test_missing_metadata_returns_none_pair(self):
        text = "dbo:\n  target: public\n  tables:\n    whitelist: []\n"
        self.assertEqual(self.extract(text), (None, None))

    def test_metadata_not_a_dict_returns_none_pair(self):
        self.assertEqual(self.extract("exportMetadata: just-a-string\n"), (None, None))
        self.assertEqual(self.extract("exportMetadata:\n- a\n- b\n"), (None, None))

    def test_non_dict_root_returns_none_pair(self):
        self.assertEqual(self.extract("- a\n- b\n"), (None, None))
        self.assertEqual(self.extract("just a scalar"), (None, None))
        self.assertEqual(self.extract(""), (None, None))

    def test_empty_mapping_returns_none_pair(self):
        self.assertEqual(self.extract("{}"), (None, None))

    def test_invalid_yaml_returns_none_pair(self):
        self.assertEqual(self.extract("foo: [unclosed\nbar: : :"), (None, None))

    def test_empty_metadata_values_return_none(self):
        text = "exportMetadata:\n  pipelineId: ''\n  pipelineName: ''\n"
        self.assertEqual(self.extract(text), (None, None))


# ---------------------------------------------------------------------------
# export_pipeline_yaml embeds exportMetadata
# ---------------------------------------------------------------------------
class ExportPipelineYamlMetadataTests(unittest.TestCase):
    """Verify exports embed pipeline identity as a first-class YAML key."""

    def _run_export(self, *, pipeline_fetch=None, schemas=None):
        import automator_app.corehub as corehub

        schemas = schemas if schemas is not None else {
            "dbo": {
                "target": "public",
                "tables": {"whitelist": set(), "custom": {}},
            }
        }

        def fake_fetch(path, method="GET", token=None, **kwargs):
            if path == "/pipelines/pipe-1":
                if pipeline_fetch is not None:
                    return pipeline_fetch()
                return {"id": "pipe-1", "name": "My Pipeline"}
            return {}

        patches = [
            mock.patch.object(corehub, "configure_core_hub"),
            mock.patch.object(corehub, "fetch_pipeline_entities", return_value=[]),
            mock.patch.object(corehub, "build_entities_maps", return_value={}),
            mock.patch.object(corehub, "fetch_groups_map", return_value=({}, {}, {})),
            mock.patch.object(corehub, "build_schemas_from_entities", return_value=schemas),
            mock.patch.object(corehub, "infer_agent_schema_types", return_value=("SQL", "SQL")),
            mock.patch.object(corehub, "fetch_pipeline_jobs", return_value=[]),
            mock.patch.object(corehub, "attach_schedules_from_jobs"),
            mock.patch.object(corehub, "enrich_null_column_types_from_discovery", return_value=0),
            mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch),
            mock.patch.object(corehub, "build_export_header", return_value="# header\n"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        return corehub.export_pipeline_yaml(
            token="tok",
            base_url="http://test",
            pipeline_id="pipe-1",
            use_ssl=False,
            skip_verify=False,
            include_global_config=False,
        )

    def test_export_embeds_pipeline_id_and_name(self):
        yaml_text = self._run_export()
        parsed = yaml.safe_load(yaml_text)

        self.assertIn("exportMetadata", parsed)
        self.assertEqual(parsed["exportMetadata"]["pipelineId"], "pipe-1")
        self.assertEqual(parsed["exportMetadata"]["pipelineName"], "My Pipeline")

    def test_export_metadata_is_first_key(self):
        yaml_text = self._run_export()
        parsed = yaml.safe_load(yaml_text)
        self.assertEqual(next(iter(parsed)), "exportMetadata")

    def test_export_preserves_schema_content(self):
        yaml_text = self._run_export()
        parsed = yaml.safe_load(yaml_text)
        self.assertIn("dbo", parsed)
        self.assertEqual(parsed["dbo"]["target"], "public")

    def test_export_with_empty_schemas_still_carries_metadata(self):
        # Previously empty pipelines exported as bare {}; now identity survives
        yaml_text = self._run_export(schemas={})
        parsed = yaml.safe_load(yaml_text)
        self.assertEqual(parsed["exportMetadata"]["pipelineId"], "pipe-1")

    def test_name_fetch_failure_falls_back_to_pipeline_id(self):
        def boom():
            raise RuntimeError("CoreHub unavailable")

        yaml_text = self._run_export(pipeline_fetch=boom)
        parsed = yaml.safe_load(yaml_text)
        self.assertEqual(parsed["exportMetadata"]["pipelineId"], "pipe-1")
        self.assertEqual(parsed["exportMetadata"]["pipelineName"], "pipe-1")

    def test_roundtrip_export_then_import_metadata(self):
        import automator_app.app as app_module

        yaml_text = self._run_export()
        pipeline_id, pipeline_name = app_module._extract_export_metadata_from_yaml_text(yaml_text)
        self.assertEqual(pipeline_id, "pipe-1")
        self.assertEqual(pipeline_name, "My Pipeline")


# ---------------------------------------------------------------------------
# exportMetadata must never be mistaken for a schema
# ---------------------------------------------------------------------------
class ExportMetadataSchemaIsolationTests(unittest.TestCase):
    """YAML schema extraction helpers must ignore the exportMetadata key."""

    YAML_WITH_METADATA = (
        "exportMetadata:\n"
        "  pipelineId: abc123\n"
        "  pipelineName: Pipe\n"
        "dbo:\n"
        "  target: public\n"
        "  sourceType: SQL\n"
        "  targetType: SQL\n"
        "  tables:\n"
        "    whitelist: []\n"
        "    custom: {}\n"
    )

    def _write_tmp(self, text):
        import tempfile

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        )
        tmp.write(text)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name

    def test_extract_all_schemas_ignores_export_metadata(self):
        from commons import extract_all_schemas_from_yaml

        path = self._write_tmp(self.YAML_WITH_METADATA)
        pairs = extract_all_schemas_from_yaml(path)
        self.assertEqual(pairs, [("dbo", "public")])
        self.assertNotIn(
            "exportMetadata", [src for src, _ in pairs]
        )

    def test_extract_schema_types_ignores_export_metadata(self):
        from commons import extract_schema_types_from_yaml

        path = self._write_tmp(self.YAML_WITH_METADATA)
        source_type, target_type = extract_schema_types_from_yaml(path)
        self.assertEqual(source_type, "SQL")
        self.assertEqual(target_type, "SQL")

    def test_metadata_only_yaml_yields_no_schemas(self):
        from commons import extract_all_schemas_from_yaml

        path = self._write_tmp(
            "exportMetadata:\n  pipelineId: abc123\n  pipelineName: Pipe\n"
        )
        self.assertEqual(extract_all_schemas_from_yaml(path), [])


# ---------------------------------------------------------------------------
# /api/import/all endpoint regression test
# ---------------------------------------------------------------------------
def _build_backup_zip(pipeline_yamls: dict, agents_config: dict) -> bytes:
    """Build an in-memory Export All ZIP: {filename: yaml_text} + agents-config."""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "agents-config.yaml",
            yaml.safe_dump(agents_config, sort_keys=False),
        )
        for filename, text in pipeline_yamls.items():
            zf.writestr(filename, text)
    return buf.getvalue()


def _pipeline_yaml(pipeline_id: str, pipeline_name: str) -> str:
    return yaml.safe_dump(
        {
            "exportMetadata": {
                "pipelineId": pipeline_id,
                "pipelineName": pipeline_name,
            },
            "dbo": {
                "target": "public",
                "sourceType": "SQL",
                "targetType": "SQL",
                "tables": {"whitelist": [], "custom": {}},
            },
        },
        sort_keys=False,
    )


AGENTS_CONFIG = {
    "agents": [
        {
            "agentType": "SOURCE",
            "agentTag": "mssql-cdc",
            "agentId": "src-1",
            "hostCredentials": {"host": "1.1.1.1", "password": "p1"},
            "customHostCredentials": {},
            "specificConfiguration": {},
        },
        {
            "agentType": "TARGET",
            "agentTag": "mariadb-cdc",
            "agentId": "tgt-1",
            "hostCredentials": {"host": "2.2.2.2", "password": "p2"},
            "customHostCredentials": {},
            "specificConfiguration": {},
        },
        {
            "agentType": "SOURCE",
            "agentTag": "mssql-cdc",
            "agentId": "src-2",
            "hostCredentials": {"host": "3.3.3.3", "password": "p3"},
            "customHostCredentials": {},
            "specificConfiguration": {},
        },
        {
            "agentType": "TARGET",
            "agentTag": "postgresql-cdc",
            "agentId": "tgt-2",
            "hostCredentials": {"host": "4.4.4.4", "password": "p4"},
            "customHostCredentials": {},
            "specificConfiguration": {},
        },
    ],
    "pipelines": [
        {
            "pipelineId": "aaaa1111",
            "pipelineName": "First Pipeline",
            "agents": [
                {"agentType": "SOURCE", "agentTag": "mssql-cdc", "agentId": "src-1"},
                {"agentType": "TARGET", "agentTag": "mariadb-cdc", "agentId": "tgt-1"},
            ],
        },
        {
            "pipelineId": "bbbb2222",
            "pipelineName": "Second Pipeline",
            "agents": [
                {"agentType": "SOURCE", "agentTag": "mssql-cdc", "agentId": "src-2"},
                {"agentType": "TARGET", "agentTag": "postgresql-cdc", "agentId": "tgt-2"},
            ],
        },
    ],
}


class ImportAllPipelineIdentityTests(unittest.TestCase):
    """End-to-end regression test for /api/import/all agent selection.

    The original bug attached all agents to every pipeline because the
    pipeline ID parsed from the filename did not match agents-config.yaml.
    """

    @classmethod
    def setUpClass(cls):
        import automator_app.app as app_module
        from automator_app.state import state
        from fastapi.testclient import TestClient

        cls.app_module = app_module
        cls.state = state
        cls.client = TestClient(app_module.create_app())

    def setUp(self):
        self.state.set_auth("tok", "http://test", use_ssl=False, skip_verify=False)
        self.addCleanup(self.state.clear_auth)

        import automator_app.corehub as corehub

        self.imported_configs = []

        def fake_import_config_only(**kwargs):
            cfg = kwargs["config"]
            self.imported_configs.append(cfg)
            idx = len(self.imported_configs)
            return {
                "pipelineId": f"new-{idx}",
                "pipelineName": cfg.get("pipelineName"),
            }

        patches = [
            mock.patch.object(
                corehub, "import_pipeline_config_only",
                side_effect=fake_import_config_only,
            ),
            mock.patch.object(
                corehub, "run_create_entities", return_value={"success": True}
            ),
            mock.patch.object(corehub, "fetch_core_hub", return_value={}),
            mock.patch.object(
                self.app_module.create_user_defined_functions, "main", return_value=None
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _post_zip(self, zip_bytes: bytes):
        return self.client.post(
            "/api/import/all",
            files={"file": ("backup.zip", zip_bytes, "application/zip")},
        )

    def test_each_pipeline_gets_only_its_own_agents(self):
        zip_bytes = _build_backup_zip(
            {
                "backup_First Pipeline_aaaa1111_20260610_112407.yaml": _pipeline_yaml(
                    "aaaa1111", "First Pipeline"
                ),
                "backup_Second Pipeline_bbbb2222_20260610_112414.yaml": _pipeline_yaml(
                    "bbbb2222", "Second Pipeline"
                ),
            },
            AGENTS_CONFIG,
        )

        response = self._post_zip(zip_bytes)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.imported_configs), 2)

        by_name = {c["pipelineName"]: c for c in self.imported_configs}
        self.assertIn("First Pipeline", by_name)
        self.assertIn("Second Pipeline", by_name)

        first_agents = by_name["First Pipeline"]["agents"]
        second_agents = by_name["Second Pipeline"]["agents"]

        # Regression: each pipeline must receive exactly 2 agents, not all 4
        self.assertEqual(len(first_agents), 2)
        self.assertEqual(len(second_agents), 2)

        self.assertEqual(
            {a["agentId"] for a in first_agents}, {"src-1", "tgt-1"}
        )
        self.assertEqual(
            {a["agentId"] for a in second_agents}, {"src-2", "tgt-2"}
        )

    def test_pipeline_names_restored_from_metadata(self):
        zip_bytes = _build_backup_zip(
            {
                "backup_First Pipeline_aaaa1111_20260610_112407.yaml": _pipeline_yaml(
                    "aaaa1111", "First Pipeline"
                ),
            },
            AGENTS_CONFIG,
        )

        response = self._post_zip(zip_bytes)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.imported_configs[0]["pipelineName"], "First Pipeline")
        # Regression: must not fall back to "Imported pipeline <HHMMSS>"
        self.assertNotIn("112407", response.json()["message"])

    def test_renamed_backup_file_still_resolved_via_metadata(self):
        """Filename is unparsable, but embedded exportMetadata saves the day."""

        zip_bytes = _build_backup_zip(
            {"some-renamed-file.yaml": _pipeline_yaml("bbbb2222", "Second Pipeline")},
            AGENTS_CONFIG,
        )

        response = self._post_zip(zip_bytes)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.imported_configs), 1)

        cfg = self.imported_configs[0]
        self.assertEqual(cfg["pipelineName"], "Second Pipeline")
        self.assertEqual(
            {a["agentId"] for a in cfg["agents"]}, {"src-2", "tgt-2"}
        )

    def test_legacy_backup_without_metadata_uses_filename(self):
        """Legacy YAML (no exportMetadata) still resolves via filename parsing."""

        legacy_yaml = yaml.safe_dump(
            {
                "dbo": {
                    "target": "public",
                    "sourceType": "SQL",
                    "targetType": "SQL",
                    "tables": {"whitelist": [], "custom": {}},
                }
            },
            sort_keys=False,
        )
        zip_bytes = _build_backup_zip(
            {"backup_First Pipeline_aaaa1111_20260610_112407.yaml": legacy_yaml},
            AGENTS_CONFIG,
        )

        response = self._post_zip(zip_bytes)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.imported_configs), 1)

        cfg = self.imported_configs[0]
        self.assertEqual(cfg["pipelineName"], "First Pipeline")
        self.assertEqual(
            {a["agentId"] for a in cfg["agents"]}, {"src-1", "tgt-1"}
        )

    def test_unknown_pipeline_falls_back_to_all_agents(self):
        """No metadata match at all: legacy fallback binds the full agent list."""

        zip_bytes = _build_backup_zip(
            {"backup_Mystery_99999999x_20260610_112407.yaml": _pipeline_yaml(
                "99999999x", "Mystery"
            )},
            AGENTS_CONFIG,
        )

        response = self._post_zip(zip_bytes)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.imported_configs), 1)
        # Documented fallback behavior: all agents are bound when the
        # pipeline cannot be matched in agents-config.yaml
        self.assertEqual(len(self.imported_configs[0]["agents"]), 4)


if __name__ == "__main__":
    unittest.main()
