#!/usr/bin/env python3
"""Run one shard of the bootstrapper core unit tests (CI job bootstrapper_core_unit_tests).

The job used to run the GROUPS below one after the other, each in its own `python3 -m unittest` process. Now N parallel
jobs run them, and every test CLASS is assigned to exactly one shard:

  * the classes named in CLASS_SHARD go to the shard listed there (the slow ones, balanced by measured duration),
  * every other class, including classes added later, goes to DEFAULT_SHARD,

so no test can be left out, whatever is added to the test files. Each group still runs in its own process, as before.

  run_unit_shard.py [SHARD]    run the tests of SHARD (default: $CI_NODE_INDEX); SHARD is 1..SHARDS
  run_unit_shard.py --list     print which classes and how many tests every shard runs

Run it from the repository root.
"""
import json
import os
import subprocess
import sys
import unittest

SHARDS = 4
DEFAULT_SHARD = 4

# Same invocations, in the same order, as the old single job. A group with "discover" is
# `unittest discover integration-tests -p <pattern>`, otherwise `unittest <names...>`.
GROUPS = [
    {"title": "bootstrapper core YAML payload unit tests",
     "discover": ("integration-tests", "test_bootstrapper_*.py")},
    {"title": "Connect portable unit tests",
     "names": ["integration-tests.test_connect_setup"]},
    {"title": "Chronos export regression tests",
     "names": ["integration-tests.test_export_template_from_corehub"]},
    {"title": "automator CoreHub flow tests",
     "names": ["integration-tests.test_automator_corehub_flows"]},
    {"title": "automator backup/export tests",
     "names": ["integration-tests.test_automator_backup_export"]},
    {"title": "automator backup/import tests",
     "names": ["integration-tests.test_automator_backup_import"]},
    {"title": "AI settings backup tests",
     "names": ["integration-tests.test_ai_settings"]},
    {"title": "user group and data-access policy backup tests",
     "names": ["integration-tests.test_automator_user_groups_backup"]},
    {"title": "Connect backup exclusion tests",
     "names": ["integration-tests.test_automator_connect_backup_exclusion"]},
    {"title": "column mapping matrix regression tests",
     "names": ["integration-tests.test_unlocked_schema_and_target_only_columns",
               "integration-tests.test_multitable_chains",
               "integration-tests.test_field_functions_export_import"]},
    {"title": "demo-kits round-trip verifier tests",
     "names": ["integration-tests.test_automator_roundtrip"]},
    {"title": "automator app regression tests",
     "names": ["integration-tests.test_automator_app_preexisting_bugs"]},
]

# "<test module>.<test class>": shard. Local durations (Python 3.11, seconds) of the classes that dominate the run:
#   test_bootstrapper_function_collection.ExportTests           60   -> 1
#   test_automator_backup_export.ExportGlobalConfigYamlTests    30 \
#   test_automator_backup_export.ExportPipelineYamlTests        30 / -> 2
#   test_automator_backup_export.ExportAllPipelinesYamlTests    13 \
#   test_automator_backup_export.ExportPipelineEntitiesPackage  12  | -> 3
#   test_automator_backup_export.ExportPipelineFullBackupTests  12 /
# Everything else (about 32 s: the other backup export classes, the rest of the bootstrapper_* files, the small
# modules) is the default shard.
CLASS_SHARD = {
    "test_bootstrapper_function_collection.ExportTests": 1,
    "test_automator_backup_export.ExportGlobalConfigYamlTests": 2,
    "test_automator_backup_export.ExportPipelineYamlTests": 2,
    "test_automator_backup_export.ExportAllPipelinesYamlTests": 3,
    "test_automator_backup_export.ExportPipelineEntitiesPackageTests": 3,
    "test_automator_backup_export.ExportPipelineFullBackupTests": 3,
}


def class_key(test):
    cls = type(test)
    return f"{cls.__module__.rsplit('.', 1)[-1]}.{cls.__name__}"


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def load_group(group):
    loader = unittest.TestLoader()
    if "discover" in group:
        start_dir, pattern = group["discover"]
        return list(flatten(loader.discover(start_dir, pattern=pattern)))
    return list(flatten(loader.loadTestsFromNames(group["names"])))


def shard_of(test):
    return CLASS_SHARD.get(class_key(test), DEFAULT_SHARD)


def child(index, shard):
    """Load GROUPS[index] and run its tests of `shard` (shard 0: only print the per-class counts as JSON)."""
    sys.path.insert(0, os.getcwd())  # what `python -m unittest` does
    tests = load_group(GROUPS[index])
    if shard == 0:
        counts = {}
        for test in tests:
            counts.setdefault(class_key(test), [shard_of(test), 0])[1] += 1
        print(json.dumps(counts))
        return 0
    mine = [test for test in tests if shard_of(test) == shard]
    if not mine:
        print(f"no test of this group in shard {shard}")
        return 0
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(mine))
    return 0 if result.wasSuccessful() else 1


def spawn(index, shard, capture=False):
    cmd = [sys.executable, os.path.abspath(__file__), "--group", str(index), str(shard)]
    return subprocess.run(cmd, stdout=subprocess.PIPE if capture else None, text=True)


def list_shards():
    per_shard = {n: {} for n in range(1, SHARDS + 1)}
    for index in range(len(GROUPS)):
        done = spawn(index, 0, capture=True)
        if done.returncode != 0:
            return done.returncode
        for key, (shard, count) in json.loads(done.stdout.strip().splitlines()[-1]).items():
            per_shard[shard][key] = per_shard[shard].get(key, 0) + count
    total = 0
    for shard, classes in per_shard.items():
        n = sum(classes.values())
        total += n
        print(f"shard {shard}: {n} tests in {len(classes)} classes")
        for key, count in sorted(classes.items()):
            print(f"    {count:4d}  {key}")
    print(f"total: {total} tests")
    return 0


def run_shard(shard):
    failed = []
    for index, group in enumerate(GROUPS):
        print(f"Running {group['title']} (shard {shard}/{SHARDS})...", flush=True)
        if spawn(index, shard).returncode != 0:
            failed.append(group["title"])
    if failed:
        print("FAILED groups: " + "; ".join(failed))
        return 1
    return 0


def main(argv):
    if argv[:1] == ["--group"]:
        return child(int(argv[1]), int(argv[2]))
    if argv[:1] == ["--list"]:
        return list_shards()
    shard = int(argv[0]) if argv else int(os.environ.get("CI_NODE_INDEX", "1"))
    if not 1 <= shard <= SHARDS:
        sys.exit(f"shard must be 1..{SHARDS}, got {shard}")
    total = int(os.environ.get("CI_NODE_TOTAL", SHARDS))
    if total != SHARDS:
        sys.exit(f"CI_NODE_TOTAL is {total} but this script splits into {SHARDS} shards: keep `parallel:` in sync")
    return run_shard(shard)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
