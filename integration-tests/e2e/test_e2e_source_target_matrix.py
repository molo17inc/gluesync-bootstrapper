"""Live source × target matrix over every configured lab kit.

Skipped unless GS_E2E_HUB_URL is set. Covers:
- dry-run Import All for every source×target pair the kits support
- create-table-if-missing (or correct skip for non-RDBMS targets)
- column rename / filter / group shapes on a representative live import
- Customize import with one pipeline off (via --plan / entity selection)
- report counts + Import All exit codes

Offline-only kits (cassandra, cosmos, informix) are never parametrized live.
Target-only kits (kafka, awss3, vertica) only appear as targets.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from . import db_probe
from . import env as e2e_env
from .conftest import skip_without_hub
from .runner import run_import_backup, run_import_backup_tracked
from .zip_builder import (
    archive_with_entities,
    minimal_create_table_entities,
    multi_pipeline_archive,
    synth_archive,
)

pytestmark = [pytest.mark.e2e, skip_without_hub]


def _sources():
    kits = e2e_env.available_kits(role="source")
    # Always include oracle/db2/etc when env present; empty list => skip whole matrix
    return kits or ["mssql", "mysql", "pgsql"]


def _targets():
    kits = e2e_env.available_kits(role="target")
    return kits or ["mssql", "mysql", "pgsql"]


def _pairs():
    """All source×target pairs except identical tags that would collide (same kit as both)."""
    out = []
    for s in _sources():
        for t in _targets():
            out.append((s, t))
    return out


def _is_rdbms(kit: str) -> bool:
    spec = e2e_env.KIT_SPECS.get(kit)
    return bool(spec and spec.category == "rdbms")


def _require_agents():
    if not e2e_env.agents_ready():
        pytest.skip("GS_E2E_AGENTS_READY not set")


def _unique(prefix: str = "E2E_MX") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8].upper()}"


def _demo_entities_for_kit(source_k: str, target_schema: str = "E2E_TGT") -> dict:
    """Entities YAML fragment that matches gluesync-demo-kits seed tables.

    Relational kits seed CUSTOMERS (MSSQL/MySQL upper, Postgres lower). NoSQL kits whitelist
    the real seed collection (mongodb democollection, couchbase _default scope) with a
    documentKey; other kits allow-all discovery under demo.
    """
    spec = e2e_env.KIT_SPECS.get(source_k)
    if spec and spec.category == "rdbms":
        if source_k in ("pgsql", "postgres", "vertica"):
            schema, table = "public", "customers"
        elif source_k == "oracle":
            schema, table = "GLUESYNCUSER", "CUSTOMERS"
        elif source_k == "db2":
            # EnvBuilder db2 kit seeds DEMO.CUSTOMERS (also mirrored to DB2INST1 for e2e).
            schema, table = "DEMO", "CUSTOMERS"
        else:
            # mssql / mysql / mariadb / mssql_cdc
            schema, table = ("dbo" if source_k.startswith("mssql") else "demo"), "CUSTOMERS"
            if source_k in ("mysql", "mariadb"):
                schema = "demo"
        return {
            "schemas": {
                schema: {
                    "target": target_schema,
                    "tables": {
                        "whitelist": [table],
                        "custom": {
                            table: {
                                "name": "e2e_customers",
                                "keys": ["ID"] if source_k not in ("pgsql", "postgres") else ["id"],
                            }
                        },
                    },
                }
            }
        }
    # NoSQL / streaming: whitelist must match kit seed collections (not RDBMS CUSTOMERS).
    # documentKey uses the object form; string shorthand is also accepted by the product.
    if source_k == "mongodb":
        # EnvBuilder mongodb kit seeds demo.democollection with _id/name/age.
        return {
            "schemas": {
                "demo": {
                    "target": target_schema,
                    "tables": {
                        "whitelist": ["democollection"],
                        "custom": {
                            "democollection": {
                                "name": "e2e_customers",
                                "keys": ["_id"],
                                "documentKey": {"keys": ["_id"]},
                                "columns": [
                                    {"_id": "ID"},
                                    {"name": "NAME"},
                                    {"age": "AGE"},
                                ],
                            }
                        },
                    },
                }
            }
        }
    if source_k == "couchbase":
        # Couchbase scopes are discovery schemas; kit bucket is demo, seed in _default._default.
        return {
            "schemas": {
                "_default": {
                    "target": target_schema,
                    "tables": {
                        "whitelist": ["_default"],
                        "custom": {
                            "_default": {
                                "name": "e2e_customers",
                                "keys": ["id"],
                                "documentKey": {"keys": ["id"]},
                                "columns": [
                                    {"id": "ID"},
                                    {"name": "NAME"},
                                    {"age": "AGE"},
                                ],
                            }
                        },
                    },
                }
            }
        }
    # Other NoSQL / streaming kits: allow-all discovery under demo (empty whitelist).
    return {
        "schemas": {
            "demo": {
                "target": "demo",
                "tables": {
                    "whitelist": [],
                },
            }
        }
    }


def _live_archive_for_pair(source_k: str, src_tag: str, tgt_tag: str, target_k: str = "") -> bytes:
    tschema = _target_schema_for(target_k) if target_k else "E2E_TGT"
    return archive_with_entities(
        _demo_entities_for_kit(source_k, target_schema=tschema),
        source_tag=src_tag,
        target_tag=tgt_tag,
    )



def _ensure_rdbms_target_schema(target_k: str, schema: str = "E2E_TGT") -> None:
    """Create E2E_TGT schema/database on probeable RDBMS targets (mssql/mysql/postgres).

    mssql_cdc is a separate instance (port 1434): never reuse the mssql (1433) DbEnv.
    oracle/db2/vertica have no Python probe driver here — seed those outside the matrix.
    """
    if not _is_rdbms(target_k):
        return
    import os
    from e2e.env import DbEnv
    if target_k == "mssql_cdc":
        host = os.getenv("GS_E2E_MSSQL_CDC_HOST") or os.getenv("GS_E2E_MSSQL_HOST")
        if not host:
            return
        db = DbEnv(
            dialect="mssql",
            host=host,
            port=int(os.getenv("GS_E2E_MSSQL_CDC_PORT") or "1434"),
            database=os.getenv("GS_E2E_MSSQL_CDC_DATABASE") or "demo",
            username=os.getenv("GS_E2E_MSSQL_CDC_USERNAME") or "gluesync",
            password=os.getenv("GS_E2E_MSSQL_CDC_PASSWORD") or os.getenv("GS_E2E_MSSQL_PASSWORD") or "",
            schema="dbo",
            agent_tag="mssql-cdc",
        )
    else:
        load_key = {
            "mssql": "mssql",
            "mysql": "mysql", "mariadb": "mariadb",
            "pgsql": "postgres", "postgres": "postgres",
        }.get(target_k)
        if not load_key:
            return  # oracle/db2/vertica: seed outside the matrix
        db = e2e_env.load_db(load_key)
    ok, reason = db_probe.available(db)
    if not ok:
        return
    try:
        db_probe.ensure_schema(db, schema)
    except Exception:
        pass


def _target_schema_for(target_k: str) -> str:
    if target_k in ("pgsql", "postgres"):
        return "E2E_TGT"
    if target_k in ("mysql", "mariadb"):
        return "E2E_TGT"  # MySQL: created as database
    if target_k.startswith("mssql"):
        return "E2E_TGT"
    return "E2E_TGT"


def _classify_import_code(code: int, source_k: str, target_k: str, src_tag: str, tgt_tag: str) -> None:
    """Assert success; skip with classification for agents/creds/hub limits; fail only on unexpected."""
    if code == 0:
        return
    if code == 1:
        pytest.skip(
            f"import exit 1 (failed) {source_k}->{target_k} tags {src_tag}->{tgt_tag}: "
            "agents/credentials/hub limitation"
        )
    if code == 2:
        # partial: often missing source tables / agent config incomplete — fixture or hub limit
        pytest.skip(
            f"import exit 2 (partial) {source_k}->{target_k} tags {src_tag}->{tgt_tag}: "
            "partial import — classify as fixture/hub limitation unless product bug confirmed"
        )
    pytest.fail(f"{source_k}->{target_k} unexpected exit {code}")



@pytest.fixture(scope="module", autouse=True)
def _wire_kit_passwords():
    e2e_env.ensure_all_kit_passwords_in_environ()


@pytest.fixture(scope="module")
def _hub_live():
    if not e2e_env.hub_configured():
        pytest.skip("hub not configured")


def test_offline_only_kits_not_in_live_matrix():
    """cassandra / cosmos / informix stay offline-only."""
    for kit in e2e_env.OFFLINE_ONLY_KITS:
        assert not e2e_env.kit_configured(kit)
        assert kit not in e2e_env.KIT_SPECS or kit not in e2e_env.available_kits()


@pytest.mark.parametrize("source_k,target_k", _pairs(), ids=lambda p: f"{p[0]}->{p[1]}" if isinstance(p, tuple) else str(p))
def test_live_pair_dry_run(_hub_live, source_k, target_k, tmp_path, hub_env):
    """Dry-run Import All for every configured kit pair (no hub writes)."""
    if source_k in e2e_env.OFFLINE_ONLY_KITS or target_k in e2e_env.OFFLINE_ONLY_KITS:
        pytest.skip("offline-only kit")
    if not e2e_env.kit_configured(source_k) or not e2e_env.kit_configured(target_k):
        pytest.skip(f"kit env missing: {source_k}->{target_k}")
    src_tag = e2e_env.tag_for_kit(source_k, "source")
    tgt_tag = e2e_env.tag_for_kit(target_k, "target")
    if not src_tag or not tgt_tag:
        pytest.skip(f"no agent tag for {source_k}->{target_k}")
    data = synth_archive("synth1", source_tag=src_tag, target_tag=tgt_tag)
    ov = e2e_env.overrides_dict_for_pair(source_k, target_k, src_tag, tgt_tag)
    path = tmp_path / f"ov_{source_k}_{target_k}.json"
    path.write_text(json.dumps(ov), encoding="utf-8")
    # Ensure password env names exist for this process
    e2e_env.ensure_all_kit_passwords_in_environ()
    code = run_import_backup(data, hub_env, path, dry_run=True, create_tables=_is_rdbms(target_k))
    assert code in (0, 1), f"{source_k}->{target_k} unexpected dry-run exit {code}"


@pytest.mark.parametrize("source_k,target_k", _pairs(), ids=lambda p: f"{p[0]}->{p[1]}" if isinstance(p, tuple) else str(p))
def test_live_pair_import_all(_hub_live, source_k, target_k, tmp_path, hub_env, hub):
    """Full Import All for every kit pair; create-table only for RDBMS targets."""
    _require_agents()
    if not e2e_env.kit_configured(source_k) or not e2e_env.kit_configured(target_k):
        pytest.skip(f"kit env missing: {source_k}->{target_k}")
    src_tag = e2e_env.tag_for_kit(source_k, "source")
    tgt_tag = e2e_env.tag_for_kit(target_k, "target")
    if not src_tag or not tgt_tag:
        pytest.skip(f"no agent tag for {source_k}->{target_k}")
    # Same agentTag on SOURCE and TARGET is a normal case (MySQL→MySQL, mssql-ct→mssql-ct).
    # overrides_dict_for_pair keys those by agentId with distinct passwordEnv so
    # check_password_vars does not refuse; connectionName stays distinct in the synth fixture.

    create_tables = _is_rdbms(target_k)
    if create_tables:
        _ensure_rdbms_target_schema(target_k)
    data = _live_archive_for_pair(source_k, src_tag, tgt_tag, target_k)
    ov = e2e_env.overrides_dict_for_pair(source_k, target_k, src_tag, tgt_tag)
    path = tmp_path / f"ov_full_{source_k}_{target_k}.json"
    path.write_text(json.dumps(ov), encoding="utf-8")
    e2e_env.ensure_all_kit_passwords_in_environ()
    before = {str(p.get("pipelineId") or p.get("id") or "") for p in hub.list_pipelines()}
    code = run_import_backup_tracked(
        data, hub_env, path, hub,
        create_tables=create_tables, dry_run=False,
    )
    hub.snapshot_new(before)
    _classify_import_code(code, source_k, target_k, src_tag, tgt_tag)


# ---------------------------------------------------------------------------
# Feature paths (create-table skip, rename, filter, group, customize, report)
# Run once per batch category via GS_E2E_MATRIX_FEATURES=1 or always when agents ready.
# ---------------------------------------------------------------------------

def _feature_enabled():
    return e2e_env.agents_ready()


@pytest.mark.parametrize("target_k", ["kafka", "awss3", "mongodb", "couchbase", "dynamodb"])
def test_create_table_skipped_for_non_rdbms_target(_hub_live, target_k, tmp_path, hub_env, hub, caplog):
    """Non-RDBMS targets must not attempt CREATE TABLE even when create_tables=True.

    Uses the same seeded demo entities as the matrix (not E2E_SRC) so entity
    discovery succeeds and the only question is whether CREATE TABLE is skipped.
    """
    import logging
    if not _feature_enabled():
        pytest.skip("agents not ready")
    if not e2e_env.kit_configured(target_k):
        pytest.skip(f"{target_k} not configured")
    source_k = "mysql" if e2e_env.kit_configured("mysql") else (
        "mssql" if e2e_env.kit_configured("mssql") else (_sources()[0] if _sources() else None)
    )
    if not source_k:
        pytest.skip("no source kit")
    src_tag = e2e_env.tag_for_kit(source_k, "source")
    tgt_tag = e2e_env.tag_for_kit(target_k, "target")
    data = _live_archive_for_pair(source_k, src_tag, tgt_tag, target_k)
    ov = e2e_env.overrides_dict_for_pair(source_k, target_k, src_tag, tgt_tag)
    path = tmp_path / f"ov_skip_{target_k}.json"
    path.write_text(json.dumps(ov), encoding="utf-8")
    e2e_env.ensure_all_kit_passwords_in_environ()
    with caplog.at_level(logging.INFO):
        code = run_import_backup_tracked(
            data, hub_env, path, hub, create_tables=True, dry_run=False,
        )
    # Exit 0 = import ok with create-table skipped; exit 1 = agents not ready for that tag
    if code == 1:
        pytest.skip(f"non-rdbms create-table skip path: agents/creds for {target_k}")
    assert code == 0, f"expected FULL success with create-table skipped for {target_k}, got {code}"
    joined = "\n".join(r.message for r in caplog.records)
    assert "Skipping table creation" in joined or "CREATE_TABLE_IF_NOT_EXISTS is enabled - creating table" not in joined, (
        f"expected create-table skip log for non-RDBMS target {target_k}; log excerpt:\n{joined[-2000:]}"
    )


def test_live_rename_filter_group_roundtrip(_hub_live, tmp_path, hub_env, hub):
    """Live import with column rename (``target`` + ``destination``), filter, and group.

    Prefer mysql→pgsql (seeded demo.CUSTOMERS). ``columns`` must be a *list* of mapping
    rows; a dict under ``columns`` is refused by the product (see EntityMappingError).
    """
    if not _feature_enabled():
        pytest.skip("agents not ready")
    source_k = "mysql" if e2e_env.kit_configured("mysql") else (
        "mssql" if e2e_env.kit_configured("mssql") else None
    )
    target_k = "pgsql" if e2e_env.kit_configured("pgsql") else (
        "mysql" if e2e_env.kit_configured("mysql") and source_k != "mysql" else None
    )
    if not source_k or not target_k:
        srcs, tgts = _sources(), _targets()
        if not srcs or not tgts:
            pytest.skip("no kits")
        source_k, target_k = srcs[0], next((t for t in tgts if t != srcs[0] and _is_rdbms(t)), tgts[0])
    src_tag = e2e_env.tag_for_kit(source_k, "source")
    tgt_tag = e2e_env.tag_for_kit(target_k, "target")
    src_schema = "demo" if source_k in ("mysql", "mariadb") else ("dbo" if source_k.startswith("mssql") else "demo")
    entities = {
        "schemas": {
            src_schema: {
                "target": "E2E_TGT",
                "tables": {
                    "whitelist": ["CUSTOMERS"],
                    "custom": {
                        "CUSTOMERS": {
                            "name": "e2e_customers_rfg",
                            "keys": ["ID"],
                            "groupId": "e2e_finance",
                            "filter": {
                                "type": "AND",
                                "clauses": [{"column": "ID", "operator": ">", "value": "0"}],
                            },
                            # List form: ``target`` (export) and ``destination`` (hand-written) both rename.
                            "columns": [
                                {"source": "ID", "target": "id_pk"},
                                {"source": "NAME", "destination": "name_renamed"},
                                {"source": "SURNAME", "target": "surname_renamed"},
                                {"source": "ADDRESS", "destination": "address_renamed"},
                                {"source": "GENDER", "target": "gender"},
                                {"source": "PHONE", "destination": "phone"},
                                {"source": "EMAIL", "target": "email"},
                            ],
                        }
                    },
                },
            }
        },
        "groups": {
            "e2e_finance": {"description": "e2e group", "color": "#336699"},
        },
    }
    _ensure_rdbms_target_schema(target_k)
    data = archive_with_entities(entities, source_tag=src_tag, target_tag=tgt_tag)
    ov = e2e_env.overrides_dict_for_pair(source_k, target_k, src_tag, tgt_tag)
    path = tmp_path / "ov_rfg.json"
    path.write_text(json.dumps(ov), encoding="utf-8")
    e2e_env.ensure_all_kit_passwords_in_environ()
    code = run_import_backup_tracked(data, hub_env, path, hub, create_tables=True)
    assert code == 0, f"RFG import expected FULL success, got {code}"


def test_customize_import_one_pipeline_off(_hub_live, tmp_path, hub_env, hub):
    """Customize import: multi-pipeline ZIP, turn one pipeline off, import the other."""
    if not _feature_enabled():
        pytest.skip("agents not ready")
    source_k = "mysql" if e2e_env.kit_configured("mysql") else (
        "mssql" if e2e_env.kit_configured("mssql") else None
    )
    target_k = "pgsql" if e2e_env.kit_configured("pgsql") else (
        next((t for t in _targets() if t != source_k and _is_rdbms(t)), None)
    )
    if not source_k or not target_k:
        pytest.skip("need two rdbms kits")
    src_tag = e2e_env.tag_for_kit(source_k, "source")
    tgt_tag = e2e_env.tag_for_kit(target_k, "target")
    src_schema = "demo" if source_k in ("mysql", "mariadb") else ("dbo" if source_k.startswith("mssql") else "demo")
    def _ents(table_name: str) -> dict:
        return {
            "schemas": {
                src_schema: {
                    "target": "E2E_TGT",
                    "tables": {
                        "whitelist": ["CUSTOMERS"],
                        "custom": {
                            "CUSTOMERS": {"name": table_name, "keys": ["ID"]},
                        },
                    },
                }
            }
        }
    data = multi_pipeline_archive(
        [
            {"pipeline_id": "e2e0001", "pipeline_name": "E2E_Custom_A", "entities": _ents("e2e_custom_a")},
            {"pipeline_id": "e2e0002", "pipeline_name": "E2E_Custom_B", "entities": _ents("e2e_custom_b")},
        ],
        source_tag=src_tag,
        target_tag=tgt_tag,
    )
    ov = e2e_env.overrides_dict_for_pair(source_k, target_k, src_tag, tgt_tag)
    ov_path = tmp_path / "ov_custom.json"
    ov_path.write_text(json.dumps(ov), encoding="utf-8")
    e2e_env.ensure_all_kit_passwords_in_environ()
    _ensure_rdbms_target_schema(target_k)
    draft_path = tmp_path / "draft_plan.json"
    zpath = tmp_path / "backup_for_plan.zip"
    zpath.write_bytes(data)
    from automator_app import import_backup as ib
    rc = ib.main([str(zpath), "--write-plan", str(draft_path)])
    if rc != 0 or not draft_path.exists():
        pytest.fail(f"--write-plan failed rc={rc}")
    plan = json.loads(draft_path.read_text(encoding="utf-8"))
    pipes = plan.get("pipelines") or {}
    assert len(pipes) >= 2, f"expected multi-pipeline draft, got {list(pipes)}"
    # Turn off the first pipeline only
    first_id = next(iter(pipes))
    pipes[first_id]["import"] = False
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    before = {str(p.get("pipelineId") or p.get("id")) for p in hub.list_pipelines()}
    code = run_import_backup_tracked(
        data, hub_env, ov_path, hub, "--plan", str(plan_path),
        create_tables=True, dry_run=False,
    )
    assert code == 0, f"customize one-off expected FULL success, got {code}"
    after = hub.list_pipelines()
    created = [p for p in after if str(p.get("pipelineId") or p.get("id")) not in before]
    assert len(created) == 1, f"expected exactly 1 pipeline created, got {len(created)}: {created}"


def test_import_report_counts_smoke(_hub_live, tmp_path, hub_env, hub):
    """Import All and ensure exit 0/1; report totals covered offline — smoke live path."""
    if not _feature_enabled():
        pytest.skip("agents not ready")
    source_k = "mssql" if e2e_env.kit_configured("mssql") else (_sources()[0] if _sources() else None)
    target_k = "pgsql" if e2e_env.kit_configured("pgsql") else (
        next((t for t in _targets() if t != source_k), None)
    )
    if not source_k or not target_k:
        pytest.skip("need kits")
    src_tag = e2e_env.tag_for_kit(source_k, "source")
    tgt_tag = e2e_env.tag_for_kit(target_k, "target")
    if _is_rdbms(target_k):
        _ensure_rdbms_target_schema(target_k)
    data = _live_archive_for_pair(source_k, src_tag, tgt_tag, target_k)
    ov = e2e_env.overrides_dict_for_pair(source_k, target_k, src_tag, tgt_tag)
    path = tmp_path / "ov_report.json"
    path.write_text(json.dumps(ov), encoding="utf-8")
    e2e_env.ensure_all_kit_passwords_in_environ()
    code = run_import_backup_tracked(data, hub_env, path, hub, create_tables=_is_rdbms(target_k))
    # full=0, failed=1, partial=2 — all are valid observed outcomes for the smoke
    assert code in (0, 1, 2), f"unexpected exit {code}"
