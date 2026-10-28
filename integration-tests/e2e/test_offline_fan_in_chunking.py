"""Offline: fan-in merge, chunking helpers, synthetic fixture field inventory."""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import fan_in  # noqa: E402
from .zip_builder import entities_dict, FIXTURES


class TestFanInOffline:
    def test_merge_types_widens_varchar(self):
        result, err = fan_in.merge_types([
            ("varchar", 10, None, None),
            ("varchar", 100, None, None),
        ])
        assert err is None, err
        assert result is not None
        base, length, *_rest = result
        assert base == "varchar"
        assert length == 100

    def test_type_signature_uses_data_length(self):
        assert fan_in.type_signature({"type": "varchar", "dataLength": 10}) == "varchar(10)"
        # dataLength 0 is falsy → bare kind
        assert fan_in.type_signature({"type": "varchar", "dataLength": 0}) == "varchar"

    def test_peers_of_returns_list(self):
        config = entities_dict("synth1")
        schema = next(iter(config["schemas"]))
        tables = config["schemas"][schema]["tables"]["custom"]
        name, cfg = next(iter(tables.items()))
        target = cfg.get("name") or name
        peers = fan_in.peers_of(config, config["schemas"][schema].get("target"), target)
        assert isinstance(peers, list)


class TestChunkingOffline:
    def test_create_entities_callable(self):
        import create_all_entities as cae
        assert callable(cae.create_entities)
        assert inspect.signature(cae.create_entities)


class TestFixtureInventory:
    def test_synth1_has_export_metadata(self):
        config = entities_dict("synth1")
        assert config.get("exportMetadata", {}).get("pipelineId") == "5e1f0001"
        assert "schemas" in config

    def test_inventory_snapshot_keys_present_or_absent_documented(self):
        text = (FIXTURES / "synth1_entities.yaml").read_text(encoding="utf-8")
        # Soft inventory — fixtures may omit some features; just ensure file is readable YAML entities
        assert "schemas:" in text
