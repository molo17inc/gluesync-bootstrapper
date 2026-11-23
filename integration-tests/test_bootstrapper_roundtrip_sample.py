#!/usr/bin/env python3
"""The Automator round trip can check a sample of a large pipeline's entities (AUTOMATOR_ROUNDTRIP_SAMPLE).

The 1000-table pipeline of as400-mssql-TRUNCATE took 13 of the kit's 67 minutes to round-trip (23 Nov 2026). With a
sample, only the sampled entities are restored and compared, and a shape the sampler cannot cut safely keeps the whole
pipeline.
"""

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import automator_roundtrip as rt  # noqa: E402


def export_of(n, schema="TESTTAB1K", groups=None, without_entity_name=()):
    tables = [f"TABLE{i:05d}" for i in range(1, n + 1)]
    doc = {
        schema: {
            "target": schema,
            "sourceType": "SQL",
            "targetType": "SQL",
            "tables": {
                "whitelist": tables,
                "custom": {t: {"name": t, "keys": ["ID"]} if t in without_entity_name
                           else {"name": t, "entityName": f"{schema}.{t}", "keys": ["ID"]} for t in tables},
            },
        },
        "groups": groups if groups is not None else {"default": {"id": "_default", "color": "#00000000"}},
    }
    return "# header\n# Pipeline ID : abc\n" + yaml.safe_dump(doc, sort_keys=False)


def side_of(n, export=None):
    return {
        "export": export or export_of(n),
        "entities": [{"entityId": f"id{i}", "entityName": f"TESTTAB1K.TABLE{i:05d}"} for i in range(1, n + 1)],
    }


class PickSampleTests(unittest.TestCase):
    def test_spread_over_the_names_first_and_last_included(self):
        names = [f"T{i:04d}" for i in range(1, 1001)]
        sample = rt.pick_sample(names, 50)
        self.assertEqual(len(sample), 50)
        self.assertEqual(sample[0], "T0001")
        self.assertEqual(sample[-1], "T1000")
        self.assertEqual(sample, rt.pick_sample(list(reversed(names)), 50), "same names, same sample")

    def test_all_names_when_not_more_than_the_size(self):
        self.assertEqual(rt.pick_sample(["b", "a"], 5), ["a", "b"])
        self.assertEqual(rt.pick_sample(["b", "a"], 0), ["a", "b"])


class SampleExportTests(unittest.TestCase):
    def test_keeps_the_sampled_tables_and_the_rest_of_the_block(self):
        keep = {"TESTTAB1K.TABLE00001", "TESTTAB1K.TABLE00007"}
        out = rt.sample_export(export_of(10), keep)
        tables = out["TESTTAB1K"]["tables"]
        self.assertEqual(tables["whitelist"], ["TABLE00001", "TABLE00007"])
        self.assertEqual(sorted(tables["custom"]), ["TABLE00001", "TABLE00007"])
        self.assertEqual(out["TESTTAB1K"]["target"], "TESTTAB1K")
        self.assertIn("groups", out)

    def test_refuses_shapes_it_cannot_cut(self):
        keep = {"TESTTAB1K.TABLE00001"}
        no_entity_name = export_of(3, without_entity_name=("TABLE00002",))
        self.assertIsNone(rt.sample_export(no_entity_name, keep))
        group_with_members = export_of(3, groups={"g": {"id": "g", "entities": ["TESTTAB1K.TABLE00001"]}})
        self.assertIsNone(rt.sample_export(group_with_members, keep))
        chain = yaml.safe_dump({"S": {"tables": {"whitelist": ["A"], "custom": {"A": {"entityName": "S.A"}}},
                                      }, "chains": {"c": {"tables": ["A", "B"]}}})
        self.assertIsNone(rt.sample_export(chain, keep))


class ApplySampleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)

    def test_cuts_the_side_and_writes_the_export_to_restore(self):
        side = side_of(1000)
        note = rt.apply_sample(side, 50, self.out)
        self.assertEqual(note, f"sample of 50 of 1000 entities ({rt.SAMPLE_ENV}=50)")
        self.assertEqual(len(side["entities"]), 50)
        restored_export = yaml.safe_load(rt.strip_export_header((self.out / "sampled-export.yaml").read_text()))
        self.assertEqual(len(restored_export["TESTTAB1K"]["tables"]["whitelist"]), 50)
        self.assertEqual({e["entityName"] for e in side["entities"]},
                         {c["entityName"] for c in restored_export["TESTTAB1K"]["tables"]["custom"].values()})

    def test_no_sample_for_a_small_pipeline_or_size_zero(self):
        for size in (0, 50):
            side = side_of(10)
            self.assertIsNone(rt.apply_sample(side, size, self.out))
            self.assertEqual(len(side["entities"]), 10)
        self.assertFalse((self.out / "sampled-export.yaml").exists())

    def test_whole_pipeline_when_the_shape_cannot_be_cut(self):
        side = side_of(100, export=export_of(100, without_entity_name=("TABLE00002",)))
        self.assertIsNone(rt.apply_sample(side, 10, self.out))
        self.assertEqual(len(side["entities"]), 100)

    def test_sampled_side_compares_equal_to_a_restore_of_the_sample(self):
        side = side_of(200)
        rt.apply_sample(side, 20, self.out)
        restored = {"export": (self.out / "sampled-export.yaml").read_text(), "idMap": {}}
        side["idMap"] = {}
        self.assertTrue(rt.check_export_stable(side, restored).passed)


class SampleSizeFromEnvTests(unittest.TestCase):
    def test_reads_the_variable(self):
        with mock.patch.dict(os.environ, {rt.SAMPLE_ENV: "50"}):
            self.assertEqual(rt.sample_size_from_env(), 50)
        with mock.patch.dict(os.environ, {rt.SAMPLE_ENV: ""}):
            self.assertEqual(rt.sample_size_from_env(), 0)
        with mock.patch.dict(os.environ, {rt.SAMPLE_ENV: "-1"}):
            self.assertRaises(ValueError, rt.sample_size_from_env)


if __name__ == "__main__":
    unittest.main()
