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

"""Import a GS1 (Gluesync 1.x) configuration into a Gluesync 2 pipeline as entities.

1. The GS1 JSON configuration is converted (``gs1_migration``): one draft per GS1 entity Gluesync 2 can
   reproduce, and a report of what became of every GS1 entity. Secret-looking keys are removed unread.
2. Core Hub checks each draft: a Query draft is described against the source agent (its structure checks run
   first and refuse a statement that depends on the moment it runs or a key that is not a plain column; then
   the agent types the columns and proposes the keys GS1 took from the FROM table), its target is mapped onto the
   described result view, and every draft is validated as an entity. A document-split draft is validated only.
3. The drafts the operator chooses are saved with ``PUT /pipelines/{id}/config/entities``, and only once the
   operator confirms. Nothing is saved before.

Command line (the token is read from GS1_IMPORT_TOKEN, else a login as admin with DEFAULT_PASSWORD):

    python gs1_import.py convert --config gs1.json --pipeline ID [--offline] [--output report.json]
    python gs1_import.py import --config gs1.json --pipeline ID [--entities A,B] [--yes]

Both take --source-agent and --target-agent (default: the pipeline's SOURCE and TARGET agents),
--repository SQL_TO_NOSQL | NOSQL_TO_SQL | NOSQL_TO_SQL_SYBASE (default SQL_TO_NOSQL) and --root-counts, a JSON
file mapping each GS1 entity name to its number of root rows (the delete window is 1 % of it, at least 100).
Connection: CORE_HUB_URL, SSL_ENABLED, SSL_SKIP_VERIFY as for the Bootstrapper.
"""

import argparse
import copy
import json
import os
import sys
from typing import Any, Callable, Dict, Iterable, List, Optional

from commons import fetch_core_hub
from gs1_migration import REPOSITORIES, ConversionError, conversion_request, convert, load_config
from gs1_migration.json_values import utf16_sort_key
from gs1_migration import model as gs2

TOKEN_ENV = "GS1_IMPORT_TOKEN"
SAVE_PATH = "/pipelines/{pipeline_id}/config/entities"
VALIDATE_PATH = "/pipelines/{pipeline_id}/config/entities/validate"
DESCRIBE_PATH = "/pipelines/{pipeline_id}/agents/{agent_id}/discovery/data-model"
NODE_INFO_PATH = "/pipelines/{pipeline_id}/agents/{agent_id}/discovery/node-info"


class Gs1ImportError(RuntimeError):
    """The import cannot go on; the message is for the operator and carries no configuration value."""


# ---------- Core Hub ----------

def describe_model(token: str, pipeline_id: str, agent_id: str, model: Dict[str, Any]) -> Dict[str, Any]:
    """Core Hub's describe of a relational model on the source agent (structure checks first, then the agent)."""
    path = DESCRIBE_PATH.format(pipeline_id=pipeline_id, agent_id=agent_id)
    return _dict(fetch_core_hub(path, method="POST", token=token, body={"model": model}), "describe")


def validate_entities(token: str, pipeline_id: str, entities: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Core Hub's check of entity payloads without writing them: ``{valid, issues}``."""
    path = VALIDATE_PATH.format(pipeline_id=pipeline_id)
    return _dict(fetch_core_hub(path, method="POST", token=token, body={"entities": entities}), "validate")


def save_entities(token: str, pipeline_id: str, entities: List[Dict[str, Any]]) -> Any:
    return fetch_core_hub(SAVE_PATH.format(pipeline_id=pipeline_id), method="PUT", token=token, body={"entities": entities})


def data_types(token: str, pipeline_id: str, agent_id: str) -> List[Dict[str, Any]]:
    """The data types matrix an agent declares (its node info)."""
    answer = fetch_core_hub(NODE_INFO_PATH.format(pipeline_id=pipeline_id, agent_id=agent_id), token=token)
    matrix = answer.get("dataTypesMatrix") if isinstance(answer, dict) else None
    return [entry for entry in matrix if isinstance(entry, dict)] if isinstance(matrix, list) else []


def pipeline_agents(token: str, pipeline_id: str) -> Dict[str, Optional[str]]:
    """The ids of the pipeline's SOURCE and TARGET agents."""
    config = fetch_core_hub(f"/pipelines/{pipeline_id}/config", token=token)
    agents = config.get("agents") if isinstance(config, dict) else None
    found: Dict[str, Optional[str]] = {"SOURCE": None, "TARGET": None}
    for agent in agents if isinstance(agents, list) else []:
        if isinstance(agent, dict) and agent.get("agentType") in found and found[agent["agentType"]] is None:
            found[agent["agentType"]] = agent.get("agentId") or agent.get("id")
    return {"source": found["SOURCE"], "target": found["TARGET"]}


def _dict(answer: Any, what: str) -> Dict[str, Any]:
    if not isinstance(answer, dict):
        raise Gs1ImportError(f"Unexpected {what} answer from Core Hub")
    return answer


def validation_issues(answer: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Core Hub's validation issues in the shape of the converter's issues."""
    issues = []
    for raw in answer.get("issues") or []:
        if not isinstance(raw, dict):
            continue
        out = {"code": raw.get("code") or "VALIDATION", "severity": raw.get("severity") or "ERROR"}
        for key in ("level", "location"):
            if raw.get(key) is not None:
                out[key] = raw[key]
        out["message"] = raw.get("message") or ""
        if raw.get("hint") is not None:
            out["hint"] = raw["hint"]
        issues.append(out)
    return issues


def _refused_before_the_agent(description: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The structure refusals of a describe: ERRORs answered before the agent was asked (no level described)."""
    if description.get("levels"):
        return []
    return [issue for issue in description.get("issues") or [] if isinstance(issue, dict) and issue.get("severity") == "ERROR"]


# ---------- the flow ----------

def plan_import(token: Optional[str], request: Dict[str, Any], check: bool = True,
                log: Callable[[str], None] = lambda _message: None) -> Dict[str, Any]:
    """Converts the configuration and, with ``check``, has Core Hub describe and validate every draft.

    The answer holds the converter's ``report``, the ``drafts`` ready to save by entity name (a Query draft whose
    root key GS1 took from the FROM table gets the key describe proposes), Core Hub's ``checks`` per entity and
    whether the structure was checked. Nothing is written to Core Hub.
    """
    checks: Dict[str, Dict[str, Any]] = {}
    pipeline_id = request["pipelineId"]

    def structure_check(subject: Dict[str, Any]) -> List[Dict[str, Any]]:
        name = subject["entity"]
        entry = checks.setdefault(name, {})
        try:
            if subject["kind"] == "sql":
                log(f"Describing {name} on agent {request['sourceAgentId']}")
                description = describe_model(token, pipeline_id, request["sourceAgentId"], subject["model"])
                entry["describe"] = description
                return _refused_before_the_agent(description)
            log(f"Validating {name}")
            answer = validate_entities(token, pipeline_id, [subject["draft"]])
            entry["validate"] = answer
            return validation_issues(answer)
        except (RuntimeError, ValueError) as exc:
            entry["error"] = str(exc)
            log(f"Core Hub could not check {name}: {exc}")
            return []

    try:
        report = convert(request, structure_check if check else None)
    except ConversionError as exc:
        raise Gs1ImportError(str(exc)) from None
    types: Dict[str, List[Dict[str, Any]]] = {}

    def type_matrices():
        if not types:
            types["source"] = data_types(token, pipeline_id, request["sourceAgentId"])
            types["target"] = data_types(token, pipeline_id, request["targetAgentId"])
        return types["source"], types["target"]

    drafts = {}
    for entity in report["entities"]:
        name = entity["entityName"]
        try:
            draft = _described_draft(entity, checks.get(name, {}).get("describe"), type_matrices)
        except (RuntimeError, ValueError) as exc:
            checks.setdefault(name, {})["error"] = str(exc)
            draft = copy.deepcopy(entity)
        drafts[name] = draft
        is_query = any(agent_entity.get("type") == "Query" for agent_entity in draft["agentEntities"])
        if check and is_query and "error" not in checks.get(name, {}):
            try:
                log(f"Validating {name}")
                checks[name]["validate"] = validate_entities(token, pipeline_id, [draft])
            except (RuntimeError, ValueError) as exc:
                checks[name]["error"] = str(exc)
    return {"report": report, "drafts": drafts, "checks": checks, "structureChecked": check}


def _described_draft(entity: Dict[str, Any], description: Optional[Dict[str, Any]],
                     type_matrices: Callable[[], Any]) -> Dict[str, Any]:
    """The draft completed from describe, as Core Hub's own data-model entity draft completes it.

    The model is the one describe normalised (typed labels, proposed keys) with the converter's options. The
    target maps the result view of the root (its labels, then one array per child level) onto the collection, each
    column in the target's own type; a label the converter writes as a static field (``type``, ``scope``) keeps
    the static value. A Query without ``entitiesKeyMapping`` had GS1 ids from the FROM table's primary key: the id
    keys are the root key describe proposes, sorted by label as GS1 laid them out.
    """
    draft = copy.deepcopy(entity)
    if not description or not description.get("levels") or not isinstance(description.get("model"), dict):
        return draft
    query = next((a for a in draft["agentEntities"] if a.get("type") == "Query"), None)
    target = next((a for a in draft["agentEntities"] if a.get("type") == "NoSqlEntity"), None)
    if query is None or target is None:
        return draft
    query["model"] = dict(copy.deepcopy(description["model"]), options=query["model"]["options"])
    root = query["model"].get("root") or {}
    root_key = root.get("key") or []
    keys = target.get("keyMapping") or {}
    if keys.get("valueRendering") == "GS1" and not keys.get("keys") and root_key:
        keys["keys"] = [gs2.column_id(label) for label in sorted(root_key, key=utf16_sort_key)]
    _map_result_view(target, root, *type_matrices())
    return draft


def _entry_for(data_type: str, matrix: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    wanted = str(data_type).lower()
    for entry in matrix:
        names = [entry.get("defaultType"), entry.get("gluesyncDataType"), *(entry.get("supportedTypes") or [])]
        if wanted in (str(name).lower() for name in names if name):
            return entry
    return None


def _target_type(data_type: str, source_matrix: List[Dict[str, Any]], target_matrix: List[Dict[str, Any]]) -> str:
    """The target's own type for a source type, matched by Gluesync type; the source type when none matches."""
    source_entry = _entry_for(data_type, source_matrix)
    target_entry = next((e for e in target_matrix
                         if source_entry and e.get("gluesyncDataType") == source_entry.get("gluesyncDataType")), None)
    if target_entry is None:
        return data_type
    same = [t for t in target_entry.get("supportedTypes") or [] if str(t).lower() == str(data_type).lower()]
    return same[0] if same else target_entry.get("defaultType") or data_type


def _map_result_view(target: Dict[str, Any], root: Dict[str, Any], source_matrix: List[Dict[str, Any]],
                     target_matrix: List[Dict[str, Any]]) -> None:
    object_id = target["entityObject"]["id"]
    view_table = gs2.level_table_id(root.get("name", ""))
    array_type = next((e.get("defaultType") for e in target_matrix if e.get("gluesyncDataType") == "ARRAY"), "gs:array")
    static_ids = {column["id"] for column in target["columns"]}
    view = [dict(column, isPK=column.get("name") in (root.get("key") or [])) for column in root.get("columns") or []]
    view += [gs2.container_column(gs2.id64(child["path"]), view_table, child["path"], "gs:array")
             for child in root.get("children") or []]
    mapped = [column for column in view if column["id"] not in static_ids]
    target["columns"] = [
        dict(column, tableId=object_id,
             dataType=array_type if column["dataType"] == "gs:array" else _target_type(column["dataType"], source_matrix,
                                                                                        target_matrix))
        for column in mapped
    ] + target["columns"]
    target["entityType"]["columnsMappingMatrix"] = [
        gs2.mapping_matrix_entry(view_table, object_id, column["id"], column["id"]) for column in mapped
    ]


def save_drafts(token: str, pipeline_id: str, plan: Dict[str, Any], names: Iterable[str], confirmed: bool) -> List[str]:
    """Saves the chosen drafts of a plan; refuses unless the operator confirmed. Returns the names saved."""
    if not confirmed:
        raise Gs1ImportError("Nothing saved: the operator did not confirm")
    chosen = list(dict.fromkeys(names))
    unknown = [name for name in chosen if name not in plan["drafts"]]
    if unknown:
        raise Gs1ImportError(f"No draft for {', '.join(unknown)}: only converted entities can be saved")
    if not chosen:
        raise Gs1ImportError("Nothing to save: no entity chosen")
    save_entities(token, pipeline_id, [plan["drafts"][name] for name in chosen])
    return chosen


# ---------- what the operator reads ----------

def _issue_line(issue: Dict[str, Any]) -> str:
    where = "/".join(str(issue[key]) for key in ("level", "location") if issue.get(key))
    return f"{issue.get('severity')} {issue.get('code')}{f' [{where}]' if where else ''}: {issue.get('message')}"


def summarize(plan: Dict[str, Any]) -> List[str]:
    """The report as lines: per GS1 entity its bucket, issues and Core Hub's checks, then the keys stripped."""
    report = plan["report"]
    lines = []
    if not plan.get("structureChecked"):
        lines.append("Core Hub did not check the drafts (offline): structure errors show up only when you save them.")
    for entry in report["perEntity"]:
        name = entry["gs1Name"]
        drafted = " (draft)" if name in plan["drafts"] else ""
        lines.append(f"{name}: {entry['bucket']}{drafted}")
        lines.extend(f"  {_issue_line(issue)}" for issue in entry["issues"])
        lines.extend(f"  {line}" for line in _check_lines(plan["checks"].get(name, {}), plan["drafts"].get(name)))
    stripped = report.get("strippedKeys") or []
    lines.append(f"Secret keys removed unread: {', '.join(stripped) if stripped else 'none'}")
    return lines


def _check_lines(check: Dict[str, Any], draft: Optional[Dict[str, Any]]) -> List[str]:
    lines = []
    if check.get("error"):
        lines.append(f"Core Hub could not check it: {check['error']}")
    description = check.get("describe")
    if description and description.get("levels"):
        mode = (description.get("verdict") or {}).get("mode")
        keys = ", ".join(f"{level.get('level')} {level.get('proposedKey')}" for level in description["levels"])
        lines.append(f"describe: {mode}; proposed keys {keys}")
    validation = check.get("validate")
    if validation is not None and draft is not None:
        issues = validation_issues(validation)
        lines.append("validate: " + ("valid" if validation.get("valid") else "refused"))
        lines.extend(f"  {_issue_line(issue)}" for issue in issues)
    return lines


# ---------- command line ----------

def _admin_login() -> str:
    response = fetch_core_hub(
        "/authentication/login",
        method="POST",
        body={"username": "admin", "password": os.getenv("DEFAULT_PASSWORD", "")},
    )
    token = response.get("apiToken") if isinstance(response, dict) else None
    if not token:
        raise SystemExit(f"Cannot log in to Core Hub: set {TOKEN_ENV} or DEFAULT_PASSWORD")
    return token


def read_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        config = load_config(f.read())
    if not isinstance(config, dict):
        raise Gs1ImportError("The GS1 configuration is not a JSON object")
    return config


def _root_counts(path: Optional[str]) -> Dict[str, int]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        counts = json.load(f)
    if not isinstance(counts, dict) or not all(isinstance(v, int) and not isinstance(v, bool) for v in counts.values()):
        raise Gs1ImportError("--root-counts must be a JSON object of entity name to number of roots")
    return counts


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Import a GS1 configuration into a Gluesync 2 pipeline")
    parser.add_argument("--token", help=f"Core Hub token (default ${TOKEN_ENV}, else admin login)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("convert", "Convert and check the drafts; save nothing"),
                            ("import", "Convert, check, and save the chosen drafts once confirmed")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", required=True, help="GS1 configuration (JSON)")
        command.add_argument("--pipeline", required=True, help="Gluesync 2 pipeline id")
        command.add_argument("--source-agent", help="Source agent id (default: the pipeline's SOURCE agent)")
        command.add_argument("--target-agent", help="Target agent id (default: the pipeline's TARGET agent)")
        command.add_argument("--repository", choices=REPOSITORIES, default="SQL_TO_NOSQL",
                             help="The GS1 product the configuration comes from (default SQL_TO_NOSQL)")
        command.add_argument("--root-counts", help="JSON file: GS1 entity name -> number of roots")
        command.add_argument("--output", help="Write the report, the drafts and Core Hub's checks to this JSON file")
    commands.choices["convert"].add_argument("--offline", action="store_true",
                                             help="Do not call Core Hub: no structure check, no describe, no validate")
    commands.choices["import"].add_argument("--entities", help="Comma-separated GS1 entity names (default: every draft)")
    commands.choices["import"].add_argument("--yes", action="store_true", help="Confirm the save without asking")
    args = parser.parse_args(argv)

    try:
        config = read_config(args.config)
        offline = getattr(args, "offline", False)
        token = None if offline else (args.token or os.getenv(TOKEN_ENV) or _admin_login())
        source, target = args.source_agent, args.target_agent
        if not offline and not (source and target):
            agents = pipeline_agents(token, args.pipeline)
            source, target = source or agents["source"], target or agents["target"]
        if not (source and target):
            raise Gs1ImportError("Name --source-agent and --target-agent (the pipeline has no SOURCE or TARGET agent)")
        request = conversion_request(config, args.pipeline, source, target, _root_counts(args.root_counts), args.repository)
        plan = plan_import(token, request, check=not offline, log=lambda message: print(message, file=sys.stderr))
        print("\n".join(summarize(plan)))
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump({k: plan[k] for k in ("report", "drafts", "checks")}, f, indent=2, ensure_ascii=False)
        if args.command == "convert":
            return 0
        names = [n.strip() for n in args.entities.split(",") if n.strip()] if args.entities else list(plan["drafts"])
        if not names:
            print("No entity to save")
            return 0
        confirmed = args.yes or _confirm(f"Save {len(names)} entities ({', '.join(names)}) to pipeline {args.pipeline}?")
        saved = save_drafts(token, args.pipeline, plan, names, confirmed)
        print(f"Saved {len(saved)} entities: {', '.join(saved)}")
        return 0
    except (Gs1ImportError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"ERROR: Core Hub refused the request: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
