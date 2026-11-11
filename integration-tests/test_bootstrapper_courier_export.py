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
        items[body["id"]] = saved
        return saved


def source_agent(pipeline_agent_id=AGENT, courier=None):
    agent = {"agentType": "SOURCE", "agentTag": "courier-agent", "agentId": pipeline_agent_id, "customHostCredentials": {}}
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


class InboundExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hub = StatefulCoreHub()
        provision_courier_agent("jwt", PIPELINE, source_agent(courier=inbound_config(self.tmp.name)), fetch=self.hub)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_export_is_the_config_with_ids_and_without_tokens(self):
        block = export_courier_block("jwt", PIPELINE, source_agent(), fetch=self.hub)

        self.assertNotIn("tokens", block)
        shop, billing = block["collections"]
        self.assertEqual((shop["name"], shop["basePath"], shop["allowlist"]), ("shop", "shop", True))
        self.assertEqual(shop["allowedTokenIds"], ["token-1"])
        self.assertEqual((billing["name"], billing["allowlist"]), ("billing", False))
        self.assertEqual(billing["allowedTokenIds"], ["token-2"], "a token joins the list of its collections, on or off")
        orders = shop["endpoints"][0]
        self.assertEqual(orders["path"], "/orders/{id}")
        self.assertEqual(orders["operationBinding"], "op")
        self.assertEqual(orders["bindings"][1], {"name": "op", "location": "QUERY", "type": "STRING", "required": False,
                                                 "defaultValue": "Insert"})
        self.assertEqual(orders["bindings"][0], {"name": "id", "location": "PATH", "type": "NUMBER"})
        self.assertTrue(shop["id"] and orders["id"])

    def test_only_the_endpoints_of_the_agent_are_exported(self):
        other = dict(next(iter(self.hub.store[INBOUND_ENDPOINTS_PATH].values())), id="sql-1", name="sql",
                     pipelineId=PIPELINE, agentId="postgres-1")
        self.hub.store[INBOUND_ENDPOINTS_PATH]["sql-1"] = other
        self.hub.store[INBOUND_COLLECTIONS_PATH]["lonely"] = {"id": "lonely", "name": "lonely", "basePath": "lonely"}

        block = export_courier_block("jwt", PIPELINE, source_agent(), fetch=self.hub)

        self.assertEqual([c["name"] for c in block["collections"]], ["shop", "billing"])
        self.assertNotIn("sql", [e["name"] for c in block["collections"] for e in c["endpoints"]])
        self.assertIsNone(export_courier_block("jwt", "another-pipeline", source_agent(), fetch=self.hub))

    def test_a_restore_onto_the_pipeline_it_was_exported_from_gives_the_same_export_and_creates_no_token(self):
        exported = export_courier_block("jwt", PIPELINE, source_agent(), fetch=self.hub)

        provision_courier_agent("jwt", PIPELINE, source_agent(courier=copy.deepcopy(exported)), fetch=self.hub,
                                keep_other_pipelines=True)

        self.assertEqual(export_courier_block("jwt", PIPELINE, source_agent(), fetch=self.hub), exported)
        self.assertEqual(len(self.hub.tokens), 2, "the restore creates no token and revokes none")
        self.assertEqual(len(self.hub.store[INBOUND_COLLECTIONS_PATH]), 2, "the same collections are updated in place")

    def test_a_restore_into_another_pipeline_never_takes_the_endpoints_of_a_live_one(self):
        exported = export_courier_block("jwt", PIPELINE, source_agent(), fetch=self.hub)
        writes_before = len(self.hub.writes)
        restored = source_agent(RESTORED_AGENT, courier=copy.deepcopy(exported))

        with self.assertRaises(CourierConfigError) as raised:
            provision_courier_agent("jwt", RESTORED_PIPELINE, restored, fetch=self.hub, keep_other_pipelines=True)

        self.assertIn("'shop/orders' (pipeline p1)", str(raised.exception))
        self.assertEqual(len(self.hub.writes), writes_before, "refused before anything is written")
        self.assertEqual(export_courier_block("jwt", PIPELINE, source_agent(), fetch=self.hub), exported,
                         "the original pipeline keeps its endpoints")

    def test_a_restore_takes_the_endpoints_of_a_deleted_pipeline_or_of_a_live_one_with_force(self):
        exported = export_courier_block("jwt", PIPELINE, source_agent(), fetch=self.hub)
        self.hub.pipelines = [{"pipelineId": RESTORED_PIPELINE}]

        provision_courier_agent("jwt", RESTORED_PIPELINE, source_agent(RESTORED_AGENT, courier=copy.deepcopy(exported)),
                                fetch=self.hub, keep_other_pipelines=True)
        self.assertIsNone(export_courier_block("jwt", PIPELINE, source_agent(), fetch=self.hub))

        self.hub.pipelines = [{"pipelineId": PIPELINE}, {"pipelineId": RESTORED_PIPELINE}]
        back = copy.deepcopy(exported)
        back["force"] = True
        provision_courier_agent("jwt", PIPELINE, source_agent(courier=back), fetch=self.hub, keep_other_pipelines=True)
        self.assertEqual(export_courier_block("jwt", PIPELINE, source_agent(), fetch=self.hub), exported)

    def test_a_bootstrapper_run_moves_the_endpoints_to_its_new_pipeline_and_says_so(self):
        exported = export_courier_block("jwt", PIPELINE, source_agent(), fetch=self.hub)

        log = mock.Mock()
        provision_courier_agent("jwt", RESTORED_PIPELINE, source_agent(RESTORED_AGENT, courier=copy.deepcopy(exported)),
                                fetch=self.hub, logger=log)

        moved = [c.args for c in log.warning.call_args_list if "moves from pipeline" in c.args[0]]
        self.assertEqual([(a[1], a[2], a[3]) for a in moved], [("shop", "orders", PIPELINE), ("billing", "invoices", PIPELINE)])
        self.assertEqual(export_courier_block("jwt", RESTORED_PIPELINE, source_agent(RESTORED_AGENT), fetch=self.hub), exported)

    def test_an_endpoint_renamed_in_the_ui_is_found_by_the_id_of_the_block(self):
        exported = export_courier_block("jwt", PIPELINE, source_agent(), fetch=self.hub)
        orders_id = exported["collections"][0]["endpoints"][0]["id"]
        self.hub.store[INBOUND_ENDPOINTS_PATH][orders_id]["name"] = "orders-renamed"
        restored = source_agent(RESTORED_AGENT, courier=copy.deepcopy(exported))

        with self.assertRaises(CourierConfigError):
            provision_courier_agent("jwt", RESTORED_PIPELINE, restored, fetch=self.hub, keep_other_pipelines=True)
        self.assertEqual(self.hub.store[INBOUND_ENDPOINTS_PATH][orders_id]["pipelineId"], PIPELINE)


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


class RoundTripCourierTests(unittest.TestCase):
    def test_the_restore_config_takes_the_exported_blocks_without_the_kit_tokens(self):
        kit = {"agents": [
            source_agent(courier={"collections": [{"name": "shop"}], "tokens": [{"name": "t"}]}),
            target_agent(courier={"collections": [{"name": "kit-only"}]}),
            {"agentType": "TARGET", "agentTag": "postgresql-cdc", "hostCredentials": {"certificatePath": "certs/ca.pem"}},
        ]}
        exported = {agent_key(source_agent()): {"collections": [{"id": "c1", "name": "shop"}]}}

        restored = rt.courier_restore_config(kit, exported, Path("/kit"))

        self.assertEqual(restored["agents"][0]["courier"], {"collections": [{"id": "c1", "name": "shop"}]})
        self.assertNotIn("courier", restored["agents"][1], "nothing exported for it: nothing restored")
        self.assertEqual(restored["agents"][2]["hostCredentials"]["certificatePath"], "/kit/certs/ca.pem")
        self.assertIn("tokens", kit["agents"][0]["courier"], "the kit config is left untouched")

    def test_a_pipeline_with_an_http_source_is_skipped_before_the_restore(self):
        blocks = {agent_key(source_agent()): {"collections": []}}
        with self.assertRaises(rt.RoundTripSkipped) as raised:
            rt.refuse_http_source_round_trip(blocks, PIPELINE)
        self.assertIn("SOURCE:courier-agent", str(raised.exception))
        rt.refuse_http_source_round_trip({agent_key(target_agent()): {"collections": []}}, PIPELINE)
        rt.refuse_http_source_round_trip({}, PIPELINE)

    def test_a_skipped_round_trip_passes_and_says_so_in_junit(self):
        with tempfile.TemporaryDirectory() as out, \
                mock.patch.object(rt, "export_and_restore", side_effect=rt.RoundTripSkipped("an HTTP Source")):
            code = rt.run(PIPELINE, Path(out), "token.json", 10, "suite")
            junit = (Path(out) / "junit.xml").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn('<skipped message="an HTTP Source"/>', junit)
        with tempfile.TemporaryDirectory() as out, \
                mock.patch.object(rt, "export_and_restore", side_effect=rt.RoundTripError("boom")):
            self.assertEqual(rt.run(PIPELINE, Path(out), "token.json", 10, "suite"), 1)

    def test_the_courier_check(self):
        block = {"SOURCE:courier-agent": {"collections": [{"name": "shop"}]}}
        self.assertTrue(rt.check_courier({"courier": block}, {"courier": copy.deepcopy(block)}).passed)
        failed = rt.check_courier({"courier": block}, {"courier": {}})
        self.assertFalse(failed.passed)
        self.assertTrue(failed.diff)
        names = [c.name for c in rt.run_checks(
            {"courier": block, "entities": [], "groups": [], "jobs": [], "export": "", "idMap": {}},
            {"courier": block, "entities": [], "groups": [], "jobs": [], "export": "", "idMap": {}})]
        self.assertIn("courier", names)


if __name__ == "__main__":
    unittest.main()
