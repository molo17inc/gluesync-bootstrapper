# Backup-import e2e harness (MR !69)

**Mac / localhost lab:** see [RUN_E2E.md](RUN_E2E.md) for the exact env + single pytest command.

Pytest package under `integration-tests/e2e/`.

## Markers

- Tests marked `@pytest.mark.e2e` talk to a live Core Hub / Automator / DB.
- They **skip** unless `GS_E2E_HUB_URL` (and password) are set.
- Offline tests in this package (no `e2e` mark, or always-run modules) execute without a hub.

## Environment variables (no secrets in files)

| Variable | Purpose |
|----------|---------|
| `GS_E2E_HUB_URL` | Core Hub base URL (required for live) |
| `GS_E2E_HUB_USERNAME` | default `admin` (or `GS_COREHUB_USERNAME`) |
| `GS_E2E_HUB_PASSWORD` | Hub password (or `GS_COREHUB_PASSWORD`) |
| `GS_E2E_SKIP_VERIFY` | `1` to skip TLS verify (default on) |
| `GS_E2E_ALLOW_HOSTS` | Extra hostnames allowed past the root network guard |
| `GS_E2E_AUTOMATOR_URL` | Automator UI base (for `/api/import/*`) |
| `GS_E2E_MSSQL_HOST` / `_PORT` / `_DATABASE` / `_USERNAME` / `_PASSWORD` / `_SCHEMA` | Direct MSSQL asserts |
| `GS_E2E_MYSQL_*` | Direct MySQL asserts |
| `GS_E2E_POSTGRES_*` | Direct PostgreSQL asserts |
| `GS_IMPORT_AGENT_PASSWORD_MSSQL_CT` | Target agent password for overrides |
| `GS_IMPORT_AGENT_PASSWORD_IBM_ISERIES` | Source agent password |
| `GS_E2E_SOURCE_PASSWORD` | Fallback source agent password |
| `GS_E2E_ROUNDTRIP_PIPELINE_ID` | Existing pipeline for export→import compare |
| `GS_E2E_AI_SETTINGS=1` | Enable live AI-settings path |
| `GS_E2E_UDF=1` | Enable live UDF restore path |

Sample hosts in fixtures/builders use `192.0.2.x` / `localhost` only.

## Commands

```bash
# Offline only (default when hub env unset — e2e tests skip)
cd /path/to/gluesync-bootstrapper
python -m pytest integration-tests/e2e -q

# Full unit + integration suite (no live hub)
python -m pytest integration-tests -q --ignore=integration-tests/e2e
python -m pytest integration-tests/e2e/test_offline_gaps.py \
                 integration-tests/e2e/test_offline_fan_in_chunking.py -q

# Live e2e (lab)
export GS_E2E_HUB_URL=https://localhost
export GS_E2E_HUB_PASSWORD=...          # from vault / operator
export GS_E2E_MSSQL_HOST=127.0.0.1
export GS_E2E_MSSQL_PASSWORD=...
export GS_IMPORT_AGENT_PASSWORD_MSSQL_CT=...
export GS_IMPORT_AGENT_PASSWORD_IBM_ISERIES=...
export GS_E2E_AUTOMATOR_URL=http://127.0.0.1:8080   # optional
python -m pytest integration-tests/e2e -m e2e -v
```

Optional DB drivers: `pymssql`, `pymysql`, `psycopg2-binary`.

## Layout

| File | Role |
|------|------|
| `conftest.py` | skip-unless-hub, `hub` session, dialect DB fixture |
| `env.py` / `hub_api.py` / `db_probe.py` / `zip_builder.py` | helpers |
| `test_offline_*.py` | always-run gap + bug repros |
| `test_e2e_create_table.py` | create-table-if-missing live |
| `test_e2e_import_cli.py` | CLI dry-run / exit codes / plan |
| `test_e2e_automator_api.py` | Automator `/api/import/*` |
| `test_e2e_roundtrip_udf_ai.py` | idempotent, resume, UDF/AI gates |
| `COVERAGE_MATRIX.md` | path → test → needs live? |

Do not commit secrets. Do not push from this box (token cannot push).
