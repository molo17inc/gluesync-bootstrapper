# Scripts

This directory contains utility scripts for the Gluesync Bootstrapper project.

## fetch-agents.py

Automatically fetches the latest agents data (the backoffice `AvailableAgents` catalog) from the Gluesync Courier inbound REST API and updates the `agents.json` file in the project root.

### Usage

```bash
COURIER_BACKOFFICE_TOKEN=... python3 scripts/fetch-agents.py
```

### What it does

1. Fetches every `AvailableAgents` row from Courier (`GET <base>/available-agents`)
2. Turns the Courier string values back into the types `agents.json` always had (booleans for the bit columns, a number for `id`), leaves out null columns and sorts the rows by `id`
3. Renames `moduleType` to `type` for compatibility
4. Saves the processed data to `agents.json` in the project root (`{"data": [...], "hasNextPage": false}`)

### CI Integration

This script is automatically run in CI/CD pipeline:

- **Stage**: `fetch-agents` (runs after `validate`)
- **Triggers**:
  - On every commit to the default branch
  - On merge request events
- **Behavior**:
  - Fetches latest agents data
  - Commits changes back to the repository if `agents.json` is updated
  - Fails the pipeline if the fetch fails

### Dependencies

- `requests` - Already included in `requirements.txt`

### API Configuration

- `COURIER_BACKOFFICE_TOKEN` (required): Courier bearer token, a masked CI/CD variable
- `COURIER_BACKOFFICE_URL` (optional): Courier backoffice base URL, defaults to `https://gluesync.hq.molo17.com/api-inbound/backoffice`

Courier is reachable from the HQ network only, so the `fetch_agents` job runs on the HQ runners (default `gs-runners-hq` tag).
