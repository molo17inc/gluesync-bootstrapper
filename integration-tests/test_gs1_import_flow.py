"""The GS1 import flow against a fake Core Hub: convert, describe and validate each draft, save only once confirmed.

Covers the command line (gs1_import.py) and the Automator routes (/api/gs1/convert, /api/gs1/save).
"""

import io
import json
from pathlib import Path
from unittest import mock

import pytest

import gs1_import
from gs1_migration import conversion_request
from gs1_migration.model import column_id, level_table_id

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gs1_converter"
PIPELINE = "pipe-1"

GS1_CONFIG = {
    "couchbase": {"useCollections": True},
    "sourceHost": "sql.example.test",
    "sourcePassword": "fixture-source-password",
    "entitiesKeyMapping": {"KEYED": {"prefix": "K", "separator": "-", "fields": ["ID"]}},
    "sourceEntities": {
        "ORDERS": {"type": "ORDERS", "scope": "S", "query": "SELECT o.ID, o.TOTAL FROM dbo.ORDERS o"},
        "KEYED": {"type": "KEYED", "scope": "S", "query": "SELECT k.ID AS ID FROM dbo.KEYED k"},
        "RECENT": {"type": "RECENT", "scope": "S", "query": "SELECT r.ID AS ID FROM dbo.R r WHERE r.D > GETDATE()"},
        "PLAIN": {"schema": "dbo", "table": "PLAIN"},
    },
}

NON_DETERMINISTIC = {"code": "NON_DETERMINISTIC_QUERY", "severity": "ERROR", "level": "RECENT", "location": "1:45",
                     "message": "GETDATE() depends on the moment the statement runs", "hint": "Rewrite it."}


class FakeCoreHub:
    """Describe refuses a statement on GETDATE before the agent and proposes ID as key otherwise; validate accepts."""

    def __init__(self):
        self.calls = []
        self.saved = []

    def __call__(self, path, method="GET", token=None, body=None, **_kwargs):
        self.calls.append((method, path))
        if path.endswith("/discovery/node-info"):
            if "/agents/mssql-cdc/" in path:
                return {"dataTypesMatrix": [{"defaultType": "int", "gluesyncDataType": "INTEGER", "supportedTypes": ["int"]}]}
            return {"dataTypesMatrix": [{"defaultType": "number", "gluesyncDataType": "INTEGER", "supportedTypes": []},
                                        {"defaultType": "array", "gluesyncDataType": "ARRAY", "supportedTypes": []}]}
        if path == f"/pipelines/{PIPELINE}/config":
            return {"agents": [{"agentType": "SOURCE", "agentId": "mssql-cdc"}, {"agentType": "TARGET", "agentId": "couchbase"}]}
        if path.endswith("/discovery/data-model"):
            root = body["model"]["root"]
            if "GETDATE" in root["source"]["query"]:
                return {"model": body["model"], "capturedTables": [], "levels": [],
                        "verdict": {"mode": "SNAPSHOT_ONLY", "reasons": []}, "issues": [NON_DETERMINISTIC]}
            described = json.loads(json.dumps(body["model"]))
            described["root"]["key"] = described["root"]["key"] or ["ID"]
            described["root"]["columns"] = [
                {"id": column_id("ID"), "tableId": level_table_id(root["name"]), "name": "ID", "position": 1,
                 "dataType": "int", "isPK": False, "isIdentity": False, "isNullable": False},
                {"id": column_id("type"), "tableId": level_table_id(root["name"]), "name": "type", "position": 2,
                 "dataType": "int", "isPK": False, "isIdentity": False, "isNullable": False},
            ]
            described["options"] = {"emptyArray": "EMPTY"}
            return {"model": described, "capturedTables": [], "levels": [
                {"level": root["name"], "columns": [], "occurrences": [], "constructs": [], "proposedKey": ["ID"]}],
                "verdict": {"mode": "CDC", "reasons": []}, "issues": []}
        if path.endswith("/config/entities/validate"):
            return {"valid": True, "issues": [{"severity": "WARNING", "message": "fine", "entityName": "x"}]}
        if path == f"/pipelines/{PIPELINE}/config/entities" and method == "PUT":
            self.saved.append(body)
            return "Entity upsert success"
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def hub():
    fake = FakeCoreHub()
    with mock.patch.object(gs1_import, "fetch_core_hub", fake):
        yield fake


def _request(config=None):
    return conversion_request(config or GS1_CONFIG, PIPELINE, "mssql-cdc", "couchbase")


def test_each_query_draft_is_described_and_validated_and_nothing_is_saved(hub):
    plan = gs1_import.plan_import("tok", _request())

    buckets = {entry["gs1Name"]: entry["bucket"] for entry in plan["report"]["perEntity"]}
    assert buckets == {"ORDERS": "FLAT", "KEYED": "FLAT", "RECENT": "UNSUPPORTED", "PLAIN": "UNSUPPORTED"}
    assert set(plan["drafts"]) == {"ORDERS", "KEYED"}
    recent = next(e for e in plan["report"]["perEntity"] if e["gs1Name"] == "RECENT")
    assert [i["code"] for i in recent["issues"]] == ["NON_DETERMINISTIC_QUERY", "GS1_ROOT_KEY_TO_CONFIRM"]
    assert plan["checks"]["ORDERS"]["validate"]["valid"] is True
    assert "validate" not in plan["checks"]["RECENT"]
    assert not any(method == "PUT" for method, _ in hub.calls)
    assert plan["report"]["strippedKeys"] == ["sourcePassword"]


def test_a_query_keyed_on_the_from_table_takes_the_proposed_key_in_its_gs1_id(hub):
    plan = gs1_import.plan_import("tok", _request())

    orders = plan["drafts"]["ORDERS"]
    source, target = orders["agentEntities"]
    assert source["model"]["root"]["key"] == ["ID"]
    assert [c["name"] for c in source["model"]["root"]["columns"]] == ["ID", "type"]
    assert source["model"]["options"]["emptyArray"] == "OMIT", "the converter's options are kept"
    assert target["keyMapping"]["prefix"] == "ORDERS"
    assert target["keyMapping"]["keys"] == [column_id("ID")]
    assert [(c["name"], c["dataType"], c["isPK"]) for c in target["columns"]] == [
        ("ID", "number", True), ("type", "string", False), ("scope", "string", False)], "type stays the static field"
    assert target["entityType"]["columnsMappingMatrix"] == [{
        "sourceTableObjectId": level_table_id("ORDERS"), "targetTableObjectId": target["entityObject"]["id"],
        "sourceColumnIds": [column_id("ID")], "targetColumnIds": [column_id("ID")]}]
    report_draft = next(e for e in plan["report"]["entities"] if e["entityName"] == "ORDERS")
    assert report_draft["agentEntities"][1]["keyMapping"]["keys"] == [], "the converter's report is left as converted"


def test_offline_calls_no_core_hub(hub):
    plan = gs1_import.plan_import(None, _request(), check=False)

    assert hub.calls == []
    assert set(plan["drafts"]) == {"ORDERS", "KEYED", "RECENT"}
    assert gs1_import.summarize(plan)[0].startswith("Core Hub did not check the drafts")


def test_a_core_hub_failure_is_reported_and_the_draft_kept(hub):
    def failing(path, **kwargs):
        if path.endswith("/discovery/data-model"):
            raise RuntimeError("CoreHub request POST failed: 500")
        return hub(path, **kwargs)

    with mock.patch.object(gs1_import, "fetch_core_hub", failing):
        plan = gs1_import.plan_import("tok", _request())

    assert "500" in plan["checks"]["ORDERS"]["error"]
    assert "ORDERS" in plan["drafts"]
    assert any("Core Hub could not check it" in line for line in gs1_import.summarize(plan))


def test_a_failing_target_node_info_is_reported_for_every_query_draft(hub):
    def failing(path, **kwargs):
        if path.endswith("/discovery/node-info") and "/agents/couchbase/" in path:
            raise RuntimeError("CoreHub request GET failed: 503")
        return hub(path, **kwargs)

    with mock.patch.object(gs1_import, "fetch_core_hub", failing):
        plan = gs1_import.plan_import("tok", _request())

    assert set(plan["drafts"]) == {"ORDERS", "KEYED"}
    assert "503" in plan["checks"]["ORDERS"]["error"]
    assert "503" in plan["checks"]["KEYED"]["error"], "the second draft must not fail on a half-filled cache"
    assert "validate" not in plan["checks"]["KEYED"]


def test_nothing_is_saved_without_confirmation(hub):
    plan = gs1_import.plan_import("tok", _request())

    with pytest.raises(gs1_import.Gs1ImportError):
        gs1_import.save_drafts("tok", PIPELINE, plan, ["ORDERS"], confirmed=False)
    assert hub.saved == []


def test_only_the_chosen_drafts_are_saved_once_confirmed(hub):
    plan = gs1_import.plan_import("tok", _request())

    saved = gs1_import.save_drafts("tok", PIPELINE, plan, ["KEYED"], confirmed=True)

    assert saved == ["KEYED"]
    assert [[e["entityName"] for e in body["entities"]] for body in hub.saved] == [["KEYED"]]
    with pytest.raises(gs1_import.Gs1ImportError):
        gs1_import.save_drafts("tok", PIPELINE, plan, ["RECENT"], confirmed=True)


def test_a_document_split_is_validated_and_its_errors_refuse_it(hub):
    request = json.loads((FIXTURES / "nosql_default.request.json").read_text())
    request["pipelineId"] = PIPELINE

    def refusing(path, **kwargs):
        if path.endswith("/config/entities/validate"):
            return {"valid": False, "issues": [{"severity": "ERROR", "code": "PARENT_LINK_MISSING", "level": "Cards",
                                                "message": "no link", "hint": "Link it."}]}
        return hub(path, **kwargs)

    with mock.patch.object(gs1_import, "fetch_core_hub", refusing):
        plan = gs1_import.plan_import("tok", request)

    [entry] = plan["report"]["perEntity"]
    assert entry["bucket"] == "UNSUPPORTED"
    assert entry["issues"][0] == {"code": "PARENT_LINK_MISSING", "severity": "ERROR", "level": "Cards",
                                  "message": "no link", "hint": "Link it."}
    assert plan["drafts"] == {}


# ---------- command line ----------

def _write_config(tmp_path):
    path = tmp_path / "gs1.json"
    path.write_text(json.dumps(GS1_CONFIG))
    return str(path)


def test_cli_convert_writes_the_report_and_saves_nothing(hub, tmp_path, capsys):
    output = tmp_path / "report.json"

    code = gs1_import.main(["--token", "tok", "convert", "--config", _write_config(tmp_path), "--pipeline", PIPELINE,
                            "--output", str(output)])

    assert code == 0
    assert hub.saved == []
    assert set(json.loads(output.read_text())["drafts"]) == {"ORDERS", "KEYED"}
    printed = capsys.readouterr().out
    assert "RECENT: UNSUPPORTED" in printed
    assert "Secret keys removed unread: sourcePassword" in printed
    assert "fixture-source-password" not in printed + output.read_text()


def test_cli_import_without_a_terminal_or_yes_saves_nothing(hub, tmp_path, capsys):
    with mock.patch("sys.stdin", io.StringIO("y\n")):
        code = gs1_import.main(["--token", "tok", "import", "--config", _write_config(tmp_path), "--pipeline", PIPELINE])

    assert code == 2
    assert hub.saved == []
    assert "did not confirm" in capsys.readouterr().err


def test_cli_import_saves_once_confirmed(hub, tmp_path):
    with mock.patch.object(gs1_import, "_confirm", return_value=True):
        code = gs1_import.main(["--token", "tok", "import", "--config", _write_config(tmp_path), "--pipeline", PIPELINE,
                                "--entities", "ORDERS"])

    assert code == 0
    assert [[e["entityName"] for e in body["entities"]] for body in hub.saved] == [["ORDERS"]]


def test_cli_import_yes_confirms_every_draft(hub, tmp_path):
    code = gs1_import.main(["--token", "tok", "import", "--config", _write_config(tmp_path), "--pipeline", PIPELINE, "--yes"])

    assert code == 0
    assert [[e["entityName"] for e in body["entities"]] for body in hub.saved] == [["ORDERS", "KEYED"]]


def _refusing_keyed(hub, valid_for=None, fail=False):
    """A Core Hub that refuses (or cannot reach) the validation of KEYED and accepts the rest."""
    def answer(path, **kwargs):
        if path.endswith("/config/entities/validate") and kwargs["body"]["entities"][0]["entityName"] == "KEYED":
            if fail:
                raise RuntimeError("CoreHub request POST failed: 401")
            return {"valid": False, "issues": [{"severity": "ERROR", "code": "ROOT_KEY_NOT_MAPPED", "message": "no"}]}
        return hub(path, **kwargs)
    return answer


def test_cli_import_without_entities_saves_only_the_drafts_checked_and_valid(hub, tmp_path, capsys):
    with mock.patch.object(gs1_import, "fetch_core_hub", _refusing_keyed(hub)):
        code = gs1_import.main(["--token", "tok", "import", "--config", _write_config(tmp_path), "--pipeline", PIPELINE,
                                "--yes"])

    assert code == 0
    assert [[e["entityName"] for e in body["entities"]] for body in hub.saved] == [["ORDERS"]]
    assert "Not saved by default: KEYED" in capsys.readouterr().out


def test_cli_import_does_not_save_a_draft_core_hub_could_not_validate(hub, tmp_path, capsys):
    with mock.patch.object(gs1_import, "fetch_core_hub", _refusing_keyed(hub, fail=True)):
        code = gs1_import.main(["--token", "tok", "import", "--config", _write_config(tmp_path), "--pipeline", PIPELINE,
                                "--yes"])

    assert code == 0
    assert [[e["entityName"] for e in body["entities"]] for body in hub.saved] == [["ORDERS"]]
    assert "KEYED (Core Hub could not check it)" in capsys.readouterr().out


def test_cli_import_names_win_over_the_default_choice(hub, tmp_path):
    with mock.patch.object(gs1_import, "fetch_core_hub", _refusing_keyed(hub)):
        code = gs1_import.main(["--token", "tok", "import", "--config", _write_config(tmp_path), "--pipeline", PIPELINE,
                                "--entities", "KEYED", "--yes"])

    assert code == 0
    assert [[e["entityName"] for e in body["entities"]] for body in hub.saved] == [["KEYED"]]


def test_without_a_check_every_draft_is_the_default_choice(hub):
    plan = gs1_import.plan_import(None, _request(), check=False)

    assert gs1_import.default_names(plan) == ["ORDERS", "KEYED", "RECENT"]


def test_cli_offline_convert_needs_the_agents(tmp_path, capsys):
    code = gs1_import.main(["convert", "--offline", "--config", _write_config(tmp_path), "--pipeline", PIPELINE])

    assert code == 2
    assert "--source-agent" in capsys.readouterr().err


# ---------- Automator routes ----------

@pytest.fixture
def client(hub):
    from fastapi.testclient import TestClient

    from automator_app import app as automator_app, gs1_routes

    with mock.patch.object(type(gs1_routes.state), "token", new_callable=mock.PropertyMock, return_value="tok"), \
            mock.patch.object(type(gs1_routes.state), "base_url", new_callable=mock.PropertyMock,
                              return_value="http://hub.test"), \
            mock.patch.object(gs1_routes.corehub, "configure_core_hub"):
        yield TestClient(automator_app.create_app())


def _convert(client, **form):
    files = {"file": ("gs1.json", json.dumps(GS1_CONFIG).encode(), "application/json")}
    return client.post("/api/gs1/convert", files=files, data={"pipelineId": PIPELINE, **form})


def test_route_convert_reports_and_keeps_the_plan(client, hub):
    response = _convert(client, rootCounts='{"ORDERS": 54321}')

    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["drafts"] == ["ORDERS", "KEYED"]
    assert plan["checks"]["ORDERS"]["describe"]["proposedKeys"] == {"ORDERS": ["ID"]}
    assert plan["checks"]["ORDERS"]["validate"]["valid"] is True
    assert plan["strippedKeys"] == ["sourcePassword"]
    assert hub.saved == []


def test_route_save_needs_the_confirmation(client, hub):
    plan_id = _convert(client).json()["planId"]

    refused = client.post("/api/gs1/save", json={"planId": plan_id, "entities": ["ORDERS"]})
    saved = client.post("/api/gs1/save", json={"planId": plan_id, "entities": ["ORDERS"], "confirmed": True})

    assert refused.status_code == 400
    assert saved.status_code == 200, saved.text
    assert saved.json()["saved"] == ["ORDERS"]
    assert [[e["entityName"] for e in body["entities"]] for body in hub.saved] == [["ORDERS"]]


def test_route_refuses_a_file_that_is_not_json_and_an_unknown_plan(client):
    bad = client.post("/api/gs1/convert", files={"file": ("x.json", b"not json", "application/json")},
                      data={"pipelineId": PIPELINE})
    unknown = client.post("/api/gs1/save", json={"planId": "nope", "entities": ["ORDERS"], "confirmed": True})

    assert bad.status_code == 400
    assert unknown.status_code == 404


def test_route_offline_conversion_calls_no_check(client, hub):
    response = _convert(client, check="false", sourceAgentId="mssql-cdc", targetAgentId="couchbase")

    assert response.status_code == 200
    assert response.json()["structureChecked"] is False
    assert hub.calls == []


def test_route_refuses_an_upload_larger_than_the_cap(client, hub):
    big = b" " * (2 * 1024 * 1024)

    with mock.patch.dict("os.environ", {"GS_IMPORT_MAX_UPLOAD_MB": "1"}):
        response = client.post("/api/gs1/convert", files={"file": ("gs1.json", big, "application/json")},
                               data={"pipelineId": PIPELINE})

    assert response.status_code == 413
    assert "GS_IMPORT_MAX_UPLOAD_MB" in response.json()["detail"]
    assert hub.calls == []


def test_route_refuses_an_empty_upload(client):
    response = client.post("/api/gs1/convert", files={"file": ("gs1.json", b"", "application/json")},
                           data={"pipelineId": PIPELINE})

    assert response.status_code == 400


def test_route_answers_400_for_a_configuration_nested_too_deeply(client, hub):
    deep = b"[" * 200_000 + b"]" * 200_000

    response = client.post("/api/gs1/convert", files={"file": ("gs1.json", deep, "application/json")},
                           data={"pipelineId": PIPELINE})

    assert response.status_code == 400
    assert "deeply" in response.json()["detail"]


def test_a_configuration_nested_too_deeply_for_the_converter_is_a_conversion_error():
    config = {"sourceEntities": {}}
    node = config
    for _ in range(5000):
        node["next"] = {}
        node = node["next"]

    with pytest.raises(gs1_import.Gs1ImportError) as raised:
        gs1_import.plan_import("tok", _request(config), check=False)

    assert "deeply" in str(raised.value)


def test_cli_answers_cleanly_for_a_configuration_nested_too_deeply(hub, tmp_path, capsys):
    path = tmp_path / "deep.json"
    path.write_text("[" * 200_000 + "]" * 200_000)

    code = gs1_import.main(["--token", "tok", "convert", "--config", str(path), "--pipeline", PIPELINE])

    assert code == 2
    err = capsys.readouterr().err
    assert "deeply" in err
    assert "Core Hub refused" not in err
