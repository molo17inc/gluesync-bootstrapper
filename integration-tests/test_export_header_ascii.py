#!/usr/bin/env python3
"""
GSSD-1183 – export header must be pure ASCII / UTF-8-safe for Japanese Windows.

Root cause:
  build_export_header() used a Unicode en-dash (U+2013) in the comment line
  "# Gluesync Automator – Pipeline Configuration Export".
  On Japanese Windows the default locale encoding is CP932, and loading that
  UTF-8 file without an explicit encoding raised:
      UnicodeDecodeError: 'cp932' codec can't decode byte 0x93 ...

Fixes:
  1. Export header uses ASCII hyphen-minus only.
  2. load_yaml_config always opens files as UTF-8.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CUSTOMER_YAML = PROJECT_ROOT.parent.parent / "Downloads" / "GSSD-1183_attachments" / "backup_82447495_20260713_164005.yaml"
# Prefer the ticket attachment path used in this workspace
_ATTACHMENT_CANDIDATES = [
    Path("/Users/danieleangeli/Downloads/GSSD-1183_attachments/backup_82447495_20260713_164005.yaml"),
    PROJECT_ROOT / "integration-tests" / "data" / "gssd1183_backup.yaml",
]


def _customer_sample_bytes() -> bytes:
    for p in _ATTACHMENT_CANDIDATES:
        if p.exists():
            return p.read_bytes()
    # Fallback: reconstruct the exact failing header from the ticket
    return (
        b"# ---------------------------------------------------------------\n"
        b"# Gluesync Automator \xe2\x80\x93 Pipeline Configuration Export\n"
        b"# ---------------------------------------------------------------\n"
        b"exportMetadata:\n"
        b"  pipelineId: '82447495'\n"
        b"  pipelineName: 1st pipeline\n"
        b"public:\n"
        b"  target: public\n"
        b"  tables:\n"
        b"    whitelist: [cities]\n"
    )


class ExportHeaderAsciiTests(unittest.TestCase):
    def test_build_export_header_is_pure_ascii(self):
        import export_template_from_corehub as export_mod

        with mock.patch.object(export_mod, "fetch_core_hub", side_effect=Exception("offline")):
            header = export_mod.build_export_header(token="tok", pipeline_id="pipe1", base_url="https://localhost")

        self.assertIsInstance(header, str)
        header.encode("ascii")  # raises if any non-ASCII remains
        self.assertIn("Gluesync Automator - Pipeline Configuration Export", header)
        self.assertNotIn("\u2013", header)
        self.assertNotIn("\u2014", header)

    def test_build_export_header_is_cp932_encodable(self):
        import export_template_from_corehub as export_mod

        with mock.patch.object(export_mod, "fetch_core_hub", side_effect=Exception("offline")):
            header = export_mod.build_export_header(token="tok", pipeline_id="pipe1")

        # Must survive round-trip through Japanese Windows default encoding
        header.encode("cp932")
        header.encode("utf-8")

    def test_customer_sample_contains_en_dash_and_fails_cp932(self):
        """Document the original failure mode from GSSD-1183 attachments."""
        data = _customer_sample_bytes()
        self.assertIn(b"\xe2\x80\x93", data, "customer sample should contain UTF-8 en-dash")
        with self.assertRaises(UnicodeDecodeError):
            data.decode("cp932")

    def test_load_yaml_config_reads_utf8_with_en_dash_header(self):
        """Even legacy exports with en-dash must load when opened as UTF-8."""
        from commons import load_yaml_config

        data = _customer_sample_bytes()
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as tmp:
            tmp.write(data)
            path = tmp.name

        try:
            cfg = load_yaml_config(path)
        finally:
            Path(path).unlink(missing_ok=True)

        self.assertIsInstance(cfg, dict)
        self.assertIn("exportMetadata", cfg)
        self.assertEqual(str(cfg["exportMetadata"].get("pipelineId")), "82447495")

    def test_load_yaml_config_uses_utf8_encoding(self):
        """Regression: open() must pass encoding='utf-8' (not locale default)."""
        from commons import load_yaml_config
        import builtins

        opened = {}
        real_open = builtins.open

        def tracking_open(file, mode='r', *args, **kwargs):
            opened['encoding'] = kwargs.get('encoding')
            opened['mode'] = mode
            return real_open(file, mode, *args, **kwargs)

        data = b"demo:\n  target: public\n"
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as tmp:
            tmp.write(data)
            path = tmp.name

        try:
            with mock.patch("builtins.open", side_effect=tracking_open):
                cfg = load_yaml_config(path)
        finally:
            Path(path).unlink(missing_ok=True)

        self.assertEqual(opened.get("encoding"), "utf-8")
        self.assertEqual(cfg.get("demo", {}).get("target"), "public")


if __name__ == "__main__":
    unittest.main()
