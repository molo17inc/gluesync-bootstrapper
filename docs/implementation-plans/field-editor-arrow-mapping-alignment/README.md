---
title: Bootstrapper alignment with the field editor arrow mapping (Gluesync 2.3)
status: "Implemented on feature/field-editor-arrow-mapping-alignment (2026-10-12); BOP-19 open (implemented on its recommendation); PV-2, PV-3 and the live checks pending (sections 4 and 5)"
owner: gluesync-bootstrapper
lastUpdated: 2026-10-12
---

# Bootstrapper alignment with the field editor arrow mapping

Gluesync 2.3 replaces the paired-row column mapping of the field editor with arrows, backed by a grouped
`columnsMappingMatrix`, a mapping validation stage and new key rules (gluesync-kotlin MR !2521, branch
`feature/field-editor-arrow-mapping` onto `release/2.3.0.0`, plan in
`gluesync-kotlin/docs/implementation-plans/field-editor-arrow-mapping/`). This plan says what that change does to the
bootstrapper, the Automator and the bootstrapper MCP server, and how to align them. It is analysis and plan only.

How to read it:

- [01-current-state.md](01-current-state.md): every place in the bootstrapper that reads or writes the matrix, keys,
  field functions, UDF requests, unlocked schema and entity upserts, with `file:line`, and what each meets on a 2.3
  CoreHub.
- [02-design.md](02-design.md): the changes, each naming the code it rewrites (R14.4), the YAML format, export and
  import, the version switch.
- [03-tdd-and-merge-requests.md](03-tdd-and-merge-requests.md): red tests first, live checks against a local CoreHub,
  the merge request and its landing order.
- [04-open-points-and-risks.md](04-open-points-and-risks.md): open points (options, recommendation), pending
  verifications, risks.

Bases. Bootstrapper `main` @ `2c540dd`; gluesync-kotlin `feature/field-editor-arrow-mapping` @ `82048c36f3` (first
written against `918f4e5630`; the branch was rebased onto `release/2.3.0.0` @ `4848c644db` and the backend references
were checked again on 2026-10-12), plus rule P16 from `local/be-technical-ff` @ `62ef93bb43` (backend plan appendix
A.19, not pushed yet); `release/2.3.0.0` and every 2.2.x CoreHub still have the four-`Long` entry. Path prefixes: `BE:` is the gluesync-kotlin root, `ORCH` is
`BE: corehub/src/main/kotlin/com/molo17/gluesync/corehub/server/GluesyncMainRoutesOrchestrator.kt`, `BE-plan §n` is a
section of the backend plan. Bootstrapper paths are relative to the repository root. Backend requirement and decision
ids (R1-R14, D1-D30, OP-n, M1-M7, P1-P15, B1-B7) are the backend plan's; this plan's own ids are `BS-n` (breaking
points), `C-n` (changes), `BOP-n` (open points), `PV-n` (pending verifications), `RK-n` (risks), `T-n` (tests).

## 1. Summary

The bootstrapper writes every entity through `PUT /pipelines/{id}/config/entities` with no `entityId`, so CoreHub sees
each one as new (`ORCH:1972`), `mappingInputsChanged` is true, and the whole 2.3 mapping stage runs as errors on it
(`ORCH:1351-1361`): matrix rules M1-M7, producer rows P1-P15, key rules B1-B7 against discovery, fail-closed. None of
the backend's compatibility leniency (unchanged entities, the whole-pipeline import that refuses nothing) applies to a
bootstrapper run, restores from a 2.2.x backup included. The writer's legacy single pairs still decode
(`BE: commons-model/.../entity/EntityType.kt:276-300`), so the matrix shape itself does not break the import.

What breaks or changes against a 2.3 CoreHub (details in [01 §3](01-current-state.md#3-what-a-23-corehub-does-to-each-path)):

| Id | What | Effect | Where |
|---|---|---|---|
| BS-1 | The export reads `sourceColumnId`/`targetColumnId`; a 2.3 CoreHub returns lists only | every renamed target is exported under the source name, silently; N:1, 1:N, 1:null and null:1 are lost or flattened | `export_template_from_corehub.py:524-590` |
| BS-2 | Keys are the union of discovery `isPK` and YAML `keys:`, and the target also takes the source flags by name | B1/B2 refuse YAML keys that differ from a physical key, and a target whose own primary key differs; B3 refuses a source key mapped by name onto a non-key target column (R12.8); a target primary key the source does not carry (identity or default) must be in the entity and needs an `expression` (B2, P12, OP-112 A), where today the database filled it | `create_all_entities.py:352-375`, `:1136`, `:1182`, `:1213`, `:1237`, `:1758`, `:1824`, `:1876`, `:1911`, `:2222` |
| BS-3 | The `@@` duplicate loop and the MultiTable builder map every discovered column, even those the YAML filters out | M5 refuses ids that are not columns of the entity; the duplicate loop also ignores target renames and discovered target ids | `create_all_entities.py:2647-2675`, `:2685-2702`, `:3345-3375`, `:3153-3155` |
| BS-4 | MultiTable `targetOnlyColumns` are written without an entry, a producer or an unlocked schema | P12 refuses the entity (today every row of such a column fails at runtime) | `create_all_entities.py:3172-3250` |
| BS-5 | Duplicate YAML rows (`{A: x}, {A: y}`, or two sources on one target) give two entries naming one column | M7 refuses the entity (today: first wins at runtime); a YAML source discovery does not find gets a pair to no column, which M5 refuses | `create_all_entities.py:267-319`, `:1113-1153` |
| BS-6 | MongoDB discovery no longer flags `_id` as a key (`BE: mongodb/.../dao/MongoDbInformationSchemaDao.kt:118`) | a MongoDB source without YAML `keys:` gets no key and is refused ("Missing entity primary keys") | `create_all_entities.py:1395-1413` |
| BS-7 | Physical keys are read from discovery at save, fail-closed (OP-21 B) | a run while an agent is not loaded is refused for every entity; a target table that does not exist at save is pending PV-1 | `ORCH:2926-2947` |
| BS-8 | One refused entity refuses the whole request (the stage throws at the first one, `ORCH:1354-1361`) | a chunk of 50 entities fails for one bad entity; matrix, producer and key refusals answer 400 with the stage's lines (an undecodable body answered 500 before) | `create_all_entities.py:3526-3593` |
| BS-9 | The UDF test body still carries `isSchemaLocked` and lacks the 2.3 fields | no runtime effect today: the only call is commented out | `create_user_defined_functions.py:96-114`, `:342` |
| BS-10 | A YAML row, or a row of a 2.2.x export, that binds a fixed or technical field function (Static*, LocalTs, LocalDate, TsOff, RecordID) to a target a source feeds | P16 refuses the entity, with or without a UDF (backend appendix A.19, `62ef93bb43`, not pushed yet); allowed on a null:1 target | `field_function_utils.py:112-167`, `:200-238` |

Not affected: a MongoDB target `_id`, which the producer rows now judge like any column (OP-117 B,
`e491b9e6a4`), since the bootstrapper writes no unfed target column outside BS-4 and BOP-10 ([01 §3](01-current-state.md#3-what-a-23-corehub-does-to-each-path); live check 5 checks it);
unlocked-schema entities (the `(0,0)` marker decodes to `[]/[]`, valid on an unlocked table, M6, P13);
field functions on mapped columns other than the fixed or technical ones (BS-10); filters; UDF compile; the base-code route (nothing in the repository calls it, so its
automatic casts of `d3c1b4ef98` change nothing here; the "get base-code" line at `mcp_server/server.py:18` is stale).

What changes (details in [02](02-design.md)):

- **C-1 One entity-mapping module** (`entity_mapping.py`, new, reason in 02 §1) replaces the three inline builders and the
  export reader: it builds grouped entries from relations, emits every checked column (R7.4), orders ids by ordinal
  position (D4), refuses M:N and duplicates before the request, reads both wire shapes, and encodes lists for a 2.3
  CoreHub and today's single pairs for an older one.
- **C-2 One CoreHub version read** in `commons.py`, rewritten from `corehub_udfs_take_is_snapshot`; the UDF signature
  and the matrix encoding both read it.
- **C-3 Keys derived by the R12 rules** instead of the union, with the backend's MCP draft as reference
  (`BE: corehub-mcp/.../schema/EntityDraft.kt:339-438`, `:544`).
- **C-4 YAML**: the explicit column form gains list-valued `source`, `target: []` for 1:null, an optional `targetKeys`;
  the existing `{target, expression}` row becomes a real null:1 column (today it is dropped on import). Old YAML and old
  exports keep their meaning.
- **C-5 Export** reads the grouped matrix and writes the new forms only where a relation needs them.
- **Field functions on fed targets (P16)**: a fixed or technical field function (Static*, LocalTs, LocalDate, TsOff,
  RecordID) on a target that a source feeds is refused before the request on 2.3 (BOP-14); on a null:1 target it stays
  the producer the plan relies on (BOP-10, BOP-12, BOP-13).
- **C-6 Chunk refusal isolation**, **C-7 UDF test body**, **C-8 MCP tool texts**, **C-9 dead entity-level matrix removed**.

One merge request on the bootstrapper, merged after !2521 is in `release/2.3.0.0` and released with Gluesync 2.3
([03 §4](03-tdd-and-merge-requests.md#4-merge-request-and-landing)).

## 2. Scope

In scope: `create_all_entities.py` (SingleTable, `@@` duplicate and MultiTable paths), `export_template_from_corehub.py`
(SingleTable and MultiTable export), `field_function_utils.py`, `create_user_defined_functions.py`, `commons.py`,
`utils/udf_signature.py`, `mcp_server/server.py` (tool texts), `table-list-template.yaml`, `README.md`, the tests under
`integration-tests/` and `.gitlab-ci.yml` (test job).

The Automator (`automator_app/`) goes through the export and `create_entities`, so it inherits the changes; two of its
flows needed their own (added after the first review, see section 5): `duplicate_pipeline` created the entities before
cloning their UDFs, which a 2.3 CoreHub refuses for an entity that relies on its UDF, and the export's null-type
enrichment stopped at a row with a list `source`. The Automator now reports the entities a run refuses (BOP-18 A).

Out of scope, with the reason: `create_all_tables.py` (it sends only source-discovered keys, `:394-407`, which the key rules accept);
`dbmoto-converter/` (its UDF packages use `unlockedSchema`, `generate_udf_import_package.py:1056`, and its YAML keeps
today's meaning); `main.py:772-803` (passes configured entities through, and the example configuration has none); the
`gluesync-client-sdk` submodule (not initialised and not needed: no matrix code reads it); function collections
(the bootstrapper never installs one, BOP-9).

## 3. Compatibility

| CoreHub | Bootstrapper today | After this plan |
|---|---|---|
| 2.2.x and 2.3 builds without !2521 | works | unchanged payloads (single pairs, today's key union kept, BOP-5); YAML that uses a 2.3-only form is refused before any request, naming the table |
| 2.3 with !2521 | export loses renames and relations (BS-1); some imports refused (BS-2 to BS-8) | lists, R7.4-complete entries, R12 keys; every YAML form available |
| export from 2.2.x imported into 2.3 | works only where BS-2 to BS-5 do not apply | works: the reader takes both shapes and the YAML of a 2.2.x export means what it meant |
| export from 2.3 imported into 2.2.x | n/a | works for all-1:1 entities; grouped forms are refused with a message |

The bootstrapper needs a version switch for the writer only (C-2): a 2.2.x CoreHub cannot decode a lists-only entry
(four required `Long`s at `release/2.3.0.0`, `BE: commons-model/.../entity/EntityType.kt:156-162`), and the 2.3 decoder
cannot express N:1 or 1:N from single pairs (two pairs on one column are refused by M7). The reader needs no switch.

## 4. Decisions

Each is argued in [04 §1](04-open-points-and-risks.md#1-open-points). Answers given by the maintainer on 2026-10-12
are marked "answered"; the others keep the recommendation as the default the plan is written on.

| Id | Question | Answer or recommendation |
|---|---|---|
| BOP-1 | YAML syntax for N:1, 1:N, 1:null and target keys | answered A: extend the explicit column form (list-valued `source`, repeated `source` for 1:N, `target: []`, `targetKeys`), keeping backward compatibility with the old matrix: every existing YAML form and every 2.2.x export keep their meaning, the reader decodes the four-scalar entries, and a CoreHub older than 2.3 gets them (BOP-5). Sub-question answered (a): a `targetKeys:` column whose single source is not in `keys:` is refused, naming the source |
| BOP-2 | Export of relations YAML cannot express on the target CoreHub | write the new forms; an import into a 2.2.x CoreHub refuses them with a message |
| BOP-3 | YAML `keys:` that differ from the source's physical key (B1), or from the K2b keys (B6, B7) | answered A: refuse before the request, with the rule and the columns |
| BOP-4 | A source physical key matched by name onto a non-key target column while the target has its own key (R12.8) | answered A: implicit (by name): leave it unlinked (1:null) with a warning; explicit YAML row: refuse |
| BOP-5 | Writer version switch, or lists always | answered A: switch on `GET /version` (2.3 and later: lists; older: today's pairs); land after !2521 is in every 2.3 build |
| BOP-6 | MongoDB source without YAML `keys:` | answered A: refuse with a message that names `_id` as the usual choice |
| BOP-7 | One refused entity fails its chunk of 50 | on a 400, re-send the chunk's entities one by one |
| BOP-8 | The dormant UDF test request | rewrite the body to 2.3 and keep it unwired |
| BOP-9 | Entities with custom field functions restored into a new pipeline (no function collection) | out of scope; document that the collection must be installed first |
| BOP-10 | Fix the dropped `{target, expression}` rows (pre-existing) as part of this work | yes: it is the null:1 path the rewrite builds anyway (P16 allows a technical function there) |
| BOP-11 | Bootstrapper and Automator version number for the release | maintainer's call (today `automator_app/VERSION` = 1.4.10) |
| BOP-12 | MultiTable `targetOnlyColumns` without a producer (P12), and the MultiTable export that drops target names | answered A: entry plus the row's `expression`, refused without one on 2.3; export the same rows as SingleTable |
| BOP-13 | A target primary key the source does not carry (identity or default column), which B2 and P12 (OP-112 A) now require in the entity with a producer | answered A: refuse before the request, naming the column and suggesting an `expression` |
| BOP-14 | A fixed or technical field function on a target a source feeds (P16, BS-10) | answered A: refuse before the request on 2.3, naming table, column and function, and suggesting to drop the `source` (null:1) |
| BOP-15 | A target column name that differs from discovery only by case (B2 compares names case-sensitively) | answered A: on 2.3 only, write the discovered spelling (implemented) |
| BOP-16 | MultiTable chains: keys and the 2.3 forms | answered A for both: on 2.3 the keys of each table pair follow the R12 rules, and a chain table takes every 2.3 form (N:1, 1:N, 1:null, `{target, expression}`, `targetKeys`) |
| BOP-17 | Seven choices made while implementing (left-out key columns, keyless targets, chained tables in the first loop, MultiTable filtered sources, null:1 rows on 2.2, refusals after table creation, N:1 without a function) | answered: all seven A, as implemented |
| BOP-18 | How the Automator reports the entities a run refuses (`create_all_entities.main` returned nothing, so every run looked successful) | answered A: `main` returns the result with each entity not created and its reason under `errors`, and the Automator marks the run failed |
| BOP-19 | The target table of a chain table whose `name` differs from its YAML key (the first loop creates `name`, the chain entity named the key) | open, found while implementing BOP-16; implemented B: on 2.3 the table `name` gives, older CoreHubs unchanged |
| Extraction | The per-table assembly of the first loop extracted and reused by the `@@` duplicate loop in this merge request (02 §4) | answered yes: in its own commit, with T8 and T11 comparing payloads before and after (RK-5) |

BOP-15 to BOP-17 were found while implementing and answered on 2026-10-12; BOP-19, found while implementing BOP-16, is
written on its recommendation until the maintainer answers; BOP-11 at release.

Pending verifications that can change the design: PV-1 (answered: an empty set, so no design change), PV-2 (a 2.2.x runtime with a null:1 column and no entry), PV-3 (the 2.3 build used by the gs-2
suites; no such build exists yet, the maintainer will say when it does), in [04 §2](04-open-points-and-risks.md#2-pending-verifications).

## 5. Implementation

Branch `feature/field-editor-arrow-mapping-alignment` from `main` @ `2c540dd`, one commit per change:

| Commit | Change | Tests |
|---|---|---|
| test: pin the entity payloads sent to a CoreHub older than 2.3 | golden payloads of nine YAML shapes on a fake 2.2 CoreHub (RK-2); the three unrun matrix modules join CI (RK-8) | `test_bootstrapper_payload_golden` |
| refactor: share the per-table assembly with the @@ loop | 02 §4, BS-3 for `@@` tables | T11 `test_bootstrapper_duplicate_tables` |
| feat: read the CoreHub version once | C-2 | T16 part, `test_bootstrapper_udf_is_snapshot` |
| feat: add entity_mapping | C-1 | T1-T4 `test_bootstrapper_entity_mapping` |
| feat: read the 2.3 column relations from the YAML | C-4 grammar | T5 `test_bootstrapper_entity_mapping_yaml` |
| feat: derive the keys by the R12 rules | C-3 | T7 `test_bootstrapper_entity_keys` |
| feat: write SingleTable entities for the 2.3 mapping stage | writers, BOP-10, BOP-14, BOP-15 | T8 `test_bootstrapper_entity_payloads_23`, T9, T12, T18 (in T8) |
| feat: write MultiTable entities for the 2.3 mapping stage | BOP-12, C-9 | T10 `test_bootstrapper_multitable_23` |
| feat: export the 2.3 column relations | C-5 | T13 `test_bootstrapper_export_mapping`, T14 `test_bootstrapper_export_import_round_trip` |
| feat: send a refused chunk again one entity at a time | C-6 | T15 `test_bootstrapper_chunk_refusal` |
| feat: write the UDF test request body for 2.3 | C-7 | T16 |
| docs: MCP tool texts | C-8 | T17 `test_bootstrapper_mcp_texts` |
| docs: column relations and key rules | 02 §10 | |
| fix: clone the UDFs of a pipeline before its entities in the Automator | Automator duplicate on 2.3; null-type enrichment and N:1 rows | `DuplicatePipelineUdfTests` in `test_automator_backup_import`, `test_null_column_type_fallback` |
| fix: read discovery under its own names in the Automator templates | pre-existing defect found while aligning the Automator: template keys were always empty and types null | `test_bootstrapper_automator_templates` |
| feat: report the entities a run could not create | BOP-18 A | `test_bootstrapper_run_errors` |
| feat: write each table pair of a chain as a SingleTable entity on 2.3 | BOP-16 A, BOP-19 B; the 2.3 chain export writes SingleTable rows | `test_bootstrapper_multitable_23`, `test_bootstrapper_export_mapping`, `test_bootstrapper_export_import_round_trip` |

Still to do before the merge request leaves Draft: the live checks of 03 §3 against a CoreHub built from
`feature/field-editor-arrow-mapping` (force-pushed again on 2026-10-12: head `93a21ae11a` on `release/2.3.0.0` @
`3c840bfbf4`, P16 included as `ae03b09d15`; since `76079558a7` it adds only plan documents and a data-comparison
refactor, and the release only validator and Couchbase fixes, none read by the bootstrapper), PV-2, PV-3 (no 2.3
build with !2521 yet), the answer to BOP-19. Left as found on `main`: `test_object_store_table_creation` fails and is not run in CI, and the
`@@` loop reads an undefined `groups_by_name` when a duplicate table sets `group:` (a `NameError`).
