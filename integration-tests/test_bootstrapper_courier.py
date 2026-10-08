#!/usr/bin/env python3
"""
Unit tests for the HTTP Target (``api-agent-shadow``, Courier outbound) support of the bootstrapper.

They cover, without a running CoreHub:

- the normalisation of the ``courier`` configuration block into the payloads CoreHub stores
  (``EndpointCollection`` / ``EndpointDefinition``), including the shorthand defaults;
- the validation that mirrors ``EndpointCollectionValidator`` / ``EndpointDefinitionValidator``;
- the exact REST calls ``provision_courier`` issues, on first run (POST) and on re-run (PUT, ids kept);
- the ``outboundCollection`` defaulting of the agent custom host credentials;
- the SQL/NoSQL category inference for the embedded agent, even when agents.json lacks it;
- the entity payload the bootstrapper sends for a Courier target: ``NoSqlEntity`` with the Courier
  collection as scope, the endpoint as collection, the bindings as columns and a column mapping
  matrix that points source columns to the discovered binding ids.
"""

import contextlib
import copy
import io
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import courier_support  # noqa: E402
from courier_support import (  # noqa: E402
    COLLECTIONS_PATH,
    ENDPOINTS_PATH,
    CourierConfigError,
    apply_courier_defaults,
    courier_block,
    is_courier_agent,
    plan_courier_definitions,
    provision_courier,
    stable_id,
)


COURIER_CONFIG = {
    "collections": [
        {
            "name": "demo-api",
            "baseUrl": "http://wiremock:8080/",
            "auth": {"type": "Bearer", "token": "test-token"},
            "endpoints": [
                {
                    "name": "customers",
                    "httpVerb": "post",
                    "pathTemplate": "/customers/{id}",
                    "headers": [
                        {"name": "X-Operation", "valueTemplate": "{__operation}"},
                        {"name": "X-Tenant", "value": "{tenant}"},
                    ],
                    "bindings": [
                        {"name": "id", "location": "PATH", "type": "number"},
                        {"name": "name", "location": "BODY"},
                        {"name": "email", "location": "BODY", "required": False},
                        {"name": "active", "location": "BODY", "type": "BOOLEAN"},
                        {"name": "tenant", "location": "HEADER", "default": "acme"},
                    ],
                },
                {
                    "name": "orders",
                    "method": "PUT",
                    "path": "/orders/{id}",
                    "bodyType": "FORM_URLENCODED",
                    "bindings": [
                        {"name": "id", "location": "PATH", "type": "NUMBER"},
                        {"name": "region", "location": "QUERY"},
                        {"name": "status", "location": "BODY"},
                        {"name": "amount", "location": "BODY", "type": "NUMBER"},
                    ],
                },
                {
                    "name": "customers-delete",
                    "httpVerb": "DELETE",
                    "pathTemplate": "/customers/{id}",
                    "bindings": [{"name": "id", "location": "PATH"}],
                },
            ],
        }
    ]
}


class FakeCoreHub:
    """Records every call of ``provision_courier`` and answers the two GETs it issues."""

    def __init__(self, collections=None, endpoints_by_collection=None):
        self.collections = collections or []
        self.endpoints_by_collection = endpoints_by_collection or {}
        self.calls = []

    def __call__(self, path, method="GET", token=None, body=None, params=None, headers=None):
        self.calls.append((method, path, copy.deepcopy(body), copy.deepcopy(params)))
        if method == "GET" and path == COLLECTIONS_PATH:
            return copy.deepcopy(self.collections)
        if method == "GET" and path == ENDPOINTS_PATH:
            return copy.deepcopy(self.endpoints_by_collection.get((params or {}).get("collectionId"), []))
        return body


class CourierAgentDetectionTests(unittest.TestCase):
    def test_detects_the_http_target_by_tag_or_internal_name(self):
        self.assertTrue(is_courier_agent({"agentTag": "api-agent-shadow"}))
        self.assertTrue(is_courier_agent({"agentTag": "my-http-target", "agentInternalName": "API-Agent-Shadow"}))
        self.assertFalse(is_courier_agent({"agentTag": "kafka"}))
        self.assertFalse(is_courier_agent(None))

    def test_courier_block_is_a_deep_copy(self):
        agent = {"agentTag": "api-agent-shadow", "courier": {"collections": [{"name": "x"}]}}
        block = courier_block(agent)
        block["collections"][0]["name"] = "changed"
        self.assertEqual(agent["courier"]["collections"][0]["name"], "x")
        self.assertIsNone(courier_block({"agentTag": "api-agent-shadow"}))


class CourierPlanTests(unittest.TestCase):
    def test_normalises_shorthand_into_corehub_payloads(self):
        plan = plan_courier_definitions(copy.deepcopy(COURIER_CONFIG))

        self.assertEqual(len(plan), 1)
        collection = plan[0]["collection"]
        self.assertFalse(plan[0]["exists"])
        self.assertEqual(collection["id"], stable_id("collection", "demo-api"))
        self.assertEqual(collection["name"], "demo-api")
        self.assertEqual(collection["baseUrl"], "http://wiremock:8080/")
        self.assertEqual(collection["auth"], {"type": "Bearer", "token": "test-token"})
        self.assertEqual(collection["tags"], [])

        customers, orders, delete = [e["definition"] for e in plan[0]["endpoints"]]

        self.assertEqual(customers["id"], stable_id("endpoint", "customers", collection["id"]))
        self.assertEqual(customers["collectionId"], collection["id"])
        self.assertEqual(customers["httpVerb"], "POST")
        self.assertEqual(customers["bodyType"], "JSON", "POST defaults to a JSON body")
        self.assertEqual(customers["operationField"], "operation")
        self.assertEqual(customers["headers"], [
            {"name": "X-Operation", "valueTemplate": "{__operation}", "description": None},
            {"name": "X-Tenant", "valueTemplate": "{tenant}", "description": None},
        ])
        self.assertEqual(customers["bindings"][0], {
            "name": "id", "type": "NUMBER", "location": "PATH", "defaultValue": None, "required": True, "description": None,
        })
        self.assertEqual(customers["bindings"][1]["type"], "STRING", "binding type defaults to STRING")
        self.assertFalse(customers["bindings"][2]["required"])
        self.assertEqual(customers["bindings"][3]["type"], "BOOLEAN")
        self.assertEqual(customers["bindings"][4]["defaultValue"], "acme")
        self.assertEqual(customers["bindings"][4]["location"], "HEADER")

        self.assertEqual(orders["httpVerb"], "PUT")
        self.assertEqual(orders["pathTemplate"], "/orders/{id}", "'path' is accepted as a shorthand for pathTemplate")
        self.assertEqual(orders["bodyType"], "FORM_URLENCODED")

        self.assertEqual(delete["httpVerb"], "DELETE")
        self.assertEqual(delete["bodyType"], "NONE", "a verb without body defaults to bodyType NONE")
        self.assertEqual(delete["bindings"], [{
            "name": "id", "type": "STRING", "location": "PATH", "defaultValue": None, "required": True, "description": None,
        }])

    def test_reuses_ids_of_definitions_already_stored(self):
        existing_collections = [{"id": "col-1", "name": "demo-api", "baseUrl": "http://old"}]
        existing_endpoints = {"col-1": [{"id": "ep-1", "name": "customers"}]}

        plan = plan_courier_definitions(
            copy.deepcopy(COURIER_CONFIG),
            existing_collections,
            lambda collection_id: existing_endpoints.get(collection_id, []),
        )

        self.assertTrue(plan[0]["exists"])
        self.assertEqual(plan[0]["collection"]["id"], "col-1")
        by_name = {e["definition"]["name"]: e for e in plan[0]["endpoints"]}
        self.assertTrue(by_name["customers"]["exists"])
        self.assertEqual(by_name["customers"]["definition"]["id"], "ep-1")
        self.assertEqual(by_name["customers"]["definition"]["collectionId"], "col-1")
        self.assertFalse(by_name["orders"]["exists"])
        self.assertEqual(by_name["orders"]["definition"]["id"], stable_id("endpoint", "orders", "col-1"))

    def test_stable_ids_do_not_change_between_runs(self):
        self.assertEqual(stable_id("collection", "demo-api"), stable_id("collection", "demo-api"))
        self.assertNotEqual(stable_id("collection", "demo-api"), stable_id("collection", "other"))
        self.assertNotEqual(stable_id("endpoint", "customers", "a"), stable_id("endpoint", "customers", "b"))

    def _assert_rejected(self, config, *fragments):
        with self.assertRaises(CourierConfigError) as raised:
            plan_courier_definitions(config)
        message = str(raised.exception)
        for fragment in fragments:
            self.assertIn(fragment, message)

    def test_rejects_what_corehub_validators_reject(self):
        base = lambda **endpoint: {"collections": [{  # noqa: E731
            "name": "c", "baseUrl": "http://api", "endpoints": [{"name": "e", "httpVerb": "POST", "pathTemplate": "/x", **endpoint}],
        }]}

        self._assert_rejected({"collections": [{"name": "c", "baseUrl": "ftp://api", "endpoints": [{"name": "e"}]}]},
                              "baseUrl must be an http:// or https:// URL")
        self._assert_rejected({"collections": [{"name": "", "baseUrl": "http://api", "endpoints": [{"name": "e"}]}]},
                              "name is required")
        self._assert_rejected({"collections": [{"name": "c", "baseUrl": "http://api", "auth": {"type": "Bearer"},
                                                "endpoints": [{"name": "e"}]}]}, "auth.token is required")
        self._assert_rejected({"collections": [{"name": "c", "baseUrl": "http://api", "auth": {"type": "ApiKey", "name": "k"},
                                                "endpoints": [{"name": "e"}]}]}, "auth.value is required")
        self._assert_rejected({"collections": [{"name": "c", "baseUrl": "http://api", "endpoints": []}]},
                              "declares no endpoint")
        self._assert_rejected(base(bindings=[{"name": "a"}, {"name": "a"}]), "binding 'a' is declared twice")
        self._assert_rejected(base(bindings=[{"name": "1bad"}]), "binding name '1bad' is not valid")
        self._assert_rejected(base(bindings=[{"name": "a", "location": "SOMEWHERE"}]), "location 'SOMEWHERE' is not one of")
        self._assert_rejected(base(httpVerb="GET", bindings=[{"name": "a", "location": "BODY"}]), "BODY bindings need a body")
        self._assert_rejected(base(bodyType="NONE", bindings=[{"name": "a", "location": "BODY"}]), "BODY bindings need a body")
        self._assert_rejected(base(operationField="a", bindings=[{"name": "a", "location": "BODY"}]),
                              "operationField 'a' is also a binding name")
        self._assert_rejected(base(pathTemplate="/x/{missing}"), "placeholder '{missing}' has no binding")
        self._assert_rejected(base(pathTemplate="x/{id}", bindings=[{"name": "id", "location": "PATH"}]), "must start with '/'")
        self._assert_rejected(base(httpVerb="FETCH"), "httpVerb 'FETCH' is not one of")
        self._assert_rejected({"collections": "nope"}, "'courier.collections' must be a non-empty list")

    def test_operation_placeholder_is_allowed_without_a_binding(self):
        config = {"collections": [{"name": "c", "baseUrl": "http://api", "endpoints": [
            {"name": "e", "httpVerb": "POST", "pathTemplate": "/events/{__operation}", "bindings": []},
        ]}]}
        plan = plan_courier_definitions(config)
        self.assertEqual(plan[0]["endpoints"][0]["definition"]["pathTemplate"], "/events/{__operation}")


class ProvisionCourierTests(unittest.TestCase):
    def test_first_run_creates_collection_then_endpoints_in_order(self):
        hub = FakeCoreHub()

        names = provision_courier("token", copy.deepcopy(COURIER_CONFIG), fetch=hub)

        self.assertEqual(names, ["demo-api"])
        methods_and_paths = [(method, path) for method, path, _, _ in hub.calls]
        self.assertEqual(methods_and_paths, [
            ("GET", COLLECTIONS_PATH),
            ("POST", COLLECTIONS_PATH),
            ("POST", ENDPOINTS_PATH),
            ("POST", ENDPOINTS_PATH),
            ("POST", ENDPOINTS_PATH),
        ])
        collection_body = hub.calls[1][2]
        self.assertEqual(collection_body["name"], "demo-api")
        self.assertEqual(collection_body["auth"]["token"], "test-token")
        endpoint_bodies = [call[2] for call in hub.calls[2:]]
        self.assertEqual([e["name"] for e in endpoint_bodies], ["customers", "orders", "customers-delete"])
        self.assertTrue(all(e["collectionId"] == collection_body["id"] for e in endpoint_bodies))
        # The endpoint discovery of a collection that does not exist yet is never asked for.
        self.assertFalse(any(path == ENDPOINTS_PATH and method == "GET" for method, path, _, _ in hub.calls))

    def test_second_run_updates_in_place_and_keeps_ids(self):
        collection_id = stable_id("collection", "demo-api")
        hub = FakeCoreHub(
            collections=[{"id": collection_id, "name": "demo-api", "baseUrl": "http://wiremock:8080/", "auth": {"type": "Bearer"}}],
            endpoints_by_collection={collection_id: [
                {"id": "stored-customers", "name": "customers", "collectionId": collection_id},
            ]},
        )

        provision_courier("token", copy.deepcopy(COURIER_CONFIG), fetch=hub)

        methods_and_paths = [(method, path) for method, path, _, _ in hub.calls]
        self.assertEqual(methods_and_paths, [
            ("GET", COLLECTIONS_PATH),
            ("GET", ENDPOINTS_PATH),
            ("PUT", f"{COLLECTIONS_PATH}/{collection_id}"),
            ("PUT", f"{ENDPOINTS_PATH}/stored-customers"),
            ("POST", ENDPOINTS_PATH),
            ("POST", ENDPOINTS_PATH),
        ])
        self.assertEqual(hub.calls[1][3], {"collectionId": collection_id})
        self.assertEqual(hub.calls[3][2]["id"], "stored-customers")
        self.assertEqual(hub.calls[3][2]["collectionId"], collection_id)

    def test_invalid_configuration_sends_nothing(self):
        hub = FakeCoreHub()
        with self.assertRaises(CourierConfigError):
            provision_courier("token", {"collections": [{"name": "c", "baseUrl": "nope", "endpoints": [{"name": "e"}]}]}, fetch=hub)
        self.assertEqual([m for m, _, _, _ in hub.calls], ["GET"], "only the read of the existing collections happened")


class OutboundCollectionDefaultTests(unittest.TestCase):
    def test_single_collection_fills_outbound_collection(self):
        agent = {"agentTag": "api-agent-shadow", "customHostCredentials": {}}
        apply_courier_defaults(agent, ["demo-api"])
        self.assertEqual(agent["customHostCredentials"], {"outboundCollection": "demo-api"})

    def test_explicit_outbound_collection_is_kept(self):
        agent = {"agentTag": "api-agent-shadow", "customHostCredentials": {"outboundCollection": "other", "inboundPort": 8090}}
        apply_courier_defaults(agent, ["demo-api"])
        self.assertEqual(agent["customHostCredentials"]["outboundCollection"], "other")
        self.assertEqual(agent["customHostCredentials"]["inboundPort"], 8090)

    def test_several_collections_leave_it_unset(self):
        agent = {"agentTag": "api-agent-shadow"}
        apply_courier_defaults(agent, ["a", "b"])
        self.assertEqual(agent["customHostCredentials"], {})


class EmbeddedAgentCategoryTests(unittest.TestCase):
    def test_commons_exposes_the_http_target_as_object_store(self):
        import commons

        self.assertEqual(commons.EMBEDDED_AGENT_CATEGORIES["api-agent-shadow"], "Object Store")
        self.assertEqual(commons.embedded_agent_categories(str.upper), {"api-agent-shadow": "OBJECT STORE"})

    def test_export_inference_knows_the_http_target_without_agents_json(self):
        import export_template_from_corehub as export

        with mock.patch.object(export, "_AGENT_TYPE_BY_NAME", None), \
             mock.patch.object(Path, "exists", return_value=False):
            catalog = export._load_agent_type_catalog()
        export._AGENT_TYPE_BY_NAME = None
        self.assertEqual(catalog.get("api-agent-shadow"), "NoSQL")

    def test_automator_inference_treats_the_http_target_as_nosql(self):
        import json
        import tempfile

        from automator_app import corehub

        # An agents.json without the HTTP Target row, as the backoffice export may still produce.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"data": [{"internalName": "kafka", "type": "Event Streaming"}]}, fh)
            agents_file = fh.name
        with mock.patch.object(corehub, "_AGENT_TYPE_BY_NAME", None), \
             mock.patch.object(corehub, "_get_agents_file_path", return_value=agents_file):
            catalog = corehub._load_agent_type_catalog()
        corehub._AGENT_TYPE_BY_NAME = None
        self.assertEqual(catalog.get("kafka"), "EVENT STREAMING")
        self.assertEqual(corehub._normalize_agent_category(catalog.get("api-agent-shadow")), "NoSQL")

    def test_agents_json_catalog_lists_the_http_target_as_target_only_object_store(self):
        import json

        with (PROJECT_ROOT / "agents.json").open(encoding="utf-8") as fh:
            entries = json.load(fh)["data"]
        entry = next(e for e in entries if e.get("internalName") == "api-agent-shadow")
        self.assertEqual(entry["type"], "Object Store")
        self.assertTrue(entry["isTarget"])
        self.assertFalse(entry["isSource"])

    def test_object_store_targets_skip_create_table(self):
        import create_all_entities

        self.assertTrue(create_all_entities.is_no_create_table_target("Object Store"))
        self.assertTrue(create_all_entities.is_no_create_table_target("NoSQL"))


COURIER_TABLES_TEMPLATE = """
public:
  target: demo-api
  tables:
    whitelist:
      - customers
    blacklist: []
    custom:
      customers:
        keys:
          - id
        columns:
          - id: id
          - full_name: name
          - email: email
"""


def _column(name, idx, data_type="varchar", is_pk=False, nullable=True):
    return {
        "id": idx,
        "position": idx,
        "name": name,
        "dataType": data_type,
        "isPK": is_pk,
        "isNullable": nullable,
        "charMaxLength": 255 if data_type.lower() in {"varchar"} else 0,
        "numPrec": 0,
        "numScale": 0,
    }


# What the HTTP Target discovery answers for the `customers` endpoint: one column per binding, the
# required PATH binding as primary key (EndpointCache.columnsFor), ids = Java hashCode of the name.
BINDING_COLUMNS = [
    _column("id", 3355, "NUMERIC", is_pk=True, nullable=False),
    _column("name", 3373707, "VARCHAR", nullable=False),
    _column("email", 96619420, "VARCHAR", nullable=True),
    _column("active", -1422950650, "BOOLEAN", nullable=False),
]

SOURCE_COLUMNS = [
    _column("id", 1, "int", is_pk=True, nullable=False),
    _column("full_name", 2),
    _column("email", 3),
    _column("created_at", 4, "timestamp"),
]


class CreateEntitiesForCourierTargetTests(unittest.TestCase):
    def test_entity_payload_maps_source_columns_onto_bindings(self):
        import create_all_entities

        yaml_config = yaml.safe_load(COURIER_TABLES_TEMPLATE)
        captured_puts = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path.endswith("/config/entities") and method == "PUT":
                captured_puts.append(copy.deepcopy(body))
                return {"ok": True}
            if path.endswith("/entities"):
                entities = captured_puts[-1]["entities"] if captured_puts else []
                return [{"entity": {"entityId": f"entity-{idx}", **entity}} for idx, entity in enumerate(entities, 1)]
            return {}

        def fake_columns(token, pipeline_id, agent_id, schema, table):
            if agent_id == "target-agent":
                self.assertEqual((schema, table), ("demo-api", "customers"), "target discovery is asked for collection/endpoint")
                return {"columns": copy.deepcopy(BINDING_COLUMNS)}
            return {"columns": copy.deepcopy(SOURCE_COLUMNS)}

        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
             mock.patch.object(create_all_entities, "get_node_info", side_effect=[
                 {"category": "RDBMS", "dataTypesMatrix": []},
                 {"category": "Object Store", "dataTypesMatrix": []},
             ]), \
             mock.patch.object(create_all_entities, "get_agent_tables",
                               return_value=[{"name": "customers", "schema": "demo-api", "id": 777}]), \
             mock.patch.object(create_all_entities, "get_table_columns", side_effect=fake_columns), \
             mock.patch.object(create_all_entities, "map_data_type", side_effect=lambda source_type, *args, **kwargs: source_type), \
             mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=fake_fetch), \
             contextlib.redirect_stdout(io.StringIO()):
            result = create_all_entities.create_entities(
                token="token",
                pipeline_id="pipeline",
                source_schema="public",
                target_schema="demo-api",
                tables=[{"name": "customers", "schema": "public", "id": 101}],
                source_agent_id="source-agent",
                target_agent_id="target-agent",
                source_type="SQL",
                target_type="NoSQL",
                yaml_config=yaml_config,
                chunk_size=50,
                source_agent_tag="postgresql-cdc",
                target_agent_tag="api-agent-shadow",
            )

        self.assertEqual(result, {"successful": 1, "failed": 0, "total": 1})
        entity = captured_puts[0]["entities"][0]
        source_entity, target_entity = entity["agentEntities"]

        # The HTTP Target implements NoSqlEntity only: scope = Courier collection, collection = endpoint.
        self.assertEqual(target_entity["type"], "NoSqlEntity")
        self.assertEqual(target_entity["agentId"], "target-agent")
        self.assertEqual(target_entity["entityObject"], {"id": "777", "scope": "demo-api", "collection": "customers"})
        self.assertEqual(target_entity["table"]["schema"], "demo-api")
        self.assertEqual(target_entity["table"]["name"], "customers")

        # Target columns are the bindings the source columns are mapped onto, with the discovered ids.
        target_columns = {c["name"]: c for c in target_entity["columns"]}
        self.assertEqual(set(target_columns), {"id", "name", "email"}, "unmapped source columns are not sent")
        self.assertEqual(target_columns["id"]["id"], 3355)
        self.assertEqual(target_columns["name"]["id"], 3373707)
        self.assertEqual(target_columns["email"]["id"], 96619420)
        self.assertEqual(target_columns["id"]["dataType"], "NUMERIC")
        self.assertTrue(target_columns["id"]["isPK"])
        self.assertFalse(target_columns["name"]["isPK"])
        self.assertTrue(target_columns["email"]["isNullable"])

        # The mapping matrix is what OutboundHttpWriter.mapToTargetColumns uses to rename
        # `full_name` into the `name` binding: source column id -> discovered binding id.
        matrix = target_entity["entityType"]["columnsMappingMatrix"]
        self.assertEqual(
            sorted((m["sourceColumnId"], m["targetColumnId"]) for m in matrix),
            [(1, 3355), (2, 3373707), (3, 96619420)],
        )
        self.assertTrue(all(m["sourceTableObjectId"] == 101 and m["targetTableObjectId"] == 777 for m in matrix))
        self.assertEqual(target_entity["entityType"]["tablesWithUnlockedSchema"], [])

        # No CREATE TABLE and no key mapping: the endpoint has its own key (the PATH binding).
        self.assertNotIn("keyMapping", target_entity)
        self.assertEqual(source_entity["type"], "SingleTable")


if __name__ == "__main__":
    unittest.main()
