#!/usr/bin/env python3
"""
Pin: the full Core Hub export and import deliberately leave out the Connect backup feature.

The recovery key is a human-held secret and the stored public key would be useless (or dangerous) on a
restored installation, so the backup settings (/backup routes, BACKUP_CONFIGURATION global-config key) are
never exported or imported. Core Hub restores its own backups with its offline command, after which a human
enables backups again in Settings > Backup.

Core Hub is faked by an in-memory router patched over corehub.fetch_core_hub that records every request.
"""

import io
import json
import logging
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import yaml

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import automator_app.corehub as corehub  # noqa: E402

CONNECTION = {"base_url": "http://hub", "use_ssl": False, "skip_verify": False}
BACKUP_KEY = "BACKUP_CONFIGURATION"
BACKUP_SETTINGS = {"enabled": True, "publicKey": "age1-public-key-of-the-source", "schedule": "0 3 * * *"}


class RecordingHub:
    """Answers every route with something harmless and records each request, backup routes included."""

    def __init__(self):
        self.requests = []

    def __call__(self, path, method="GET", token=None, body=None, **kwargs):
        self.requests.append((method, path, body))
        if path.startswith("/global-config"):
            return {"value": "x"}
        if path == "/backup":
            return {"enabled": True, "publicKey": BACKUP_SETTINGS["publicKey"]}
        if path in ("/users", "/user-groups", "/connections"):
            return []
        return {}

    def paths(self):
        return [path for _, path, _ in self.requests]

    def touched_backup(self):
        """Requests on a /backup route, or naming the BACKUP_CONFIGURATION key in path or body."""
        return [
            request for request in self.requests
            if request[1].startswith("/backup") or BACKUP_KEY in request[1] or BACKUP_KEY in json.dumps(request[2], default=str)
        ]


def _full_export(hub):
    with mock.patch.object(corehub, "fetch_core_hub", side_effect=hub), \
            mock.patch.object(corehub, "configure_core_hub"), \
            mock.patch.object(corehub, "export_all_pipelines_yaml", side_effect=RuntimeError("no pipelines")), \
            mock.patch.object(corehub.ai_settings, "export_ai_settings", side_effect=corehub.ai_settings.AiSettingsUnsupported("old")):
        return corehub.export_full_corehub_backup(token="tok", **CONNECTION)


def _full_import(hub, archive):
    with mock.patch.object(corehub, "fetch_core_hub", side_effect=hub), \
            mock.patch.object(corehub, "configure_core_hub"):
        return corehub.import_full_corehub_backup(token="tok", zip_bytes=archive, **CONNECTION)


class ConnectBackupExclusionTests(unittest.TestCase):
    def test_a_full_export_never_asks_for_the_backup_settings(self):
        hub = RecordingHub()

        _full_export(hub)

        self.assertTrue(hub.requests, "the export should have talked to Core Hub")
        self.assertEqual([], hub.touched_backup())

    def test_the_archive_has_no_backup_section(self):
        archive = _full_export(RecordingHub())

        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            names = zf.namelist()
            contents = {name: zf.read(name).decode("utf-8") for name in names}
        self.assertFalse([name for name in names if "backup" in name.lower()], names)
        for name, text in contents.items():
            self.assertNotIn(BACKUP_KEY, text, name)
            self.assertNotIn(BACKUP_SETTINGS["publicKey"], text, name)
        # The global configs are an explicit allowlist: no key outside it, so none for the backup either.
        global_configs = yaml.safe_load(contents["global-configs.yaml"])
        self.assertFalse([key for key in global_configs if "backup" in key.lower()], list(global_configs))

    def test_a_full_import_never_calls_the_backup_routes_or_writes_the_backup_key(self):
        archive = _full_export(RecordingHub())
        hub = RecordingHub()

        results = _full_import(hub, archive)

        self.assertTrue([r for r in hub.requests if r[0] != "GET"], "the import should have written something")
        self.assertEqual([], hub.touched_backup())
        self.assertFalse([key for key in results if "backup" in key.lower()], list(results))

    def test_an_archive_that_does_carry_backup_data_is_not_applied_either(self):
        """A hand-made or foreign archive with backup settings in its global configs and an extra file."""
        archive = _full_export(RecordingHub())
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(archive)) as source, zipfile.ZipFile(buffer, "w") as zf:
            for name in source.namelist():
                if name == "global-configs.yaml":
                    configs = yaml.safe_load(source.read(name))
                    configs[BACKUP_KEY] = BACKUP_SETTINGS
                    configs["backup"] = BACKUP_SETTINGS
                    zf.writestr(name, yaml.safe_dump(configs))
                else:
                    zf.writestr(name, source.read(name))
            zf.writestr("backup.yaml", yaml.safe_dump(BACKUP_SETTINGS))
        hub = RecordingHub()

        results = _full_import(hub, buffer.getvalue())

        self.assertEqual([], hub.touched_backup())
        self.assertFalse([key for key in results if "backup" in key.lower()], list(results))


if __name__ == "__main__":
    unittest.main()
