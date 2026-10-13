#!/usr/bin/env python3
"""A run reports the entities it could not create (BOP-18 A of the alignment plan).

``create_all_entities.main`` returned nothing, so the Automator read every run, import and duplicate
as successful: a table refused before any request, an entity CoreHub refused and a failed chunk or
MultiTable entity showed only as ERROR lines in the log. ``create_entities`` now lists each of them
under ``errors`` with its reason, ``main`` returns that result, and the Automator marks the run failed.
"""

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fake_corehub import SOURCE_AGENT, TARGET_AGENT, FakeCoreHub, run_create_entities  # noqa: E402
from test_bootstrapper_payload_golden import SOURCE_TABLES, TARGET_TABLES, _source_list  # noqa: E402

V23 = "2.3.0.0 - build test"

def single(*tables, **custom):
    """A SingleTable YAML of ``tables`` with each ``custom`` table's lines."""
    lines = ["src:", "  target: tgt", "  tables:", f"    whitelist: [{', '.join(tables)}]", "    custom:" if custom else "    custom: {}"]
    for name, body in custom.items():
        lines += [f"      {name}:"] + [f"        {line}" for line in body]
    return "\n".join(lines) + "\n"


REFUSED_KEYS = ["name: customers", "keys: [ID, NAME]"]
NOPK_KEYS = ["keys: [CODE]"]


CHAINS = """
src:
  target: tgt
  tables:
    whitelist: [ORDERS, PARTS, CUSTOMERS, NOPK]
    custom:
      ORDERS:
        chainId: refused_chain
        customProperties:
          target:
            targetOnlyColumns:
              - SYNCED_AT
      PARTS:
        chainId: refused_chain
      CUSTOMERS:
        chainId: failing_chain  # entity src.CUSTOMERS
      NOPK:
        chainId: failing_chain
        keys: [CODE]
"""


def refuse_entity(name, reason):
    def refuse(body):
        names = [entity["entityName"] for entity in body["entities"]]
        return reason if name in names else None
    return refuse


def run(yaml_text, tables, refuse=None):
    hub = FakeCoreHub(SOURCE_TABLES, TARGET_TABLES, version=V23, refuse=refuse)
    return run_create_entities(hub, yaml_text, _source_list(*tables))


def run_single(*tables, refuse=None, **custom):
    return run(single(*tables, **custom), tables, refuse=refuse)


def errors_naming(result, name):
    return [error for error in result["errors"] if name in error]


class CreateEntitiesErrorsTests(unittest.TestCase):
    def test_a_table_refused_before_any_request_is_listed_with_its_rule(self):
        result = run_single("PARTS", "CUSTOMERS", CUSTOMERS=REFUSED_KEYS)
        self.assertEqual(len(result["errors"]), 1)
        self.assertRegex(errors_naming(result, "CUSTOMERS")[0], r"R12\.1")
        self.assertEqual((result["successful"], result["failed"], result["total"]), (1, 1, 2))

    def test_an_entity_corehub_refuses_in_a_chunk_is_listed_with_corehub_reason(self):
        result = run_single("PARTS", "NOPK", refuse=refuse_entity("src.NOPK", "R12.6: the stage's line"), NOPK=NOPK_KEYS)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("R12.6: the stage's line", errors_naming(result, "src.NOPK")[0])

    def test_every_entity_of_a_failed_chunk_is_listed(self):
        result = run_single("NOPK", refuse=refuse_entity("src.NOPK", "CoreHub unavailable"), NOPK=NOPK_KEYS)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("CoreHub unavailable", errors_naming(result, "src.NOPK")[0])

    def test_a_run_without_failures_has_no_error(self):
        result = run_single("PARTS")
        self.assertEqual(result, {"successful": 1, "failed": 0, "total": 1, "errors": []})

    def test_multitable_refusals_and_failures_are_listed_and_counted(self):
        result = run(CHAINS, ["ORDERS", "PARTS", "CUSTOMERS", "NOPK"],
                     refuse=refuse_entity("src.CUSTOMERS", "MultiTable refused by CoreHub"))
        self.assertEqual(len(result["errors"]), 2)
        self.assertRegex(errors_naming(result, "refused_chain")[0], "SYNCED_AT")
        self.assertIn("MultiTable refused by CoreHub", errors_naming(result, "src.CUSTOMERS")[0])
        self.assertEqual((result["successful"], result["failed"], result["total"]), (0, 2, 2))

    def test_a_created_multitable_entity_counts_as_successful(self):
        result = run(CHAINS, ["ORDERS", "PARTS", "CUSTOMERS", "NOPK"])
        self.assertEqual((result["successful"], result["failed"], result["total"]), (1, 1, 2))


def run_main(create_entities=None, tables=("PARTS",)):
    import create_all_entities

    agents = [{"agentType": "SOURCE", "agentId": SOURCE_AGENT}, {"agentType": "TARGET", "agentId": TARGET_AGENT}]
    with mock.patch.object(create_all_entities, "get_pipeline_config", return_value={}), \
         mock.patch.object(create_all_entities, "get_pipeline_agents", return_value=agents), \
         mock.patch.object(create_all_entities, "get_agent_tables", return_value=_source_list(*tables)), \
         mock.patch.object(create_all_entities, "create_entities", side_effect=create_entities), \
         mock.patch.object(create_all_entities, "lockfile_complete"), \
         mock.patch.object(create_all_entities, "lockfile_failure"):
        return create_all_entities.main("pipeline", "src", "tgt", "SQL", "SQL", None, "token")


class MainResultTests(unittest.TestCase):
    def test_main_returns_the_result_of_create_entities(self):
        result = {"successful": 1, "failed": 1, "total": 2, "errors": ["CUSTOMERS: R12.1"]}
        self.assertEqual(run_main(lambda *args, **kwargs: dict(result)), result)

    def test_an_error_skipped_by_main_is_returned_as_an_error(self):
        result = run_main(tables=())
        self.assertEqual((result["successful"], result["failed"], result["total"]), (0, 0, 0))
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("No tables found in schema src", result["errors"][0])


class AutomatorRunTests(unittest.TestCase):
    """The Automator flows read the result ``main`` returns."""

    def run_automator(self, result):
        import automator_app.corehub as corehub
        import create_all_entities

        agents = [{"agentType": "SOURCE", "agentId": SOURCE_AGENT}, {"agentType": "TARGET", "agentId": TARGET_AGENT}]
        with mock.patch.object(corehub, "configure_core_hub"), \
             mock.patch.object(corehub, "infer_agent_schema_types", return_value=("SQL", "SQL")), \
             mock.patch.object(create_all_entities, "get_pipeline_config", return_value={}), \
             mock.patch.object(create_all_entities, "get_pipeline_agents", return_value=agents), \
             mock.patch.object(create_all_entities, "get_agent_tables", return_value=_source_list("PARTS")), \
             mock.patch.object(create_all_entities, "create_entities", return_value=result), \
             mock.patch.object(create_all_entities, "lockfile_complete"), \
             mock.patch.object(create_all_entities, "lockfile_failure"):
            return corehub.run_create_entities(
                token="tok", base_url="http://test", pipeline_id="pipe", source_schema="src", target_schema="tgt",
                source_type="SQL", target_type="SQL", yaml_file=None, skip_errors=True, chunk_size=50,
                enable_scheduling=False, create_tables=False, use_ssl=False, skip_verify=False,
            )

    def test_a_run_with_refused_entities_fails_with_their_reasons(self):
        payload = self.run_automator({"successful": 1, "failed": 1, "total": 2, "errors": ["CUSTOMERS: R12.1"]})
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"], "CUSTOMERS: R12.1")

    def test_a_run_without_failures_succeeds(self):
        payload = self.run_automator({"successful": 1, "failed": 0, "total": 1, "errors": []})
        self.assertTrue(payload["success"])

    def test_bulk_creation_names_the_refused_entities(self):
        import automator_app.corehub as corehub

        agents = [{"agentType": "SOURCE", "agentId": SOURCE_AGENT}, {"agentType": "TARGET", "agentId": TARGET_AGENT}]
        result = {"successful": 0, "failed": 1, "total": 1, "errors": ["MultiTable chain c1: SYNCED_AT has no expression"]}
        with mock.patch.object(corehub, "configure_core_hub"), \
             mock.patch.object(corehub, "infer_agent_schema_types", return_value=("SQL", "SQL")), \
             mock.patch.object(corehub, "get_pipeline_agents", return_value=agents), \
             mock.patch.object(corehub, "get_agent_tables", return_value=_source_list("PARTS")), \
             mock.patch.object(corehub, "create_entities", return_value=result):
            payload = corehub.run_create_entities_for_tables(
                token="tok", base_url="http://test", pipeline_id="pipe", source_schema="src", target_schema="tgt",
                source_type="SQL", target_type="SQL", table_names=["PARTS"], skip_errors=True, chunk_size=50,
                enable_scheduling=False, create_tables=False, use_ssl=False, skip_verify=False,
            )
        self.assertFalse(payload["success"])
        self.assertIn("MultiTable chain c1: SYNCED_AT has no expression", payload["error"])


if __name__ == "__main__":
    unittest.main()
