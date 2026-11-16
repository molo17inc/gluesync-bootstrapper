#!/usr/bin/env python3
"""
Unit tests for the entities of Courier EXECUTE routes (courier_routine_entities), without a running Core Hub.

The entity built on an EXECUTE route has the routine as its target: each IN or INOUT parameter takes the binding the
``call`` names (by default the binding of its name, with or without ``p_``), and one output of the routine is mapped onto
the route's ``_response`` column. The payload mirrors what Core Hub's MCP tool ``draft_routine_entity`` drafts.
"""

import copy
import json
import logging
import sys
import unittest
from pathlib import Path

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from courier_routine_entities import (  # noqa: E402
    ROUTINE_TARGET_PATH,
    ROUTINES_PATH,
    binding_matches,
    build_entity,
    create_routine_entities,
    default_output,
    plan_call,
)
from courier_support import CourierConfigError  # noqa: E402

PIPELINE, SOURCE, TARGET = "p1", "http-source", "pg"

ROUTE = {
    "name": "Place order",
    "httpVerb": "POST",
    "path": "/place-order",
    "bindings": [{"name": "customer"}, {"name": "AMOUNT", "type": "NUMBER"}, {"name": "X-Tenant", "location": "HEADER"}],
    "routine": {"answer": {"body": "ALL", "format": "JSON"}},
}

SOURCE_COLUMNS = [
    {"id": 101, "tableId": 100, "name": "customer"},
    {"id": 102, "tableId": 100, "name": "AMOUNT"},
    {"id": 103, "tableId": 100, "name": "X-Tenant"},
    {"id": 104, "tableId": 100, "name": "_response"},
    {"id": 105, "tableId": 100, "name": "_seq"},
]

DEFINITION = {
    "kind": "PROCEDURE",
    "parameters": [
        {"name": "p_customer", "mode": "IN", "sqlType": "VARCHAR", "nativeType": "text"},
        {"name": "p_amount", "mode": "IN", "sqlType": "NUMERIC", "nativeType": "numeric"},
        {"name": "p_note", "mode": "IN", "sqlType": "VARCHAR", "nativeType": "text"},
        {"name": "p_total", "mode": "OUT", "sqlType": "NUMERIC", "nativeType": "numeric"},
    ],
}


def target(definition=None):
    definition = definition or DEFINITION
    names = [p["name"] for p in definition["parameters"]] + ["_result", "_rows", "_value"]
    if definition.get("kind") == "FUNCTION":
        names.append("_return")
    return {
        "table": {"id": 200, "schema": "shop", "name": "place_order", "type": "ROUTINE"},
        "columns": [{"id": 201 + i, "tableId": 200, "name": n} for i, n in enumerate(names)],
        "customProperties": {"courierRoutine": json.dumps(definition)},
    }


def mapping(entity):
    """Source column name -> the target column names it is mapped onto."""
    source, target_side = entity["agentEntities"]
    source_names = {c["id"]: c["name"] for c in source["columns"]}
    target_names = {c["id"]: c["name"] for c in target_side["columns"]}
    return {source_names[e["sourceColumnIds"][0]]: [target_names[i] for i in e["targetColumnIds"]]
            for e in target_side["entityType"]["columnsMappingMatrix"]}


class PlanCallTests(unittest.TestCase):
    def test_each_parameter_takes_the_binding_of_its_name_and_the_whole_answer_by_default(self):
        plan = plan_call(ROUTE, {"schema": "shop", "name": "place_order"}, target())
        self.assertEqual(plan["inputs"], {"p_customer": "customer", "p_amount": "AMOUNT", "p_note": None})
        self.assertEqual(plan["response"], "_result")
        self.assertEqual(plan["warnings"], ["parameter 'p_note' of 'shop.place_order' takes no binding: it is called with NULL"])

    def test_explicit_bindings_and_output_win(self):
        route = {**ROUTE, "routine": {"answer": {"body": "OUTPUT"}}}
        plan = plan_call(route, {"schema": "shop", "name": "place_order", "response": "p_total",
                                 "parameters": {"p_note": "X-Tenant", "p_amount": None}}, target())
        self.assertEqual(plan["inputs"], {"p_customer": "customer", "p_amount": None, "p_note": "X-Tenant"})
        self.assertEqual(plan["response"], "p_total")

    def test_a_parameter_a_binding_or_an_output_that_does_not_exist_is_refused(self):
        with self.assertRaises(CourierConfigError) as raised:
            plan_call(ROUTE, {"schema": "shop", "name": "place_order", "response": "p_customer",
                              "parameters": {"p_x": "customer", "p_note": "nope"}}, target())
        message = str(raised.exception)
        self.assertIn("has no parameter 'p_x'", message)
        self.assertIn("has no binding 'nope'", message)
        self.assertIn("'p_customer' is no output", message)

    def test_the_default_output_follows_the_route_return_type(self):
        self.assertEqual(default_output("ROWS", []), "_rows")
        self.assertEqual(default_output("VALUE", []), "_value")
        self.assertEqual(default_output("OUTPUT", ["p_total", "_result", "_return"]), "_return")
        self.assertEqual(default_output("OUTPUT", ["p_total", "_result"]), "p_total")
        with self.assertRaises(CourierConfigError):
            default_output("OUTPUT", ["p_a", "p_b", "_result"])

    def test_a_binding_matches_its_parameter_ignoring_case_and_the_p_prefix(self):
        self.assertTrue(binding_matches("Amount", "p_amount"))
        self.assertTrue(binding_matches("p_amount", "P_AMOUNT"))
        self.assertFalse(binding_matches("amount", "p_amounts"))

    def test_an_unreadable_definition_is_refused(self):
        with self.assertRaises(CourierConfigError):
            plan_call(ROUTE, {"schema": "shop", "name": "x"}, {"customProperties": {"courierRoutine": "{nope"}})
        with self.assertRaises(CourierConfigError):
            plan_call(ROUTE, {"schema": "shop", "name": "x"}, {"customProperties": {}})


class BuildEntityTests(unittest.TestCase):
    def test_the_route_table_feeds_the_routine_and_the_response_takes_one_output(self):
        entity = build_entity(ROUTE, {"schema": "shop", "name": "place_order"}, "shop", SOURCE, SOURCE_COLUMNS, TARGET,
                              target(), entity_id="e1")

        self.assertEqual((entity["entityId"], entity["entityName"], entity["groupId"]), ("e1", "Place order", "_default"))
        source, routine = entity["agentEntities"]
        self.assertEqual((source["type"], source["agentId"]), ("NoSqlEntity", SOURCE))
        self.assertEqual(source["entityObject"], {"id": 100, "scope": "shop", "collection": "Place order"})
        self.assertEqual((routine["type"], routine["agentId"], routine["table"]["type"]), ("SingleTable", TARGET, "ROUTINE"))
        self.assertEqual(json.loads(routine["customProperties"]["courierRoutine"]), DEFINITION)
        self.assertEqual(routine["tablesProperties"], {"shop.place_order": {}})
        self.assertEqual(mapping(entity), {
            "customer": ["p_customer"], "AMOUNT": ["p_amount"], "X-Tenant": [], "_response": ["_result"], "_seq": [],
        })
        self.assertTrue(all(e["targetTableObjectId"] == 200 and e["sourceTableObjectId"] == 100
                            for e in routine["entityType"]["columnsMappingMatrix"]))

    def test_a_route_the_http_source_does_not_list_is_refused(self):
        with self.assertRaises(CourierConfigError) as raised:
            build_entity(ROUTE, {"schema": "shop", "name": "place_order"}, "shop", SOURCE, [], TARGET, target())
        self.assertIn("does not list route 'Place order'", str(raised.exception))


class FakeCoreHub:
    def __init__(self, routines=None, entities=None):
        self.routines = routines if routines is not None else [{"name": "place_order", "kind": "PROCEDURE"}]
        self.entities = entities or []
        self.calls = []

    def __call__(self, path, method="GET", token=None, body=None, params=None, headers=None):
        self.calls.append((method, path, copy.deepcopy(params), copy.deepcopy(body)))
        if path == ROUTINES_PATH:
            return copy.deepcopy(self.routines)
        if path == ROUTINE_TARGET_PATH:
            return target()
        if path.endswith("/discovery/columns"):
            return {"columns": copy.deepcopy(SOURCE_COLUMNS)}
        if path == f"/pipelines/{PIPELINE}/entities":
            return copy.deepcopy(self.entities)
        return body


def agents(call=None):
    route = {**ROUTE, "call": call or {"schema": "shop", "name": "place_order"}}
    return [
        {"agentId": SOURCE, "agentType": "SOURCE", "agentTag": "courier-agent",
         "courier": {"collections": [{"name": "shop", "endpoints": [{"name": "w", "path": "/", "bindings": ["a"]}, route]}]}},
        {"agentId": TARGET, "agentType": "TARGET", "agentTag": "postgresql-cdc"},
    ]


class CreateRoutineEntitiesTests(unittest.TestCase):
    def test_reads_the_kind_the_target_and_the_route_table_then_saves_the_entities(self):
        hub = FakeCoreHub(entities=[{"entity": {"entityId": "stored", "entityName": "Place order"}}])

        names = create_routine_entities("jwt", PIPELINE, agents(), fetch=hub)

        self.assertEqual(names, ["Place order"])
        methods_and_paths = [(m, p) for m, p, _, _ in hub.calls]
        self.assertEqual(methods_and_paths, [
            ("GET", f"/pipelines/{PIPELINE}/entities"),
            ("GET", ROUTINES_PATH),
            ("GET", ROUTINE_TARGET_PATH),
            ("GET", f"/pipelines/{PIPELINE}/agents/{SOURCE}/discovery/columns"),
            ("PUT", f"/pipelines/{PIPELINE}/config/entities"),
        ])
        self.assertEqual(hub.calls[2][2], {"pipelineId": PIPELINE, "agentId": TARGET, "schema": "shop",
                                           "name": "place_order", "kind": "PROCEDURE"})
        self.assertEqual(hub.calls[3][2], {"tableschema": "shop", "tablename": "Place order"})
        (entity,) = hub.calls[4][3]["entities"]
        self.assertEqual(entity["entityId"], "stored", "a re-run updates the entity of that name")
        self.assertNotIn("warnings", entity)

    def test_a_declared_kind_skips_the_catalog(self):
        hub = FakeCoreHub()
        create_routine_entities("jwt", PIPELINE, agents({"schema": "shop", "name": "place_order", "kind": "function"}), fetch=hub)
        self.assertNotIn(ROUTINES_PATH, [p for _, p, _, _ in hub.calls])
        self.assertEqual(hub.calls[1][2]["kind"], "FUNCTION")

    def test_nothing_to_do_without_a_call(self):
        hub = FakeCoreHub()
        source = agents()[0]
        source["courier"]["collections"][0]["endpoints"].pop()
        self.assertEqual(create_routine_entities("jwt", PIPELINE, [source, agents()[1]], fetch=hub), [])
        self.assertEqual(hub.calls, [])

    def test_refusals(self):
        cases = [
            (agents({"schema": "shop"}), FakeCoreHub(), "needs the routine's 'schema' and 'name'"),
            (agents({"schema": "shop", "name": "place_order", "kind": "TRIGGER"}), FakeCoreHub(), "call.kind 'TRIGGER'"),
            (agents(), FakeCoreHub(routines=[]), "is not in the catalog"),
            (agents(), FakeCoreHub(routines=[{"name": "place_order"}] * 2), "is more than once in the catalog"),
            (agents()[:1], FakeCoreHub(), "the pipeline has 0 target agents"),
        ]
        for configured, hub, fragment in cases:
            with self.subTest(fragment=fragment), self.assertRaises(CourierConfigError) as raised:
                create_routine_entities("jwt", PIPELINE, configured, fetch=hub)
            self.assertIn(fragment, str(raised.exception))
            self.assertNotIn("PUT", [m for m, _, _, _ in hub.calls], "nothing saved")


if __name__ == "__main__":
    unittest.main()
