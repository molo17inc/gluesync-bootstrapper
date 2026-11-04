"""The GS1 configuration converter reproduces Core Hub's converter (Gluesync 2.3 query-entity branch) byte for byte.

``fixtures/gs1_converter`` holds, per case, the request Core Hub's converter took, the response it gave, and every
structure check it ran (the model or draft it checked and the issues it found), captured from the Kotlin converter
on the inputs of its own unit tests. The structure checks parse the level SQL and stay in Core Hub: the test replays
them, after asserting that the Python converter asks to check exactly the model Core Hub checked.
"""

import json
from pathlib import Path

import pytest

from gs1_migration import ConversionError, conversion_request, convert, load_config, strip
from gs1_migration.json_values import as_bool, as_int, content, json_text, take_utf16
from gs1_migration.model import column_id, level_table_id, table_or_object_id

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gs1_converter"
CASES = sorted(path.name[: -len(".request.json")] for path in FIXTURES.glob("*.request.json"))


def _read(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def _compact(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


class _Replay:
    """The structure checks Core Hub ran, in order; each call must check the same entity and model."""

    def __init__(self, records):
        self.records = list(records)
        self.calls = 0

    def __call__(self, subject):
        assert self.calls < len(self.records), f"unexpected structure check of {subject['entity']}"
        record = self.records[self.calls]
        self.calls += 1
        assert (subject["entity"], subject["kind"]) == (record["entity"], record["kind"])
        if subject["kind"] == "sql":
            assert subject["keysProposed"] == record["keysProposed"], subject["entity"]
            assert _compact(subject["model"]) == _compact(record["model"]), subject["entity"]
        else:
            assert _compact(subject["draft"]) == _compact(record["entity_draft"]), subject["entity"]
        return record["issues"]


def test_every_case_of_the_kotlin_tests_is_captured():
    assert len(CASES) == 49
    assert {case.split("_")[0] for case in CASES} == {"sql", "nosql"}


@pytest.mark.parametrize("case", CASES)
def test_the_response_is_the_golden_byte_for_byte(case):
    request = load_config(_read(f"{case}.request.json"))
    replay = _Replay(json.loads(_read(f"{case}.structure.json")))

    response = convert(request, structure_check=replay)

    assert _compact(response) == _read(f"{case}.response.json")
    assert replay.calls == len(replay.records)


def test_the_secret_stripper_keeps_the_names_of_a_fields_or_keys_map():
    golden = json.loads(_read("strip_fields_and_keys.json"))

    config, paths = strip(golden["input"])

    assert paths == golden["paths"] == ["targetPassword", "table.options.apiToken"]
    assert _compact(config) == _compact(golden["config"])


def test_secrets_are_never_echoed_in_the_report():
    request = load_config(_read("sql_fixture_secrets.request.json"))

    echoed = _compact(convert(request))

    for secret in ("fixture-source-password", "fixture-target-password", "fixture-root-certificate", "fixture-api-token"):
        assert secret not in echoed


def test_without_structure_checks_every_draft_the_converter_accepts_is_returned():
    request = load_config(_read("sql_fixture.request.json"))

    report = convert(request)

    unsupported = {entry["gs1Name"] for entry in report["perEntity"] if entry["bucket"] == "UNSUPPORTED"}
    assert unsupported == set()
    assert len(report["entities"]) == 37
    assert all(issue["code"].startswith("GS1_") for entry in report["perEntity"] for issue in entry["issues"])


def test_a_structure_error_keeps_the_draft_and_sets_the_migration_hint_on_a_joined_table_key():
    request = conversion_request(
        {"couchbase": {"useCollections": True}, "sourceEntities": {"ORD": {"type": "ORD", "scope": "S", "queryDataModeling": {
            "primaryTableKeys": ["ID"], "levels": [
                {"level": 1, "name": "ORD", "levelKeys": ["ID"], "query": "SELECT o.ID FROM dbo.ORDERS o"},
                {"level": 2, "name": "LINES", "levelKeys": ["ID", "K"], "query": "SELECT l.ID, c.K FROM dbo.L l JOIN dbo.C c"},
            ]}}}},
        "pipeline", "source", "target",
    )
    joined = {"code": "KEY_NOT_PLAIN_COLUMN", "severity": "ERROR", "level": "LINES", "location": "K",
              "message": "K is a column of dbo.C, not of dbo.L, the table of the level's root key", "hint": "catalogue"}

    report = convert(request, structure_check=lambda subject: [joined])

    assert report["entities"] == []
    [entry] = report["perEntity"]
    assert entry["bucket"] == "UNSUPPORTED"
    assert entry["issues"][0]["hint"].startswith("Move the array LINES into an entity of its own, keyed on the table that holds K")


def test_root_counts_set_one_percent_of_the_roots_as_the_delete_window():
    request = load_config(_read("sql_fixture_deterministic_root_counts.request.json"))

    report = convert(request, structure_check=_Replay(json.loads(_read("sql_fixture_deterministic_root_counts.structure.json"))))

    windows = {e["entityName"]: e["agentEntities"][0]["model"]["options"]["maxDeletesPerWindow"] for e in report["entities"]}
    assert windows["ENTITY_03"] == 543
    assert windows["ENTITY_04"] == 100
    assert windows["ENTITY_05"] == 100


def test_an_unknown_repository_is_refused():
    with pytest.raises(ValueError):
        conversion_request({}, "p", "s", "t", repository="SQL_TO_SQL")


def test_a_failure_names_no_configuration_value():
    request = conversion_request(
        {"sourceEntities": {"E": {"dataModeling": [
            {"level": 1, "name": "E", "tables": [{"table": "dbo.E", "fromTable": True, "keys": {"id": "ID"}}]},
            {"level": 2, "name": "secret-array-name", "tables": []},
        ]}}},
        "p", "s", "t", repository="NOSQL_TO_SQL",
    )

    with pytest.raises(ConversionError) as raised:
        convert(request)

    assert "secret-array-name" not in str(raised.value)


def test_values_read_as_core_hub_reads_them():
    config = load_config('{"a": 1.50, "b": 1e3, "c": -0, "d": "7", "e": null, "f": true}')

    assert [content(config[k]) for k in "abcdef"] == ["1.50", "1e3", "-0", "7", "null", "true"]
    assert as_int(config["d"]) == 7
    assert as_int(config["a"]) is None
    assert as_int("2147483648") is None
    assert as_int("\u0661") is None, "only ASCII digits, as Core Hub reads them"
    assert as_bool("true") is True
    assert as_bool("yes") is None
    assert json_text({"x": [1, "y\n"], "z": config["a"]}) == '{"x":[1,"y\\n"],"z":1.50}'
    assert take_utf16("a\U0001F600b", 3) == "a\U0001F600"


def test_ids_are_core_hub_ids():
    golden = json.loads(_read("sql_item_key_reordered.response.json"))
    target = golden["entities"][0]["agentEntities"][1]

    assert target["entityObject"]["id"] == table_or_object_id("S", "ORD")
    assert [c["id"] for c in target["columns"]] == [column_id("type"), column_id("scope")]
    assert level_table_id("Cards") == table_or_object_id("#gs_model", "Cards")


@pytest.mark.parametrize("message, joined", [
    ("K is a column of dbo.C, not of dbo.L, the table of the level's root key", True),
    ("K is a column of X, not of Y, the table of the level's root key.", True),
    (" is a column of X, not of Y, the table of the level's root key", True),
    (" is a column of , not of , the table of the level's root key", False),
    ("K is a column of X, not of Y,\n the table of the level's root key", False),
    ("K is a column of X\u2028, not of Y, the table of the level's root key", False),
    ("first line\nK is a column of X, not of Y, the table of the level's root key", True),
    ("K is not a column of anything", False),
    ("", False),
])
def test_the_joined_table_message_is_recognised_as_core_hub_words_it(message, joined):
    """As Core Hub's `" is a column of .+, not of .+, the table of the level's root key"` finds it on one line."""
    from gs1_migration.converter import _names_a_joined_table

    assert _names_a_joined_table(message) is joined
