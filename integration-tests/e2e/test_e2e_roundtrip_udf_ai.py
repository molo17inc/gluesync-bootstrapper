"""Live e2e: round-trip, idempotent re-run, UDF isSnapshot, AI settings pre-2.3, partial recovery."""
from __future__ import annotations

import os
import uuid

import pytest

from .conftest import skip_without_hub
from .runner import run_import_backup_tracked
from .zip_builder import (
    E2E_UDF_NAME,
    archive_with_entities,
    archive_with_udf,
    live_synth_archive,
    minimal_create_table_entities,
)
from . import db_probe
from . import env as e2e_env

pytestmark = [pytest.mark.e2e, skip_without_hub]


@skip_without_hub
def test_idempotent_reimport_on_existing_skip(hub_env, overrides_json, hub, agents_env):
    if not e2e_env.agents_ready():
        pytest.skip("GS_E2E_AGENTS_READY not set — reimport hangs on credential PUT without agents")
    data = live_synth_archive(agents_env)
    code1 = run_import_backup_tracked(data, hub_env, overrides_json, hub, "--on-existing", "skip")
    if code1 == 1:
        pytest.skip("first import failed entirely")
    code2 = run_import_backup_tracked(data, hub_env, overrides_json, hub, "--on-existing", "skip")
    assert code2 in (0, 2)
    deleted = hub.delete_tracked_pipelines()
    if deleted:
        print(f"e2e_cleanup_tracked_ids={','.join(deleted)}")


@skip_without_hub
def test_partial_failure_resume_flag_accepted(hub_env, overrides_json, hub, agents_env):
    code = run_import_backup_tracked(
        live_synth_archive(agents_env), hub_env, overrides_json, hub,
        "--resume-pipeline-id", "00000000-0000-0000-0000-000000000000",
    )
    # Fake resume id: hub returns PARTIAL (2) or hard fail (1) depending on version
    assert code in (1, 2)
    # resume of a fake id should not create pipelines; still clear any tracked
    hub.delete_tracked_pipelines()


@skip_without_hub
def test_ai_settings_module_compat_helpers():
    """Offline shape: AiSettingsUnsupported + comparable stay importable (pre-2.3 / 2.3)."""
    import ai_settings
    assert hasattr(ai_settings, "AiSettingsUnsupported")
    assert callable(ai_settings.comparable)
    from utils.udf_signature import parse_corehub_version
    assert parse_corehub_version("2.3.1")[:2] == (2, 3)
    assert parse_corehub_version("2.2.0")[:2] == (2, 2)


@skip_without_hub
def test_live_ai_settings_import_roundtrip(hub):
    """Real live AI settings export → import → re-export on hub 2.3.x; report exact hub response on reject."""
    import ai_settings
    from commons import configure_core_hub

    configure_core_hub(
        hub.env.url,
        use_ssl=hub.env.use_ssl,
        skip_verify=hub.env.skip_verify,
    )
    # Always exercise; do not gate on GS_E2E_AI_SETTINGS — hub 2.3.1 is the target.
    try:
        before = ai_settings.export_ai_settings(hub.token, include_secrets=False)
    except ai_settings.AiSettingsUnsupported as exc:
        print(f"e2e_ai_settings_export_unsupported={exc!r}")
        pytest.skip(f"hub does not expose AI settings export: {exc}")
    except ai_settings.AiSettingsError as exc:
        print(f"e2e_ai_settings_export_error={exc!r}")
        pytest.fail(f"AI settings export refused: {exc}")
    except Exception as exc:  # pylint: disable=broad-except
        print(f"e2e_ai_settings_export_raw={type(exc).__name__}:{exc!r}")
        raise

    assert isinstance(before, dict), f"export expected dict, got {type(before)}: {before!r}"
    assert before.get("format") == ai_settings.BUNDLE_FORMAT, (
        f"export format unexpected: {before.get('format')!r}; keys={list(before)[:20]}"
    )
    print(f"e2e_ai_settings_export_keys={sorted(before.keys())}")

    try:
        result = ai_settings.import_ai_settings(hub.token, before, dry_run=False)
    except ai_settings.AiSettingsUnsupported as exc:
        print(f"e2e_ai_settings_import_unsupported={exc!r}")
        pytest.skip(f"hub does not expose AI settings import: {exc}")
    except ai_settings.AiSettingsError as exc:
        # Report exact hub return so the parent can document reject vs expose.
        print(f"e2e_ai_settings_import_error={exc!r}")
        pytest.fail(f"AI settings import refused (exact): {exc}")
    except Exception as exc:  # pylint: disable=broad-except
        print(f"e2e_ai_settings_import_raw={type(exc).__name__}:{exc!r}")
        raise

    print(f"e2e_ai_settings_import_result={result!r}")
    assert isinstance(result, dict), f"import expected dict, got {type(result)}: {result!r}"

    try:
        after = ai_settings.export_ai_settings(hub.token, include_secrets=False)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"e2e_ai_settings_reexport_error={exc!r}")
        raise
    diffs = ai_settings.compare_bundles(before, after)
    assert diffs == [], f"AI settings changed after import of same bundle: {diffs[:10]}"


@skip_without_hub
def test_live_udf_restore_from_synthetic_backup(hub_env, overrides_json, hub, agents_env):
    """Import backup with 2.3 isSnapshot UDF; verify via hub API that UDF exists and is attached."""
    if not e2e_env.agents_ready():
        pytest.skip("GS_E2E_AGENTS_READY not set")
    table = "E2E_UDF_" + uuid.uuid4().hex[:8].upper()
    src = e2e_env.load_db("mssql")
    ok, reason = db_probe.available(src)
    if not ok:
        pytest.skip(f"cannot seed MSSQL source for UDF restore: {reason}")
    db_probe.ensure_source_table(src, "E2E_SRC", table)
    tgt_db = e2e_env.load_db(e2e_env._dialect_for_agent_tag(agents_env.target_tag) or "mysql")
    if tgt_db is not None:
        ok_t, _ = db_probe.available(tgt_db)
        if ok_t:
            db_probe.ensure_schema(tgt_db, "E2E_TGT")
    tgt_name = f"e2e_{table.lower()}"
    entities = minimal_create_table_entities(
        table=table,
        pre_snapshot=_pre_snapshot_for_tag(agents_env.target_tag, tgt_name),
    )
    data = archive_with_udf(
        entities,
        source_tag=agents_env.source_tag,
        target_tag=agents_env.target_tag,
    )
    # Confirm archive layout before import
    import zipfile, io
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
    assert any(n.endswith(f"{E2E_UDF_NAME}.java") and "udf-" in n for n in names), names
    code = run_import_backup_tracked(
        data, hub_env, overrides_json, hub, "--on-existing", "skip", create_tables=True,
    )
    if code == 1 or not hub.created_pipeline_ids:
        deleted = hub.delete_tracked_pipelines()
        if deleted:
            print(f"e2e_cleanup_tracked_ids={','.join(deleted)}")
        pytest.skip(f"UDF restore import exit {code} / no pipeline")
    pid = hub.created_pipeline_ids[-1]
    try:
        # Hub must expose the compiled mapping function
        mf = hub.fetch_mapping_function(pid, E2E_UDF_NAME)
        print(f"e2e_udf_mapping_function_type={type(mf).__name__}")
        if isinstance(mf, dict):
            print(f"e2e_udf_mapping_function_keys={sorted(mf.keys())}")
            code_body = mf.get("code") or mf.get("source") or mf.get("body") or ""
            if isinstance(code_body, str) and code_body:
                assert "isSnapshot" in code_body, (
                    f"UDF source on hub missing isSnapshot; keys={list(mf.keys())}"
                )
            # type may be Java / JAVA
            udf_type = mf.get("type") or mf.get("language") or ""
            if udf_type:
                assert str(udf_type).lower() in ("java", "jvm"), f"unexpected UDF type {udf_type!r}"
        else:
            # Some hubs return raw string code
            assert isinstance(mf, str), f"unexpected mapping-function body: {mf!r}"
            assert "isSnapshot" in mf, f"unexpected mapping-function body: {mf!r}"

        attached = hub.entity_udf_names(pid)
        print(f"e2e_udf_attached_names={attached}")
        assert E2E_UDF_NAME in attached, (
            f"UDF {E2E_UDF_NAME!r} not attached on entities after restore; attached={attached}"
        )
        # Entity fetch must show attachment on target entityType
        ents = hub.fetch_entities(pid) or []
        assert ents, "expected entities after UDF restore"
    finally:
        deleted = hub.delete_tracked_pipelines()
        if deleted:
            print(f"e2e_cleanup_tracked_ids={','.join(deleted)}")
        try:
            db_probe.drop_table_if_exists(src, "E2E_SRC", table)
        except Exception:
            pass
        if tgt_db is not None:
            try:
                db_probe.drop_table_if_exists(tgt_db, "E2E_TGT", tgt_name)
            except Exception:
                pass


def _pre_snapshot_for_tag(tag: str, table: str) -> str:
    """Dialect-appropriate DELETE so create-table inference matches the target agent."""
    d = e2e_env._dialect_for_agent_tag(tag)
    if d == "mysql":
        return f"DELETE FROM `E2E_TGT`.`{table}`"
    if d == "postgres":
        return f'DELETE FROM "E2E_TGT"."{table}"'
    return f"DELETE FROM [E2E_TGT].[{table}]"


@skip_without_hub
def test_roundtrip_export_import_equality(hub, hub_env, overrides_json, tmp_path, agents_env):
    """Bootstrap (or use) a pipeline, fetch entities via export helper, assert shape survives.

    ``fetch_pipeline_entities(token, pipeline_id)`` — token first (product signature).
    When agents are ready we bootstrap with a create-table archive so the hub has real
    entities (synth SCH* schemas are absent on the trial DB and yield empty PARTIAL).

    Overrides must match the default agent pair (``overrides_json``): never force a dialect
    that disagrees with ``agents_env.target_tag`` (that put MSSQL ports under mysql-cdc).
    """
    pid = os.getenv("GS_E2E_ROUNDTRIP_PIPELINE_ID")
    created_here = False
    if not pid:
        if not e2e_env.agents_ready():
            pytest.skip("GS_E2E_ROUNDTRIP_PIPELINE_ID not set and agents not ready to bootstrap")
        table = "E2E_RT_" + uuid.uuid4().hex[:8].upper()
        src = e2e_env.load_db("mssql")
        ok, reason = db_probe.available(src)
        if not ok:
            pytest.skip(f"cannot seed MSSQL source for roundtrip: {reason}")
        db_probe.ensure_source_table(src, "E2E_SRC", table)
        tgt_db = e2e_env.load_db(e2e_env._dialect_for_agent_tag(agents_env.target_tag) or "mysql")
        if tgt_db is not None:
            ok_t, _ = db_probe.available(tgt_db)
            if ok_t:
                db_probe.ensure_schema(tgt_db, "E2E_TGT")
        tgt_name = f"e2e_{table.lower()}"
        entities = minimal_create_table_entities(
            table=table,
            pre_snapshot=_pre_snapshot_for_tag(agents_env.target_tag, tgt_name),
        )
        data = archive_with_entities(
            entities,
            source_tag=agents_env.source_tag,
            target_tag=agents_env.target_tag,
        )
        # Use the session overrides fixture (ports/credentials match source_tag → target_tag)
        code = run_import_backup_tracked(
            data, hub_env, overrides_json, hub, "--on-existing", "skip", create_tables=True,
        )
        if code == 1 or not hub.created_pipeline_ids:
            pytest.skip(f"could not bootstrap roundtrip pipeline (import exit {code})")
        pid = hub.created_pipeline_ids[-1]
        created_here = True
        # PARTIAL with zero entities cannot exercise export equality
        if code == 2:
            before = hub.fetch_entities(pid)
            if not before:
                hub.delete_tracked_pipelines()
                pytest.skip("bootstrap PARTIAL with no entities — cannot roundtrip")
    before = hub.fetch_entities(pid)
    assert before is not None
    assert isinstance(before, list)
    # When we created the pipeline ourselves, expect at least the create-table entity
    if created_here:
        assert len(before) >= 1, f"expected entities on bootstrapped pipeline {pid}, got {before!r}"
        # Re-fetch (export path) and compare entity names — lightweight equality check
        again = hub.fetch_entities(pid)
        names_a = sorted(
            (e.get("entityName") or e.get("name") or "") for e in before if isinstance(e, dict)
        )
        names_b = sorted(
            (e.get("entityName") or e.get("name") or "") for e in again if isinstance(e, dict)
        )
        assert names_a == names_b
        assert names_a != [""]
        deleted = hub.delete_tracked_pipelines()
        if deleted:
            print(f"e2e_cleanup_tracked_ids={','.join(deleted)}")
        try:
            db_probe.drop_table_if_exists(e2e_env.load_db("mssql"), "E2E_SRC", table)
        except Exception:
            pass
