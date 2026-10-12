#!/usr/bin/env python3
"""The key rules of a Gluesync 2.3 entity, applied before the request (T7 of the alignment plan).

CoreHub 2.3 refuses an entity whose key flags break the rules B1-B7
(gluesync-kotlin ``GluesyncMainRoutesOrchestrator.kt`` ``entityKeyIssues``); the field editor and
the MCP draft (``corehub-mcp/.../schema/EntityDraft.kt`` ``keyLinks``, ``k2bKeys``, ``resolveKeys``)
derive keys that pass them. ``derive_keys`` and ``drop_forbidden_key_links`` do the same for a YAML
table, so a valid YAML gives a payload CoreHub accepts and an invalid one is refused naming the rule.
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from entity_mapping import EntityMappingError, derive_keys, drop_forbidden_key_links  # noqa: E402


def rel(sources, targets, explicit=True):
    return {"sources": list(sources), "targets": list(targets), "explicit": explicit}


def keys(relations, *, ph_s=(), ph_t=(), yaml_keys=(), yaml_target_keys=(), sources=None, targets=None,
         produced=(), source_is_nosql=False):
    all_sources = sources or sorted({s for r in relations for s in r["sources"]}.union(ph_s, yaml_keys))
    all_targets = targets or sorted({t for r in relations for t in r["targets"]}.union(ph_t))
    return derive_keys(
        relations=relations,
        source_names=all_sources,
        target_names=all_targets,
        physical_source=set(ph_s),
        physical_target=set(ph_t),
        yaml_keys=list(yaml_keys),
        yaml_target_keys=list(yaml_target_keys),
        produced_targets=set(produced),
        label="src.T",
        source_is_nosql=source_is_nosql,
    )


class SourceKeyTests(unittest.TestCase):
    def test_b1_source_primary_key_is_the_key(self):
        self.assertEqual(keys([rel(["ID"], ["id"]), rel(["NAME"], ["name"])], ph_s={"ID"}), (["ID"], ["id"]))

    def test_b1_yaml_keys_equal_to_the_primary_key_are_accepted_in_any_case(self):
        self.assertEqual(keys([rel(["ID"], ["id"])], ph_s={"ID"}, yaml_keys=["id"])[0], ["ID"])

    def test_b1_yaml_keys_that_differ_from_the_primary_key_are_refused(self):
        with self.assertRaisesRegex(EntityMappingError, r"R12\.1.*src\.T.*CODE.*ID"):
            keys([rel(["ID"], ["id"]), rel(["CODE"], ["code"])], ph_s={"ID"}, yaml_keys=["ID", "CODE"])

    def test_k2b_source_without_key_takes_the_sources_of_the_target_key(self):
        # Worked case 3 of R12: the source has no key, the target is keyed on order_id.
        self.assertEqual(
            keys([rel(["ORDER_NO"], ["order_id"]), rel(["NOTE"], ["note"])], ph_t={"order_id"}),
            (["ORDER_NO"], ["order_id"]),
        )

    def test_b6_b7_yaml_keys_must_be_the_k2b_keys(self):
        with self.assertRaisesRegex(EntityMappingError, "K9.*NOTE"):
            keys([rel(["ORDER_NO"], ["order_id"]), rel(["NOTE"], ["note"])], ph_t={"order_id"}, yaml_keys=["ORDER_NO", "NOTE"])
        with self.assertRaisesRegex(EntityMappingError, "K2b.*ORDER_NO"):
            keys([rel(["ORDER_NO"], ["order_id"]), rel(["NOTE"], ["note"])], ph_t={"order_id"}, yaml_keys=["NOTE"])

    def test_yaml_keys_on_tables_without_physical_keys(self):
        self.assertEqual(keys([rel(["CODE"], ["code"]), rel(["DESCR"], ["descr"])], yaml_keys=["CODE"]), (["CODE"], ["code"]))

    def test_no_key_at_all_is_refused(self):
        with self.assertRaisesRegex(EntityMappingError, "no key"):
            keys([rel(["A"], ["a"])])

    def test_mongodb_source_without_keys_names_id(self):
        with self.assertRaisesRegex(EntityMappingError, "_id"):
            keys([rel(["_id"], ["_id"]), rel(["name"], ["name"])], source_is_nosql=True)

    def test_unknown_yaml_key_is_refused(self):
        with self.assertRaisesRegex(EntityMappingError, "MISSING"):
            keys([rel(["A"], ["a"])], yaml_keys=["MISSING"], sources=["A"])


class TargetKeyTests(unittest.TestCase):
    def test_b2_target_primary_key_stays_the_target_key(self):
        # Worked case 2 of R12: both keyed, the source key feeds the target key.
        self.assertEqual(keys([rel(["ID"], ["id"]), rel(["NAME"], ["name"])], ph_s={"ID"}, ph_t={"id"}), (["ID"], ["id"]))

    def test_b4_target_without_key_inherits_the_targets_of_the_source_key(self):
        self.assertEqual(keys([rel(["ID"], ["a", "b"]), rel(["NAME"], ["name"])], ph_s={"ID"})[1], ["a", "b"])

    def test_b7_target_keys_next_to_inherited_ones_are_refused(self):
        with self.assertRaisesRegex(EntityMappingError, "K9.*name"):
            keys([rel(["ID"], ["id"]), rel(["NAME"], ["name"])], ph_s={"ID"}, yaml_target_keys=["name"])

    def test_b5_target_keys_of_a_single_non_key_source_are_refused(self):
        with self.assertRaisesRegex(EntityMappingError, "DESCR"):
            keys([rel(["CODE"], ["code"]), rel(["DESCR"], ["descr"])], yaml_keys=["CODE"], yaml_target_keys=["descr"])

    def test_b5_target_keys_of_an_n_to_one_or_of_a_produced_column_are_accepted(self):
        result = keys(
            [rel(["CODE"], ["code"]), rel(["FIRST", "LAST"], ["full"]), rel([], ["row_id"])],
            yaml_keys=["CODE"], yaml_target_keys=["full", "row_id"], produced={"full", "row_id"},
        )
        self.assertEqual(result[1], ["code", "full", "row_id"])

    def test_b2_target_keys_that_differ_from_its_primary_key_are_refused(self):
        with self.assertRaisesRegex(EntityMappingError, r"R12\.2"):
            keys([rel(["ID"], ["id"]), rel(["NAME"], ["name"])], ph_s={"ID"}, ph_t={"id"}, yaml_target_keys=["name"])

    def test_a_target_with_no_key_is_refused(self):
        # CoreHub refuses an agent entity without keys (checkEntity), the target one included.
        with self.assertRaisesRegex(EntityMappingError, "target table of src.T has no key"):
            keys([rel(["ID"], []), rel(["NAME"], ["name"])], yaml_keys=["ID"])

    def test_bop13_target_primary_key_no_source_feeds_is_refused_with_the_expression_hint(self):
        with self.assertRaisesRegex(EntityMappingError, "ROW_ID.*expression"):
            keys([rel(["ORDER_ID"], ["ORDER_ID"])], ph_s={"ORDER_ID"}, ph_t={"ROW_ID"}, targets=["ORDER_ID"])

    def test_bop13_target_primary_key_produced_by_a_function_is_accepted(self):
        result = keys([rel(["ORDER_ID"], ["ORDER_ID"]), rel([], ["ROW_ID"])], ph_s={"ORDER_ID"}, ph_t={"ROW_ID"},
                      produced={"ROW_ID"})
        self.assertEqual(result[1], ["ROW_ID"])


class KeyLinkTests(unittest.TestCase):
    def test_r12_8_implicit_link_is_dropped_with_a_warning(self):
        relations, dropped, warnings = drop_forbidden_key_links(
            [rel(["ID"], ["ID"], explicit=False), rel(["NAME"], ["NAME"], explicit=False)],
            physical_source={"ID"}, physical_target={"ROW_ID"}, label="src.T",
        )
        self.assertEqual([(r["sources"], r["targets"]) for r in relations], [(["ID"], []), (["NAME"], ["NAME"])])
        self.assertEqual(dropped, ["ID"])
        self.assertTrue(any("R12.8" in w and "ID" in w for w in warnings))

    def test_r12_8_explicit_link_is_refused(self):
        with self.assertRaisesRegex(EntityMappingError, r"R12\.8.*ID.*ROW_ID"):
            drop_forbidden_key_links([rel(["ID"], ["ID"])], physical_source={"ID"}, physical_target={"ROW_ID"}, label="src.T")

    def test_links_onto_the_target_key_are_kept(self):
        relations, dropped, warnings = drop_forbidden_key_links(
            [rel(["ID"], ["ROW_ID"])], physical_source={"ID"}, physical_target={"ROW_ID"}, label="src.T",
        )
        self.assertEqual((relations[0]["targets"], dropped, warnings), (["ROW_ID"], [], []))

    def test_nothing_is_dropped_on_a_target_without_primary_key(self):
        relations, dropped, _ = drop_forbidden_key_links([rel(["ID"], ["x"], explicit=False)], physical_source={"ID"},
                                                         physical_target=set(), label="src.T")
        self.assertEqual((relations[0]["targets"], dropped), (["x"], []))


if __name__ == "__main__":
    unittest.main()
