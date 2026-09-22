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

"""GSSD-1359: IBM i TIMESTMP must become MSSQL datetime2 (not TIMESTAMP/rowversion).

Minimal MOVRAT00F-style fixture: RATUPDLOG / RATCRTLOG typed as TIMESTMP on AS400,
replicated to SQL Server. Generated source YAML columns must use datetime2.
"""

import os
import sys
import tempfile
import textwrap
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parse_dbmoto_metadata_xml as parser


def _movrat_style_xml(src_ts_type: str = "TIMESTMP", tgt_ts_type: str = "TIMESTAMP") -> str:
    """Minimal AS400 → SQL Server metadata with MOVRAT00F-like timestamp columns."""
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="utf-8"?>
        <metadata version="10.7.1">
          <tables>
            <DBMMConnections>
              <ConnectionID>1</ConnectionID><UUID>src-uuid</UUID>
              <Timestamp>1</Timestamp><Name>AS400MTG</Name>
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
              <SchemaID>1</SchemaID><ConnectionID>1</ConnectionID>
              <Name>ES2DAT</Name>
            </DBMMSchemas>
            <DBMMSchemas>
              <SchemaID>2</SchemaID><ConnectionID>2</ConnectionID>
              <Name>ES2DAT</Name>
            </DBMMSchemas>
            <DBMMTables>
              <TableID>1</TableID><SchemaID>1</SchemaID>
              <Name>MOVRAT00F</Name>
            </DBMMTables>
            <DBMMTables>
              <TableID>2</TableID><SchemaID>2</SchemaID>
              <Name>MOVRAT00F</Name>
            </DBMMTables>
            <DBMMFields>
              <FieldID>1</FieldID><TableID>1</TableID>
              <Name>RATVEPC</Name><Ordinal>1</Ordinal>
              <Type>CHAR</Type><InternalType>9</InternalType>
              <Size>4</Size><Precision>0</Precision><Scale>0</Scale>
              <AllowNull>N</AllowNull><PrimaryKeyPos>1</PrimaryKeyPos>
            </DBMMFields>
            <DBMMFields>
              <FieldID>2</FieldID><TableID>1</TableID>
              <Name>RATUPDLOG</Name><Ordinal>2</Ordinal>
              <Type>{src_ts_type}</Type><InternalType>25</InternalType>
              <Size>10</Size><Precision>0</Precision><Scale>6</Scale>
              <AllowNull>N</AllowNull><PrimaryKeyPos>0</PrimaryKeyPos>
            </DBMMFields>
            <DBMMFields>
              <FieldID>3</FieldID><TableID>1</TableID>
              <Name>RATCRTLOG</Name><Ordinal>3</Ordinal>
              <Type>{src_ts_type}</Type><InternalType>25</InternalType>
              <Size>10</Size><Precision>0</Precision><Scale>6</Scale>
              <AllowNull>N</AllowNull><PrimaryKeyPos>0</PrimaryKeyPos>
            </DBMMFields>
            <DBMMFields>
              <FieldID>11</FieldID><TableID>2</TableID>
              <Name>RATVEPC</Name><Ordinal>1</Ordinal>
              <Type>CHAR</Type><InternalType>9</InternalType>
              <Size>4</Size><Precision>0</Precision><Scale>0</Scale>
              <AllowNull>N</AllowNull><PrimaryKeyPos>1</PrimaryKeyPos>
            </DBMMFields>
            <DBMMFields>
              <FieldID>12</FieldID><TableID>2</TableID>
              <Name>RATUPDLOG</Name><Ordinal>2</Ordinal>
              <Type>{tgt_ts_type}</Type><InternalType>25</InternalType>
              <Size>8</Size><Precision>0</Precision><Scale>0</Scale>
              <AllowNull>N</AllowNull><PrimaryKeyPos>0</PrimaryKeyPos>
            </DBMMFields>
            <DBMMFields>
              <FieldID>13</FieldID><TableID>2</TableID>
              <Name>RATCRTLOG</Name><Ordinal>3</Ordinal>
              <Type>{tgt_ts_type}</Type><InternalType>25</InternalType>
              <Size>8</Size><Precision>0</Precision><Scale>0</Scale>
              <AllowNull>N</AllowNull><PrimaryKeyPos>0</PrimaryKeyPos>
            </DBMMFields>
            <DBMMGroups>
              <GroupID>1</GroupID><Name>default</Name><Type>0</Type>
            </DBMMGroups>
            <DBMMReplications>
              <ReplicationID>1</ReplicationID>
              <GroupID>1</GroupID>
              <SrcTableID>1</SrcTableID>
              <TrgTableID>2</TrgTableID>
              <ReplStatus>1</ReplStatus>
              <Name>MOVRAT00F</Name>
            </DBMMReplications>
            <DBMMFieldMappings>
              <FieldMappingID>101</FieldMappingID>
              <ReplicationID>1</ReplicationID>
              <SrcFieldID>1</SrcFieldID><TrgFieldID>11</TrgFieldID>
              <IsForth>Y</IsForth>
            </DBMMFieldMappings>
            <DBMMFieldMappings>
              <FieldMappingID>102</FieldMappingID>
              <ReplicationID>1</ReplicationID>
              <SrcFieldID>2</SrcFieldID><TrgFieldID>12</TrgFieldID>
              <IsForth>Y</IsForth>
            </DBMMFieldMappings>
            <DBMMFieldMappings>
              <FieldMappingID>103</FieldMappingID>
              <ReplicationID>1</ReplicationID>
              <SrcFieldID>3</SrcFieldID><TrgFieldID>13</TrgFieldID>
              <IsForth>Y</IsForth>
            </DBMMFieldMappings>
          </tables>
        </metadata>
    """)


class TestMapTypeForTargetHelpers(unittest.TestCase):
    def test_ibmi_timestmp_to_mssql_datetime2(self):
        self.assertEqual(
            parser.map_type_for_target("TIMESTMP", "IBM.DB2.i", "Microsoft.SQLServer"),
            "datetime2",
        )
        self.assertEqual(
            parser.map_type_for_target("TIMESTAMP", "IBM.DB2.i", "Microsoft.SQLServer"),
            "datetime2",
        )

    def test_non_mssql_target_keeps_timestamp(self):
        self.assertEqual(
            parser.map_type_for_target("TIMESTAMP", "IBM.DB2.i", "PostgreSQL"),
            "TIMESTAMP",
        )

    def test_mssql_to_mssql_does_not_remap_rowversion(self):
        # Pure MSSQL TIMESTAMP (rowversion) must not be rewritten just because
        # the target is also MSSQL — only IBM i sources are remapped.
        self.assertEqual(
            parser.map_type_for_target("TIMESTAMP", "Microsoft.SQLServer", "Microsoft.SQLServer"),
            "TIMESTAMP",
        )


class TestMovratTimestmpYaml(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = os.path.join(self.temp_dir.name, "out")
        self.template_path = os.path.join(self.temp_dir.name, "missing-template.yaml")
        os.makedirs(self.output_dir, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run(self, xml_body: str):
        xml_path = os.path.join(self.temp_dir.name, "movrat.xml")
        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(xml_body)
        # Reset mutable globals used by the CLI-oriented parser module
        parser.args.xml_path = xml_path
        parser.args.output_dir = self.output_dir
        parser.args.template = self.template_path
        parser.args.include_targets = False
        parser.args.force_schemas = None
        parser.XML_PATH = xml_path
        parser.OUTPUT_DIR = self.output_dir
        parser.TEMPLATE_PATH = self.template_path
        parser.INCLUDE_TARGETS = False
        (
            connections,
            groups,
            chains,
            replications,
            source_to_target_schemas,
            field_mappings,
            field_id_to_name,
            record_id_mappings,
            journal_checkpoints,
            refresh_filters,
        ) = parser.parse_xml()
        parser.export_as_yaml(
            connections,
            groups,
            chains,
            replications,
            source_to_target_schemas,
            field_mappings,
            field_id_to_name,
            record_id_mappings,
            output_dir=self.output_dir,
            template_file=self.template_path,
            journal_checkpoints=journal_checkpoints,
            refresh_filters=refresh_filters,
        )
        yamls = {}
        for name in os.listdir(self.output_dir):
            if name.endswith(".yaml"):
                with open(os.path.join(self.output_dir, name), encoding="utf-8") as f:
                    yamls[name] = yaml.safe_load(f)
        return yamls

    def test_movrat_timestmp_becomes_datetime2(self):
        """MOVRAT00F-style TIMESTMP columns must emit datetime2 for MSSQL target."""
        yamls = self._run(_movrat_style_xml(src_ts_type="TIMESTMP", tgt_ts_type="TIMESTAMP"))
        self.assertTrue(yamls, f"expected YAML output, got: {list(yamls)}")
        # Source connection YAML
        src_yaml = next((v for k, v in yamls.items() if "AS400MTG" in k), None)
        self.assertIsNotNone(src_yaml, f"AS400MTG yaml missing: {list(yamls)}")
        schema = src_yaml["ES2DAT"]
        cols = {c["name"]: c for c in schema["tables"]["custom"]["MOVRAT00F"]["columns"]}
        self.assertEqual(cols["RATUPDLOG"]["type"], "datetime2")
        self.assertEqual(cols["RATCRTLOG"]["type"], "datetime2")
        self.assertEqual(cols["RATVEPC"]["type"], "CHAR")

    def test_movrat_when_target_already_datetime2(self):
        """If DbMoto target metadata already has datetime2, keep it."""
        yamls = self._run(_movrat_style_xml(src_ts_type="TIMESTMP", tgt_ts_type="datetime2"))
        src_yaml = next((v for k, v in yamls.items() if "AS400MTG" in k), None)
        cols = {c["name"]: c for c in src_yaml["ES2DAT"]["tables"]["custom"]["MOVRAT00F"]["columns"]}
        self.assertEqual(cols["RATUPDLOG"]["type"], "datetime2")
        self.assertEqual(cols["RATCRTLOG"]["type"], "datetime2")


if __name__ == "__main__":
    unittest.main()
