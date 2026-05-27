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

import os
import sys
import tempfile
import shutil
import unittest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parse_dbmoto_metadata_xml as parser


class TestRefreshFilterExtraction(unittest.TestCase):
    """Test RefreshFilter -> whereClause extraction using mase_meta_simplified.xml."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cls.test_xml = os.path.join(cls.repo_root, 'mase_meta_simplified.xml')
        if not os.path.exists(cls.test_xml):
            raise unittest.SkipTest("mase_meta_simplified.xml not found at repo root")

    def setUp(self):
        self.temp_output_dir = tempfile.mkdtemp()
        parser.args.xml_path = self.test_xml
        parser.args.output_dir = self.temp_output_dir
        parser.args.template = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'table-list-template-basic.yaml'
        )
        parser.args.include_targets = True
        parser.args.force_schemas = None

    def tearDown(self):
        if os.path.exists(self.temp_output_dir):
            shutil.rmtree(self.temp_output_dir)

    def test_refresh_filters_extracted_in_parse_xml(self):
        """parse_xml() should discover non-empty RefreshFilter values."""
        result = parser.parse_xml()
        refresh_filters = result[-1]

        self.assertIsInstance(refresh_filters, dict)
        self.assertGreater(len(refresh_filters), 0, "Expected at least one RefreshFilter")

        # Spot-check a known filter from the XML
        for repl_id, filt in refresh_filters.items():
            self.assertIsInstance(repl_id, str)
            self.assertIsInstance(filt, str)
            self.assertTrue(len(filt) > 0)
            # HTML entities should be decoded
            self.assertNotIn("&lt;", filt)
            self.assertNotIn("&gt;", filt)

    def test_whereClause_present_in_generated_yaml(self):
        """Generated YAML tables should contain whereClause when a RefreshFilter exists."""
        result = parser.parse_xml()
        refresh_filters = result[-1]

        parser.export_as_yaml(
            *result[:-2],
            journal_checkpoints=result[-2],
            refresh_filters=refresh_filters,
            output_dir=self.temp_output_dir
        )

        yaml_files = [f for f in os.listdir(self.temp_output_dir) if f.endswith('.yaml')]
        self.assertGreater(len(yaml_files), 0, "At least one YAML file should be generated")

        found_where = False
        for yf in yaml_files:
            with open(os.path.join(self.temp_output_dir, yf), 'r') as fh:
                content = yaml.safe_load(fh)
            for schema_name, schema_data in content.items():
                if isinstance(schema_data, dict) and 'tables' in schema_data:
                    custom = schema_data['tables'].get('custom', {})
                    for table_name, table_def in custom.items():
                        if 'whereClause' in table_def:
                            found_where = True
                            wc = table_def['whereClause']
                            self.assertIsInstance(wc, str)
                            self.assertTrue(len(wc) > 0)
                            # Must not contain XML entities
                            self.assertNotIn("&lt;", wc)
                            self.assertNotIn("&gt;", wc)

        self.assertTrue(found_where, "Expected at least one table with a whereClause in output YAML")

    def test_empty_refresh_filter_skipped(self):
        """Empty RefreshFilter values should not produce a whereClause."""
        result = parser.parse_xml()
        refresh_filters = result[-1]

        # Ensure there are blank filters in the source XML that are NOT in the dict
        for repl_id, filt in refresh_filters.items():
            self.assertNotEqual(filt.strip(), '', f"Empty filter for repl {repl_id} should be skipped")


class TestRefreshFilterUnescaping(unittest.TestCase):
    """Test that RefreshFilter values are fully unescaped (XML entities + backslash escapes)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.xml_path = os.path.join(self.temp_dir, 'test_unescape.xml')

        # Synthetic XML with both XML entities and backslash escapes in RefreshFilter
        with open(self.xml_path, 'w', encoding='utf-8') as f:
            f.write(r'<?xml version="1.0" encoding="utf-8"?>' + '\n')
            f.write(r'<metadata version="10.7.1">' + '\n')
            f.write(r'  <tables>' + '\n')
            # Connection
            f.write(r'    <DBMMConnections>' + '\n')
            f.write(r'      <ConnectionID>1</ConnectionID>' + '\n')
            f.write(r'      <Name>TestSource</Name>' + '\n')
            f.write(r'      <IsSource>Y</IsSource>' + '\n')
            f.write(r'    </DBMMConnections>' + '\n')
            # Schema
            f.write(r'    <DBMMSchemas>' + '\n')
            f.write(r'      <SchemaID>1</SchemaID>' + '\n')
            f.write(r'      <ConnectionID>1</ConnectionID>' + '\n')
            f.write(r'      <Name>TESTSCHEMA</Name>' + '\n')
            f.write(r'    </DBMMSchemas>' + '\n')
            # Table
            f.write(r'    <DBMMTables>' + '\n')
            f.write(r'      <TableID>1</TableID>' + '\n')
            f.write(r'      <SchemaID>1</SchemaID>' + '\n')
            f.write(r'      <Name>T1</Name>' + '\n')
            f.write(r'    </DBMMTables>' + '\n')
            # Field
            f.write(r'    <DBMMFields>' + '\n')
            f.write(r'      <FieldID>1</FieldID>' + '\n')
            f.write(r'      <TableID>1</TableID>' + '\n')
            f.write(r'      <Name>ID</Name>' + '\n')
            f.write(r'      <Type>INT</Type>' + '\n')
            f.write(r'      <Size>10</Size>' + '\n')
            f.write(r'      <PrimaryKeyPos>1</PrimaryKeyPos>' + '\n')
            f.write(r'    </DBMMFields>' + '\n')
            # Replication
            f.write(r'    <DBMMReplications>' + '\n')
            f.write(r'      <ReplicationID>99</ReplicationID>' + '\n')
            f.write(r'      <GroupID>0</GroupID>' + '\n')
            f.write(r'      <Name>R1</Name>' + '\n')
            f.write(r'      <SrcTableID>1</SrcTableID>' + '\n')
            f.write(r'      <TrgTableID>1</TrgTableID>' + '\n')
            f.write(r'    </DBMMReplications>' + '\n')
            # RefreshFilter with &gt;\= (XML entity + backslash escape)
            f.write(r'    <DBMMReplStatuses>' + '\n')
            f.write(r'      <ReplicationID>99</ReplicationID>' + '\n')
            f.write(r'      <Properties>RefreshFilter=(A*100+B)&gt;\=to_char(1);BlockSize=10000;</Properties>' + '\n')
            f.write(r'    </DBMMReplStatuses>' + '\n')
            # Another with &lt;\= and semicolon-in-value (\;)
            f.write(r'    <DBMMReplStatuses>' + '\n')
            f.write(r'      <ReplicationID>98</ReplicationID>' + '\n')
            f.write(r'      <Properties>RefreshFilter=X&lt;\=5\;Y&gt;\=10;BlockSize=10000;</Properties>' + '\n')
            f.write(r'    </DBMMReplStatuses>' + '\n')
            f.write(r'  </tables>' + '\n')
            f.write(r'</metadata>' + '\n')

        parser.args.xml_path = self.xml_path
        parser.args.output_dir = self.temp_dir
        parser.args.template = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'table-list-template-basic.yaml'
        )
        parser.args.include_targets = True
        parser.args.force_schemas = None

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_backslash_and_xml_entities_unescaped(self):
        r"""\= and \; inside RefreshFilter must become = and ; after parsing."""
        result = parser.parse_xml()
        refresh_filters = result[-1]

        self.assertIn('99', refresh_filters)
        self.assertEqual(refresh_filters['99'], "(A*100+B)>=to_char(1)")

        self.assertIn('98', refresh_filters)
        self.assertEqual(refresh_filters['98'], "X<=5;Y>=10")

    def test_whereClause_in_yaml_with_unescaped_operators(self):
        r"""Exported YAML must contain >= and <=, not &gt;\= or &lt;\=."""
        result = parser.parse_xml()
        refresh_filters = result[-1]

        parser.export_as_yaml(
            *result[:-2],
            journal_checkpoints=result[-2],
            refresh_filters=refresh_filters,
            output_dir=self.temp_dir
        )

        yaml_files = [f for f in os.listdir(self.temp_dir) if f.endswith('.yaml')]
        self.assertGreater(len(yaml_files), 0)

        for yf in yaml_files:
            with open(os.path.join(self.temp_dir, yf), 'r') as fh:
                content = yaml.safe_load(fh)
            for schema_name, schema_data in content.items():
                if isinstance(schema_data, dict) and 'tables' in schema_data:
                    custom = schema_data['tables'].get('custom', {})
                    for table_name, table_def in custom.items():
                        if 'whereClause' in table_def:
                            wc = table_def['whereClause']
                            self.assertNotIn("&gt;", wc)
                            self.assertNotIn("&lt;", wc)
                            self.assertNotIn("\\=", wc)
                            self.assertNotIn("\\;", wc)
                            # Should contain actual operators
                            self.assertTrue(
                                '>=' in wc or '<=' in wc,
                                f"whereClause should contain >= or <=, got: {wc}"
                            )


if __name__ == '__main__':
    unittest.main()
