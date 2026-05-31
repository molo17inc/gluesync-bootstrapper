# Unit Test Coverage Report

> Generated: 2026-05-31
> Scope: `gluesync-bootstrapper` + `automator_app`

---

## How to Run

```bash
cd /Users/danieleangeli/Documents/Repositories/gluesync-bootstrapper
python3 -m pytest integration-tests/ -v
```

---

## Existing Test Files (pre-2026-05-31)

### `test_bootstrapper_core_yaml_outputs.py`
- **Coverage**: Core bootstrapper payload generation using realistic YAML templates
  - `MYSQL_MYSQL_TABLES_TEMPLATE` — whitelist/blacklist, custom keys, basic columns
  - `MSSQL_COUCHBASE_DOCUMENT_KEY_TEMPLATE` — document keys, filters, custom properties
  - `MYSQL_VERTICA_CHRONOS_TEMPLATE` — entity-level schedules, document keys, filters, UDFs
- **Features tested**:
  - `create_entities_builds_document_keys_filters_and_custom_properties`
  - `create_tables_uses_yaml_target_schema_whitelist_custom_keys_and_quoted_names`
  - `create_entities_wires_entity_and_group_schedules_from_chronos_template`
  - `datatype_takes_priority_over_type`
  - `discovery_exception_does_not_propagate`

### `test_export_template_from_corehub.py`
- **Coverage**: Chronos pipeline job fetching
- **Features tested**:
  - `fetch_pipeline_jobs_accepts_paginated_items_shape`
  - `fetch_pipeline_jobs_accepts_legacy_list_shape`

### `test_null_column_type_fallback.py`
- **Coverage**: Null column type handling during export/import
- **Features tested**:
  - `backfills_null_types_from_discovery`
  - `does_not_raise_with_all_null_types`
  - `entity_payload_has_non_null_source_datatype`
  - `entity_payload_has_non_null_target_datatype`
  - `none_or_empty_returns_none`
  - `skips_tables_with_no_null_types`
  - `skips_tables_with_partial_types`

### `test_object_store_table_creation.py`
- **Coverage**: NoSQL/Object Store target handling
- **Features tested**:
  - `nosql_is_skipped`
  - `nosql_stays_nosql`
  - `object_store_case_insensitive`
  - `object_store_is_skipped`
  - `object_store_normalizes_to_nosql`
  - `object_store_target_disables_table_creation`
  - `rdbms_becomes_sql`
  - `sql_stays_sql`
  - `sql_target_keeps_table_creation_enabled`
  - `sql_targets_are_not_skipped`

### `test_pyinstaller_compatibility.py`
- **Coverage**: Python 3.10+ union type syntax (`X | None`) detection
- **Features tested**:
  - `no_union_type_syntax_in_type_hints`
  - `specific_known_problematic_patterns`
  - `imports_work_in_frozen_executable`

### `test_yaml_keys_with_null_source_pk.py`
- **Coverage**: YAML-defined primary keys when source reports `isPK=None`
- **Features tested**:
  - `yaml_keys_are_not_sent_when_source_isPK_is_null`
  - `yaml_keys_do_not_override_source_isPK_false`
  - `yaml_keys_missing_from_source_columns_not_sent`
  - `multiple_yaml_keys_not_sent_when_source_isPK_is_null`

---

## New Test Files (2026-05-31)

### `test_unlocked_schema_and_target_only_columns.py` (6 tests)
**Integration test YAMLs covered**: `mysql8-vertica`, `pgsqlwal-couchbase`, `oracle-triggers-vertica` (17+ tests use these features)

| Test | Feature |
|------|---------|
| `test_unlocked_schema_sets_single_zero_mapping_matrix` | `unlockedSchema: true` produces single `columnsMappingMatrix` entry with `sourceColumnId=0`, `targetColumnId=0` |
| `test_unlocked_schema_without_udf_raises_when_not_skipping_errors` | UDF mandatory validation for unlocked schema (`skip_errors=False`) |
| `test_unlocked_schema_without_udf_skips_when_skip_errors_true` | Unlocked table skipped when UDF missing and `skip_errors=True` |
| `test_target_only_columns_appended_to_target_entity` | `targetOnlyColumns` (simple + object format) appended to target columns with sequential IDs |
| `test_target_only_columns_without_unlocked_schema_are_skipped` | Target-only columns ignored when `unlockedSchema` is not true |
| `test_udf_config_in_target_entity_type` | UDF moved from `customProperties` to `entityType` with `mappingFunctionInfo` |

---

### `test_multitable_chains.py` (2 tests)
**Integration test YAMLs covered**: `mssql-ct-mysql-chains`, `mssql-cdc-mysql-chains`, `mysql8-mssql-ct-chains`, `mysql8-mssql-cdc-chains`

| Test | Feature |
|------|---------|
| `test_chain_creates_multitable_entity_with_table_ids_in_headers` | `chainId` groups tables into MultiTable entity; table headers in `columns` have `id` (TableWithId) |
| `test_agent_entity_has_hardcoded_order_index_zero` | `orderIndex` in agentEntity is currently hardcoded to `0` (not propagated from YAML) |

---

### `test_snapshot_write_method_and_delete_filter.py` (2 tests)
**Integration test YAMLs covered**: `mssql-ct-mssql-ct-conditional-deletion`, `as400-pgsql-TRUNCATE`, `oracle-19c-logminer-postgres`

| Test | Feature |
|------|---------|
| `test_snapshot_delete_filter_clauses_in_target_entity_type` | `snapshotDeleteFilter` clauses serialized into target `entityType` |
| `test_snapshot_write_method_not_in_custom_properties` | `snapshotWriteMethod` not leaked into `customProperties` (v2.1.17 regression fix) |

---

### `test_target_data_type_override.py` (2 tests)
**Integration test YAMLs covered**: `pgsqlwal-cassandra`, `pgsqlwal-bigquery`, `pgsqlwal-vertica`, `pgsqlwal-couchbase`

| Test | Feature |
|------|---------|
| `test_explicit_mapping_target_data_type_override` | `targetDataType` in explicit `{source, target, targetDataType}` mappings bypasses `map_data_type` |
| `test_whitelist_target_data_type_override` | `targetDataType` in whitelist format overrides automatic type mapping |

---

### `test_where_clause_and_allowed_operations.py` (3 tests)
**Integration test YAMLs covered**: `mysql8-vertica`, `mysql8-awss3-skip-deletions`, `mssql-ct-mssql-ct`

| Test | Feature |
|------|---------|
| `test_where_clause_in_source_tables_properties` | `whereClause` propagated to source `tablesProperties` |
| `test_explicit_allowed_operations_in_target_entity_type` | Table-level `allowedOperations` array in target `entityType` |
| `test_global_allowed_operations_override_defaults` | Schema-level global `allowedOperations` propagated to all entities |

---

### `test_pipeline_and_group_schedules.py` (2 tests)
**Integration test YAMLs covered**: `mysql8-vertica-chronos`, `mysql8-vertica-chronos-TLS`, `mysql8-vertica-chronos-INSERT`

| Test | Feature |
|------|---------|
| `test_group_schedules_list_format_with_group_ids` | `group_schedules` list format with `group_ids` resolved to group UUIDs |
| `test_pipeline_schedules_are_created` | Pipeline-level `schedules` (`pipeline_start`, `pipeline_enter_maintenance`, etc.) |

---

### `test_target_rename_and_entity_name.py` (2 tests)
**Integration test YAMLs covered**: `pgsqlwal-couchbase`, `as400-pgsql-TRUNCATE`, `mssql-couchbase-collections-CUSTOM-DOCUMENT-ID`

| Test | Feature |
|------|---------|
| `test_target_table_name_override` | YAML `name` overrides target `entityObject.collection` and `table.name` |
| `test_entity_name_override` | YAML `entityName` overrides default `schema.table` entity name |

---

### `test_advanced_custom_properties.py` (3 tests)
**Integration test YAMLs covered**: `mssql-ct-mssql-ct`, `as400-pgsql-TRUNCATE`, `pgsqlwal-couchbase`

| Test | Feature |
|------|---------|
| `test_pre_post_snapshot_commands_in_target` | `preSnapshotCommand` / `postSnapshotCommand` in target `entityType` |
| `test_bulk_and_concurrency_props_in_target_entity_type` | `snapshotWritingConcurrency`, `useBulkOperationsDuringCDC`, `useBulkOperationsWhileSnapshot` |
| `test_source_custom_properties_not_leaking_snapshot_write_method` | Source `customProperties` does not contain target-only keys |

---

### `test_automator_corehub_flows.py` (5 tests)
**Coverage**: Automator UI bulk operations and pipeline management

| Test | Feature |
|------|---------|
| `test_rdbms_agents_return_sql` | `infer_agent_schema_types` returns SQL for RDBMS agents |
| `test_nosql_target_returns_nosql` | NoSQL target agent correctly inferred |
| `test_object_store_target_returns_nosql` | Object Store target normalized to NoSQL |
| `test_filters_tables_and_calls_create_entities` | `run_create_entities_for_tables` filters discovered tables by user selection |
| `test_nosql_target_disables_table_creation` | NoSQL target overrides `create_tables=True` to `False` |
| `test_normalizes_various_id_fields` | `list_pipelines` normalizes `pipelineId` / `id` / `pipeline_id` fields |

---

## Coverage Matrix

| YAML Feature | Integration Tests Using It | Unit Tests Covering It |
|-------------|---------------------------|----------------------|
| `unlockedSchema` | 17 | `test_unlocked_schema_and_target_only_columns.py` |
| `targetOnlyColumns` | 14 | `test_unlocked_schema_and_target_only_columns.py` |
| `udf` | 3 | `test_unlocked_schema_and_target_only_columns.py` |
| `targetDataType` | 4 | `test_target_data_type_override.py` |
| `chainId` / `orderIndex` | 4 | `test_multitable_chains.py` |
| `snapshotWriteMethod` | 5 | `test_snapshot_write_method_and_delete_filter.py` |
| `snapshotDeleteFilter` | 1 | `test_snapshot_write_method_and_delete_filter.py` |
| `whereClause` | 2 | `test_where_clause_and_allowed_operations.py` |
| `allowedOperations` | Many | `test_where_clause_and_allowed_operations.py` |
| Pipeline `schedules` | 1 | `test_pipeline_and_group_schedules.py` |
| `group_schedules` | 1 | `test_pipeline_and_group_schedules.py` |
| `name` (target rename) | Many | `test_target_rename_and_entity_name.py` |
| `entityName` | 1 | `test_target_rename_and_entity_name.py` |
| `preSnapshotCommand` / `postSnapshotCommand` | 1 | `test_advanced_custom_properties.py` |
| `useBulkOperations*` | 2 | `test_advanced_custom_properties.py` |
| `snapshotWritingConcurrency` | 1 | `test_advanced_custom_properties.py` |
| `writePageSize` / `writePageMultiplier` | 1 | `test_advanced_custom_properties.py` |
| `maxFetchItemsCountPerIteration` | 1 | `test_advanced_custom_properties.py` |
| `maxTransactionMessageKbSize` | 1 | `test_advanced_custom_properties.py` |
| Automator flows | — | `test_automator_corehub_flows.py` |

---

## Known Gaps (still not unit-tested)

These features are present in integration test YAMLs but have **no dedicated unit tests**:

1. **`unchangedDataFilterType`** — Used in ~56 integration tests at source customProperties level. Tested implicitly via payload generation but not explicitly asserted.
2. **`blacklist`** — Only implicitly present; no explicit exclusion test.
3. **`sourceType` / `targetType`** at schema level — Not tested in schema-level type hinting.
4. **`transactionsAudit`** — Only in template; not tested.
5. **Top-level `groups:` definition** — Only in template; not tested.
6. **`columns` with full object format** (`id`, `ordinalPosition`, negative IDs) — Used by `pgsqlwal-cassandra` and `pgsqlwal-couchbase`; tests use shorthand only.
7. **Multi-schema YAML** (`schemas:` wrapper) — Only in template; not tested.
8. **Special characters in table names** (`£$€!%&?^`) — Used by `mssql-ct-mssql-ct`; no test validates URL encoding or quoting.
9. **`partitions`** (simple string + extended dict) — Used by `mssql-ct-mssql-ct`; not unit-tested.
10. **`orderIndex` propagation** — Currently hardcoded to `0` in MultiTable agentEntities regardless of YAML value.

---

## Behaviors Observed During Test Development

1. **`orderIndex` not propagated to MultiTable agentEntity**  
   YAML `orderIndex: 1` / `orderIndex: 2` for chained tables is ignored; both source and target MultiTable `agentEntities` hardcode `"orderIndex": 0`.

2. **MultiTable result counts missing from return value**  
   `create_entities()` returns `{"successful": 0, "failed": 0, "total": 0}` when only MultiTable entities exist, because the return dict only counts regular (SingleTable) entities.

---

## Test Count Summary

| Suite | Tests |
|-------|-------|
| Pre-existing | 28 tests |
| New (2026-05-31) | 28 tests |
| **Total** | **56 tests** (66 pytest items including subtests) |

All tests pass in `0.90s` with **zero regressions**.
