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

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parse_dbmoto_metadata_xml as parser


class TestJournalCheckpointExtraction(unittest.TestCase):
    """Unit tests for journal checkpoint extraction and file generation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.xml_path = os.path.join(self.temp_dir, 'test_journal.xml')
        with open(self.xml_path, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="utf-8"?>\n')
            f.write('<metadata version="10.7.1">\n')
            f.write('  <tables>\n')
            f.write('    <DBMMConnections>\n')
            f.write('      <ConnectionID>1</ConnectionID>\n')
            f.write('      <Name>TestSource</Name>\n')
            f.write('      <IsSource>Y</IsSource>\n')
            f.write('    </DBMMConnections>\n')
            f.write('    <DBMMSchemas>\n')
            f.write('      <SchemaID>1</SchemaID>\n')
            f.write('      <ConnectionID>1</ConnectionID>\n')
            f.write('      <Name>TESTSCHEMA</Name>\n')
            f.write('    </DBMMSchemas>\n')
            f.write('    <DBMMTables>\n')
            f.write('      <TableID>1</TableID>\n')
            f.write('      <SchemaID>1</SchemaID>\n')
            f.write('      <Name>T1</Name>\n')
            f.write('    </DBMMTables>\n')
            f.write('    <DBMMFields>\n')
            f.write('      <FieldID>1</FieldID>\n')
            f.write('      <TableID>1</TableID>\n')
            f.write('      <Name>ID</Name>\n')
            f.write('      <Type>INT</Type>\n')
            f.write('      <Size>10</Size>\n')
            f.write('      <PrimaryKeyPos>1</PrimaryKeyPos>\n')
            f.write('    </DBMMFields>\n')
            f.write('    <DBMMReplications>\n')
            f.write('      <ReplicationID>1</ReplicationID>\n')
            f.write('      <GroupID>0</GroupID>\n')
            f.write('      <Name>R1</Name>\n')
            f.write('      <SrcTableID>1</SrcTableID>\n')
            f.write('      <TrgTableID>1</TrgTableID>\n')
            f.write('    </DBMMReplications>\n')
            f.write('    <DBMMReplStatuses>\n')
            f.write('      <ReplicationID>1</ReplicationID>\n')
            f.write('      <TransactionID>123</TransactionID>\n')
            f.write('      <TransactionTS>123456789</TransactionTS>\n')
            f.write('      <Properties>ReceiverName=RCV001;ReceiverLibrary=LIB1;JournalLibrary=LIB1;JournalName=JRN001;</Properties>\n')
            f.write('    </DBMMReplStatuses>\n')
            f.write('  </tables>\n')
            f.write('</metadata>\n')

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

    def test_journal_checkpoints_extracted_in_parse_xml(self):
        """parse_xml() should discover journal checkpoints with correct data."""
        result = parser.parse_xml()
        journal_checkpoints = result[-2]

        self.assertIsInstance(journal_checkpoints, dict)
        self.assertIn('TESTSCHEMA', journal_checkpoints)
        self.assertIn('JRN001', journal_checkpoints['TESTSCHEMA'])

        cp = journal_checkpoints['TESTSCHEMA']['JRN001']
        self.assertEqual(cp['journalLibrary'], 'LIB1')
        self.assertEqual(cp['journalName'], 'JRN001')
        self.assertEqual(cp['receiverLibrary'], 'LIB1')
        self.assertEqual(cp['receiverName'], 'RCV001')
        self.assertEqual(cp['sequenceNumber'], '00000000000000000123')
        self.assertEqual(cp['timestamp'], 123456789)

    def test_no_checkpoint_without_journal_properties(self):
        """If DBMMReplStatuses lacks ReceiverName/JournalName etc, no checkpoint should be created."""
        xml_no_journal = os.path.join(self.temp_dir, 'no_journal.xml')
        with open(xml_no_journal, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="utf-8"?>\n')
            f.write('<metadata version="10.7.1">\n')
            f.write('  <tables>\n')
            f.write('    <DBMMConnections>\n')
            f.write('      <ConnectionID>1</ConnectionID>\n')
            f.write('      <Name>TestSource</Name>\n')
            f.write('      <IsSource>Y</IsSource>\n')
            f.write('    </DBMMConnections>\n')
            f.write('    <DBMMSchemas>\n')
            f.write('      <SchemaID>1</SchemaID>\n')
            f.write('      <ConnectionID>1</ConnectionID>\n')
            f.write('      <Name>TESTSCHEMA</Name>\n')
            f.write('    </DBMMSchemas>\n')
            f.write('    <DBMMTables>\n')
            f.write('      <TableID>1</TableID>\n')
            f.write('      <SchemaID>1</SchemaID>\n')
            f.write('      <Name>T1</Name>\n')
            f.write('    </DBMMTables>\n')
            f.write('    <DBMMFields>\n')
            f.write('      <FieldID>1</FieldID>\n')
            f.write('      <TableID>1</TableID>\n')
            f.write('      <Name>ID</Name>\n')
            f.write('      <Type>INT</Type>\n')
            f.write('      <Size>10</Size>\n')
            f.write('      <PrimaryKeyPos>1</PrimaryKeyPos>\n')
            f.write('    </DBMMFields>\n')
            f.write('    <DBMMReplications>\n')
            f.write('      <ReplicationID>1</ReplicationID>\n')
            f.write('      <GroupID>0</GroupID>\n')
            f.write('      <Name>R1</Name>\n')
            f.write('      <SrcTableID>1</SrcTableID>\n')
            f.write('      <TrgTableID>1</TrgTableID>\n')
            f.write('    </DBMMReplications>\n')
            f.write('    <DBMMReplStatuses>\n')
            f.write('      <ReplicationID>1</ReplicationID>\n')
            f.write('      <Properties>RefreshFilter=1=1;</Properties>\n')
            f.write('    </DBMMReplStatuses>\n')
            f.write('  </tables>\n')
            f.write('</metadata>\n')

        parser.args.xml_path = xml_no_journal
        result = parser.parse_xml()
        journal_checkpoints = result[-2]

        self.assertEqual(journal_checkpoints, {})

    def test_checkpoint_file_created_by_export(self):
        """export_as_yaml() should write the .cp file for each discovered checkpoint."""
        result = parser.parse_xml()

        parser.export_as_yaml(
            *result[:-2],
            journal_checkpoints=result[-2],
            refresh_filters=result[-1],
            output_dir=self.temp_dir
        )

        cp_path = os.path.join(self.temp_dir, 'TESTSCHEMA', 'JRN001.cp')
        self.assertTrue(os.path.exists(cp_path))

        with open(cp_path, 'r') as fh:
            content = json.load(fh)

        self.assertEqual(content['journalLibrary'], 'LIB1')
        self.assertEqual(content['journalName'], 'JRN001')
        self.assertEqual(content['receiverLibrary'], 'LIB1')
        self.assertEqual(content['receiverName'], 'RCV001')
        self.assertEqual(content['sequenceNumber'], '00000000000000000123')
        self.assertEqual(content['timestamp'], 123456789)

    def test_sequence_number_zero_padded(self):
        """TransactionID must be padded to 20 digits."""
        xml_short = os.path.join(self.temp_dir, 'short_seq.xml')
        with open(xml_short, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="utf-8"?>\n')
            f.write('<metadata version="10.7.1">\n')
            f.write('  <tables>\n')
            f.write('    <DBMMConnections>\n')
            f.write('      <ConnectionID>1</ConnectionID>\n')
            f.write('      <Name>TestSource</Name>\n')
            f.write('      <IsSource>Y</IsSource>\n')
            f.write('    </DBMMConnections>\n')
            f.write('    <DBMMSchemas>\n')
            f.write('      <SchemaID>1</SchemaID>\n')
            f.write('      <ConnectionID>1</ConnectionID>\n')
            f.write('      <Name>TESTSCHEMA</Name>\n')
            f.write('    </DBMMSchemas>\n')
            f.write('    <DBMMTables>\n')
            f.write('      <TableID>1</TableID>\n')
            f.write('      <SchemaID>1</SchemaID>\n')
            f.write('      <Name>T1</Name>\n')
            f.write('    </DBMMTables>\n')
            f.write('    <DBMMFields>\n')
            f.write('      <FieldID>1</FieldID>\n')
            f.write('      <TableID>1</TableID>\n')
            f.write('      <Name>ID</Name>\n')
            f.write('      <Type>INT</Type>\n')
            f.write('      <Size>10</Size>\n')
            f.write('      <PrimaryKeyPos>1</PrimaryKeyPos>\n')
            f.write('    </DBMMFields>\n')
            f.write('    <DBMMReplications>\n')
            f.write('      <ReplicationID>1</ReplicationID>\n')
            f.write('      <GroupID>0</GroupID>\n')
            f.write('      <Name>R1</Name>\n')
            f.write('      <SrcTableID>1</SrcTableID>\n')
            f.write('      <TrgTableID>1</TrgTableID>\n')
            f.write('    </DBMMReplications>\n')
            f.write('    <DBMMReplStatuses>\n')
            f.write('      <ReplicationID>1</ReplicationID>\n')
            f.write('      <TransactionID>7</TransactionID>\n')
            f.write('      <TransactionTS>999</TransactionTS>\n')
            f.write('      <Properties>ReceiverName=R;ReceiverLibrary=L;JournalLibrary=L;JournalName=J;</Properties>\n')
            f.write('    </DBMMReplStatuses>\n')
            f.write('  </tables>\n')
            f.write('</metadata>\n')

        parser.args.xml_path = xml_short
        result = parser.parse_xml()
        journal_checkpoints = result[-2]

        self.assertEqual(journal_checkpoints['TESTSCHEMA']['J']['sequenceNumber'], '00000000000000000007')

    def test_checkpoint_written_without_yaml_export(self):
        """If a schema has a checkpoint but no tables with fields, the .cp file should still be written."""
        xml_no_fields = os.path.join(self.temp_dir, 'no_fields.xml')
        with open(xml_no_fields, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="utf-8"?>\n')
            f.write('<metadata version="10.7.1">\n')
            f.write('  <tables>\n')
            f.write('    <DBMMConnections>\n')
            f.write('      <ConnectionID>1</ConnectionID>\n')
            f.write('      <Name>TestSource</Name>\n')
            f.write('      <IsSource>Y</IsSource>\n')
            f.write('    </DBMMConnections>\n')
            f.write('    <DBMMSchemas>\n')
            f.write('      <SchemaID>1</SchemaID>\n')
            f.write('      <ConnectionID>1</ConnectionID>\n')
            f.write('      <Name>EMPTYSCHEMA</Name>\n')
            f.write('    </DBMMSchemas>\n')
            f.write('    <DBMMTables>\n')
            f.write('      <TableID>1</TableID>\n')
            f.write('      <SchemaID>1</SchemaID>\n')
            f.write('      <Name>T1</Name>\n')
            f.write('    </DBMMTables>\n')
            f.write('    <DBMMReplications>\n')
            f.write('      <ReplicationID>99</ReplicationID>\n')
            f.write('      <GroupID>0</GroupID>\n')
            f.write('      <Name>R1</Name>\n')
            f.write('      <SrcTableID>1</SrcTableID>\n')
            f.write('      <TrgTableID>1</TrgTableID>\n')
            f.write('    </DBMMReplications>\n')
            f.write('    <DBMMReplStatuses>\n')
            f.write('      <ReplicationID>99</ReplicationID>\n')
            f.write('      <TransactionID>42</TransactionID>\n')
            f.write('      <TransactionTS>123456789</TransactionTS>\n')
            f.write('      <Properties>ReceiverName=RCV001;ReceiverLibrary=LIB1;JournalLibrary=LIB1;JournalName=JRN001;</Properties>\n')
            f.write('    </DBMMReplStatuses>\n')
            f.write('  </tables>\n')
            f.write('</metadata>\n')

        parser.args.xml_path = xml_no_fields
        result = parser.parse_xml()

        parser.export_as_yaml(
            *result[:-2],
            journal_checkpoints=result[-2],
            refresh_filters=result[-1],
            output_dir=self.temp_dir
        )

        cp_path = os.path.join(self.temp_dir, 'EMPTYSCHEMA', 'JRN001.cp')
        self.assertTrue(os.path.exists(cp_path), f"Checkpoint file should be written even without fields")

        with open(cp_path, 'r') as fh:
            content = json.load(fh)
        self.assertEqual(content['sequenceNumber'], '00000000000000000042')


class TestJournalCheckpointGoldenSelection(unittest.TestCase):
    """Test that only the checkpoint with the most remote (largest) timestamp is kept per journal."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.xml_path = os.path.join(self.temp_dir, 'test_golden.xml')

        with open(self.xml_path, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="utf-8"?>\n')
            f.write('<metadata version="10.7.1">\n')
            f.write('  <tables>\n')
            f.write('    <DBMMConnections>\n')
            f.write('      <ConnectionID>1</ConnectionID>\n')
            f.write('      <Name>TestSource</Name>\n')
            f.write('      <IsSource>Y</IsSource>\n')
            f.write('    </DBMMConnections>\n')
            f.write('    <DBMMSchemas>\n')
            f.write('      <SchemaID>1</SchemaID>\n')
            f.write('      <ConnectionID>1</ConnectionID>\n')
            f.write('      <Name>TESTSCHEMA</Name>\n')
            f.write('    </DBMMSchemas>\n')
            f.write('    <DBMMTables>\n')
            f.write('      <TableID>1</TableID>\n')
            f.write('      <SchemaID>1</SchemaID>\n')
            f.write('      <Name>T1</Name>\n')
            f.write('    </DBMMTables>\n')
            f.write('    <DBMMFields>\n')
            f.write('      <FieldID>1</FieldID>\n')
            f.write('      <TableID>1</TableID>\n')
            f.write('      <Name>ID</Name>\n')
            f.write('      <Type>INT</Type>\n')
            f.write('      <Size>10</Size>\n')
            f.write('      <PrimaryKeyPos>1</PrimaryKeyPos>\n')
            f.write('    </DBMMFields>\n')
            f.write('    <DBMMReplications>\n')
            f.write('      <ReplicationID>1</ReplicationID>\n')
            f.write('      <GroupID>0</GroupID>\n')
            f.write('      <Name>R1</Name>\n')
            f.write('      <SrcTableID>1</SrcTableID>\n')
            f.write('      <TrgTableID>1</TrgTableID>\n')
            f.write('    </DBMMReplications>\n')
            f.write('    <DBMMReplStatuses>\n')
            f.write('      <ReplicationID>1</ReplicationID>\n')
            f.write('      <TransactionID>10</TransactionID>\n')
            f.write('      <TransactionTS>100000000000000000</TransactionTS>\n')
            f.write('      <Properties>ReceiverName=RCV_OLD;ReceiverLibrary=OLD;JournalLibrary=OLD;JournalName=JRN001;</Properties>\n')
            f.write('    </DBMMReplStatuses>\n')
            f.write('    <DBMMReplStatuses>\n')
            f.write('      <ReplicationID>1</ReplicationID>\n')
            f.write('      <TransactionID>99</TransactionID>\n')
            f.write('      <TransactionTS>900000000000000000</TransactionTS>\n')
            f.write('      <Properties>ReceiverName=RCV_NEW;ReceiverLibrary=NEW;JournalLibrary=NEW;JournalName=JRN001;</Properties>\n')
            f.write('    </DBMMReplStatuses>\n')
            f.write('  </tables>\n')
            f.write('</metadata>\n')

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

    def test_most_remote_timestamp_wins(self):
        """When multiple checkpoints exist for the same journal, the one with the largest timestamp must be kept."""
        result = parser.parse_xml()
        journal_checkpoints = result[-2]

        self.assertIn('TESTSCHEMA', journal_checkpoints)
        self.assertIn('JRN001', journal_checkpoints['TESTSCHEMA'])

        cp = journal_checkpoints['TESTSCHEMA']['JRN001']
        self.assertEqual(cp['receiverName'], 'RCV_NEW')
        self.assertEqual(cp['receiverLibrary'], 'NEW')
        self.assertEqual(cp['journalLibrary'], 'NEW')
        self.assertEqual(cp['sequenceNumber'], '00000000000000000099')
        self.assertEqual(cp['timestamp'], 900000000000000000)

    def test_single_checkpoint_written_to_disk(self):
        """Only the golden checkpoint file should be written, no duplicates."""
        result = parser.parse_xml()

        parser.export_as_yaml(
            *result[:-2],
            journal_checkpoints=result[-2],
            refresh_filters=result[-1],
            output_dir=self.temp_dir
        )

        cp_path = os.path.join(self.temp_dir, 'TESTSCHEMA', 'JRN001.cp')
        self.assertTrue(os.path.exists(cp_path))

        with open(cp_path, 'r') as fh:
            content = json.load(fh)

        self.assertEqual(content['receiverName'], 'RCV_NEW')
        self.assertEqual(content['timestamp'], 900000000000000000)

    def test_different_journals_are_independent(self):
        """Different journal names within the same schema should not interfere."""
        xml_two_journals = os.path.join(self.temp_dir, 'two_journals.xml')
        with open(xml_two_journals, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="utf-8"?>\n')
            f.write('<metadata version="10.7.1">\n')
            f.write('  <tables>\n')
            f.write('    <DBMMConnections>\n')
            f.write('      <ConnectionID>1</ConnectionID>\n')
            f.write('      <Name>TestSource</Name>\n')
            f.write('      <IsSource>Y</IsSource>\n')
            f.write('    </DBMMConnections>\n')
            f.write('    <DBMMSchemas>\n')
            f.write('      <SchemaID>1</SchemaID>\n')
            f.write('      <ConnectionID>1</ConnectionID>\n')
            f.write('      <Name>MIXED</Name>\n')
            f.write('    </DBMMSchemas>\n')
            f.write('    <DBMMTables>\n')
            f.write('      <TableID>1</TableID>\n')
            f.write('      <SchemaID>1</SchemaID>\n')
            f.write('      <Name>T1</Name>\n')
            f.write('    </DBMMTables>\n')
            f.write('    <DBMMFields>\n')
            f.write('      <FieldID>1</FieldID>\n')
            f.write('      <TableID>1</TableID>\n')
            f.write('      <Name>ID</Name>\n')
            f.write('      <Type>INT</Type>\n')
            f.write('      <Size>10</Size>\n')
            f.write('      <PrimaryKeyPos>1</PrimaryKeyPos>\n')
            f.write('    </DBMMFields>\n')
            f.write('    <DBMMReplications>\n')
            f.write('      <ReplicationID>1</ReplicationID>\n')
            f.write('      <GroupID>0</GroupID>\n')
            f.write('      <Name>R1</Name>\n')
            f.write('      <SrcTableID>1</SrcTableID>\n')
            f.write('      <TrgTableID>1</TrgTableID>\n')
            f.write('    </DBMMReplications>\n')
            f.write('    <DBMMReplStatuses>\n')
            f.write('      <ReplicationID>1</ReplicationID>\n')
            f.write('      <TransactionID>1</TransactionID>\n')
            f.write('      <TransactionTS>100</TransactionTS>\n')
            f.write('      <Properties>ReceiverName=A_OLD;ReceiverLibrary=LIB;JournalLibrary=LIB;JournalName=JRNA;</Properties>\n')
            f.write('    </DBMMReplStatuses>\n')
            f.write('    <DBMMReplStatuses>\n')
            f.write('      <ReplicationID>1</ReplicationID>\n')
            f.write('      <TransactionID>2</TransactionID>\n')
            f.write('      <TransactionTS>200</TransactionTS>\n')
            f.write('      <Properties>ReceiverName=A_NEW;ReceiverLibrary=LIB;JournalLibrary=LIB;JournalName=JRNA;</Properties>\n')
            f.write('    </DBMMReplStatuses>\n')
            f.write('    <DBMMReplStatuses>\n')
            f.write('      <ReplicationID>1</ReplicationID>\n')
            f.write('      <TransactionID>5</TransactionID>\n')
            f.write('      <TransactionTS>500</TransactionTS>\n')
            f.write('      <Properties>ReceiverName=B_ONLY;ReceiverLibrary=LIB;JournalLibrary=LIB;JournalName=JRNB;</Properties>\n')
            f.write('    </DBMMReplStatuses>\n')
            f.write('  </tables>\n')
            f.write('</metadata>\n')

        parser.args.xml_path = xml_two_journals
        result = parser.parse_xml()
        journal_checkpoints = result[-2]

        self.assertIn('MIXED', journal_checkpoints)
        self.assertEqual(len(journal_checkpoints['MIXED']), 2)

        self.assertEqual(journal_checkpoints['MIXED']['JRNA']['receiverName'], 'A_NEW')
        self.assertEqual(journal_checkpoints['MIXED']['JRNA']['timestamp'], 200)

        self.assertEqual(journal_checkpoints['MIXED']['JRNB']['receiverName'], 'B_ONLY')
        self.assertEqual(journal_checkpoints['MIXED']['JRNB']['timestamp'], 500)


class TestJournalCheckpointIntegration(unittest.TestCase):
    """Integration test using the real as400-postgres.xml sample."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cls.test_xml = os.path.join(
            cls.repo_root,
            'dbmoto-converter',
            'tests',
            'fixtures',
            'as400-postgres.xml'
        )
        if not os.path.exists(cls.test_xml):
            cls.test_xml = '/Users/danieleangeli/Library/Containers/com.mimestream.Mimestream/Data/Library/Mail Downloads/Attachments/p58449/as400-postgres.xml'
        if not os.path.exists(cls.test_xml):
            raise unittest.SkipTest("as400-postgres.xml fixture not found")

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

    def test_as400_sample_creates_demo_checkpoint(self):
        """The real AS400 sample must produce DEMO/QSQJRN.cp with expected values."""
        result = parser.parse_xml()
        journal_checkpoints = result[-2]

        self.assertIn('DEMO', journal_checkpoints)
        self.assertIn('QSQJRN', journal_checkpoints['DEMO'])

        cp = journal_checkpoints['DEMO']['QSQJRN']
        self.assertEqual(cp['receiverName'], 'QSQJRN0061')
        self.assertEqual(cp['receiverLibrary'], 'DEMO')
        self.assertEqual(cp['journalLibrary'], 'DEMO')
        self.assertEqual(cp['journalName'], 'QSQJRN')
        self.assertEqual(cp['sequenceNumber'], '00000000000000000013')
        self.assertEqual(cp['timestamp'], 639149856572246400)

        parser.export_as_yaml(
            *result[:-2],
            journal_checkpoints=result[-2],
            refresh_filters=result[-1],
            output_dir=self.temp_output_dir
        )

        cp_path = os.path.join(self.temp_output_dir, 'DEMO', 'QSQJRN.cp')
        self.assertTrue(os.path.exists(cp_path))

        with open(cp_path, 'r') as fh:
            content = json.load(fh)
        self.assertEqual(content['sequenceNumber'], '00000000000000000013')


if __name__ == '__main__':
    unittest.main()
