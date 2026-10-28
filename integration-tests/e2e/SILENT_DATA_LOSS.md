# Silent data-loss review (export ↔ import)

Findings from code inspection + offline assertions in `test_offline_silent_loss.py`
and round-trip coverage in `test_offline_s1_s2_roundtrip.py`.
None of these crash; they drop or omit fields so a round-trip can change behaviour.

## S1 — `snapshotWriteMethod` (table-level) — **BY DESIGN (not silent loss)**

| | |
|-|-|
| **Where** | YAML `tables.custom.<T>.snapshotWriteMethod`; import helper `_log_snapshot_write_method` in `create_all_entities.py` |
| **Import entity PUT** | Intentionally **not** written into the entity config body (logged only) |
| **Applied at sync** | `main.py` / MCP start-sync / restart / snapshot pass `?snapshotWriteMethod=` (default **UPSERT**) |
| **Hub model (2.3.1.1 / 2.2.11.13)** | Confirmed by Portal Molo17 / gluesync-kotlin: SWM lives in **EntityStatus**, not entity config. It is **not** persisted on the entity PUT/GET config shape. |
| **Export** | Does **not** read SWM from entity status (and must not invent mapping from `entityType.insertConflictStrategy`, which is a different field) |
| **Impact** | YAML SWM is honored when sync is started via bootstrapper/`main.py` with that YAML; a bare re-export cannot reconstruct SWM from entity config because the hub does not store it there |
| **Test** | `test_snapshot_write_method_only_logged_on_import`; `test_s1_snapshot_write_method_not_on_entity_put`; `test_s1_no_insert_conflict_strategy_wiring_in_bootstrapper`; `test_s1_export_does_not_invent_snapshot_write_method` |
| **Fixed?** | **N/A — by design.** Do not add export-from-status. Do not map to `insertConflictStrategy`. |

## S2 — `preSnapshotCommand` / `postSnapshotCommand` — **FIXED**

| | |
|-|-|
| **Where** | YAML `customProperties.target.preSnapshotCommand` / `postSnapshotCommand` |
| **Import** | SingleTable: spread onto `entityType` via `_target_extra` (`create_all_entities._target_entity_type`). MultiTable: `_snapshot_commands_from_target_props` → AE `customProperties` **and** `entityType` (strings, matching SingleTable; dicts still accepted). |
| **Export** | `_target_custom_properties` lifts non-empty string `preSnapshotCommand` / `postSnapshotCommand` from `entityType` into YAML `customProperties.target` when AE `customProperties` is empty (hub-fixture shape), same pattern as concurrency / bulk / allowedOperations. MultiTable export attaches reconstructed target CP to the **last** chain table. |
| **Impact (before)** | Commands applied on import; fresh export from Core Hub omitted them → next import lost inference + commands |
| **Test** | `test_export_lifts_snapshot_commands_from_entity_type`; `test_s2_pre_post_survive_export_import_export`; MultiTable string path in same round-trip module |
| **Fixed?** | **Yes** — `export_template_from_corehub.py` (`_target_custom_properties` + MultiTable last-table attach); `create_all_entities.py` (`_snapshot_commands_from_target_props`) |

## Not silent (clear errors)

- Literal `password` in agent overrides → `AgentOverrideError`
- Unknown override fields → `AgentOverrideError`
- Wrong agent tag in overrides → `AgentOverrideError`
- Bare-list / empty discovery columns → `TargetTableCreationError` (after B2 fix)
- Invalid `on_existing` → `ValueError`
