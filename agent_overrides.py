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

"""Agent connection overrides applied to a backup's agents-config.yaml at import time.

A backup carries the connection of every agent (host, port, database, username, password) as it was on the
Core Hub it came from. Core Hub 2.3.1 validates the connection when the credentials are saved, so a backup
can only be imported where those databases are reachable. An override points an agent somewhere else (for
example a local MSSQL container) before its credentials are sent.

The mapping is keyed by the agent's tag (``agentTag``, e.g. ``mssql-ct``; case-insensitive) or by its id in the
backup (``agentId``, e.g. ``a9e00004``)::

    {"mssql-ct": {"host": "host.docker.internal", "port": 1433, "database": "demo", "username": "sa"}}

Sources, later ones win field by field: the JSON file named by ``GS_IMPORT_AGENT_OVERRIDES_FILE``, the inline
JSON of ``GS_IMPORT_AGENT_OVERRIDES``, then what the caller passes (the Automator form field / CLI flag).

Passwords are never read from the mapping (a ``password`` key is refused): an agent's password comes from the
environment variable ``GS_IMPORT_AGENT_PASSWORD_<TAG>`` (tag upper-cased, anything not A-Z/0-9 turned into
``_``: ``mssql-ct`` -> ``GS_IMPORT_AGENT_PASSWORD_MSSQL_CT``), or from the variable an entry names under
``passwordEnv``. Nothing here ever logs or returns a password.
"""

import json
import os
import re
from typing import Any, Dict, List, Mapping, Optional

ENV_INLINE = "GS_IMPORT_AGENT_OVERRIDES"
ENV_FILE = "GS_IMPORT_AGENT_OVERRIDES_FILE"
PASSWORD_ENV_PREFIX = "GS_IMPORT_AGENT_PASSWORD_"

# override field -> hostCredentials key of agents-config.yaml
FIELDS = {"host": "host", "port": "port", "database": "databaseName", "databaseName": "databaseName",
          "username": "username"}
_PASSWORD_KEYS = ("password", "pwd", "pass")


class AgentOverrideError(ValueError):
    """An override mapping that cannot be applied (bad JSON, unknown field, unknown agent, a password in it)."""


def password_env_name(agent_tag: Any) -> str:
    """``GS_IMPORT_AGENT_PASSWORD_<TAG>`` of an agent tag. AgentOverrideError without a tag (there is no
    ``..._NONE`` variable): such an agent takes its password only from a ``passwordEnv`` it names."""
    if agent_tag is None or not str(agent_tag).strip():
        raise AgentOverrideError("an agent without agentTag has no GS_IMPORT_AGENT_PASSWORD_<TAG> variable; "
                                 "name one under 'passwordEnv' (keyed by its agentId)")
    return PASSWORD_ENV_PREFIX + re.sub(r"[^A-Z0-9]", "_", str(agent_tag).upper())


def _default_password_var(agent: Dict[str, Any], entry: Dict[str, Any]) -> Optional[str]:
    """The variable the password of ``agent`` is read from: its ``passwordEnv``, else the tag's
    ``GS_IMPORT_AGENT_PASSWORD_<TAG>``; None for an agent with neither."""
    if entry.get("passwordEnv"):
        return str(entry["passwordEnv"])
    tag = agent.get("agentTag")
    return password_env_name(tag) if tag is not None and str(tag).strip() else None


def _agent_label(agent: Dict[str, Any]) -> str:
    return f"{agent.get('agentType')} agent {agent.get('agentTag')} (id {agent.get('agentId') or agent.get('id')})"


def check_password_vars(agents: List[Any], overrides: Dict[str, Dict[str, Any]], env: Mapping[str, str]) -> None:
    """AgentOverrideError when two different agents would read their password from the same variable that is
    set: tags differing only by case or separators (``mssql-ct`` / ``mssql_ct`` / ``MSSQL.CT``), a SOURCE and a
    TARGET with one tag, or one ``passwordEnv`` named twice. One agent could otherwise get another's password."""
    by_var: Dict[str, List[str]] = {}
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        _, entry = _entry_for(agent, overrides)
        var = _default_password_var(agent, entry)
        if var and env.get(var):
            by_var.setdefault(var, []).append(_agent_label(agent))
    clashes = {var: labels for var, labels in by_var.items() if len(labels) > 1}
    if clashes:
        var, labels = sorted(clashes.items())[0]
        raise AgentOverrideError(f"${var} would give the same password to {', '.join(labels)}; give each agent "
                                 "its own variable with 'passwordEnv' (keyed by agentId)")


def _parse(text: str, origin: str) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentOverrideError(f"{origin}: not valid JSON ({exc.msg} at line {exc.lineno})") from None
    if not isinstance(data, dict):
        raise AgentOverrideError(f"{origin}: expected a JSON object keyed by agent tag or agent id")
    return {str(key): _parsed_entry(key, entry, origin) for key, entry in data.items()}


def _check_entry_field(key: Any, field: str, origin: str) -> None:
    if field.lower() in _PASSWORD_KEYS:
        raise AgentOverrideError(
            f"{origin}: {key!r} has a {field!r} key; passwords are never read from the mapping, set the "
            f"environment variable {password_env_name(key)} (or name one under 'passwordEnv')")
    if field not in FIELDS and field != "passwordEnv":
        raise AgentOverrideError(f"{origin}: {key!r} has unknown field {field!r} (allowed: host, port, "
                                 f"database, username, passwordEnv)")


def _parsed_entry(key: Any, entry: Any, origin: str) -> Dict[str, Any]:
    """The checked override of agent ``key``, its port as a number."""
    if not isinstance(entry, dict):
        raise AgentOverrideError(f"{origin}: the override of {key!r} must be an object")
    for field in entry:
        _check_entry_field(key, field, origin)
    if "port" in entry:
        try:
            entry = {**entry, "port": int(entry["port"])}
        except (TypeError, ValueError):
            raise AgentOverrideError(f"{origin}: port of {key!r} must be a number") from None
    return entry


def load(text: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> Dict[str, Dict[str, Any]]:
    """The merged override mapping: ``GS_IMPORT_AGENT_OVERRIDES_FILE``, then ``GS_IMPORT_AGENT_OVERRIDES``, then
    ``text`` (each a JSON object). Empty when none is set."""
    env = os.environ if env is None else env
    merged: Dict[str, Dict[str, Any]] = {}
    sources = []
    path = (env.get(ENV_FILE) or "").strip()
    if path:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as handle:
                sources.append((handle.read(), f"{ENV_FILE}={path}"))
        except OSError as exc:
            raise AgentOverrideError(f"{ENV_FILE}: cannot read {path}: {exc.strerror}") from None
    if (env.get(ENV_INLINE) or "").strip():
        sources.append((env[ENV_INLINE], ENV_INLINE))
    if text and text.strip():
        sources.append((text, "agent overrides"))
    for source_text, origin in sources:
        for key, entry in _parse(source_text, origin).items():
            merged.setdefault(key, {}).update(entry)
    return merged


def _entry_for(agent: Dict[str, Any], overrides: Dict[str, Dict[str, Any]]) -> tuple[List[str], Dict[str, Any]]:
    tag = str(agent.get("agentTag") or "")
    agent_id = str(agent.get("agentId") or agent.get("id") or "")
    keys, entry = [], {}
    # by tag first, then by id: an id entry wins on the fields both set
    for key, value in overrides.items():
        if (tag and key.casefold() == tag.casefold()) or (agent_id and key == agent_id):
            keys.append(key)
    keys.sort(key=lambda k: k == agent_id)
    for key in keys:
        entry.update(overrides[key])
    return keys, entry


def _agent_refs(agents: List[Any]) -> List[str]:
    """Every agentTag and agentId of the agents, sorted."""
    return sorted({str(a.get(k)) for a in agents if isinstance(a, dict) for k in ("agentTag", "agentId")
                   if a.get(k)})


def _overridden_credentials(creds: Dict[str, Any], entry: Dict[str, Any], changed: List[str]) -> None:
    """Set the overridden fields of ``entry`` on ``creds``, noting the changed ones."""
    for field, value in entry.items():
        if field == "passwordEnv":
            continue
        target = FIELDS[field]
        if creds.get(target) != value:
            changed.append(f"{target}={value}")
        creds[target] = value


def _apply_to_agent(agent: Dict[str, Any], entry: Dict[str, Any], env: Mapping[str, str]) -> Optional[str]:
    """Apply one agent's override and password, in place; the change line (no password), or None."""
    label = _agent_label(agent)
    creds = dict(agent.get("hostCredentials") or {})
    changed: List[str] = []
    _overridden_credentials(creds, entry, changed)
    password_var = _default_password_var(agent, entry)
    if password_var and env.get(password_var):
        creds["password"] = env[password_var]
        changed.append(f"password from ${password_var}")
    elif entry.get("passwordEnv"):
        raise AgentOverrideError(f"{label}: passwordEnv names {password_var}, which is not set")
    if not changed:
        return None
    agent["hostCredentials"] = creds
    return f"Agent override: {label}: {', '.join(changed)}"


def apply(agents: List[Any], overrides: Dict[str, Dict[str, Any]],
          env: Optional[Mapping[str, str]] = None) -> List[str]:
    """Apply the overrides (and the ``GS_IMPORT_AGENT_PASSWORD_*`` passwords) to the agents of agents-config.yaml,
    in place. Returns what was changed, without any password. An override naming no agent of the backup is an
    error, so that a typo does not silently leave the backup's (unreachable) host in place."""
    env = os.environ if env is None else env
    check_password_vars(agents, overrides, env)
    used = set()
    lines = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        keys, entry = _entry_for(agent, overrides)
        used.update(keys)
        line = _apply_to_agent(agent, entry, env)
        if line:
            lines.append(line)
    unknown = sorted(set(overrides) - used)
    if unknown:
        known = _agent_refs(agents)
        raise AgentOverrideError(f"agent overrides name no agent of the backup: {', '.join(unknown)} "
                                 f"(agents in the backup: {', '.join(known)})")
    return lines
