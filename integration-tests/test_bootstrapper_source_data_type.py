#!/usr/bin/env python3
"""
Regression tests for the ``sourceDataType`` of an exported row.

A restore builds the source columns from live discovery. A source with no schema (ArangoDB, CouchDB, MongoDB,
Neo4j...) infers the type of a column from a sample of its data, so a restore after the data changed re-infers
another one: ``w`` was ``number`` (a 1.5 existed) and came back ``integer``, which reads a later 1.5 as null. A
column retyped by hand (to ``json``) lost its type on every restore, SQL sources included.

The export therefore writes the dataType the entity stores for the source column (``sourceDataType``), and the
restore puts it back when the source agent knows the type, as it does for ``charSet``. A row without the key (an older
backup) restores from discovery, as it always did.

Used by:
- pgsqlwal-arangodb-integration-test (Automator round trip, ArangoDB -> ArangoDB)
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


def _source_node_info():
    """A schemaless source: number, integer, string and json are the types it knows."""
    return {
        "category": "NoSQL",
        "dataTypesMatrix": [
            {"gluesyncDataType": "DOUBLE", "defaultType": "number", "supportedTypes": ["number"]},
            {"gluesyncDataType": "LONG", "defaultType": "integer", "supportedTypes": ["integer"]},
            {"gluesyncDataType": "STRING", "defaultType": "string", "supportedTypes": ["string"]},
            {"gluesyncDataType": "JSON", "defaultType": "json", "supportedTypes": ["json"]},
        ],
    }


def _target_node_info():
    return {
        "category": "NoSQL",
        "dataTypesMatrix": [
            {"gluesyncDataType": "DOUBLE", "defaultType": "number", "supportedTypes": ["number"]},
            {"gluesyncDataType": "LONG", "defaultType": "integer", "supportedTypes": ["integer"]},
            {"gluesyncDataType": "STRING", "defaultType": "string", "supportedTypes": ["string"]},
            {"gluesyncDataType": "JSON", "defaultType": "json", "supportedTypes": ["json"]},
        ],
    }


def _col(name, col_id, data_type, is_pk=False):
    return {"id": col_id, "position": col_id, "name": name, "dataType": data_type, "isPK": is_pk,
            "isNullable": True, "charMaxLength": 0}


def _backup_entity(w_type="number"):
    """The entity of the ArangoDB -> ArangoDB pipeline: ``w`` holds a 1.5 somewhere, so it is a number."""
    source_columns = [_col("_key", 1, "string", True), _col("w", 2, w_type), _col("n", 3, "integer")]
    target_columns = [_col("_key", 1, "string", True), _col("w", 2, "number"), _col("n", 3, "integer")]
    matrix = [{"sourceColumnId": i, "targetColumnId": i} for i in (1, 2, 3)]
    return {
        "entityId": "ent-docs",
        "entityName": "db.docs",
        "groupId": "",
        "agentEntities": [
            {
                "type": "SingleTable", "agentId": "src-agent", "entityType": {"type": "Source"},
                "entityObject": {"id": "101", "schema": "db", "collection": "docs"},
                "table": {"id": "101", "name": "docs", "schema": "db"},
                "columns": source_columns,
                "keys": [copy.deepcopy(source_columns[0])],
            },
            {
                "type": "SingleTable", "agentId": "tgt-agent",
                "entityType": {"type": "Target", "allowedOperations": ["INSERT"], "columnsMappingMatrix": matrix},
                "entityObject": {"id": "201", "schema": "db", "collection": "docs"},
                "table": {"id": "201", "name": "docs", "schema": "db"},
                "columns": target_columns,
            },
        ],
    }


def _export_yaml(entity):
    """Run the export the way a backup does, down to the YAML text and back."""
    from export_template_from_corehub import build_schemas_from_entities, build_yaml_structure

    structure = build_yaml_structure(build_schemas_from_entities([entity], {}), {})
    return yaml.safe_load(yaml.safe_dump(structure, allow_unicode=True, sort_keys=False))


def _fake_fetch_factory(captured_puts):
    def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
        if path.endswith("/config/entities") and method == "PUT":
            captured_puts.append(copy.deepcopy(body))
            return {"ok": True}
        if path.endswith("/entities"):
            entities = captured_puts[-1]["entities"] if captured_puts else []
            return [{"entity": {"entityId": f"entity-{e['entityName']}", **e}} for e in entities]
        return {}

    return fake_fetch


def _run_create_entities(yaml_config, captured_puts, get_columns, tables, source_node_info=None):
    import create_all_entities

    source_node_info = _source_node_info() if source_node_info is None else source_node_info
    with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
         mock.patch.object(create_all_entities, "get_node_info",
                           side_effect=[source_node_info, _target_node_info()]), \
         mock.patch.object(create_all_entities, "get_agent_tables", return_value=[]), \
         mock.patch.object(create_all_entities, "get_table_columns", side_effect=get_columns), \
         mock.patch.object(create_all_entities, "map_data_type", side_effect=lambda src, *a, **k: src or "string"), \
         mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=_fake_fetch_factory(captured_puts)), \
         mock.patch.object(create_all_entities, "create_group", return_value="grp-id"), \
         mock.patch.object(create_all_entities, "handle_table_creation", return_value=None), \
         mock.patch.object(create_all_entities, "assign_entities_to_group", return_value=True), \
         mock.patch("create_all_entities.handle_udf_function_definition"):
        create_all_entities.create_entities(
            token="tok", pipeline_id="pipe", source_schema="db", target_schema="db", tables=tables,
            source_agent_id="src", target_agent_id="tgt", source_type="NoSQL", target_type="NoSQL",
            yaml_config=yaml_config, skip_errors=False, chunk_size=50,
        )


def _discovered(w_type="integer"):
    return {"columns": [_col("_key", 1, "string", True), _col("w", 2, w_type), _col("n", 3, "integer")]}


def _restore(yaml_config, discovered, source_node_info=None):
    captured = []
    _run_create_entities(yaml_config, captured, lambda *a, **k: copy.deepcopy(discovered),
                         [{"name": "docs", "schema": "db", "id": 101}], source_node_info)
    return captured[0]["entities"][0]["agentEntities"][0]


def _types(source_ae):
    return {c["name"]: c["dataType"] for c in source_ae["columns"]}


def _template(columns, keys="[_key]"):
    return yaml.safe_load(f"""
db:
  target: db
  tables:
    whitelist: [docs]
    blacklist: []
    custom:
      docs:
        keys: {keys}
        columns: {columns}
""")


class ExportWritesSourceDataTypeTests(unittest.TestCase):

    def test_mapped_rows_carry_the_type_of_the_source_column(self):
        columns = _export_yaml(_backup_entity())["db"]["tables"]["custom"]["docs"]["columns"]
        by_source = {c["source"]: c for c in columns}
        self.assertEqual(by_source["w"]["sourceDataType"], "number")
        self.assertEqual(by_source["n"]["sourceDataType"], "integer")

    def test_it_is_the_type_of_the_source_column_not_the_one_of_the_target_column(self):
        entity = _backup_entity(w_type="json")  # retyped by hand to json, the target column stays a number
        by_source = {c["source"]: c for c in _export_yaml(entity)["db"]["tables"]["custom"]["docs"]["columns"]}
        self.assertEqual(by_source["w"]["sourceDataType"], "json")
        self.assertEqual(by_source["w"]["type"], "number")

    def test_the_key_column_row_carries_it(self):
        by_source = {c["source"]: c for c in _export_yaml(_backup_entity())["db"]["tables"]["custom"]["docs"]["columns"]}
        self.assertTrue(by_source["_key"]["isPK"])
        self.assertEqual(by_source["_key"]["sourceDataType"], "string")

    def test_whitelist_rows_carry_it(self):
        from export_template_from_corehub import _multitable_column_metadata

        rows = _multitable_column_metadata([_col("_key", 1, "string", True), _col("w", 2, "number")])
        self.assertEqual({r["name"]: r["sourceDataType"] for r in rows}, {"_key": "string", "w": "number"})

    def test_a_column_without_a_type_writes_null(self):
        from export_template_from_corehub import _source_column_fields

        fields = _source_column_fields({"name": "x"}, 1)
        self.assertIn("sourceDataType", fields)
        self.assertIsNone(fields["sourceDataType"])

    def test_legacy_type_field_is_read_as_the_entity_does(self):
        from export_template_from_corehub import _source_column_fields

        self.assertEqual(_source_column_fields({"name": "x", "type": "number"}, 1)["sourceDataType"], "number")


class RestoreUsesSourceDataTypeTests(unittest.TestCase):

    def test_the_exported_type_wins_over_the_discovered_one(self):
        template = _template("""
          - {source: _key, target: _key, type: string, sourceDataType: string}
          - {source: w, target: w, type: number, sourceDataType: number}
          - {source: n, target: n, type: integer, sourceDataType: integer}""")
        types = _types(_restore(template, _discovered("integer")))
        self.assertEqual(types["w"], "number")
        self.assertEqual(types["n"], "integer")

    def test_a_key_column_gets_it(self):
        template = _template("""
          - {source: _key, target: _key, type: string, sourceDataType: json}
          - {source: w, target: w, type: number}""")
        self.assertEqual(_types(_restore(template, _discovered()))["_key"], "json")

    def test_whitelist_rows_use_it_too(self):
        template = _template("""
          - {name: _key, sourceName: _key, type: string}
          - {name: w, sourceName: w, type: number, sourceDataType: number}""")
        types = _types(_restore(template, _discovered("integer")))
        self.assertEqual(types["w"], "number")

    def test_no_mapping_rows_for_a_column_it_does_not_name_keeps_discovery(self):
        template = _template("""
          - {source: _key, target: _key, type: string, sourceDataType: string}
          - {source: w, target: w, type: number, sourceDataType: number}
          - {source: n, target: n, type: integer}""")
        types = _types(_restore(template, _discovered("integer")))
        self.assertEqual(types["n"], "integer")

    def test_a_backup_without_the_key_restores_from_discovery(self):
        template = _template("""
          - {source: _key, target: _key, type: string}
          - {source: w, target: w, type: number}
          - {source: n, target: n, type: integer}""")
        types = _types(_restore(template, _discovered("integer")))
        self.assertEqual(types["w"], "integer")

    def test_a_null_value_restores_from_discovery(self):
        template = _template("""
          - {source: _key, target: _key, type: string}
          - {source: w, target: w, type: number, sourceDataType: null}""")
        self.assertEqual(_types(_restore(template, _discovered("integer")))["w"], "integer")

    def test_a_type_the_source_does_not_know_warns_and_keeps_discovery(self):
        import create_all_entities

        template = _template("""
          - {source: _key, target: _key, type: string}
          - {source: w, target: w, type: number, sourceDataType: decimal128}""")
        with mock.patch.object(create_all_entities.logger, "warning") as warn:
            types = _types(_restore(template, _discovered("integer")))
        self.assertEqual(types["w"], "integer")
        messages = [str(c.args[0]) for c in warn.call_args_list]
        self.assertTrue(any("decimal128" in m and "'w'" in m and "docs" in m for m in messages), messages)

    def test_a_different_type_is_logged_with_both(self):
        import create_all_entities

        template = _template("""
          - {source: _key, target: _key, type: string}
          - {source: w, target: w, type: number, sourceDataType: number}""")
        with mock.patch.object(create_all_entities.logger, "info") as info:
            _restore(template, _discovered("integer"))
        messages = [str(c.args[0]) for c in info.call_args_list]
        self.assertTrue(any("'w'" in m and "number" in m and "integer" in m and "docs" in m for m in messages), messages)

    def test_a_source_without_a_type_matrix_takes_the_exported_type(self):
        template = _template("""
          - {source: _key, target: _key, type: string}
          - {source: w, target: w, type: number, sourceDataType: number}""")
        types = _types(_restore(template, _discovered("integer"), source_node_info={}))
        self.assertEqual(types["w"], "number")

    def test_the_type_is_not_read_as_a_column_name(self):
        from create_all_entities import _EXPORT_METADATA_FIELDS

        self.assertIn("sourceDataType", _EXPORT_METADATA_FIELDS)

    def test_the_type_does_not_reach_the_target_column(self):
        template = _template("""
          - {source: _key, target: _key, type: string}
          - {source: w, target: w, type: integer, sourceDataType: json}""")
        captured = []
        _run_create_entities(template, captured, lambda *a, **k: copy.deepcopy(_discovered()),
                             [{"name": "docs", "schema": "db", "id": 101}])
        target_ae = captured[0]["entities"][0]["agentEntities"][1]
        self.assertNotEqual({c["name"]: c["dataType"] for c in target_ae["columns"]}["w"], "json")


class ChainRestoreTests(unittest.TestCase):

    def test_a_chain_component_uses_it(self):
        template = yaml.safe_load("""
db:
  target: db
  tables:
    whitelist: [docs, docs2]
    blacklist: []
    custom:
      docs:
        chainId: c1
        orderIndex: 1
        keys: [_key]
        columns:
          - {source: w, target: w, type: number, sourceDataType: number}
      docs2:
        chainId: c1
        orderIndex: 2
        keys: [_key]
        columns:
          - _key: _key
          - w: w
""")
        captured = []
        _run_create_entities(template, captured, lambda *a, **k: copy.deepcopy(_discovered("integer")),
                             [{"name": "docs", "schema": "db", "id": 101}, {"name": "docs2", "schema": "db", "id": 102}])
        multi = next(e for e in captured[0]["entities"] if e["agentEntities"][0].get("type") == "MultiTable")
        by_header, header = {}, None
        for item in multi["agentEntities"][0]["columns"]:
            if isinstance(item, dict):
                header = item.get("name")
            else:
                by_header[header] = {c["name"]: c["dataType"] for c in item}
        self.assertEqual(by_header["docs"]["w"], "number")
        self.assertEqual(by_header["docs2"]["w"], "integer")


class ArangoDbRoundTripTests(unittest.TestCase):

    def test_a_number_exported_comes_back_a_number_when_discovery_now_says_integer(self):
        """The ArangoDB case: the 1.5 that made ``w`` a number is gone from the sample, discovery says integer."""
        exported = _export_yaml(_backup_entity(w_type="number"))
        source_ae = _restore(exported, _discovered(w_type="integer"))
        self.assertEqual(_types(source_ae)["w"], "number")

    def test_a_backup_written_before_the_key_existed_comes_back_as_discovery_says(self):
        exported = _export_yaml(_backup_entity(w_type="number"))
        for row in exported["db"]["tables"]["custom"]["docs"]["columns"]:
            row.pop("sourceDataType")
        self.assertEqual(_types(_restore(exported, _discovered(w_type="integer")))["w"], "integer")


if __name__ == "__main__":
    unittest.main()
