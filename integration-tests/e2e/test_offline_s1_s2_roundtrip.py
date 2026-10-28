"""Export → import → export round-trips for silent-loss fields S1/S2.

S2 must survive with hub-like empty agentEntity.customProperties (commands on entityType).
S1 is by design: SWM is EntityStatus / sync query-param only — assert it is never on the entity PUT
and that export does not invent it from insertConflictStrategy.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
IT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(IT))

from export_template_from_corehub import (  # noqa: E402
    _process_multitable_entity,
    _process_single_entity,
    _target_custom_properties,
)
from fake_corehub import FakeCoreHub, run_create_entities  # noqa: E402
from test_bootstrapper_entity_payloads_23 import table  # noqa: E402
from test_bootstrapper_multitable_23 import yaml_of  # noqa: E402
from test_bootstrapper_payload_golden import SOURCE_TABLES, TARGET_TABLES, _source_list  # noqa: E402

V23 = "2.3.0.0 - build test"

PRE = "ALTER TABLE public.customers NOCHECK CONSTRAINT ALL;"
POST = "ALTER TABLE public.customers CHECK CONSTRAINT ALL;"


def _single_yaml_with_commands(table_name="CUSTOMERS"):
    body = f"""        name: customers
        keys: [ID]
        customProperties:
          target:
            preSnapshotCommand: "{PRE}"
            postSnapshotCommand: "{POST}"
        columns:
          - ID: id
          - NAME: name
"""
    return table(body, table_name)


def _create_single(yaml_text, table_name="CUSTOMERS"):
    hub = FakeCoreHub(SOURCE_TABLES, TARGET_TABLES, version=V23)
    result = run_create_entities(hub, yaml_text, _source_list(table_name))
    assert result["failed"] == 0, result
    return hub.puts[0]["entities"][0]


def _hub_strip_ae_custom_properties(entity):
    """Simulate live hub fixtures: agentEntity.customProperties empty; entityType keeps settings."""
    ent = json.loads(json.dumps(entity))
    for ae in ent["agentEntities"]:
        ae["customProperties"] = {}
    return ent


def _export_single(entity):
    source_ae, target_ae = json.loads(json.dumps(entity["agentEntities"]))
    schemas = {}
    _process_single_entity(entity, source_ae, target_ae, schemas, {})
    custom = schemas["src"]["tables"]["custom"]
    return {"src": {"target": "tgt", "tables": {"whitelist": list(custom), "custom": custom}}}


def _target_cp_from_export(exported):
    custom = exported["src"]["tables"]["custom"]
    # table key may be lowercased entity name or source table name
    for cfg in custom.values():
        tcp = (cfg.get("customProperties") or {}).get("target") or {}
        if "preSnapshotCommand" in tcp or "postSnapshotCommand" in tcp:
            return tcp
    # fall back to first table
    first = next(iter(custom.values()))
    return (first.get("customProperties") or {}).get("target") or {}


class TestS2PrePostRoundTrip:
    def test_s2_pre_post_survive_export_import_export(self):
        original = _create_single(_single_yaml_with_commands())
        target_et = original["agentEntities"][1]["entityType"]
        assert target_et["preSnapshotCommand"] == PRE
        assert target_et["postSnapshotCommand"] == POST

        # Hub-like: strip AE customProperties so export must lift from entityType
        hub_shaped = _hub_strip_ae_custom_properties(original)
        assert hub_shaped["agentEntities"][1]["customProperties"] == {}
        assert hub_shaped["agentEntities"][1]["entityType"]["preSnapshotCommand"] == PRE

        exported = _export_single(hub_shaped)
        tcp = _target_cp_from_export(exported)
        assert tcp.get("preSnapshotCommand") == PRE
        assert tcp.get("postSnapshotCommand") == POST

        # Re-import from that export YAML and export again (still hub-stripped)
        restored = _create_single(yaml.safe_dump(exported, sort_keys=False))
        assert restored["agentEntities"][1]["entityType"]["preSnapshotCommand"] == PRE
        assert restored["agentEntities"][1]["entityType"]["postSnapshotCommand"] == POST

        exported2 = _export_single(_hub_strip_ae_custom_properties(restored))
        tcp2 = _target_cp_from_export(exported2)
        assert tcp2.get("preSnapshotCommand") == PRE
        assert tcp2.get("postSnapshotCommand") == POST

    def test_s2_direct_target_custom_properties_lift(self):
        out = _target_custom_properties(
            {"customProperties": {}},
            {"preSnapshotCommand": PRE, "postSnapshotCommand": POST},
        )
        assert out == {"preSnapshotCommand": PRE, "postSnapshotCommand": POST}


class TestS2MultiTableStringCommands:
    def test_multitable_string_pre_post_land_on_entity_type_and_survive_export(self):
        # MultiTable takes target customProperties from the *last* table in the chain (PARTS).
        body = """name: customers
keys: [ID]
columns:
  - ID: id
  - NAME: name"""
        yaml_text = yaml_of("CUSTOMERS", body, chained=True)
        cfg = yaml.safe_load(yaml_text)
        cfg["src"]["tables"]["custom"]["PARTS"] = {
            "chainId": "c1",
            "customProperties": {
                "target": {
                    "preSnapshotCommand": PRE,
                    "postSnapshotCommand": POST,
                }
            },
        }
        yaml_text = yaml.safe_dump(cfg, sort_keys=False)

        hub = FakeCoreHub(SOURCE_TABLES, TARGET_TABLES, version=V23)
        result = run_create_entities(hub, yaml_text, _source_list("CUSTOMERS", "PARTS"))
        assert result["failed"] == 0, result
        entity = hub.puts[0]["entities"][0]
        target_ae = entity["agentEntities"][1]
        assert target_ae["entityType"].get("preSnapshotCommand") == PRE
        assert target_ae["entityType"].get("postSnapshotCommand") == POST
        assert target_ae["customProperties"].get("preSnapshotCommand") == PRE

        hub_shaped = _hub_strip_ae_custom_properties(entity)
        source_ae, target_ae2 = json.loads(json.dumps(hub_shaped["agentEntities"]))
        schemas = {}
        _process_multitable_entity(hub_shaped, source_ae, target_ae2, schemas, {})
        found = False
        for table_cfg in schemas["src"]["tables"]["custom"].values():
            tcp = (table_cfg.get("customProperties") or {}).get("target") or {}
            if tcp.get("preSnapshotCommand") == PRE and tcp.get("postSnapshotCommand") == POST:
                found = True
                break
        assert found, schemas["src"]["tables"]["custom"]


class TestS1ByDesign:
    def test_s1_snapshot_write_method_not_on_entity_put(self):
        body = """        name: customers
        keys: [ID]
        snapshotWriteMethod: INSERT
        columns:
          - ID: id
          - NAME: name
"""
        entity = _create_single(table(body, "CUSTOMERS"))
        target_ae = entity["agentEntities"][1]
        target_et = target_ae["entityType"]
        blob = json.dumps(entity)
        assert "snapshotWriteMethod" not in blob
        assert "insertConflictStrategy" not in blob
        # Explicit: SWM must not appear as entityType field we invent
        assert "snapshotWriteMethod" not in target_et
        assert "insertConflictStrategy" not in target_et

    def test_s1_export_does_not_invent_snapshot_write_method(self):
        """insertConflictStrategy is a different field; export must not map it to snapshotWriteMethod."""
        entity = _create_single(_single_yaml_with_commands())
        hub_shaped = _hub_strip_ae_custom_properties(entity)
        # Inject fixture-like field; export must not map it to snapshotWriteMethod without a verified wire
        hub_shaped["agentEntities"][1]["entityType"]["insertConflictStrategy"] = "UPSERT"
        exported = _export_single(hub_shaped)
        for cfg in exported["src"]["tables"]["custom"].values():
            assert "snapshotWriteMethod" not in cfg
