import os
import sys
import tempfile
import textwrap
import unittest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import parse_dbmoto_metadata_xml as parser


class TestCaseInsensitiveColumnMatching(unittest.TestCase):
    """When source and target tables have columns that differ only in casing
    (e.g. MySQL on Linux), the converter must use the target's actual column
    names and set sourceName on each column for Gluesync to map correctly."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.xml_path = os.path.join(self.temp_dir.name, 'case-mismatch.xml')
        self.template_path = os.path.join(self.temp_dir.name, 'template.yaml')
        self.output_dir = os.path.join(self.temp_dir.name, 'out')

        with open(self.template_path, 'w') as f:
            yaml.dump({'schemas': {}}, f)

        xml = textwrap.dedent('''\
            <?xml version="1.0" encoding="utf-8"?>
            <metadata version="10.7.1">
              <tables>
                <DBMMConnections>
                  <ConnectionID>1</ConnectionID>
                  <Name>src_mysql</Name>
                  <IsSource>Y</IsSource>
                </DBMMConnections>
                <DBMMConnections>
                  <ConnectionID>2</ConnectionID>
                  <Name>tgt_mysql</Name>
                  <IsSource>N</IsSource>
                </DBMMConnections>

                <DBMMSchemas>
                  <SchemaID>10</SchemaID>
                  <ConnectionID>1</ConnectionID>
                  <Name>demo</Name>
                </DBMMSchemas>
                <DBMMSchemas>
                  <SchemaID>20</SchemaID>
                  <ConnectionID>2</ConnectionID>
                  <Name>demo_target</Name>
                </DBMMSchemas>

                <!-- Source table: loweruppercase -->
                <DBMMTables>
                  <TableID>100</TableID>
                  <SchemaID>10</SchemaID>
                  <Name>loweruppercase</Name>
                </DBMMTables>
                <!-- Target table: LOWERUPPERCASE -->
                <DBMMTables>
                  <TableID>200</TableID>
                  <SchemaID>20</SchemaID>
                  <Name>LOWERUPPERCASE</Name>
                </DBMMTables>

                <!-- Source fields: ID, Name -->
                <DBMMFields>
                  <FieldID>1001</FieldID>
                  <TableID>100</TableID>
                  <Name>ID</Name>
                  <Type>INTEGER</Type>
                  <Size>4</Size>
                  <Precision>10</Precision>
                  <Scale>0</Scale>
                  <AllowNull>N</AllowNull>
                  <PrimaryKeyPos>1</PrimaryKeyPos>
                </DBMMFields>
                <DBMMFields>
                  <FieldID>1002</FieldID>
                  <TableID>100</TableID>
                  <Name>Name</Name>
                  <Type>CHAR</Type>
                  <Size>10</Size>
                  <Precision>0</Precision>
                  <Scale>0</Scale>
                  <AllowNull>Y</AllowNull>
                  <PrimaryKeyPos>0</PrimaryKeyPos>
                </DBMMFields>

                <!-- Target fields: ID, NAME, x__last_update -->
                <DBMMFields>
                  <FieldID>2001</FieldID>
                  <TableID>200</TableID>
                  <Name>ID</Name>
                  <Type>INTEGER</Type>
                  <Size>4</Size>
                  <Precision>10</Precision>
                  <Scale>0</Scale>
                  <AllowNull>N</AllowNull>
                  <PrimaryKeyPos>1</PrimaryKeyPos>
                </DBMMFields>
                <DBMMFields>
                  <FieldID>2002</FieldID>
                  <TableID>200</TableID>
                  <Name>NAME</Name>
                  <Type>CHAR</Type>
                  <Size>10</Size>
                  <Precision>0</Precision>
                  <Scale>0</Scale>
                  <AllowNull>Y</AllowNull>
                  <PrimaryKeyPos>0</PrimaryKeyPos>
                </DBMMFields>
                <DBMMFields>
                  <FieldID>2003</FieldID>
                  <TableID>200</TableID>
                  <Name>x__last_update</Name>
                  <Type>DATETIME</Type>
                  <Size>8</Size>
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
                  <Name>loweruppercase_repl</Name>
                  <SrcTableID>100</SrcTableID>
                  <TrgTableID>200</TrgTableID>
                  <ReplStatus>0</ReplStatus>
                  <Properties></Properties>
                </DBMMReplications>
              </tables>
            </metadata>
        ''')
        with open(self.xml_path, 'w', encoding='utf-8') as fh:
            fh.write(xml)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_target_column_names_used_with_source_name(self):
        parser.args.xml_path = self.xml_path
        parser.args.output_dir = self.output_dir
        parser.args.template = self.template_path
        parser.args.include_targets = True
        parser.args.force_schemas = None

        connections, groups, chains, replications, source_to_target_schemas, \
            field_mappings, field_id_to_name, record_id_mappings, \
            journal_checkpoints, refresh_filters = parser.parse_xml()

        parser.export_as_yaml(
            connections, groups, chains, replications,
            source_to_target_schemas, field_mappings, field_id_to_name,
            record_id_mappings,
            journal_checkpoints=journal_checkpoints,
            refresh_filters=refresh_filters,
            output_dir=self.output_dir,
            template_file=self.template_path,
        )

        yaml_file = os.path.join(self.output_dir, 'src_mysql__demo.yaml')
        self.assertTrue(os.path.exists(yaml_file),
                        f'Expected src_mysql__demo.yaml, got: {os.listdir(self.output_dir)}')

        with open(yaml_file, 'r', encoding='utf-8') as fh:
            data = yaml.safe_load(fh)

        custom = data['demo']['tables']['custom']
        self.assertIn('loweruppercase', custom)

        table_config = custom['loweruppercase']
        self.assertEqual(table_config['name'], 'LOWERUPPERCASE')

        columns = table_config['columns']
        col_by_name = {c['name']: c for c in columns}

        # ID is same on both sides — no sourceName needed
        self.assertIn('ID', col_by_name)
        self.assertNotIn('sourceName', col_by_name['ID'])

        # Name (source) -> NAME (target) — case-insensitive match
        self.assertIn('NAME', col_by_name)
        self.assertEqual(col_by_name['NAME'].get('sourceName'), 'Name')

        # x__last_update is target-only — should not appear (no expression mapping)
        self.assertNotIn('x__last_update', col_by_name)

        # Primary keys should use source field names
        self.assertEqual(table_config['keys'], ['ID'])


if __name__ == '__main__':
    unittest.main()
