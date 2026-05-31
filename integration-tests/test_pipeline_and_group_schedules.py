#!/usr/bin/env python3
"""
Regression tests for pipeline-level and group-level schedule creation.

Used by:
- mysql8-vertica-chronos-integration-test (group_schedules, entity schedules)
- mysql8-vertica-chronos-TLS-integration-test (pipeline schedules)
- mysql8-vertica-chronos-INSERT-integration-test (group_schedules)
"""

import copy
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


def _node_info(default_type="varchar", gs_type="STRING"):
    return {
        "category": "RDBMS",
        "dataTypesMatrix": [
            {
                "gluesyncDataType": gs_type,
                "defaultType": default_type,
                "supportedTypes": [default_type],
            }
        ],
    }


def _source_col(name, col_id, data_type="varchar", is_pk=False, nullable=True):
    return {
        "id": col_id,
        "position": col_id,
        "name": name,
        "dataType": data_type,
        "isPK": is_pk,
        "isNullable": nullable,
        "charMaxLength": 255,
        "numPrec": 0,
        "numScale": 0,
    }


GROUP_SCHEDULES_LIST_TEMPLATE = """
demo:
  target: public
  groups:
    finance:
      id: "grp-finance"
      description: "Finance group"
  tables:
    whitelist: [DRIVERS]
    blacklist: []
    custom:
      DRIVERS:
        keys: [ID]
        groupId: "finance"
  group_schedules:
    - name: "Daily finance snapshot"
      task_type: "group_snapshot"
      group_ids: ["finance"]
      cron_expression: "0 2 * * *"
      with_snapshot: true
      snapshot_write_method: "UPSERT"
      enabled: true
"""

PIPELINE_SCHEDULES_TEMPLATE = """
demo:
  target: public
  schedules:
    - name: "Daily pipeline startup"
      task_type: "pipeline_start"
      schedule:
        days_of_week: ["monday"]
        hour: 7
        minute: 0
      enabled: true
  tables:
    whitelist: [DRIVERS]
    blacklist: []
    custom:
      DRIVERS:
        keys: [ID]
"""


class GroupSchedulesTests(unittest.TestCase):
    """Verify group_schedules with list format and group_ids resolution."""

    def test_group_schedules_list_format_with_group_ids(self):
        import create_all_entities

        yaml_config = yaml.safe_load(GROUP_SCHEDULES_LIST_TEMPLATE)
        captured_group_schedules = []
        captured_puts = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path.endswith("/config/entities") and method == "PUT":
                captured_puts.append(copy.deepcopy(body))
                return {"ok": True}
            if path.endswith("/entities"):
                return [{"entity": {"entityId": "ent-1", "entityName": "demo.DRIVERS"}}]
            return {}

        source_columns = {
            "columns": [
                _source_col("ID", 1, "int", is_pk=True),
            ]
        }

        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
             mock.patch.object(create_all_entities, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_entities, "get_agent_tables", return_value=[]), \
             mock.patch.object(create_all_entities, "get_table_columns", return_value=source_columns), \
             mock.patch.object(create_all_entities, "map_data_type", side_effect=lambda src, *a, **k: src or "varchar"), \
             mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=fake_fetch), \
             mock.patch.object(create_all_entities, "create_group", return_value="grp-finance"), \
             mock.patch.object(create_all_entities, "handle_table_creation", return_value=None), \
             mock.patch.object(create_all_entities, "assign_entities_to_group", return_value=True), \
             mock.patch("create_all_entities.handle_udf_function_definition"), \
             mock.patch("create_all_entities.create_group_schedules", side_effect=lambda *a, **k: captured_group_schedules.append(a)):
            create_all_entities.create_entities(
                token="tok",
                pipeline_id="pipe",
                source_schema="demo",
                target_schema="public",
                tables=[{"name": "DRIVERS", "schema": "demo", "id": 101}],
                source_agent_id="src",
                target_agent_id="tgt",
                source_type="SQL",
                target_type="SQL",
                yaml_config=yaml_config,
                skip_errors=False,
                chunk_size=50,
            )

        self.assertTrue(captured_group_schedules, "create_group_schedules was not called")
        _token, _pipeline_id, prepared = captured_group_schedules[0]
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0]["task_type"], "group_snapshot")
        self.assertEqual(prepared[0]["group_ids"], ["grp-finance"])
        self.assertTrue(prepared[0]["with_snapshot"])


class PipelineSchedulesTests(unittest.TestCase):
    """Verify pipeline-level schedule creation."""

    def test_pipeline_schedules_are_created(self):
        import create_all_entities

        yaml_config = yaml.safe_load(PIPELINE_SCHEDULES_TEMPLATE)
        captured_pipeline_schedules = []
        captured_puts = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path.endswith("/config/entities") and method == "PUT":
                captured_puts.append(copy.deepcopy(body))
                return {"ok": True}
            if path.endswith("/entities"):
                return [{"entity": {"entityId": "ent-1", "entityName": "demo.DRIVERS"}}]
            return {}

        source_columns = {
            "columns": [
                _source_col("ID", 1, "int", is_pk=True),
            ]
        }

        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
             mock.patch.object(create_all_entities, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_entities, "get_agent_tables", return_value=[]), \
             mock.patch.object(create_all_entities, "get_table_columns", return_value=source_columns), \
             mock.patch.object(create_all_entities, "map_data_type", side_effect=lambda src, *a, **k: src or "varchar"), \
             mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=fake_fetch), \
             mock.patch.object(create_all_entities, "create_group", return_value="grp-id"), \
             mock.patch.object(create_all_entities, "handle_table_creation", return_value=None), \
             mock.patch.object(create_all_entities, "assign_entities_to_group", return_value=True), \
             mock.patch("create_all_entities.handle_udf_function_definition"), \
             mock.patch("create_all_entities.create_pipeline_schedules", side_effect=lambda *a, **k: captured_pipeline_schedules.append(a)):
            create_all_entities.create_entities(
                token="tok",
                pipeline_id="pipe",
                source_schema="demo",
                target_schema="public",
                tables=[{"name": "DRIVERS", "schema": "demo", "id": 101}],
                source_agent_id="src",
                target_agent_id="tgt",
                source_type="SQL",
                target_type="SQL",
                yaml_config=yaml_config,
                skip_errors=False,
                chunk_size=50,
            )

        self.assertTrue(captured_pipeline_schedules, "create_pipeline_schedules was not called")
        _token, _pipeline_id, schedules = captured_pipeline_schedules[0]
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0]["task_type"], "pipeline_start")
        self.assertEqual(schedules[0]["schedule"]["days_of_week"], ["monday"])


if __name__ == "__main__":
    unittest.main()
