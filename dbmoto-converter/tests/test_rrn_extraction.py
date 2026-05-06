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

import unittest
import sys
import os
import tempfile
import shutil
import yaml

# Add parent directory to path to import parse_dbmoto_metadata_xml
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import parse_dbmoto_metadata_xml as parser


class TestRRNExtraction(unittest.TestCase):
    """Test RRN (RecordID) extraction from DbMoto XML metadata."""

    def setUp(self):
        """Set up test fixtures."""
        self.test_data_dir = os.path.join(os.path.dirname(__file__), 'data')
        self.temp_output_dir = tempfile.mkdtemp()
        
        # Create test data directory if it doesn't exist
        os.makedirs(self.test_data_dir, exist_ok=True)
        
        # Use RRN-test-no-schema.xml from test data directory
        self.test_xml = os.path.join(self.test_data_dir, 'RRN-test-no-schema.xml')
        if not os.path.exists(self.test_xml):
            self.skipTest("RRN-test-no-schema.xml not found in test data directory")

    def tearDown(self):
        """Clean up test fixtures."""
        if os.path.exists(self.temp_output_dir):
            shutil.rmtree(self.temp_output_dir)

    def test_rrn_extraction_from_rrn_test_no_schema(self):
        """Test that RRN extraction works with RRN-test-no-schema.xml."""
        # Run the parser
        parser.args.xml_path = self.test_xml
        parser.args.output_dir = self.temp_output_dir
        parser.args.template = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'table-list-template-basic.yaml'
        )
        parser.args.include_targets = True
        parser.args.force_schemas = None
        
        # Parse and export
        connections, groups, chains, replications, source_to_target_schemas, field_mappings, field_id_to_name, record_id_mappings = parser.parse_xml()
        parser.export_as_yaml(
            connections, groups, chains, replications,
            source_to_target_schemas, field_mappings, field_id_to_name, record_id_mappings,
            output_dir=self.temp_output_dir
        )
        
        # Check that record_id_mappings was populated
        self.assertIsNotNone(record_id_mappings)
        self.assertIn('250', record_id_mappings, "Replication 250 should have RecordID mappings")
        self.assertIn('18354', record_id_mappings['250'], "Field ID 18354 (RRN) should be in RecordID mappings")
        
        # Check that the generated YAML contains the RRN column
        yaml_file = os.path.join(self.temp_output_dir, 'MaseratiTest__MASERATIF.yaml')
        self.assertTrue(os.path.exists(yaml_file), "YAML file should be generated")
        
        with open(yaml_file, 'r') as f:
            yaml_content = yaml.safe_load(f)
        
        # Check that WARDE00F table has RRN column with sourceName _RRN
        self.assertIn('MASERATIF', yaml_content)
        self.assertIn('tables', yaml_content['MASERATIF'])
        self.assertIn('custom', yaml_content['MASERATIF']['tables'])
        self.assertIn('WARDE00F', yaml_content['MASERATIF']['tables']['custom'])
        
        warde00f_config = yaml_content['MASERATIF']['tables']['custom']['WARDE00F']
        self.assertIn('columns', warde00f_config)
        
        # Find the RRN column
        rrn_column = None
        for col in warde00f_config['columns']:
            if col.get('name') == 'RRN':
                rrn_column = col
                break
        
        self.assertIsNotNone(rrn_column, "RRN column should be present")
        self.assertEqual(rrn_column.get('sourceName'), '_RRN', "RRN column should have sourceName _RRN")
        self.assertEqual(rrn_column.get('type'), 'DECIMAL', "RRN column should be DECIMAL type")
        self.assertEqual(rrn_column.get('dataLength'), 15, "RRN column should have dataLength 15")
        self.assertFalse(rrn_column.get('isNullable'), "RRN column should not be nullable")
        
        # Check that keys use _RRN (not all columns as fallback)
        self.assertIn('keys', warde00f_config)
        self.assertEqual(warde00f_config['keys'], ['_RRN'], "Keys should be ['_RRN'] when no primary keys found but RRN mapping exists")

    def test_rrn_mapping_detected_in_parse_xml(self):
        """Test that parse_xml correctly detects [!RecordID] mappings."""
        parser.args.xml_path = self.test_xml
        parser.args.output_dir = self.temp_output_dir
        
        connections, groups, chains, replications, source_to_target_schemas, field_mappings, field_id_to_name, record_id_mappings = parser.parse_xml()
        
        # Verify RecordID mapping was detected
        self.assertIsNotNone(record_id_mappings)
        self.assertIn('250', record_id_mappings)
        self.assertEqual(record_id_mappings['250']['18354'], '[!RecordID]')


if __name__ == '__main__':
    unittest.main()
