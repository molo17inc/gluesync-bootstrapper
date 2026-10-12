#!/usr/bin/env python3
"""The MCP server describes the Gluesync 2.3 entity upsert (T17 of the alignment plan, C-8)."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SERVER = (PROJECT_ROOT / "mcp_server" / "server.py").read_text(encoding="utf-8")


class McpTextTests(unittest.TestCase):
    def test_upsert_entities_describes_grouped_entries_and_the_refusals(self):
        start = SERVER.index('name="upsert_entities"')
        description = SERVER[start:SERVER.index("inputSchema", start)]
        for fragment in ("sourceColumnIds", "targetColumnIds", "N:1", "1:N", "1:null", "exactly", "400"):
            self.assertIn(fragment, description)

    def test_docstring_names_no_missing_tool(self):
        docstring = SERVER.split('"""')[1]
        self.assertNotIn("base-code", docstring)
        self.assertIn('name="list_mapping_functions"', SERVER)
        self.assertIn('name="get_mapping_function_code"', SERVER)


if __name__ == "__main__":
    unittest.main()
