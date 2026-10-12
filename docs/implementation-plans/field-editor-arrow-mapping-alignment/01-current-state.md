# 01 Current state

Every reference is at bootstrapper `main` @ `2c540dd` unless prefixed `BE:` (gluesync-kotlin
`feature/field-editor-arrow-mapping` @ `82048c36f3`, rebased; P16 from `local/be-technical-ff` @ `62ef93bb43`, not pushed yet).

## 1. CoreHub routes the bootstrapper uses for entities

| Route | Caller | Body | 2.3 behaviour |
|---|---|---|---|
| `PUT /pipelines/{id}/config/entities` | `create_all_entities.py:3510-3515` (MultiTable, one entity per request), `:3580-3585` (chunks of 50); `automator_app/corehub.py:3710-3715` (re-send after linking a UDF); `mcp_server/server.py:1005-1012` (`upsert_entities`); `main.py:799-803` | `{entities: [...]}` without `entityId` (new) or as returned by CoreHub | the stage runs on every new or changed entity and throws at the first refused one (`ORCH:1351-1361`); 400 `InvalidColumnsMappingMatrixException` (`BE: corehub/.../route/rest/ConfigurationRoutes.kt:430`), 400 on an undecodable body (`:451`), keys refused as `MissingEntityPrimaryKeysException` |
| `GET /pipelines/{id}/entities` | `export_template_from_corehub.py:155-176` (`fetch_pipeline_entities`), `create_all_entities.py:732`, `:3612`, `:3698`; MCP `get_pipeline_entities`, `get_entity` | n/a | every entry is written with `sourceColumnIds`/`targetColumnIds` only, no scalar (BE-plan §3.2) |
| `POST .../mapping-functions/compile-mapping-function` | `create_user_defined_functions.py:249-264`; `automator_app/corehub.py:3663` | `{code, type, udfName}` | unchanged |
| `POST .../mapping-functions/test-mapping-function` | `create_user_defined_functions.py:220-246`, never called (the call at `:342` is commented out); `integration-tests/test_bootstrapper_udf_is_snapshot.py:446-460` | `UdfFunctionTestRequest` (`:96-114`) | `isSchemaLocked` ignored; new optional `fieldFunctions`, `tablesWithUnlockedSchema`, `entityUsesArrowOnlyShapes` (`BE: commons-model/.../api/MappingFunctions.kt:51-80`); `FunctionColum.isPrimaryKey` required (`:85-90`); findings in one 400 |
| `POST .../mapping-functions/base-code` | none | n/a | new required body (`BE: .../MappingFunctions.kt:111-126`); nothing to align |
| `GET .../mapping-functions/{udfName}` | `automator_app/corehub.py:1164`, `:1952`, `:3575`; `mcp_server/server.py:1195-1201` | n/a | unchanged |
| `POST .../config/entities/validate` | none | n/a | exists only on 2.3 (added by `94a133549a`, in no 2.2 tag) |
| `PUT /pipelines/{id}/config` (whole-pipeline import, never refuses for the new rules) | none | n/a | not used: the bootstrapper restores entity by entity through the upsert |
| `GET /version` | `create_user_defined_functions.py:116-139` (UDF signature), `export_template_from_corehub.py:1145` (export header), `mcp_server/server.py:1258` | n/a | reports `2.3.0.0 - build ...` for builds with and without !2521 (PV-3) |

## 2. Code map

### 2.1 Matrix writers (`create_all_entities.py`)

`create_entities` (`:448`) has three paths that each build the matrix inline with the legacy scalar shape:

- **SingleTable, first loop** (`:887-2333`). Matrix at `:1586-1691`:
  - unlocked: one `(0,0)` row plus `tablesWithUnlockedSchema = [target_table_id]` (`:1589-1602`);
  - locked with a `columns:` mapping or whitelist: one pair per entry of the source `columns_def`, target id from the
    discovered target column of the alias name, else the source id (`:1617-1647`);
  - locked without one: one pair per discovered source column, by the same name (`:1648-1677`);
  - `targetOnlyColumns` on a locked table: a no-op (`:1685-1688`); they are added only to unlocked tables
    (`:1923-2009`).
  The target columns are built from the same source columns in four branches (`:1708-1766`, `:1767-1840`,
  `:1842-1885`, `:1887-1921`), so every target column has a pair and every pair names target columns of the entity.
- **`@@` duplicate loop** (`:2335-2843`). Source columns follow the YAML mapping (`:2440-2500`), but the target
  columns are every discovered source column under its source name with the source id (`:2685-2702`) and the matrix
  pairs every discovered source column with itself (`:2647-2675`): a filtered YAML gives pairs whose source id is not
  an entity column, and target renames and discovered target ids are ignored.
- **MultiTable** (`:2850-3425`). Source columns are every discovered column (`:2918-2934`); target columns skip the
  ones the YAML filters out (`:3153-3155`) and add `customProperties.target.targetOnlyColumns` with no unlocked
  schema and no pair (`:3172-3250`); the matrix pairs every discovered source column with the same id (`:3345-3375`).
- **Entity-level matrix**: `:2811` and `:3422` put `columnsMappingMatrix` on the entity, which has no such field
  (`BE: commons-model/.../entity/Entity.kt:10-16`); the shared Json ignores it. Dead.

### 2.2 Keys

- Source `isPK` = discovery flag or YAML `keys:` (`_is_in_keys`, `:202-214`; `_coerce_is_pk`, `:322-332`) at `:1136`,
  `:1150`, `:1182`, `:1213`, `:1237`; the duplicate loop `:2463`, `:2475`, `:2491`; MultiTable `:2933`.
- Source `keys` list (`:1244-1413`): YAML keys resolved through mappings, discovery or `columns_def`, a key column the
  mapping dropped re-added to `columns_def` with `isPK` (`:1336-1350`); without YAML keys, the discovered primary key
  (`:1395-1413`). `_paint_is_pk_on_columns` (`:352-375`) then forces `isPK` on the `keys` columns (`:1416`, `:2558`).
- Target `isPK` = discovered target flag or source flag or YAML key on the source name (`:1758`, `:1824`, `:1876`,
  `:1911`; duplicate loop `:2701`; MultiTable `:3169`); target keys (`:2013-2220`) re-add a missed key column to the
  target (`:2124-2134`); painted at `:2222`, `:2764`. Target-only columns are never keys (`:2007`, `:3248`).
- NoSQL: the entity type is `NoSqlEntity` when the side's category contains "nosql" (`:1503`, `:2226`); keys follow
  the same union. There is no `_id` special case anywhere in the repository.
- Table creation sends only source-discovered keys (`create_all_tables.py:394-407`), skips creation when the YAML uses
  source→target mappings (`:538-541`), and runs before the target discovery of `create_entities` (`:1429-1448`,
  `:1566-1581`).
- `documentKey` (`:1037-1073`) maps names to source column ids for `keyMapping`; unrelated to the matrix.

### 2.3 Field functions, UDF, unlocked schema

- `field_function_utils.py`: `build_field_functions_for_entity_type` (`:200-238`) binds YAML `expression` to target
  columns by name; `apply_field_functions_to_column_mappings` (`:112-167`) attaches expressions on export and appends a
  `{target, type, expression}` row for a function whose target has no mapping. Attached at `create_all_entities.py:2231-2240`, `:2766-2775`.
- `{target, expression}` rows yield no pair (`_extract_mapping_pairs`, `:297-303`) and no target column, so on import
  `build_field_functions_for_entity_type` misses them by name and the function is dropped (pre-existing; the
  regression guards `:1220-1240`, `:1887-1921` only keep the keys).
- Custom field functions: passed through as any `expression` (`normalize_expression_for_corehub`, `:66-82`); the
  repository installs no function collection.
- UDF: `customProperties.target.udf` goes to `entityType.udf` and `mappingFunctionInfo` (`:1561-1564`, `:2643-2645`);
  compiled by `handle_udf_function_definition` (`:2326`, `:2833`) through `create_user_defined_functions.py:266-343`. The
  `onChange` signature already switches on `GET /version` (`create_user_defined_functions.py:116-155`,
  `utils/udf_signature.py:118-146`).
- Unlocked schema: YAML `unlockedSchema: true` requires a UDF (`:1000-1007`, `:2399-2404`).

### 2.4 Export (`export_template_from_corehub.py`)

- SingleTable (`_process_single_entity`, `:385-761`): `keys` from the source key list or source `isPK` (`:485-495`);
  columns sorted by `ordinalPosition` or id (`:509-522`); the matrix read from the entity, then the target entity type
  (`:524-533`; the entity-level read is dead, §2.1); for each source column, the first entry whose `sourceColumnId`
  equals its id gives the target (`:556-561`), else the source name, and a positional fallback only when there is no
  matrix (`:572-591`); one `{source, target, type, ..., isPK, id, ordinalPosition, charSet}` row per source column
  (`:603-622`); field functions attached (`:629-648`); `unlockedSchema` from `tablesWithUnlockedSchema`
  (`:661-664`).
- MultiTable (`_process_multitable_entity`, `:763-935`): whitelist rows (`name`, type, `isPK`) per table and keys from
  `isPK`; it never reads the matrix and loses target renames (pre-existing).

### 2.5 MCP server (`mcp_server/server.py`)

Pass-through tools: `upsert_entities` (`:394-415`, `:1005-1012`), `get_entity`, `get_pipeline_entities`,
`discover_columns` (`:302-317`), `get_mapping_function_code`. The module docstring lists "get base-code" (`:18`), which
no tool implements. No tool builds a matrix.

### 2.6 Tests

`integration-tests/` holds mock-based unit tests (`unittest`, a fake CoreHub), apart from `test_live_backup_export.py`.
CI runs only `test_bootstrapper_*.py` and four named modules (`.gitlab-ci.yml:118-126`): the tests that pin the matrix
today (`test_unlocked_schema_and_target_only_columns.py:121-186`, `test_multitable_chains.py:178-186`,
`test_field_functions_export_import.py:117-170`) are not run in CI.

## 3. What a 2.3 CoreHub does to each path

| Path | 2.3 decoding | 2.3 stage (new entity) | Verdict |
|---|---|---|---|
| SingleTable locked, all 1:1, keys from discovery only | pairs decode to `[s]/[t]` | passes when target keys match (B2) | works (BS-2 if the target's own key differs) |
| SingleTable with YAML `keys:` on a table with a physical key | same | B1 refuses when the YAML adds a column; B2 when the target has its own key | BS-2 |
| Source physical key mapped by name onto a non-key target column, target keyed elsewhere | same | B3 (R12.8) | BS-2 |
| Unlocked (`(0,0)` + UDF) | `[]/[]` | M6, P13 skip | works |
| `@@` duplicate with a filtering `columns:` | same | M5 | BS-3 |
| MultiTable with a filtering `columns:` | same | M5 | BS-3 |
| MultiTable `targetOnlyColumns` | same | P12 (no producer) | BS-4 |
| Two YAML rows on one source or one target | two entries | M7 | BS-5 |
| A YAML `source` that discovery does not find: generated id and no target column (`create_all_entities.py:1140-1153`) | pair decodes | M5 (the target id is no column) | BS-5 |
| Target table whose primary key the source does not carry (an identity or default column) | n/a | B2 needs the column checked and keyed, then P12 needs a producer for it (OP-112 A) | BS-2 |
| MongoDB source, no YAML `keys:` | n/a | `checkEntity` refuses an agent entity without keys | BS-6 |
| Any entity while an agent is not loaded | n/a | B-rule line "keys could not be read" (`ORCH:2926-2947`, OP-21 B) | BS-7 |
| Chunk with one refused entity | n/a | whole request 400 | BS-8 |
| Export of any 2.3 entity | n/a | n/a | BS-1 |

Key rules apply to SQL targets only (`ORCH:3457-3460` returns nothing for a `NoSqlEntity` target); a NoSQL, Cassandra or
ScyllaDB side has no physical key (`BE: commons/.../componentsetup/CheckEntitiesPhysicalKeysSymmetryComponentSetup.kt:28`,
`:36`). The producer rows apply to every locked target table (P12, `_id` included since OP-117 B), but the bootstrapper
never writes an unfed target column outside BS-4 and the dropped `{target, expression}` rows.

## 4. Reference writers

- UI builder: `gluesync-nodejs-monorepo` worktree `field-editor-arrow-mapping`,
  `packages/apps/gluesync-ui/src/app/pipeline/containers/AddEntity/utils/entityBuilder.ts:187-243`: linked components
  ordered by their first source, then one `[s]/[]` per unlinked checked source, then one `[]/[t]` per unlinked checked
  target; ids inside an entry in ordinal order (D4).
- MCP draft: `BE: corehub-mcp/src/main/kotlin/com/molo17/gluesync/corehub/mcp/schema/EntityDraft.kt`: one entry per
  drafted source and `[k]/[]` for an unmapped key column (`:161-176`); keys by R12 (`resolveKeys`, `:390-438`, with
  `k2bKeys`, `:369`); the R12.8 link dropped when matched by name, refused when explicit (`keyLinks`, `:339-366`); the
  target keeps its own primary key or inherits (`:544`).
