"""Live e2e: the GS1 import against a real Core Hub (2.3 with Query entities), SQL Server source and Couchbase target.

Core Hub's describe runs the structure checks the converter leaves to it: a statement filtering on GETDATE() is
refused (NON_DETERMINISTIC_QUERY) before the agent is asked, a plain Query is described by the source agent and
validated as an entity. Nothing is saved before the confirmation; the saved entity is deleted at the end.

Needs, besides the hub variables of RUN_E2E.md:
- GS_E2E_GS1_PIPELINE: a pipeline whose SOURCE agent is SQL Server (CDC) and whose TARGET agent is Couchbase;
- GS_E2E_GS1_TABLE: ``schema.table`` of the source with a primary key, and GS_E2E_GS1_KEY: that key column;
- optionally GS_E2E_GS1_SCOPE (default ``_default``) for the Couchbase scope the draft writes to.
"""
from __future__ import annotations

import os
import uuid

import pytest

import gs1_import
from commons import fetch_core_hub
from gs1_migration import conversion_request

pytestmark = [pytest.mark.e2e]


def _setting(name):
    value = (os.getenv(name) or "").strip()
    if not value:
        pytest.skip(f"{name} not set")
    return value


@pytest.fixture
def gs1_target(hub):
    pipeline = _setting("GS_E2E_GS1_PIPELINE")
    table = _setting("GS_E2E_GS1_TABLE")
    key = _setting("GS_E2E_GS1_KEY")
    agents = gs1_import.pipeline_agents(hub.token, pipeline)
    if not (agents["source"] and agents["target"]):
        pytest.skip(f"pipeline {pipeline} has no SOURCE or TARGET agent")
    return {"pipeline": pipeline, "table": table, "key": key, "scope": os.getenv("GS_E2E_GS1_SCOPE") or "_default",
            "source": agents["source"], "target": agents["target"]}


def _entities(token, pipeline):
    from export_template_from_corehub import fetch_pipeline_entities

    return fetch_pipeline_entities(token, pipeline) or []


def test_gs1_import_checks_with_core_hub_and_saves_only_once_confirmed(hub, gs1_target):
    suffix = uuid.uuid4().hex[:8].upper()
    good, drifting = f"E2E_GS1_{suffix}", f"E2E_GS1_NOW_{suffix}"
    key, table, scope = gs1_target["key"], gs1_target["table"], gs1_target["scope"]
    config = {
        "couchbase": {"useCollections": True},
        "sourcePassword": "e2e-not-a-secret",
        "entitiesKeyMapping": {good: {"prefix": good, "separator": "::", "fields": [key]}},
        "sourceEntities": {
            good: {"type": good, "scope": scope, "query": f"SELECT t.{key} AS {key} FROM {table} t"},
            drifting: {"type": drifting, "scope": scope,
                       "query": f"SELECT t.{key} AS {key} FROM {table} t WHERE GETDATE() > '2000-01-01'"},
        },
    }
    request = conversion_request(config, gs1_target["pipeline"], gs1_target["source"], gs1_target["target"],
                                 {good: 54321})

    plan = gs1_import.plan_import(hub.token, request)

    buckets = {entry["gs1Name"]: entry for entry in plan["report"]["perEntity"]}
    assert buckets[good]["bucket"] == "FLAT", buckets[good]
    assert buckets[drifting]["bucket"] == "UNSUPPORTED"
    assert "NON_DETERMINISTIC_QUERY" in [issue["code"] for issue in buckets[drifting]["issues"]]
    assert plan["report"]["strippedKeys"] == ["sourcePassword"]
    check = plan["checks"][good]
    assert "error" not in check, check
    assert check["describe"]["levels"], "the source agent described the statement"
    assert "validate" in check
    assert list(plan["drafts"]) == [good]
    assert not any(e.get("entityName") in (good, drifting) for e in _entities(hub.token, gs1_target["pipeline"]))

    with pytest.raises(gs1_import.Gs1ImportError):
        gs1_import.save_drafts(hub.token, gs1_target["pipeline"], plan, [good], confirmed=False)
    assert not any(e.get("entityName") == good for e in _entities(hub.token, gs1_target["pipeline"]))

    saved_ids = []
    try:
        assert gs1_import.save_drafts(hub.token, gs1_target["pipeline"], plan, [good], confirmed=True) == [good]
        stored = [e for e in _entities(hub.token, gs1_target["pipeline"]) if e.get("entityName") == good]
        saved_ids = [e.get("entityId") for e in stored if e.get("entityId")]
        assert len(stored) == 1
        target = next(a for a in stored[0]["agentEntities"] if a.get("type") == "NoSqlEntity")
        assert target["keyMapping"]["valueRendering"] == "GS1_DROP_NULLS"
        assert target["entityType"]["allowedOperations"] == ["INSERT", "UPDATE"]
        query = next(a for a in stored[0]["agentEntities"] if a.get("type") == "Query")
        assert query["model"]["options"]["maxDeletesPerWindow"] == 543
        assert query["model"]["options"]["emptyArray"] == "OMIT"
    finally:
        if saved_ids:
            fetch_core_hub(f"/pipelines/{gs1_target['pipeline']}/config/entities", method="DELETE", token=hub.token,
                           body={"entities": saved_ids})
