"""Offline assertions documenting silent data-loss paths (see SILENT_DATA_LOSS.md)."""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import create_all_entities as cae  # noqa: E402
import export_template_from_corehub as exp  # noqa: E402


class TestSilentLossDocumented:
    def test_snapshot_write_method_only_logged_on_import(self):
        """S1 by design: helper logs SWM and never mutates an entity payload."""
        src = inspect.getsource(cae._log_snapshot_write_method)
        assert "snapshotWriteMethod" in src
        assert "logger" in src
        # Must not assign into a payload/body inside the logger helper
        assert "target_entity" not in src
        assert "fetch_core_hub" not in src
        assert "insertConflictStrategy" not in src

    def test_s1_no_insert_conflict_strategy_wiring_in_bootstrapper(self):
        """S1 by design: insertConflictStrategy is unrelated; bootstrapper must not map SWM to it."""
        for mod in (cae, exp):
            text = Path(mod.__file__).read_text(encoding="utf-8")
            assert "insertConflictStrategy" not in text, (
                f"{mod.__name__} unexpectedly references insertConflictStrategy; "
                "if intentional, update SILENT_DATA_LOSS.md S1 and wire SWM explicitly"
            )

    def test_export_lifts_snapshot_commands_from_entity_type(self):
        """S2 fixed: export lifts pre/postSnapshotCommand from entityType (hub-empty customProperties)."""
        src = inspect.getsource(exp._target_custom_properties)
        assert "preSnapshotCommand" in src
        assert "postSnapshotCommand" in src
        assert "snapshotWritingConcurrency" in src
        # Behavioural: empty AE customProperties + commands on entityType → YAML target cp
        lifted = exp._target_custom_properties(
            {"customProperties": {}},
            {
                "preSnapshotCommand": "DELETE FROM tgt.t",
                "postSnapshotCommand": "ANALYZE tgt.t",
                "snapshotWritingConcurrency": 1,
            },
        )
        assert lifted["preSnapshotCommand"] == "DELETE FROM tgt.t"
        assert lifted["postSnapshotCommand"] == "ANALYZE tgt.t"
        # Empty / whitespace omitted (pre-2.3 / hub noise)
        empty = exp._target_custom_properties(
            {"customProperties": {}},
            {"preSnapshotCommand": "  ", "postSnapshotCommand": ""},
        )
        assert "preSnapshotCommand" not in empty
        assert "postSnapshotCommand" not in empty
        # AE customProperties wins when already present (do not overwrite)
        kept = exp._target_custom_properties(
            {"customProperties": {"preSnapshotCommand": "FROM_AE"}},
            {"preSnapshotCommand": "FROM_ET"},
        )
        assert kept["preSnapshotCommand"] == "FROM_AE"

    def test_import_reads_snapshot_commands_from_custom_properties_target(self):
        text = Path(cae.__file__).read_text(encoding="utf-8")
        assert "preSnapshotCommand" in text and "postSnapshotCommand" in text
        assert "_snapshot_commands_from_target_props" in text
