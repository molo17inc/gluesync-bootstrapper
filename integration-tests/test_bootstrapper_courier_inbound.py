#!/usr/bin/env python3
"""
Unit tests for the HTTP Source (``api-agent-shadow`` as SOURCE, Courier inbound) support of the bootstrapper.

They cover, without a running CoreHub:

- the normalisation of the ``courier`` block of a SOURCE agent into the payloads CoreHub stores
  (``InboundEndpointCollection`` / ``InboundEndpoint``), including the shorthand defaults;
- the validation that mirrors ``InboundEndpointValidator`` for pipeline endpoints;
- the exact REST calls ``provision_inbound`` issues: tokens first (previous ones revoked), their ids on
  the collection allowlist, collections and endpoints created (POST) or updated in place (PUT);
- the token file (plaintext written there only, owner-only permissions) and the 409 of a mapped endpoint;
- the ``inboundCollection`` defaulting and the SOURCE/TARGET dispatch of ``provision_courier_agent``.
"""

import copy
import json
import logging
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import courier_inbound_support  # noqa: E402
from courier_support import CourierConfigError, stable_id  # noqa: E402
from courier_inbound_support import (  # noqa: E402
    API_TOKENS_PATH,
    ENDPOINT_INBOUND_PERMISSION,
    INBOUND_COLLECTIONS_PATH,
    INBOUND_ENDPOINTS_PATH,
    apply_inbound_defaults,
    is_http_source_agent,
    plan_inbound_definitions,
    provision_courier_agent,
    provision_inbound,
)

PIPELINE = "pipeline-1"
AGENT = "agent-http-source"


def inbound_config(token_file=None):
    config = {
        "collections": [
            {
                "name": "shop",
                "basePath": "/shop/",
                "allowlist": True,
                "endpoints": [
                    {
                        "name": "orders",
                        "method": "post",
                        "path": "/orders/{id}",
                        "operationBinding": "op",
                        "bindings": [
                            {"name": "id", "location": "PATH", "type": "number"},
                            {"name": "op", "location": "QUERY", "required": False, "default": "Insert"},
                            {"name": "status", "location": "BODY"},
                            {"name": "amount", "location": "BODY", "type": "NUMBER", "required": False},
                            {"name": "customer.city", "location": "BODY", "required": False},
                        ],
                    },
                    {
                        "name": "events",
                        "path": "/events",
                        "bodyType": "FORM",
                        "bindings": [
                            {"name": "kind", "location": "BODY"},
                            {"name": "X-Source", "location": "HEADER", "required": False},
                        ],
                    },
                    {
                        "name": "orders-delete",
                        "httpVerb": "DELETE",
                        "path": "/orders/{id}",
                        "bindings": [{"name": "id", "location": "PATH", "type": "NUMBER"}],
                    },
                ],
            }
        ],
    }
    if token_file:
        config["tokens"] = [{"name": "shop-client", "expiresInDays": 7, "outputFile": token_file}]
    return config


class FakeCoreHub:
    """Records every call of ``provision_inbound`` and answers the GETs and the token creation."""

    def __init__(self, collections=None, endpoints_by_collection=None, tokens=None, refuse_put_with=None):
        self.collections = collections or []
        self.endpoints_by_collection = endpoints_by_collection or {}
        self.tokens = tokens or []
        self.refuse_put_with = refuse_put_with
        self.calls = []

    def __call__(self, path, method="GET", token=None, body=None, params=None, headers=None):
        self.calls.append((method, path, copy.deepcopy(body), copy.deepcopy(params)))
        if method == "GET" and path == INBOUND_COLLECTIONS_PATH:
            return copy.deepcopy(self.collections)
        if method == "GET" and path == INBOUND_ENDPOINTS_PATH:
            return copy.deepcopy(self.endpoints_by_collection.get((params or {}).get("collectionId"), []))
        if method == "GET" and path == API_TOKENS_PATH:
            return copy.deepcopy(self.tokens)
        if method == "POST" and path == API_TOKENS_PATH:
            return {"id": "token-new", "name": body["name"], "token": "gsp_plaintext", "preview": "gsp_pl"}
        if method == "PUT" and path.startswith(INBOUND_ENDPOINTS_PATH) and self.refuse_put_with:
            raise self.refuse_put_with
        return body

    def methods_and_paths(self):
        return [(method, path) for method, path, _, _ in self.calls]


class HttpSourceDetectionTests(unittest.TestCase):
    def test_only_the_source_side_of_the_agent_is_the_http_source(self):
        self.assertTrue(is_http_source_agent({"agentTag": "api-agent-shadow", "agentType": "source"}))
        self.assertTrue(is_http_source_agent({"agentTag": "my-http", "agentInternalName": "api-agent-shadow", "agentType": "SOURCE"}))
        self.assertFalse(is_http_source_agent({"agentTag": "api-agent-shadow", "agentType": "TARGET"}))
        self.assertFalse(is_http_source_agent({"agentTag": "postgresql-cdc", "agentType": "SOURCE"}))
        self.assertFalse(is_http_source_agent(None))


class InboundPlanTests(unittest.TestCase):
    def plan(self, config=None, **kwargs):
        return plan_inbound_definitions(config or inbound_config(), PIPELINE, AGENT, **kwargs)

    def test_normalises_collection_and_pipeline_endpoints(self):
        plan = self.plan()
        collection = plan["collections"][0]["collection"]
        self.assertEqual(collection, {
            "id": stable_id("inbound-collection", "shop"),
            "name": "shop",
            "description": None,
            "basePath": "shop",
            "tags": [],
            "allowlistEnabled": True,
            "allowedTokenIds": [],
        })
        orders, events, delete = [e["definition"] for e in plan["collections"][0]["endpoints"]]
        self.assertEqual(orders["httpVerb"], "POST")
        self.assertEqual(orders["operation"], "CREATE")
        self.assertEqual(orders["bodyType"], "JSON")
        self.assertEqual((orders["pipelineId"], orders["agentId"]), (PIPELINE, AGENT))
        self.assertEqual(orders["collectionId"], collection["id"])
        self.assertEqual(orders["operationBinding"], "op")
        self.assertEqual(orders["keyBindings"], [])
        self.assertEqual(orders["bindings"][0], {
            "name": "id", "type": "NUMBER", "location": "PATH", "defaultValue": None, "required": True, "description": None,
        })
        self.assertEqual(orders["bindings"][1]["defaultValue"], "Insert")
        self.assertEqual(events["httpVerb"], "POST")
        self.assertEqual(events["bodyType"], "FORM_URLENCODED")
        self.assertEqual(delete["operation"], "DELETE")
        self.assertEqual(delete["bodyType"], "NONE")
        self.assertFalse(plan["force"])
        self.assertEqual(plan["tokens"], [])

    def test_base_path_defaults_to_the_name_and_ids_are_stable(self):
        config = inbound_config()
        del config["collections"][0]["basePath"]
        first = self.plan(config)
        second = self.plan(config)
        self.assertEqual(first["collections"][0]["collection"]["basePath"], "shop")
        self.assertEqual(first, second)

    def test_stored_definitions_keep_their_ids(self):
        plan = self.plan(
            existing_collections=[{"id": "stored-shop", "name": "shop"}],
            existing_endpoints_for=lambda cid: [{"id": "stored-orders", "name": "orders"}] if cid == "stored-shop" else [],
        )
        item = plan["collections"][0]
        self.assertTrue(item["exists"])
        self.assertEqual(item["collection"]["id"], "stored-shop")
        self.assertEqual(item["endpoints"][0]["definition"]["id"], "stored-orders")
        self.assertTrue(item["endpoints"][0]["exists"])
        self.assertFalse(item["endpoints"][1]["exists"])
        self.assertEqual(item["endpoints"][1]["definition"]["collectionId"], "stored-shop")

    def assertRefused(self, endpoint, *fragments):
        config = {"collections": [{"name": "c", "endpoints": [endpoint]}]}
        with self.assertRaises(CourierConfigError) as raised:
            self.plan(config)
        for fragment in fragments:
            self.assertIn(fragment, str(raised.exception))

    def test_mirrors_the_pipeline_endpoint_rules_of_corehub(self):
        self.assertRefused(
            {"name": "e", "httpVerb": "PATCH", "path": "/x/{id}", "bindings": [{"name": "id", "location": "PATH"}]},
            "PATCH is not available",
        )
        self.assertRefused({"name": "e", "httpVerb": "GET", "path": "/", "bindings": [{"name": "a", "location": "QUERY"}]}, "httpVerb 'GET'")
        self.assertRefused({"name": "e", "httpVerb": "POST", "operation": "UPDATE", "path": "/", "bindings": ["a"]}, "does not fit")
        self.assertRefused({"name": "e", "path": "/{id}", "bindings": ["a"]}, "'{id}' needs a PATH binding")
        self.assertRefused({"name": "e", "path": "/", "bindings": [{"name": "id", "location": "PATH"}]}, "has no '{id}' placeholder")
        self.assertRefused({"name": "e", "path": "/x{id}", "bindings": ["a"]}, "whole path segment")
        self.assertRefused({"name": "e", "path": "/", "bindings": ["a", "A"]}, "declared more than once")
        self.assertRefused({"name": "e", "path": "/", "bindings": ["_seq"]}, "reserved for the request sequence")
        self.assertRefused({"name": "e", "path": "/", "bindings": [{"name": "Authorization", "location": "HEADER"}]}, "credentials")
        self.assertRefused({"name": "e", "path": "/", "bindings": [{"name": "bad header", "location": "HEADER"}]}, "not valid")
        self.assertRefused({"name": "e", "path": "/{id}", "bindings": [{"name": "id", "location": "PATH", "required": False}]}, "must be required")
        self.assertRefused({"name": "e", "httpVerb": "DELETE", "path": "/", "bindings": [{"name": "a", "location": "QUERY"}]}, "needs a key")
        self.assertRefused({"name": "e", "path": "/", "operationBinding": "op", "bindings": ["op"]}, "needs a key")
        self.assertRefused({"name": "e", "path": "/{id}", "operationBinding": "op", "bindings": [
            {"name": "id", "location": "PATH"}, {"name": "op", "type": "NUMBER"}]}, "must be a STRING binding")
        self.assertRefused({"name": "e", "path": "/", "operationBinding": "nope", "bindings": ["a"]}, "'nope' is not declared")
        self.assertRefused({"name": "e", "path": "/", "keyBindings": ["k"], "bindings": [{"name": "k", "required": False}]}, "must be required")
        self.assertRefused({"name": "e", "path": "/", "keyBindings": ["k"], "bindings": ["a"]}, "'k' is not declared")
        self.assertRefused({"name": "e", "httpVerb": "DELETE", "path": "/{id}", "bindings": [
            {"name": "id", "location": "PATH"}, {"name": "why", "location": "BODY"}]}, "needs a verb with a body")
        self.assertRefused({"name": "e", "path": "/", "bodyType": "NONE", "bindings": ["a"]}, "BODY bindings need a body type")
        self.assertRefused({"name": "e", "path": "/", "bodyType": "XML", "bindings": ["a"]}, "bodyType 'XML'")
        self.assertRefused({"name": "e", "path": "/", "bindings": [{"name": "n", "type": "NUMBER", "default": "abc"}]}, "is not a number")
        self.assertRefused({"name": "e", "path": "/", "bindings": [{"name": "b", "type": "BOOLEAN", "default": "yes"}]}, "is not true or false")
        self.assertRefused({"name": "e", "path": "/"}, "at least one binding")
        self.assertRefused({"name": "e", "path": "orders", "bindings": ["a"]}, "must start with '/'")

    def test_collection_rules(self):
        with self.assertRaises(CourierConfigError) as raised:
            self.plan({"collections": [
                {"name": "a", "basePath": "same", "endpoints": [{"name": "e", "path": "/", "bindings": ["x"]}]},
                {"name": "b", "basePath": "same", "endpoints": [{"name": "e", "path": "/", "bindings": ["x"]}]},
                {"name": "c", "basePath": "bad path", "endpoints": []},
            ]})
        message = str(raised.exception)
        self.assertIn("basePath 'same' is used by two collections", message)
        self.assertIn("basePath 'bad path' is not valid", message)
        self.assertIn("collection 'c' declares no endpoint", message)
        with self.assertRaises(CourierConfigError):
            plan_inbound_definitions({"collections": []}, PIPELINE, AGENT)
        with self.assertRaises(CourierConfigError):
            plan_inbound_definitions(inbound_config(), "", AGENT)

    def test_token_rules(self):
        config = inbound_config()
        config["tokens"] = [{"name": "", "collections": ["nope"], "expiresInDays": 0}]
        with self.assertRaises(CourierConfigError) as raised:
            self.plan(config)
        message = str(raised.exception)
        self.assertIn("name is required", message)
        self.assertIn("outputFile is required", message)
        self.assertIn("expiresInDays must be positive", message)
        self.assertIn("unknown collection 'nope'", message)

    def test_token_joins_every_collection_by_default(self):
        plan = self.plan(inbound_config(token_file="/tmp/x.json"))
        self.assertEqual(plan["tokens"], [{"name": "shop-client", "expiresInDays": 7, "collections": ["shop"], "outputFile": "/tmp/x.json"}])


class ProvisionInboundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token_file = os.path.join(self.tmp.name, "secrets", "http-source-token.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_run_creates_token_then_collection_with_allowlist_then_endpoints(self):
        hub = FakeCoreHub()

        names = provision_inbound("jwt", inbound_config(self.token_file), PIPELINE, AGENT, fetch=hub)

        self.assertEqual(names, ["shop"])
        self.assertEqual(hub.methods_and_paths(), [
            ("GET", INBOUND_COLLECTIONS_PATH),
            ("GET", API_TOKENS_PATH),
            ("POST", API_TOKENS_PATH),
            ("POST", INBOUND_COLLECTIONS_PATH),
            ("POST", INBOUND_ENDPOINTS_PATH),
            ("POST", INBOUND_ENDPOINTS_PATH),
            ("POST", INBOUND_ENDPOINTS_PATH),
        ])
        self.assertEqual(hub.calls[2][2], {
            "name": "shop-client",
            "expiresInDays": 7,
            "scopes": [{"pipelineId": PIPELINE, "agentId": AGENT}],
            "permissionScopes": [ENDPOINT_INBOUND_PERMISSION],
        })
        collection = hub.calls[3][2]
        self.assertTrue(collection["allowlistEnabled"])
        self.assertEqual(collection["allowedTokenIds"], ["token-new"])
        self.assertEqual([c[2]["name"] for c in hub.calls[4:]], ["orders", "events", "orders-delete"])

        with open(self.token_file, encoding="utf-8") as handle:
            written = json.load(handle)
        self.assertEqual(written, {
            "token": "gsp_plaintext", "tokenId": "token-new", "name": "shop-client", "pipelineId": PIPELINE, "agentId": AGENT,
        })
        self.assertEqual(stat.S_IMODE(os.stat(self.token_file).st_mode), 0o600)

    def test_plaintext_never_reaches_the_log(self):
        hub = FakeCoreHub()
        logger = logging.getLogger("inbound-test")
        with mock.patch.object(logger, "info") as info, mock.patch.object(logger, "warning") as warning:
            provision_inbound("jwt", inbound_config(self.token_file), PIPELINE, AGENT, fetch=hub, logger=logger)
        logged = " ".join(str(a) for call in info.call_args_list + warning.call_args_list for a in call.args)
        self.assertNotIn("gsp_plaintext", logged)
        self.assertIn(self.token_file, logged)

    def test_second_run_revokes_the_previous_token_and_updates_in_place(self):
        hub = FakeCoreHub(
            collections=[{"id": "stored-shop", "name": "shop", "basePath": "shop"}],
            endpoints_by_collection={"stored-shop": [{"id": "stored-orders", "name": "orders"}]},
            tokens=[
                {"id": "old-active", "name": "shop-client", "active": True},
                {"id": "old-revoked", "name": "shop-client", "active": False},
                {"id": "other", "name": "someone-else", "active": True},
            ],
        )

        provision_inbound("jwt", inbound_config(self.token_file), PIPELINE, AGENT, fetch=hub)

        self.assertEqual(hub.methods_and_paths(), [
            ("GET", INBOUND_COLLECTIONS_PATH),
            ("GET", INBOUND_ENDPOINTS_PATH),
            ("GET", API_TOKENS_PATH),
            ("POST", f"{API_TOKENS_PATH}/old-active/revoke"),
            ("POST", API_TOKENS_PATH),
            ("PUT", f"{INBOUND_COLLECTIONS_PATH}/stored-shop"),
            ("PUT", f"{INBOUND_ENDPOINTS_PATH}/stored-orders"),
            ("POST", INBOUND_ENDPOINTS_PATH),
            ("POST", INBOUND_ENDPOINTS_PATH),
        ])
        self.assertIsNone(hub.calls[6][3], "no force unless asked")
        self.assertEqual(hub.calls[5][2]["allowedTokenIds"], ["token-new"])

    def test_force_is_passed_on_update(self):
        config = inbound_config()
        config["force"] = True
        hub = FakeCoreHub(
            collections=[{"id": "stored-shop", "name": "shop"}],
            endpoints_by_collection={"stored-shop": [{"id": "stored-orders", "name": "orders"}]},
        )
        provision_inbound("jwt", config, PIPELINE, AGENT, fetch=hub)
        put = next(c for c in hub.calls if c[0] == "PUT" and c[1].startswith(INBOUND_ENDPOINTS_PATH))
        self.assertEqual(put[3], {"force": "true"})

    def test_a_mapped_endpoint_refused_with_409_explains_force(self):
        conflict = Exception("409 Conflict")
        conflict.response = mock.Mock(status_code=409)
        hub = FakeCoreHub(
            collections=[{"id": "stored-shop", "name": "shop"}],
            endpoints_by_collection={"stored-shop": [{"id": "stored-orders", "name": "orders"}]},
            refuse_put_with=conflict,
        )
        with self.assertRaises(CourierConfigError) as raised:
            provision_inbound("jwt", inbound_config(), PIPELINE, AGENT, fetch=hub)
        self.assertIn('"force": true', str(raised.exception))

    def test_other_errors_propagate_unchanged(self):
        failure = Exception("500")
        failure.response = mock.Mock(status_code=500)
        hub = FakeCoreHub(
            collections=[{"id": "stored-shop", "name": "shop"}],
            endpoints_by_collection={"stored-shop": [{"id": "stored-orders", "name": "orders"}]},
            refuse_put_with=failure,
        )
        with self.assertRaises(Exception) as raised:
            provision_inbound("jwt", inbound_config(), PIPELINE, AGENT, fetch=hub)
        self.assertIs(raised.exception, failure)

    def test_without_tokens_no_token_call_is_made(self):
        hub = FakeCoreHub()
        provision_inbound("jwt", inbound_config(), PIPELINE, AGENT, fetch=hub)
        self.assertFalse(any(path.startswith(API_TOKENS_PATH) for _, path in hub.methods_and_paths()))

    def test_token_creation_without_plaintext_fails(self):
        hub = FakeCoreHub()
        original = hub.__call__

        def no_plaintext(path, method="GET", **kwargs):
            result = original(path, method=method, **kwargs)
            if method == "POST" and path == API_TOKENS_PATH:
                return {"id": "token-new"}
            return result

        with self.assertRaises(RuntimeError):
            provision_inbound("jwt", inbound_config(self.token_file), PIPELINE, AGENT, fetch=no_plaintext)

    def test_invalid_configuration_sends_nothing_but_the_read(self):
        hub = FakeCoreHub()
        with self.assertRaises(CourierConfigError):
            provision_inbound("jwt", {"collections": [{"name": "c", "endpoints": [{"name": "e", "path": "/"}]}]},
                              PIPELINE, AGENT, fetch=hub)
        self.assertEqual(hub.methods_and_paths(), [("GET", INBOUND_COLLECTIONS_PATH)])


class InboundCollectionDefaultTests(unittest.TestCase):
    def test_single_collection_fills_inbound_collection(self):
        agent = {"agentTag": "api-agent-shadow"}
        apply_inbound_defaults(agent, ["shop"])
        self.assertEqual(agent["customHostCredentials"], {"inboundCollection": "shop"})

    def test_explicit_value_or_several_collections_leave_it_alone(self):
        agent = {"customHostCredentials": {"inboundCollection": "other", "inboundPort": 8091}}
        apply_inbound_defaults(agent, ["shop"])
        self.assertEqual(agent["customHostCredentials"], {"inboundCollection": "other", "inboundPort": 8091})
        agent = {"customHostCredentials": {}}
        apply_inbound_defaults(agent, ["a", "b"])
        self.assertEqual(agent["customHostCredentials"], {})


class ProvisionCourierAgentDispatchTests(unittest.TestCase):
    """``provision_courier_agent`` (called by ``main._prepare_courier_agent``) routes a SOURCE to the
    inbound provisioning and a TARGET to the outbound one."""

    def test_source_goes_to_inbound(self):
        agent = {"agentId": AGENT, "agentTag": "api-agent-shadow", "agentType": "SOURCE",
                 "customHostCredentials": {}, "courier": inbound_config()}
        with mock.patch.object(courier_inbound_support, "provision_inbound", return_value=["shop"]) as inbound, \
                mock.patch.object(courier_inbound_support, "provision_courier") as outbound:
            result = provision_courier_agent("jwt", PIPELINE, agent)
        inbound.assert_called_once()
        self.assertEqual(inbound.call_args.args[2:4], (PIPELINE, AGENT))
        outbound.assert_not_called()
        self.assertEqual(result, {"inboundCollection": "shop"})

    def test_target_goes_to_outbound(self):
        agent = {"agentId": "t", "agentTag": "api-agent-shadow", "agentType": "TARGET",
                 "customHostCredentials": {}, "courier": {"collections": []}}
        with mock.patch.object(courier_inbound_support, "provision_inbound") as inbound, \
                mock.patch.object(courier_inbound_support, "provision_courier", return_value=["demo-api"]) as outbound:
            result = provision_courier_agent("jwt", PIPELINE, agent)
        outbound.assert_called_once()
        inbound.assert_not_called()
        self.assertEqual(result, {"outboundCollection": "demo-api"})

    def test_other_agents_and_missing_block_need_nothing(self):
        self.assertIsNone(provision_courier_agent("jwt", PIPELINE, {"agentTag": "kafka", "agentType": "SOURCE"}))
        agent = {"agentId": AGENT, "agentTag": "api-agent-shadow", "agentType": "SOURCE", "courier": None}
        self.assertIsNone(provision_courier_agent("jwt", PIPELINE, agent))

    def test_invalid_block_raises_before_any_write(self):
        hub = FakeCoreHub()
        agent = {"agentId": AGENT, "agentTag": "api-agent-shadow", "agentType": "SOURCE",
                 "customHostCredentials": {}, "courier": {"collections": []}}
        with self.assertRaises(CourierConfigError):
            provision_courier_agent("jwt", PIPELINE, agent, fetch=hub)
        self.assertEqual(hub.methods_and_paths(), [("GET", INBOUND_COLLECTIONS_PATH)])


if __name__ == "__main__":
    unittest.main()
