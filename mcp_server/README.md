# Gluesync MCP Server

The Gluesync MCP (Model Context Protocol) server exposes CoreHub pipeline
operations as tools that any MCP-compatible AI agent can call directly.

## What AI agents can do

| Tool | Description |
|---|---|
| `list_pipelines` | List all pipelines with IDs and completion status |
| `get_pipeline` | Full config and agent details for a pipeline |
| `get_pipeline_entities` | All entities and sync state for a pipeline |
| `get_pipeline_status` | Health summary (syncing / idle / erroring / paused) |
| `discover_tables` | Source or target tables discoverable by an agent |
| `discover_columns` | Column definitions for a specific table |
| `start_entity_sync` | Start sync (optionally with snapshot) |
| `stop_entity_sync` | Stop sync for one or all entities |
| `export_pipeline_yaml` | Export pipeline config to YAML |
| `get_notifications` | Recent CoreHub events and errors |
| `get_corehub_version` | CoreHub version string |

## Authentication

Every tool accepts a `token` argument (CoreHub JWT).  
Alternatively set `COREHUB_TOKEN` in the environment to avoid passing it on every call.

---

## Transport 1 — SSE (Automator web UI)

When the Automator is running, the MCP server is automatically available at:

```
http://localhost:<AUTOMATOR_PORT>/mcp/sse
```

Connect any SSE-capable MCP client (Cursor, OpenClaw, custom agent) to that URL.

---

## Transport 2 — stdio (Claude Desktop, OpenClaw, CLI agents)

Run the server as a subprocess over stdio:

```bash
# With environment token
export COREHUB_TOKEN="your-token"
python -m mcp_server.server
```

### Claude Desktop config (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "gluesync": {
      "command": "/path/to/gluesync-automator-linux",
      "args": ["--mcp-stdio"],
      "env": {
        "COREHUB_TOKEN": "your-token",
        "CORE_HUB_URL": "https://localhost:1717"
      }
    }
  }
}
```

Or pointing directly at the Python module:

```json
{
  "mcpServers": {
    "gluesync": {
      "command": "python3",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/path/to/gluesync-bootstrapper",
      "env": {
        "COREHUB_TOKEN": "your-token",
        "CORE_HUB_URL": "https://localhost:1717"
      }
    }
  }
}
```

---

## Example agent interactions

```
User: Which pipelines are currently configured?
Agent → list_pipelines()

User: Are there any errors on pipeline abc-123?
Agent → get_pipeline_status(pipeline_id="abc-123")
Agent → get_notifications(pipeline_id="abc-123", levels=["ERROR"])

User: Start a full snapshot for all entities in pipeline abc-123
Agent → start_entity_sync(pipeline_id="abc-123", with_snapshot=true)

User: Export the QA pipeline config so I can import it to production
Agent → export_pipeline_yaml(pipeline_id="abc-123", base_url="https://qa-corehub:1717")
```
