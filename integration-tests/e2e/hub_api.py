"""Thin Core Hub client for e2e asserts. Paths and response keys are taken from automator_app.corehub / commons."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from automator_app import corehub
from commons import configure_core_hub, fetch_core_hub, get_agent_tables, get_pipeline_agents, get_table_columns

from .env import HubEnv


class HubSession:
    """Authenticated session against a live Core Hub (fields verified in code: apiToken, /pipelines, …)."""

    def __init__(self, env: HubEnv):
        self.env = env
        self.token = corehub.authenticate(
            base_url=env.url,
            username=env.username,
            password=env.password,
            use_ssl=env.use_ssl,
            skip_verify=env.skip_verify,
        )
        configure_core_hub(env.url, use_ssl=env.use_ssl, skip_verify=env.skip_verify)
        # Pipeline ids this session's tests created — cleanup may delete ONLY these.
        self.created_pipeline_ids: List[str] = []

    def note_created(self, pipeline_id: str) -> None:
        pid = str(pipeline_id or "").strip()
        if pid and pid not in self.created_pipeline_ids:
            self.created_pipeline_ids.append(pid)

    def delete_tracked_pipelines(self) -> List[str]:
        """Delete only pipelines recorded via note_created / snapshot_new. Returns deleted ids."""
        deleted: List[str] = []
        for pid in list(self.created_pipeline_ids):
            try:
                self.delete_pipeline(pid)
                deleted.append(pid)
            except Exception:
                pass
        self.created_pipeline_ids = [p for p in self.created_pipeline_ids if p not in deleted]
        return deleted

    def snapshot_new(self, before_ids: set) -> List[str]:
        """Return pipeline ids present now but not in before_ids; record them as created by us."""
        now = {str(p.get("pipelineId") or p.get("id") or "") for p in self.list_pipelines()}
        new = sorted(i for i in (now - before_ids) if i)
        for pid in new:
            self.note_created(pid)
        return new

    def version(self) -> Any:
        """GET /version — shape unverified beyond parse_corehub_version usage; return raw."""
        return fetch_core_hub("/version", token=self.token)

    def list_pipelines(self) -> List[Dict[str, Any]]:
        """GET /pipelines — used by Automator; items typically have pipelineId / name (UNVERIFIED field set)."""
        response = fetch_core_hub("/pipelines", token=self.token)
        if isinstance(response, list):
            return response
        if isinstance(response, dict):
            for key in ("pipelines", "content", "data"):
                if isinstance(response.get(key), list):
                    return response[key]
        return []

    def pipeline(self, pipeline_id: str) -> Any:
        return fetch_core_hub(f"/pipelines/{pipeline_id}", token=self.token)

    def pipeline_config(self, pipeline_id: str) -> Any:
        return fetch_core_hub(f"/pipelines/{pipeline_id}/config", token=self.token, params={"include_secrets": "true"})

    def agents(self, pipeline_id: str) -> Dict[str, Any]:
        return get_pipeline_agents(self.token, pipeline_id)

    def agent_tables(self, pipeline_id: str, agent_id: str, schema_name: str) -> list:
        return get_agent_tables(self.token, pipeline_id, agent_id, schema_name)

    def table_columns(self, pipeline_id: str, agent_id: str, schema_name: str, table_name: str) -> Any:
        return get_table_columns(self.token, pipeline_id, agent_id, schema_name, table_name)

    def table_exists_via_config(self, pipeline_id: str, schema_name: str, table_name: str) -> bool:
        """Same path create_all_tables.table_exists uses (schema not URL-encoded there — see offline bug test)."""
        import urllib.parse
        encoded_table = urllib.parse.quote(table_name, safe="")
        encoded_schema = urllib.parse.quote(schema_name, safe="")
        try:
            fetch_core_hub(
                f"/pipelines/{pipeline_id}/config/entities/schemas/{encoded_schema}/tables/{encoded_table}",
                method="GET",
                token=self.token,
                expected_error_statuses={404},
            )
            return True
        except Exception:
            return False

    def delete_pipeline(self, pipeline_id: str) -> None:
        fetch_core_hub(f"/pipelines/{pipeline_id}", method="DELETE", token=self.token)

    def fetch_entities(self, pipeline_id: str) -> Any:
        """Via export helper used by Automator (verified path in export_template_from_corehub)."""
        from export_template_from_corehub import fetch_pipeline_entities
        return fetch_pipeline_entities(self.token, pipeline_id)

    def fetch_mapping_function(self, pipeline_id: str, udf_name: str) -> Any:
        """GET /pipelines/{id}/config/entities/mapping-functions/{name} (Automator export path)."""
        return fetch_core_hub(
            f"/pipelines/{pipeline_id}/config/entities/mapping-functions/{udf_name}",
            token=self.token,
        )

    def entity_udf_names(self, pipeline_id: str) -> list:
        """UDF names attached on entities (mappingFunctionInfo or entityType.udf)."""
        from automator_app.corehub import discover_udfs_from_entities
        entities = self.fetch_entities(pipeline_id) or []
        if not isinstance(entities, list):
            return []
        return sorted(discover_udfs_from_entities(entities).keys())

