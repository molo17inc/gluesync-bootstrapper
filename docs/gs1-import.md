# Importing a GS1 (Gluesync 1.x) configuration

The GS1 import turns the entities of a Gluesync 1.x configuration into Gluesync 2 entities on an existing
pipeline. It runs from the command line (`gs1_import.py`) or from the Automator (Import tab, *Import a GS1
(Gluesync 1.x) configuration*). Both run the same three steps:

1. **Convert** (`gs1_migration`, no I/O): one draft per GS1 entity Gluesync 2 can reproduce, and a report of
   what became of every GS1 entity. Secret-looking keys are removed first, unread, and listed by JSON path.
2. **Check with Core Hub**: a Query draft is described against the source agent
   (`POST /pipelines/{id}/agents/{source}/discovery/data-model`), and every draft is validated as an entity
   (`POST /pipelines/{id}/config/entities/validate`). A document-split draft (`NOSQL_TO_SQL`) is first typed from the
   agents' discovery (`GET .../discovery/columns?tableschema=&tablename=` for its collection and each of its tables:
   the converter's placeholder types are not types Core Hub accepts); a column discovery does not know keeps the
   draft's type and is listed in a `GS1_COLUMNS_NOT_DISCOVERED` warning. The pipeline's entities are read first
   (`GET /pipelines/{id}/entities`): a draft whose entity name the pipeline already holds takes that entity's
   `entityId` and, per side (matched by agent), its `agentEntityId`, so the validation and the save update it
   instead of comparing it with itself (Core Hub's keyspace check refuses two entities writing one id prefix,
   `DOCUMENT_KEYSPACE_SHARED`). Such a draft is reported as *already imported, will be updated*. Nothing is written.
3. **Save** the drafts you choose with `PUT /pipelines/{id}/config/entities`, once you confirm.
   The drafts Core Hub checked and found valid are chosen by default; one it refused, could not reach or did not
   validate is left out until you pick it, and so is an already imported one: saving it overwrites that entity, so
   a plain re-run (`import --yes` included) saves only the new drafts. Name it with `--entities`, or tick it in the
   page (its checkbox is marked *Update*, and the confirmation lists the entities it updates). Without the check (`--offline`, *Convert only*) every draft is chosen.

## What the converter drafts

| GS1 entity | Gluesync 2 draft | Bucket |
| --- | --- | --- |
| sql-to-nosql `query` | Query entity, root-only model | `FLAT` |
| sql-to-nosql `queryDataModeling` | Query entity, a root and one MANY array per level-2 entry | `MULTI_LEVEL` |
| nosql-to-sql `dataModeling` | Couchbase source with a document model, MultiTable target | `DOCUMENT_SPLIT` |
| table or mapping entity, a QDM with more than two levels, an entity in `entitiesToApplyTtl`, an `entitiesKeyMapping` separator Gluesync 2 cannot render | none: the entity stays on GS1 | `UNSUPPORTED` |

Document ids keep GS1's layout: a QDM entity `NAME::` and its `primaryTableKeys` sorted by label (GS1 ignored
`entitiesKeyMapping` for them), a Query with an `entitiesKeyMapping` entry its prefix, separator and fields in
order, dropping null values as GS1 did, and any other Query `NAME::` and the primary key of its FROM table, which
describe proposes (`GS1_ROOT_KEY_TO_CONFIRM`: the flow takes the proposed key, check it in the report).

Core Hub's converter left the target of a Query draft without its column mapping: Gluesync 2 completes it from
describe. The flow does the same once describe answers: the model takes the typed labels and proposed keys, and
the target maps the result view of the root (its labels, then one array column per child level) onto the
collection, each column in the target agent's own type. A label named like a static field (`type`, `scope`) keeps
the static value. Without describe (`--offline`, or Core Hub unreachable) the draft keeps the converter's target,
which Core Hub refuses to save (`ROOT_KEY_NOT_MAPPED`).

Converted Query entities omit empty arrays, write GS1's `type` and `scope` as static fields, and never delete:
`allowedOperations` is `INSERT, UPDATE` until the customer signs off the delete counts of the cut-over. With
`--root-counts`, `maxDeletesPerWindow` is 1 % of the entity's roots, at least 100. Document models report their
deletes (`REPORT_ONLY`), keep the children of an absent array, keep the last of two items with one key, and write
lists and maps as GS1's JSON text. Every nosql-to-sql setting the drafts do not carry gets a `GS1_SETTING_ELSEWHERE`
note naming the Gluesync 2 agent or entity setting it maps to; a setting Gluesync 2 has no counterpart for is
named without its value, and any key whose name looks like a secret (password, pwd, passphrase, token, api key,
private key, access key, auth...) is stripped unread.

## Structure checks are Core Hub's

The checks that parse the level SQL stay in Core Hub: a statement that depends on the moment it runs
(`NON_DETERMINISTIC_QUERY`, e.g. `GETDATE()`), a key that is an expression or a column of a joined table
(`KEY_NOT_PLAIN_COLUMN`; the report's hint names the array to move into an entity of its own), a level that does not
project the root key (`KEY_NOT_PROJECTED`), and the document-model checks (`PARENT_LINK_MISSING`,
`CHILD_PK_WITHOUT_ROOT_KEY`...). An ERROR makes the entity `UNSUPPORTED` and drops its draft, exactly as Core Hub's
own converter did. For a Query draft the structure ERRORs are the ones describe answers before asking the agent;
for a document split they are the validation issues. `convert --offline` (or *Convert only* in the Automator) runs
no check and no typing: every draft the converter accepts is listed, and Core Hub refuses the faulty ones (document drafts
with their placeholder column types included) only when they are saved.

When Core Hub cannot be reached for one draft, the report says so for that entity and the draft is kept.

## Command line

```bash
export CORE_HUB_URL=https://my-corehub:1717
export GS1_IMPORT_TOKEN=...            # else a login as admin with DEFAULT_PASSWORD
python gs1_import.py convert --config gs1-config.json --pipeline <PIPELINE_ID> --output gs1-report.json
python gs1_import.py import --config gs1-config.json --pipeline <PIPELINE_ID> [--entities A,B] [--yes]
```

| Option | Meaning |
| --- | --- |
| `--repository` | `SQL_TO_NOSQL` (default), `NOSQL_TO_SQL`, or `NOSQL_TO_SQL_SYBASE` (its `fields` read column -> document field) |
| `--source-agent`, `--target-agent` | agent ids; default the pipeline's SOURCE and TARGET agents |
| `--root-counts` | JSON file: GS1 entity name -> number of roots |
| `--output` | writes the report, the drafts and Core Hub's checks |
| `convert --offline` | no Core Hub call (needs `--source-agent` and `--target-agent`) |
| `import --entities` | the drafts to save (default: the drafts Core Hub checked and found valid and not already imported; the others are listed and can be named here, which updates an already imported entity) |
| `import --yes` | confirms the save; without it the command asks, and saves nothing when it cannot ask |

## After the import

The cut-over (adopt the documents GS1 wrote, compare them, reconcile the deletes after sign-off, rollback) uses
Core Hub's own tools: `compare_documents`, `reconcile_sync` and the GS1 migration runbook of Gluesync 2.

## Tests

`integration-tests/fixtures/gs1_converter` holds, for every input of Core Hub's own converter tests, the request,
the response Core Hub's converter gave, and every structure check it ran. `test_gs1_converter.py` replays the
structure checks and asserts the Python converter answers byte for byte the same JSON. The fixtures are anonymised:
entities, tables, columns and literals are generic names (`ENTITY_01`, `C001`, `'V001'`), hosts are under
`example.test`, and passwords are placeholders.

`integration-tests/e2e/test_e2e_gs1_import.py` runs the flow against a live Core Hub; see its docstring for the
variables it needs.
