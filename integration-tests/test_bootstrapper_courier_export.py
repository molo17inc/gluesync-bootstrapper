"""Export of the Courier definitions of the HTTP agents (courier_export), and their restore.

Offline: a stateful fake Core Hub stores what the provisioning writes, so a definition can be provisioned, exported,
restored from the export and exported again, as the Automator and its round trip do.
"""

import copy
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import automator_roundtrip as rt  # noqa: E402
from automator_app import corehub  # noqa: E402
from courier_export import agent_key, export_courier_block, export_pipeline_courier  # noqa: E402
from courier_inbound_support import (  # noqa: E402
    API_TOKENS_PATH,
    INBOUND_COLLECTIONS_PATH,
    INBOUND_ENDPOINTS_PATH,
    provision_courier_agent,
)
from courier_support import COLLECTIONS_PATH, ENDPOINTS_PATH, CourierConfigError  # noqa: E402

PIPELINE, AGENT = "p1", "http-source-1"
RESTORED_PIPELINE, RESTORED_AGENT = "p2", "http-source-2"
SECRET = "s3cr3t-bearer"


class StatefulCoreHub:
    """Stores the Courier collections and endpoints it is sent, as Core Hub does, and answers the GETs from them."""

    MASK = "********"

    def __init__(self):
        self.store = {path: {} for path in (INBOUND_COLLECTIONS_PATH, INBOUND_ENDPOINTS_PATH, COLLECTIONS_PATH, ENDPOINTS_PATH)}
        self.tokens = []
        self.endpoint_params = []
        self.pipelines = [{"pipelineId": PIPELINE}, {"pipelineId": RESTORED_PIPELINE}]
        self.writes = []

    def __call__(self, path, method="GET", token=None, body=None, params=None, headers=None):
        params = params or {}
        if method != "GET":
            self.writes.append((method, path))
        if path == "/pipelines":
            return copy.deepcopy(self.pipelines)
        if path == API_TOKENS_PATH:
            if method == "GET":
                return copy.deepcopy(self.tokens)
            created = {"id": f"token-{len(self.tokens) + 1}", "name": body["name"], "token": "gsp_plain", "active": True}
            self.tokens.append({k: v for k, v in created.items() if k != "token"})
            return created
        base = next(p for p in self.store if path == p or path.startswith(p + "/"))
        items = self.store[base]
        if method == "GET":
            if base in (INBOUND_ENDPOINTS_PATH, ENDPOINTS_PATH):
                self.endpoint_params.append(dict(params))
                found = [e for e in items.values() if e["collectionId"] == params.get("collectionId")]
                if base == ENDPOINTS_PATH and params.get("include_secrets") != "true":
                    found = [{**e, "headers": [{**h, "valueTemplate": self.MASK} for h in e.get("headers") or []]} for e in found]
                return copy.deepcopy(found)
            if base == COLLECTIONS_PATH:  # Core Hub never returns the auth secret
                return [{**c, "auth": {k: v for k, v in c["auth"].items() if k not in ("token", "password", "value")}}
                        for c in copy.deepcopy(list(items.values()))]
            if base == INBOUND_COLLECTIONS_PATH:  # Core Hub reports the HTTP Source of a pipeline it knows only
                known = {p["pipelineId"] for p in self.pipelines}
                return [{**c, "owner": c.get("owner") if (c.get("owner") or {}).get("pipelineId") in known else None}
                        for c in copy.deepcopy(list(items.values()))]
            return copy.deepcopy(list(items.values()))
        stored = items.get(body["id"])
        if method == "POST" and stored is not None:
            raise RuntimeError("409 conflict")
        saved = copy.deepcopy(body)
        if stored is not None and base == COLLECTIONS_PATH and not saved["auth"].get("token"):
            saved["auth"]["token"] = stored["auth"].get("token")  # a blank secret keeps the stored one
        if stored is not None and base == ENDPOINTS_PATH:
            old = {h["name"]: h["valueTemplate"] for h in stored.get("headers") or []}
            for header in saved.get("headers") or []:
                if header["valueTemplate"] == self.MASK:
                    header["valueTemplate"] = old.get(header["name"], "")
        if stored is not None and "owner" in stored:
            saved["owner"] = stored["owner"]  # computed by Core Hub from the HTTP Sources, never sent
        items[body["id"]] = saved
        return saved


def source_agent(pipeline_agent_id=AGENT, courier=None, collection=None):
    custom = {"inboundCollection": collection} if collection else {}
    agent = {"agentType": "SOURCE", "agentTag": "courier-agent", "agentId": pipeline_agent_id, "customHostCredentials": custom}
    if courier is not None:
        agent["courier"] = courier
    return agent


def inbound_config(output_dir):
    return {
        "collections": [
            {"name": "shop", "basePath": "shop", "allowlist": True, "endpoints": [
                {"name": "orders", "httpVerb": "POST", "path": "/orders/{id}", "operationBinding": "op", "bindings": [
                    {"name": "id", "location": "PATH", "type": "NUMBER"},
                    {"name": "op", "location": "QUERY", "required": False, "defaultValue": "Insert"},
                    {"name": "status", "location": "BODY", "required": False},
                ]},
                {"name": "order", "httpVerb": "GET", "path": "/orders/{id}",
                 "bindings": [{"name": "id", "location": "PATH", "type": "NUMBER"}],
                 "responseFields": [{"name": "id", "type": "NUMBER"}, "status"]},
                {"name": "place", "path": "/place-order", "bindings": ["customer"],
                 "routine": {"answer": {"body": "OUTPUT"}}, "call": {"schema": "shop", "name": "place_order"}},
            ]},
            {"name": "billing", "basePath": "billing", "endpoints": [
                {"name": "invoices", "httpVerb": "POST", "path": "/invoices/{id}",
                 "bindings": [{"name": "id", "location": "PATH", "type": "NUMBER"}]},
            ]},
        ],
        "tokens": [
            {"name": "shop-client", "collections": ["shop"], "outputFile": f"{output_dir}/shop.json"},
            {"name": "billing-client", "collections": ["billing"], "outputFile": f"{output_dir}/billing.json"},
        ],
    }


def target_agent(outbound_collection=None, courier=None):
    custom = {"outboundCollection": outbound_collection} if outbound_collection else {}
    agent = {"agentType": "TARGET", "agentTag": "courier-agent", "agentId": "http-target-1", "customHostCredentials": custom}
    if courier is not None:
        agent["courier"] = courier
    return agent


def outbound_config():
    return {"collections": [
        {"name": "demo-api", "baseUrl": "http://api.example.com", "auth": {"type": "Bearer", "token": SECRET}, "endpoints": [
            {"name": "customers", "httpVerb": "POST", "pathTemplate": "/customers/{id}",
             "headers": [{"name": "X-Api-Key", "valueTemplate": "key-123"}],
             "bindings": [{"name": "id", "location": "PATH", "type": "NUMBER"}, {"name": "name"}]},
        ]},
        {"name": "other-api", "baseUrl": "https://other.example.com", "endpoints": [
            {"name": "ping", "httpVerb": "POST", "pathTemplate": "/ping", "bindings": ["x"]},
        ]},
    ]}


def attach(hub, pipeline_id, agent):
    """Provisions an HTTP Source entry and configures the agent, as main.py does: Core Hub then reports the collection
    its ``inboundCollection`` names as attached to it (the collection's ``owner``)."""
    custom = provision_courier_agent("jwt", pipeline_id, agent, fetch=hub)
    handle = custom["inboundCollection"]
    for collection in hub.store[INBOUND_COLLECTIONS_PATH].values():
        if collection.get("owner") == {"pipelineId": pipeline_id, "agentId": agent["agentId"]}:
            collection["owner"] = None
        if handle in (collection["name"], collection["basePath"], collection["id"]):
            collection["owner"] = {"pipelineId": pipeline_id, "agentId": agent["agentId"]}
    return custom


class InboundExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hub = StatefulCoreHub()
        attach(self.hub, PIPELINE, source_agent(courier=inbound_config(self.tmp.name), collection="shop"))

    def tearDown(self):
        self.tmp.cleanup()

    def restore(self, pipeline_id, agent_id, block, restore=True, logger=None):
        agent = source_agent(agent_id, courier=copy.deepcopy(block))
        return provision_courier_agent("jwt", pipeline_id, agent, fetch=self.hub, logger=logger, keep_other_pipelines=restore)

    def test_the_export_is_the_served_collection_with_its_routes_ids_and_without_tokens(self):
        block = export_courier_block("jwt", PIPELINE, source_agent(collection="shop"), fetch=self.hub)

        self.assertNotIn("tokens", block)
        (shop,) = block["collections"]
        self.assertEqual((shop["name"], shop["basePath"], shop["allowlist"]), ("shop", "shop", True))
        self.assertEqual(shop["allowedTokenIds"], ["token-1"])
        orders, order, place = shop["endpoints"]
        self.assertEqual(orders["path"], "/orders/{id}")
        self.assertEqual(orders["operationBinding"], "op")
        self.assertEqual(orders["bindings"][1], {"name": "op", "location": "QUERY", "type": "STRING", "required": False,
                                                 "defaultValue": "Insert"})
        self.assertEqual(orders["bindings"][0], {"name": "id", "location": "PATH", "type": "NUMBER"})
        self.assertNotIn("routine", orders)
        self.assertEqual(order["responseFields"], [{"name": "id", "type": "NUMBER"}, {"name": "status", "type": "STRING"}])
        self.assertEqual(place["routine"]["answer"], {"body": "OUTPUT", "format": "JSON"})
        self.assertNotIn("call", place, "the routine an EXECUTE route calls is its entity's")
        self.assertTrue(shop["id"] and orders["id"])

    def test_the_attached_collection_is_exported_with_all_its_endpoints_whoever_they_serve(self):
        orders = next(iter(self.hub.store[INBOUND_ENDPOINTS_PATH].values()))
        # Core Hub 2.3.2.2+: free routes, no pipeline or agent.
        free = {k: v for k, v in orders.items() if k not in ("pipelineId", "agentId")}
        free.update(id="free-1", name="free", path="/free")
        self.hub.store[INBOUND_ENDPOINTS_PATH]["free-1"] = free
        self.hub.store[INBOUND_COLLECTIONS_PATH]["lonely"] = {"id": "lonely", "name": "lonely", "basePath": "lonely"}

        block = export_courier_block("jwt", "another-pipeline", source_agent(collection="shop"), fetch=self.hub)

        self.assertEqual([c["name"] for c in block["collections"]], ["shop"], "only the collection it is attached to")
        self.assertEqual([e["name"] for e in block["collections"][0]["endpoints"]], ["orders", "order", "place", "free"])
        self.assertEqual(self.hub.endpoint_params[-1], {"collectionId": block["collections"][0]["id"]})

    def test_the_attached_collection_is_found_by_id_name_or_base_path(self):
        shop_id = next(c["id"] for c in self.hub.store[INBOUND_COLLECTIONS_PATH].values() if c["name"] == "shop")
        self.hub.store[INBOUND_COLLECTIONS_PATH][shop_id]["basePath"] = "shop-path"
        by_id, by_name, by_path = (
            export_courier_block("jwt", PIPELINE, source_agent(collection=ref), fetch=self.hub)
            for ref in (shop_id, "shop", "shop-path")
        )
        self.assertEqual(by_id, by_name)
        self.assertEqual(by_id, by_path)
        self.assertEqual(by_id["collections"][0]["id"], shop_id)

    def test_an_agent_attached_to_no_or_an_unknown_collection_has_no_block(self):
        self.assertIsNone(export_courier_block("jwt", PIPELINE, source_agent(), fetch=self.hub))
        self.assertIsNone(export_courier_block("jwt", PIPELINE, source_agent(collection="unknown"), fetch=self.hub))
        hosted = {"agentType": "SOURCE", "agentTag": "courier-agent", "agentId": AGENT,
                  "hostCredentialsCustomProperties": {"inboundCollection": "billing"}}
        self.assertEqual(export_courier_block("jwt", PIPELINE, hosted, fetch=self.hub)["collections"][0]["name"], "billing")

    def test_a_restore_onto_the_pipeline_it_was_exported_from_gives_the_same_export_and_creates_no_token(self):
        exported = export_courier_block("jwt", PIPELINE, source_agent(collection="shop"), fetch=self.hub)

        custom = self.restore(PIPELINE, AGENT, exported)

        self.assertEqual(custom["inboundCollection"], "shop")
        self.assertEqual(export_courier_block("jwt", PIPELINE, source_agent(collection="shop"), fetch=self.hub), exported)
        self.assertEqual(len(self.hub.tokens), 2, "the restore creates no token and revokes none")
        self.assertEqual(len(self.hub.store[INBOUND_COLLECTIONS_PATH]), 2, "the same collections are updated in place")

    def test_a_restore_into_another_pipeline_never_takes_the_collection_of_a_live_one(self):
        exported = export_courier_block("jwt", PIPELINE, source_agent(collection="shop"), fetch=self.hub)
        writes_before = len(self.hub.writes)

        with self.assertRaises(CourierConfigError) as raised:
            self.restore(RESTORED_PIPELINE, RESTORED_AGENT, exported)

        self.assertIn("'shop' is served by HTTP Source http-source-1 of pipeline p1", str(raised.exception))
        self.assertEqual(len(self.hub.writes), writes_before, "refused before anything is written")

    def test_a_restore_or_a_run_after_the_original_pipeline_is_gone_takes_the_collection(self):
        exported = export_courier_block("jwt", PIPELINE, source_agent(collection="shop"), fetch=self.hub)
        self.hub.pipelines = [{"pipelineId": RESTORED_PIPELINE}]
        for restore in (True, False):
            custom = self.restore(RESTORED_PIPELINE, RESTORED_AGENT, exported, restore=restore)
            self.assertEqual(custom["inboundCollection"], "shop")

    def test_a_bootstrapper_run_never_takes_the_collection_of_a_live_pipeline_either(self):
        exported = export_courier_block("jwt", PIPELINE, source_agent(collection="shop"), fetch=self.hub)
        tokens = copy.deepcopy(self.hub.tokens)
        block = {**copy.deepcopy(exported), "tokens": [{"name": "shop-client", "outputFile": f"{self.tmp.name}/x.json"}]}
        with self.assertRaises(CourierConfigError):
            self.restore(RESTORED_PIPELINE, RESTORED_AGENT, block, restore=False)
        self.assertEqual(self.hub.tokens, tokens, "the live pipeline's token is not revoked")

    def test_an_endpoint_renamed_in_the_ui_is_found_by_the_id_of_the_block(self):
        exported = export_courier_block("jwt", PIPELINE, source_agent(collection="shop"), fetch=self.hub)
        orders_id = exported["collections"][0]["endpoints"][0]["id"]
        self.hub.store[INBOUND_ENDPOINTS_PATH][orders_id]["name"] = "orders-renamed"

        self.restore(PIPELINE, AGENT, exported)

        self.assertEqual(self.hub.store[INBOUND_ENDPOINTS_PATH][orders_id]["name"], "orders")
        self.assertEqual(len(self.hub.store[INBOUND_ENDPOINTS_PATH]), 4, "updated in place, not duplicated")


class OutboundExportTests(unittest.TestCase):
    def setUp(self):
        self.hub = StatefulCoreHub()
        provision_courier_agent("jwt", PIPELINE, target_agent(courier=outbound_config()), fetch=self.hub)

    def test_the_export_follows_outbound_collection_and_leaves_the_secret_out(self):
        block = export_courier_block("jwt", PIPELINE, target_agent("demo-api"), fetch=self.hub)

        (collection,) = block["collections"]
        self.assertEqual(collection["name"], "demo-api")
        self.assertEqual(collection["auth"], {"type": "Bearer"})
        endpoint = collection["endpoints"][0]
        self.assertEqual(endpoint["headers"], [{"name": "X-Api-Key", "valueTemplate": StatefulCoreHub.MASK}])
        self.assertEqual(endpoint["bindings"][1], {"name": "name", "location": "BODY", "type": "STRING"})

    def test_every_collection_without_outbound_collection_and_header_values_with_include_secrets(self):
        block = export_courier_block("jwt", PIPELINE, target_agent(), fetch=self.hub, include_secrets=True)

        self.assertEqual([c["name"] for c in block["collections"]], ["demo-api", "other-api"])
        self.assertEqual(block["collections"][0]["endpoints"][0]["headers"][0]["valueTemplate"], "key-123")
        self.assertTrue(all(p.get("include_secrets") == "true" for p in self.hub.endpoint_params))

    def test_a_restore_onto_the_same_core_hub_keeps_the_stored_secrets(self):
        exported = export_courier_block("jwt", PIPELINE, target_agent("demo-api"), fetch=self.hub)

        provision_courier_agent("jwt", RESTORED_PIPELINE, target_agent("demo-api", courier=copy.deepcopy(exported)),
                                fetch=self.hub)

        stored = next(c for c in self.hub.store[COLLECTIONS_PATH].values() if c["name"] == "demo-api")
        self.assertEqual(stored["auth"]["token"], SECRET)
        endpoint = next(e for e in self.hub.store[ENDPOINTS_PATH].values() if e["name"] == "customers")
        self.assertEqual(endpoint["headers"][0]["valueTemplate"], "key-123")
        self.assertEqual(export_courier_block("jwt", RESTORED_PIPELINE, target_agent("demo-api"), fetch=self.hub), exported)

    def test_a_restore_of_masked_header_values_where_core_hub_has_none_to_keep_is_refused(self):
        exported = export_courier_block("jwt", PIPELINE, target_agent("demo-api"), fetch=self.hub)
        exported["collections"][0]["auth"]["token"] = SECRET  # the auth secret typed in: only the header is missing

        other_hub = StatefulCoreHub()
        agent = target_agent("demo-api", courier=copy.deepcopy(exported))
        with self.assertRaises(CourierConfigError) as raised:
            provision_courier_agent("jwt", PIPELINE, agent, fetch=other_hub)
        self.assertIn("header 'X-Api-Key' is masked", str(raised.exception))
        self.assertEqual(other_hub.writes, [], "refused before anything is written")

        moved = copy.deepcopy(exported)
        moved["collections"][0]["baseUrl"] = "https://elsewhere.example.com"
        moved["force"] = True
        agent = target_agent("demo-api", courier=moved)
        with self.assertRaises(CourierConfigError) as raised:
            provision_courier_agent("jwt", PIPELINE, agent, fetch=self.hub)
        self.assertIn("moves to another origin", str(raised.exception))

        same_origin = copy.deepcopy(exported)
        same_origin["collections"][0]["baseUrl"] = "http://api.example.com:80/v2"
        provision_courier_agent("jwt", PIPELINE, target_agent("demo-api", courier=same_origin), fetch=self.hub)
        endpoint = next(e for e in self.hub.store[ENDPOINTS_PATH].values() if e["name"] == "customers")
        self.assertEqual(endpoint["headers"][0]["valueTemplate"], "key-123", "same origin: the stored value is kept")

    def test_a_restore_onto_another_core_hub_asks_for_the_secret(self):
        exported = export_courier_block("jwt", PIPELINE, target_agent("demo-api"), fetch=self.hub)

        agent, other_hub = target_agent("demo-api", courier=exported), StatefulCoreHub()
        with self.assertRaises(CourierConfigError) as raised:
            provision_courier_agent("jwt", PIPELINE, agent, fetch=other_hub)
        self.assertIn("auth.token is required", str(raised.exception))

    def test_a_non_http_agent_has_no_block(self):
        self.assertIsNone(export_courier_block("jwt", PIPELINE, {"agentType": "SOURCE", "agentTag": "postgresql-cdc"},
                                               fetch=self.hub))

    def test_the_blocks_of_a_pipeline_are_keyed_by_type_and_tag(self):
        blocks = export_pipeline_courier("jwt", PIPELINE, [target_agent(), {"agentType": "SOURCE", "agentTag": "x"}],
                                         fetch=self.hub)
        self.assertEqual(list(blocks), ["TARGET:courier-agent"])


def corehub_error(status, body=""):
    """What ``fetch_core_hub`` raises: a RuntimeError caused by the HTTP error that carries the response."""
    http_error = Exception(f"{status} Client Error")
    http_error.response = mock.Mock(status_code=status, text=body)
    try:
        raise RuntimeError(f"CoreHub request failed: {http_error} | Response: {body}") from http_error
    except RuntimeError as wrapped:
        return wrapped


class OlderCoreHubTests(unittest.TestCase):
    """A Core Hub without token collection scopes, or without a Courier route, is handled, not failed on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = {"collections": inbound_config(self.tmp.name)["collections"][:1],
                       "tokens": [{"name": "shop-client", "outputFile": f"{self.tmp.name}/shop.json"}]}

    def tearDown(self):
        self.tmp.cleanup()

    def _token_posts(self, hub):
        return [body for method, path, body in hub.posts if path == API_TOKENS_PATH]

    def test_a_refused_collection_scope_is_raised_and_never_retried_without_it(self):
        hub = StatefulCoreHub()
        hub.posts = []
        answer = hub.__call__

        def refusing(path, method="GET", token=None, body=None, params=None, headers=None):
            if method == "POST":
                hub.posts.append((method, path, copy.deepcopy(body)))
                if path == API_TOKENS_PATH and "collectionScopes" in body:
                    raise corehub_error(400, "Collection scopes need the gluesync.https_agent permission scope")
            return answer(path, method=method, token=token, body=body, params=params, headers=headers)

        agent = source_agent(courier=self.config)
        with self.assertRaises(RuntimeError):
            provision_courier_agent("jwt", PIPELINE, agent, fetch=refusing)

        # One POST, with the scopes: no unscoped token is minted in its place.
        (only,) = self._token_posts(hub)
        self.assertIn("collectionScopes", only)
        self.assertEqual(len(hub.tokens), 0)

    def test_a_core_hub_ignoring_collection_scopes_is_warned_about_and_one_honouring_them_is_not(self):
        log = mock.Mock()
        provision_courier_agent("jwt", PIPELINE, source_agent(courier=self.config), fetch=StatefulCoreHub(), logger=log)
        self.assertTrue(any("does not scope" in c.args[0] for c in log.warning.call_args_list))

        honouring = StatefulCoreHub()
        answer = honouring.__call__

        def scoped(path, method="GET", token=None, body=None, params=None, headers=None):
            created = answer(path, method=method, token=token, body=body, params=params, headers=headers)
            if method == "POST" and path == API_TOKENS_PATH:
                created["collectionScopes"] = body["collectionScopes"]
            return created

        log = mock.Mock()
        provision_courier_agent("jwt", PIPELINE, source_agent(courier=self.config), fetch=scoped, logger=log)
        self.assertFalse(any("does not scope" in c.args[0] for c in log.warning.call_args_list))

    def test_another_token_refusal_is_not_retried(self):
        def failing(path, method="GET", **kwargs):
            if method == "POST" and path == API_TOKENS_PATH:
                raise corehub_error(400, "Token name cannot be blank")
            return [] if method == "GET" else kwargs.get("body")

        agent = source_agent(courier=self.config)
        with self.assertRaises(RuntimeError):
            provision_courier_agent("jwt", PIPELINE, agent, fetch=failing)

    def test_no_courier_route_exports_nothing_and_another_failure_is_raised(self):
        def missing(path, method="GET", **kwargs):
            raise corehub_error(404, "Not Found")

        self.assertIsNone(export_courier_block("jwt", PIPELINE, source_agent(), fetch=missing))
        self.assertIsNone(export_courier_block("jwt", PIPELINE, target_agent(), fetch=missing))
        self.assertEqual(export_pipeline_courier("jwt", PIPELINE, [source_agent(), target_agent()], fetch=missing), {})

        def broken(path, method="GET", **kwargs):
            raise corehub_error(500, "boom")

        agent = target_agent()
        with self.assertRaises(RuntimeError):
            export_courier_block("jwt", PIPELINE, agent, fetch=broken)


class AutomatorCourierTests(unittest.TestCase):
    def test_the_backup_carries_the_courier_block_of_an_http_agent(self):
        backup = mock.Mock(token="jwt", include_secrets=False, seen_agents=set(), agents=[])
        agent = dict(target_agent(), hostCredentials={"connectionName": "HTTP Target"})
        block = {"collections": [{"name": "demo-api"}]}
        with mock.patch.object(corehub, "export_courier_block", return_value=block) as export:
            corehub._collect_backup_agents(backup, [agent], PIPELINE)

        self.assertEqual(backup.agents[0]["courier"], block)
        export.assert_called_once()
        self.assertEqual(export.call_args.args[1], PIPELINE)

    def test_a_failed_courier_export_does_not_stop_the_backup(self):
        backup = mock.Mock(token="jwt", include_secrets=False, seen_agents=set(), agents=[])
        agent = dict(target_agent(), hostCredentials={"connectionName": "HTTP Target"})
        with mock.patch.object(corehub, "export_courier_block", side_effect=RuntimeError("boom")):
            corehub._collect_backup_agents(backup, [agent], PIPELINE)
        self.assertNotIn("courier", backup.agents[0])

    def test_the_restore_provisions_the_block_before_the_credentials(self):
        conf_agent = target_agent(courier={"collections": [{"name": "demo-api"}]})
        with mock.patch.object(corehub, "provision_courier_agent", return_value={"outboundCollection": "demo-api"}) as provision:
            custom = corehub._restore_courier_block("jwt", PIPELINE, "new-id", conf_agent, {})

        self.assertEqual(custom, {"outboundCollection": "demo-api"})
        self.assertEqual(provision.call_args.args[2]["agentId"], "new-id")
        self.assertTrue(provision.call_args.kwargs["keep_other_pipelines"], "a restore never takes a live pipeline's endpoints")

    def test_an_agent_without_a_block_is_left_as_it_is(self):
        with mock.patch.object(corehub, "provision_courier_agent") as provision:
            self.assertEqual(corehub._restore_courier_block("jwt", PIPELINE, "id", target_agent(), {"a": 1}), {"a": 1})
        provision.assert_not_called()


EXPORT_YAML = """# ---------------------------------------------------------------
# Gluesync Automator - Pipeline Configuration Export
# Pipeline ID     : PIPE
# ---------------------------------------------------------------
shop:
  target: inbound
  tables:
    whitelist:
    - events
    custom:
      events:
        name: events
        entityName: shop.events
        keys: [_seq]
groups:
  default:
    id: _default
"""


def exported_inbound():
    return {"collections": [{
        "id": "c1", "name": "shop", "basePath": "shop", "allowlist": True, "allowedTokenIds": ["t1"],
        "blockedTokenIds": ["t9"],
        "endpoints": [{"id": "e1", "name": "events", "httpVerb": "POST", "path": "/events",
                       "bindings": [{"name": "kind", "location": "BODY", "type": "STRING"}]}],
    }]}


class RoundTripCourierTests(unittest.TestCase):
    def test_the_restore_config_takes_the_exported_outbound_blocks_without_the_kit_tokens(self):
        kit = {"agents": [
            target_agent(courier={"collections": [{"name": "kit-only"}], "tokens": [{"name": "t"}]}),
            {"agentType": "TARGET", "agentTag": "postgresql-cdc", "hostCredentials": {"certificatePath": "certs/ca.pem"}},
        ]}
        outbound = {"collections": [{"id": "o1", "name": "demo-api", "endpoints": [{"id": "oe1", "name": "customers"}]}]}

        restored = rt.courier_restore_config(kit, {agent_key(target_agent()): outbound}, Path("/kit"))

        self.assertEqual(restored["agents"][0]["courier"], outbound, "an outbound block is restored as exported, ids included")
        self.assertEqual(restored["agents"][1]["hostCredentials"]["certificatePath"], "/kit/certs/ca.pem")
        self.assertIn("tokens", kit["agents"][0]["courier"], "the kit config is left untouched")
        self.assertNotIn("courier", rt.courier_restore_config(kit, {}, Path("/kit"))["agents"][0],
                         "nothing exported for it: nothing restored")

    def test_an_http_source_is_restored_as_a_renamed_copy_with_a_token_of_its_own(self):
        kit = {"agents": [source_agent(courier={"collections": [{"name": "shop"}], "tokens": [{"name": "kit-token"}]})]}
        kit["agents"][0]["customHostCredentials"] = {"inboundCollection": "shop", "maxBodyKb": 1}
        exported = exported_inbound()
        with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as token_dir:
            restored = rt.courier_restore_config(kit, {agent_key(source_agent()): exported}, Path("/kit"), PIPELINE,
                                                 Path(token_dir))

            agent = restored["agents"][0]
            (collection,) = agent["courier"]["collections"]
            self.assertEqual((collection["name"], collection["basePath"]), ("shop-roundtrip", "shop-roundtrip"))
            self.assertTrue(collection["allowlist"])
            for dropped in ("id", "allowedTokenIds", "blockedTokenIds"):
                self.assertNotIn(dropped, collection)
            self.assertNotIn("id", collection["endpoints"][0])
            self.assertEqual(collection["endpoints"][0]["path"], "/events")
            self.assertEqual(agent["customHostCredentials"], {"inboundCollection": "shop-roundtrip", "maxBodyKb": 1})
            (token,) = agent["courier"]["tokens"]
            self.assertEqual(token["name"], f"roundtrip-courier-agent-{PIPELINE}")
            self.assertEqual((token["expiresInDays"], token["collections"]), (1, ["shop-roundtrip"]))
            self.assertEqual(Path(token["outputFile"]).parent, Path(token_dir))
            self.assertNotIn(out, token["outputFile"])
        self.assertEqual(exported, exported_inbound(), "the exported block is not modified")
        self.assertEqual(kit["agents"][0]["customHostCredentials"]["inboundCollection"], "shop")

    def test_the_copy_names_are_valid_for_provisioning(self):
        with tempfile.TemporaryDirectory() as token_dir:
            restored = rt.courier_restore_config({"agents": [source_agent()]}, {agent_key(source_agent()): exported_inbound()},
                                                 Path("/kit"), PIPELINE, Path(token_dir))
        agent = restored["agents"][0]
        provision_courier_agent("jwt", "p9", agent, fetch=StatefulCoreHub())
        self.assertEqual(agent["customHostCredentials"]["inboundCollection"], "shop-roundtrip")

    def test_the_export_yaml_is_rewritten_for_the_copy_in_the_collection_places_only(self):
        text = rt.rename_export_collections(EXPORT_YAML, ["shop"])
        document = yaml.safe_load(text)

        self.assertEqual(list(document), ["shop-roundtrip", "groups"])
        custom = document["shop-roundtrip"]["tables"]["custom"]["events"]
        self.assertEqual((custom["name"], custom["entityName"]), ("events", "shop-roundtrip.events"))
        self.assertEqual(document["shop-roundtrip"]["tables"]["whitelist"], ["events"])
        self.assertEqual(document["groups"], {"default": {"id": "_default"}})
        self.assertEqual(rt.strip_export_header(text), text, "no header: nothing to strip, still the same YAML")
        self.assertEqual(yaml.safe_load(rt.strip_export_header(EXPORT_YAML)), yaml.safe_load(
            rt.restore_original_names({"export": text, "agents": [], "entities": [], "groups": []}, ["shop"])["export"]))

    def test_the_restored_side_is_mapped_back_and_its_id_map_recomputed(self):
        agents = [{"agentId": "src", "agentType": "SOURCE"}, {"agentId": "tgt", "agentType": "TARGET"}]
        entities = [{"entityId": "ent-1", "entityName": "shop-roundtrip.events", "agentEntities": [
            {"agentEntityId": "ae-src-12345", "agentId": "src", "type": "SOURCE",
             "table": {"schema": "shop-roundtrip", "name": "events"}}]}]
        side = {
            "pipelineId": "p2", "agents": agents, "entities": entities, "groups": [], "jobs": [],
            "courier": {"SOURCE:courier-agent": {"collections": [{"name": "shop-roundtrip", "basePath": "shop-roundtrip"}]}},
            "export": "shop-roundtrip:\n  tables:\n    custom:\n      events:\n        entityName: shop-roundtrip.events\n",
            "idMap": rt.build_id_map("p2", agents, entities, []),
        }
        self.assertIn("<entity:shop-roundtrip.events>", side["idMap"].values())

        mapped = rt.restore_original_names(side, ["shop"])

        self.assertEqual(mapped["entities"][0]["entityName"], "shop.events")
        self.assertEqual(mapped["entities"][0]["agentEntities"][0]["table"], {"schema": "shop", "name": "events"})
        self.assertEqual(mapped["courier"]["SOURCE:courier-agent"]["collections"][0], {"name": "shop", "basePath": "shop"})
        self.assertNotIn("-roundtrip", mapped["export"])
        self.assertEqual(mapped["idMap"]["ent-1"], "<entity:shop.events>")
        self.assertEqual(mapped["idMap"]["ae-src-12345"], "<agentEntity:shop.events:SOURCE>")
        self.assertFalse(any("roundtrip" in v for v in mapped["idMap"].values()))
        self.assertIn("shop-roundtrip", side["export"], "the input is not modified")
        self.assertIs(rt.restore_original_names(side, []), side)

    def test_the_inbound_collection_names_come_from_the_source_blocks_only(self):
        blocks = {agent_key(source_agent()): exported_inbound(),
                  agent_key(target_agent()): {"collections": [{"name": "demo-api"}]}}
        self.assertEqual(rt.inbound_collection_names(blocks), ["shop"])

    def test_a_restored_collection_differing_only_by_ids_passes_the_courier_check(self):
        original = {"courier": {"SOURCE:courier-agent": exported_inbound()}}
        restored = copy.deepcopy(original)
        collection = restored["courier"]["SOURCE:courier-agent"]["collections"][0]
        collection.update(id="c2", allowedTokenIds=["t2", "t3"])
        collection.pop("blockedTokenIds")
        collection["endpoints"][0]["id"] = "e2"
        self.assertTrue(rt.check_courier(original, restored).passed)

        collection["endpoints"][0]["path"] = "/events2"
        failed = rt.check_courier(original, restored)
        self.assertFalse(failed.passed)
        self.assertTrue(any("/events2" in line for line in failed.diff))
        self.assertFalse(rt.check_courier(original, {"courier": {}}).passed)

    def test_the_courier_check_still_compares_outbound_blocks(self):
        block = {"TARGET:courier-agent": {"collections": [{"name": "demo-api", "baseUrl": "http://a"}]}}
        self.assertTrue(rt.check_courier({"courier": block}, {"courier": copy.deepcopy(block)}).passed)
        other = copy.deepcopy(block)
        other["TARGET:courier-agent"]["collections"][0]["baseUrl"] = "http://b"
        self.assertFalse(rt.check_courier({"courier": block}, {"courier": other}).passed)

    def test_an_http_source_round_trip_end_to_end_passes_once_mapped_back(self):
        def pipeline_side(pipeline_id, schema, courier):
            agents = [{"agentId": f"src-{pipeline_id}", "agentType": "SOURCE"},
                      {"agentId": f"tgt-{pipeline_id}", "agentType": "TARGET"}]
            entities = [{"entityId": f"ent-{pipeline_id}1", "entityName": f"{schema}.events", "agentEntities": [
                {"agentEntityId": f"ae-{pipeline_id}-src", "agentId": f"src-{pipeline_id}", "type": "SOURCE",
                 "table": {"id": f"{pipeline_id}-tbl", "schema": schema, "name": "events"}},
                {"agentEntityId": f"ae-{pipeline_id}-tgt", "agentId": f"tgt-{pipeline_id}", "type": "TARGET",
                 "table": {"schema": "public", "name": "events"}}]}]
            return {"pipelineId": pipeline_id, "agents": agents, "entities": entities, "groups": [], "jobs": [],
                    "courier": courier,
                    "export": EXPORT_YAML.replace("shop", schema).replace("PIPE", pipeline_id),
                    "idMap": rt.build_id_map(pipeline_id, agents, entities, [])}

        original = pipeline_side("aaaaaaaa", "shop", {"SOURCE:courier-agent": exported_inbound()})
        with tempfile.TemporaryDirectory() as token_dir:
            copied = rt.courier_restore_config({"agents": [source_agent()]}, original["courier"], Path("/kit"), "aaaaaaaa",
                                               Path(token_dir))["agents"][0]["courier"]
        # What Core Hub reports for the copy: new ids, the token of the restore on its allowlist.
        for collection in copied["collections"]:
            collection.update(id="c-copy", allowedTokenIds=["t-new"])
            for endpoint in collection["endpoints"]:
                endpoint["id"] = "e-copy"
        copied.pop("tokens")
        restored = pipeline_side("bbbbbbbb", "shop-roundtrip", {"SOURCE:courier-agent": copied})

        unmapped = rt.run_checks(original, restored)
        self.assertFalse(all(c.passed for c in unmapped), "without the map back the names differ")

        checks = rt.run_checks(original, rt.restore_original_names(restored, rt.inbound_collection_names(original["courier"])))
        self.assertTrue(all(c.passed for c in checks), [c.to_dict() for c in checks])
        self.assertIn("courier", [c.name for c in checks])
