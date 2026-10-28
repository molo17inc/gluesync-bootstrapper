"""Offline gap-fill and bug repros for MR !69 — always run (no live hub)."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import urllib.parse
import zipfile
from pathlib import Path
from unittest import mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import agent_overrides  # noqa: E402
import create_all_tables as cat  # noqa: E402
import table_target_schema as tts  # noqa: E402
from automator_app import import_backup  # noqa: E402

from .zip_builder import (
    E2E_UDF_JAVA,
    E2E_UDF_NAME,
    archive_with_entities,
    archive_with_udf,
    entities_dict,
    entities_with_udf,
    minimal_create_table_entities,
    synth_archive,
)


def _cfg(*commands):
    out = {"preSnapshotCommand": commands[0]}
    if len(commands) > 1:
        out["postSnapshotCommand"] = commands[1]
    return out


class TestInferenceGaps:
    """Dialect / edge cases for target-schema inference (extends test_target_schema_inference)."""

    def test_mysql_backticks_two_and_three_part(self):
        assert tts.inferred_target_schema(_cfg("DELETE FROM `TGT02`.`TAB050`"), "TGT01") == "TGT02"
        assert tts.inferred_target_schema(_cfg("DELETE FROM `DB1`.`TGT02`.`TAB050`"), "TGT01") == "TGT02"
        assert tts.inferred_target_schema(_cfg("DELETE FROM TGT02.`Tab 50`"), "TGT01") == "TGT02"

    def test_snapshot_delete_filter_alone_does_not_infer(self):
        """snapshotDeleteFilter is clause-based in exports; inference only walks pre/postSnapshotCommand."""
        assert tts.inferred_target_schema({"snapshotDeleteFilter": "DELETE FROM TGT02.TAB050"}, "TGT01") is None
        assert tts.inferred_target_schema({
            "snapshotDeleteFilter": {"type": "AND", "clauses": []},
        }, "TGT01") is None

    def test_comments_in_filters_still_infer(self):
        cmd = "/* lead */ DELETE FROM TGT02.TAB050 -- trail\n"
        assert tts.inferred_target_schema(_cfg(cmd), "TGT01") == "TGT02"

    def test_quoted_three_part_with_spaces(self):
        assert tts.inferred_target_schema(
            _cfg('DELETE FROM "DB One"."Tgt Two"."Tab 50"'), "TGT01") == "Tgt Two"

    def test_ambiguous_double_dot_skipped(self):
        assert tts.inferred_target_schema(_cfg("DELETE FROM DB1..TAB050"), "TGT01") is None


class TestSchemaUrlEncodingBug:
    """create_all_tables URL-encodes schema and table names (spaces/unicode/reserved)."""

    def test_table_exists_url_encodes_schema_with_spaces(self, monkeypatch):
        captured = []

        def fake_fetch(path, method="GET", token=None, expected_error_statuses=None, **kwargs):
            captured.append(path)
            raise RuntimeError("404 not found")

        monkeypatch.setattr(cat, "fetch_core_hub", fake_fetch)
        monkeypatch.setattr(cat, "CREATE_TABLE_IF_NOT_EXISTS", True)
        assert cat.table_exists("pipe1", "Tgt Two", "Tab 50", "tok") is False
        assert captured, "fetch_core_hub was not called"
        assert "Tgt%20Two" in captured[0], f"schema not encoded in {captured[0]!r}"
        assert "Tab%2050" in captured[0]

    def test_generate_create_table_statement_encodes_schema(self, monkeypatch):
        captured = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            captured.append(path)
            return "CREATE TABLE ..."

        monkeypatch.setattr(cat, "fetch_core_hub", fake_fetch)
        req = cat.GenerateCreateTargetTableStatementRequest(columns=[
            cat.Column(id=1, tableId=1, name="ID", dataType="INTEGER", isPK=True, isNullable=False, position=1),
        ])
        cat.generate_create_table_statement("pipe1", "Tgt Two", "Tab 50", "tok", req)
        assert "Tgt%20Two" in captured[0]


class TestAgentOverridesOffline:
    def test_password_key_refused(self):
        payload = json.dumps({"mssql-ct": {"password": "nope"}})
        with pytest.raises(agent_overrides.AgentOverrideError) as exc:
            agent_overrides.load(payload)
        assert "password" in str(exc.value).lower()
        assert "GS_IMPORT_AGENT_PASSWORD_MSSQL_CT" in str(exc.value)

    def test_password_env_name_applied(self, monkeypatch):
        monkeypatch.setenv("GS_E2E_FAKE_AGENT_PW", "synth-only-not-real")
        overrides = agent_overrides.load(json.dumps({
            "mssql-ct": {"host": "192.0.2.10", "passwordEnv": "GS_E2E_FAKE_AGENT_PW"},
        }))
        agents = [{"agentTag": "mssql-ct", "hostCredentials": {"host": "old.example.invalid", "password": "<PASSWORD>"}}]
        notes = agent_overrides.apply(agents, overrides)
        assert agents[0]["hostCredentials"]["host"] == "192.0.2.10"
        assert agents[0]["hostCredentials"]["password"] == "synth-only-not-real"
        assert notes


class TestImportBackupCliOffline:
    def test_write_plan_exit_0(self, tmp_path):
        zip_path = tmp_path / "synth1.zip"
        zip_path.write_bytes(synth_archive("synth1"))
        plan_path = tmp_path / "plan.json"
        code = import_backup.main([str(zip_path), "--write-plan", str(plan_path)])
        assert code == 0
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        assert "pipelines" in plan
        assert "5e1f0001" in plan["pipelines"]

    def test_missing_password_exit_1(self, tmp_path, monkeypatch):
        zip_path = tmp_path / "synth1.zip"
        zip_path.write_bytes(synth_archive("synth1"))
        monkeypatch.delenv("GS_COREHUB_PASSWORD", raising=False)
        monkeypatch.delenv("GS_E2E_HUB_PASSWORD", raising=False)
        code = import_backup.main([
            str(zip_path),
            "--base-url", "https://192.0.2.20:1717",
            "--password-env", "GS_COREHUB_PASSWORD_MISSING_FOR_TEST",
        ])
        assert code == 1

    def test_exit_codes_map(self):
        assert import_backup.EXIT_CODES == {"full": 0, "failed": 1, "partial": 2}

    def test_bad_zip_write_plan_exit_1(self, tmp_path):
        zip_path = tmp_path / "bad.zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "not a backup")
        zip_path.write_bytes(buf.getvalue())
        code = import_backup.main([str(zip_path), "--write-plan", str(tmp_path / "p.json")])
        assert code == 1


class TestCreateTableHandlerOffline:
    def test_existing_table_untouched(self, monkeypatch):
        monkeypatch.setattr(cat, "table_exists", lambda **kwargs: True)
        monkeypatch.setattr(cat, "CREATE_TABLE_IF_NOT_EXISTS", True)
        created = cat.handle_table_creation(
            "p", "TAB", "SCH", ["ID"], "tok", [], {"columns": [{"source": "ID", "destination": "ID"}]},
            {"type": "mssql"}, {"type": "mssql"},
        )
        assert created is False

    def test_flag_off_skips_creation(self, monkeypatch):
        monkeypatch.setattr(cat, "table_exists", lambda **kwargs: False)
        monkeypatch.setattr(cat, "CREATE_TABLE_IF_NOT_EXISTS", False)
        created = cat.handle_table_creation(
            "p", "TAB", "SCH", ["ID"], "tok", [], {"columns": [{"source": "ID", "destination": "ID"}]},
            {"type": "mssql"}, {"type": "mssql"},
        )
        assert created is False

    def test_no_columns_raises(self, monkeypatch):
        monkeypatch.setattr(cat, "table_exists", lambda **kwargs: False)
        monkeypatch.setattr(cat, "CREATE_TABLE_IF_NOT_EXISTS", True)
        # Discovery shape is a dict with "columns" (see commons.get_table_columns / _source_column_dtos).
        with pytest.raises(cat.TargetTableCreationError):
            cat.handle_table_creation(
                "p", "TAB", "SCH", ["ID"], "tok", {"columns": []}, {"columns": []},
                {"type": "mssql"}, {"type": "mssql"},
            )

    def test_bare_list_discovery_columns_raises_target_table_creation_error(self, monkeypatch):
        monkeypatch.setattr(cat, "table_exists", lambda **kwargs: False)
        monkeypatch.setattr(cat, "CREATE_TABLE_IF_NOT_EXISTS", True)
        with pytest.raises(cat.TargetTableCreationError):
            cat.handle_table_creation(
                "p", "TAB", "SCH", ["ID"], "tok", [], {"columns": []},
                {"type": "mssql"}, {"type": "mssql"},
            )


class TestMinimalZipBuilder:
    def test_minimal_entities_roundtrip_yaml(self):
        entities = minimal_create_table_entities(
            pre_snapshot="DELETE FROM [E2E_TGT2].[E2E_TAB_001]",
        )
        assert tts.inferred_target_schema(
            entities["schemas"]["E2E_SRC"]["tables"]["custom"]["E2E_TAB_001"],
            "E2E_TGT",
        ) == "E2E_TGT2"
        data = archive_with_entities(entities)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert "entities.yaml" in zf.namelist()
            loaded = yaml.safe_load(zf.read("entities.yaml"))
        assert "E2E_SRC" in loaded["schemas"]


def test_agent_overrides_custom_host_credentials_mysql():
    """MySQL allowPublicKeyRetrieval merges into customHostCredentials (no password keys)."""
    import agent_overrides
    agents = [{
        "agentType": "TARGET",
        "agentTag": "mysql-cdc",
        "agentId": "a1",
        "hostCredentials": {"host": "old", "port": 1, "databaseName": "d", "username": "u"},
        "customHostCredentials": {},
    }]
    ov = agent_overrides.load(
        '{"mysql-cdc": {"host": "h", "port": 3306, "customHostCredentials": {"allowPublicKeyRetrieval": true}}}'
    )
    lines = agent_overrides.apply(agents, ov, env={})
    assert agents[0]["customHostCredentials"]["allowPublicKeyRetrieval"] is True
    assert any("customHostCredentials.allowPublicKeyRetrieval" in ln for ln in lines)
    # password key inside customHostCredentials refused
    import pytest
    with pytest.raises(agent_overrides.AgentOverrideError):
        agent_overrides.load(
            '{"mysql-cdc": {"customHostCredentials": {"password": "x"}}}'
        )


class TestUdfArchiveLayout:
    """Synthetic backup with 2.3 isSnapshot UDF under udf-*/."""

    def test_archive_with_udf_contains_java_and_attachment(self):
        entities = minimal_create_table_entities(table="E2E_TAB_UDF1")
        data = archive_with_udf(entities, source_tag="mssql-ct", target_tag="mysql-cdc")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            assert "agents-config.yaml" in names
            assert "entities.yaml" in names
            udf_members = [n for n in names if n.endswith(f"{E2E_UDF_NAME}.java")]
            assert udf_members, names
            assert any(n.startswith("udf-") for n in udf_members)
            code = zf.read(udf_members[0]).decode("utf-8")
            assert "isSnapshot" in code
            assert "boolean isSnapshot" in code
            ents = yaml.safe_load(zf.read("entities.yaml"))
        attached = entities_with_udf(entities)
        # zip already attaches via archive_with_udf
        custom = ents["schemas"]["E2E_SRC"]["tables"]["custom"]["E2E_TAB_UDF1"]
        udf = custom["customProperties"]["target"]["udf"]
        assert udf[0]["name"] == E2E_UDF_NAME
        assert udf[0]["type"] == "Java"
        assert "isSnapshot" in E2E_UDF_JAVA

    def test_entities_with_udf_does_not_mutate_input(self):
        entities = minimal_create_table_entities(table="E2E_TAB_UDF2")
        before = yaml.safe_dump(entities)
        _ = entities_with_udf(entities)
        assert yaml.safe_dump(entities) == before
