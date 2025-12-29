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
  "services":             [...],     # payload sent to /services
  "containers_started": true|false,
  "start_response":     {...}      # Conductor reply (on success)
  "start_error":        "…"        # only if start failed
  …fields echoed back by /services…
}
"""

# This program is part of Gluesync.
#
# Bootstrapper is dual-licensed under the following licenses:
#
# 1. GNU General Public License (GPL) Version 3
#    You may use, modify, and distribute this software under the terms of the GPL v3.
#    See the LICENSE-GPL file or <http://www.gnu.org/licenses/gpl-3.0.html> for details.
#    This option is available at no cost, but any derivative works must also be licensed under GPL v3.
#
# 2. MOLO17 Commercial License
#    Alternatively, you may use this software under the MOLO17 Commercial License,
#    which includes a warranty and permits proprietary use. Contact MOLO17 at info@molo17.com
#    for licensing terms and conditions.
#
# You must choose one of these licenses to use this software. Using this software implies
# acceptance of one of these licenses. See the accompanying LICENSE files or contact
# MOLO17 for more information.
#
# Copyright (C) 2025 MOLO17. All rights reserved.

import json
import logging
import os
import tempfile
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

def _load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)

def _build_agent_spec(agent: dict, globals_cfg: dict) -> Dict[str, Any]:
    """
    Convert one agent block + globals into Conductor API agent specification.
    """
    tag = agent["agentTag"].lower()
    agent_type = agent["agentType"].lower()
    
    # Ensure we never forward docker-compose style overrides that conductor would reject
    for forbidden_key in ("deploy", "resources"):
        if forbidden_key in agent:
            logger.warning(
                "Ignoring forbidden key '%s' in agent %s to avoid conductor deploy overrides",
                forbidden_key,
                agent.get("agentTag"),
            )
            agent.pop(forbidden_key, None)

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
    volumes = agent.get("volumes") or []

    # Build labels object
    labels = {
        "com.molo17.conductor.unique_id": tag,
        "com.molo17.conductor.type": "agent"
    }
    
    # Return full agent specification per OpenAPI schema
    spec: Dict[str, Any] = {
        "imageName": f"gluesync-{tag}",
        "type": "agent",
        "agentType": agent_type,
        "environment": env_vars,
        "labels": labels,
    }

    ports = agent.get("ports")
    if ports:
        spec["ports"] = ports
    if volumes:
        spec["volumes"] = volumes

    return spec

def _normalize_conductor_url(conductor_url: str) -> str:
    if not conductor_url:
        return conductor_url
    base = conductor_url.rstrip("/")
    if not base.endswith("/api"):
        base = f"{base}/api"
    return base


def add_agents_with_conductor(
    config_path: str,
    conductor_url: str,
    auth_token: Optional[str] = None,
    verify_ssl: bool = True,
) -> Dict[str, Any]:
    conductor_url = _normalize_conductor_url(conductor_url)
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

    # 3. POST /services with full specifications
    payload = {"services": agent_specs}
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["apiKey"] = auth_token

    # **LOG REQUEST DETAILS**
    logger.info(f"POST {conductor_url}/services")
    logger.info(f"Headers: {headers}")
    logger.info(f"Payload: {json.dumps(payload, indent=2)}")
    logger.info("SSL verification for conductor calls: %s", verify_ssl)

    try:
        resp = requests.post(
            f"{conductor_url}/services",
            json=payload,
            headers=headers,
            timeout=30,
            verify=verify_ssl,
        )
        
        # **LOG RESPONSE DETAILS**
        logger.info(f"Response Status: {resp.status_code}")
        logger.info(f"Response Headers: {dict(resp.headers)}")
        logger.info(f"Response Body: {resp.text}")
        
        resp.raise_for_status()
        result = resp.json()
        logger.info(f"Add Agents result: {result}")
    except requests.exceptions.HTTPError as exc:
        # **LOG ERROR RESPONSE BODY**
        logger.error(f"HTTP Error: {exc}")
        logger.error(f"Response Text: {resp.text}")
        return {"error": f"Request to /services failed: {exc}", "response_body": resp.text}
    except Exception as exc:
        logger.error(f"Request failed: {exc}")
        return {"error": f"Request to /services failed: {exc}"}


    # 4. Extract service names from Conductor response
    service_names = []
    if "results" in result and isinstance(result["results"], list):
        service_names = [r.get("serviceId", "") for r in result["results"] if r.get("success")]

    # 5. POST /containers action=start using Conductor's service IDs
    if service_names:
        start_body = {"action": "start", "ids": service_names}
        logger.info(f"Start services result: {start_body}")
        try:
            start_r = requests.post(
                f"{conductor_url}/containers",
                json=start_body,
                headers=headers,
                timeout=60,
            verify=verify_ssl,
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
        start_err = "No service names returned from /services"

    # 6. Return enriched response
    result.update({
        "service_names": service_names,
        "services": agent_specs,
        "containers_started": containers_ok
    })
    
    if containers_ok:
        result["start_response"] = start_json
    else:
        result["start_error"] = start_err

    return result
