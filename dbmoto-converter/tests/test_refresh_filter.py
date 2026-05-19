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
            *result[:-1],
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


if __name__ == '__main__':
    unittest.main()
