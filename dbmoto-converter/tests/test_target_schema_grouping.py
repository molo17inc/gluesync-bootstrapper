import os
import sys
import tempfile
import textwrap
import unittest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import parse_dbmoto_metadata_xml as parser


class TestTargetSchemaGrouping(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.xml_path = os.path.join(self.temp_dir.name, 'mixed-targets.xml')
        self.template_path = os.path.join(self.temp_dir.name, 'missing-template.yaml')
        self.output_dir = os.path.join(self.temp_dir.name, 'out')

        xml = textwrap.dedent('''\
            <?xml version="1.0" encoding="utf-8"?>
            <metadata version="10.7.1">
              <tables>
                <DBMMConnections>
                  <ConnectionID>1</ConnectionID>
                  <Name>Source</Name>
                  <Type>3</Type>
                  <IsSource>Y</IsSource>
                </DBMMConnections>
                <DBMMConnections>
                  <ConnectionID>2</ConnectionID>
                  <Name>Target</Name>
                  <Type>1</Type>
                  <IsSource>N</IsSource>
                </DBMMConnections>
                <DBMMSchemas>
                  <SchemaID>10</SchemaID>
                  <ConnectionID>1</ConnectionID>
                  <Name>FILELIB</Name>
                </DBMMSchemas>
                <DBMMSchemas>
                  <SchemaID>20</SchemaID>
                  <ConnectionID>2</ConnectionID>
                  <Name>Claims</Name>
                </DBMMSchemas>
                <DBMMSchemas>
                  <SchemaID>21</SchemaID>
                  <ConnectionID>2</ConnectionID>
                  <Name>UW</Name>
                </DBMMSchemas>
                <DBMMTables>
                  <TableID>100</TableID>
                  <SchemaID>10</SchemaID>
                  <Name>LGLSETPM</Name>
                </DBMMTables>
                <DBMMTables>
                  <TableID>101</TableID>
                  <SchemaID>10</SchemaID>
                  <Name>UNDLTRPM</Name>
                </DBMMTables>
                <DBMMTables>
                  <TableID>200</TableID>
                  <SchemaID>20</SchemaID>
                  <Name>FILELIB.LGLSETPM</Name>
                </DBMMTables>
                <DBMMTables>
                  <TableID>201</TableID>
                  <SchemaID>21</SchemaID>
                  <Name>FILELIB.UNDLTRPM</Name>
                </DBMMTables>
                <DBMMFields>
                  <FieldID>1001</FieldID>
                  <TableID>100</TableID>
                  <Name>ID</Name>
                  <Type>DECIMAL</Type>
                  <Size>9</Size>
                  <Precision>9</Precision>
                  <Scale>0</Scale>
                  <AllowNull>N</AllowNull>
                  <PrimaryKeyPos>1</PrimaryKeyPos>
                </DBMMFields>
                <DBMMFields>
                  <FieldID>1002</FieldID>
                  <TableID>100</TableID>
                  <Name>NAME</Name>
                  <Type>CHAR</Type>
                  <Size>20</Size>
                  <Precision>0</Precision>
                  <Scale>0</Scale>
                  <AllowNull>Y</AllowNull>
                  <PrimaryKeyPos>0</PrimaryKeyPos>
                </DBMMFields>
                <DBMMFields>
                  <FieldID>1011</FieldID>
                  <TableID>101</TableID>
                  <Name>CODE</Name>
                  <Type>CHAR</Type>
                  <Size>2</Size>
                  <Precision>0</Precision>
                  <Scale>0</Scale>
                  <AllowNull>N</AllowNull>
                  <PrimaryKeyPos>1</PrimaryKeyPos>
                </DBMMFields>
                <DBMMFields>
                  <FieldID>1012</FieldID>
                  <TableID>101</TableID>
                  <Name>DESC</Name>
                  <Type>CHAR</Type>
                  <Size>25</Size>
                  <Precision>0</Precision>
                  <Scale>0</Scale>
                  <AllowNull>Y</AllowNull>
                  <PrimaryKeyPos>0</PrimaryKeyPos>
                </DBMMFields>
                <DBMMGroups>
                  <GroupID>1</GroupID>
                  <Name>Default</Name>
                  <Type>0</Type>
                </DBMMGroups>
                <DBMMReplications>
                  <ReplicationID>1</ReplicationID>
                  <GroupID>1</GroupID>
                  <Name>LGLSETPM</Name>
                  <SrcTableID>100</SrcTableID>
                  <TrgTableID>200</TrgTableID>
                  <ReplStatus>0</ReplStatus>
                  <Properties>GroupPriority=1;</Properties>
                </DBMMReplications>
                <DBMMReplications>
                  <ReplicationID>2</ReplicationID>
                  <GroupID>1</GroupID>
                  <Name>UNDLTRPM</Name>
                  <SrcTableID>101</SrcTableID>
                  <TrgTableID>201</TrgTableID>
                  <ReplStatus>0</ReplStatus>
                  <Properties>GroupPriority=1;</Properties>
                </DBMMReplications>
              </tables>
            </metadata>
        ''')
        with open(self.xml_path, 'w', encoding='utf-8') as fh:
            fh.write(xml)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_mixed_target_schemas_split_into_distinct_yaml_files(self):
        parser.args.xml_path = self.xml_path
        parser.args.output_dir = self.output_dir
        parser.args.template = self.template_path
        parser.args.include_targets = True
        parser.args.force_schemas = None

        connections, groups, chains, replications, source_to_target_schemas, field_mappings, field_id_to_name, record_id_mappings, refresh_filters = parser.parse_xml()
        exported = parser.export_as_yaml(
            connections,
            groups,
            chains,
            replications,
            source_to_target_schemas,
            field_mappings,
            field_id_to_name,
            record_id_mappings,
            refresh_filters=refresh_filters,
            output_dir=self.output_dir,
            template_file=self.template_path,
        )

        self.assertEqual(exported, 2)
        claims_yaml = os.path.join(self.output_dir, 'Source__FILELIB__Claims.yaml')
        uw_yaml = os.path.join(self.output_dir, 'Source__FILELIB__UW.yaml')
        self.assertTrue(os.path.exists(claims_yaml))
        self.assertTrue(os.path.exists(uw_yaml))

        with open(claims_yaml, 'r', encoding='utf-8') as fh:
            claims_data = yaml.safe_load(fh)
        with open(uw_yaml, 'r', encoding='utf-8') as fh:
            uw_data = yaml.safe_load(fh)

        self.assertEqual(claims_data['FILELIB']['target'], 'Claims')
        self.assertEqual(uw_data['FILELIB']['target'], 'UW')
        self.assertIn('LGLSETPM', claims_data['FILELIB']['tables']['custom'])
        self.assertNotIn('UNDLTRPM', claims_data['FILELIB']['tables']['custom'])
        self.assertIn('UNDLTRPM', uw_data['FILELIB']['tables']['custom'])
        self.assertNotIn('LGLSETPM', uw_data['FILELIB']['tables']['custom'])


if __name__ == '__main__':
    unittest.main()
