# Copyright (c) 2025 MOLO17
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Tests for:
- PK fallback: when the source has no primary keys the converter should derive
  them from the target table instead of dumping all columns.
- Expression columns: field mappings that have a SrcExpression but no
  SrcFieldID (type="expression") must appear in the generated column list.
"""

import os
import shutil
import sys
import tempfile
import textwrap
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parse_dbmoto_metadata_xml as parser


# ---------------------------------------------------------------------------
# Minimal XML factory
# ---------------------------------------------------------------------------

def _build_xml(
    src_pks: dict,       # {field_name: pk_pos}  – pos 0 means "not a PK"
    tgt_pks: dict,       # {field_name: pk_pos}
    extra_field_mappings: str = "",   # raw <DBMMFieldMappings> elements
) -> str:
    """Return a minimal but valid DbMoto metadata XML string.

    Topology:
      connection 1  (AS400_source, IsSource=Y)
        schema 1  (DEMO)
          table 1  (MYTABLE)  – source fields: COLA, COLB, CHCO, CHONO

      connection 2  (SqlServer_target, IsSource=N)
        schema 2  (dbo)
          table 2  (MYTABLE)  – target fields mirror source + optional extras

    Replication 1: src=table1 → trg=table2
    Field mappings: COLA→COLA, COLB→COLB, CHCO→CHCO, CHONO→CHONO (direct, 1:1)
    Any extra mappings (e.g. expression) are injected via extra_field_mappings.
    """

    def _src_field(fid, name, ordinal, pk_pos=0):
        return f"""
        <DBMMFields>
          <FieldID>{fid}</FieldID><TableID>1</TableID>
          <Name>{name}</Name><Ordinal>{ordinal}</Ordinal>
          <Type>CHAR</Type><InternalType>9</InternalType>
          <Size>10</Size><Precision>0</Precision><Scale>0</Scale>
          <IsUnsigned>N</IsUnsigned><Ccsid>37</Ccsid>
          <AllowNull>N</AllowNull>
          <PrimaryKeyPos>{pk_pos}</PrimaryKeyPos>
          <IsAutoIncrement>N</IsAutoIncrement>
        </DBMMFields>"""

    def _tgt_field(fid, name, ordinal, pk_pos=0, ftype="CHAR", size=10):
        return f"""
        <DBMMFields>
          <FieldID>{fid}</FieldID><TableID>2</TableID>
          <Name>{name}</Name><Ordinal>{ordinal}</Ordinal>
          <Type>{ftype}</Type><InternalType>9</InternalType>
          <Size>{size}</Size><Precision>0</Precision><Scale>0</Scale>
          <IsUnsigned>N</IsUnsigned><Ccsid>0</Ccsid>
          <AllowNull>N</AllowNull>
          <PrimaryKeyPos>{pk_pos}</PrimaryKeyPos>
          <IsAutoIncrement>N</IsAutoIncrement>
        </DBMMFields>"""

    # Source fields: IDs 1-4
    src_field_defs = [
        ("COLA",  1, 1),
        ("COLB",  2, 2),
        ("CHCO",  3, 3),
        ("CHONO", 4, 4),
    ]
    # Target fields: IDs 11-14 (same names, 1:1 mapping)
    tgt_field_defs = [
        ("COLA",  11, 1),
        ("COLB",  12, 2),
        ("CHCO",  13, 3),
        ("CHONO", 14, 4),
    ]

    src_fields_xml = "".join(
        _src_field(fid, name, ord_, src_pks.get(name, 0))
        for name, fid, ord_ in src_field_defs
    )
    tgt_fields_xml = "".join(
        _tgt_field(fid, name, ord_, tgt_pks.get(name, 0))
        for name, fid, ord_ in tgt_field_defs
    )

    # Direct field mappings (src field id → tgt field id)
    direct_mappings = "".join(f"""
        <DBMMFieldMappings>
          <FieldMappingID>{100 + i}</FieldMappingID>
          <ReplicationID>1</ReplicationID>
          <SrcFieldID>{sfid}</SrcFieldID>
          <TrgFieldID>{tfid}</TrgFieldID>
          <IsForth>Y</IsForth>
        </DBMMFieldMappings>"""
        for i, (sfid, tfid) in enumerate([(1, 11), (2, 12), (3, 13), (4, 14)])
    )

    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="utf-8"?>
        <metadata version="10.7.1">
          <tables>
            <DBMMConnections>
              <ConnectionID>1</ConnectionID><UUID>src-uuid</UUID>
              <Timestamp>1</Timestamp><Name>AS400_source</Name>
              <Type>3</Type><IsSource>Y</IsSource>
              <ConnectParams>connectiontype=DotNet;datasourcename=IBM.DB2.i;</ConnectParams>
              <ConnectString></ConnectString><StgConnectParams></StgConnectParams>
              <StgConnectString /><Properties></Properties>
              <CreatedBy>test</CreatedBy><CreatedAt>1</CreatedAt>
              <ModifiedBy>test</ModifiedBy><ModifiedAt>1</ModifiedAt>
            </DBMMConnections>
            <DBMMConnections>
              <ConnectionID>2</ConnectionID><UUID>tgt-uuid</UUID>
              <Timestamp>2</Timestamp><Name>SqlServer_target</Name>
              <Type>1</Type><IsSource>N</IsSource>
              <ConnectParams>connectiontype=DotNet;datasourcename=Microsoft.SQLServer;</ConnectParams>
              <ConnectString></ConnectString><StgConnectParams></StgConnectParams>
              <StgConnectString /><Properties></Properties>
              <CreatedBy>test</CreatedBy><CreatedAt>2</CreatedAt>
              <ModifiedBy>test</ModifiedBy><ModifiedAt>2</ModifiedAt>
            </DBMMConnections>
            <DBMMSchemas>
              <SchemaID>1</SchemaID><ConnectionID>1</ConnectionID><Name>DEMO</Name>
            </DBMMSchemas>
            <DBMMSchemas>
              <SchemaID>2</SchemaID><ConnectionID>2</ConnectionID><Name>dbo</Name>
            </DBMMSchemas>
            <DBMMTables>
              <TableID>1</TableID><SchemaID>1</SchemaID><Name>MYTABLE</Name>
            </DBMMTables>
            <DBMMTables>
              <TableID>2</TableID><SchemaID>2</SchemaID><Name>MYTABLE</Name>
            </DBMMTables>
            {src_fields_xml}
            {tgt_fields_xml}
            <DBMMGroups>
              <GroupID>1</GroupID><Name>TestGroup</Name><Type>0</Type>
            </DBMMGroups>
            <DBMMReplications>
              <ReplicationID>1</ReplicationID><GroupID>1</GroupID>
              <Name>TestReplication</Name><ReplStatus>0</ReplStatus>
              <SrcTableID>1</SrcTableID><TrgTableID>2</TrgTableID>
              <Properties>GroupPriority=0;</Properties>
            </DBMMReplications>
            {direct_mappings}
            {extra_field_mappings}
          </tables>
        </metadata>
    """)


# ---------------------------------------------------------------------------
# Helper: run the full parse + export pipeline on an XML string
# ---------------------------------------------------------------------------

def _run_pipeline(xml_content: str, output_dir: str) -> dict:
    """Write xml_content to a temp file, run the pipeline, return parsed YAMLs."""
    xml_path = os.path.join(output_dir, "test_input.xml")
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(xml_content)

    parser.args.xml_path = xml_path
    parser.args.output_dir = output_dir
    parser.args.template = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "table-list-template-basic.yaml",
    )
    parser.args.include_targets = True
    parser.args.force_schemas = None

    (
        connections, groups, chains, replications,
        source_to_target_schemas, field_mappings, field_id_to_name,
        record_id_mappings, journal_checkpoints, refresh_filters,
    ) = parser.parse_xml()

    parser.export_as_yaml(
        connections, groups, chains, replications,
        source_to_target_schemas, field_mappings, field_id_to_name,
        record_id_mappings,
        journal_checkpoints=journal_checkpoints,
        refresh_filters=refresh_filters,
        output_dir=output_dir,
    )

    # Load all generated YAML files
    results = {}
    for fname in os.listdir(output_dir):
        if fname.endswith(".yaml"):
            with open(os.path.join(output_dir, fname)) as f:
                results[fname] = yaml.safe_load(f)
    return results


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestPKFallbackFromTarget(unittest.TestCase):
    """When the source table has no primary keys, the converter should derive
    them from the corresponding target table rather than using all columns."""

    def setUp(self):
        self.output_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.output_dir)

    def _get_mytable_config(self, yamls: dict) -> dict:
        """Find the MYTABLE custom config in any of the generated YAMLs."""
        for content in yamls.values():
            for schema_data in content.values():
                custom = schema_data.get("tables", {}).get("custom", {})
                if "MYTABLE" in custom:
                    return custom["MYTABLE"]
        self.fail("MYTABLE not found in any generated YAML")

    # ------------------------------------------------------------------

    def test_source_has_pks_uses_source_pks(self):
        """Sanity check: when the source has PKs they are used directly."""
        xml = _build_xml(
            src_pks={"CHCO": 1, "CHONO": 2},
            tgt_pks={"CHCO": 1, "CHONO": 2},
        )
        yamls = _run_pipeline(xml, self.output_dir)
        cfg = self._get_mytable_config(yamls)
        self.assertEqual(sorted(cfg["keys"]), ["CHCO", "CHONO"])

    def test_no_source_pks_derives_from_target(self):
        """Core fix: when source has no PKs, target PKs are used instead of
        falling back to ALL columns."""
        xml = _build_xml(
            src_pks={},                          # no PKs on source
            tgt_pks={"CHCO": 1, "CHONO": 2},    # PKs only on target
        )
        yamls = _run_pipeline(xml, self.output_dir)
        cfg = self._get_mytable_config(yamls)

        keys = cfg["keys"]
        # Must NOT be the entire column list (old buggy behaviour)
        self.assertLess(
            len(keys), 4,
            f"Expected derived PKs, got all-columns fallback: {keys}",
        )
        # Must contain the two target PK names mapped back to source names
        self.assertIn("CHCO",  keys)
        self.assertIn("CHONO", keys)

    def test_no_source_pks_and_no_target_pks_falls_back_to_all_columns(self):
        """Last-resort fallback: when neither side has PKs, all columns are used."""
        xml = _build_xml(src_pks={}, tgt_pks={})
        yamls = _run_pipeline(xml, self.output_dir)
        cfg = self._get_mytable_config(yamls)

        # All 4 source columns should appear as keys
        self.assertEqual(len(cfg["keys"]), 4)


class TestExpressionColumnsIncluded(unittest.TestCase):
    """Columns that are populated via a SrcExpression (no SrcFieldID) must
    appear in the generated column list."""

    def setUp(self):
        self.output_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.output_dir)

    def _get_mytable_config(self, yamls: dict) -> dict:
        for content in yamls.values():
            for schema_data in content.values():
                custom = schema_data.get("tables", {}).get("custom", {})
                if "MYTABLE" in custom:
                    return custom["MYTABLE"]
        self.fail("MYTABLE not found in any generated YAML")

    # ------------------------------------------------------------------

    def test_expression_column_added_to_columns(self):
        """A MakeDateTime expression mapping should produce an extra column."""

        # Add a target-only field (ID=20, name=INDDATE) to the target table
        # and a field mapping that populates it via an expression.
        extra_target_field = """
        <DBMMFields>
          <FieldID>20</FieldID><TableID>2</TableID>
          <Name>INDDATE</Name><Ordinal>5</Ordinal>
          <Type>datetime2</Type><InternalType>25</InternalType>
          <Size>7</Size><Precision>23</Precision><Scale>3</Scale>
          <IsUnsigned>N</IsUnsigned><Ccsid>0</Ccsid>
          <AllowNull>Y</AllowNull>
          <PrimaryKeyPos>0</PrimaryKeyPos>
          <IsAutoIncrement>N</IsAutoIncrement>
        </DBMMFields>"""

        expression_mapping = """
        <DBMMFieldMappings>
          <FieldMappingID>200</FieldMappingID>
          <ReplicationID>1</ReplicationID>
          <TrgFieldID>20</TrgFieldID>
          <SrcExpression>MakeDateTime([INDMO], [INDDA], [INDYR], [INDCT])</SrcExpression>
          <IsForth>Y</IsForth>
        </DBMMFieldMappings>"""

        xml = _build_xml(
            src_pks={"CHCO": 1},
            tgt_pks={"CHCO": 1},
            extra_field_mappings=extra_target_field + expression_mapping,
        )
        yamls = _run_pipeline(xml, self.output_dir)
        cfg = self._get_mytable_config(yamls)

        col_names = [c["name"] for c in cfg["columns"]]
        self.assertIn(
            "INDDATE", col_names,
            f"Expression column INDDATE missing from columns: {col_names}",
        )

    def test_expression_column_has_correct_type(self):
        """The expression column should inherit its type from the target field."""

        extra_target_field = """
        <DBMMFields>
          <FieldID>21</FieldID><TableID>2</TableID>
          <Name>COMPUTEDCOL</Name><Ordinal>5</Ordinal>
          <Type>DECIMAL</Type><InternalType>12</InternalType>
          <Size>15</Size><Precision>15</Precision><Scale>2</Scale>
          <IsUnsigned>N</IsUnsigned><Ccsid>0</Ccsid>
          <AllowNull>N</AllowNull>
          <PrimaryKeyPos>0</PrimaryKeyPos>
          <IsAutoIncrement>N</IsAutoIncrement>
        </DBMMFields>"""

        expression_mapping = """
        <DBMMFieldMappings>
          <FieldMappingID>201</FieldMappingID>
          <ReplicationID>1</ReplicationID>
          <TrgFieldID>21</TrgFieldID>
          <SrcExpression>[COLA] * 1.5</SrcExpression>
          <IsForth>Y</IsForth>
        </DBMMFieldMappings>"""

        xml = _build_xml(
            src_pks={"CHCO": 1},
            tgt_pks={"CHCO": 1},
            extra_field_mappings=extra_target_field + expression_mapping,
        )
        yamls = _run_pipeline(xml, self.output_dir)
        cfg = self._get_mytable_config(yamls)

        col = next((c for c in cfg["columns"] if c["name"] == "COMPUTEDCOL"), None)
        self.assertIsNotNone(col, "COMPUTEDCOL column should be present")
        self.assertEqual(col["type"], "DECIMAL")
        self.assertEqual(col["dataLength"], 15)
        self.assertEqual(col["numericPrecision"], 15)
        self.assertEqual(col["numericScale"], 2)

    def test_recordid_expression_not_added_as_expression_column(self):
        """[!RecordID] expressions must NOT be added twice — they are handled
        separately via the _RRN path and must remain deduplicated."""

        extra_target_field = """
        <DBMMFields>
          <FieldID>22</FieldID><TableID>2</TableID>
          <Name>MYRRN</Name><Ordinal>5</Ordinal>
          <Type>DECIMAL</Type><InternalType>12</InternalType>
          <Size>15</Size><Precision>15</Precision><Scale>0</Scale>
          <IsUnsigned>N</IsUnsigned><Ccsid>0</Ccsid>
          <AllowNull>N</AllowNull>
          <PrimaryKeyPos>0</PrimaryKeyPos>
          <IsAutoIncrement>N</IsAutoIncrement>
        </DBMMFields>"""

        recordid_mapping = """
        <DBMMFieldMappings>
          <FieldMappingID>202</FieldMappingID>
          <ReplicationID>1</ReplicationID>
          <TrgFieldID>22</TrgFieldID>
          <SrcExpression>[!RecordID]</SrcExpression>
          <IsForth>Y</IsForth>
        </DBMMFieldMappings>"""

        xml = _build_xml(
            src_pks={},
            tgt_pks={},
            extra_field_mappings=extra_target_field + recordid_mapping,
        )
        yamls = _run_pipeline(xml, self.output_dir)
        cfg = self._get_mytable_config(yamls)

        col_names = [c["name"] for c in cfg["columns"]]
        # MYRRN should appear exactly once (added via _RRN path)
        self.assertEqual(
            col_names.count("MYRRN"), 1,
            f"MYRRN should appear exactly once, got: {col_names}",
        )

    def test_recordid_expression_on_same_named_source_field_no_duplicate(self):
        """Real-world case: the source table has a physical column named RECORDID
        AND a [!RecordID] expression maps to the same-named target column.
        The column must appear exactly once, with sourceName='_RRN'."""

        # Add a source field named RECORDID (physical column, PrimaryKeyPos=0)
        extra_src_field = """
        <DBMMFields>
          <FieldID>5</FieldID><TableID>1</TableID>
          <Name>RECORDID</Name><Ordinal>5</Ordinal>
          <Type>DECIMAL</Type><InternalType>12</InternalType>
          <Size>9</Size><Precision>9</Precision><Scale>0</Scale>
          <IsUnsigned>N</IsUnsigned><Ccsid>0</Ccsid>
          <AllowNull>N</AllowNull>
          <PrimaryKeyPos>0</PrimaryKeyPos>
          <IsAutoIncrement>N</IsAutoIncrement>
        </DBMMFields>"""

        # Target also has a RECORDID field (ID=23)
        extra_tgt_field = """
        <DBMMFields>
          <FieldID>23</FieldID><TableID>2</TableID>
          <Name>RECORDID</Name><Ordinal>5</Ordinal>
          <Type>DECIMAL</Type><InternalType>12</InternalType>
          <Size>9</Size><Precision>9</Precision><Scale>0</Scale>
          <IsUnsigned>N</IsUnsigned><Ccsid>0</Ccsid>
          <AllowNull>N</AllowNull>
          <PrimaryKeyPos>0</PrimaryKeyPos>
          <IsAutoIncrement>N</IsAutoIncrement>
        </DBMMFields>"""

        # Direct field mapping: source RECORDID (5) → target RECORDID (23)
        direct_mapping = """
        <DBMMFieldMappings>
          <FieldMappingID>204</FieldMappingID>
          <ReplicationID>1</ReplicationID>
          <SrcFieldID>5</SrcFieldID>
          <TrgFieldID>23</TrgFieldID>
          <IsForth>Y</IsForth>
        </DBMMFieldMappings>"""

        # [!RecordID] expression mapping also targets RECORDID (23)
        recordid_mapping = """
        <DBMMFieldMappings>
          <FieldMappingID>205</FieldMappingID>
          <ReplicationID>1</ReplicationID>
          <TrgFieldID>23</TrgFieldID>
          <SrcExpression>[!RecordID]</SrcExpression>
          <IsForth>Y</IsForth>
        </DBMMFieldMappings>"""

        xml = _build_xml(
            src_pks={"CHCO": 1},
            tgt_pks={"CHCO": 1},
            extra_field_mappings=(
                extra_src_field + extra_tgt_field + direct_mapping + recordid_mapping
            ),
        )
        yamls = _run_pipeline(xml, self.output_dir)

        # The _RRN update only applies to the source-side YAML (AS400_source__DEMO.yaml).
        # The target-side YAML (SqlServer_target__dbo.yaml) has its own direct RECORDID
        # column without _RRN, which is correct for that direction.
        src_yaml = yamls.get("AS400_source__DEMO.yaml")
        self.assertIsNotNone(src_yaml, f"AS400_source__DEMO.yaml not found. Got: {list(yamls.keys())}")
        cfg = src_yaml["DEMO"]["tables"]["custom"]["MYTABLE"]

        col_names = [c["name"] for c in cfg["columns"]]
        # Must not be duplicated
        self.assertEqual(
            col_names.count("RECORDID"), 1,
            f"RECORDID should appear exactly once, got columns: {col_names}",
        )
        # The surviving entry must have sourceName=_RRN
        recordid_col = next(c for c in cfg["columns"] if c["name"] == "RECORDID")
        self.assertEqual(
            recordid_col.get("sourceName"), "_RRN",
            "RECORDID column must have sourceName=_RRN when [!RecordID] mapping exists",
        )

    def test_direct_mapped_column_not_duplicated_by_expression_path(self):
        """A column that already exists via a direct mapping must not be
        added again even if a stale expression entry is somehow present."""

        # Inject an expression mapping pointing at an already-direct-mapped field (CHCO, tid=13)
        stale_expression_mapping = """
        <DBMMFieldMappings>
          <FieldMappingID>203</FieldMappingID>
          <ReplicationID>1</ReplicationID>
          <TrgFieldID>13</TrgFieldID>
          <SrcExpression>[CHCO]</SrcExpression>
          <IsForth>Y</IsForth>
        </DBMMFieldMappings>"""

        xml = _build_xml(
            src_pks={"CHCO": 1},
            tgt_pks={"CHCO": 1},
            extra_field_mappings=stale_expression_mapping,
        )
        yamls = _run_pipeline(xml, self.output_dir)
        cfg = self._get_mytable_config(yamls)

        col_names = [c["name"] for c in cfg["columns"]]
        self.assertEqual(
            col_names.count("CHCO"), 1,
            f"CHCO should appear exactly once, got: {col_names}",
        )


if __name__ == "__main__":
    unittest.main()
