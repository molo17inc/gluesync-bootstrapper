#!/usr/bin/env python3
"""Warn when a backup has no function-collection/ but YAML uses CustomFn (Portal D).

Soft job.notes only — Import All must not hard-block (collection may already exist on pipeline).
"""

import logging
import sys
import tempfile
import unittest
from pathlib import Path

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for path in (TESTS_DIR, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from automator_app import app  # noqa: E402
import function_collection  # noqa: E402

YAML_WITH_CUSTOM_FN = """
src:
  target: tgt
  tables:
    whitelist: [CUSTOMERS]
    custom:
      CUSTOMERS:
        name: customers
        columns:
          - ID: id
          - source: NAME
            target: name
            expression:
              type: CustomFn
              functionName: convertDate
              arguments:
                - {type: SourceColumnArg, name: NAME}
"""

YAML_WITHOUT_CUSTOM_FN = """
src:
  target: tgt
  tables:
    whitelist: [CUSTOMERS]
    custom:
      CUSTOMERS:
        name: customers
        columns:
          - ID: id
          - NAME: name
"""

NOTE_SNIPPET = "backup has no function-collection/; CustomFn may refuse unless the collection is already installed on the pipeline"


def _job(udf_root):
    return app._ArchiveImport(
        contents=b"",
        agents_list=[],
        meta_by_old_id={},
        udf_root=udf_root,
    )


class MissingFunctionCollectionWarnTests(unittest.TestCase):
    def test_note_when_folder_absent_and_custom_fn(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = _job(Path(tmp))  # empty dir: no function-collection/
            ok = app._restore_imported_function_collection(
                job, "pipe.yaml", "old-1", "new-1", yaml_text=YAML_WITH_CUSTOM_FN,
            )
            self.assertTrue(ok)
            self.assertEqual(job.errors, [])
            matching = [n for n in job.notes if NOTE_SNIPPET in n]
            self.assertEqual(len(matching), 1)
            self.assertTrue(matching[0].startswith("pipe.yaml:"))

    def test_no_note_when_no_custom_fn(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = _job(Path(tmp))
            ok = app._restore_imported_function_collection(
                job, "pipe.yaml", "old-1", "new-1", yaml_text=YAML_WITHOUT_CUSTOM_FN,
            )
            self.assertTrue(ok)
            self.assertEqual(job.notes, [])

    def test_no_note_when_folder_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fc = root / function_collection.FUNCTION_COLLECTION_FOLDER
            fc.mkdir()
            (fc / "Helpers.java").write_text(
                "package x; public class Helpers { @GsFunction public static String convertDate(String s){return s;} }\n",
                encoding="utf-8",
            )
            job = _job(root)
            # Avoid hitting CoreHub: patch restore to succeed without network.
            from unittest import mock
            with mock.patch.object(function_collection, "restore_function_collection", return_value=None):
                ok = app._restore_imported_function_collection(
                    job, "pipe.yaml", "old-1", "new-1", yaml_text=YAML_WITH_CUSTOM_FN,
                )
            self.assertTrue(ok)
            self.assertFalse(any(NOTE_SNIPPET in n for n in job.notes))

    def test_planned_off_missing_folder_still_notes_custom_fn(self):
        """Plan functionCollection off + no folder + CustomFn → soft note (not hard error)."""
        import import_plan
        with tempfile.TemporaryDirectory() as tmp:
            job = _job(Path(tmp))
            # Non-empty plan that turns functionCollection off for this pipeline.
            plan = import_plan.load({
                "pipelines": {
                    "old-1": {"parts": {"functionCollection": False}},
                }
            })
            job.plan = plan
            pipeline_plan = job.plan.pipeline("old-1")
            ok = app._restore_planned_function_collection(
                job, "pipe.yaml", "old-1", "new-1", pipeline_plan, yaml_text=YAML_WITH_CUSTOM_FN,
            )
            self.assertTrue(ok)
            self.assertEqual(job.errors, [])
            self.assertTrue(any(NOTE_SNIPPET in n for n in job.notes))

    def test_helper_detects_custom_fn(self):
        self.assertTrue(app._yaml_names_custom_fn(YAML_WITH_CUSTOM_FN))
        self.assertFalse(app._yaml_names_custom_fn(YAML_WITHOUT_CUSTOM_FN))
        self.assertFalse(app._yaml_names_custom_fn(None))


if __name__ == "__main__":
    unittest.main()
