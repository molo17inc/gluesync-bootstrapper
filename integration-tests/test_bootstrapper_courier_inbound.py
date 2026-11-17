#!/usr/bin/env python3
"""
Unit tests for the HTTP Source (``courier-agent`` as SOURCE, Courier inbound) support of the bootstrapper.

They cover, without a running CoreHub:

- the normalisation of the ``courier`` block of a SOURCE agent into the payloads CoreHub stores
  (``InboundEndpointCollection`` / ``InboundEndpoint``), including the shorthand defaults;
- the validation that mirrors ``InboundEndpointValidator`` for the free routes of Core Hub 2.3.2.2 (writes, PATCH,
  reads with response fields, EXECUTE routes with their options);
- the exact REST calls ``provision_inbound`` issues: tokens first (previous ones revoked), their ids on
  the collection allowlist, collections and endpoints created (POST) or updated in place (PUT);
- the token file (plaintext written there only, owner-only permissions) and the 409 of a mapped endpoint;
- the ``inboundCollection`` defaulting (the one collection the HTTP Source serves), the listener settings of 2.3.2.1
  dropped, and the SOURCE/TARGET dispatch of ``provision_courier_agent``.
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
    HTTPS_AGENT_PERMISSION,
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



def corehub_error(status, body=""):
    """What ``fetch_core_hub`` raises: a RuntimeError caused by the HTTP error that carries the response."""
    http_error = Exception(f"{status} Client Error")
    http_error.response = mock.Mock(status_code=status, text=body)
    try:
        raise RuntimeError(f"CoreHub request failed: {http_error} | Response: {body}") from http_error
    except RuntimeError as wrapped:
        return wrapped


class CoreHubFailure(Exception):
    """A CoreHub call that fails with an HTTP status the provisioning does not handle."""

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
        self.assertTrue(is_http_source_agent({"agentTag": "courier-agent", "agentType": "source"}))
        self.assertTrue(is_http_source_agent({"agentTag": "my-http", "agentInternalName": "courier-agent", "agentType": "SOURCE"}))
        self.assertFalse(is_http_source_agent({"agentTag": "courier-agent", "agentType": "TARGET"}))
        self.assertFalse(is_http_source_agent({"agentTag": "postgresql-cdc", "agentType": "SOURCE"}))
        self.assertFalse(is_http_source_agent(None))


class InboundPlanTests(unittest.TestCase):
    def plan(self, config=None, **kwargs):
        return plan_inbound_definitions(config or inbound_config(), **kwargs)

    def test_normalises_collection_and_free_routes(self):
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
            "blockedTokenIds": [],
        })
        orders, events, delete = [e["definition"] for e in plan["collections"][0]["endpoints"]]
        self.assertEqual(orders["httpVerb"], "POST")
        self.assertEqual(orders["operation"], "CREATE")
        self.assertEqual(orders["bodyType"], "JSON")
        self.assertNotIn("pipelineId", orders, "a route names no pipeline")
        self.assertNotIn("agentId", orders)
        self.assertEqual(orders["responseFields"], [])
        self.assertNotIn("routine", orders)
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
        self.assertEqual(plan["attached"], "shop", "the only collection is the one the HTTP Source serves")

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

    def test_mirrors_the_write_rules_of_corehub(self):
        self.assertRefused({"name": "e", "httpVerb": "PATCH", "path": "/x/{id}", "operationBinding": "op",
                            "bindings": [{"name": "id", "location": "PATH"}, "op"]}, "takes no operation binding")
        self.assertRefused({"name": "e", "httpVerb": "HEAD", "path": "/", "operation": "CREATE", "bindings": ["a"]}, "does not fit")
        self.assertRefused({"name": "e", "path": "/", "bindings": ["_response"]}, "reserved for the answer of a routine")
        self.assertRefused({"name": "e", "path": "/", "bindings": ["a"], "responseFields": ["a"]}, "response fields are a read's")
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
        self.assertIn("an HTTP Source serves one collection", message)
        with self.assertRaises(CourierConfigError):
            plan_inbound_definitions({"collections": []})


    def test_a_base_path_is_one_segment_and_a_token_is_not_both_allowed_and_blocked(self):
        config = {"collections": [
            {"name": "a", "basePath": "shop/v2", "endpoints": [{"name": "e", "path": "/", "bindings": ["x"]}]},
            {"name": "b", "allowedTokenIds": ["t1", "t2"], "blockedTokenIds": ["t2"],
             "endpoints": [{"name": "e", "path": "/", "bindings": ["x"]}]},
        ]}
        with self.assertRaises(CourierConfigError) as raised:
            plan_inbound_definitions(config, inbound_collection="a")
        message = str(raised.exception)
        self.assertIn("basePath 'shop/v2' is not valid: one segment", message)
        self.assertIn("a token cannot be both allowed and blocked: t2", message)

    def test_the_blocklist_is_sent_with_the_collection(self):
        config = inbound_config()
        config["collections"][0]["blockedTokenIds"] = ["leaked"]
        collection = plan_inbound_definitions(config)["collections"][0]["collection"]
        self.assertEqual(collection["blockedTokenIds"], ["leaked"])
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

        plan = provision_inbound("jwt", inbound_config(self.token_file), PIPELINE, AGENT, fetch=hub)

        self.assertEqual(plan["attached"], "shop")
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
            "permissionScopes": [HTTPS_AGENT_PERMISSION],
            "collectionScopes": [hub.calls[3][2]["id"]],
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

    def test_a_token_is_scoped_to_the_collections_it_is_for_only(self):
        config = inbound_config(self.token_file)
        billing = copy.deepcopy(config["collections"][0])
        billing["name"] = "billing"
        billing["basePath"] = "billing"
        config["collections"].append(billing)
        config["tokens"][0]["collections"] = ["billing"]
        hub = FakeCoreHub()

        provision_inbound("jwt", config, PIPELINE, AGENT, fetch=hub, inbound_collection="billing")

        created = next(body for method, path, body, _ in hub.calls if method == "POST" and path == API_TOKENS_PATH)
        saved = {body["name"]: body for method, path, body, _ in hub.calls if method == "POST" and path == INBOUND_COLLECTIONS_PATH}
        self.assertEqual(created["collectionScopes"], [saved["billing"]["id"]])
        self.assertEqual(saved["billing"]["allowedTokenIds"], ["token-new"])
        self.assertNotIn("token-new", saved["shop"]["allowedTokenIds"])

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
        config = inbound_config()
        with self.assertRaises(CourierConfigError) as raised:
            provision_inbound("jwt", config, PIPELINE, AGENT, fetch=hub)
        self.assertIn('"force": true', str(raised.exception))

    def test_a_409_wrapped_by_fetch_core_hub_also_explains_force(self):
        hub = FakeCoreHub(
            collections=[{"id": "stored-shop", "name": "shop"}],
            endpoints_by_collection={"stored-shop": [{"id": "stored-orders", "name": "orders"}]},
            refuse_put_with=corehub_error(409, '{"message":"Entity orders reads this endpoint"}'),
        )
        config = inbound_config()
        with self.assertRaises(CourierConfigError) as raised:
            provision_inbound("jwt", config, PIPELINE, AGENT, fetch=hub)
        self.assertIn('"force": true', str(raised.exception))

    def test_a_create_of_a_stored_id_is_repeated_as_an_update(self):
        class Conflicting(FakeCoreHub):
            def __call__(self, path, method="GET", **kwargs):
                result = super().__call__(path, method=method, **kwargs)
                if method == "POST" and path in (INBOUND_COLLECTIONS_PATH, INBOUND_ENDPOINTS_PATH):
                    raise corehub_error(409, '{"message":"already exists: update it with PUT"}')
                return result

        hub = Conflicting()
        provision_inbound("jwt", inbound_config(), PIPELINE, AGENT, fetch=hub)

        collection_id = stable_id("inbound-collection", "shop")
        orders_id = stable_id("inbound-endpoint", "orders", collection_id)
        self.assertEqual(hub.methods_and_paths()[1:4], [
            ("POST", INBOUND_COLLECTIONS_PATH),
            ("PUT", f"{INBOUND_COLLECTIONS_PATH}/{collection_id}"),
            ("POST", INBOUND_ENDPOINTS_PATH),
        ])
        self.assertEqual(hub.methods_and_paths()[4], ("PUT", f"{INBOUND_ENDPOINTS_PATH}/{orders_id}"))

    def test_a_collection_no_token_can_call_says_to_configure_one(self):
        class NoToken(FakeCoreHub):
            def __call__(self, path, method="GET", **kwargs):
                if method == "POST" and path == INBOUND_COLLECTIONS_PATH:
                    raise corehub_error(400, '{"message":"This collection needs an active Personal API Token","code":"MISSING_TOKEN"}')
                return super().__call__(path, method=method, **kwargs)

        config, hub = inbound_config(), NoToken()
        with self.assertRaises(CourierConfigError) as raised:
            provision_inbound("jwt", config, PIPELINE, AGENT, fetch=hub)
        self.assertIn("courier.tokens", str(raised.exception))
        self.assertIn(HTTPS_AGENT_PERMISSION, str(raised.exception))

    def test_other_errors_propagate_unchanged(self):
        failure = CoreHubFailure("500")
        failure.response = mock.Mock(status_code=500)
        hub = FakeCoreHub(
            collections=[{"id": "stored-shop", "name": "shop"}],
            endpoints_by_collection={"stored-shop": [{"id": "stored-orders", "name": "orders"}]},
            refuse_put_with=failure,
        )
        config = inbound_config()
        with self.assertRaises(CoreHubFailure) as raised:
            provision_inbound("jwt", config, PIPELINE, AGENT, fetch=hub)
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

        config = inbound_config(self.token_file)
        with self.assertRaises(RuntimeError):
            provision_inbound("jwt", config, PIPELINE, AGENT, fetch=no_plaintext)

    def test_invalid_configuration_sends_nothing_but_the_read(self):
        hub = FakeCoreHub()
        with self.assertRaises(CourierConfigError):
            provision_inbound("jwt", {"collections": [{"name": "c", "endpoints": [{"name": "e", "path": "/"}]}]},
                              PIPELINE, AGENT, fetch=hub)
        self.assertEqual(hub.methods_and_paths(), [("GET", INBOUND_COLLECTIONS_PATH)])


class InboundCollectionDefaultTests(unittest.TestCase):
    def test_the_served_collection_fills_inbound_collection(self):
        agent = {"agentTag": "courier-agent"}
        apply_inbound_defaults(agent, "shop")
        self.assertEqual(agent["customHostCredentials"], {"inboundCollection": "shop"})

    def test_an_explicit_value_stays_and_the_listener_settings_of_2_3_2_1_go(self):
        agent = {"customHostCredentials": {"inboundCollection": "other", "inboundPort": 8091,
                                           "inboundAllowPlainHttp": True, "maxBodyKb": 4}}
        apply_inbound_defaults(agent, "shop")
        self.assertEqual(agent["customHostCredentials"], {"inboundCollection": "other", "maxBodyKb": 4})
        agent = {"customHostCredentials": {}}
        apply_inbound_defaults(agent, None)
        self.assertEqual(agent["customHostCredentials"], {})


class ProvisionCourierAgentDispatchTests(unittest.TestCase):
    """``provision_courier_agent`` (called by ``main._prepare_courier_agent``) routes a SOURCE to the
    inbound provisioning and a TARGET to the outbound one."""

    def test_source_goes_to_inbound(self):
        agent = {"agentId": AGENT, "agentTag": "courier-agent", "agentType": "SOURCE",
                 "customHostCredentials": {}, "courier": inbound_config()}
        with mock.patch.object(courier_inbound_support, "provision_inbound", return_value={"attached": "shop"}) as inbound, \
                mock.patch.object(courier_inbound_support, "provision_courier") as outbound:
            result = provision_courier_agent("jwt", PIPELINE, agent)
        inbound.assert_called_once()
        self.assertEqual(inbound.call_args.args[2:4], (PIPELINE, AGENT))
        outbound.assert_not_called()
        self.assertEqual(result, {"inboundCollection": "shop"})

    def test_target_goes_to_outbound(self):
        agent = {"agentId": "t", "agentTag": "courier-agent", "agentType": "TARGET",
                 "customHostCredentials": {}, "courier": {"collections": []}}
        with mock.patch.object(courier_inbound_support, "provision_inbound") as inbound, \
                mock.patch.object(courier_inbound_support, "provision_courier", return_value=["demo-api"]) as outbound:
            result = provision_courier_agent("jwt", PIPELINE, agent)
        outbound.assert_called_once()
        inbound.assert_not_called()
        self.assertEqual(result, {"outboundCollection": "demo-api"})

    def test_other_agents_and_missing_block_need_nothing(self):
        self.assertIsNone(provision_courier_agent("jwt", PIPELINE, {"agentTag": "kafka", "agentType": "SOURCE"}))
        agent = {"agentId": AGENT, "agentTag": "courier-agent", "agentType": "SOURCE", "courier": None}
        self.assertIsNone(provision_courier_agent("jwt", PIPELINE, agent))

    def test_invalid_block_raises_before_any_write(self):
        hub = FakeCoreHub()
        agent = {"agentId": AGENT, "agentTag": "courier-agent", "agentType": "SOURCE",
                 "customHostCredentials": {}, "courier": {"collections": []}}
        with self.assertRaises(CourierConfigError):
            provision_courier_agent("jwt", PIPELINE, agent, fetch=hub)
        self.assertEqual(hub.methods_and_paths(), [("GET", INBOUND_COLLECTIONS_PATH)])


def routes_config(*endpoints, collections=None):
    return {"collections": collections or [{"name": "shop", "endpoints": list(endpoints)}]}


class FreeRouteTests(unittest.TestCase):
    """The routes of Core Hub 2.3.2.2 besides the writes: PATCH, reads and EXECUTE."""

    def routes(self, *endpoints, **kwargs):
        plan = plan_inbound_definitions(routes_config(*endpoints), **kwargs)
        return [e["definition"] for e in plan["collections"][0]["endpoints"]]

    def assertRefused(self, endpoint, *fragments):
        with self.assertRaises(CourierConfigError) as raised:
            self.routes(endpoint)
        for fragment in fragments:
            self.assertIn(fragment, str(raised.exception))

    def test_a_patch_is_an_update_and_a_get_a_read_or_a_list_by_its_path(self):
        patch, read, listing = self.routes(
            {"name": "patch", "httpVerb": "PATCH", "path": "/orders/{id}",
             "bindings": [{"name": "id", "location": "PATH", "type": "NUMBER"}, {"name": "status", "required": False}]},
            {"name": "read", "httpVerb": "GET", "path": "/orders/{id}",
             "bindings": [{"name": "id", "location": "PATH", "type": "NUMBER"}],
             "responseFields": [{"name": "id", "type": "number"}, "status"]},
            {"name": "list", "httpVerb": "GET", "path": "/orders", "maxRows": 50, "keyBindings": ["id"],
             "bindings": [{"name": "status", "location": "QUERY", "required": False}],
             "responseFields": [{"name": "id", "type": "NUMBER"}, "status"]},
        )
        self.assertEqual((patch["operation"], patch["bodyType"]), ("UPDATE", "JSON"))
        self.assertEqual((read["operation"], read["bodyType"]), ("READ", "NONE"))
        self.assertEqual(read["responseFields"], [
            {"name": "id", "type": "NUMBER", "description": None},
            {"name": "status", "type": "STRING", "description": None},
        ])
        self.assertEqual((listing["operation"], listing["maxRows"]), ("LIST", 50))

    def test_mirrors_the_read_rules_of_corehub(self):
        self.assertRefused({"name": "r", "httpVerb": "GET", "path": "/orders/{id}",
                            "bindings": [{"name": "id", "location": "PATH"}]}, "at least one response field")
        self.assertRefused({"name": "r", "httpVerb": "GET", "path": "/orders", "responseFields": ["id"]}, "needs a key")
        self.assertRefused({"name": "r", "httpVerb": "GET", "path": "/orders/{id}", "responseFields": ["id"],
                            "bindings": [{"name": "id", "location": "PATH"}, {"name": "q", "location": "QUERY"}]},
                           "only a LIST filters on")
        self.assertRefused({"name": "r", "httpVerb": "GET", "path": "/orders", "keyBindings": ["id"], "responseFields": ["id"],
                            "bindings": [{"name": "b", "location": "BODY"}]}, "reads PATH and QUERY bindings only")
        self.assertRefused({"name": "r", "httpVerb": "GET", "operation": "READ", "path": "/orders", "keyBindings": ["id"],
                            "responseFields": ["id"]}, "READ needs a path placeholder")
        self.assertRefused({"name": "r", "httpVerb": "GET", "operation": "LIST", "path": "/orders/{id}",
                            "bindings": [{"name": "id", "location": "PATH"}], "responseFields": ["id"]}, "LIST takes no path placeholder")
        self.assertRefused({"name": "r", "httpVerb": "GET", "path": "/orders/{id}", "responseFields": [{"name": "id", "type": "NUMBER"}],
                            "bindings": [{"name": "id", "location": "PATH"}]}, "one column has one type")
        self.assertRefused({"name": "r", "httpVerb": "GET", "path": "/orders", "keyBindings": ["nope"], "responseFields": ["id"]},
                           "neither a filter nor a response field")
        self.assertRefused({"name": "r", "httpVerb": "GET", "path": "/o/{id}", "maxRows": 0, "responseFields": ["id"],
                            "bindings": [{"name": "id", "location": "PATH"}]}, "maxRows must be between")

    def test_an_execute_route_carries_its_options_and_return_type(self):
        (route,) = self.routes({"name": "place", "path": "/place-order", "bindings": ["customer"],
                                "routine": {"timeoutSeconds": 10, "answer": {"body": "rows", "format": "xml"}}})
        self.assertEqual(route["operation"], "EXECUTE")
        self.assertEqual(route["routine"], {"readOnly": False, "timeoutSeconds": 10, "ownTransaction": False,
                                            "answer": {"body": "ROWS", "format": "XML"}})
        (called,) = self.routes({"name": "total", "httpVerb": "GET", "path": "/total/{customer}",
                                 "bindings": [{"name": "customer", "location": "PATH"}],
                                 "routine": {"readOnly": True}, "call": {"schema": "shop", "name": "customer_total"}})
        self.assertEqual(called["routine"]["answer"], {"body": "ALL", "format": "JSON"}, "the whole answer in JSON by default")
        self.assertEqual((called["operation"], called["bodyType"]), ("EXECUTE", "NONE"))

    def test_mirrors_the_execute_rules_of_corehub(self):
        self.assertRefused({"name": "x", "httpVerb": "GET", "path": "/x", "routine": {}}, "does not fit operation EXECUTE")
        self.assertRefused({"name": "x", "path": "/x", "bodyType": "FORM", "bindings": ["a"], "routine": {}}, "form body")
        self.assertRefused({"name": "x", "path": "/x", "bindings": ["a.b"], "routine": {}}, "nested value")
        self.assertRefused({"name": "x", "path": "/x", "routine": {"readOnly": True, "ownTransaction": True}}, "cannot also commit")
        self.assertRefused({"name": "x", "path": "/x", "routine": {"timeoutSeconds": 3601}}, "timeoutSeconds must be between")
        self.assertRefused({"name": "x", "path": "/x", "routine": {"answer": {"body": "FIRST"}}}, "answer.body 'FIRST'")
        self.assertRefused({"name": "x", "path": "/x", "keyBindings": ["a"], "bindings": ["a"], "routine": {}}, "no key or operation binding")
        self.assertRefused({"name": "x", "path": "/x", "operation": "CREATE", "bindings": ["a"], "routine": {}},
                           "only an EXECUTE endpoint calls a routine")

    def test_two_routes_on_one_verb_and_path_conflict(self):
        with self.assertRaises(CourierConfigError) as raised:
            self.routes({"name": "a", "path": "/o/{id}", "bindings": [{"name": "id", "location": "PATH"}]},
                        {"name": "b", "path": "/o/{key}", "bindings": [{"name": "key", "location": "PATH"}]})
        self.assertIn("conflicts with endpoint 'a'", str(raised.exception))

    def test_several_collections_need_the_served_one_named(self):
        collections = [{"name": "shop", "endpoints": [{"name": "e", "path": "/", "bindings": ["a"]}]},
                       {"name": "billing", "basePath": "bill", "endpoints": [{"name": "e", "path": "/", "bindings": ["a"]}]}]
        with self.assertRaises(CourierConfigError) as raised:
            plan_inbound_definitions(routes_config(collections=collections))
        self.assertIn("customHostCredentials.inboundCollection", str(raised.exception))
        self.assertEqual(plan_inbound_definitions(routes_config(collections=collections), inbound_collection="/bill/")["attached"],
                         "billing", "named by its base path, as the agent resolves it")
        self.assertIsNone(plan_inbound_definitions(routes_config(collections=collections), inbound_collection="elsewhere")["attached"])

    def test_a_call_belongs_to_the_served_collection(self):
        collections = [{"name": "shop", "endpoints": [{"name": "e", "path": "/", "bindings": ["a"]}]},
                       {"name": "other", "endpoints": [{"name": "x", "path": "/x", "call": {"schema": "s", "name": "p"}}]}]
        with self.assertRaises(CourierConfigError) as raised:
            plan_inbound_definitions(routes_config(collections=collections), inbound_collection="shop")
        self.assertIn("routes x cannot have an entity of this pipeline", str(raised.exception))

    def test_routine_calls_lists_the_routes_whose_entity_the_bootstrapper_builds(self):
        call = {"schema": "shop", "name": "place_order"}
        agent = {"agentTag": "courier-agent", "agentType": "SOURCE", "courier": routes_config(
            {"name": "w", "path": "/", "bindings": ["a"]},
            {"name": "x", "path": "/x", "call": call},
        )}
        self.assertEqual(courier_inbound_support.routine_calls(agent),
                         [{"collection": "shop", "route": agent["courier"]["collections"][0]["endpoints"][1], "call": call}])
        self.assertEqual(courier_inbound_support.routine_calls({**agent, "agentType": "TARGET"}), [])


class AttachedElsewhereTests(unittest.TestCase):
    """A collection is served by one HTTP Source: Core Hub refuses an HTTP Source whose collection another one holds,
    and reports a collection's owner only while that pipeline exists."""

    def test_a_collection_another_pipeline_holds_is_refused_before_anything_is_written(self):
        for restore in (False, True):
            hub = FakeCoreHub(collections=[{"id": "stored-shop", "name": "shop",
                                            "owner": {"pipelineId": "original", "agentId": "http-original"}}])
            with self.subTest(restore=restore), self.assertRaises(CourierConfigError) as raised:
                provision_inbound("jwt", inbound_config("/tmp/never-written.json"), PIPELINE, AGENT, fetch=hub,
                                  keep_other_pipelines=restore)
            self.assertIn("served by HTTP Source http-original of pipeline original", str(raised.exception))
            self.assertEqual(hub.methods_and_paths(), [("GET", INBOUND_COLLECTIONS_PATH), ("GET", INBOUND_ENDPOINTS_PATH)],
                             "no token revoked or created, nothing saved")

    def test_a_collection_this_pipeline_or_nobody_holds_is_updated(self):
        for owner in (None, {"pipelineId": PIPELINE, "agentId": AGENT}):
            hub = FakeCoreHub(collections=[{"id": "stored-shop", "name": "shop", "owner": owner}])
            provision_inbound("jwt", inbound_config(), PIPELINE, AGENT, fetch=hub)
            self.assertIn(("PUT", f"{INBOUND_COLLECTIONS_PATH}/stored-shop"), hub.methods_and_paths())


class CallAndValueRuleTests(unittest.TestCase):
    """What Core Hub would refuse only after the pipeline exists is refused at plan time."""

    def refused(self, *endpoints, collections=None):
        with self.assertRaises(CourierConfigError) as raised:
            plan_inbound_definitions(routes_config(*endpoints, collections=collections))
        return str(raised.exception)

    def test_a_call_needs_an_execute_route_a_schema_a_name_and_a_known_kind(self):
        message = self.refused(
            {"name": "w", "operation": "CREATE", "path": "/w", "bindings": ["a"], "call": {"schema": "s", "name": "p"}},
            {"name": "x", "path": "/x", "call": {"kind": "TRIGGER", "parameters": ["p_a"]}},
            {"name": "y", "path": "/y", "call": "place_order"},
        )
        self.assertIn("endpoint 'w': call: only an EXECUTE route calls a routine (operation CREATE)", message)
        self.assertIn("endpoint 'x': call needs the routine's 'schema'", message)
        self.assertIn("endpoint 'x': call needs the routine's 'name'", message)
        self.assertIn("call.kind 'TRIGGER'", message)
        self.assertIn("call.parameters must map", message)
        self.assertIn("endpoint 'y': call must be an object", message)

    def test_a_default_must_fit_the_column_or_the_routine_argument(self):
        message = self.refused(
            {"name": "w", "path": "/w", "bindings": [
                {"name": "n", "type": "NUMBER", "default": "1.234"},
                {"name": "m", "type": "NUMBER", "default": "12345678901234567"},
                {"name": "s", "default": "x" * 256},
            ]},
        )
        self.assertIn("'n': defaultValue '1.234' takes at most 2 decimal places", message)
        self.assertIn("'m': defaultValue '12345678901234567' takes at most 16 integer digits", message)
        self.assertIn("'s': defaultValue is longer than 255 characters", message)
        routes = plan_inbound_definitions(routes_config(
            {"name": "w", "path": "/w", "bindings": [{"name": "n", "type": "NUMBER", "default": "1e2"}, {"name": "s", "default": "x" * 255}]},
            {"name": "x", "path": "/x", "routine": {}, "bindings": [{"name": "n", "type": "NUMBER", "default": "1.234"},
                                                                    {"name": "s", "default": "x" * 300}]},
        ))["collections"][0]["endpoints"]
        self.assertEqual(len(routes), 2, "1e2 fits NUMERIC(18,2); a routine takes 38 decimal places and long text")
        self.assertIn("38 integer digits", self.refused({"name": "x", "path": "/x", "routine": {}, "bindings": [
            {"name": "n", "type": "NUMBER", "default": "1" * 39}]}))

    def test_a_handle_of_another_stored_collection_is_refused(self):
        stored = [{"id": "other-id", "name": "orders", "basePath": "shop"}]
        with self.assertRaises(CourierConfigError) as raised:
            plan_inbound_definitions(routes_config({"name": "w", "path": "/w", "bindings": ["a"]}), existing_collections=stored)
        self.assertIn("is a handle of stored collection 'orders'", str(raised.exception))

if __name__ == "__main__":
    unittest.main()
