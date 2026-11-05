# This program is part of Gluesync.
#
# Automator is dual-licensed under the following licenses:
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

"""The GS1 configuration import of the Automator UI: convert and check, then save the drafts the operator confirms.

``POST /api/gs1/convert`` converts an uploaded GS1 configuration for a pipeline and has Core Hub describe and
validate every draft; it saves nothing and keeps the plan under an id. A draft whose entity the pipeline already
holds is listed under ``existing``: saving it updates that entity, so the page leaves it unchecked.
``POST /api/gs1/save`` saves the drafts of that plan the operator chose, and only when the request says the
operator confirmed.
"""

import json
import logging
import threading
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

import gs1_import
from gs1_migration import REPOSITORIES, conversion_request, load_config
from gs1_migration.json_values import NestedTooDeeply

from . import corehub
from .state import state

logger = logging.getLogger(__name__)

_MAX_PLANS = 5
_PLANS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_PLANS_LOCK = threading.Lock()

_RESPONSES: Dict[Any, Dict[str, Any]] = {
    400: {"description": "The configuration, the choice or the confirmation is refused"},
    401: {"description": "Not signed in to a Core Hub"},
    404: {"description": "The plan is no longer kept: convert again"},
    413: {"description": "The configuration is larger than GS_IMPORT_MAX_UPLOAD_MB"},
    500: {"description": "Core Hub failed"},
}


class Gs1SaveRequest(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    plan_id: str = Field(..., alias="planId")
    entities: List[str]
    # The operator answered yes to the save prompt; without it nothing is written.
    confirmed: bool = False


def _require_session() -> None:
    if not state.token or not state.base_url:
        raise HTTPException(status_code=401, detail="Sign in to a Core Hub first")


def _keep(plan: Dict[str, Any]) -> str:
    plan_id = uuid.uuid4().hex
    with _PLANS_LOCK:
        _PLANS[plan_id] = plan
        while len(_PLANS) > _MAX_PLANS:
            _PLANS.popitem(last=False)
    return plan_id


def _kept(plan_id: str) -> Dict[str, Any]:
    with _PLANS_LOCK:
        plan = _PLANS.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="The conversion is no longer kept: convert the configuration again")
    return plan


async def _read_capped(file: UploadFile) -> bytes:
    """The uploaded configuration, read in chunks; 413 as soon as it exceeds GS_IMPORT_MAX_UPLOAD_MB, as for analyze."""
    from .app import import_upload_max_bytes  # app.py imports this module: look it up when called

    max_bytes = import_upload_max_bytes()
    chunks, size = [], 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"The configuration is larger than {max_bytes // (1024 * 1024)} MB (GS_IMPORT_MAX_UPLOAD_MB).")
        chunks.append(chunk)
    return b"".join(chunks)


def _root_counts(text: Optional[str]) -> Dict[str, int]:
    if not text or not text.strip():
        return {}
    try:
        counts = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Root counts must be JSON: {\"ENTITY\": 12345}") from exc
    if not isinstance(counts, dict) or not all(isinstance(v, int) and not isinstance(v, bool) for v in counts.values()):
        raise HTTPException(status_code=400, detail="Root counts must map each GS1 entity name to a number of roots")
    return counts


def _check_view(check: Dict[str, Any]) -> Dict[str, Any]:
    """What the page shows of Core Hub's checks of one draft."""
    view: Dict[str, Any] = {}
    if check.get("error"):
        view["error"] = check["error"]
    description = check.get("describe")
    if description:
        described = bool(description.get("levels"))
        view["describe"] = {
            # A describe refused for its structure never reached the agent: it has no verdict to show.
            "verdict": (description.get("verdict") or {}).get("mode") if described else "REFUSED",
            "proposedKeys": {level.get("level"): level.get("proposedKey") for level in description.get("levels") or []},
            "issues": description.get("issues") or [],
        }
    validation = check.get("validate")
    if validation is not None:
        view["validate"] = {"valid": bool(validation.get("valid")), "issues": gs1_import.validation_issues(validation)}
    return view


def _plan_view(plan_id: str, plan: Dict[str, Any]) -> Dict[str, Any]:
    report = plan["report"]
    return {
        "planId": plan_id,
        "pipelineId": plan["pipelineId"],
        "structureChecked": plan["structureChecked"],
        "perEntity": report["perEntity"],
        "strippedKeys": report["strippedKeys"],
        "drafts": list(plan["drafts"]),
        # the drafts the pipeline already holds: saving one updates that entity, so the page never preselects it
        "existing": list(plan.get("existing") or {}),
        "existingError": plan.get("existingError"),
        "checks": {name: _check_view(check) for name, check in plan["checks"].items()},
        "summary": gs1_import.summarize(plan),
    }


def _plan(config: Dict[str, Any], pipeline_id: str, repository: str, source_agent_id: Optional[str],
          target_agent_id: Optional[str], root_counts: Dict[str, int], check: bool) -> Dict[str, Any]:
    corehub.configure_core_hub(state.base_url, use_ssl=state.use_ssl, skip_verify=state.skip_verify)
    if not (source_agent_id and target_agent_id):
        agents = gs1_import.pipeline_agents(state.token, pipeline_id)
        source_agent_id = source_agent_id or agents["source"]
        target_agent_id = target_agent_id or agents["target"]
    if not (source_agent_id and target_agent_id):
        raise gs1_import.Gs1ImportError("The pipeline has no SOURCE or TARGET agent")
    request = conversion_request(config, pipeline_id, source_agent_id, target_agent_id, root_counts, repository)
    plan = gs1_import.plan_import(state.token, request, check=check, log=lambda message: logger.info("GS1 import: %s", message))
    plan["pipelineId"] = pipeline_id
    return plan


async def _convert(
    file: UploadFile = File(...),
    pipeline_id: str = Form(..., alias="pipelineId"),
    repository: str = Form("SQL_TO_NOSQL"),
    source_agent_id: Optional[str] = Form(None, alias="sourceAgentId"),
    target_agent_id: Optional[str] = Form(None, alias="targetAgentId"),
    root_counts: Optional[str] = Form(None, alias="rootCounts"),
    check: bool = Form(True),
) -> Dict[str, Any]:
    """Converts a GS1 configuration and has Core Hub check every draft. Saves nothing."""
    _require_session()
    if repository not in REPOSITORIES:
        raise HTTPException(status_code=400, detail=f"The repository is one of {', '.join(REPOSITORIES)}")
    try:
        config = load_config((await _read_capped(file)).decode("utf-8"))
    except NestedTooDeeply as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise HTTPException(status_code=400, detail="The file is not a GS1 configuration (JSON)") from exc
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="The file is not a GS1 configuration (a JSON object)")
    counts = _root_counts(root_counts)
    try:
        plan = await run_in_threadpool(
            _plan, config, pipeline_id, repository, source_agent_id or None, target_agent_id or None, counts, check)
    except gs1_import.Gs1ImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.warning("GS1 import: Core Hub failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _plan_view(_keep(plan), plan)


async def _save(request: Gs1SaveRequest) -> Dict[str, Any]:
    """Saves the chosen drafts of a converted plan, only when the operator confirmed."""
    _require_session()
    if not request.confirmed:
        raise HTTPException(status_code=400, detail="Nothing saved: confirm the save first")
    plan = _kept(request.plan_id)
    corehub.configure_core_hub(state.base_url, use_ssl=state.use_ssl, skip_verify=state.skip_verify)
    try:
        saved = await run_in_threadpool(
            gs1_import.save_drafts, state.token, plan["pipelineId"], plan, request.entities, request.confirmed)
    except gs1_import.Gs1ImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.warning("GS1 import: saving failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    updated = gs1_import.updated_names(plan, saved)
    note = f" (already imported, updated: {', '.join(updated)})" if updated else ""
    return {"saved": saved, "updated": updated,
            "message": f"Saved {len(saved)} entities to pipeline {plan['pipelineId']}{note}"}


def register_gs1_routes(app: FastAPI) -> None:
    app.add_api_route("/api/gs1/convert", _convert, methods=["POST"], responses=_RESPONSES)
    app.add_api_route("/api/gs1/save", _save, methods=["POST"], responses=_RESPONSES)
