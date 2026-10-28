# Run live e2e on macOS (localhost Core Hub + DBs)

The lab is **on your Mac**: Core Hub at `https://localhost` (self-signed TLS), databases on localhost ports.
These tests are **skipped on CI / the agent box** unless the env vars below are set.

## One-shot command (repo root)

```bash
cd /path/to/gluesync-bootstrapper   # feat/simple-backup-import checked out

# --- Hub (required) ---
export GS_E2E_HUB_URL=https://localhost
export GS_E2E_HUB_USERNAME=admin
export GS_E2E_HUB_PASSWORD=  # set me   # or: export GS_COREHUB_PASSWORD=...
export GS_E2E_SKIP_VERIFY=1                      # self-signed TLS

# --- Agent tags (match your Core Hub agents) ---
export GS_E2E_SOURCE_AGENT_TAG=ibm-iseries
export GS_E2E_TARGET_AGENT_TAG=mssql-ct
# Optional: names of env vars that hold agent passwords (defaults follow GS_IMPORT_AGENT_PASSWORD_<TAG>)
# export GS_E2E_SOURCE_PASSWORD_ENV=GS_IMPORT_AGENT_PASSWORD_IBM_ISERIES
# export GS_E2E_TARGET_PASSWORD_ENV=GS_IMPORT_AGENT_PASSWORD_MSSQL_CT

# --- Agent passwords (required for real import; never put in files) ---
export GS_IMPORT_AGENT_PASSWORD_MSSQL_CT='...'
export GS_IMPORT_AGENT_PASSWORD_IBM_ISERIES='...'
# Or shorter aliases the harness copies into the import names:
# export GS_E2E_TARGET_PASSWORD=  # set me
# export GS_E2E_SOURCE_PASSWORD=  # set me

# --- Target DB for create-table asserts (optional per dialect; host=localhost on Mac) ---
export GS_E2E_TARGET_DIALECT=mssql
export GS_E2E_MSSQL_HOST=127.0.0.1
export GS_E2E_MSSQL_PORT=1433
export GS_E2E_MSSQL_DATABASE=demo
export GS_E2E_MSSQL_USERNAME=sa
export GS_E2E_MSSQL_PASSWORD=  # set me
export GS_E2E_MSSQL_SCHEMA=dbo
# export GS_E2E_MSSQL_AGENT_TAG=mssql-ct

# MySQL / PostgreSQL (optional; each enables that dialect parametrize)
# export GS_E2E_MYSQL_HOST=127.0.0.1 GS_E2E_MYSQL_PORT=3306 GS_E2E_MYSQL_DATABASE=demo \
#   GS_E2E_MYSQL_USERNAME=root GS_E2E_MYSQL_PASSWORD=  # set me GS_E2E_MYSQL_AGENT_TAG=mysql
# export GS_E2E_POSTGRES_HOST=127.0.0.1 GS_E2E_POSTGRES_PORT=5432 GS_E2E_POSTGRES_DATABASE=demo \
#   GS_E2E_POSTGRES_USERNAME=postgres GS_E2E_POSTGRES_PASSWORD=  # set me GS_E2E_POSTGRES_AGENT_TAG=postgresql

# --- Automator UI (optional) ---
# export GS_E2E_AUTOMATOR_URL=http://127.0.0.1:8080

# --- Optional gates ---
# export GS_E2E_ROUNDTRIP_PIPELINE_ID=<existing-pipeline-id>
# export GS_E2E_AI_SETTINGS=1
# export GS_E2E_UDF=1

python -m pip install -q -r requirements.txt pymssql pymysql psycopg2-binary
python -m pytest integration-tests/e2e -m e2e -v
```

Offline-only (no hub; always safe on the box or Mac):

```bash
python -m pytest integration-tests/e2e -m "not e2e" -v
# or the whole package — live tests simply skip:
python -m pytest integration-tests/e2e -v
```

## Notes

- Root `conftest.py` blocks non-local HTTP; `localhost` / `127.0.0.1` / `::1` are always allowed. Extra hosts: `GS_E2E_ALLOW_HOSTS`.
- Passwords must stay in the environment (or a var named by `GS_E2E_*_PASSWORD_ENV`); overrides JSON only carries `passwordEnv` names.
- Synthetic ZIP fixtures use `192.0.2.x` / `example.invalid`; overrides retarget agents to your localhost DBs.
