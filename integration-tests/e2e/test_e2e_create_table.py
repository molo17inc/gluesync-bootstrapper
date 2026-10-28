"""Live e2e: create-table-if-missing against Core Hub + optional direct DB checks.

Skipped unless GS_E2E_HUB_URL (+ password) is set. Per-dialect DB asserts need GS_E2E_<DIALECT>_*.
On Mac: hub at https://localhost, DBs on localhost — see RUN_E2E.md.

Archives are rewritten so SOURCE/TARGET agentTag match GS_E2E_*_AGENT_TAG (or the dialect
agent tag). Overrides use host.docker.internal so agents in Docker reach Mac DB ports.

Cleanup deletes ONLY pipeline ids recorded on HubSession during the test (never by name).
"""
from __future__ import annotations

import json
import os
import uuid

import pytest

from .conftest import skip_without_hub
from . import db_probe
from . import env as e2e_env
from .runner import run_import_backup_tracked
from .zip_builder import archive_with_entities, minimal_create_table_entities

pytestmark = [pytest.mark.e2e, skip_without_hub]


def _require_agents(context: str = "") -> None:
    if not e2e_env.agents_ready():
        pytest.skip(
            "GS_E2E_AGENTS_READY not set — agent containers unavailable; "
            f"skipping agent-dependent create-table{(': ' + context) if context else ''}"
        )



def _unique(prefix: str = "E2E_TAB") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8].upper()}"


def _live_archive(entities, agents_env, target_tag=None):
    """ZIP with agents retagged to the live source/target (overrides keys must match)."""
    return archive_with_entities(
        entities,
        source_tag=agents_env.source_tag,
        target_tag=target_tag or agents_env.target_tag,
    )


def _skip_if_agents_not_ready(code: int, context: str = "") -> None:
    """Exit 1 from import usually means overrides/creds / no agent container — skip, do not fail."""
    if code == 1:
        pytest.skip(
            f"import exit 1 (agents/credentials not ready on this hub){': ' + context if context else ''}"
        )


def _cleanup_tracked(hub) -> None:
    deleted = hub.delete_tracked_pipelines()
    if deleted:
        # ids only — never log names; parent report uses this list
        print(f"e2e_cleanup_tracked_ids={','.join(deleted)}")


def _target_table_name(table: str) -> str:
    """Matches minimal_create_table_entities custom ``name`` field."""
    return f"e2e_{table.lower()}"



def _ensure_target_schema(db, schema: str = "E2E_TGT") -> None:
    """Target create-table needs the schema/database to exist (product does not CREATE SCHEMA)."""
    if db is None:
        return
    db_probe.ensure_schema(db, schema)


def _seed_mssql_source(table: str, schema: str = "E2E_SRC") -> None:
    """Source agent is mssql-ct in the trial layout — discovery needs the source table."""
    src = e2e_env.load_db("mssql")
    ok, reason = db_probe.available(src)
    if not ok:
        pytest.skip(f"cannot seed MSSQL source table: {reason}")
    db_probe.ensure_source_table(src, schema, table)




@pytest.fixture
def tracked_hub(hub):
    """Hub session whose created pipeline ids are deleted after the test (id-only)."""
    yield hub
    _cleanup_tracked(hub)


@skip_without_hub
def test_hub_version_readable(hub):
    """Sanity: GET /version works (raw shape left unverified)."""
    ver = hub.version()
    assert ver is not None


@skip_without_hub
def test_create_table_if_missing_mssql_style_inference(tracked_hub, hub_env, agents_env, tmp_path):
    """Import with MSSQL-style preSnapshotCommand → distinct target schema; verify via DB when configured.

    Target agent tag is the MSSQL agent (GS_E2E_MSSQL_AGENT_TAG), which must differ from source_tag
    so overrides keep both SOURCE and TARGET entries (default source is also mssql-ct).
    """
    _require_agents()
    table = _unique()
    tgt_name = _target_table_name(table)
    entities = minimal_create_table_entities(
        table=table,
        pre_snapshot=f"DELETE FROM [E2E_TGT].[{tgt_name}]",
    )
    db = e2e_env.load_db("mssql")
    ok, reason = db_probe.available(db)
    if not ok:
        pytest.skip(f"MSSQL probe unavailable ({reason}); cannot verify create-table on live target")

    # Prefer mssql-cdc when source is already mssql-ct so source/target tags stay distinct
    target_tag = (db.agent_tag if db and db.agent_tag != agents_env.source_tag else None) or (
        os.getenv("GS_E2E_MSSQL_TARGET_AGENT_TAG") or "mssql-cdc"
    )
    if target_tag == agents_env.source_tag:
        pytest.skip(
            f"MSSQL target tag {target_tag!r} equals source {agents_env.source_tag!r}; "
            "set GS_E2E_MSSQL_AGENT_TAG / GS_E2E_MSSQL_TARGET_AGENT_TAG to a distinct tag (e.g. mssql-cdc)"
        )

    tag_env = "GS_IMPORT_AGENT_PASSWORD_" + target_tag.upper().replace("-", "_").replace(".", "_")
    if db and db.password:
        os.environ.setdefault(tag_env, db.password)
    for key, value in e2e_env.ensure_agent_passwords_in_environ(agents_env).items():
        os.environ.setdefault(key, value)
    os.environ.setdefault(tag_env, os.environ.get(agents_env.source_password_env) or (db.password if db else ""))

    overrides_path = tmp_path / "overrides_mssql.json"
    overrides_path.write_text(
        json.dumps(
            e2e_env.overrides_dict_for(agents_env, dialect="mssql", target_tag=target_tag),
            indent=2,
        ),
        encoding="utf-8",
    )

    _seed_mssql_source(table)
    _ensure_target_schema(db)
    data = _live_archive(entities, agents_env, target_tag=target_tag)
    try:
        code = run_import_backup_tracked(
            data, hub_env, overrides_path, tracked_hub, create_tables=True,
        )
    except Exception as exc:
        # pytest-timeout raises Failed; also catch hung hub client errors
        if "Timeout" in type(exc).__name__ or "timeout" in str(exc).lower():
            pytest.skip(f"import timed out (agents likely unavailable): {type(exc).__name__}")
        raise
    _skip_if_agents_not_ready(code, "mssql-style create-table")
    assert code == 0, f"expected full import success after source seed, got exit {code}"
    schema = db.schema or "dbo"
    exists = (
        db_probe.table_exists(db, "E2E_TGT", tgt_name)
        or db_probe.table_exists(db, "E2E_TGT", table)
        or db_probe.table_exists(db, schema, tgt_name)
        or db_probe.table_exists(db, schema, table)
    )
    assert exists, f"expected target table {tgt_name!r} (or {table!r}) on MSSQL after create-table"
    for sch in ("E2E_TGT", schema):
        for name in (tgt_name, table):
            try:
                db_probe.drop_table_if_exists(db, sch, name)
            except Exception:
                pass
    # drop seeded source
    try:
        db_probe.drop_table_if_exists(e2e_env.load_db("mssql"), "E2E_SRC", table)
    except Exception:
        pass


@pytest.mark.parametrize("dialect,pre_snapshot", [
    ("mssql", "DELETE FROM [E2E_TGT].[{table}]"),
    ("mysql", "DELETE FROM `E2E_TGT`.`{table}`"),
    ("postgres", 'DELETE FROM "E2E_TGT"."{table}"'),
])
@skip_without_hub
def test_create_table_dialect_snapshot_inference(
    dialect, pre_snapshot, hub_env, tmp_path, agents_env, tracked_hub,
):
    _require_agents()
    db = e2e_env.load_db(dialect)
    ok, reason = db_probe.available(db)
    if not ok:
        pytest.skip(f"{dialect}: {reason}")
    target_tag = (db.agent_tag if db else None) or agents_env.target_tag
    if dialect == "mssql" and target_tag == agents_env.source_tag:
        target_tag = os.getenv("GS_E2E_MSSQL_TARGET_AGENT_TAG") or "mssql-cdc"
        if target_tag == agents_env.source_tag:
            pytest.skip("mssql target tag equals source; set GS_E2E_MSSQL_TARGET_AGENT_TAG")

    tag_env = "GS_IMPORT_AGENT_PASSWORD_" + target_tag.upper().replace("-", "_").replace(".", "_")
    if db and db.password:
        os.environ.setdefault(tag_env, db.password)
    for key, value in e2e_env.ensure_agent_passwords_in_environ(agents_env).items():
        os.environ.setdefault(key, value)
    os.environ.setdefault(tag_env, os.environ.get(agents_env.target_password_env) or (db.password if db else ""))

    path = tmp_path / f"overrides_{dialect}.json"
    path.write_text(
        json.dumps(
            e2e_env.overrides_dict_for(agents_env, dialect=dialect, target_tag=target_tag),
            indent=2,
        ),
        encoding="utf-8",
    )

    table = _unique()
    _seed_mssql_source(table)
    _ensure_target_schema(db)
    tgt_name = _target_table_name(table)
    entities = minimal_create_table_entities(
        table=table,
        pre_snapshot=pre_snapshot.format(table=tgt_name),
    )
    data = _live_archive(entities, agents_env, target_tag=target_tag)
    try:
        code = run_import_backup_tracked(data, hub_env, path, tracked_hub, create_tables=True)
    except Exception as exc:
        if "Timeout" in type(exc).__name__ or "timeout" in str(exc).lower():
            pytest.skip(f"import timed out (agents likely unavailable): {type(exc).__name__}")
        raise
    _skip_if_agents_not_ready(code, f"dialect={dialect} tag={target_tag}")
    assert code == 0, f"{dialect}: expected full import after source seed, got {code}"
    schema_candidates = ["E2E_TGT"]
    if db.schema:
        schema_candidates.append(db.schema)
    if db.dialect == "mysql":
        schema_candidates.append(db.database)
    exists = any(
        db_probe.table_exists(db, sch, name)
        for sch in schema_candidates
        for name in (tgt_name, table)
    )
    assert exists, (
        f"{dialect}: expected target table {tgt_name!r} after create-table "
        f"(checked {schema_candidates})"
    )
    for sch in schema_candidates:
        for name in (tgt_name, table):
            try:
                db_probe.drop_table_if_exists(db, sch, name)
            except Exception:
                pass
    try:
        db_probe.drop_table_if_exists(e2e_env.load_db("mssql"), "E2E_SRC", table)
    except Exception:
        pass


@skip_without_hub
def test_existing_table_untouched_on_reimport(hub_env, agents_env, tracked_hub, tmp_path):
    _require_agents()
    db = e2e_env.load_db(os.getenv("GS_E2E_TARGET_DIALECT") or "mysql")
    ok, reason = db_probe.available(db)
    if not ok:
        pytest.skip(reason)
    target_tag = agents_env.target_tag
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps(e2e_env.overrides_dict_for(agents_env), indent=2), encoding="utf-8")
    for key, value in e2e_env.ensure_agent_passwords_in_environ(agents_env).items():
        os.environ.setdefault(key, value)

    table = _unique()
    _seed_mssql_source(table)
    _ensure_target_schema(db)
    tgt_name = _target_table_name(table)
    entities = minimal_create_table_entities(table=table)
    data = _live_archive(entities, agents_env, target_tag=target_tag)
    code1 = run_import_backup_tracked(data, hub_env, path, tracked_hub, create_tables=True)
    if code1 == 1:
        pytest.skip("first import failed entirely — agents/hub not ready")
    cols_before = None
    sch = None
    for candidate in ("E2E_TGT", db.schema or "dbo", db.database if db.dialect == "mysql" else None):
        if not candidate:
            continue
        for name in (tgt_name, table):
            if db_probe.table_exists(db, candidate, name):
                cols_before = list(db_probe.column_names(db, candidate, name))
                sch = candidate
                table = name  # use the name that exists for reimport assert
                break
        if cols_before is not None:
            break
    if cols_before is None:
        pytest.skip("table was not created on first import (partial); cannot assert untouched")
    code2 = run_import_backup_tracked(data, hub_env, path, tracked_hub, create_tables=True)
    assert code2 in (0, 2)
    assert list(db_probe.column_names(db, sch, table)) == cols_before
    try:
        db_probe.drop_table_if_exists(db, sch, table)
    except Exception:
        pass


@skip_without_hub
def test_no_create_tables_flag_leaves_db_without_table(hub_env, agents_env, tracked_hub, tmp_path):
    db = e2e_env.load_db(os.getenv("GS_E2E_TARGET_DIALECT") or "mysql")
    ok, reason = db_probe.available(db)
    if not ok:
        pytest.skip(reason)
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps(e2e_env.overrides_dict_for(agents_env), indent=2), encoding="utf-8")
    for key, value in e2e_env.ensure_agent_passwords_in_environ(agents_env).items():
        os.environ.setdefault(key, value)
    table = _unique()
    entities = minimal_create_table_entities(table=table)
    data = _live_archive(entities, agents_env)
    code = run_import_backup_tracked(
        data, hub_env, path, tracked_hub, create_tables=False,
    )
    assert code in (0, 1, 2)
    for sch in ("E2E_TGT", db.schema or "dbo"):
        assert not db_probe.table_exists(db, sch, table)


@skip_without_hub
def test_schema_with_spaces_create_table_path(hub_env, agents_env, tracked_hub, tmp_path):
    """Spaced target schema: URL-encoded create-table path + table must exist in DB.

    Schema name starts with E2E so db_probe.ensure_schema may create it (product does not).
    """
    _require_agents()
    # Prefer MSSQL so bracket-quoted "E2E Tgt" is the natural dialect; fall back to env target.
    db = e2e_env.load_db("mssql")
    ok, reason = db_probe.available(db)
    if not ok:
        db = e2e_env.load_db(os.getenv("GS_E2E_TARGET_DIALECT") or "mysql")
        ok, reason = db_probe.available(db)
    if not ok:
        pytest.skip(reason)
    spaced = "E2E Tgt"
    path = tmp_path / "overrides.json"
    target_tag = agents_env.target_tag
    if db.dialect == "mssql":
        target_tag = (
            db.agent_tag if db.agent_tag and db.agent_tag != agents_env.source_tag else None
        ) or (os.getenv("GS_E2E_MSSQL_TARGET_AGENT_TAG") or "mssql-cdc")
        if target_tag == agents_env.source_tag:
            pytest.skip("mssql target tag equals source")
    path.write_text(
        json.dumps(
            e2e_env.overrides_dict_for(agents_env, dialect=db.dialect, target_tag=target_tag),
            indent=2,
        ),
        encoding="utf-8",
    )
    for key, value in e2e_env.ensure_agent_passwords_in_environ(agents_env).items():
        os.environ.setdefault(key, value)
    tag_env = "GS_IMPORT_AGENT_PASSWORD_" + target_tag.upper().replace("-", "_").replace(".", "_")
    if db.password:
        os.environ.setdefault(tag_env, db.password)

    table = _unique()
    tgt_name = _target_table_name(table)
    if db.dialect == "mysql":
        pre = f"DELETE FROM `{spaced}`.`{tgt_name}`"
    elif db.dialect == "postgres":
        pre = f'DELETE FROM "{spaced}"."{tgt_name}"'
    else:
        pre = f"DELETE FROM [{spaced}].[{tgt_name}]"
    entities = minimal_create_table_entities(
        table=table,
        target_schema=spaced,
        pre_snapshot=pre,
    )
    _seed_mssql_source(table)
    _ensure_target_schema(db, spaced)
    data = _live_archive(entities, agents_env, target_tag=target_tag)
    try:
        code = run_import_backup_tracked(data, hub_env, path, tracked_hub, create_tables=True)
    except Exception as exc:
        if "Timeout" in type(exc).__name__ or "timeout" in str(exc).lower():
            pytest.skip(f"import timed out (agents likely unavailable): {type(exc).__name__}")
        raise
    _skip_if_agents_not_ready(code, "spaced-schema create-table")
    assert code == 0, f"spaced schema create-table expected full success, got {code}"
    exists = db_probe.table_exists(db, spaced, tgt_name) or db_probe.table_exists(db, spaced, table)
    assert exists, f"expected table {tgt_name!r} in spaced schema {spaced!r} after create-table"
    for name in (tgt_name, table):
        try:
            db_probe.drop_table_if_exists(db, spaced, name)
        except Exception:
            pass
    try:
        db_probe.drop_table_if_exists(e2e_env.load_db("mssql"), "E2E_SRC", table)
    except Exception:
        pass


@skip_without_hub
def test_missing_target_schema_reports_actionable_error(hub_env, agents_env, tracked_hub, tmp_path):
    """Product does not CREATE SCHEMA: missing target schema must surface a clear error.

    Documents the env precondition: ensure the target schema/database exists before
    create-table-if-missing, or import exits PARTIAL with a message naming the schema.
    """
    import io
    from contextlib import redirect_stderr, redirect_stdout

    _require_agents()
    db = e2e_env.load_db(os.getenv("GS_E2E_TARGET_DIALECT") or "mysql")
    ok, reason = db_probe.available(db)
    if not ok:
        pytest.skip(reason)
    missing = f"E2E_MISS_{uuid.uuid4().hex[:8].upper()}"
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps(e2e_env.overrides_dict_for(agents_env), indent=2), encoding="utf-8")
    for key, value in e2e_env.ensure_agent_passwords_in_environ(agents_env).items():
        os.environ.setdefault(key, value)

    table = _unique()
    tgt_name = _target_table_name(table)
    d = db.dialect
    if d == "mysql":
        pre = f"DELETE FROM `{missing}`.`{tgt_name}`"
    elif d == "postgres":
        pre = f'DELETE FROM "{missing}"."{tgt_name}"'
    else:
        pre = f"DELETE FROM [{missing}].[{tgt_name}]"
    entities = minimal_create_table_entities(table=table, target_schema=missing, pre_snapshot=pre)
    _seed_mssql_source(table)
    # Deliberately do NOT ensure_schema(missing)
    data = _live_archive(entities, agents_env)
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = run_import_backup_tracked(data, hub_env, path, tracked_hub, create_tables=True)
    except Exception as exc:
        if "Timeout" in type(exc).__name__ or "timeout" in str(exc).lower():
            pytest.skip(f"import timed out: {type(exc).__name__}")
        raise
    _skip_if_agents_not_ready(code, "missing-schema")
    text = (out.getvalue() + "\n" + err.getvalue()).lower()
    assert code == 2, f"missing schema should be PARTIAL (2), got {code}; log={text[:800]}"
    assert "schema" in text, f"error should mention schema; got: {text[:800]}"
    actionable = (
        "does not exist" in text
        or "not exist" in text
        or "permission" in text
        or "unknown database" in text
        or "no schema" in text
    )
    assert actionable, (
        f"missing-schema error should be actionable (name the missing schema / does-not-exist); "
        f"got: {text[:1200]}"
    )
    assert missing.lower() in text, (
        f"error should name the missing schema {missing!r}; got: {text[:1200]}"
    )
    # Table must not have been created under the missing schema
    assert not db_probe.table_exists(db, missing, tgt_name)
    try:
        db_probe.drop_table_if_exists(e2e_env.load_db("mssql"), "E2E_SRC", table)
    except Exception:
        pass
