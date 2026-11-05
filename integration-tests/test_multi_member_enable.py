#!/usr/bin/env python3
"""IBM i YAML ``@@`` multi-member keys require source enableMultiMember (Portal B).

Synthetic fixtures only. Non-IBM ``CUSTOMERS@@2`` duplicate-entity keys must keep working.
Member-key existence is not asserted until Portal names the exact config fields.
"""

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for path in (TESTS_DIR, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import multi_member  # noqa: E402
import create_all_entities  # noqa: E402
import import_options  # noqa: E402
from fake_corehub import FakeCoreHub, SOURCE_AGENT, TARGET_AGENT, column, patched  # noqa: E402
import yaml  # noqa: E402

V23 = "2.3.0.0 - build test"

PHYSICAL = [
    column("ROW_ID", 1, "int", is_pk=True, nullable=False),
    column("PAYLOAD", 2, "varchar"),
]
SOURCE_TABLES = {("src", "CUSTOMERS"): list(PHYSICAL)}
TARGET_TABLES = {
    ("tgt", "customers"): [
        column("ROW_ID", 11, "int", is_pk=True, nullable=False),
        column("PAYLOAD", 12, "varchar"),
    ],
    ("tgt", "customers_m2"): [
        column("ROW_ID", 11, "int", is_pk=True, nullable=False),
        column("PAYLOAD", 12, "varchar"),
    ],
}


YAML_WITH_MEMBER = """
src:
  target: tgt
  tables:
    whitelist: [CUSTOMERS]
    custom:
      CUSTOMERS:
        name: customers
      CUSTOMERS@@2:
        sourceTableName: CUSTOMERS
        name: customers_m2
"""

YAML_PLAIN = """
src:
  target: tgt
  tables:
    whitelist: [CUSTOMERS]
    custom:
      CUSTOMERS:
        name: customers
"""


class MultiMemberHelperTests(unittest.TestCase):
    def test_detects_at_at_keys(self):
        keys = multi_member.yaml_multi_member_keys({"CUSTOMERS": {}, "CUSTOMERS@@2": {}})
        self.assertEqual(keys, ["CUSTOMERS@@2"])

    def test_ibm_iseries_tag(self):
        self.assertTrue(multi_member.is_ibm_iseries_agent("ibm-iseries"))
        self.assertTrue(multi_member.is_ibm_iseries_agent("IBM_ISERIES"))
        self.assertFalse(multi_member.is_ibm_iseries_agent("mssql-ct"))
        self.assertFalse(multi_member.is_ibm_iseries_agent(None))

    def test_enable_multi_member_truthy(self):
        self.assertTrue(multi_member.enable_multi_member({"enableMultiMember": True}))
        self.assertTrue(multi_member.enable_multi_member({"enableMultiMember": "true"}))
        self.assertFalse(multi_member.enable_multi_member({"enableMultiMember": False}))
        self.assertFalse(multi_member.enable_multi_member({}))
        self.assertFalse(multi_member.enable_multi_member(None))

    def test_preflight_message_ibm_without_flag(self):
        msg = multi_member.multi_member_preflight_message(
            custom_tables={"CUSTOMERS@@2": {"sourceTableName": "CUSTOMERS"}},
            source_agent_tag="ibm-iseries",
            specific_configuration={},
        )
        self.assertIsNotNone(msg)
        self.assertIn("enableMultiMember", msg)
        self.assertIn("ibm-iseries", msg)
        self.assertIn("CUSTOMERS@@2", msg)
        self.assertIn("rediscover", msg)

    def test_preflight_ok_when_flag_true(self):
        msg = multi_member.multi_member_preflight_message(
            custom_tables={"CUSTOMERS@@2": {}},
            source_agent_tag="ibm-iseries",
            specific_configuration={"enableMultiMember": True},
        )
        self.assertIsNone(msg)

    def test_preflight_skips_non_ibm(self):
        msg = multi_member.multi_member_preflight_message(
            custom_tables={"CUSTOMERS@@2": {}},
            source_agent_tag="mssql-ct",
            specific_configuration={},
        )
        self.assertIsNone(msg)

    def test_message_constant_shape(self):
        msg = multi_member.missing_enable_multi_member_message(["ORDERS@@3"])
        self.assertEqual(
            msg,
            "YAML table key(s) ORDERS@@3 use IBM i multi-member naming (`@@`), "
            "but the source agent's specificConfiguration.enableMultiMember is not true. "
            "Enable `enableMultiMember` on the source agent (ibm-iseries), rediscover the tables, "
            "then re-import.",
        )


def _agents(tag, *, enable_mm=False):
    specific = {"enableMultiMember": True} if enable_mm else {}
    return [
        {"agentId": SOURCE_AGENT, "agentType": "SOURCE", "agentTag": tag,
         "specificConfiguration": specific},
        {"agentId": TARGET_AGENT, "agentType": "TARGET", "agentTag": "mssql-ct"},
    ]


def _run(yaml_text, *, source_agent_tag=None, agents=None, version=V23):
    hub = FakeCoreHub(SOURCE_TABLES, TARGET_TABLES, version=version)
    agents_payload = agents if agents is not None else []

    def fake_agents(token, pipeline_id):
        return agents_payload

    with patched(hub):
        with mock.patch.object(create_all_entities, "get_pipeline_agents", side_effect=fake_agents):
            with import_options.use(import_options.ImportOptions(validate=True, strict_validate=False)):
                result = create_all_entities.create_entities(
                    token="token",
                    pipeline_id="pipeline",
                    source_schema="src",
                    target_schema="tgt",
                    tables=[{"name": "CUSTOMERS", "schema": "src", "id": 501}],
                    source_agent_id=SOURCE_AGENT,
                    target_agent_id=TARGET_AGENT,
                    source_type="RDBMS",
                    target_type="RDBMS",
                    yaml_config=yaml.safe_load(yaml_text),
                    skip_errors=True,
                    source_agent_tag=source_agent_tag,
                )
    return hub, result


class MultiMemberCreateEntitiesTests(unittest.TestCase):
    def test_ibm_without_flag_refuses_member_key(self):
        hub, result = _run(YAML_WITH_MEMBER, source_agent_tag="ibm-iseries",
                           agents=_agents("ibm-iseries", enable_mm=False))
        blob = " ".join(result.get("refused") or []) + " " + " ".join(result.get("errors") or [])
        self.assertIn("CUSTOMERS@@2", blob)
        self.assertIn("enableMultiMember", blob)
        put_blob = " ".join(e.get("entityName") or "" for put in hub.puts for e in put.get("entities") or [])
        self.assertNotIn("customers_m2", put_blob)

    def test_ibm_with_flag_allows_member_key(self):
        hub, result = _run(YAML_WITH_MEMBER, source_agent_tag="ibm-iseries",
                           agents=_agents("ibm-iseries", enable_mm=True))
        errors = " ".join(result.get("errors") or [])
        self.assertNotIn("enableMultiMember", errors)
        put_blob = " ".join(e.get("entityName") or "" for put in hub.puts for e in put.get("entities") or [])
        self.assertIn("customers_m2", put_blob)
        self.assertGreaterEqual(result.get("successful") or 0, 2)

    def test_mssql_duplicate_at_at_unaffected(self):
        """Regression: MSSQL CUSTOMERS@@2 must not require enableMultiMember."""
        hub, result = _run(YAML_WITH_MEMBER, source_agent_tag="mssql-ct",
                           agents=_agents("mssql-ct", enable_mm=False))
        errors = " ".join(result.get("errors") or [])
        self.assertNotIn("enableMultiMember", errors)
        put_blob = " ".join(e.get("entityName") or "" for put in hub.puts for e in put.get("entities") or [])
        self.assertIn("customers_m2", put_blob)

    def test_no_at_at_no_check(self):
        hub, result = _run(YAML_PLAIN, source_agent_tag="ibm-iseries",
                           agents=_agents("ibm-iseries", enable_mm=False))
        errors = " ".join(result.get("errors") or [])
        self.assertNotIn("enableMultiMember", errors)


if __name__ == "__main__":
    unittest.main()
