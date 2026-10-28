# Coverage matrix — backup import / create-table-if-missing (MR !69)

Legend: **live?** = needs `GS_E2E_HUB_URL` (and often DB/Automator env). Offline tests always run.

| User-facing path / option | Test | Live? | Notes |
|---------------------------|------|-------|-------|
| create table if missing (flag on) | `test_e2e_create_table.py::test_create_table_if_missing_mssql_style_inference` | yes | DB assert if `GS_E2E_MSSQL_*` |
| create table if missing (flag off) | `test_no_create_tables_flag_leaves_db_without_table` | yes | |
| existing table untouched | `test_existing_table_untouched_on_reimport` + offline `test_existing_table_untouched` | yes / no | |
| MSSQL dialect snapshot inference | `test_create_table_dialect_snapshot_inference[mssql]` | yes | |
| MySQL dialect | same parametrize | yes | skipped until agent-tag mapping verified |
| PostgreSQL dialect | same parametrize | yes | skipped until agent-tag mapping verified |
| three-part names | offline `test_quoted_three_part_with_spaces` + existing `test_target_schema_inference` | no | |
| quoted identifiers / spaces | offline + `test_schema_with_spaces_create_table_path` | mixed | **BUG** schema URL encoding |
| MySQL backticks | `test_mysql_backticks_two_and_three_part` | no | |
| comments in filters | `test_comments_in_filters_still_infer` | no | |
| ambiguous inference skip | `test_ambiguous_double_dot_skipped` + existing | no | |
| snapshotDeleteFilter alone ≠ inference | `test_snapshot_delete_filter_alone_does_not_infer` | no | clause-based; SQL not inferred |
| fan-in | `test_offline_fan_in_chunking.py` + existing unit | no | live fan-in create **UNVERIFIED** |
| agent overrides + passwordEnv | `TestAgentOverridesOffline` + CLI live | mixed | |
| password key refused | `test_password_key_refused` | no | |
| dry-run | `test_dry_run_exits_0_or_1_never_writes` | yes | |
| exit codes 0/1/2 | `test_exit_codes_map` + `test_full_import_exit_code_semantics` | mixed | |
| missing password → 1 | `test_missing_password_exit_1` | no | |
| --write-plan | `test_write_plan_exit_0` / live plan import | mixed | |
| --plan | `test_write_plan_then_import_with_plan` | yes | |
| skip-errors / chunking | offline chunking smoke + README; live **UNVERIFIED** | partial | entity CLI flags |
| UDF import 2.3 isSnapshot vs older | `test_udf_is_snapshot_signature_helper` | gated | `GS_E2E_UDF=1` |
| filters / snapshotDeleteFilter / snapshotWriteMethod | fixture inventory + existing export tests | no | live restore **UNVERIFIED** |
| groups | fixture inventory | no | live **UNVERIFIED** |
| Chronos schedules | fixture inventory / `--no-scheduling` | no | live **UNVERIFIED** |
| 2.3 column-relation mapping | existing `test_bootstrapper_*_23*` | no | live hub **UNVERIFIED** |
| AI settings pre-2.3 (unsupported ≠ error) | `test_ai_settings_unsupported_not_fatal_offline_module` | gated | `GS_E2E_AI_SETTINGS=1` |
| round-trip export→import equality | `test_roundtrip_export_import_equality` | gated | `GS_E2E_ROUNDTRIP_PIPELINE_ID` |
| idempotent re-run | `test_idempotent_reimport_on_existing_skip` | yes | |
| partial failure recovery / resume | `test_partial_failure_resume_flag_accepted` | yes | bad id → exit 1 |
| Automator `/api/import/analyze` | `test_login_then_analyze_synth_zip` | Automator | |
| Automator `/api/import/preview` | `test_preview_after_analyze` | Automator | |
| Automator `/api/import/all` | `test_import_all_form_fields_documented` | Automator | |
| Automator other import routes | `test_import_routes_not_missing` | Automator | |
| pre-2.3 Core Hub compatibility | AI/UDF gates + README claims | partial | needs older hub lab |

## Bugs fixed in this worktree (were under test)

| Bug | File:line | Status | Test |
|-----|-----------|--------|------|
| Schema name not URL-encoded in create-table paths | `create_all_tables.py:143–174`, `:191–195` | **Fixed** (`urllib.parse.quote`) | `test_table_exists_url_encodes_schema_with_spaces`, `test_generate_create_table_statement_encodes_schema` (no longer xfail) |
| Bare list/None discovery → TypeError | `create_all_tables.py:727` `_discovery_column_list` | **Fixed** | `test_bare_list_discovery_columns_raises_target_table_creation_error` |

See also `FIELD_MATRIX.md` and silent-loss items in `SILENT_DATA_LOSS.md` (S2 fixed; S1 by design — EntityStatus / sync query param, not entity config).

## Mac lab

Live env is on the developer Mac (`https://localhost`, DBs on `127.0.0.1`). See `RUN_E2E.md`. Not reachable from the agent box — all live rows above remain skipped until env vars are set locally.
