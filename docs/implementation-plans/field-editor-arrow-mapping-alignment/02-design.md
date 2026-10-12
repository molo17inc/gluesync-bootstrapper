# 02 Design

Each change names the code it rewrites (R14.4). The plan is written on the recommendations of the open points
([04 §1](04-open-points-and-risks.md#1-open-points)); where an answer would change a section, the section says so.

## 1. C-1 One module for the entity mapping

**Why a new file.** The matrix is built inline three times in `create_all_entities.py` (`:1586-1691`, `:2647-2675`,
`:3345-3375`), read once in `export_template_from_corehub.py` (`:524-591`), and the keys are computed in a dozen
expressions of the same file (01 §2.2). The export and the import must share the reader, the relation model and the
key rules, and the only module both import today for entity content is `field_function_utils.py`, which is about field
functions. `commons.py` (1,128 lines of CoreHub client helpers, discovery and type mapping) could hold it, but would
mix pure payload rules with I/O. So one new module, `entity_mapping.py` at the repository root, next to
`field_function_utils.py` and in its style: pure functions over dicts, no I/O, no classes. Nothing else is added.

Contents:

- `read_entry(entry) -> (source_table_id, target_table_id, source_ids, target_ids)`: the CoreHub decoder's rules
  (`BE: commons-model/.../entity/EntityType.kt:276-300`): a list wins; a scalar next to it must agree (`[v]` with `v`,
  `[]` with `0`) or a `ValueError` names the side; a scalar alone gives `[]` for `0` and `[v]` otherwise; a `null` list
  counts as absent. Serves the export (both shapes: 2.2.x and 2.3 CoreHubs) and the tests.
- `relations_of(entries, source_columns, target_columns)`: per table pair, each target column with the union of its
  sources across entries (a legacy N:1 stored as two rows reads as one N:1, as the UI loader does, BE-plan D27), the
  source columns no entry names or with an empty target list (1:null), the target columns with no source (null:1).
  Unlocked tables (`tablesWithUnlockedSchema`) give no relation.
- `build_entries(source_table_id, target_table_id, relations, checked_sources, checked_targets)`: grouped entries
  from relations, R7.4-complete like the UI builder (`entityBuilder.ts:187-243`): the linked groups ordered by their
  first source, then one `[s]/[]` per checked source without a relation, then one `[]/[t]` per checked target without
  one; ids inside an entry by ascending ordinal position (`position` of discovery), then id (D4, OP-12 A). It refuses,
  before any request and naming table and columns: a column in two groups (M7), a group with several sources and
  several targets (M:N, R8), an id `0`, a duplicate id, and an entry with several columns on one side and none on the
  other (the decoder refuses it, `BE: commons-model/.../entity/EntityType.kt`, `ColumnMappingMatrix` init). Unlocked target table: one `[]/[]` entry (today's marker).
- `encode_entries(entries, grouped)`: `grouped` true writes `sourceTableObjectId`, `targetTableObjectId`,
  `sourceColumnIds`, `targetColumnIds` and no scalar (what CoreHub writes, OP-54 C); false writes today's four scalars,
  `0` for an empty side, and refuses an entry with more than one id on a side (it cannot be sent to a 2.2.x CoreHub).
  With `grouped` false, `[s]/[]` entries are not written (a 2.2.x runtime fails a `(s,0)` row, BE-plan §4.13) and
  neither are `[]/[t]` entries (PV-2); the marker is written as `(0,0)` as today.
- `derive_keys(...)`: the key rules of §5.
- `yaml_relations(columns_yaml, ...)` and `yaml_rows(relations, ...)`: the YAML grammar of §3, in both directions.

## 2. C-2 One CoreHub version read

`commons.py` gains `corehub_version(token)`, cached per CoreHub base URL for the run, rewritten from
`_IS_SNAPSHOT_SUPPORT_BY_COREHUB` and `corehub_udfs_take_is_snapshot` (`create_user_defined_functions.py:116-139`),
which then reads it; `utils/udf_signature.py:118-146` keeps `parse_corehub_version` and the `isSnapshot` predicate.
The matrix encoding asks `corehub_version(token) >= (2, 3)` (BOP-5); an unreadable version counts as current, as the
UDF signature does today. `create_entities` reads it once and passes `grouped` down to the three paths.

## 3. C-4 YAML

The explicit column form gains two things and keeps every meaning it has today: an absent `target` and `target: null`
still mean the source name (`create_all_entities.py:292-294`), the shorthand `{SRC: tgt}` is still 1:1, the whitelist
`{name, sourceName}` rows (DBMoto) are unchanged.

| Relation (R8) | YAML row(s) under `columns:` | CoreHub needs (P rows) |
|---|---|---|
| 1:1 | `- ID: id`, or `- source: ID` / `target: id` | nothing (P15) |
| N:1 | `- source: [FIRST_NAME, LAST_NAME]` / `target: full_name` / `expression: {type: CustomFn, functionName: concat, arguments: [{type: SourceColumnArg, name: FIRST_NAME}, {type: SourceColumnArg, name: LAST_NAME}]}` | a custom field function on the target, or the entity UDF (P1-P4) |
| 1:N | one row per target with the same `source`, each with an optional `expression` | nothing: without a custom field function or the UDF each target gets a copy of the source value (a WARNING of `POST /validate`, backend appendix A.18) |
| 1:null | `- source: INTERNAL_NOTE` / `target: []` | nothing; the column is read, never written |
| null:1 | `- target: ingest_ts` / `type: TIMESTAMP` / `expression: {type: LocalTs}` (today's export row) | a technical field function, a custom one, or the UDF (P7-P11) |
| keys | `keys:` source key columns (as today); `targetKeys:` target key columns, written only where they differ from the keys the rules derive (§5) | R12 |

On 2.3 an `expression` on a row with a `source` must not be a fixed or technical field function (Static*, LocalTs,
LocalDate, TsOff, RecordID): P16 refuses it on a fed target, so `build_field_functions_for_entity_type` (`field_function_utils.py:200-238`), the one
place that holds both the relation and the function, refuses it before the request (BOP-14); it stays allowed on a
`{target, expression}` row (null:1). The rows of an imported 2.2.x export go through the same function. P16 is on
`feature/field-editor-arrow-mapping` (`b1c352e912`, rebased from `62ef93bb43`), and since `2063e42c63` it also names a
target scalar function on a fed column (already refused there at every save by rule (f)), which the check refuses too.

The `CustomFn` shape is CoreHub's (`BE: commons-model/.../entity/FieldFunction.kt:900-925`); arguments bind by source
name (BE-plan D18). Rows are grouped into entries by `build_entries`: rows sharing a source form one 1:N entry; a row
with list-valued `source` is one N:1 entry; a source that appears in an N:1 row and in another row is M:N and refused.
On a CoreHub older than 2.3 any of `source: [...]`, a repeated source, `target: []` and `targetKeys` is refused
before the first request, naming the table. A repeated source is the one change for an older CoreHub: today it gives
two source column entries with one id and is sent as it is. The checked columns are the columns the rows name, as today; with no
`columns:` every discovered column is checked and related by name, as today.

`yaml_relations` rewrites `_extract_mapping_pairs` (`create_all_entities.py:267-319`): it returns relation rows
(source names, target names, the row) instead of pairs, and keeps the metadata handling GSSD-1355 settled (the comment
at `:307-313`). `_is_column_mappings` (`:224-243`) and `_is_column_whitelist` (`:246-264`) keep their detection; a list
`source` is a mapping row.

The `{target, expression}` row becomes a real null:1 column (BOP-10): the target column is added from the row's type
(or the discovered target column of that name), with an `[]/[t]` entry on 2.3 and the function bound by
`build_field_functions_for_entity_type` (`field_function_utils.py:200-238`), which then finds it by name. This rewrites
the regression guards that keep only the keys when the mapping produces no column (`create_all_entities.py:1220-1240`,
`:1887-1921`): the target-only rows no longer empty the mapping.

## 4. Writers

- **SingleTable, first loop.** The matrix block `:1586-1691` becomes `build_entries` over the relations of §3 and
  `encode_entries(grouped)`; the unlocked branch keeps `tablesWithUnlockedSchema` and the marker. The four target
  column branches (`:1708-1921`) are rewritten to walk the relations, so a target column exists for each target the
  relations name and for each target physical key (§5), and none for a 1:null source.
- **`@@` duplicate loop.** Its matrix (`:2647-2675`) and its simplified target columns (`:2685-2702`) are the cause of
  BS-3. The per-table assembly of the first loop (source columns, relations, target columns, keys, matrix, field
  functions) is extracted into one function that both loops call; the second loop's copies are deleted. This is the
  only change that touches the loop structure, and it is needed: two copies of the same rules cannot be kept in step.
- **MultiTable.** The matrix (`:3345-3375`) becomes `build_entries` per table pair over the source and target columns
  the entity holds, so a filtered column is in neither (BS-3); `targetOnlyColumns` of a MultiTable table get a `[]/[t]`
  entry and their `expression` if the YAML row has one, and are refused before the request on 2.3 when they have none
  (BOP-12).
- **Dead entity-level matrix (C-9).** `:2811`, `:3422` and the entity-level read at
  `export_template_from_corehub.py:525-529` are deleted.

## 5. C-3 Keys

On a 2.3 CoreHub with a SQL target the keys are derived by the R12 rules, so that a payload built from a valid YAML passes B1-B7; on an
older CoreHub today's union stays (BOP-5), since its symmetric check expects it. Both are one function: `derive_keys`
takes the same `grouped` flag as `encode_entries` and, when it is false, returns today's union (discovery `isPK` or
YAML key on each side, the target also taking the source flags by name), so there is one key path. `derive_keys` rewrites the source and
target `isPK` expressions (01 §2.2) and `_paint_is_pk_on_columns` (`create_all_entities.py:352-375`); the `keys` lists
fed to table creation are unchanged. The backend MCP draft is the reference implementation
(`BE: corehub-mcp/.../schema/EntityDraft.kt:339-438`, `:544`).

The key rules do not run for a NoSQL target (`ORCH:3457-3460` returns no issue for a `NoSqlEntity` target), so a NoSQL
target keeps today's union on 2.3 too, and BOP-3 and BOP-4 refuse nothing there; BOP-6 still applies, since a source
without keys is refused whatever the target.

The key rules compare the persisted key names with the discovered ones as case-sensitive sets (`KeyTable.primaryKeyIssue`).
On the 2.3 path a YAML target name that matches a discovered target column case-insensitively is written with the
discovered spelling, so a hand-written `id:` against a physical `ID` passes B2, and so is the source name of a whitelist
row (B1); on an older CoreHub the YAML spelling stays (BOP-15).

Inputs per table pair: `Ph_S`, `Ph_T` = the discovered `isPK` columns of the source and of the existing target table
(read after table creation, `:1566-1581`), empty on a side that does not honour physical keys (NoSQL category, or agent
tag `cassandra` or `scylladb`, mirroring `BE: commons/.../CheckEntitiesPhysicalKeysSymmetryComponentSetup.kt:28`,
`:36`); YAML `keys:` and `targetKeys:`; the relations.

1. Source keys `K_S`: `Ph_S` when not empty (B1); else, when `Ph_T` is not empty, the single source of each relation
   onto a `Ph_T` column (K2b, B6, B7); else the YAML `keys:`. YAML `keys:` that differ from the first two are refused
   (BOP-3) with the rule and the columns; empty `keys:` take them.
2. Links (B3, R12.8): with `Ph_T` not empty, a `Ph_S` source related to a target outside `Ph_T` is unlinked (1:null)
   with a warning when the relation came from name matching, and refused when a YAML row names it (BOP-4).
3. Target keys `K_T`: `Ph_T` when not empty (B2); its columns are always target columns (R5.4), and one with no
   relation and no expression is refused (R12.9, P12). Else the targets of relations with a source in `K_S` (B4, B5),
   plus `targetKeys:`; with neither table keyed, a `targetKeys:` column whose single source is not in `K_S` is refused
   (B5 reverse; the field editor would propagate the key to the source instead, R12.6, see BOP-1), and a key column
   produced by a function needs no relation (R12.4). A target primary key the source does not carry (an identity or
   default column) is refused before the request unless the YAML gives it an `expression` (BOP-13).
4. No keys at all on the source: refused before the request with the message of BS-6 for a MongoDB source (BOP-6),
   today's "Missing entity primary keys" otherwise.

`documentKey` (`:1037-1073`) is untouched.

## 6. C-5 Export

`_process_single_entity` (`export_template_from_corehub.py:509-622`) reads the target entity type's entries with
`read_entry`, takes `relations_of`, and writes the rows with `yaml_rows`: a 1:1 relation as today's
`{source, target, type, ..., isPK, id, ordinalPosition, charSet}` row, so an all-1:1 export reads as before; the other
shapes in the forms of §3. `apply_field_functions_to_column_mappings` (`field_function_utils.py:112-167`) keeps
attaching expressions by target name and appending null:1 rows; a null:1 target without a function (produced by the UDF)
gets a `{target, type}` row. `keys:` stays the source key list (`:485-495`); `targetKeys:` is written when the target's
stored `isPK` set differs from the set of targets reached by a relation from a source `isPK` column. This reads stored
data only: the export has no discovery, and after a 2.3 save the stored flags are the derived keys (BE-plan D21). The positional fallback (`:572-591`) stays for an entity with
no matrix.

MultiTable export (`:763-935`) never read the matrix and writes whitelist rows without target names; aligning it with
the same rows is BOP-12 (recommended in this MR, since the import already accepts mapping rows, `:3116-3128`).

## 7. C-6 A refused entity does not take its chunk down

The chunk loop's `except` (`create_all_entities.py:3586-3593`) is rewritten: on an HTTP 400 for a chunk of more than one
entity, its entities are sent again one by one, so the valid ones are saved and each refusal is logged with CoreHub's
message and counted per entity (BOP-7). `skip_errors=False` still raises at the first refusal. Errors still surface as
`requests.exceptions.RequestException` through `utils/core_hub_client.py:124-164`, so callers need no change.

## 8. C-7 UDF test body

`UdfFunctionTestRequest` (`create_user_defined_functions.py:96-114`) gains `fieldFunctions: list[dict] = []`,
`tablesWithUnlockedSchema: list[int] = []` and `entityUsesArrowOnlyShapes: Optional[bool] = None` (left out of the body
when `None`); its matrix is written with `encode_entries` for the CoreHub's version; the column dicts carry
`isPrimaryKey` (required by `BE: .../MappingFunctions.kt:85-90`). `isSchemaLocked` stays: 2.2.x requires it and 2.3
ignores it, so no switch is needed for the other fields either (both ignore unknown keys). It stays unwired (BOP-8).

## 9. C-8 MCP server texts

`upsert_entities` (`mcp_server/server.py:394-415`) states the grouped entry shape, that every checked column must
appear, and the 400 reasons (matrix, producers, keys, an agent not loaded); the "get base-code" line of the docstring
(`:18`) is corrected to what the tools do. No tool is added.

## 10. Documentation

`README.md` "Table Configuration", "Unlocked Schema Feature" and "Target-Only Columns" describe the forms of §3, the
key rules of §5 and the 2.3 requirement; `table-list-template.yaml` gets one example per shape next to the VEHICLES
example (`:290-323`).

## 11. Rewrites and additions

| Rewritten | By |
|---|---|
| `create_all_entities.py:1586-1691`, `:2647-2675`, `:3345-3375` (matrix) | C-1 `build_entries`, `encode_entries` |
| `create_all_entities.py:267-319` (`_extract_mapping_pairs`) | C-4 `yaml_relations` |
| `create_all_entities.py:1220-1240`, `:1887-1921` (regression guards) and the target column branches `:1708-1921` | C-4, §4 |
| the `@@` loop's copies (`:2440-2775`) | §4: the extracted per-table assembly |
| `create_all_entities.py:352-375` and the `isPK` expressions of 01 §2.2 | C-3 `derive_keys` |
| `create_all_entities.py:3586-3593` (chunk `except`) | C-6 |
| `export_template_from_corehub.py:509-622` (rows), `:525-529` (dead read) | C-5 |
| `field_function_utils.py:200-238` (`build_field_functions_for_entity_type`): refuses a fixed or technical function on a fed target on 2.3 | BOP-14 |
| `create_user_defined_functions.py:96-139` (test body, version cache) | C-7, C-2 |
| `mcp_server/server.py:18`, `:394-415` (texts) | C-8 |
| `create_all_entities.py:2811`, `:3422` (dead entity-level matrix) | deleted (C-9) |

| Added | Why nothing existing could be rewritten |
|---|---|
| `entity_mapping.py` | §1 |
| `commons.corehub_version` | moved from `create_user_defined_functions.py`, which `export_template_from_corehub.py` and `create_all_entities.py` must not import for it |
| `columns:` list-valued `source`, `target: []`, `targetKeys:` | R8 shapes and R12 target keys have no YAML form today (BOP-1) |
