# Scripts

This directory contains utility scripts for the Gluesync Bootstrapper project.

## fetch-agents.py

Automatically fetches the latest agents data from the Molo17 backoffice API and updates the `agents.json` file in the project root.

### Usage

```bash
python3 scripts/fetch-agents.py
```

### What it does

1. Fetches data from the Molo17 backoffice API
2. Processes the data to rename `moduleType` to `type` for compatibility
3. Saves the processed data to `agents.json` in the project root

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

The script uses the same API endpoint and credentials as the documentation import script in the gluesync-docs-contents repository.
