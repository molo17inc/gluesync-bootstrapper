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

"""Back up and restore the AI settings of a Core Hub.

Core Hub (Gluesync 2.3.0.1 and later) exports every AI setting as one bundle:
LLM providers, AI Studio agents, model deployments, skill and agent versions,
skill attachments, remote MCP servers, Enterprise Brain settings, platform API
keys, budgets and every user's conversation retention. Ids and versions are
kept, so an import into another Core Hub restores them exactly. Secrets travel
only when asked, sealed with a passphrase of at least 12 characters.

Command line (the passphrase and the password are read from environment
variables, never from the command line, where other users of the machine could
see them):

    python ai_settings.py export --output ai-settings.json [--include-secrets]
    python ai_settings.py import --input ai-settings.json [--dry-run]
    python ai_settings.py compare left.json right.json

Connection: CORE_HUB_URL, SSL_ENABLED, SSL_SKIP_VERIFY as for the Bootstrapper;
the token comes from --token, else AI_SETTINGS_TOKEN, else a login as admin
with DEFAULT_PASSWORD. The passphrase comes from AI_SETTINGS_PASSPHRASE (or
the variable --passphrase-env names).

Exporting secrets takes the signed-in user's current password, as Core Hub
asks it again for that (Gluesync 2.3.0.1): it comes from
AI_SETTINGS_CURRENT_PASSWORD (or the variable --password-env names), else, when
this script signs in as admin itself, from DEFAULT_PASSWORD. The token must be
a sign-in token: Core Hub refuses secrets to a personal API token.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from commons import fetch_core_hub

EXPORT_PATH = "/ai-studio/settings/export"
IMPORT_PATH = "/ai-studio/settings/import"
BUNDLE_FORMAT = "gluesync-ai-settings"
BACKUP_FILENAME = "ai-settings.json"
MIN_PASSPHRASE_LENGTH = 12
PASSPHRASE_ENV = "AI_SETTINGS_PASSPHRASE"
CURRENT_PASSWORD_ENV = "AI_SETTINGS_CURRENT_PASSWORD"
TOKEN_ENV = "AI_SETTINGS_TOKEN"
UNSUPPORTED_MESSAGE = (
    "This Core Hub cannot back up AI settings: it needs Gluesync 2.3.0.1 or later"
)


class AiSettingsUnsupported(RuntimeError):
    """The Core Hub has no AI settings endpoints (older than 2.3.0.1)."""


class AiSettingsError(RuntimeError):
    """Core Hub refused an AI settings request; the message is Core Hub's own."""


def _http_status(exc: BaseException) -> Optional[int]:
    response = getattr(exc.__cause__, "response", None)
    return getattr(response, "status_code", None)


def _corehub_message(exc: BaseException) -> str:
    response = getattr(exc.__cause__, "response", None)
    try:
        payload = response.json() if response is not None else None
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("message"):
        return str(payload["message"])
    return str(exc)


def _call(path: str, token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        result = fetch_core_hub(path, method="POST", token=token, body=body)
    except RuntimeError as exc:
        status = _http_status(exc)
        if status in (404, 405):
            raise AiSettingsUnsupported(UNSUPPORTED_MESSAGE) from exc
        if status is not None and 400 <= status < 500:
            raise AiSettingsError(_corehub_message(exc)) from exc
        raise
    if not isinstance(result, dict):
        raise AiSettingsError(f"Unexpected answer from Core Hub: {str(result)[:200]}")
    return result


def check_passphrase(passphrase: Optional[str]) -> str:
    if passphrase is None or len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise ValueError(f"The passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters long")
    return passphrase


def check_current_password(current_password: Optional[str]) -> str:
    """Refuse before asking Core Hub, where a missing password still counts as a refused export."""
    if not current_password:
        raise ValueError("Exporting secrets needs your current Core Hub password")
    return current_password


def export_ai_settings(
    token: str,
    include_secrets: bool = False,
    passphrase: Optional[str] = None,
    current_password: Optional[str] = None,
) -> Dict[str, Any]:
    """The AI settings bundle of the configured Core Hub.

    With secrets, Core Hub 2.3.0.1 asks the signed-in user's current password again
    (``currentPassword``) and refuses a personal API token.
    """
    body: Dict[str, Any] = {"includeSecrets": include_secrets}
    if include_secrets:
        body["passphrase"] = check_passphrase(passphrase)
        body["currentPassword"] = check_current_password(current_password)
    return _call(EXPORT_PATH, token, body)


def import_ai_settings(
    token: str,
    bundle: Dict[str, Any],
    passphrase: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Import a bundle; returns Core Hub's per-section counts and warnings."""
    if not isinstance(bundle, dict) or bundle.get("format") != BUNDLE_FORMAT:
        raise ValueError(f"Not an AI settings bundle: expected format '{BUNDLE_FORMAT}'")
    if bundle.get("secrets") and not passphrase:
        raise ValueError("This bundle carries secrets: give the passphrase it was exported with")
    body: Dict[str, Any] = {"bundle": bundle, "dryRun": dry_run}
    if passphrase:
        body["passphrase"] = passphrase
    return _call(IMPORT_PATH, token, body)


def summarize_import(result: Dict[str, Any]) -> str:
    """One line per section that changed something, then the warnings."""
    lines: List[str] = []
    for section in result.get("sections") or []:
        counts = [
            f"{section.get(key, 0)} {label}"
            for key, label in (("created", "created"), ("updated", "updated"), ("skipped", "skipped"))
            if section.get(key)
        ]
        if counts:
            lines.append(f"{section.get('name')}: {', '.join(counts)}")
    if not lines:
        lines.append("nothing to change")
    prefix = "Dry run, nothing written. " if result.get("dryRun") else ""
    warnings = result.get("warnings") or []
    text = prefix + "; ".join(lines)
    if warnings:
        text += ". To do: " + " | ".join(warnings)
    return text


# Fields that differ between two exports of the same settings without being settings.
_VOLATILE_KEYS = ("exportedAt", "secrets")


def comparable(bundle: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in bundle.items() if key not in _VOLATILE_KEYS}


def compare_bundles(left: Dict[str, Any], right: Dict[str, Any]) -> List[str]:
    """Differences between two bundles, by section and id; empty when they hold the same settings."""
    differences: List[str] = []
    left_cmp, right_cmp = comparable(left), comparable(right)
    for section in sorted(set(left_cmp) | set(right_cmp)):
        left_value, right_value = left_cmp.get(section), right_cmp.get(section)
        if left_value == right_value:
            continue
        if isinstance(left_value, list) and isinstance(right_value, list):
            differences.extend(_compare_rows(section, left_value, right_value))
        else:
            differences.append(f"{section}: {json.dumps(left_value)} != {json.dumps(right_value)}")
    return differences


def _row_key(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("id") or row.get("owner") or json.dumps(row, sort_keys=True))
    return json.dumps(row, sort_keys=True)


def _compare_rows(section: str, left: List[Any], right: List[Any]) -> List[str]:
    left_rows = {_row_key(row): row for row in left}
    right_rows = {_row_key(row): row for row in right}
    differences = [f"{section}: {key} missing" for key in sorted(set(left_rows) - set(right_rows))]
    differences += [f"{section}: {key} unexpected" for key in sorted(set(right_rows) - set(left_rows))]
    for key in sorted(set(left_rows) & set(right_rows)):
        if left_rows[key] != right_rows[key]:
            changed = sorted(
                field
                for field in set(left_rows[key]) | set(right_rows[key])
                if left_rows[key].get(field) != right_rows[key].get(field)
            ) if isinstance(left_rows[key], dict) and isinstance(right_rows[key], dict) else ["value"]
            differences.append(f"{section}: {key} differs in {', '.join(changed)}")
    return differences


def _token(explicit: Optional[str]) -> str:
    token = explicit or os.getenv(TOKEN_ENV)
    if token:
        return token
    return _admin_login()


def _admin_login() -> str:
    response = fetch_core_hub(
        "/authentication/login",
        method="POST",
        body={"username": "admin", "password": os.getenv("DEFAULT_PASSWORD", "")},
    )
    token = response.get("apiToken") if isinstance(response, dict) else None
    if not token:
        raise SystemExit("Cannot log in to Core Hub: set AI_SETTINGS_TOKEN or DEFAULT_PASSWORD")
    return token


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Back up and restore the AI settings of a Core Hub")
    parser.add_argument("--token", help=f"Core Hub token (default ${TOKEN_ENV}, else admin login)")
    parser.add_argument("--passphrase-env", default=PASSPHRASE_ENV,
                        help=f"Environment variable holding the passphrase (default {PASSPHRASE_ENV})")
    parser.add_argument("--password-env", default=CURRENT_PASSWORD_ENV,
                        help="Environment variable holding your current Core Hub password, needed to export "
                             f"secrets (default {CURRENT_PASSWORD_ENV})")
    commands = parser.add_subparsers(dest="command", required=True)

    export_cmd = commands.add_parser("export", help="Write the AI settings bundle to a file")
    export_cmd.add_argument("--output", default=BACKUP_FILENAME)
    export_cmd.add_argument("--include-secrets", action="store_true",
                            help="Seal provider keys, MCP credentials and API key hashes with the passphrase "
                                 "(needs your current Core Hub password)")

    import_cmd = commands.add_parser("import", help="Restore an AI settings bundle")
    import_cmd.add_argument("--input", default=BACKUP_FILENAME)
    import_cmd.add_argument("--dry-run", action="store_true", help="Only report what would change")

    compare_cmd = commands.add_parser("compare", help="Exit 1 when two bundles hold different settings")
    compare_cmd.add_argument("left")
    compare_cmd.add_argument("right")

    args = parser.parse_args(argv)
    passphrase = os.getenv(args.passphrase_env)

    try:
        if args.command == "compare":
            differences = compare_bundles(_read_json(args.left), _read_json(args.right))
            print("\n".join(differences) or "The two bundles hold the same AI settings")
            return 1 if differences else 0
        token = _token(args.token)
        if args.command == "export":
            current_password = os.getenv(args.password_env)
            if not current_password and not (args.token or os.getenv(TOKEN_ENV)):
                # This script signed in as admin with DEFAULT_PASSWORD: that is the password Core Hub asks again.
                current_password = os.getenv("DEFAULT_PASSWORD")
            bundle = export_ai_settings(token, include_secrets=args.include_secrets, passphrase=passphrase,
                                        current_password=current_password)
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(bundle, f, indent=2, ensure_ascii=False)
            print(f"AI settings written to {args.output} ({'with' if args.include_secrets else 'without'} secrets)")
            return 0
        result = import_ai_settings(token, _read_json(args.input), passphrase=passphrase, dry_run=args.dry_run)
        print(summarize_import(result))
        return 0
    except (AiSettingsUnsupported, AiSettingsError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
