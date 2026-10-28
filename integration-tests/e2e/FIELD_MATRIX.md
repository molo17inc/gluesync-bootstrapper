# FIELD_MATRIX — every user-customizable input (MR !69 e2e)

Generated for branch `feat/simple-backup-import` @ `70b3fed` (+ local create_all_tables fixes).
Legend: **PASS** = covered offline and/or unit with clear success/error; **PARTIAL** = shape/parse only or live UNVERIFIED; **FAIL** = broken; bug column cites id.

Scope: defaults, valid values, boundary/invalid (clear error, no crash), related-field interaction, pre-2.3 vs 2.3 where applicable, export→import round-trip.

Live Mac hub (`GS_E2E_HUB_URL`) still required for rows marked gated/UNVERIFIED.

| Field | Where defined | Default / notes | Tests | Result | Bug? |
|-------|---------------|-----------------|-------|--------|------|
| dry_run / --dry-run / GS_IMPORT_DRY_RUN | import_options.py; import_backup --dry-run | false | e2e TestImportOptionsFields.test_defaults/from_env; test_dry_run_*; integration test_import_options | PASS offline: blocks writes (DryRunWriteBlocked); CLI dry-run exits without hub; live dry-run gated | — |
| validate / --no-validate / GS_IMPORT_NO_VALIDATE | import_options.py | validate=true | test_from_env; integration test_import_options | PASS offline (env flips validate off) | — |
| strict_validate / --strict-validate / GS_IMPORT_STRICT_VALIDATE | import_options.py | false | integration test_import_options | PASS unit; live hub UNVERIFIED | — |
| allow_offline_entities / --allow-offline-entities / GS_IMPORT_ALLOW_OFFLINE | import_options.py | false | integration + Automator pending path sets True | PASS unit; Core Hub 2.3.1 refuses unreachable agents (README VERIFIED) | — |
| no_discovery / --no-discovery / GS_IMPORT_NO_DISCOVERY | import_options.py | false | integration import options / safety | PASS unit; same 2.3.1 refusal as offline | — |
| allow_missing_tables / --allow-missing-tables / GS_IMPORT_ALLOW_MISSING_TABLES | import_options.py | false | test_import_safety_features | PASS unit; Core Hub acceptance UNVERIFIED | — |
| on_existing / --on-existing / GS_IMPORT_ON_EXISTING | import_options.py choices skip\|update\|fail | None (send as before); resume defaults skip | test_on_existing_invalid; test_from_env; test_idempotent_reimport_on_existing_skip; resume tests | PASS: invalid raises ValueError; skip/fail unit; update UNVERIFIED on live hub | — |
| corehub_version / --corehub-version / COREHUB_VERSION | import_options.py; commons.py | None (GET /version) | integration test_import_options corehub_version; UDF isSnapshot boundaries | PASS offline for 2.2 vs 2.3 helpers; live pre-2.3 hub UNVERIFIED | — |
| --remap / GS_IMPORT_REMAP | import_options.py Remap | empty | integration test_import_options remap/table-map | PASS unit | — |
| --schema-map / --target-schema-map | import_options.py | none | integration remap tests | PASS unit | — |
| --table-map / --target-table-map | import_options.py | none | integration remap tests | PASS unit | — |
| --entity-prefix / --entity-suffix | import_options.py | none | integration remap | PASS unit; live round-trip UNVERIFIED | — |
| --group-map / --agent-map | import_options.py | none | integration remap | PASS unit | — |
| overrides.host | agent_overrides.FIELDS → hostCredentials.host | backup value | test_agent_override_fields_accepted; backup_import_resume_overrides | PASS | — |
| overrides.port | agent_overrides (must be number) | backup value | test_agent_override_fields_accepted; invalid port → AgentOverrideError (unit) | PASS; non-numeric clear error | — |
| overrides.database / databaseName | agent_overrides → databaseName | backup value | test_agent_override_fields_accepted | PASS | — |
| overrides.username | agent_overrides → username | backup value | test_agent_override_fields_accepted | PASS | — |
| overrides.passwordEnv | agent_overrides; GS_IMPORT_AGENT_PASSWORD_<TAG> | required when overriding | test_password_env_name_applied; test_missing_password_env_named_in_override; test_password_key_refused | PASS: applies named env; missing → clear error; literal password refused | — |
| GS_IMPORT_AGENT_OVERRIDES / _FILE | agent_overrides.load merge order | none | test_backup_import_resume_overrides merge; e2e wrong-tag | PASS; unknown tag → AgentOverrideError | — |
| GS_IMPORT_AGENT_PASSWORD_<TAG> | agent_overrides.password_env_name | unset | gaps + broad + import_report_fixes | PASS; missing → exit 1 on CLI import | — |
| unknown override field | agent_overrides.validate | n/a | test_agent_override_rejects_unknown_field | PASS clear AgentOverrideError | — |
| CREATE_TABLE_IF_NOT_EXISTS / --no-create-tables / createTables | create_all_tables.py; import_backup; Automator form/prefs | true | test_flag_off_skips_creation; test_no_create_tables_flag_*; Automator prefs | PASS offline; live gated on Mac | — |
| import_backup --base-url | import_backup.py / GS_COREHUB_URL | https://localhost:1717 | e2e runner uses hub_env | PASS when hub set; else skip | — |
| import_backup --username / --password-env | import_backup.py | admin / GS_COREHUB_PASSWORD | e2e runner | PASS when env set | — |
| import_backup --use-ssl / --no-use-ssl / --skip-verify | import_backup.py | from URL scheme | CLI parse + runner | PASS parse; live TLS UNVERIFIED on box | — |
| import_backup --no-scheduling | import_backup.py | scheduling on | fixture inventory; live Chronos UNVERIFIED | PARTIAL offline shape only | — |
| import_backup --agent-overrides FILE | import_backup.py | none | e2e overrides_json fixture | PASS when hub set | — |
| import_backup --resume-pipeline-id / GS_IMPORT_RESUME_PIPELINE_ID | import_backup.py | none | test_partial_failure_resume_flag_accepted; resume unit tests | PASS flag accepted; bad id → exit 1 | — |
| import_backup --resume-from-pipeline / GS_IMPORT_RESUME_FROM_PIPELINE | import_backup.py | none | unit: errors without resume-pipeline-id | PASS clear parser error | — |
| import_backup --plan / --write-plan | import_backup.py; import_plan.py | none | test_write_plan_exit_0; test_write_plan_then_import_with_plan; test_custom_import | PASS offline write-plan; live plan import gated | — |
| import_backup --no-infer-target-schema / GS_IMPORT_INFER_TARGET_SCHEMA | table_target_schema.py | infer on | test_synthetic_backups infer false; gaps dialect/quote/comment tests | PASS offline inference matrix | — |
| GS_IMPORT_ON_EXISTING_PIPELINE | automator_app (suffix\|fail) | suffix | analyze/import-all unit paths | PASS unit | — |
| GS_IMPORT_CLEANUP_ON_FAILURE | automator_app | false | README + unit mentions; DELETE route UNVERIFIED | PARTIAL | — |
| GS_IMPORT_CONTINUE_ON_CREDENTIALS_FAILURE | automator_app/corehub.py | false | test_credentials_pending | PASS unit; live 2.3.1 behaviour documented VERIFIED in README | — |
| GS_IMPORT_ATTEMPT_ENTITIES_WHEN_PENDING | automator_app | false | test_k9_pending_condense; credentials_pending | PASS unit | — |
| GS_IMPORT_MAX_UPLOAD_MB | automator_app analyze | 512 | test_import_hardening | PASS; junk value handled | — |
| GS_IMPORT_RESUME_PIPELINE_ID (Automator form resume_pipeline_id) | import_all Form | none | e2e form fields documented; resume units | PASS | — |
| GS_IMPORT_PLAN_PASSWORD_* / plan passwordRef | import_plan.py | n/a | custom import / plan tests; password key refused | PASS; plan never stores password | — |
| create_all_entities --pipeline/--source-schema/--target-schema/--source-type/--target-type/--token | create_all_entities.py | required | integration entity creation suite | PASS existing suite | — |
| create_all_entities --chunk-size | create_all_entities.py | 50 | test_offline_fan_in_chunking smoke; live chunking UNVERIFIED | PARTIAL | — |
| create_all_entities --skip-errors | create_all_entities.py | false | documented; live UNVERIFIED in e2e | PARTIAL | — |
| create_all_entities --yaml-file | create_all_entities.py | default template | integration YAML loaders | PASS | — |
| create_all_entities --chronos-token | create_all_entities.py | falls back to --token | Chronos schedule fixtures offline | PARTIAL live | — |
| create_all_tables --pipeline/.../--skip-errors/--yaml-file/--token | create_all_tables.py | mirrors entities subset | gaps create-table path tests | PASS offline after B1/B2 | — |
| export_template_from_corehub --pipeline/--token/--output | export_template_from_corehub.py | backup_<id>.yaml | roundtrip gated; S2 fixed; S1 by design | PASS offline export; SWM not in entity config | S1 by design |
| export_all_pipelines / Automator export | export_all_pipelines.py; automator | n/a | synth archive roundtrip shape | PARTIAL | — |
| /api/import/all file | Form UploadFile | required unless uploadId | test_import_all_form_fields_documented; TestClient analyze | PASS offline TestClient; live Automator gated | — |
| /api/import/all agent_overrides | Form JSON | none | form documented + override unit | PASS | — |
| /api/import/all resume_pipeline_id | Form | none | form documented + resume unit | PASS | — |
| /api/import/all plan | Form JSON | none | custom import + form | PASS unit | — |
| /api/import/all uploadId | Form alias uploadId | none | analyze→import path unit | PASS unit | — |
| /api/import/all agent_passwords | Form JSON in-memory | none | plan/password tests; never logged | PASS unit | — |
| /api/import/analyze file | UploadFile | required | test_login_then_analyze_*; automator_analyze_offline_testclient | PASS | — |
| /api/import/preview | POST after analyze | n/a | test_preview_after_analyze | PASS when Automator up | — |
| /api/import/validate-all\|full-backup\|users\|oidc\|ai-settings | automator_app routes | n/a | test_import_routes_not_missing; ai_settings unsupported≠fatal | PASS registration; AI live gated | — |
| Automator createTables preference | prefs + request createTables | true | corehub _apply_create_tables_setting | PASS unit wiring | — |
| schemas.<S>.target / targetSchema | table-list YAML | same as source or inferred | inference gaps + synthetic; explicit wins | PASS offline | — |
| schemas.<S>.sourceType / targetType | YAML | from agents | template load | PASS shape | — |
| schemas.<S>.tables.whitelist | YAML | all discovered if absent | fixtures | PASS shape | — |
| tables.custom.<T>.name | YAML entity name | table name | yaml roundtrip broad | PASS | — |
| tables.custom.<T>.keys / targetKeys | YAML; 2.3 column-relation | from discovery | 2.3 unit tests; e2e live UNVERIFIED | PASS unit | — |
| tables.custom.<T>.columns / column map | YAML | identity | roundtrip; discovery bare-list B2 | PASS after B2 | B2 fixed |
| tables.custom.<T>.groupId | YAML + groups.* | _default | test_groups_and_group_schedules_shape | PASS yaml shape; live UNVERIFIED | — |
| groups.<id>.description/color | YAML groups | optional | groups shape test | PASS | — |
| tables.custom.<T>.filter / snapshotDeleteFilter | YAML clauses | none | filter+snapshotDelete combo roundtrip; delete-filter alone ≠ inference | PASS offline | — |
| tables.custom.<T>.snapshotWriteMethod | YAML INSERT\|UPSERT | UPSERT (EntityStatus / sync query param) | silent_loss asserts not on PUT; sync via main.py `?snapshotWriteMethod=` | BY DESIGN — not entity config; not insertConflictStrategy | S1 by design |
| tables.custom.<T>.customProperties.target.pre/postSnapshotCommand | YAML | none | inference + S2 export→import→export roundtrip | Import PASS; export lift FIXED | S2 fixed |
| tables.custom.<T>.customProperties.target.udf / ttl / allowedOperations / bulk flags | YAML | none | UDF signature 2.3 vs older; export lifts udf | PASS offline UDF adapt; live UDF gated | — |
| tables.custom.<T>.schedules / schema schedules / group_schedules | YAML Chronos | none | groups/schedules shape; --no-scheduling | PASS shape; live Chronos UNVERIFIED | — |
| tables.custom.<T>.documentKey.* | YAML | none | template examples | PARTIAL (shape only) | — |
| tables.custom.<T>.whereClause / chainId / unlockedSchema / targetOnlyColumns | YAML | none | template + 2.3 units | PARTIAL | — |
| tables.custom.<T>.transactionsAudit (schema-level) | YAML | off | README; unit if present | PARTIAL | — |
| 2.3 column-relation source/destination forms | YAML columns | n/a | test_source_destination_form; test_source_target_list_form | PASS offline | — |
| agents-config hostCredentials.* | agents-config.yaml in ZIP | from export | overrides apply; password masking | PASS; passwords via env only on import | — |
| config.json / example-config.json agents[] | main.py pipeline bootstrap | n/a | existing main/bootstrap tests | PASS existing suite | — |
| main.py --pipeline-name | main.py | optional | main CLI | PASS parse | — |
| GS_IMPORT_DRY_RUN via main.py | main.py refuses | n/a | main.py message to use entity dry-run | PASS clear refusal (creates pipeline first) | — |

## Bugs referenced

| Id | Status | File:line | Summary |
|----|--------|-----------|---------|
| B1 | **Fixed** | `create_all_tables.py:143–174`, `:191–195` | URL-encode schema (and table) in `table_exists` / `generate_create_table_statement` |
| B2 | **Fixed** | `create_all_tables.py:727–735` (`_discovery_column_list`), used at `:741`, `:931` | Tolerate bare-list / None discovery columns like `_exported_column_dtos` |
| S1 | **By design** | `_log_snapshot_write_method`; `main.py` `?snapshotWriteMethod=` | Portal/kotlin 2.3.1.1+2.2.11.13: SWM is EntityStatus, set only on sync start/restart/snapshot — not entity config; do not map insertConflictStrategy; no export-from-status |
| S2 | **Fixed** | `export_template_from_corehub._target_custom_properties`; MultiTable `_snapshot_commands_from_target_props` | Export lifts pre/postSnapshotCommand from entityType; MultiTable import accepts strings onto entityType |

## Counts (this harness)

| Suite | Collected / result |
|-------|-------------------|
| `integration-tests/e2e/` | offline **85 passed**, 21 skipped (need hub/Automator/gates); includes S1/S2 round-trips |
| Related offline + unit (export/import/SWM/advanced props/e2e) | **300 passed**, 21 skipped, 7 subtests; MultiTable/automator extras **141 passed** |

## Round-trip notes

- Synth entities YAML dump→load equality: PASS (`test_entities_yaml_roundtrip_equality`, filter/group shapes).
- Full export→import equality against live hub: gated (`GS_E2E_ROUNDTRIP_PIPELINE_ID`). S1 SWM is not in entity config (by design). S2 pre/post commands survive offline export→import→export.
- pre-2.3: UDF `isSnapshot` stripped by `adapt_udf_signature`; AI unsupported ≠ fatal; grouped-matrix / column-relation assume 2.3 unless `--corehub-version` / `COREHUB_VERSION` set.
