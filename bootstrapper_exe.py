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

"""Entry point of ``gluesync-bootstrapper.exe``, the PyInstaller build inside the Windows container image.

The Linux image runs ``python main.py`` (the bootstrapper) or ``python -c <script>`` (the ``hub-first-login``
one-shot of the demo kits). The Windows image has no Python, so one exe carries both:

    gluesync-bootstrapper.exe [run] [--pipeline-name NAME]   the bootstrapper (the default, like ``python main.py``)
    gluesync-bootstrapper.exe hub-first-login                admin/admin -> HUB_ADMIN_PASSWORD on a fresh Core Hub
    gluesync-bootstrapper.exe create-entities ...            create_all_entities.py; the bootstrapper runs it per schema
    gluesync-bootstrapper.exe self-test                      load every module the exe bundles, no Core Hub needed
    gluesync-bootstrapper.exe --help | --version

``main.py`` logs in to Core Hub and creates the pipeline while it is imported, so it is only loaded for the
bootstrapper command: ``--help``, ``--version``, ``self-test`` and ``hub-first-login`` never touch it.
"""

import importlib
import os
import runpy
import subprocess
import sys

# Modules the bootstrapper command imports besides main.py: self-test loads them to prove the exe is complete.
BUNDLED_MODULES = (
    "requests", "urllib3", "yaml", "faker", "colorama", "pydantic", "logstash_async",
    "commons", "create_all_entities", "create_all_tables", "create_user_defined_functions", "entity_mapping",
    "field_function_utils", "function_collection", "import_options", "fan_in", "table_target_schema", "connect_portable", "hub_first_login",
    "import_plan", "utils.log", "utils.core_hub_client", "utils.transactions_audit_client", "utils.gluesync_sdk_client",
    "utils.udf_signature", "utils.redact", "utils.chronos_client",
)

USAGE = """\
Gluesync Bootstrapper (Windows container image)

Usage:
  gluesync-bootstrapper.exe [run] [--pipeline-name NAME]
      Create the pipeline described by FILE_CONF_PATH and TABLE_LIST_YAML on the Core Hub at CORE_HUB_URL
      (the Linux image's `python main.py`). Also accepts the --connect-* options of main.py.
  gluesync-bootstrapper.exe hub-first-login [--attempts N] [--interval SECONDS] [--timeout SECONDS]
      Log in to a fresh Core Hub as admin/admin and set the admin password to HUB_ADMIN_PASSWORD.
      Exit codes: 0 done, 1 unexpected Core Hub response, 2 Core Hub not reachable.
  gluesync-bootstrapper.exe create-entities --pipeline ID --source-schema S ...
      create_all_entities.py (the bootstrapper starts it once per schema; there is no Python to start it with).
  gluesync-bootstrapper.exe self-test
      Load every bundled module and exit; needs no Core Hub.
  gluesync-bootstrapper.exe --help | --version

Environment (bootstrapper): CORE_HUB_URL, FILE_CONF_PATH, TABLE_LIST_YAML, SOURCE_TYPE, TARGET_TYPE,
  DEFAULT_PASSWORD, ADMIN_PASSWORD, CREATE_TABLE_IF_NOT_EXISTS, ENTITY_START_TIMEOUT, START_SYNC, SSL_ENABLED,
  SSL_SKIP_VERIFY, LOG_DIR, LOG_LEVEL, UDF_PATH, TEST_NAME, JOB_ID, VERSION, BOOTSTRAPPER_RESULT_FILE.
Environment (hub-first-login): CORE_HUB_URL, HUB_ADMIN_PASSWORD, HUB_LOGIN_ATTEMPTS, HUB_LOGIN_INTERVAL,
  SSL_SKIP_VERIFY.
Paths follow the Linux image with C:\\ in front: /opt/config -> C:\\opt\\config, /logs -> C:\\logs.
"""


def read_version():
    """The version baked in at build time (the VERSION file next to the modules), 'dev' when there is none."""
    roots = [getattr(sys, "_MEIPASS", None), os.path.dirname(os.path.abspath(__file__))]
    for root in roots:
        if not root:
            continue
        try:
            with open(os.path.join(root, "VERSION"), encoding="utf-8-sig") as handle:
                version = handle.read().strip()
        except OSError:
            continue
        if version:
            return version
    return "dev"


def self_test():
    """Import every bundled module and check the data files; returns the exit code."""
    failures = []
    for name in BUNDLED_MODULES:
        try:
            importlib.import_module(name)
        except Exception as error:  # pylint: disable=broad-except
            failures.append(f"{name}: {type(error).__name__}: {error}")
    if getattr(sys, "frozen", False) and not sys.flags.utf8_mode:
        failures.append("Python UTF-8 mode is off: the spec must pass the 'X utf8' runtime option")
    import commons  # noqa: E402  pylint: disable=import-outside-toplevel
    agents_json = os.path.join(os.path.dirname(os.path.abspath(commons.__file__)), "agents.json")
    if not os.path.isfile(agents_json):
        failures.append(f"agents.json is not bundled (looked for {agents_json})")
    if getattr(sys, "frozen", False):
        # main.py starts `<exe> create-entities` once per schema: the exe must be able to run itself
        child = subprocess.run(
            [sys.executable, "create-entities", "--help"], capture_output=True, text=True, check=False, timeout=120
        )
        if child.returncode != 0 or "--pipeline" not in child.stdout:
            failures.append(f"the exe cannot run itself as `create-entities` (exit {child.returncode}): {child.stderr[-300:]}")
    if failures:
        print("self-test FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(
        f"self-test OK: {len(BUNDLED_MODULES)} modules and agents.json loaded "
        f"(version {read_version()}, utf8_mode={sys.flags.utf8_mode}, python {sys.version.split()[0]})"
    )
    return 0


def run_module_as_script(module, argv):
    """Run ``module`` exactly like ``python <module>.py <argv>``: its script code runs while it is loaded as __main__."""
    sys.argv = [sys.argv[0]] + list(argv)
    runpy.run_module(module, run_name="__main__")
    return 0


def run_bootstrapper(argv):
    """``python main.py <argv>``: main.py logs in and creates the pipeline while it is loaded."""
    return run_module_as_script("main", argv)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "run"

    if command in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if command in ("--version", "version"):
        print(read_version())
        return 0
    if command == "self-test":
        return self_test()
    if command == "hub-first-login":
        import hub_first_login  # pylint: disable=import-outside-toplevel
        return hub_first_login.main(argv[1:])
    if command == "create-entities":
        return run_module_as_script("create_all_entities", argv[1:])
    if command == "run":
        argv = argv[1:]
    return run_bootstrapper(argv)


if __name__ == "__main__":
    sys.exit(main())
