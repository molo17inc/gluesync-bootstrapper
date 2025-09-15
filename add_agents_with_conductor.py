#!/usr/bin/env python3
"""
add_agents_with_conductor.py
──────────────────────────
1. Parse config.json (globals + agents).
2. POST /agents with full agent specifications.
3. POST /containers action=start to launch them.

The Conductor API handles Docker Compose generation internally.
We send complete agent specifications per OpenAPI contract.

Return structure
----------------
{
  "service_names":      [...],     # service IDs from Conductor
  "agents":             [...],     # payload sent to /agents
  "containers_started": true|false,
  "start_response":     {...}      # Conductor reply (on success)
  "start_error":        "…"        # only if start failed
  …fields echoed back by /agents…
}
"""

import os
import json
from typing import Dict, Any, Optional
import requests
from utils.log import get_logger, create_log_file

# Initialize logger
log_file = create_log_file()
logger = get_logger(log_file)

def _load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)

def _build_agent_spec(agent: dict, globals_cfg: dict) -> Dict[str, Any]:
    """
    Convert one agent block + globals into Conductor API agent specification.
    """
    tag = agent["agentTag"].lower()
    agent_type = agent["agentType"].lower()
    version = os.getenv('GSCOREVERSION', 'latest')
    logger.info(f"GSCOREVERSION: {version}")
    
    # Build environment variables
    env_vars = agent.get("environment", {})
    if not env_vars:
        env_vars = {
            "TEST_NAME": globals_cfg.get("testName", ""),
            "TYPE": agent_type,
            "LOG_CONFIG_FILE": "/opt/gluesync/data/logback.xml",
            "JOB_ID": globals_cfg.get("jobId", "")
        }
        if tag == "gridgain":
            env_vars["JAVA_TOOL_OPTIONS"] = "--add-opens=java.base/java.nio=ALL-UNNAMED"
    
    # Build volumes array (strings per OpenAPI spec)
    log_dir = os.getenv('LOG_DIR', '/tmp/logs')

    volumes = agent.get("volumes", [])
    log_volumes = [
        f"./gluesync-{tag}-{agent_type}-agent:/opt/gluesync/logs"
    ]

    volumes += log_volumes

    if not volumes:
        volumes = [
            f"{globals_cfg.get('commonDir', '/commons/gluesync')}/scripts:/scripts",
            f"{globals_cfg.get('licenseFile', './gs-license.dat')}:/opt/gluesync/data/gs-license.dat:ro",
            f"{globals_cfg.get('logbackFile', './logback.xml')}:/opt/gluesync/data/logback.xml:ro",
            f"{globals_cfg.get('securityConfig', './security-config.json')}:/opt/gluesync/data/security-config.json:ro",
            f"{globals_cfg.get('keystore', './gluesync.com.jks')}:/opt/gluesync/data/gluesync.com.jks:ro",
            # f"{globals_cfg.get('coreHubBootstrap', './bootstrap-core-hub.json')}:/opt/gluesync/data/bootstrap-core-hub.json:ro",
        ]
    
    # Build labels object
    labels = {
        "com.molo17.conductor.unique_id": tag,
        "com.molo17.conductor.type": "agent"
    }
    
    # Build dependsOn (camelCase for API)
    depends_on = agent.get("dependsOn", {})
    if not depends_on:
        depends_on = {"gluesync-core-hub": {"condition": "service_started"}}
        if tag in ("mongodb", "gridgain"):
            depends_on[tag] = {"condition": "service_healthy"}
    
    # Return full agent specification per OpenAPI schema
    return {
        "imageName": f"gluesync-{tag}",
        "type": agent_type,
        "environment": env_vars,
        "labels": labels,
        "ports": agent.get("ports", []),
        "volumes": volumes,
        "reservations": agent.get("reservations", {}),
        "limits": agent.get("limits", {}),
        "dependsOn": depends_on  # camelCase per OpenAPI spec
    }

def add_agents_with_conductor(
    config_path: str,
    conductor_url: str,
    auth_token: Optional[str] = None
) -> Dict[str, Any]:
    # 1. Load config
    try:
        cfg = _load_config(config_path)
    except Exception as exc:
        return {"error": f"Failed to load config.json: {exc}"}

    globals_cfg = cfg.get("globals", {})
    agent_specs = []

    # 2. Build agent specifications
    for agent in cfg.get("agents", []):
        spec = _build_agent_spec(agent, globals_cfg)
        agent_specs.append(spec)

    # 3. POST /agents with full specifications
    payload = {"agents": agent_specs}
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["apiKey"] = auth_token

    try:
        resp = requests.post(
            f"{conductor_url}/agents",
            json=payload,
            headers=headers,
            timeout=30
        )
        resp.raise_for_status()
        result = resp.json()
        logger.info(f"Add Agents result: {result}")
    except Exception as exc:
        return {"error": f"Request to /agents failed: {exc}"}

    # 4. Extract service names from Conductor response
    service_names = []
    if "results" in result and isinstance(result["results"], list):
        service_names = [r.get("serviceId", "") for r in result["results"] if r.get("success")]

    # 5. POST /containers action=start using Conductor's service IDs
    if service_names:
        start_body = {"action": "start", "ids": service_names}
        logger.info(f"Start Agents result: {start_body}")
        try:
            start_r = requests.post(
                f"{conductor_url}/containers",
                json=start_body,
                headers=headers,
                timeout=60
            )
            start_r.raise_for_status()
            start_json = start_r.json()
            logger.info(f"Start Agents result: {start_json}")
            containers_ok = True
            start_err = ""
        except Exception as exc:
            containers_ok = False
            start_json = {}
            start_err = str(exc)
    else:
        containers_ok = False
        start_json = {}
        start_err = "No service names returned from /agents"

    # 6. Return enriched response
    result.update({
        "service_names": service_names,
        "agents": agent_specs,
        "containers_started": containers_ok
    })
    
    if containers_ok:
        result["start_response"] = start_json
    else:
        result["start_error"] = start_err

    return result
