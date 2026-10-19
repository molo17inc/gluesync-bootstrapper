#!/usr/bin/env python3
"""A restore must not read the columns of an entity back from the target it writes to.

Found by the Automator round trip (mssql-couchbase-collections-CUSTOM-DOCUMENT-ID, mssql-cdc-couchbase-collections,
mongo8.0.3-couchbase-two-ways): the restore ran against a Couchbase collection the original pipeline had already
filled. Couchbase infers the columns of a collection from its documents, so discovery reported

- an empty dataType for a field that is always null (``Shape``, a SQL Server geometry): the restored entity then
  carried ``"dataType": null`` and CoreHub answered 400 to the whole PUT;
- ``number`` for an int and a long alike, ``string`` for a datetime, and every field as not nullable,
  where the original entity had the source column's type, nullability, collation and lengths.

The fixtures keep the shape of those discoveries and of the exported rows.
"""

import logging
import sys
import unittest
from pathlib import Path

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import fake_corehub  # noqa: E402
from fake_corehub import FakeCoreHub, column, run_create_entities  # noqa: E402

SOURCE = {("dbo", "TEST"): [
    column("ID", 10, "int", is_pk=True, nullable=False, numPrec=10, numPrecRadix=10, charMaxLength=0),
    column("Name", 11, "varchar", collation="SQL_Latin1_General_CP1_CI_AS", charSet="iso_1", charMaxLength=1000),
    column("Shape", 12, "geometry", charMaxLength=-1),
    column("Bint", 13, "bigint", numPrec=19),
    column("Ts", 14, "datetime2", datetimePrec=7),
]}

# What Couchbase reports for the collection the original pipeline wrote: inferred from the documents.
INFERRED = {("dbo", "TEST"): [
    column("ID", 10, "number", is_pk=True, nullable=False),
    column("Name", 11, "string", nullable=False),
    column("Shape", 12, "", nullable=True),
    column("Bint", 13, "number", nullable=False),
    column("Ts", 14, "string", nullable=False),
]}

EXPORTED_ROWS = """
dbo:
  target: dbo
  tables:
    whitelist: [TEST]
    custom:
      TEST:
        keys: [ID]
        columns:
          - {source: ID, target: ID, type: int, isNullable: false, isPK: true}
          - {source: Name, target: Name, type: string, isNullable: true}
          - {source: Shape, target: Shape, type: string, isNullable: true}
          - {source: Bint, target: Bint, type: long, isNullable: true}
          - {source: Ts, target: Ts, type: datetime, isNullable: true}
"""


class _HubWithMatrix(FakeCoreHub):
    """A target that advertises its types, as the Couchbase agent does."""

    def node_info(self, token, pipeline_id, agent_id):
        info = super().node_info(token, pipeline_id, agent_id)
        if agent_id == fake_corehub.TARGET_AGENT:
            info["dataTypesMatrix"] = [
                {"gluesyncDataType": "STRING", "defaultType": "string", "supportedTypes": []},
                {"gluesyncDataType": "INT", "defaultType": "int", "supportedTypes": []},
                {"gluesyncDataType": "LONG", "defaultType": "long", "supportedTypes": []},
                {"gluesyncDataType": "DATETIME", "defaultType": "datetime", "supportedTypes": []},
            ]
        return info


def restore(yaml_text, target_tables, target_type, hub_class=FakeCoreHub):
    hub = hub_class(SOURCE, target_tables, version="2.3.0.2 - build test", target_category=target_type)
    source_list = [{"name": "TEST", "schema": "dbo", "id": 500}]
    result = run_create_entities(hub, yaml_text, source_list, source_schema="dbo", target_schema="dbo",
                                 target_type=target_type)
    entities = {e["entityName"]: e for put in hub.puts for e in put["entities"]}
    return result, entities["dbo.TEST"]["agentEntities"][1]


def by_name(target):
    return {c["name"]: c for c in target["columns"]}


class NoSqlTargetTests(unittest.TestCase):
    def test_a_field_the_target_infers_without_a_type_never_becomes_a_null_datatype(self):
        result, target = restore(EXPORTED_ROWS, INFERRED, "NoSQL")
        self.assertEqual(result["failed"], 0, result.get("errors"))
        for col in target["columns"]:
            self.assertTrue(col["dataType"], f"{col['name']} has no dataType")
        self.assertEqual(by_name(target)["Shape"]["dataType"], "geometry")  # the (stubbed) mapping of the source type

    def test_columns_come_from_the_source_not_from_what_the_collection_infers(self):
        _, target = restore(EXPORTED_ROWS, INFERRED, "NoSQL")
        cols = by_name(target)
        self.assertEqual(cols["ID"]["dataType"], "int")
        self.assertEqual(cols["Bint"]["dataType"], "bigint")
        self.assertEqual(cols["Ts"]["dataType"], "datetime2")
        self.assertEqual([cols[n]["isNullable"] for n in ("ID", "Name", "Bint", "Ts")], [False, True, True, True])
        # the source column's own metadata stays on the target column
        self.assertEqual((cols["Name"]["collation"], cols["Name"]["charSet"], cols["Name"]["charMaxLength"]),
                         ("SQL_Latin1_General_CP1_CI_AS", "iso_1", 1000))
        self.assertEqual((cols["ID"]["numPrec"], cols["ID"]["numPrecRadix"]), (10, 10))
        self.assertEqual(cols["Ts"]["datetimePrec"], 7)

    def test_it_equals_the_entity_built_before_the_collection_existed(self):
        _, fresh = restore(EXPORTED_ROWS, {}, "NoSQL")
        _, existing = restore(EXPORTED_ROWS, INFERRED, "NoSQL")
        strip = lambda t: [{k: v for k, v in c.items() if k != "tableId"} for c in t["columns"]]  # noqa: E731
        self.assertEqual(strip(existing), strip(fresh))
        pairs = lambda t: [(e["sourceColumnIds"], e["targetColumnIds"])  # noqa: E731
                           for e in t["entityType"]["columnsMappingMatrix"]]
        self.assertEqual(pairs(existing), pairs(fresh))  # the table ids are raw ids, which differ

    def test_discovery_still_decides_the_column_ids(self):
        inferred = {("dbo", "TEST"): [dict(c, id=c["id"] + 1000) for c in INFERRED[("dbo", "TEST")]]}
        _, target = restore(EXPORTED_ROWS, inferred, "NoSQL")
        self.assertEqual({c["name"]: c["id"] for c in target["columns"]},
                         {"ID": 1010, "Name": 1011, "Shape": 1012, "Bint": 1013, "Ts": 1014})

    def test_an_exported_target_type_the_target_advertises_is_restored(self):
        _, target = restore(EXPORTED_ROWS, INFERRED, "NoSQL", hub_class=_HubWithMatrix)
        cols = by_name(target)
        self.assertEqual([cols[n]["dataType"] for n in ("ID", "Name", "Shape", "Bint", "Ts")],
                         ["int", "string", "string", "long", "datetime"])

    def test_an_exported_type_the_target_does_not_advertise_falls_back_to_the_mapping(self):
        rows = EXPORTED_ROWS.replace("type: long", "type: bogus")
        _, target = restore(rows, INFERRED, "NoSQL", hub_class=_HubWithMatrix)
        self.assertEqual(by_name(target)["Bint"]["dataType"], "bigint")


class SqlTargetTests(unittest.TestCase):
    """A SQL target is a schema, so the column it reports stays the truth, but never as a null type."""

    TABLE = {("dbo", "TEST"): [
        column("ID", 10, "int", is_pk=True, nullable=False),
        column("Name", 11, "nvarchar", nullable=False),
        column("Shape", 12, ""),
        column("Bint", 13, "bigint"),
        column("Ts", 14, "datetime2"),
    ]}

    def test_the_reported_column_decides_and_an_empty_type_falls_back_to_the_mapping(self):
        result, target = restore(EXPORTED_ROWS, self.TABLE, "RDBMS")
        self.assertEqual(result["failed"], 0, result.get("errors"))
        cols = by_name(target)
        self.assertEqual((cols["Name"]["dataType"], cols["Name"]["isNullable"]), ("nvarchar", False))
        self.assertEqual(cols["Shape"]["dataType"], "geometry")
        for col in target["columns"]:
            self.assertTrue(col["dataType"])


if __name__ == "__main__":
    unittest.main()
