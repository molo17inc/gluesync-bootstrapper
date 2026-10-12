# 04 Open points, pending verifications, risks

Nothing here is decided by the plan (R14.1). Each open point has options, a recommendation and the default the plan is
written on (the recommendation, unless the maintainer answers otherwise).

## 1. Open points

### BOP-1 YAML syntax for the new relations and for target keys

Answer (maintainer, 2026-10-12): A, keeping backward compatibility with the old matrix (every existing YAML form and every 2.2.x
export keep their meaning; the four-scalar entries are still read, and still written to a CoreHub older than 2.3,
BOP-5). Sub-question: (a), noting the case should not occur in practice.

The YAML has no form for N:1, 1:N, 1:null or for target keys that differ from the source keys (R12.7, worked cases 2
and 3 of R12, K2b).
- A. Extend the explicit column form: list-valued `source` for N:1, a repeated `source` for 1:N, `target: []` for
  1:null, the existing `{target, expression}` row for null:1, a table-level `targetKeys:` (02 §3).
- B. A separate `relations:` block per table, with `columns:` keeping only the column list.
- C. No YAML form: the bootstrapper only creates 1:1 entities, and the export flattens or warns (see BOP-2).

Sub-question for A and B: with neither table keyed, a `targetKeys:` column whose single source is not in `keys:`.
(a) refuse, naming the source to add (B5); (b) add the source to the keys, as the field editor propagates (R12.6).
Recommendation (a): the YAML states the keys, and a silent addition changes the record identity at the source.

Recommendation A: one row per target, as the field editor holds relations on target rows (BE-plan D1); no existing
meaning changes (an absent or `null` target still means the source name, hence `[]` for 1:null); the export of an
all-1:1 entity is unchanged. B duplicates the column list; C makes the Automator's duplicate and backup flows lossy for
every entity the 2.3 editor saves with an arrow-only shape.

### BOP-2 Export of relations the destination cannot take

An export from 2.3 can hold N:1, 1:N, 1:null, null:1 and asymmetric keys; the YAML may be imported into a 2.2.x CoreHub.
- A. Export the new forms; the import refuses them on an older CoreHub, before any request, naming the table.
- B. Export a flattened 1:1 approximation with a warning.

Recommendation A: B changes data flow silently (a 1:null source would become a written column).

### BOP-3 YAML `keys:` that the key rules refuse

Answer (maintainer, 2026-10-12): A.

Today the source keys are the union of the physical key and YAML `keys:` (`create_all_entities.py:1136`, `:352-375`).
On 2.3, B1 refuses keys that differ from a physical key, and B6/B7 keys that differ from the K2b keys.
- A. Refuse before the request, naming the rule and the columns.
- B. Ignore the YAML keys, use the physical (or K2b) keys, log a warning.
- C. Send as today and let CoreHub refuse (one 400 per entity, after C-6).

Recommendation A: the MCP draft does the same (`BE: corehub-mcp/.../schema/EntityDraft.kt:390-438`); B changes the
keys, and so the record identity at the target, without the user seeing it; C gives the same outcome later and per
chunk. Older CoreHubs keep today's union (BOP-5).

### BOP-4 A source primary key matched onto a non-key target column (R12.8)

Answer (maintainer, 2026-10-12): A.

When the target has its own primary key on another column, B3 refuses linking a source key to a non-key target column.
- A. Matched by name (no `columns:`): leave the source key unlinked (1:null) with a warning; named by a YAML row:
  refuse. This is the MCP draft's behaviour (`EntityDraft.kt:339-366`, backend OP-26 default A).
- B. Refuse in both cases.

Recommendation A: the by-name match is the bootstrapper's guess, not the user's statement; worked case 2 of R12 is the
outcome the user wants in that situation.

### BOP-5 Writer: version switch or lists always

Answer (maintainer, 2026-10-12): A.

- A. Switch on `GET /version`: 2.3 and later get lists, R7.4 entries and R12 keys; older CoreHubs get today's payload.
- B. Lists always: drops 2.2.x CoreHubs as targets of this bootstrapper version.
- C. Today's single pairs always, lists only for N:1 and 1:N: relies on the legacy decoder for the common case and still
  needs a switch for the grouped shapes.

Recommendation A: the bootstrapper already switches the UDF signature on the same version (`create_user_defined_functions.py:116-139`),
and restores from 2.2.x installations into 2.2.x are a current use. Consequence: 2.3 builds that predate !2521 report
2.3 and refuse lists (PV-3); the merge request lands after !2521 (03 §4), as the UI does (backend OP-16 A: UI and
CoreHub ship together).

### BOP-6 MongoDB source without YAML `keys:`

Answer (maintainer, 2026-10-12): A.

Discovery no longer flags `_id` (`BE: mongodb/.../dao/MongoDbInformationSchemaDao.kt:118`), so such a YAML yields an
entity without keys, which CoreHub refuses.
- A. Refuse before the request with a message that names `_id` as the usual choice.
- B. Default to `_id` for a MongoDB source when it is discovered and no key is declared, with a warning.
- C. Do nothing (CoreHub's "Missing entity primary keys").

Recommendation A: NoSQL keys are chosen by the user (K5); B would make the bootstrapper the only writer that picks a
NoSQL key on its own. B keeps existing YAML working unchanged, which is its argument; the maintainer decides.

### BOP-7 One refused entity fails its chunk

The stage throws at the first refused entity (`ORCH:1351-1361`), so a chunk of 50 is refused for one entity.
- A. On a 400, re-send the chunk's entities one by one (C-6).
- B. POST `.../config/entities/validate` before each chunk (2.3 only) and leave out the entities with an ERROR.
- C. Chunk size 1 on 2.3.

Recommendation A: no extra request when nothing is refused, and no discovery run twice per entity as B would cost
(validate and upsert both read the physical keys, backend OP-22). B is worth offering later as a dry run of the
Automator's `/api/import/validate-all`, which today parses the archive only.

### BOP-8 The dormant UDF test request

`test_udf_function` is never called (`create_user_defined_functions.py:342`).
- A. Rewrite its body to 2.3 (C-7) and keep it unwired.
- B. Delete `UdfFunctionTestRequest` and `test_udf_function` with their test.
- C. Wire UDF testing into the import.

Recommendation A: small, keeps the test that pins `isSnapshot`; C needs sample rows the YAML does not carry.

### BOP-9 Custom field functions and function collections

N:1 and 1:N relations usually need a custom field function, which needs the pipeline's function collection; the
bootstrapper never exports or installs one. A restore into a new pipeline then fails at runtime (or at save, when the
collection checks apply).
- A. Out of scope; the README says to install the collection before the import.
- B. Export and install collections in this merge request.

Recommendation A: a separate feature with its own routes.

### BOP-10 `{target, expression}` rows are dropped on import (pre-existing)

The export writes them (`field_function_utils.py:153-163`); the import builds no target column for them, so the
function is lost (01 §2.3).
- A. Fix it here: the row becomes a null:1 column with its function (02 §3).
- B. Separate fix.

Recommendation A: it is the null:1 path the rewrite builds anyway, and P16 allows a fixed or technical function on a
null:1 target. Behaviour change: importing an old export now
creates and writes those columns, as the original entity did.

### BOP-11 Release version

`automator_app/VERSION` is 1.4.10. The maintainer picks the version and the minimum CoreHub named in the release notes.

### BOP-12 MultiTable: `targetOnlyColumns` and export rows

Answer (maintainer, 2026-10-12): A.

MultiTable `targetOnlyColumns` (`create_all_entities.py:3172-3250`) have no producer, which P12 refuses on 2.3 (and
which fails every row at runtime today); the MultiTable export writes whitelist rows without target names (`:763-935`
of the export).
- A. `targetOnlyColumns` get `[]/[t]` and the row's `expression` when it has one, and are refused before the request on
  2.3 without one; the MultiTable export writes the same rows as the SingleTable export (the import accepts them,
  `create_all_entities.py:3116-3128`).
- B. Only the refusal; the export stays as it is.

Recommendation A: the row writer is shared, and a MultiTable round trip that drops renames is the same defect as BS-1.

### BOP-13 A target primary key the source does not carry

Answer (maintainer, 2026-10-12): A.

A target table whose primary key is an identity or default column that no source column feeds: today the bootstrapper
leaves that column out of the entity and the database fills it. On 2.3, B2 needs the target's primary key in the entity
with its key flag, and P12 then needs a producer for it; the maintainer answered OP-112 A for the field editor (a target
key with a null:1 relation must have a function that generates the value, no insert-only exception).
- A. Refuse before the request, naming the column and suggesting an `expression` (for example a technical field
  function) or a relation.
- B. Add the column and let CoreHub's P12 message come back (one 400 per entity after C-6).

Recommendation A: same outcome, earlier and with the table named; the bootstrapper cannot know what the database's
identity would have produced. A technical `expression` (for example `RecordID`) is allowed here: the column has no
source, so P16 does not apply (BOP-14).

### BOP-14 A fixed or technical field function on a target a source feeds (P16)

Answer (maintainer, 2026-10-12): A.

Rule P16 (backend appendix A.19, `62ef93bb43` on `local/be-technical-ff`, not pushed yet; decided by the maintainer on
2026-10-12) refuses, on a new or modified entity and also with a UDF, a fixed or technical field function (StaticShort,
StaticInt, StaticLong, StaticFloat, StaticDouble, StaticBD, StaticStr, LocalTs, LocalDate, TsOff, RecordID) on a target
column that one or more source columns feed (1:1, 1:N, N:1); it stays allowed on a null:1 target. A target scalar
function on a fed column was already refused at every save (rule (f) of `validateScalarFunctionCalls`). The
bootstrapper meets it in two ways: a hand-written YAML row with `source`, `target` and such an `expression`, and a row
of a 2.2.x export, where `apply_field_functions_to_column_mappings` attached the function to a mapped column by target
name (`field_function_utils.py:112-167`); both bind it through `build_field_functions_for_entity_type` (`:200-238`).
Today both are accepted, and the runtime writes the function's value over the source value.
- A. Refuse before the request on 2.3, naming table, column and function, and suggesting to drop the `source` (the
  row becomes null:1, which keeps what the runtime wrote).
- B. Turn the row into null:1 on its own (drop the relation, keep the function) with a warning.
- C. Send it and let CoreHub refuse (one 400 per entity after C-6).

Recommendation A: B changes the relation without the user stating it, as BOP-3 B would change keys; C gives the same
outcome later. On a CoreHub older than 2.3 the row is sent as today (BOP-5). The check sits in
`build_field_functions_for_entity_type`. P16 reached the remote feature branch as `b1c352e912`; `2063e42c63` adds the
target scalar function on a fed column to it, which the check refuses as well. The DBMoto converter is not affected: its
expressions go to the UDF (`dbmoto-converter/generate_udf_import_package.py:977`), not to field functions.

### BOP-15 Spelling of a target column name that differs from discovery only by case

The mapping and whitelist branches write the YAML target spelling (`create_all_entities.py:1762`, `:1828`); only the
branch without `columns:` writes the discovered name (`:1874`). The 2.3 key rules compare key names case-sensitively, so
a YAML `id:` against a physical primary key `ID` is refused by B2.
- A. On 2.3 only, write the discovered spelling when the names match case-insensitively, for target names and for
  the source names of whitelist rows (B1); older CoreHubs unchanged.
- B. Do it for every CoreHub.
- C. Keep the YAML spelling and let B2 refuse.

Recommendation A: it removes a refusal 2.3 introduces without changing any 2.2.x payload (RK-2). B is a separate
behaviour change for 2.2.x users, for the maintainer to decide on its own. The plan is written on A.

### BOP-16 MultiTable chains: keys and the forms of Gluesync 2.3

Found while implementing. The 2.3 key rules run per table pair on a MultiTable target too (`entityKeyIssues` returns
early only for a NoSQL target), and the plan applies `derive_keys` to SingleTable entities only; the MultiTable
writer also builds no N:1, 1:N or 1:null relation.
- A. Apply `derive_keys` per table pair of a chain, and build the 2.3 forms in the MultiTable writer.
- B. Keep the union of discovery and YAML keys for MultiTable, refuse the 2.3 forms in a chain, and let CoreHub
  refuse a chain whose keys break the rules (one 400 per chain).

Implemented: B, the smaller change, until the maintainer answers. Recommendation A for the keys (same outcome as
SingleTable, refused earlier with the table named), B for the forms (no MultiTable user has asked for them).

### BOP-17 Choices made while implementing, to confirm at review

Each is implemented on its recommendation; the alternative is one commit away.

1. A key column that YAML `keys:` name but the mapping rows leave out. Today it is added back and mapped by name onto
   the target column of its name. A: keep that on 2.3 (implemented, BOP-1 asks old YAML to keep its meaning);
   B: leave it unlinked (1:null), as the MCP draft does for an unmapped key. Recommendation A.
2. A target left with no key column. CoreHub refuses every agent entity without keys (`checkEntity`, the target
   included). A: refuse before the request with a message naming what to add (implemented, as BOP-6 for the
   source); B: send it and let CoreHub refuse. Recommendation A.
3. Tables of a MultiTable chain in the first loop. The first loop builds a SingleTable entity for them that is not
   sent. A: build it with the older payload and no 2.3 refusal (implemented); B: skip chained tables in the first
   loop altogether, which also skips their table creation and UDF compilation. Recommendation A. The entity used to
   be dropped by parsing its name, which let a chained table with a custom `entityName` through as a SingleTable
   entity too; it is now left out by the table's `chainId`, and chains are collected from the schema being processed
   only (both fixed on this branch).
4. Source columns a MultiTable YAML filters out. 02 §4 said "a filtered column is in neither"; the source side of a
   MultiTable entity has always held every discovered column. A: keep them, each in a 1:null entry (implemented,
   source payload unchanged); B: drop them from the source columns too. Recommendation A.
5. `{target, expression}` rows on a CoreHub older than 2.3. BOP-10 makes them a column on 2.3; on an older CoreHub
   the column would have no matrix row, which PV-2 has not verified. A: leave them out there, as today, with a
   warning (implemented); B: send the column. Recommendation A until PV-2 is checked.
6. When the key rules refuse a table. They need the target's physical keys, which are read after the table is
   created (`CREATE_TABLE_IF_NOT_EXISTS`), so a refused table may already have been created. A: accept it
   (implemented); B: read the target keys first and create the table after the checks. Recommendation A.
7. N:1 without a custom field function or the UDF. CoreHub refuses it (P1); the bootstrapper does not check it
   first. A: leave it to CoreHub, isolated by C-6 (implemented); B: refuse before the request. Recommendation A.

### BOP-18 How the Automator reports the entities a run refuses

Found while aligning the Automator. `create_all_entities.main` returns nothing, so `corehub.run_create_entities` always
reports success: a refused table (before the request, or by CoreHub entity by entity after C-6) and a failed chunk show
only as ERROR lines in the run log, and an import, a duplicate or a run ends "completed". On 2.3 refusals are new and
frequent (key rules, P16, BOP-13), so a partly created pipeline looks complete.
- A. `main` returns the result of `create_entities`, which lists each refused entity and its reason under `errors`; the
  Automator then marks the import, the duplicate or the run failed with that list, as it already does for other errors.
  This also surfaces failed chunks on 2.2.x CoreHubs, which are hidden today, and a duplicate with one refused entity
  then ends in its existing "Entity cloning failed" error after the other entities were created.
- B. The same list as warnings (`refused`), success unchanged; the Automator shows it next to the result.
- C. Keep the log lines only.

Recommendation A: a failure should not read as a success, and the duplicate and import already have an error path for it.
Not implemented until the maintainer answers.

## 2. Pending verifications

| Id | Question | How | Changes |
|---|---|---|---|
| PV-1 | What the save-time key read (`ORCH:2926-2947`) answers for a target table that does not exist yet | answered 2026-10-12: an empty set, not `null`, for PostgreSQL, SQL Server and Oracle with the agent loaded (the catalog queries return no row, `BaseAgentService.getTableColumns` throws only when the database is not initialized or the query fails); the queries were run against missing tables on PostgreSQL and SQL Server, the full PUT was not (live check 6 still to run) | none: a missing target table has no physical key and the rules of a table without one apply |
| PV-2 | Whether a 2.2.x runtime writes a target column produced by a technical function that has no matrix row (what BOP-10 sends to an older CoreHub) | live check 4 | if not: refuse `{target, expression}` rows on 2.2.x, as today's result is a lost function anyway |
| PV-3 | Which 2.3 builds the gs-2 suites and the release candidates use when this merge request lands | no such build yet (maintainer, 2026-10-12); the maintainer says when it exists; then `GET /version` with the build id | the landing date of 03 §4 |
| PV-4 | That the 400 bodies of the stage carry the entity name in a form the bootstrapper's log shows (`ConfigurationRoutes.kt:430`) | live check 3 | message formatting in C-6 only |

## 3. Risks

| Id | Risk | Mitigation |
|---|---|---|
| RK-1 | The key derivation drifts from the backend's B1-B7 or from the field editor | T7 mirrors the backend golden cases by id; live check 1 opens each entity in the field editor; the derivation follows the MCP draft, which the backend tests |
| RK-2 | 2.2.x payloads change by accident | T8 pins today's payload for today's templates on a fake 2.2 CoreHub |
| RK-3 | Lists sent to a 2.3 build without !2521 | landing order (03 §4), PV-3 |
| RK-4 | Imports that worked on 2.2.x are refused on 2.3 (YAML keys, R12.8, MultiTable filters, a target identity key, an agent not loaded) | refusals before the request name the table and the rule; C-6 keeps the other entities; the merge request description lists them |
| RK-5 | The extraction of the per-table assembly (02 §4) changes the first loop's payloads | T8 and T11 compare payloads before and after on the existing templates; the extraction is its own commit |
| RK-6 | Old exports now create columns they used to drop (BOP-10) | named in the release notes; the columns are the ones the original entity wrote |
| RK-7 | `{source: [..]}` rows edited by hand bind a custom function whose arguments do not match the sources; a repeated `source` (1:N) without a function silently copies the value to every target | CoreHub's CFF binding checks refuse the first at save (backend OP-108 A); the second is a WARNING of `POST /validate`, not a refusal (backend appendix A.18, `81b5e46b51`); the README example shows the binding and states the copy |
| RK-8 | The tests that pin the matrix today are not run in CI, so the rewrite could break them unseen | 03 §1 adds them to the CI job |
| RK-9 | A CoreHub whose `GET /version` cannot be read is taken as 2.3 (C-2, as for the UDF signature), and a 2.2.x CoreHub cannot decode a lists-only entry: every entity of the run would fail there, not one UDF compile | the merge request description names it; the version read logs what it got and the choice it made |

## 4. Summary for the maintainer

Answered on 2026-10-12: BOP-1 A (with backward compatibility, sub-question (a)), BOP-3 A, BOP-4 A, BOP-5 A, BOP-6 A,
BOP-12 A, and the per-table extraction of 02 §4 in this merge request, in its own commit. Found while implementing
and written on their recommendation: BOP-15, BOP-16 (B implemented), BOP-17. BOP-18 is open, not implemented. To answer before
implementation: none; BOP-13 A and BOP-14 A were answered the same day. BOP-15 was found while implementing and is
written on its recommendation until the maintainer answers. BOP-2, BOP-7, BOP-8, BOP-9 and BOP-10 can
take their recommendations at review; BOP-11 at release. PV-1 and PV-3 are checked before the merge request leaves
Draft.
