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
    slug = re.sub(r"^-+|-+$", "", slug)
    return slug


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


def _members_from_document(data: Mapping[str, Any]) -> List[Dict[str, Any]]:
    if "members" in data:
        raw_members = data["members"]
        if not isinstance(raw_members, list) or not raw_members:
            raise ConnectError("members must be a non-empty list")
        members = raw_members
    elif "user" in data and "membership" in data:
        user = data["user"] if isinstance(data["user"], dict) else {}
        membership = data["membership"] if isinstance(data["membership"], dict) else {}
        members = [{"role": membership.get("role"), "user": user}]
    else:
        raise ConnectError("Bundle needs members, or user plus membership")

    cleaned: List[Dict[str, Any]] = []
    for index, member in enumerate(members):
        if not isinstance(member, dict):
            raise ConnectError(f"members[{index}] must be an object")
        role = member.get("role")
        if role not in ROLES:
            raise ConnectError(f"members[{index}].role must be one of {', '.join(ROLES)}")
        user = member.get("user") if isinstance(member.get("user"), dict) else {}
        email = user.get("email")
        if not isinstance(email, str) or not email.strip():
            raise ConnectError(f"members[{index}].user.email is required")
        full_name = user.get("fullName")
        cleaned_user: Dict[str, Any] = {"email": email.strip()}
        if isinstance(full_name, str) and full_name.strip():
            cleaned_user["fullName"] = full_name.strip()
        cleaned.append({"role": role, "user": cleaned_user})
    return cleaned


def _sites_from_document(data: Mapping[str, Any]) -> List[Dict[str, Any]]:
    if "sites" in data:
        raw_sites = data["sites"]
        if not isinstance(raw_sites, list) or not raw_sites:
            raise ConnectError("sites must be a non-empty list")
    elif "site" in data and isinstance(data["site"], dict):
        raw_sites = [data["site"]]
    else:
        raise ConnectError("Bundle needs site or sites")

    sites: List[Dict[str, Any]] = []
    for index, site in enumerate(raw_sites):
        if not isinstance(site, dict):
            raise ConnectError(f"sites[{index}] must be an object")
        name = site.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConnectError(f"sites[{index}].name is required")
        cleaned: Dict[str, Any] = {"name": name.strip()}
        if "id" in site and site["id"]:
            # Hint only. A fresh enroll mints a new id. Matching uses name.
            cleaned["id"] = str(site["id"])
        if "description" in site:
            description = site["description"]
            if description is not None and not isinstance(description, str):
                raise ConnectError(f"sites[{index}].description must be a string or null")
            cleaned["description"] = description
        if "tags" in site:
            tags = site["tags"]
            if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                raise ConnectError(f"sites[{index}].tags must be a list of strings")
            cleaned["tags"] = list(tags)
        if "proxyIpAllowlist" in site:
            allow = site["proxyIpAllowlist"]
            if allow is not None and (
                not isinstance(allow, list) or not all(isinstance(item, str) for item in allow)
            ):
                raise ConnectError(f"sites[{index}].proxyIpAllowlist must be a list of strings or null")
            cleaned["proxyIpAllowlist"] = allow
        sites.append(cleaned)
    return sites


def normalize_bundle(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a secret-free bundle or raise ConnectError."""
    if not isinstance(data, dict):
        raise ConnectError("Connect bundle must be an object")
    _reject_secrets(data)
    kind = data.get("kind", BUNDLE_KIND)
    if kind != BUNDLE_KIND:
        raise ConnectError(f"Unsupported bundle kind {kind!r}")
    org = data.get("organization")
    if not isinstance(org, dict):
        raise ConnectError("organization is required")
    name = org.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConnectError("organization.name is required")
    portable_org: Dict[str, Any] = {"name": name.strip()}
    if org.get("id"):
        portable_org["id"] = str(org["id"])
    if org.get("slug"):
        portable_org["slug"] = str(org["slug"]).strip()
    if "billingEmail" in org:
        billing = org["billingEmail"]
        if billing is not None and not isinstance(billing, str):
            raise ConnectError("organization.billingEmail must be a string or null")
        portable_org["billingEmail"] = billing
    if "taxId" in org:
        tax_id = org["taxId"]
        if tax_id is not None and not isinstance(tax_id, str):
            raise ConnectError("organization.taxId must be a string or null")
        portable_org["taxId"] = tax_id
    if "address" in org:
        portable_org["address"] = _copy_address(org["address"])
        _validate_address(portable_org["address"])
    if org.get("defaultTimezone"):
        portable_org["defaultTimezone"] = str(org["defaultTimezone"])
    if "settings" in org and org["settings"] is not None:
        if not isinstance(org["settings"], dict):
            raise ConnectError("organization.settings must be an object or null")
        portable_org["settings"] = org["settings"]
    if org.get("status"):
        if org["status"] not in ORG_STATUSES:
            raise ConnectError("organization.status must be active or suspended")
        portable_org["status"] = org["status"]
    for stamp in ("createdAt", "updatedAt"):
        if isinstance(org.get(stamp), str):
            portable_org[stamp] = org[stamp]

    bundle = {
        "kind": BUNDLE_KIND,
        "organization": portable_org,
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


def _drop_urls(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            norm = _norm_key(key)
            if norm in _FORBIDDEN_KEY_NORM or norm.endswith("url") or norm.endswith("uri"):
                continue
            cleaned[key] = _drop_urls(item)
        return cleaned
    if isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, str) and (_is_url(item) or _GSJ_VALUE.match(item.strip())):
                continue
            items.append(_drop_urls(item))
        return items
    if isinstance(value, str) and (_is_url(value) or _GSJ_VALUE.match(value.strip())):
        return None
    return value


def bundle_from_api(org: Mapping[str, Any], members: Iterable[Mapping[str, Any]], sites: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Project live Connect payloads onto the portable allow-list."""
    org = _drop_urls(dict(org))
    portable_org: Dict[str, Any] = {}
    for key in _ORG_EXPORT_KEYS:
        if key not in org:
            continue
        if key == "address":
            portable_org["address"] = _copy_address(org.get("address"))
            continue
        if key == "status" and org.get("status") not in ORG_STATUSES:
            continue
        if key == "settings" and org.get("settings") is None:
            continue
        portable_org[key] = org[key]
    if "name" not in portable_org:
        raise ConnectError("Connect organization response has no name")

    portable_members = []
    for member in members:
        if not isinstance(member, dict):
            continue
        role = member.get("role")
        user = member.get("user") if isinstance(member.get("user"), dict) else {}
        email = user.get("email")
        if role not in ROLES or not isinstance(email, str) or not email.strip():
            continue
        portable_user: Dict[str, Any] = {"email": email.strip()}
        full_name = user.get("fullName")
        if isinstance(full_name, str) and full_name.strip():
            portable_user["fullName"] = full_name.strip()
        portable_members.append({"role": role, "user": portable_user})
    if not portable_members:
        raise ConnectError("Connect members response has no portable user")

    portable_sites = []
    for site in sites:
        if not isinstance(site, dict):
            continue
        name = site.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        portable_site: Dict[str, Any] = {"name": name.strip()}
        if site.get("id"):
            portable_site["id"] = str(site["id"])
        if "description" in site:
            description = site.get("description")
            if isinstance(description, str) and _is_url(description):
                description = None
            portable_site["description"] = description
        if isinstance(site.get("tags"), list):
            portable_site["tags"] = [tag for tag in site["tags"] if isinstance(tag, str) and not _is_url(tag)]
        if "proxyIpAllowlist" in site:
            allow = site.get("proxyIpAllowlist")
            if allow is None:
                portable_site["proxyIpAllowlist"] = None
            elif isinstance(allow, list):
                portable_site["proxyIpAllowlist"] = [
                    item for item in allow if isinstance(item, str) and not _is_url(item)
                ]
        portable_sites.append(portable_site)
    if not portable_sites:
        raise ConnectError("Connect sites response has no portable site")

    return normalize_bundle(
        {
            "kind": BUNDLE_KIND,
            "organization": portable_org,
            "members": portable_members,
            "sites": portable_sites,
        }
    )


def export_bundle(client: ConnectClient, org_id: Optional[str] = None) -> Dict[str, Any]:
    """Pull one organization, its members, and its sites. No other read routes."""
    if not org_id:
        listed = _as_list(client.request("GET", "/api/organizations"), ("organizations", "items", "data"))
        orgs = [item for item in listed if isinstance(item, dict) and item.get("id")]
        if len(orgs) != 1:
            raise ConnectError(
                f"GET /api/organizations returned {len(orgs)} organizations. Pass --connect-org-id."
            )
        org_id = str(orgs[0]["id"])
    org = client.request("GET", f"/api/organizations/{org_id}")
    if not isinstance(org, dict):
        raise ConnectError("GET organization did not return an object")
    members = _as_list(
        client.request("GET", f"/api/organizations/{org_id}/members"),
        ("members", "items", "data"),
    )
    sites = _as_list(
        client.request("GET", f"/api/organizations/{org_id}/sites"),
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
    if not isinstance(mqtt_url, str) or not mqtt_url.strip():
        raise ConnectError("GLUESYNC_CONNECT_URL is required at setup and is not read from the bundle")
    mqtt_url = mqtt_url.strip()
    parsed = urlparse(mqtt_url if "://" in mqtt_url else f"tcp://{mqtt_url}")
    if parsed.scheme not in {"tcp", "ssl", "ws", "wss", "mqtt", "mqtts"}:
        raise ConnectError(
            "GLUESYNC_CONNECT_URL must use tcp, ssl, ws, wss, mqtt, or mqtts"
        )

    notes: List[str] = []
    org_in = normalized["organization"]
    if org_in.get("address"):
        notes.append(
            "organization.address was kept in the bundle but not sent. "
            "POST/PATCH /api/organizations does not accept address."
        )
    if org_in.get("settings"):
        notes.append(
            "organization.settings was kept in the bundle but not sent. "
            "POST/PATCH /api/organizations does not accept settings."
        )

    listed = _as_list(client.request("GET", "/api/organizations"), ("organizations", "items", "data"))
    existing_org = _match_org([item for item in listed if isinstance(item, dict)], org_in)
    if existing_org:
        org_id = str(existing_org.get("id") or "")
        if not org_id:
            raise ConnectError("Matched organization has no id")
        client.request("PATCH", f"/api/organizations/{org_id}", _org_patch(org_in))
    else:
        create_body: Dict[str, Any] = {"name": org_in["name"]}
        if org_in.get("slug"):
            create_body["slug"] = org_in["slug"]
        if org_in.get("billingEmail"):
            create_body["billingEmail"] = org_in["billingEmail"]
        created = client.request("POST", "/api/organizations", create_body)
        if not isinstance(created, dict) or not created.get("id"):
            raise ConnectError("POST /api/organizations did not return an id")
        org_id = str(created["id"])
        extra = {}
        if "taxId" in org_in:
            extra["taxId"] = org_in["taxId"]
        if org_in.get("defaultTimezone"):
            extra["defaultTimezone"] = org_in["defaultTimezone"]
        if extra:
            client.request("PATCH", f"/api/organizations/{org_id}", extra)

    members = _as_list(
        client.request("GET", f"/api/organizations/{org_id}/members"),
        ("members", "items", "data"),
    )
    by_email = {}
    for member in members:
        if not isinstance(member, dict):
            continue
        user = member.get("user") if isinstance(member.get("user"), dict) else {}
        email = user.get("email")
        if isinstance(email, str) and member.get("id"):
            by_email[email.strip().lower()] = member

    for wanted in normalized["members"]:
        email = wanted["user"]["email"]
        current = by_email.get(email.lower())
        if current:
            client.request(
                "PATCH",
                f"/api/organizations/{org_id}/members/{current['id']}",
                {"role": wanted["role"]},
            )
            continue
        if email.lower() != (admin_email or "").strip().lower():
            if not user_password:
                raise ConnectError(
                    f"User {email} is not a member. Set CONNECT_USER_PASSWORD to register them. "
                    "The admin password is not reused."
                )
            try:
                register_body: Dict[str, Any] = {"email": email, "password": user_password}
                if wanted["user"].get("fullName"):
                    register_body["fullName"] = wanted["user"]["fullName"]
                client.request("POST", "/api/auth/password/register", register_body)
            except ConnectConflict as exc:
                if not _is_user_already_exists(exc.status, {"message": str(exc)}):
                    raise
                notes.append(f"User {email} already exists. Password was not changed.")
        client.request(
            "POST",
            f"/api/organizations/{org_id}/members/direct",
            {"email": email, "role": wanted["role"]},
        )

    sites_payload = _as_list(
        client.request("GET", f"/api/organizations/{org_id}/sites"),
        ("sites", "items", "data"),
    )
    live_sites = [item for item in sites_payload if isinstance(item, dict)]
    bundle_sites = normalized["sites"]
    if site_name:
        chosen = [site for site in bundle_sites if site["name"] == site_name]
        if len(chosen) != 1:
            raise ConnectError(f"No bundle site is named {site_name!r}")
        enroll_name = site_name
    elif len(bundle_sites) == 1:
        enroll_name = bundle_sites[0]["name"]
    else:
        raise ConnectError(
            "The bundle has more than one site. Set CONNECT_SITE_NAME so exactly one enrollment token is minted."
        )

    enrolled_site_id = None
    for site in bundle_sites:
        found = _match_site(live_sites, site["name"])
        if found:
            site_id = str(found.get("id") or "")
            if not site_id:
                raise ConnectError(f"Matched site {site['name']!r} has no id")
            patch: Dict[str, Any] = {"name": site["name"]}
            if "description" in site:
                patch["description"] = site["description"]
            if "tags" in site:
                patch["tags"] = site["tags"]
            client.request("PATCH", f"/api/organizations/{org_id}/sites/{site_id}", patch)
        else:
            body = {"name": site["name"]}
            if site.get("description"):
                body["description"] = site["description"]
            if site.get("tags"):
                body["tags"] = site["tags"]
            created_site = client.request("POST", f"/api/organizations/{org_id}/sites", body)
            if not isinstance(created_site, dict) or not created_site.get("id"):
                raise ConnectError("POST site did not return an id")
            site_id = str(created_site["id"])
            live_sites.append({"id": site_id, "name": site["name"]})
        if "proxyIpAllowlist" in site:
            client.request(
                "PATCH",
                f"/api/organizations/{org_id}/sites/{site_id}/proxy-allowlist",
                {"proxyIpAllowlist": site["proxyIpAllowlist"]},
            )
        if site["name"] == enroll_name:
            enrolled_site_id = site_id

    if not enrolled_site_id:
        raise ConnectError("No site was selected for enrollment")

    enrollment = client.request(
        "POST",
        f"/api/organizations/{org_id}/sites/enrollment-tokens",
        {"siteId": enrolled_site_id, "ttlMinutes": ENROLLMENT_TTL_MINUTES},
    )
    enrollment_token = _plaintext(enrollment, "Enrollment")
    out = stdout or sys.stdout
    # Printed once. Not stored in the bundle.
    out.write(f"GLUESYNC_CONNECT_URL={mqtt_url}\n")
    out.write(f"GLUESYNC_CONNECT_TOKEN={enrollment_token}\n")
    if relay:
        out.write("GLUESYNC_JDBC_CONNECT_RELAY_ENABLED=true\n")
    out.flush()

    configured = False
    if hub is not None:
        hub.request(
            "POST",
            "/connect/configure",
            {
                "connectUrl": mqtt_url,
                "enrollmentToken": enrollment_token,
                "siteName": enroll_name,
            },
        )
        configured = True

    jdbc_token = None
    if jdbc_token_name:
        notes.append(
            "JDBC relay token requested. The site must already advertise query-forge-relay-v1. "
            "Plaintext is printed once and is not written to the bundle."
        )
        created_jdbc = client.request(
            "POST",
            f"/api/organizations/{org_id}/sites/{enrolled_site_id}/jdbc/tokens",
            {"name": jdbc_token_name},
        )
        jdbc_token = _plaintext(created_jdbc, "JDBC relay")
        out.write(f"JDBC_RELAY_TOKEN={jdbc_token}\n")
        out.flush()

    mcp_token = None
    if mcp_token_name:
        if not mcp_client_name:
            raise ConnectError("--connect-mcp-client is required with --connect-mcp-token")
        created_mcp = client.request(
            "POST",
            f"/api/organizations/{org_id}/mcp/tokens",
            {
                "name": mcp_token_name,
                "client": mcp_client_name,
                "siteIds": [enrolled_site_id],
            },
        )
        mcp_token = _plaintext(created_mcp, "MCP")
        out.write(f"MCP_TOKEN={mcp_token}\n")
        out.flush()

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


def run_from_args(args: Any, transport: Optional[Transport] = None, stdout: Any = None) -> None:
    """CLI entry used by main.py. Reads runtime values from the environment."""
    if getattr(args, "connect_export", None) and (getattr(args, "connect_import", None) or getattr(args, "connect_setup", False)):
        raise ConnectError("Use either --connect-export or --connect-import, not both")

    base_url = _env("CONNECT_API_URL")
    email = _env("CONNECT_ADMIN_EMAIL")
    password = _env("CONNECT_ADMIN_PASSWORD")
    if not base_url or not email or not password:
        raise ConnectError(
            "CONNECT_API_URL, CONNECT_ADMIN_EMAIL, and CONNECT_ADMIN_PASSWORD are required "
            "and are not read from the bundle"
        )

    client = login_connect(base_url, email, password, transport=transport)

    if getattr(args, "connect_export", None):
        org_id = getattr(args, "connect_org_id", None) or _env("CONNECT_ORG_ID")
        bundle = export_bundle(client, org_id)
        dump_bundle(bundle, args.connect_export)
        return

    if not getattr(args, "connect_import", None) or not getattr(args, "connect_setup", False):
        raise ConnectError("Connect setup requires --connect-import PATH and --connect-setup")

    bundle = load_bundle(args.connect_import)
    mqtt_url = _env("GLUESYNC_CONNECT_URL")
    if not mqtt_url:
        raise ConnectError("GLUESYNC_CONNECT_URL is required at setup and is not read from the bundle")

    hub = None
    hub_url = _env("COREHUB_URL") or _env("CORE_HUB_URL")
    hub_token = _env("COREHUB_TOKEN") or _env("GS_AUTH")
    if hub_url and hub_token:
        hub = ConnectClient(hub_url, bearer=hub_token, cookie_token=hub_token, transport=transport)
    elif hub_url or hub_token:
        print(
            "Skipping POST /connect/configure because both COREHUB_URL and COREHUB_TOKEN are required.",
            file=sys.stderr,
        )

    jdbc_name = None
    if getattr(args, "connect_jdbc_token", False):
        jdbc_name = getattr(args, "connect_jdbc_token_name", None)
        if not jdbc_name:
            raise ConnectError("--connect-jdbc-token-name is required with --connect-jdbc-token")
        print(
            "The site must already advertise query-forge-relay-v1 before a JDBC relay token can be used.",
            file=sys.stderr,
        )
    mcp_name = None
    mcp_client_name = None
    if getattr(args, "connect_mcp_token", False):
        mcp_name = getattr(args, "connect_mcp_token_name", None)
        mcp_client_name = getattr(args, "connect_mcp_client", None)
        if not mcp_name or not mcp_client_name:
            raise ConnectError("--connect-mcp-token-name and --connect-mcp-client are required")

    result = setup_bundle(
        client,
        bundle,
        mqtt_url=mqtt_url,
        user_password=_env("CONNECT_USER_PASSWORD"),
        admin_email=email,
        hub=hub,
        relay=bool(getattr(args, "connect_relay", False)),
        jdbc_token_name=jdbc_name,
        mcp_token_name=mcp_name,
        mcp_client_name=mcp_client_name,
        site_name=_env("CONNECT_SITE_NAME"),
        stdout=stdout,
    )
    for note in result["notes"]:
        print(note, file=sys.stderr)
