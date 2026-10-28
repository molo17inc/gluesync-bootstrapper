#!/usr/bin/env python3
"""Adversarial backup ZIPs: several single-pipeline exports in one ZIP (each with its own agents-config.yaml),
duplicate ZIP entries, and masked passwords other than Core Hub's ``*******``. No live Core Hub calls."""

import io
import logging
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for p in (str(TESTS_DIR), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("GLUESYNC_AUTOMATOR_WORKDIR", tempfile.mkdtemp())
logging.disable(logging.CRITICAL)

from fastapi import HTTPException  # noqa: E402

from automator_app import app  # noqa: E402
import test_custom_import as T  # noqa: E402


def _agent_ids(tag):
    config = yaml.safe_load((T.FIXTURES / f"{tag}_agents-config.yaml").read_text())
    return {str(a.get("agentId")) for a in config["agents"]}


class SeveralExportsInOneZipTests(unittest.TestCase):
    def test_each_pipeline_gets_the_agents_of_its_own_export(self):
        analysis = app._analyze_archive(T.both_archives())
        by_id = {p["oldId"]: p for p in analysis["pipelines"]}
        for tag, (old_id, _name) in T.BACKUPS.items():
            with self.subTest(tag=tag):
                bound = {str(a["agentId"]) for a in by_id[old_id]["agents"]}
                self.assertTrue(bound)
                self.assertLessEqual(bound, _agent_ids(tag))

    def test_merged_config_keeps_every_agent_once(self):
        text = app._read_agents_config_text(T.both_archives(), "Import")
        agents = yaml.safe_load(text)["agents"]
        ids = [str(a.get("agentId")) for a in agents]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), _agent_ids("synth1") | _agent_ids("synth2"))

    def test_single_export_is_returned_unchanged(self):
        text = app._read_agents_config_text(T.archive("synth1"), "Import")
        self.assertEqual(text, (T.FIXTURES / "synth1_agents-config.yaml").read_text())

    def test_unparsable_second_config_is_a_400(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            with zipfile.ZipFile(io.BytesIO(T.archive("synth1"))) as src:
                for item in src.namelist():
                    zf.writestr(item, src.read(item))
            zf.writestr("pipeline_x/agents-config.yaml", "- just\n- a list\n")
        contents = buffer.getvalue()
        with self.assertRaises(HTTPException) as ctx:
            app._read_agents_config_text(contents, "Import")
        self.assertEqual(ctx.exception.status_code, 400)


class DuplicateEntryTests(unittest.TestCase):
    def test_duplicate_zip_entries_are_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            with zipfile.ZipFile(io.BytesIO(T.archive("synth1"))) as src:
                items = [(i, src.read(i)) for i in src.namelist()]
            for item, data in items + items[:1]:
                zf.writestr(item, data)
        contents = buffer.getvalue()
        with self.assertRaises(HTTPException) as ctx:
            app._read_agents_config_text(contents, "Import")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("duplicate", str(ctx.exception.detail))


class MaskedPasswordTests(unittest.TestCase):
    def test_any_run_of_asterisks_is_masked(self):
        for value in ("***", "*******", "**********", " ******* "):
            with self.subTest(value=value):
                self.assertTrue(app._is_masked(value))
        for value in ("", "**", "a*******", None, 7, "p@ss"):
            with self.subTest(value=value):
                self.assertFalse(app._is_masked(value))

    def test_nested_password_is_found(self):
        self.assertTrue(app._has_masked_password([{"hostCredentials": {"password": "*****"}}]))
        self.assertTrue(app._has_masked_password({"x": {"customHostCredentials": {"password": "***"}}}))
        self.assertFalse(app._has_masked_password([{"hostCredentials": {"password": "real"}}]))

    def test_parse_refuses_masked_unless_allowed(self):
        text = yaml.safe_dump({"agents": [{"agentId": "a", "hostCredentials": {"password": "**********"}}]})
        with self.assertRaises(HTTPException) as ctx:
            app._parse_agents_config(text, "Import")
        self.assertEqual(ctx.exception.status_code, 400)
        _config, agents = app._parse_agents_config(text, "Import", allow_masked=True)
        self.assertEqual(len(agents), 1)

    def test_password_state_reports_masked_as_missing(self):
        state = app._backup_password_state({"hostCredentials": {"password": "*****"}})
        self.assertTrue(state.startswith("missing"))


if __name__ == "__main__":
    unittest.main()
