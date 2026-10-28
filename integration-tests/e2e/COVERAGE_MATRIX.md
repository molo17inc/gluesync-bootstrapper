# Coverage matrix — backup import (MR !69)

Legend:
- **live** = needs `GS_E2E_HUB_URL` (+ DB/Automator env on the Mac lab)
- **offline** = mocked Core Hub / unit; always runs in `pytest -q`
- **not covered** = intentional gap (why noted)

## A. Dialect-agnostic identifier matching

| Path | Coverage | Notes |
|------|----------|-------|
| `find_by_name` / `ColumnNames` exact-then-unique-CI | offline `test_identifier_case_dialects.py` | Parametrized over every RDBMS agent tag |
| Ambiguous ID+id stays exact-only | offline same | Cosmos/NoSQL case-only |
| Residual `==` sites (whitelist, custom key, documentKey, validation, create_all_tables keys) | offline same | |
| Bare-list discovery normalize | offline + commons | Root cause of “ID not a column” when hub returns a list |

## B. Entity-count report

| Path | Coverage | Notes |
|------|----------|-------|
| Empty `whitelist: []` = allow-all → total unknown | offline `test_import_report_fixes.py` | Fixes “9 of 2” |
| Prefer counted sum when discovery > YAML | offline same | |

## C. Connector catalog (agents.json data types)

| Category | Tags (examples) | Offline | Live | Why not live |
|----------|-----------------|---------|------|--------------|
| RDBMS | mssql-ct/cdc, mysql-cdc, mariadb-cdc, postgresql-cdc, oracle-*, db2-*, ibm-iseries, informix, vertica, sap-hana, sybase, singlestore, snowflake, clickhouse, cockroachdb, yugabytedb, aws-redshift, google-bigquery, dynamodb*, scylladb*, gridgain | `test_connector_matrix_offline.py` (every tag as source + target) | mssql/mysql/pgsql matrix `test_e2e_source_target_matrix.py`; create-table in `test_e2e_create_table.py` | Oracle gated (`GS_E2E_ORACLE=1`, private nexus). Others: no local compose wired to Automator e2e |
| NoSQL | couchbase, mongodb, cassandra, cosmosdb, aerospike, ravendb, redis, hbase | offline connector matrix + golden documentKey | not covered | No Automator e2e hub with these agents in Mac lab defaults |
| Object Store | awss3, azure-data-lake, google-cloud-storage | offline (create-table skipped) | not covered | Same |
| Event Streaming | kafka, solace, google-pubsub, redis-streams | offline (create-table skipped; category→NoSQL) | not covered | Same |
| Modules / infra | core-hub, chronos, conductor, traefik, … | n/a | n/a | Not data connectors for import |

\* `dynamodb` and `scylladb` are typed **RDBMS** in agents.json — create-table is attempted. Confirm with Portal docs if that classification is intentional.

### Demo-kit compose files (`/workspace/dk/development`)

`compose.mssql.yml`, `compose.mssql-cdc.yml`, `compose.mysql.yml`, `compose.mysql8.yml`, `compose.mariadb.yml`, `compose.pgsql.yml`, `compose.oracle-21-xe.yml`, `compose.oracle-19-se2-triggers.yml`, `compose.oracle-triggers.yml`, `compose.oracle-xstream.yml`, `compose.oracle-21-olr.yml`, `compose.db2.yml`, `compose.informix.yml`, `compose.informix-12.yml`, `compose.vertica.yml`, `compose.saphana.yml`, `compose.sybase.yml`, `compose.singlestore.yml`, `compose.clickhouse.yml`, `compose.cockroachdb.yml`, `compose.yugabytedb.yml`, `compose.greenplum.yml`, `compose.opengauss.yml`, `compose.dynamodb.yml`, `compose.scylladb.yml`, `compose.gridgain.yml`, `compose.mongodb.yml`, `compose.couchbase.yml`, `compose.couchbase-cluster.yml`, `compose.cassandra*` (under commons), `compose.kafka.yml`, `compose.solace.yml`, `compose.aerospike.yml`, `compose.ravendb.yml`, `compose.minio.yml` (S3), `compose.neo4j.yml`, …

## D. Import feature paths (carry-over)

| Path | Test | Live? |
|------|------|-------|
| create table if missing | `test_e2e_create_table.py` | yes |
| column rename / filter / group | offline broad + golden | live partial |
| Automator customize / Import All | `test_e2e_automator_api.py`, `test_e2e_import_cli.py` | Automator |
| dry-run / exit codes / plan | offline + live CLI | mixed |
| Chronos schedules | offline inventory; scheduler optional | live UNVERIFIED without Chronos |
| Core Hub &lt; 2.3 payloads | `test_bootstrapper_payload_golden.py` + connector matrix `version=2.2.x` | needs older hub lab |
| RDBMS↔NoSQL / NoSQL↔NoSQL | offline connector matrix | not live |

## E. Bugs fixed in this worktree

| Bug | Kind | Fix |
|-----|------|-----|
| YAML `ID` vs discovery `id` (MySQL CI) residual exact compares | product | `find_by_name` / CI helpers across entities + tables |
| Bare list discovery → empty/wrong columns | product | `normalize_columns_payload` in `get_table_columns` |
| Report “9 of 2 entities created” | product | empty whitelist → unknown total; prefer counted |
| `destination` alias ignored (fixture used destination) | product | `_row_target` accepts destination\|target |
| Event Streaming / unknown target still CREATE TABLE | product | `is_no_create_table_target` + YAML type normalize collapse |
| Fixture empty whitelist | fixture | `gs_imp_mssql_mysql_backup_v3.zip` fills whitelist |
| Scheduler not reachable | expected | Chronos optional — not a product bug |

## F. Mac lab re-run (exact steps)

Worktree used by the agent: `/workspace/gb69c` (detached at the MR tip). On your Mac, use your checkout of `feat/simple-backup-import` (or apply `/workspace/mr69-fix-import.patch`).

### Offline (always)

```bash
cd <repo>   # with this patch applied, uncommitted is fine
python3 -m pytest -q
# Expect: 1236 passed, 57 skipped (live gates)
```

### Live Automator / Core Hub (lab)

```bash
export GS_E2E_HUB_URL=https://<hub-host>:1717
export GS_E2E_HUB_USER=<user>
export GS_E2E_HUB_PASSWORD=<password>
# Optional agents/DB as in integration-tests/e2e/env.py / RUN_E2E.md
# Oracle: GS_E2E_ORACLE=1 when nexus compose is up
python3 -m pytest -q integration-tests/e2e/test_e2e_source_target_matrix.py \
  integration-tests/e2e/test_e2e_create_table.py \
  integration-tests/e2e/test_e2e_import_cli.py \
  integration-tests/e2e/test_e2e_automator_api.py
```

### Fixture for customize-import repro

`/workspace/test-backup/gs_imp_mssql_mysql_backup_v3.zip` — whitelist filled, `trustServerCertificate: true`. Import via Automator Customize-import against MSSQL→MySQL (or reverse) pipeline.

### Parity harness (optional re-check vs origin/main)

```bash
# Worktrees: origin/main → /workspace/gb69-main ; branch → /workspace/gb69c
# Overlay main tests onto branch code: /workspace/parity-overlay
# Harness: python3 /workspace/parity-harness/run_parity.py
# Results: /workspace/parity-harness/results/summary.json
```

Live rows stay skipped on the agent box (no hub).
