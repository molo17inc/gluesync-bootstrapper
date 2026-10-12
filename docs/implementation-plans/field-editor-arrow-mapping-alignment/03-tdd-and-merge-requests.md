# 03 Tests first, merge request, landing

## 1. Principles

- Red first (R14.2): every change of 02 starts with a test that fails on `main` @ `2c540dd` for the reason the change
  names; the commit body says which test was red and why.
- The tests stay mock-based `unittest` modules under `integration-tests/`, with the fake CoreHub pattern of
  `test_bootstrapper_udf_is_snapshot.py`. New modules are named `test_bootstrapper_*.py` so the CI job runs them
  (`.gitlab-ci.yml:118`); the job also gains the three existing modules that pin the matrix today and are not run
  (`test_unlocked_schema_and_target_only_columns`, `test_multitable_chains`, `test_field_functions_export_import`).
- Payload tests compare the matrix as a multiset of entries, with list order inside an entry checked (D4).
- Fixtures of a 2.3 CoreHub are copied from a real GET of the feature branch (§3), not written from the plan.

## 2. Red-test backlog

| Id | Module | Test | Change |
|---|---|---|---|
| T1 | `test_bootstrapper_entity_mapping.py` (new) | `read_entry`: `(5,7)`, `(5,0)`, `(0,7)`, `(0,0)`, lists only, a list next to an agreeing scalar, a disagreeing scalar raises naming the side, a `null` list reads as absent | C-1 |
| T2 | same | `build_entries`: 1:1, N:1, 1:N, 1:null, null:1, unlocked marker; R7.4 completeness; group order by first source, ids by position then id; M:N, a column in two groups, `0` and duplicate ids refused with the column names | C-1 |
| T3 | same | `encode_entries`: lists only and no scalar key when grouped; today's four scalars otherwise; a grouped entry refused without `grouped`; `[s]/[]` and `[]/[t]` not written without `grouped` | C-1 |
| T4 | same | `relations_of`: a legacy N:1 stored as two rows reads as one N:1; unlocked tables give no relation | C-1 |
| T5 | `test_bootstrapper_entity_mapping_yaml.py` (new) | `yaml_relations` for every row of 02 §3; shorthand, explicit, whitelist and `{target, expression}` rows keep today's meaning (absent and `null` target = source name); M:N across rows refused; 2.3-only forms refused when not grouped | C-4 |
| T6 | same | `yaml_rows` then `yaml_relations` gives back the relations, for every shape | C-4, C-5 |
| T7 | `test_bootstrapper_entity_keys.py` (new) | `derive_keys`: each B1-B7 case of the backend golden table that a YAML can express (worked cases 1-3 of R12, K2b, R12.8 by name and explicit, null:1 key, NoSQL and Cassandra sides without physical keys); YAML `keys:` against a physical key refused; MongoDB source without keys refused with its message; a target identity key without an `expression` refused (BOP-13); `grouped=False` gives today's union for the existing templates | C-3 |
| T8 | `test_bootstrapper_entity_payloads_23.py` (new; the 2.2 payloads are pinned by `test_bootstrapper_payload_golden.py`) | `create_entities` on a fake 2.3 CoreHub: SingleTable payload has lists, R7.4 entries, derived keys; on a fake 2.2 CoreHub: byte-for-byte today's payload for today's templates (a repeated source, refused, is the one exception) | C-1, C-2, C-3 |
| T9 | `test_unlocked_schema_and_target_only_columns.py` (rewrite `:121-186`) | the unlocked entry is `[]/[]` on 2.3 and `(0,0)` on 2.2 | C-1 |
| T10 | `test_bootstrapper_multitable_23.py` (new) and `test_multitable_chains.py` (`:178-186`) | no entity-level matrix; a filtered MultiTable names only entity columns; `targetOnlyColumns` with an `expression` get `[]/[t]`, without one refused on 2.3 | C-1, C-9, BOP-12 |
| T11 | `test_bootstrapper_duplicate_tables.py` (new) | an `@@` table with a filtering `columns:` and a renamed target gives the same payload as the first loop for the same YAML | §4 |
| T12 | `test_sql_entity_primary_keys_regression.py` (extend `:156-200`) | a `{target, expression}` row creates the target column, its `[]/[t]` entry and its field function; keys kept | C-4, BOP-10 |
| T13 | `test_bootstrapper_export_mapping.py` (new) | export from a lists fixture keeps renames (red today: BS-1), writes the grouped forms, `target: []`, a `{target, type}` row for a UDF-produced column, `targetKeys` exactly when the stored target `isPK` set differs from the targets reached from source `isPK` columns; the scalar fixture of a 2.2.x export still exports as today | C-5 |
| T14 | `test_bootstrapper_export_import_round_trip.py` (new) | export a 2.3 entity of each shape, import it into a fake 2.3 CoreHub, compare the PUT matrix, keys and field functions with the original | C-1, C-3, C-4, C-5 |
| T15 | `test_bootstrapper_chunk_refusal.py` (new) | a chunk answered 400 is re-sent one entity at a time; the valid ones are saved, each refusal logged with CoreHub's message; `skip_errors=False` raises | C-6 |
| T16 | `test_bootstrapper_udf_is_snapshot.py` (extend `:446-470`) | the version is read once per CoreHub for both the UDF signature and the matrix; the test body carries the 2.3 fields and lists on 2.3, today's body on 2.2 | C-2, C-7 |
| T17 | `test_bootstrapper_mcp_texts.py` (new, small) | `upsert_entities` describes grouped entries; the docstring names no missing tool | C-8 |
| T18 | `test_bootstrapper_entity_payloads_23.py` | `build_field_functions_for_entity_type`: a row with `source`, `target` and a Static, LocalTs, LocalDate, TsOff or RecordID `expression` is refused on 2.3 naming table, column and function, also from a 2.2.x export fixture; the same `expression` on a `{target, expression}` row is accepted; on 2.2 the row is sent as today; a repeated `source` without `expression` gives one 1:N entry | BOP-14, C-4 |

## 3. Live checks against a local CoreHub

Not in CI. Run with the `gluesync-local-e2e` procedure (CoreHub and agents started locally, a real source and target
database), gated by environment variables like `integration-tests/test_live_backup_export.py`:

1. **CoreHub built from `feature/field-editor-arrow-mapping`** (or `release/2.3.0.0` once !2521 is merged): for each
   shape of 02 §3 and each worked key case of R12, run `create_all_entities.py` from a template, then GET the entity:
   the matrix and keys are what T8 expects, and POST `.../config/entities/validate` on the GET body reports no ERROR.
   Open the entity in the field editor: the arrows, checkboxes and keys are the ones the YAML describes (one browser
   pass, the UI half of !683 being released together).
2. **Round trip**: export the pipeline, import the YAML into a new pipeline, compare the GETs (T14 on real data).
3. **Refusals**: YAML keys against a physical key, an R12.8 explicit row, a MultiTable `targetOnlyColumns` without
   an expression, a Static `expression` on a fed target (P16, on a CoreHub that has `62ef93bb43`), an agent stopped during the run: each refused before the request or by CoreHub with the expected
   message, and the other entities of the chunk saved.
4. **2.2.x CoreHub** (current 2.2.11.x): today's templates give today's payloads and entities; a template that uses a
   2.3-only form is refused before any request. Answers PV-2 on the way (02 §1).
5. **MongoDB**: a MongoDB source without `keys:` (BS-6) and a MongoDB target whose collection holds `_id`.
6. **Missing target table at save** with `CREATE_TABLE_IF_NOT_EXISTS=false`: answers PV-1.

## 4. Merge request and landing

- One branch from `main`, `feature/field-editor-arrow-mapping-alignment`, and one merge request into `main`. Commits in
  English, one per change of 02 in the order C-2, C-1, C-4, C-3, §4 writers, C-5, C-6, C-7, C-8, docs; no
  `Co-Authored-By` trailer.
- Gates before review: the CI test job green with the new modules; T1-T18 green; the live checks of §3 recorded in the
  merge request description (CoreHub build, databases, results).
- **Landing order.** Merge after !2521 is merged into `release/2.3.0.0` and a 2.3 build with it exists, and before the
  2.3 release: the version switch sends lists to every CoreHub that reports 2.3 (BOP-5), and a 2.3 build without !2521
  would refuse them (PV-3). Until then, keep the branch rebased on `main`.
- **Release.** Tag the Automator (`automator-x.y.z`, `.gitlab-ci.yml` tag rule) with the version the maintainer picks
  (BOP-11) and name the minimum CoreHub for the 2.3-only YAML forms in the release notes of the bootstrapper.
- The merge request description lists the behaviour changes for users of the bootstrapper: BS-1 fixed, the new YAML
  forms, the key rules applied before the request on 2.3 (BOP-3, BOP-4, BOP-6), `{target, expression}` rows now
  creating their column (BOP-10), the chunk isolation, the MultiTable `targetOnlyColumns` refusal (BOP-12), the refusal of fixed or technical functions on fed targets (BOP-14).
