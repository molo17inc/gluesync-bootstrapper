import os
import sys
import tempfile
import textwrap
import unittest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import parse_dbmoto_metadata_xml as parser


class TestCatalogFilename(unittest.TestCase):
    """When multiple schemas share the same name (e.g. 'dbo') but belong to
    different databases within the same connection, each must produce a
    separate output file named after source_db_target_db."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.xml_path = os.path.join(self.temp_dir.name, 'multi-db.xml')
        self.template_path = os.path.join(self.temp_dir.name, 'template.yaml')
        self.output_dir = os.path.join(self.temp_dir.name, 'out')

        # Minimal template so the parser doesn't fail
        with open(self.template_path, 'w') as f:
            yaml.dump({'schemas': {}}, f)

        xml = textwrap.dedent('''\
            <?xml version="1.0" encoding="utf-8"?>
            <metadata version="10.7.1">
              <tables>
                <!-- Source connection -->
                <DBMMConnections>
                  <ConnectionID>40</ConnectionID>
                  <Name>s_SQLServer</Name>
                  <IsSource>Y</IsSource>
                </DBMMConnections>
                <!-- Target connection -->
                <DBMMConnections>
                  <ConnectionID>37</ConnectionID>
                  <Name>t_MySql</Name>
                  <IsSource>N</IsSource>
                </DBMMConnections>

                <!-- Three catalogs (databases) on the source connection -->
                <DBMMCatalogs>
                  <CatalogID>54</CatalogID>
                  <ConnectionID>40</ConnectionID>
                  <Name>CENTRALDB</Name>
                </DBMMCatalogs>
                <DBMMCatalogs>
                  <CatalogID>55</CatalogID>
                  <ConnectionID>40</ConnectionID>
                  <Name>GOALDB</Name>
                </DBMMCatalogs>
                <DBMMCatalogs>
                  <CatalogID>57</CatalogID>
                  <ConnectionID>40</ConnectionID>
                  <Name>TESTDB</Name>
                </DBMMCatalogs>

                <!-- Target schema -->
                <DBMMSchemas>
                  <SchemaID>81</SchemaID>
                  <ConnectionID>37</ConnectionID>
                  <CatalogID>0</CatalogID>
                  <Name>replica</Name>
                </DBMMSchemas>

                <!-- Three source schemas all named 'dbo' but different CatalogIDs -->
                <DBMMSchemas>
                  <SchemaID>82</SchemaID>
                  <ConnectionID>40</ConnectionID>
                  <CatalogID>54</CatalogID>
                  <Name>dbo</Name>
                </DBMMSchemas>
                <DBMMSchemas>
                  <SchemaID>83</SchemaID>
                  <ConnectionID>40</ConnectionID>
                  <CatalogID>55</CatalogID>
                  <Name>dbo</Name>
                </DBMMSchemas>
                <DBMMSchemas>
                  <SchemaID>85</SchemaID>
                  <ConnectionID>40</ConnectionID>
                  <CatalogID>57</CatalogID>
                  <Name>dbo</Name>
                </DBMMSchemas>

                <!-- Source tables: one per schema -->
                <DBMMTables>
                  <TableID>300</TableID>
                  <SchemaID>82</SchemaID>
                  <Name>TABLE_CENTRAL</Name>
                </DBMMTables>
                <DBMMTables>
                  <TableID>400</TableID>
                  <SchemaID>83</SchemaID>
                  <Name>TABLE_GOAL</Name>
                </DBMMTables>
                <DBMMTables>
                  <TableID>500</TableID>
                  <SchemaID>85</SchemaID>
                  <Name>TABLE_TEST</Name>
                </DBMMTables>

                <!-- Target tables -->
                <DBMMTables>
                  <TableID>301</TableID>
                  <SchemaID>81</SchemaID>
                  <Name>TABLE_CENTRAL</Name>
                </DBMMTables>
                <DBMMTables>
                  <TableID>401</TableID>
                  <SchemaID>81</SchemaID>
                  <Name>TABLE_GOAL</Name>
                </DBMMTables>
                <DBMMTables>
                  <TableID>501</TableID>
                  <SchemaID>81</SchemaID>
                  <Name>TABLE_TEST</Name>
                </DBMMTables>

                <!-- Fields for each source table -->
                <DBMMFields>
                  <FieldID>3001</FieldID>
                  <TableID>300</TableID>
                  <Name>ID</Name>
                  <Type>INT</Type>
                  <Size>4</Size>
                  <Precision>10</Precision>
                  <Scale>0</Scale>
                  <AllowNull>N</AllowNull>
                  <PrimaryKeyPos>1</PrimaryKeyPos>
                </DBMMFields>
                <DBMMFields>
                  <FieldID>4001</FieldID>
                  <TableID>400</TableID>
                  <Name>ID</Name>
                  <Type>INT</Type>
                  <Size>4</Size>
                  <Precision>10</Precision>
                  <Scale>0</Scale>
                  <AllowNull>N</AllowNull>
                  <PrimaryKeyPos>1</PrimaryKeyPos>
                </DBMMFields>
                <DBMMFields>
                  <FieldID>5001</FieldID>
                  <TableID>500</TableID>
                  <Name>ID</Name>
                  <Type>INT</Type>
                  <Size>4</Size>
                  <Precision>10</Precision>
                  <Scale>0</Scale>
                  <AllowNull>N</AllowNull>
                  <PrimaryKeyPos>1</PrimaryKeyPos>
                </DBMMFields>

                <!-- Fields for target tables -->
                <DBMMFields>
                  <FieldID>3011</FieldID>
                  <TableID>301</TableID>
                  <Name>ID</Name>
                  <Type>INT</Type>
                  <Size>4</Size>
                  <Precision>10</Precision>
                  <Scale>0</Scale>
                  <AllowNull>N</AllowNull>
                  <PrimaryKeyPos>1</PrimaryKeyPos>
                </DBMMFields>
                <DBMMFields>
                  <FieldID>4011</FieldID>
                  <TableID>401</TableID>
                  <Name>ID</Name>
                  <Type>INT</Type>
                  <Size>4</Size>
                  <Precision>10</Precision>
                  <Scale>0</Scale>
                  <AllowNull>N</AllowNull>
                  <PrimaryKeyPos>1</PrimaryKeyPos>
                </DBMMFields>
                <DBMMFields>
                  <FieldID>5011</FieldID>
                  <TableID>501</TableID>
                  <Name>ID</Name>
                  <Type>INT</Type>
                  <Size>4</Size>
                  <Precision>10</Precision>
                  <Scale>0</Scale>
                  <AllowNull>N</AllowNull>
                  <PrimaryKeyPos>1</PrimaryKeyPos>
                </DBMMFields>

                <!-- Replications linking each source table to its target -->
                <DBMMReplications>
                  <ReplicationID>10</ReplicationID>
                  <GroupID>1</GroupID>
                  <Name>REPL_CENTRAL</Name>
                  <SrcTableID>300</SrcTableID>
                  <TrgTableID>301</TrgTableID>
                  <ReplStatus>0</ReplStatus>
                  <Properties></Properties>
                </DBMMReplications>
                <DBMMReplications>
                  <ReplicationID>20</ReplicationID>
                  <GroupID>1</GroupID>
                  <Name>REPL_GOAL</Name>
                  <SrcTableID>400</SrcTableID>
                  <TrgTableID>401</TrgTableID>
                  <ReplStatus>0</ReplStatus>
                  <Properties></Properties>
                </DBMMReplications>
                <DBMMReplications>
                  <ReplicationID>30</ReplicationID>
                  <GroupID>1</GroupID>
                  <Name>REPL_TEST</Name>
                  <SrcTableID>500</SrcTableID>
                  <TrgTableID>501</TrgTableID>
                  <ReplStatus>0</ReplStatus>
                  <Properties></Properties>
                </DBMMReplications>

                <!-- A group so replications are valid -->
                <DBMMGroups>
                  <GroupID>1</GroupID>
                  <Name>Default</Name>
                  <Type>0</Type>
                </DBMMGroups>
              </tables>
            </metadata>
        ''')
        with open(self.xml_path, 'w', encoding='utf-8') as fh:
            fh.write(xml)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_multiple_dbo_schemas_produce_distinct_files(self):
        parser.args.xml_path = self.xml_path
        parser.args.output_dir = self.output_dir
        parser.args.template = self.template_path
        parser.args.include_targets = True
        parser.args.force_schemas = None

        connections, groups, chains, replications, source_to_target_schemas, \
            field_mappings, field_id_to_name, record_id_mappings, \
            journal_checkpoints, refresh_filters = parser.parse_xml()

        exported = parser.export_as_yaml(
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

        # 3 source schemas + 1 target schema = 4 files
        self.assertEqual(exported, 4)

        # Each source schema should produce a file named source_db_target_db.yaml
        central_file = os.path.join(self.output_dir, 'CENTRALDB_replica.yaml')
        goal_file = os.path.join(self.output_dir, 'GOALDB_replica.yaml')
        test_file = os.path.join(self.output_dir, 'TESTDB_replica.yaml')

        self.assertTrue(os.path.exists(central_file),
                        'Expected CENTRALDB_replica.yaml to exist')
        self.assertTrue(os.path.exists(goal_file),
                        'Expected GOALDB_replica.yaml to exist')
        self.assertTrue(os.path.exists(test_file),
                        'Expected TESTDB_replica.yaml to exist')

        # Verify each file contains only its own table
        with open(central_file, 'r', encoding='utf-8') as fh:
            central_data = yaml.safe_load(fh)
        with open(goal_file, 'r', encoding='utf-8') as fh:
            goal_data = yaml.safe_load(fh)
        with open(test_file, 'r', encoding='utf-8') as fh:
            test_data = yaml.safe_load(fh)

        central_tables = central_data['dbo']['tables']['custom']
        goal_tables = goal_data['dbo']['tables']['custom']
        test_tables = test_data['dbo']['tables']['custom']

        self.assertIn('TABLE_CENTRAL', central_tables)
        self.assertNotIn('TABLE_GOAL', central_tables)
        self.assertNotIn('TABLE_TEST', central_tables)

        self.assertIn('TABLE_GOAL', goal_tables)
        self.assertNotIn('TABLE_CENTRAL', goal_tables)
        self.assertNotIn('TABLE_TEST', goal_tables)

        self.assertIn('TABLE_TEST', test_tables)
        self.assertNotIn('TABLE_CENTRAL', test_tables)
        self.assertNotIn('TABLE_GOAL', test_tables)

    def test_catalog_name_stored_on_schema(self):
        parser.args.xml_path = self.xml_path
        parser.args.output_dir = self.output_dir
        parser.args.template = self.template_path
        parser.args.include_targets = True
        parser.args.force_schemas = None

        connections, groups, chains, replications, source_to_target_schemas, \
            field_mappings, field_id_to_name, record_id_mappings, \
            journal_checkpoints, refresh_filters = parser.parse_xml()

        # Verify catalog_name is resolved on each source schema
        source_conn = connections['40']
        schemas_by_id = source_conn['schemas']

        self.assertEqual(schemas_by_id['82']['catalog_name'], 'CENTRALDB')
        self.assertEqual(schemas_by_id['83']['catalog_name'], 'GOALDB')
        self.assertEqual(schemas_by_id['85']['catalog_name'], 'TESTDB')

        # Target schema has no catalog (CatalogID=0)
        target_conn = connections['37']
        target_schema = list(target_conn['schemas'].values())[0]
        self.assertIsNone(target_schema['catalog_name'])


if __name__ == '__main__':
    unittest.main()
