"""Build synthetic backup ZIPs for e2e (placeholder hosts 192.0.2.x, no real secrets)."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .env import SAMPLE_HOST

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "synthetic"


def rewrite_agent_tags(
    agents: Dict[str, Any],
    *,
    source_tag: str,
    target_tag: str,
) -> Dict[str, Any]:
    """Return a copy of agents-config with SOURCE/TARGET agentTag (and pipeline refs) set for live e2e.

    Synthetic fixtures use ibm-iseries/mssql-ct; live runs retarget those slots to the tags in
    GS_E2E_SOURCE_AGENT_TAG / GS_E2E_TARGET_AGENT_TAG (or a dialect's agent tag) so overrides match.
    """
    import copy
    out = copy.deepcopy(agents)
    for agent in out.get("agents") or []:
        if not isinstance(agent, dict):
            continue
        at = str(agent.get("agentType") or "").upper()
        if at == "SOURCE":
            agent["agentTag"] = source_tag
        elif at == "TARGET":
            agent["agentTag"] = target_tag
    for pipe in out.get("pipelines") or []:
        if not isinstance(pipe, dict):
            continue
        for ref in pipe.get("agents") or []:
            if not isinstance(ref, dict):
                continue
            at = str(ref.get("agentType") or "").upper()
            if at == "SOURCE":
                ref["agentTag"] = source_tag
            elif at == "TARGET":
                ref["agentTag"] = target_tag
    return out


def synth_archive(
    tag: str = "synth1",
    *,
    source_tag: Optional[str] = None,
    target_tag: Optional[str] = None,
) -> bytes:
    """Rebuild the sanitized synthetic backup ZIP (same layout as test_custom_import.archive).

    When source_tag/target_tag are set, rewrite agents-config so live overrides keys match.
    """
    entities = (FIXTURES / f"{tag}_entities.yaml").read_text(encoding="utf-8")
    if source_tag and target_tag:
        agents_obj = rewrite_agent_tags(
            agents_dict(tag), source_tag=source_tag, target_tag=target_tag,
        )
        agents = yaml.safe_dump(agents_obj, sort_keys=False)
    else:
        agents = (FIXTURES / f"{tag}_agents-config.yaml").read_text(encoding="utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("agents-config.yaml", agents)
        zf.writestr("entities.yaml", entities)
    return buffer.getvalue()


def live_synth_archive(agents_env, tag: str = "synth1") -> bytes:
    """Synthetic ZIP retagged to agents_env.source_tag / target_tag for live hub imports."""
    return synth_archive(
        tag, source_tag=agents_env.source_tag, target_tag=agents_env.target_tag,
    )


def entities_dict(tag: str = "synth1") -> Dict[str, Any]:
    return yaml.safe_load((FIXTURES / f"{tag}_entities.yaml").read_text(encoding="utf-8"))


def agents_dict(tag: str = "synth1") -> Dict[str, Any]:
    return yaml.safe_load((FIXTURES / f"{tag}_agents-config.yaml").read_text(encoding="utf-8"))



def archive_with_entities(entities: Dict[str, Any], agents: Optional[Dict[str, Any]] = None,
                          tag: str = "synth1",
                          source_tag: Optional[str] = None,
                          target_tag: Optional[str] = None) -> bytes:
    agents = agents or agents_dict(tag)
    if source_tag and target_tag:
        agents = rewrite_agent_tags(agents, source_tag=source_tag, target_tag=target_tag)
    # Force documentation IPs so a mis-run cannot hit a real customer host from fixtures alone.
    for agent in agents.get("agents") or []:
        creds = agent.get("hostCredentials") or {}
        if isinstance(creds.get("host"), str) and "example.invalid" in creds["host"]:
            creds["host"] = SAMPLE_HOST
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("agents-config.yaml", yaml.safe_dump(agents, sort_keys=False))
        zf.writestr("entities.yaml", yaml.safe_dump(entities, sort_keys=False))
    return buffer.getvalue()


def minimal_create_table_entities(
    *,
    source_schema: str = "E2E_SRC",
    target_schema: str = "E2E_TGT",
    table: str = "E2E_TAB_001",
    pre_snapshot: Optional[str] = None,
    target_schema_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Minimal entities YAML fragment for create-table-if-missing scenarios (synthetic names only)."""
    table_cfg: Dict[str, Any] = {
        "name": f"e2e_{table.lower()}",
        "keys": ["ID"],
        "columns": [
            {"source": "ID", "destination": "ID", "type": "INTEGER"},
            {"source": "NAME", "destination": "NAME", "type": "VARCHAR", "dataLength": 64},
        ],
    }
    if pre_snapshot:
        table_cfg["preSnapshotCommand"] = pre_snapshot
    if target_schema_override is not None:
        table_cfg["targetSchema"] = target_schema_override
    return {
        "schemas": {
            source_schema: {
                "target": target_schema,
                "tables": {"custom": {table: table_cfg}},
            }
        }
    }


# Minimal identity UDF (Gluesync 2.3 signature with isSnapshot). Class name == UDF name.
E2E_UDF_NAME = "UDF_E2E_IDENTITY"
E2E_UDF_JAVA = """import java.util.Map;
import kotlin.Pair;
import com.molo17.gluesync.commons.model.api.MappingFunctionOperation;
import org.slf4j.Logger;

public class UDF_E2E_IDENTITY {

    public Pair<MappingFunctionOperation, Map<String, Object>> onChange(
            Map<String, Object> newValues,
            Map<String, Object> oldValues,
            MappingFunctionOperation operation,
            boolean isSnapshot,
            Logger logger) {
        if (isSnapshot && logger != null) {
            logger.debug("e2e identity UDF snapshot row");
        }
        return new Pair<>(operation, newValues);
    }
}
"""


def entities_with_udf(
    entities: Dict[str, Any],
    *,
    udf_name: str = E2E_UDF_NAME,
    udf_type: str = "Java",
) -> Dict[str, Any]:
    """Attach ``customProperties.target.udf`` to every custom table in ``entities``."""
    import copy
    out = copy.deepcopy(entities)
    schemas = out.get("schemas") or {}
    for _src, schema_cfg in schemas.items():
        if not isinstance(schema_cfg, dict):
            continue
        custom = ((schema_cfg.get("tables") or {}).get("custom")) or {}
        for _tname, tcfg in custom.items():
            if not isinstance(tcfg, dict):
                continue
            cp = tcfg.setdefault("customProperties", {})
            target_cp = cp.setdefault("target", {})
            target_cp["udf"] = [{"name": udf_name, "type": udf_type}]
    return out


def archive_with_udf(
    entities: Dict[str, Any],
    *,
    udf_name: str = E2E_UDF_NAME,
    udf_code: str = E2E_UDF_JAVA,
    udf_type: str = "Java",
    source_tag: Optional[str] = None,
    target_tag: Optional[str] = None,
    tag: str = "synth1",
    udf_folder: str = "udf-e2e",
) -> bytes:
    """ZIP with entities + UDF source under ``udf-<id>/`` (Automator import layout)."""
    agents = agents_dict(tag)
    if source_tag and target_tag:
        agents = rewrite_agent_tags(agents, source_tag=source_tag, target_tag=target_tag)
    for agent in agents.get("agents") or []:
        creds = agent.get("hostCredentials") or {}
        if isinstance(creds.get("host"), str) and "example.invalid" in creds["host"]:
            creds["host"] = SAMPLE_HOST
    entities = entities_with_udf(entities, udf_name=udf_name, udf_type=udf_type)
    ext = ".java" if str(udf_type).lower() == "java" else ".kt"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("agents-config.yaml", yaml.safe_dump(agents, sort_keys=False))
        zf.writestr("entities.yaml", yaml.safe_dump(entities, sort_keys=False))
        zf.writestr(f"{udf_folder}/{udf_name}{ext}", udf_code)
    return buffer.getvalue()
