import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

import parse_dbmoto_metadata_xml as parser


class TestMultiTargetReplication(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.xml_path = os.path.join(self.temp_dir.name, 'multi-target.xml')
        self.template_path = os.path.join(self.temp_dir.name, 'missing-template.yaml')
        self.output_dir = os.path.join(self.temp_dir.name, 'out')

        # One source table replicated to TWO different target connections.
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
                  <Name>Target_A</Name>
                  <Type>1</Type>
                  <IsSource>N</IsSource>
                </DBMMConnections>
                <DBMMConnections>
                  <ConnectionID>3</ConnectionID>
                  <Name>Target_B</Name>
                  <Type>1</Type>
                  <IsSource>N</IsSource>
                </DBMMConnections>
                <DBMMSchemas>
                  <SchemaID>10</SchemaID>
                  <ConnectionID>1</ConnectionID>
                  <Name>INTERNET</Name>
                </DBMMSchemas>
                <DBMMSchemas>
                  <SchemaID>20</SchemaID>
                  <ConnectionID>2</ConnectionID>
                  <Name>Remote</Name>
                </DBMMSchemas>
                <DBMMSchemas>
                  <SchemaID>30</SchemaID>
                  <ConnectionID>3</ConnectionID>
                  <Name>Remote</Name>
                </DBMMSchemas>
                <DBMMTables>
                  <TableID>100</TableID>
                  <SchemaID>10</SchemaID>
                  <Name>JOINTSALESREPXREFVIEW</Name>
                </DBMMTables>
                <DBMMTables>
                  <TableID>200</TableID>
                  <SchemaID>20</SchemaID>
                  <Name>JointSalesRepXRef</Name>
                </DBMMTables>
                <DBMMTables>
                  <TableID>300</TableID>
                  <SchemaID>30</SchemaID>
                  <Name>JointSalesRepXRef</Name>
                </DBMMTables>
                <DBMMFields>
                  <FieldID>1001</FieldID>
                  <TableID>100</TableID>
                  <Name>SALESREPNUMBER</Name>
                  <Type>CHAR</Type>
                  <Size>5</Size>
                  <Precision>0</Precision>
                  <Scale>0</Scale>
                  <AllowNull>N</AllowNull>
                  <PrimaryKeyPos>0</PrimaryKeyPos>
                </DBMMFields>
                <DBMMFields>
                  <FieldID>1002</FieldID>
                  <TableID>100</TableID>
                  <Name>JOINTSALESREPNUMBER</Name>
                  <Type>CHAR</Type>
                  <Size>5</Size>
                  <Precision>0</Precision>
                  <Scale>0</Scale>
                  <AllowNull>N</AllowNull>
                  <PrimaryKeyPos>0</PrimaryKeyPos>
                </DBMMFields>
                <DBMMFields>
                  <FieldID>2001</FieldID>
                  <TableID>200</TableID>
                  <Name>SalesRepNumber</Name>
                  <Type>varchar</Type>
                  <Size>5</Size>
                  <Precision>0</Precision>
                  <Scale>0</Scale>
                  <AllowNull>N</AllowNull>
                  <PrimaryKeyPos>2</PrimaryKeyPos>
                </DBMMFields>
                <DBMMFields>
                  <FieldID>2002</FieldID>
                  <TableID>200</TableID>
                  <Name>JointSalesRepNumber</Name>
                  <Type>varchar</Type>
                  <Size>5</Size>
                  <Precision>0</Precision>
                  <Scale>0</Scale>
                  <AllowNull>N</AllowNull>
                  <PrimaryKeyPos>1</PrimaryKeyPos>
                </DBMMFields>
                <DBMMFields>
                  <FieldID>3001</FieldID>
                  <TableID>300</TableID>
                  <Name>SalesRepNumber</Name>
                  <Type>varchar</Type>
                  <Size>5</Size>
                  <Precision>0</Precision>
                  <Scale>0</Scale>
                  <AllowNull>N</AllowNull>
                  <PrimaryKeyPos>2</PrimaryKeyPos>
                </DBMMFields>
                <DBMMFields>
                  <FieldID>3002</FieldID>
                  <TableID>300</TableID>
                  <Name>JointSalesRepNumber</Name>
                  <Type>varchar</Type>
                  <Size>5</Size>
                  <Precision>0</Precision>
                  <Scale>0</Scale>
                  <AllowNull>N</AllowNull>
                  <PrimaryKeyPos>1</PrimaryKeyPos>
                </DBMMFields>
                <DBMMGroups>
                  <GroupID>1</GroupID>
                  <Name>Default</Name>
                  <Type>0</Type>
                </DBMMGroups>
                <DBMMReplications>
                  <ReplicationID>1</ReplicationID>
                  <GroupID>1</GroupID>
                  <Name>Target_A_Repl</Name>
                  <SrcTableID>100</SrcTableID>
                  <TrgTableID>200</TrgTableID>
                  <ReplStatus>0</ReplStatus>
                </DBMMReplications>
                <DBMMReplications>
                  <ReplicationID>2</ReplicationID>
                  <GroupID>1</GroupID>
                  <Name>Target_B_Repl</Name>
                  <SrcTableID>100</SrcTableID>
                  <TrgTableID>300</TrgTableID>
                  <ReplStatus>0</ReplStatus>
                </DBMMReplications>
                <DBMMFieldMappings>
                  <FieldMappingID>101</FieldMappingID>
                  <ReplicationID>1</ReplicationID>
                  <IsForth>Y</IsForth>
                  <TrgFieldID>2001</TrgFieldID>
                  <SrcFieldID>1001</SrcFieldID>
                </DBMMFieldMappings>
                <DBMMFieldMappings>
                  <FieldMappingID>102</FieldMappingID>
                  <ReplicationID>1</ReplicationID>
                  <IsForth>Y</IsForth>
                  <TrgFieldID>2002</TrgFieldID>
                  <SrcFieldID>1002</SrcFieldID>
                </DBMMFieldMappings>
                <DBMMFieldMappings>
                  <FieldMappingID>103</FieldMappingID>
                  <ReplicationID>2</ReplicationID>
                  <IsForth>Y</IsForth>
                  <TrgFieldID>3001</TrgFieldID>
                  <SrcFieldID>1001</SrcFieldID>
                </DBMMFieldMappings>
                <DBMMFieldMappings>
                  <FieldMappingID>104</FieldMappingID>
                  <ReplicationID>2</ReplicationID>
                  <IsForth>Y</IsForth>
                  <TrgFieldID>3002</TrgFieldID>
                  <SrcFieldID>1002</SrcFieldID>
                </DBMMFieldMappings>
              </tables>
            </metadata>
        ''')
        with open(self.xml_path, 'w', encoding='utf-8') as fh:
            fh.write(xml)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run_export(self):
        parser.args.xml_path = self.xml_path
        parser.args.output_dir = self.output_dir
        parser.args.template = self.template_path
        parser.args.include_targets = True
        parser.args.force_schemas = None

        connections, groups, chains, replications, source_to_target_schemas, field_mappings, field_id_to_name, record_id_mappings, journal_checkpoints, refresh_filters = parser.parse_xml()
        return parser.export_as_yaml(
            connections,
            groups,
            chains,
            replications,
            source_to_target_schemas,
            field_mappings,
            field_id_to_name,
            record_id_mappings,
            journal_checkpoints=journal_checkpoints,
            refresh_filters=refresh_filters,
            output_dir=self.output_dir,
            template_file=self.template_path,
        )

    def test_generates_two_yaml_files_for_two_targets(self):
        exported = self._run_export()

        # With include_targets=True, target connections also export their own YAMLs,
        # so total count is 4. We verify the 2 source-connection YAMLs that carry
        # the split for the same source table across different targets.
        target_a_yaml = os.path.join(self.output_dir, 'Source__INTERNET__Target_A.yaml')
        target_b_yaml = os.path.join(self.output_dir, 'Source__INTERNET__Target_B.yaml')
        self.assertTrue(os.path.exists(target_a_yaml), "Expected Target_A YAML to exist")
        self.assertTrue(os.path.exists(target_b_yaml), "Expected Target_B YAML to exist")

        with open(target_a_yaml, 'r', encoding='utf-8') as fh:
            data_a = yaml.safe_load(fh)
        with open(target_b_yaml, 'r', encoding='utf-8') as fh:
            data_b = yaml.safe_load(fh)

        # Both should target the same schema name but from different connections.
        self.assertEqual(data_a['INTERNET']['target'], 'Remote')
        self.assertEqual(data_b['INTERNET']['target'], 'Remote')

        # Each should contain the table.
        self.assertIn('JOINTSALESREPXREFVIEW', data_a['INTERNET']['tables']['custom'])
        self.assertIn('JOINTSALESREPXREFVIEW', data_b['INTERNET']['tables']['custom'])

    def test_field_mappings_applied_per_target(self):
        self._run_export()

        target_a_yaml = os.path.join(self.output_dir, 'Source__INTERNET__Target_A.yaml')
        target_b_yaml = os.path.join(self.output_dir, 'Source__INTERNET__Target_B.yaml')

        with open(target_a_yaml, 'r', encoding='utf-8') as fh:
            data_a = yaml.safe_load(fh)
        with open(target_b_yaml, 'r', encoding='utf-8') as fh:
            data_b = yaml.safe_load(fh)

        cols_a = {c['name']: c for c in data_a['INTERNET']['tables']['custom']['JOINTSALESREPXREFVIEW']['columns']}
        cols_b = {c['name']: c for c in data_b['INTERNET']['tables']['custom']['JOINTSALESREPXREFVIEW']['columns']}

        # Field mappings from the XML map source -> target names.
        self.assertEqual(cols_a['SalesRepNumber'].get('sourceName'), 'SALESREPNUMBER')
        self.assertEqual(cols_a['JointSalesRepNumber'].get('sourceName'), 'JOINTSALESREPNUMBER')
        self.assertEqual(cols_b['SalesRepNumber'].get('sourceName'), 'SALESREPNUMBER')
        self.assertEqual(cols_b['JointSalesRepNumber'].get('sourceName'), 'JOINTSALESREPNUMBER')

    def test_target_table_name_resolved_per_replication(self):
        self._run_export()

        target_a_yaml = os.path.join(self.output_dir, 'Source__INTERNET__Target_A.yaml')
        target_b_yaml = os.path.join(self.output_dir, 'Source__INTERNET__Target_B.yaml')

        with open(target_a_yaml, 'r', encoding='utf-8') as fh:
            data_a = yaml.safe_load(fh)
        with open(target_b_yaml, 'r', encoding='utf-8') as fh:
            data_b = yaml.safe_load(fh)

        # Both target tables have the same name in this fixture, but the
        # structure verifies the per-replication resolution path.
        self.assertEqual(data_a['INTERNET']['tables']['custom']['JOINTSALESREPXREFVIEW']['name'], 'JointSalesRepXRef')
        self.assertEqual(data_b['INTERNET']['tables']['custom']['JOINTSALESREPXREFVIEW']['name'], 'JointSalesRepXRef')

    def test_disabled_replication_isolated_from_active(self):
        # Self-contained fixture: Target_A active, Target_B disabled (status 3).
        disabled_xml = textwrap.dedent('''\
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
                  <Name>Target_A</Name>
                  <Type>1</Type>
                  <IsSource>N</IsSource>
                </DBMMConnections>
                <DBMMConnections>
                  <ConnectionID>3</ConnectionID>
                  <Name>Target_B</Name>
                  <Type>1</Type>
                  <IsSource>N</IsSource>
                </DBMMConnections>
                <DBMMSchemas>
                  <SchemaID>10</SchemaID>
                  <ConnectionID>1</ConnectionID>
                  <Name>INTERNET</Name>
                </DBMMSchemas>
                <DBMMSchemas>
                  <SchemaID>20</SchemaID>
                  <ConnectionID>2</ConnectionID>
                  <Name>Remote</Name>
                </DBMMSchemas>
                <DBMMSchemas>
                  <SchemaID>30</SchemaID>
                  <ConnectionID>3</ConnectionID>
                  <Name>Remote</Name>
                </DBMMSchemas>
                <DBMMTables>
                  <TableID>100</TableID>
                  <SchemaID>10</SchemaID>
                  <Name>JOINTSALESREPXREFVIEW</Name>
                </DBMMTables>
                <DBMMTables>
                  <TableID>200</TableID>
                  <SchemaID>20</SchemaID>
                  <Name>JointSalesRepXRef</Name>
                </DBMMTables>
                <DBMMTables>
                  <TableID>300</TableID>
                  <SchemaID>30</SchemaID>
                  <Name>JointSalesRepXRef</Name>
                </DBMMTables>
                <DBMMFields>
                  <FieldID>1001</FieldID>
                  <TableID>100</TableID>
                  <Name>SALESREPNUMBER</Name>
                  <Type>CHAR</Type>
                  <Size>5</Size>
                  <PrimaryKeyPos>0</PrimaryKeyPos>
                </DBMMFields>
                <DBMMFields>
                  <FieldID>1002</FieldID>
                  <TableID>100</TableID>
                  <Name>JOINTSALESREPNUMBER</Name>
                  <Type>CHAR</Type>
                  <Size>5</Size>
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
                  <Name>Target_A_Repl</Name>
                  <SrcTableID>100</SrcTableID>
                  <TrgTableID>200</TrgTableID>
                  <ReplStatus>0</ReplStatus>
                </DBMMReplications>
                <DBMMReplications>
                  <ReplicationID>2</ReplicationID>
                  <GroupID>1</GroupID>
                  <Name>Target_B_Repl</Name>
                  <SrcTableID>100</SrcTableID>
                  <TrgTableID>300</TrgTableID>
                  <ReplStatus>3</ReplStatus>
                </DBMMReplications>
              </tables>
            </metadata>
        ''')
        with open(self.xml_path, 'w', encoding='utf-8') as fh:
            fh.write(disabled_xml)

        self._run_export()

        target_a_yaml = os.path.join(self.output_dir, 'Source__INTERNET__Target_A.yaml')
        target_b_yaml = os.path.join(self.output_dir, 'Source__INTERNET__Target_B.yaml')

        # Target_A is active -> table should be in the custom block.
        self.assertTrue(os.path.exists(target_a_yaml))
        with open(target_a_yaml, 'r', encoding='utf-8') as fh:
            data_a = yaml.safe_load(fh)
        self.assertIn('JOINTSALESREPXREFVIEW', data_a['INTERNET']['tables']['custom'])

        # Target_B is disabled -> table should be in the disabled (commented) block.
        self.assertTrue(os.path.exists(target_b_yaml))
        with open(target_b_yaml, 'r', encoding='utf-8') as fh:
            raw_b = fh.read()
        self.assertIn('# THE FOLLOWING TABLES ARE DISABLED IN DBMOTO', raw_b)
        self.assertIn('JOINTSALESREPXREFVIEW', raw_b)


if __name__ == '__main__':
    unittest.main()
