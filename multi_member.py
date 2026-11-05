# This program is part of Gluesync.
#
# IBM i multi-member YAML keys (``TABLE@@2``) require the source agent's
# specificConfiguration.enableMultiMember. Member-key existence checks wait on
# exact Portal field names and are not invented here.

"""IBM i multi-member (YAML ``@@`` keys) vs source agent ``enableMultiMember``."""

from __future__ import annotations

from typing import Any, Iterable, List, Mapping, Optional, Sequence

# Agent tags that own enableMultiMember (Portal / agents.json ibm-iseries).
_IBM_ISERIES_TAG_MARKERS = ("ibm-iseries", "ibm_iseries", "as400")


def yaml_multi_member_keys(custom_tables: Any = None, whitelist: Any = None) -> List[str]:
    """YAML custom / whitelist table keys that contain the ``@@`` multi-member marker."""
    found: List[str] = []
    seen = set()
    for key in (custom_tables or {}):
        text = str(key)
        if "@@" in text and text.casefold() not in seen:
            seen.add(text.casefold())
            found.append(text)
    for entry in whitelist or []:
        if not isinstance(entry, str):
            continue
        if "@@" in entry and entry.casefold() not in seen:
            seen.add(entry.casefold())
            found.append(entry)
    return found


def is_ibm_iseries_agent(agent_tag: Any) -> bool:
    """True when ``agent_tag`` is the IBM i / AS400 source agent family."""
    if not isinstance(agent_tag, str) or not agent_tag.strip():
        return False
    folded = agent_tag.strip().casefold().replace("_", "-")
    return any(marker.replace("_", "-") in folded for marker in _IBM_ISERIES_TAG_MARKERS)


def enable_multi_member(specific_configuration: Any) -> bool:
    """True when ``specificConfiguration.enableMultiMember`` is set truthy."""
    if not isinstance(specific_configuration, Mapping):
        return False
    value = specific_configuration.get("enableMultiMember")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().casefold() in ("1", "true", "yes", "on")
    return bool(value)


def missing_enable_multi_member_message(keys: Sequence[str]) -> str:
    """Clear refuse / Preview note when YAML uses ``@@`` without enableMultiMember."""
    sample = ", ".join(list(keys)[:5])
    more = f" (+{len(keys) - 5} more)" if len(keys) > 5 else ""
    return (
        f"YAML table key(s) {sample}{more} use IBM i multi-member naming (`@@`), "
        f"but the source agent's specificConfiguration.enableMultiMember is not true. "
        f"Enable `enableMultiMember` on the source agent (ibm-iseries), rediscover the tables, "
        f"then re-import."
    )


def _agents_as_list(agents: Any) -> List[Mapping[str, Any]]:
    if isinstance(agents, Mapping):
        values = list(agents.values())
        if values and all(isinstance(v, Mapping) for v in values):
            return values  # type: ignore[return-value]
        return []
    if isinstance(agents, (list, tuple)):
        return [a for a in agents if isinstance(a, Mapping)]
    return []


def find_source_agent(
    agents: Any,
    *,
    source_agent_id: Any = None,
) -> Optional[Mapping[str, Any]]:
    """The SOURCE agent from a pipeline agents list/dict, preferring ``source_agent_id``."""
    listed = _agents_as_list(agents)
    if source_agent_id is not None:
        wanted = str(source_agent_id)
        for agent in listed:
            aid = agent.get("agentId") or agent.get("id")
            if aid is not None and str(aid) == wanted:
                return agent
    for agent in listed:
        if str(agent.get("agentType") or "").upper() == "SOURCE":
            return agent
    return None


def multi_member_preflight_message(
    *,
    custom_tables: Any = None,
    whitelist: Any = None,
    source_agent_tag: Any = None,
    specific_configuration: Any = None,
    agents: Any = None,
    source_agent_id: Any = None,
) -> Optional[str]:
    """Refuse message when ibm-iseries YAML has ``@@`` keys and enableMultiMember is not true.

    Non-IBM agents (e.g. MSSQL ``CUSTOMERS@@2`` duplicate entities) are left alone.
    When ``agents`` is given, tag and specificConfiguration are taken from the SOURCE agent
    when the caller did not already supply them.
    """
    keys = yaml_multi_member_keys(custom_tables, whitelist)
    if not keys:
        return None
    tag = source_agent_tag
    specific = specific_configuration
    if agents is not None and (tag is None or specific is None):
        source = find_source_agent(agents, source_agent_id=source_agent_id)
        if source is not None:
            if tag is None:
                tag = source.get("agentTag")
            if specific is None:
                specific = source.get("specificConfiguration")
    if not is_ibm_iseries_agent(tag):
        return None
    if enable_multi_member(specific):
        return None
    return missing_enable_multi_member_message(keys)
