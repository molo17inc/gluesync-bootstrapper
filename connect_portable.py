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
# Copyright (C) 2026 MOLO17. All rights reserved.

"""Portable Gluesync Connect export and setup.

The bundle is organization, members, and site fields only. Secrets, enrollment
tokens, and environment URLs are never written into it. Export reads Connect.
Setup calls Connect. Core Hub configure is optional and uses this process's
GLUESYNC_CONNECT_URL, not a URL from the bundle.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

import yaml

BUNDLE_KIND = "gluesync-connect-portable"
ROLES = ("owner", "admin", "operator", "viewer")
ORG_STATUSES = ("active", "suspended")
ENROLLMENT_TTL_MINUTES = 60

_API_ORGANIZATIONS = "/api/organizations"

# Keys that must never appear in a portable bundle. Compared after stripping
# underscores and hyphens and lower-casing.
_FORBIDDEN_KEY_NORM = frozenset(
    {
        "password",
        "passwordhash",
        "passwordupdatedat",
        "enrollmenttoken",
        "bootstraptoken",
        "sessiontoken",
        "tokenhash",
        "token",
        "jwtsecret",
        "jwtsecretnext",
        "jdbcrelaytokenpepper",
        "jdbcrelayenabled",
        "mqtthubcert",
        "mqtthubkey",
        "mqttpassword",
        "mqttusername",
        "mqttbrokerurl",
        "couchbase",
        "couchbasebucket",
        "couchbasescope",
        "couchbaseconnectionstring",
        "oidc",
        "connectsettings",
        "idpsubject",
        "avatarurl",
        "disabled",
        "bootstrapadminpassword",
        "trustproxycidrs",
        "gatewayurl",
        "iotendpoint",
        "mcptoken",
        "gsjtoken",
        "connecturl",
        "corehuburl",
        "invitation",
        "invitations",
    }
)

_URL_VALUE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_GSJ_VALUE = re.compile(r"^gsj_")

_ORG_EXPORT_KEYS = (
    "id",
    "name",
    "slug",
    "billingEmail",
    "taxId",
    "address",
    "defaultTimezone",
    "settings",
    "status",
    "createdAt",
    "updatedAt",
)
_ADDRESS_KEYS = ("line1", "line2", "city", "region", "postalCode", "country")
_ADDRESS_REQUIRED = ("line1", "city", "postalCode", "country")


class ConnectError(RuntimeError):
    """Base error for Connect import and setup."""


class ConnectConflict(ConnectError):
    """A create returned a conflict. Callers must not retry it as an overwrite."""

    def __init__(self, method: str, path: str, status: int, detail: str):
        self.method = method
        self.path = path
        self.status = status
        super().__init__(
            f"{method} {path} returned {status}: {detail}. "
            "Refusing to retry the create as an overwrite."
        )


class ConnectHttpError(ConnectError):
    def __init__(self, method: str, path: str, status: int, detail: str):
        self.method = method
        self.path = path
        self.status = status
        super().__init__(f"{method} {path} returned {status}: {detail}")


Transport = Callable[[str, str, Dict[str, str], Optional[Mapping[str, Any]]], Tuple[int, Any]]


def _norm_key(key: str) -> str:
    return re.sub(r"[_-]", "", str(key)).lower()


def _is_url(value: str) -> bool:
    return bool(_URL_VALUE.match(value.strip()))


def slugify(value: str) -> str:
    """Match Connect organizationService slug generation."""
    slug = value.lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", slug)
    return slug.strip("-")


def _detail(body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = err.get("message") or err.get("code")
            if message:
                return str(message)
        if body.get("message"):
            return str(body["message"])
    text = body if isinstance(body, str) else json.dumps(body, default=str)
    return text[:500]


def _is_conflict(status: int, body: Any) -> bool:
    if status == 409:
        return True
    detail = _detail(body).lower()
    if isinstance(body, dict):
        err = body.get("error")
        code = ""
        if isinstance(err, dict):
            code = str(err.get("code") or "")
        elif isinstance(err, str):
            code = err
        if code == "conflict":
            return True
    if status == 400 and "already exists" in detail:
        return True
    return False


def _is_user_already_exists(status: int, body: Any) -> bool:
    return status == 400 and "already exists" in _detail(body).lower()


class ConnectClient:
    """JSON client. `transport` is for tests; production uses requests."""

    def __init__(
        self,
        base_url: str,
        *,
        bearer: Optional[str] = None,
        cookie_token: Optional[str] = None,
        transport: Optional[Transport] = None,
        timeout: int = 60,
    ):
        self.base_url = base_url.rstrip("/")
        self.bearer = bearer
        self.cookie_token = cookie_token
        self.transport = transport
        self.timeout = timeout

    def request(self, method: str, path: str, body: Optional[Mapping[str, Any]] = None) -> Any:
        url = self.base_url + path
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.bearer}"
        if self.cookie_token:
            headers["Cookie"] = f"gs-auth={self.cookie_token}"
        status, parsed = self._send(method.upper(), url, headers, body)
        if _is_conflict(status, parsed):
            raise ConnectConflict(method.upper(), path, status, _detail(parsed))
        if status >= 400:
            raise ConnectHttpError(method.upper(), path, status, _detail(parsed))
        return parsed

    def _send(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[Mapping[str, Any]],
    ) -> Tuple[int, Any]:
        if self.transport is not None:
            return self.transport(method, url, headers, body)
        import requests

        response = requests.request(
            method,
            url,
            headers=headers,
            json=body if body is not None else None,
            timeout=self.timeout,
        )
        try:
            parsed: Any = response.json()
        except ValueError:
            parsed = response.text
        return response.status_code, parsed


def _as_list(payload: Any, keys: Sequence[str]) -> List[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise ConnectError(f"Expected a list, got {type(payload).__name__}")


def _reject_secrets(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            norm = _norm_key(key)
            if norm in _FORBIDDEN_KEY_NORM or norm.endswith("url") or norm.endswith("uri"):
                raise ConnectError(f"Portable bundle rejects {path}.{key}")
            _reject_secrets(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secrets(item, f"{path}[{index}]")
        return
    if isinstance(value, str):
        if _GSJ_VALUE.match(value.strip()):
            raise ConnectError(f"Portable bundle rejects a gsj_ token at {path}")
        if _is_url(value):
            raise ConnectError(
                f"Portable bundle rejects a URL at {path}. "
                "Pass CONNECT_API_URL, GLUESYNC_CONNECT_URL, and COREHUB_URL at runtime."
            )


def _copy_address(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConnectError("organization.address must be an object or null")
    address = {}
    for key in _ADDRESS_KEYS:
        if key in raw and raw[key] is not None:
            address[key] = raw[key]
    return address or None


def _validate_address(address: Optional[Mapping[str, Any]]) -> None:
    if address is None:
        return
    missing = [key for key in _ADDRESS_REQUIRED if not address.get(key)]
    if missing:
        raise ConnectError("organization.address is missing " + ", ".join(missing))


def _member_rows(data: Mapping[str, Any]) -> List[Any]:
    if "members" in data:
        raw_members = data["members"]
        if not isinstance(raw_members, list) or not raw_members:
            raise ConnectError("members must be a non-empty list")
        return raw_members
    if "user" in data and "membership" in data:
        user = data["user"] if isinstance(data["user"], dict) else {}
        membership = data["membership"] if isinstance(data["membership"], dict) else {}
        return [{"role": membership.get("role"), "user": user}]
    raise ConnectError("Bundle needs members, or user plus membership")


def _clean_member(member: Any, index: int) -> Dict[str, Any]:
    if not isinstance(member, dict):
        raise ConnectError(f"members[{index}] must be an object")
    role = member.get("role")
    if role not in ROLES:
        raise ConnectError(f"members[{index}].role must be one of {', '.join(ROLES)}")
    user = member.get("user") if isinstance(member.get("user"), dict) else {}
    email = user.get("email")
    if not isinstance(email, str) or not email.strip():
        raise ConnectError(f"members[{index}].user.email is required")
    cleaned_user: Dict[str, Any] = {"email": email.strip()}
    full_name = user.get("fullName")
    if isinstance(full_name, str) and full_name.strip():
        cleaned_user["fullName"] = full_name.strip()
    return {"role": role, "user": cleaned_user}


def _members_from_document(data: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [_clean_member(member, index) for index, member in enumerate(_member_rows(data))]


def _site_rows(data: Mapping[str, Any]) -> List[Any]:
    if "sites" in data:
        raw_sites = data["sites"]
        if not isinstance(raw_sites, list) or not raw_sites:
            raise ConnectError("sites must be a non-empty list")
        return raw_sites
    if "site" in data and isinstance(data["site"], dict):
        return [data["site"]]
    raise ConnectError("Bundle needs site or sites")


def _copy_site_id(site: Mapping[str, Any], cleaned: Dict[str, Any]) -> None:
    # Hint only. A fresh enroll mints a new id. Matching uses name.
    if site.get("id"):
        cleaned["id"] = str(site["id"])


def _copy_site_description(site: Mapping[str, Any], cleaned: Dict[str, Any], index: int) -> None:
    if "description" not in site:
        return
    description = site["description"]
    if description is not None and not isinstance(description, str):
        raise ConnectError(f"sites[{index}].description must be a string or null")
    cleaned["description"] = description


def _string_list(value: Any, label: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConnectError(label)
    return list(value)


def _copy_site_tags(site: Mapping[str, Any], cleaned: Dict[str, Any], index: int) -> None:
    if "tags" not in site:
        return
    cleaned["tags"] = _string_list(site["tags"], f"sites[{index}].tags must be a list of strings")


def _copy_site_allowlist(site: Mapping[str, Any], cleaned: Dict[str, Any], index: int) -> None:
    if "proxyIpAllowlist" not in site:
        return
    allow = site["proxyIpAllowlist"]
    if allow is None:
        cleaned["proxyIpAllowlist"] = None
        return
    label = f"sites[{index}].proxyIpAllowlist must be a list of strings or null"
    cleaned["proxyIpAllowlist"] = _string_list(allow, label)


def _clean_site(site: Any, index: int) -> Dict[str, Any]:
    if not isinstance(site, dict):
        raise ConnectError(f"sites[{index}] must be an object")
    name = site.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConnectError(f"sites[{index}].name is required")
    cleaned: Dict[str, Any] = {"name": name.strip()}
    _copy_site_id(site, cleaned)
    _copy_site_description(site, cleaned, index)
    _copy_site_tags(site, cleaned, index)
    _copy_site_allowlist(site, cleaned, index)
    return cleaned


def _sites_from_document(data: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [_clean_site(site, index) for index, site in enumerate(_site_rows(data))]


def _require_org_name(org: Any) -> str:
    if not isinstance(org, dict):
        raise ConnectError("organization is required")
    name = org.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConnectError("organization.name is required")
    return name.strip()


def _copy_org_identity(org: Mapping[str, Any], portable: Dict[str, Any]) -> None:
    if org.get("id"):
        portable["id"] = str(org["id"])
    if org.get("slug"):
        portable["slug"] = str(org["slug"]).strip()


def _copy_nullable_str(org: Mapping[str, Any], portable: Dict[str, Any], key: str, label: str) -> None:
    if key not in org:
        return
    value = org[key]
    if value is not None and not isinstance(value, str):
        raise ConnectError(f"{label} must be a string or null")
    portable[key] = value


def _copy_org_address(org: Mapping[str, Any], portable: Dict[str, Any]) -> None:
    if "address" not in org:
        return
    portable["address"] = _copy_address(org["address"])
    _validate_address(portable["address"])


def _copy_org_timezone(org: Mapping[str, Any], portable: Dict[str, Any]) -> None:
    if org.get("defaultTimezone"):
        portable["defaultTimezone"] = str(org["defaultTimezone"])


def _copy_org_settings(org: Mapping[str, Any], portable: Dict[str, Any]) -> None:
    if "settings" not in org or org["settings"] is None:
        return
    if not isinstance(org["settings"], dict):
        raise ConnectError("organization.settings must be an object or null")
    portable["settings"] = org["settings"]


def _copy_org_status(org: Mapping[str, Any], portable: Dict[str, Any]) -> None:
    if not org.get("status"):
        return
    if org["status"] not in ORG_STATUSES:
        raise ConnectError("organization.status must be active or suspended")
    portable["status"] = org["status"]


def _copy_org_stamps(org: Mapping[str, Any], portable: Dict[str, Any]) -> None:
    for stamp in ("createdAt", "updatedAt"):
        if isinstance(org.get(stamp), str):
            portable[stamp] = org[stamp]


def _portable_organization(org: Any) -> Dict[str, Any]:
    portable: Dict[str, Any] = {"name": _require_org_name(org)}
    _copy_org_identity(org, portable)
    _copy_nullable_str(org, portable, "billingEmail", "organization.billingEmail")
    _copy_nullable_str(org, portable, "taxId", "organization.taxId")
    _copy_org_address(org, portable)
    _copy_org_timezone(org, portable)
    _copy_org_settings(org, portable)
    _copy_org_status(org, portable)
    _copy_org_stamps(org, portable)
    return portable


def normalize_bundle(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a secret-free bundle or raise ConnectError."""
    if not isinstance(data, dict):
        raise ConnectError("Connect bundle must be an object")
    _reject_secrets(data)
    kind = data.get("kind", BUNDLE_KIND)
    if kind != BUNDLE_KIND:
        raise ConnectError(f"Unsupported bundle kind {kind!r}")
    bundle = {
        "kind": BUNDLE_KIND,
        "organization": _portable_organization(data.get("organization")),
        "members": _members_from_document(data),
        "sites": _sites_from_document(data),
    }
    _reject_secrets(bundle)
    return bundle


def load_bundle(path: str) -> Dict[str, Any]:
    text = open(path, "r", encoding="utf-8").read()
    stripped = text.lstrip()
    if path.endswith(".json") or stripped.startswith("{") or stripped.startswith("["):
        data = json.loads(text)
    else:
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ConnectError("Connect bundle must be an object")
    return normalize_bundle(data)


def dump_bundle(bundle: Mapping[str, Any], path: str) -> None:
    """Write YAML (or JSON when the path ends with .json). Never adds secrets."""
    normalized = normalize_bundle(bundle)
    if path.endswith(".json"):
        payload = json.dumps(normalized, indent=2, sort_keys=False) + "\n"
    else:
        payload = yaml.safe_dump(normalized, sort_keys=False, allow_unicode=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(payload)


def _key_is_dropped(key: str) -> bool:
    norm = _norm_key(key)
    return norm in _FORBIDDEN_KEY_NORM or norm.endswith("url") or norm.endswith("uri")


def _text_is_dropped(value: str) -> bool:
    return bool(_is_url(value) or _GSJ_VALUE.match(value.strip()))


def _drop_dict(value: Mapping[str, Any]) -> Dict[str, Any]:
    cleaned = {}
    for key, item in value.items():
        if _key_is_dropped(key):
            continue
        cleaned[key] = _drop_urls(item)
    return cleaned


def _drop_list(value: Sequence[Any]) -> List[Any]:
    items = []
    for item in value:
        if isinstance(item, str) and _text_is_dropped(item):
            continue
        items.append(_drop_urls(item))
    return items


def _drop_urls(value: Any) -> Any:
    if isinstance(value, dict):
        return _drop_dict(value)
    if isinstance(value, list):
        return _drop_list(value)
    if isinstance(value, str) and _text_is_dropped(value):
        return None
    return value


def _keep_export_field(key: str, org: Mapping[str, Any]) -> bool:
    if key == "status" and org.get("status") not in ORG_STATUSES:
        return False
    if key == "settings" and org.get("settings") is None:
        return False
    return True


def _export_org(org: Mapping[str, Any]) -> Dict[str, Any]:
    cleaned = _drop_urls(dict(org))
    portable: Dict[str, Any] = {}
    for key in _ORG_EXPORT_KEYS:
        if key not in cleaned or not _keep_export_field(key, cleaned):
            continue
        if key == "address":
            portable["address"] = _copy_address(cleaned.get("address"))
            continue
        portable[key] = cleaned[key]
    if "name" not in portable:
        raise ConnectError("Connect organization response has no name")
    return portable


def _export_member(member: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(member, dict):
        return None
    role = member.get("role")
    user = member.get("user") if isinstance(member.get("user"), dict) else {}
    email = user.get("email")
    if role not in ROLES or not isinstance(email, str) or not email.strip():
        return None
    portable_user: Dict[str, Any] = {"email": email.strip()}
    full_name = user.get("fullName")
    if isinstance(full_name, str) and full_name.strip():
        portable_user["fullName"] = full_name.strip()
    return {"role": role, "user": portable_user}


def _export_members(members: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    portable = []
    for member in members:
        cleaned = _export_member(member)
        if cleaned:
            portable.append(cleaned)
    if not portable:
        raise ConnectError("Connect members response has no portable user")
    return portable


def _export_site_description(site: Mapping[str, Any], portable: Dict[str, Any]) -> None:
    if "description" not in site:
        return
    description = site.get("description")
    if isinstance(description, str) and _is_url(description):
        description = None
    portable["description"] = description


def _export_site_tags(site: Mapping[str, Any], portable: Dict[str, Any]) -> None:
    tags = site.get("tags")
    if not isinstance(tags, list):
        return
    portable["tags"] = [tag for tag in tags if isinstance(tag, str) and not _is_url(tag)]


def _export_site_allowlist(site: Mapping[str, Any], portable: Dict[str, Any]) -> None:
    if "proxyIpAllowlist" not in site:
        return
    allow = site.get("proxyIpAllowlist")
    if allow is None:
        portable["proxyIpAllowlist"] = None
        return
    if isinstance(allow, list):
        portable["proxyIpAllowlist"] = [
            item for item in allow if isinstance(item, str) and not _is_url(item)
        ]


def _export_site(site: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(site, dict):
        return None
    name = site.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    portable: Dict[str, Any] = {"name": name.strip()}
    if site.get("id"):
        portable["id"] = str(site["id"])
    _export_site_description(site, portable)
    _export_site_tags(site, portable)
    _export_site_allowlist(site, portable)
    return portable


def _export_sites(sites: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    portable = []
    for site in sites:
        cleaned = _export_site(site)
        if cleaned:
            portable.append(cleaned)
    if not portable:
        raise ConnectError("Connect sites response has no portable site")
    return portable


def bundle_from_api(
    org: Mapping[str, Any],
    members: Iterable[Mapping[str, Any]],
    sites: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Project live Connect payloads onto the portable allow-list."""
    return normalize_bundle(
        {
            "kind": BUNDLE_KIND,
            "organization": _export_org(org),
            "members": _export_members(members),
            "sites": _export_sites(sites),
        }
    )


def _single_org_id(listed: Sequence[Any]) -> str:
    orgs = [item for item in listed if isinstance(item, dict) and item.get("id")]
    if len(orgs) != 1:
        raise ConnectError(
            f"GET {_API_ORGANIZATIONS} returned {len(orgs)} organizations. Pass --connect-org-id."
        )
    return str(orgs[0]["id"])


def export_bundle(client: ConnectClient, org_id: Optional[str] = None) -> Dict[str, Any]:
    """Pull one organization, its members, and its sites. No other read routes."""
    if not org_id:
        listed = _as_list(client.request("GET", _API_ORGANIZATIONS), ("organizations", "items", "data"))
        org_id = _single_org_id(listed)
    org = client.request("GET", f"{_API_ORGANIZATIONS}/{org_id}")
    if not isinstance(org, dict):
        raise ConnectError("GET organization did not return an object")
    members = _as_list(
        client.request("GET", f"{_API_ORGANIZATIONS}/{org_id}/members"),
        ("members", "items", "data"),
    )
    sites = _as_list(
        client.request("GET", f"{_API_ORGANIZATIONS}/{org_id}/sites"),
        ("sites", "items", "data"),
    )
    return bundle_from_api(org, members, sites)


def _match_org(orgs: Sequence[Mapping[str, Any]], bundle_org: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    wanted = bundle_org.get("slug") or slugify(str(bundle_org["name"]))
    wanted = slugify(str(wanted))
    matches = []
    for org in orgs:
        if not isinstance(org, dict):
            continue
        current = org.get("slug") or ""
        if slugify(str(current)) == wanted:
            matches.append(org)
    if len(matches) > 1:
        raise ConnectError(f"More than one organization matches slug {wanted!r}")
    return matches[0] if matches else None


def _match_site(sites: Sequence[Mapping[str, Any]], name: str) -> Optional[Mapping[str, Any]]:
    matches = [site for site in sites if isinstance(site, dict) and site.get("name") == name]
    if len(matches) > 1:
        raise ConnectError(f"More than one site is named {name!r}")
    return matches[0] if matches else None


def _plaintext(body: Any, label: str) -> str:
    if isinstance(body, dict) and isinstance(body.get("token"), str) and body["token"]:
        return body["token"]
    raise ConnectError(f"{label} response did not include a plaintext token")


def _org_patch(bundle_org: Mapping[str, Any]) -> Dict[str, Any]:
    patch: Dict[str, Any] = {"name": bundle_org["name"]}
    if "billingEmail" in bundle_org:
        patch["billingEmail"] = bundle_org["billingEmail"]
    if "taxId" in bundle_org:
        patch["taxId"] = bundle_org["taxId"]
    if bundle_org.get("defaultTimezone"):
        patch["defaultTimezone"] = bundle_org["defaultTimezone"]
    return patch


def _require_mqtt(mqtt_url: str) -> str:
    if not isinstance(mqtt_url, str) or not mqtt_url.strip():
        raise ConnectError("GLUESYNC_CONNECT_URL is required at setup and is not read from the bundle")
    mqtt_url = mqtt_url.strip()
    parsed = urlparse(mqtt_url if "://" in mqtt_url else f"tcp://{mqtt_url}")
    if parsed.scheme not in {"tcp", "ssl", "ws", "wss", "mqtt", "mqtts"}:
        raise ConnectError("GLUESYNC_CONNECT_URL must use tcp, ssl, ws, wss, mqtt, or mqtts")
    return mqtt_url


def _bundle_notes(org_in: Mapping[str, Any]) -> List[str]:
    notes: List[str] = []
    if org_in.get("address"):
        notes.append(
            "organization.address was kept in the bundle but not sent. "
            f"POST/PATCH {_API_ORGANIZATIONS} does not accept address."
        )
    if org_in.get("settings"):
        notes.append(
            "organization.settings was kept in the bundle but not sent. "
            f"POST/PATCH {_API_ORGANIZATIONS} does not accept settings."
        )
    return notes


def _patch_existing_org(client: ConnectClient, existing: Mapping[str, Any], org_in: Mapping[str, Any]) -> str:
    org_id = str(existing.get("id") or "")
    if not org_id:
        raise ConnectError("Matched organization has no id")
    client.request("PATCH", f"{_API_ORGANIZATIONS}/{org_id}", _org_patch(org_in))
    return org_id


def _create_org_body(org_in: Mapping[str, Any]) -> Dict[str, Any]:
    body: Dict[str, Any] = {"name": org_in["name"]}
    if org_in.get("slug"):
        body["slug"] = org_in["slug"]
    if org_in.get("billingEmail"):
        body["billingEmail"] = org_in["billingEmail"]
    return body


def _created_id(created: Any, label: str) -> str:
    if not isinstance(created, dict) or not created.get("id"):
        raise ConnectError(f"{label} did not return an id")
    return str(created["id"])


def _patch_created_org(client: ConnectClient, org_id: str, org_in: Mapping[str, Any]) -> None:
    extra: Dict[str, Any] = {}
    if "taxId" in org_in:
        extra["taxId"] = org_in["taxId"]
    if org_in.get("defaultTimezone"):
        extra["defaultTimezone"] = org_in["defaultTimezone"]
    if extra:
        client.request("PATCH", f"{_API_ORGANIZATIONS}/{org_id}", extra)


def _create_org(client: ConnectClient, org_in: Mapping[str, Any]) -> str:
    created = client.request("POST", _API_ORGANIZATIONS, _create_org_body(org_in))
    org_id = _created_id(created, f"POST {_API_ORGANIZATIONS}")
    _patch_created_org(client, org_id, org_in)
    return org_id


def _upsert_organization(client: ConnectClient, org_in: Mapping[str, Any]) -> str:
    listed = _as_list(client.request("GET", _API_ORGANIZATIONS), ("organizations", "items", "data"))
    existing = _match_org([item for item in listed if isinstance(item, dict)], org_in)
    if existing:
        return _patch_existing_org(client, existing, org_in)
    return _create_org(client, org_in)


def _member_email(member: Mapping[str, Any]) -> Optional[str]:
    user = member.get("user") if isinstance(member.get("user"), dict) else {}
    email = user.get("email")
    if isinstance(email, str) and member.get("id"):
        return email.strip().lower()
    return None


def _members_by_email(client: ConnectClient, org_id: str) -> Dict[str, Mapping[str, Any]]:
    members = _as_list(
        client.request("GET", f"{_API_ORGANIZATIONS}/{org_id}/members"),
        ("members", "items", "data"),
    )
    by_email = {}
    for member in members:
        if not isinstance(member, dict):
            continue
        email = _member_email(member)
        if email:
            by_email[email] = member
    return by_email


def _register_missing_user(
    client: ConnectClient,
    email: str,
    wanted: Mapping[str, Any],
    user_password: Optional[str],
    notes: List[str],
) -> None:
    if not user_password:
        raise ConnectError(
            f"User {email} is not a member. Set CONNECT_USER_PASSWORD to register them. "
            "The admin password is not reused."
        )
    body: Dict[str, Any] = {"email": email, "password": user_password}
    if wanted["user"].get("fullName"):
        body["fullName"] = wanted["user"]["fullName"]
    try:
        client.request("POST", "/api/auth/password/register", body)
    except ConnectConflict as exc:
        if not _is_user_already_exists(exc.status, {"message": str(exc)}):
            raise
        notes.append(f"User {email} already exists. Password was not changed.")


def _apply_one_member(
    client: ConnectClient,
    org_id: str,
    wanted: Mapping[str, Any],
    by_email: Mapping[str, Mapping[str, Any]],
    admin_email: Optional[str],
    user_password: Optional[str],
    notes: List[str],
) -> None:
    email = wanted["user"]["email"]
    current = by_email.get(email.lower())
    if current:
        client.request(
            "PATCH",
            f"{_API_ORGANIZATIONS}/{org_id}/members/{current['id']}",
            {"role": wanted["role"]},
        )
        return
    if email.lower() != (admin_email or "").strip().lower():
        _register_missing_user(client, email, wanted, user_password, notes)
    client.request(
        "POST",
        f"{_API_ORGANIZATIONS}/{org_id}/members/direct",
        {"email": email, "role": wanted["role"]},
    )


def _apply_members(
    client: ConnectClient,
    org_id: str,
    wanted_members: Sequence[Mapping[str, Any]],
    by_email: Mapping[str, Mapping[str, Any]],
    admin_email: Optional[str],
    user_password: Optional[str],
    notes: List[str],
) -> None:
    for wanted in wanted_members:
        _apply_one_member(client, org_id, wanted, by_email, admin_email, user_password, notes)


def _enroll_name(bundle_sites: Sequence[Mapping[str, Any]], site_name: Optional[str]) -> str:
    if site_name:
        chosen = [site for site in bundle_sites if site["name"] == site_name]
        if len(chosen) != 1:
            raise ConnectError(f"No bundle site is named {site_name!r}")
        return site_name
    if len(bundle_sites) == 1:
        return str(bundle_sites[0]["name"])
    raise ConnectError(
        "The bundle has more than one site. Set CONNECT_SITE_NAME so exactly one enrollment token is minted."
    )


def _site_patch(site: Mapping[str, Any]) -> Dict[str, Any]:
    patch: Dict[str, Any] = {"name": site["name"]}
    if "description" in site:
        patch["description"] = site["description"]
    if "tags" in site:
        patch["tags"] = site["tags"]
    return patch


def _site_create_body(site: Mapping[str, Any]) -> Dict[str, Any]:
    body: Dict[str, Any] = {"name": site["name"]}
    if site.get("description"):
        body["description"] = site["description"]
    if site.get("tags"):
        body["tags"] = site["tags"]
    return body


def _upsert_one_site(
    client: ConnectClient,
    org_id: str,
    site: Mapping[str, Any],
    live_sites: List[Mapping[str, Any]],
) -> str:
    found = _match_site(live_sites, str(site["name"]))
    if found:
        site_id = str(found.get("id") or "")
        if not site_id:
            raise ConnectError(f"Matched site {site['name']!r} has no id")
        client.request("PATCH", f"{_API_ORGANIZATIONS}/{org_id}/sites/{site_id}", _site_patch(site))
        return site_id
    created = client.request("POST", f"{_API_ORGANIZATIONS}/{org_id}/sites", _site_create_body(site))
    site_id = _created_id(created, "POST site")
    live_sites.append({"id": site_id, "name": site["name"]})
    return site_id


def _apply_allowlist(client: ConnectClient, org_id: str, site: Mapping[str, Any], site_id: str) -> None:
    if "proxyIpAllowlist" not in site:
        return
    client.request(
        "PATCH",
        f"{_API_ORGANIZATIONS}/{org_id}/sites/{site_id}/proxy-allowlist",
        {"proxyIpAllowlist": site["proxyIpAllowlist"]},
    )


def _apply_sites(
    client: ConnectClient,
    org_id: str,
    bundle_sites: Sequence[Mapping[str, Any]],
    live_sites: List[Mapping[str, Any]],
    enroll_name: str,
) -> str:
    enrolled_site_id = None
    for site in bundle_sites:
        site_id = _upsert_one_site(client, org_id, site, live_sites)
        _apply_allowlist(client, org_id, site, site_id)
        if site["name"] == enroll_name:
            enrolled_site_id = site_id
    if not enrolled_site_id:
        raise ConnectError("No site was selected for enrollment")
    return enrolled_site_id


def _write_enrollment(out: Any, mqtt_url: str, enrollment_token: str, relay: bool) -> None:
    # Printed once. Not stored in the bundle.
    out.write(f"GLUESYNC_CONNECT_URL={mqtt_url}\n")
    out.write(f"GLUESYNC_CONNECT_TOKEN={enrollment_token}\n")
    if relay:
        out.write("GLUESYNC_JDBC_CONNECT_RELAY_ENABLED=true\n")
    out.flush()


def _configure_hub(hub: Optional[ConnectClient], mqtt_url: str, enrollment_token: str, enroll_name: str) -> bool:
    if hub is None:
        return False
    hub.request(
        "POST",
        "/connect/configure",
        {"connectUrl": mqtt_url, "enrollmentToken": enrollment_token, "siteName": enroll_name},
    )
    return True


def _mint_jdbc(
    client: ConnectClient,
    org_id: str,
    site_id: str,
    jdbc_token_name: Optional[str],
    notes: List[str],
    out: Any,
) -> None:
    if not jdbc_token_name:
        return
    notes.append(
        "JDBC relay token requested. The site must already advertise query-forge-relay-v1. "
        "Plaintext is printed once and is not written to the bundle."
    )
    created = client.request(
        "POST",
        f"{_API_ORGANIZATIONS}/{org_id}/sites/{site_id}/jdbc/tokens",
        {"name": jdbc_token_name},
    )
    out.write(f"JDBC_RELAY_TOKEN={_plaintext(created, 'JDBC relay')}\n")
    out.flush()


def _mint_mcp(
    client: ConnectClient,
    org_id: str,
    site_id: str,
    mcp_token_name: Optional[str],
    mcp_client_name: Optional[str],
    out: Any,
) -> None:
    if not mcp_token_name:
        return
    if not mcp_client_name:
        raise ConnectError("--connect-mcp-client is required with --connect-mcp-token")
    created = client.request(
        "POST",
        f"{_API_ORGANIZATIONS}/{org_id}/mcp/tokens",
        {"name": mcp_token_name, "client": mcp_client_name, "siteIds": [site_id]},
    )
    out.write(f"MCP_TOKEN={_plaintext(created, 'MCP')}\n")
    out.flush()


def setup_bundle(
    client: ConnectClient,
    bundle: Mapping[str, Any],
    *,
    mqtt_url: str,
    user_password: Optional[str] = None,
    admin_email: Optional[str] = None,
    hub: Optional[ConnectClient] = None,
    relay: bool = False,
    jdbc_token_name: Optional[str] = None,
    mcp_token_name: Optional[str] = None,
    mcp_client_name: Optional[str] = None,
    site_name: Optional[str] = None,
    stdout: Any = None,
) -> Dict[str, Any]:
    """Create or update Connect objects, then mint exactly one enrollment token.

    Existing organization (same slug), site (same name), and member (same email)
    are PATCHED. Creates that return 409 are not retried.
    """
    normalized = normalize_bundle(bundle)
    mqtt_url = _require_mqtt(mqtt_url)
    notes = _bundle_notes(normalized["organization"])
    org_id = _upsert_organization(client, normalized["organization"])
    _apply_members(
        client,
        org_id,
        normalized["members"],
        _members_by_email(client, org_id),
        admin_email,
        user_password,
        notes,
    )
    sites_payload = _as_list(
        client.request("GET", f"{_API_ORGANIZATIONS}/{org_id}/sites"),
        ("sites", "items", "data"),
    )
    live_sites = [item for item in sites_payload if isinstance(item, dict)]
    enroll_name = _enroll_name(normalized["sites"], site_name)
    enrolled_site_id = _apply_sites(client, org_id, normalized["sites"], live_sites, enroll_name)
    enrollment = client.request(
        "POST",
        f"{_API_ORGANIZATIONS}/{org_id}/sites/enrollment-tokens",
        {"siteId": enrolled_site_id, "ttlMinutes": ENROLLMENT_TTL_MINUTES},
    )
    out = stdout or sys.stdout
    _write_enrollment(out, mqtt_url, _plaintext(enrollment, "Enrollment"), relay)
    configured = _configure_hub(hub, mqtt_url, _plaintext(enrollment, "Enrollment"), enroll_name)
    _mint_jdbc(client, org_id, enrolled_site_id, jdbc_token_name, notes, out)
    _mint_mcp(client, org_id, enrolled_site_id, mcp_token_name, mcp_client_name, out)
    return {
        "organizationId": org_id,
        "siteId": enrolled_site_id,
        "siteName": enroll_name,
        "configured": configured,
        "notes": notes,
    }


def _env(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def login_connect(base_url: str, email: str, password: str, transport: Optional[Transport] = None) -> ConnectClient:
    anonymous = ConnectClient(base_url, transport=transport)
    result = anonymous.request(
        "POST",
        "/api/auth/password/login",
        {"email": email, "password": password},
    )
    if not isinstance(result, dict) or not isinstance(result.get("token"), str):
        raise ConnectError("POST /api/auth/password/login did not return a token")
    return ConnectClient(base_url, bearer=result["token"], transport=transport)


def _reject_mixed_mode(args: Any) -> None:
    exporting = bool(getattr(args, "connect_export", None))
    importing = bool(getattr(args, "connect_import", None) or getattr(args, "connect_setup", False))
    if exporting and importing:
        raise ConnectError("Use either --connect-export or --connect-import, not both")


def _require_admin_env() -> Tuple[str, str, str]:
    base_url = _env("CONNECT_API_URL")
    email = _env("CONNECT_ADMIN_EMAIL")
    password = _env("CONNECT_ADMIN_PASSWORD")
    if not base_url or not email or not password:
        raise ConnectError(
            "CONNECT_API_URL, CONNECT_ADMIN_EMAIL, and CONNECT_ADMIN_PASSWORD are required "
            "and are not read from the bundle"
        )
    return base_url, email, password


def _export_requested(args: Any, client: ConnectClient) -> bool:
    if not getattr(args, "connect_export", None):
        return False
    org_id = getattr(args, "connect_org_id", None) or _env("CONNECT_ORG_ID")
    dump_bundle(export_bundle(client, org_id), args.connect_export)
    return True


def _hub_from_env(transport: Optional[Transport]) -> Optional[ConnectClient]:
    hub_url = _env("COREHUB_URL") or _env("CORE_HUB_URL")
    hub_token = _env("COREHUB_TOKEN") or _env("GS_AUTH")
    if hub_url and hub_token:
        return ConnectClient(hub_url, bearer=hub_token, cookie_token=hub_token, transport=transport)
    if hub_url or hub_token:
        print(
            "Skipping POST /connect/configure because both COREHUB_URL and COREHUB_TOKEN are required.",
            file=sys.stderr,
        )
    return None


def _jdbc_name_from_args(args: Any) -> Optional[str]:
    if not getattr(args, "connect_jdbc_token", False):
        return None
    jdbc_name = getattr(args, "connect_jdbc_token_name", None)
    if not jdbc_name:
        raise ConnectError("--connect-jdbc-token-name is required with --connect-jdbc-token")
    print(
        "The site must already advertise query-forge-relay-v1 before a JDBC relay token can be used.",
        file=sys.stderr,
    )
    return jdbc_name


def _mcp_from_args(args: Any) -> Tuple[Optional[str], Optional[str]]:
    if not getattr(args, "connect_mcp_token", False):
        return None, None
    mcp_name = getattr(args, "connect_mcp_token_name", None)
    mcp_client_name = getattr(args, "connect_mcp_client", None)
    if not mcp_name or not mcp_client_name:
        raise ConnectError("--connect-mcp-token-name and --connect-mcp-client are required")
    return mcp_name, mcp_client_name


def _require_import(args: Any) -> str:
    path = getattr(args, "connect_import", None)
    if not path or not getattr(args, "connect_setup", False):
        raise ConnectError("Connect setup requires --connect-import PATH and --connect-setup")
    return path


def run_from_args(args: Any, transport: Optional[Transport] = None, stdout: Any = None) -> None:
    """CLI entry used by main.py. Reads runtime values from the environment."""
    _reject_mixed_mode(args)
    base_url, email, password = _require_admin_env()
    client = login_connect(base_url, email, password, transport=transport)
    if _export_requested(args, client):
        return
    bundle = load_bundle(_require_import(args))
    mqtt_url = _env("GLUESYNC_CONNECT_URL")
    if not mqtt_url:
        raise ConnectError("GLUESYNC_CONNECT_URL is required at setup and is not read from the bundle")
    mcp_name, mcp_client_name = _mcp_from_args(args)
    result = setup_bundle(
        client,
        bundle,
        mqtt_url=mqtt_url,
        user_password=_env("CONNECT_USER_PASSWORD"),
        admin_email=email,
        hub=_hub_from_env(transport),
        relay=bool(getattr(args, "connect_relay", False)),
        jdbc_token_name=_jdbc_name_from_args(args),
        mcp_token_name=mcp_name,
        mcp_client_name=mcp_client_name,
        site_name=_env("CONNECT_SITE_NAME"),
        stdout=stdout,
    )
    for note in result["notes"]:
        print(note, file=sys.stderr)


def parse_connect_args(argv: Optional[Sequence[str]] = None) -> Any:
    parser = argparse.ArgumentParser(description="Gluesync Connect setup")
    parser.add_argument("--pipeline-name", type=str, help="Ignored when Connect flags are set.")
    parser.add_argument(
        "--connect-import",
        metavar="PATH",
        help="Portable Connect bundle (YAML or JSON) to import. Broker and hub URLs are not read from this file.",
    )
    parser.add_argument(
        "--connect-setup",
        action="store_true",
        help="Apply --connect-import on Connect and mint one enrollment token.",
    )
    parser.add_argument(
        "--connect-export",
        metavar="PATH",
        help="Export a portable Connect bundle by calling the Connect read APIs.",
    )
    parser.add_argument("--connect-org-id", help="Organization id for --connect-export. Defaults to CONNECT_ORG_ID.")
    parser.add_argument(
        "--connect-relay",
        action="store_true",
        help="Print GLUESYNC_JDBC_CONNECT_RELAY_ENABLED=true for the hub process. Does not mint a JDBC token.",
    )
    parser.add_argument(
        "--connect-jdbc-token",
        action="store_true",
        help=(
            "Mint one JDBC relay token after enrollment. The site must already "
            "advertise query-forge-relay-v1. Plaintext is printed once and is not written to the bundle."
        ),
    )
    parser.add_argument("--connect-jdbc-token-name", help="Name for --connect-jdbc-token.")
    parser.add_argument(
        "--connect-mcp-token",
        action="store_true",
        help="Mint one MCP token after enrollment. Plaintext is printed once and is not written to the bundle.",
    )
    parser.add_argument("--connect-mcp-token-name", help="Name for --connect-mcp-token.")
    parser.add_argument("--connect-mcp-client", help="Client name for --connect-mcp-token.")
    return parser.parse_args(argv)


def run_cli(argv: Optional[Sequence[str]] = None) -> None:
    try:
        run_from_args(parse_connect_args(argv))
    except ConnectError as exc:
        print(f"Connect setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
