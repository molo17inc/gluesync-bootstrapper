"""K9 only on a confirmed key-less target, entities not attempted while agents are unreachable, condensed errors."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from entity_mapping import EntityMappingError, derive_keys  # noqa: E402
from automator_app import app  # noqa: E402


def rel(s, t):
    return {"sources": [s], "targets": [t], "explicit": True}


RELS = [rel("ACCOUNT_NUMBER", "ACCOUNT_NUMBER"), rel("COL0003", "COL0003"), rel("COL0004", "COL0004")]
NAMES = ["ACCOUNT_NUMBER", "COL0003", "COL0004"]


def keys(ph_t=(), target_keys=("ACCOUNT_NUMBER", "COL0003", "COL0004"), **kw):
    return derive_keys(relations=RELS, source_names=NAMES, target_names=NAMES,
                       physical_source={"ACCOUNT_NUMBER", "COL0003"}, physical_target=set(ph_t),
                       yaml_keys=[], yaml_target_keys=list(target_keys), produced_targets=set(),
                       label="SCH01.TAB001", source_is_nosql=False, **kw)


class K9Tests(unittest.TestCase):
    def test_unknown_target_key_warns_and_keeps_keys(self):
        warnings = []
        _, tk = keys(target_key_known=False, warnings=warnings)
        self.assertIn("COL0004", tk)
        self.assertTrue(any("K9 not checked" in w and "Core Hub decides" in w for w in warnings))

    def test_confirmed_keyless_target_refuses(self):
        with self.assertRaisesRegex(EntityMappingError, "K9.*COL0004"):
            keys(target_key_known=True)

    def test_strict_refuses_unknown_target(self):
        with self.assertRaisesRegex(EntityMappingError, "strict-validate"):
            keys(target_key_known=False, strict=True)

    def test_physical_target_key_exact_match_is_accepted(self):
        _, tk = keys(ph_t=NAMES)
        self.assertEqual(sorted(tk), sorted(NAMES))

    def test_physical_target_key_mismatch_names_the_key(self):
        with self.assertRaisesRegex(EntityMappingError, "R12.2.*physical primary key"):
            keys(ph_t=["ACCOUNT_NUMBER", "COL0003"])


class CondenseTests(unittest.TestCase):
    def test_same_rule_on_many_tables_is_one_line(self):
        errs = [f"SCH{n - 2:02d}.TAB001: K9: targetKeys of SCH{n - 2:02d}.TAB001 names COL0004" for n in range(3, 12)]
        errs.append("X.Y: other")
        out = app._condense_errors(errs)
        self.assertEqual(len(out), 2)
        self.assertIn("x9 tables (SCH01.TAB001, SCH02.TAB001, SCH03.TAB001, ...)", out[0])
        self.assertEqual(out[1], "X.Y: other")

    def test_unlabelled_duplicates_are_counted(self):
        self.assertEqual(app._condense_errors(["boom", "boom"]), ["boom x2"])


class NotAttemptedTests(unittest.TestCase):
    def _job(self):
        return app._ArchiveImport(contents=b"", agents_list=[], meta_by_old_id={}, udf_root=None,
                                  continue_on_credentials_failure=True)

    def test_opt_in_switch(self):
        with mock.patch.dict(os.environ, {"GS_IMPORT_ATTEMPT_ENTITIES_WHEN_PENDING": ""}):
            self.assertFalse(app._attempt_entities_when_pending())
        with mock.patch.dict(os.environ, {"GS_IMPORT_ATTEMPT_ENTITIES_WHEN_PENDING": "true"}):
            self.assertTrue(app._attempt_entities_when_pending())

    def test_synth1_report_is_short(self):
        job = self._job()
        job.created_pipelines.append({"pipelineId": "e61466b6", "pipelineName": "Synth1MSSQL"})
        app._outcome(job, "e61466b6", "Synth1MSSQL")["credentialsPending"] = True
        src = ('CoreHub request PUT /pipelines/e61466b6/agents/a1/config/credentials failed: 500 Server Error | '
               'Response: {"notificationId": 109, "exceptionMessage": "Failed to initialize pool: The application '
               'requester cannot establish the connection. (Connection refused)", "message": "Something went wrong"}')
        tgt = ('CoreHub request PUT /pipelines/e61466b6/agents/b1/config/credentials failed: 500 | Response: '
               '{"notificationId": 111, "exceptionMessage": "Failed to initialize pool: The TCP/IP connection to the '
               'host 10.17.3.164, port 1433 has failed. Error: \\"Connect timed out. Verify the connection.\\"", '
               '"message": "x"}')
        for agent_id, kind, tag, err in (("a1", "SOURCE", "ibm-iseries", src), ("b1", "TARGET", "mssql-ct", tgt)):
            app.corehub.record_credentials_pending(job.credentials_pending, "e61466b6", agent_id, kind, tag,
                                                   RuntimeError(err))
            job.credentials_pending[-1]["specificConfigurationError"] = "500 ... Impossible to change"
        job.credentials_pending[-1]["tablesNotCreated"] = True
        with mock.patch.object(app, "_yaml_table_count", return_value=58):
            app._record_entities_not_attempted(job, "Synth1MSSQL", Path("x.yaml"), [("S", "T")],
                                               "e61466b6", frozenset({"SOURCE", "TARGET"}))
        outcome = app._outcome(job, "e61466b6")
        outcome["total"] = 58
        self.assertEqual(outcome["failed"], 0)
        self.assertEqual(job.errors, [])
        self.assertEqual(app._report_lines(job), [
            "PARTIAL: Synth1MSSQL (e61466b6) - 0 of 58 entities created (58 not attempted: agents not reachable).",
            "Source ibm-iseries: can't connect to database (Connection refused) - specific configuration not saved.",
            "Target mssql-ct: can't connect to 10.17.3.164:1433 (timed out) - specific configuration not saved, "
            "target tables not created.",
            app.CREDENTIALS_PENDING_HELP,
            "Next: fix connections (overrides file or Core Hub UI), then resume with "
            "GS_IMPORT_RESUME_PIPELINE_ID=e61466b6 - or delete the pipeline.",
        ])
        self.assertEqual(app.import_status(job), "partial")

    def test_other_errors_are_grouped(self):
        job = self._job()
        job.created_pipelines.append({"pipelineId": "p", "pipelineName": "A"})
        job.errors.extend(["S.T1: refused", "S.T2: refused"])
        lines = app._report_lines(job)
        self.assertIn("refused x2 tables (S.T1, S.T2)", lines)
        self.assertTrue(lines[-1].startswith("Next: fix the errors above"))


class TableCountTests(unittest.TestCase):
    def test_whitelist_and_custom_are_one_set(self):
        import tempfile
        text = ("schemas:\n  A:\n    target: X\n    tables:\n      whitelist: [T1, T2]\n      custom:\n"
                "        T2: {}\n        T3: {}\n      blacklist: [T1]\n  B:\n    tables:\n      whitelist: [U]\n")
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write(text)
        try:
            self.assertEqual(app._yaml_table_count(Path(handle.name), [("A", "X"), ("B", "X")]), 3)
            self.assertIsNone(app._yaml_table_count(Path(handle.name), [("C", "X")]))
        finally:
            os.unlink(handle.name)


class ReasonTests(unittest.TestCase):
    def test_fallback_keeps_first_sentence(self):
        self.assertEqual(app.corehub.short_connection_reason("boom happened. more"),
                         "can't connect to database (boom happened)")


if __name__ == "__main__":
    unittest.main()
