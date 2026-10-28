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

"""Import an Export All / pipeline backup ZIP from the command line, the way the Automator's Import All does.

    GS_COREHUB_PASSWORD=... python -m automator_app.import_backup backup.zip \
        --base-url https://localhost:1717 --username admin --skip-verify \
        [--agent-overrides overrides.json] [--resume-pipeline-id <id>] [--on-existing skip|update|fail]
        [--plan plan.json] [--dry-run]

    python -m automator_app.import_backup backup.zip --write-plan plan.json   # the draft plan, offline

A plan (import_plan.py; the Automator's "Download plan JSON" writes the same file) picks the pipelines and
tables, renames, agent edits, skipped parts and on-existing behaviour; its passwords are ``passwordRef:
"env:NAME"`` references, read from this process' environment. ``--dry-run`` prints the preview (Core Hub is
only read) and exits 1 when the preview finds a problem.

The Core Hub password is read from the environment (``--password-env``, default GS_COREHUB_PASSWORD), never
from a flag. Agent passwords come from ``GS_IMPORT_AGENT_PASSWORD_<TAG>`` (see agent_overrides.py).

Exit code: 0 every pipeline fully imported; 2 a pipeline was created/resumed but only partially restored (an
entity failed or was not created); 1 nothing was imported (or the archive / overrides were refused).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Optional

EXIT_CODES = {"full": 0, "failed": 1, "partial": 2}


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import a Gluesync backup ZIP into a Core Hub")
    parser.add_argument("zip", help="backup ZIP (Export All or single-pipeline full backup)")
    parser.add_argument("--base-url", default=os.getenv("GS_COREHUB_URL", "https://localhost:1717"),
                        help="Core Hub URL (env GS_COREHUB_URL; default https://localhost:1717)")
    parser.add_argument("--username", default=os.getenv("GS_COREHUB_USERNAME", "admin"),
                        help="Core Hub user (env GS_COREHUB_USERNAME; default admin)")
    parser.add_argument("--password-env", default="GS_COREHUB_PASSWORD",
                        help="environment variable holding the Core Hub password (default GS_COREHUB_PASSWORD)")
    parser.add_argument("--use-ssl", action=argparse.BooleanOptionalAction, default=None,
                        help="talk to Core Hub over HTTPS (default: when --base-url starts with https://)")
    parser.add_argument("--skip-verify", action="store_true", help="do not verify the Core Hub TLS certificate")
    parser.add_argument("--no-create-tables", action="store_true", help="do not create missing target tables")
    parser.add_argument("--no-scheduling", action="store_true", help="do not restore entity schedules")
    parser.add_argument("--agent-overrides", metavar="FILE",
                        help="JSON file of agent connection overrides (same as GS_IMPORT_AGENT_OVERRIDES_FILE)")
    parser.add_argument("--resume-pipeline-id", metavar="ID",
                        help="resume this existing pipeline: only specific configuration, entities and UDFs are "
                             "restored (same as GS_IMPORT_RESUME_PIPELINE_ID)")
    parser.add_argument("--resume-from-pipeline", metavar="OLD_ID",
                        help="with --resume-pipeline-id: the backup pipeline id to take, when the ZIP has several")
    parser.add_argument("--on-existing", choices=("skip", "update", "fail"),
                        help="entities that already exist (GS_IMPORT_ON_EXISTING; a resume defaults to skip)")
    parser.add_argument("--no-infer-target-schema", action="store_true",
                        help="do not take a table's target schema from its snapshot commands "
                             "(same as GS_IMPORT_INFER_TARGET_SCHEMA=false)")
    parser.add_argument("--plan", metavar="FILE", help="import plan JSON (what the Automator's Customize step saves)")
    parser.add_argument("--write-plan", metavar="FILE",
                        help="write the draft plan of the ZIP (every pipeline and table) to FILE and exit; offline")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what the import would do (only GET requests to Core Hub) and exit")
    args = parser.parse_args(argv)
    if args.resume_from_pipeline and not (args.resume_pipeline_id or os.getenv("GS_IMPORT_RESUME_PIPELINE_ID")):
        parser.error("--resume-from-pipeline needs --resume-pipeline-id <id of the pipeline to resume on Core Hub>; "
                     "--resume-from-pipeline only picks which backup pipeline of the ZIP to take")
    if args.use_ssl is None:
        args.use_ssl = str(args.base_url).strip().lower().startswith("https://")
    return args


def _write_plan(args: argparse.Namespace, contents: bytes) -> int:
    import json  # pylint: disable=import-outside-toplevel
    from fastapi import HTTPException  # pylint: disable=import-outside-toplevel
    from . import app  # pylint: disable=import-outside-toplevel

    try:
        analysis = app._analyze_archive(contents)  # pylint: disable=protected-access
    except HTTPException as exc:
        print(f"Cannot analyse {args.zip}: {exc.detail}", file=sys.stderr)
        return 1
    with open(args.write_plan, "w", encoding="utf-8") as handle:
        json.dump(analysis["plan"], handle, indent=2)
        handle.write("\n")
    for item in analysis["pipelines"]:
        print(f"{item['oldId']}: {item['name']} ({len(item['entities'])} tables)")
    print(f"Draft plan written to {args.write_plan}")
    return 0


def _load_plan(args: argparse.Namespace):
    from .app import import_plan  # pylint: disable=import-outside-toplevel

    if not args.plan:
        return None
    try:
        with open(args.plan, "r", encoding="utf-8") as handle:
            return import_plan.load(handle.read())
    except OSError as exc:
        raise import_plan.PlanError(f"cannot read {args.plan}: {exc.strerror}") from None


def _print_preview(preview: dict) -> int:
    for item in preview["pipelines"]:
        if not item.get("import"):
            print(f"{item['oldId']} {item['backupName']}: not imported")
            continue
        print(f"{item['oldId']} {item['backupName']}: {item.get('action', item.get('error'))}, "
              f"{item.get('entityCount', 0)} entities")
        for entity in item.get("entities") or []:
            print(f"  {entity['source']} -> {entity['targetSchema']}.{entity['targetTable']}")
        for agent in item.get("agents") or []:
            print(f"  agent {agent['agentTag']}: {agent['host']}:{agent['port']} db={agent['database']} "
                  f"user={agent['username']} password: {agent['password']}")
        if item.get("conflicts"):
            print(f"  already in the pipeline: {', '.join(item['conflicts'])} (on existing: {item['onExistingEntity']})")
    print(f"Globals: {preview['globals']}")
    print(f"Total: {preview['totals']['pipelines']} pipeline(s), {preview['totals']['entities']} entities")
    for error in preview["errors"]:
        print(f"PROBLEM: {error}")
    return 0 if preview["ok"] else 1


def _export_options_env(args: argparse.Namespace) -> None:
    """Hand the CLI options the import reads from the environment over to it."""
    if args.agent_overrides:
        os.environ["GS_IMPORT_AGENT_OVERRIDES_FILE"] = args.agent_overrides
    if args.on_existing:
        os.environ["GS_IMPORT_ON_EXISTING"] = args.on_existing
    if args.no_infer_target_schema:
        os.environ["GS_IMPORT_INFER_TARGET_SCHEMA"] = "false"


def _read_zip(args: argparse.Namespace) -> Optional[bytes]:
    """The backup ZIP's bytes; None (after saying why) when it cannot be read."""
    try:
        with open(args.zip, "rb") as handle:
            return handle.read()
    except OSError as exc:
        print(f"Cannot read {args.zip}: {exc.strerror}", file=sys.stderr)
        return None


def _dry_run(args: argparse.Namespace, contents: bytes, plan: Any) -> int:
    from fastapi import HTTPException  # pylint: disable=import-outside-toplevel
    from . import app  # pylint: disable=import-outside-toplevel
    from .app import import_plan  # pylint: disable=import-outside-toplevel
    try:
        preview = app._preview_import(contents, plan or import_plan.ImportPlan(),  # pylint: disable=protected-access
                                      resume_pipeline_id=args.resume_pipeline_id)
    except HTTPException as exc:
        print(f"Preview refused: {exc.detail}", file=sys.stderr)
        return 1
    return _print_preview(preview)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    if args.write_plan:
        try:
            with open(args.zip, "rb") as handle:
                return _write_plan(args, handle.read())
        except OSError as exc:
            print(f"Cannot read {args.zip}: {exc.strerror}", file=sys.stderr)
            return 1
    from .app import import_plan  # pylint: disable=import-outside-toplevel
    try:
        plan = _load_plan(args)
    except import_plan.PlanError as exc:
        print(f"Import plan refused: {exc}", file=sys.stderr)
        return 1
    password = os.getenv(args.password_env)
    if not password:
        print(f"Set the Core Hub password in ${args.password_env}", file=sys.stderr)
        return 1
    _export_options_env(args)
    contents = _read_zip(args)
    if contents is None:
        return 1

    from fastapi import HTTPException  # pylint: disable=import-outside-toplevel
    from . import app, corehub  # pylint: disable=import-outside-toplevel
    from .state import state  # pylint: disable=import-outside-toplevel

    try:
        token = corehub.authenticate(base_url=args.base_url, username=args.username, password=password,
                                     use_ssl=args.use_ssl, skip_verify=args.skip_verify)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"Core Hub login failed: {exc}", file=sys.stderr)
        return 1
    state.set_auth(token, args.base_url, use_ssl=args.use_ssl, skip_verify=args.skip_verify)
    state.set_preferences(enable_scheduling=not args.no_scheduling, create_tables=not args.no_create_tables)

    if args.dry_run:
        return _dry_run(args, contents, plan)

    run_kwargs = {"resume_pipeline_id": args.resume_pipeline_id,
                  "resume_source_pipeline_id": args.resume_from_pipeline}
    if plan is not None:
        run_kwargs.update(plan=plan, plan_env=os.environ)
    try:
        job = app._run_archive_import(contents, **run_kwargs)  # pylint: disable=protected-access
    except HTTPException as exc:
        print(f"Import refused: {exc.detail}", file=sys.stderr)
        return 1
    for line in job.notes:
        print(line)
    for line in app._report_lines(job):  # pylint: disable=protected-access
        print(line)
    status = app.import_status(job)
    print(f"Import status: {status.upper()}")
    return EXIT_CODES[status]


if __name__ == "__main__":
    sys.exit(main())
