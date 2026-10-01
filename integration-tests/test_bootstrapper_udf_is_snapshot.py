#!/usr/bin/env python3
"""UDFs and the Gluesync 2.3 ``isSnapshot`` parameter of ``onChange``.

From Gluesync 2.3 a UDF entry point is
``onChange(newValues, oldValues, operation, isSnapshot, logger)``. A 2.3 CoreHub refuses to
compile the previous signature, and a CoreHub before 2.3 cannot call the new one. These tests
lock the rewrite the bootstrapper applies before compiling (mirroring
``MappingFunctionSignature`` in gluesync-kotlin), the choice of the signature from
``GET /version``, the ``isSnapshot`` field of ``test-mapping-function``, and the UDFs this
repository generates and ships.
"""

import base64
import importlib.util
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.udf_signature import (  # noqa: E402
    UdfSignatureError,
    adapt_udf_signature,
    add_is_snapshot_parameter,
    corehub_supports_is_snapshot,
    parse_corehub_version,
    remove_is_snapshot_parameter,
)

JAVA_PREVIOUS = "Map<String, Object> newValues, Map<String, Object> oldValues, MappingFunctionOperation operation, Logger logger"
JAVA_CURRENT = (
    "Map<String, Object> newValues, Map<String, Object> oldValues, MappingFunctionOperation operation, "
    "boolean isSnapshot, Logger logger"
)
KOTLIN_PREVIOUS = (
    "newValues: Map<String, Any?>, oldValues: Map<String, Any?>, operation: MappingFunctionOperation, logger: Logger"
)
KOTLIN_CURRENT = (
    "newValues: Map<String, Any?>, oldValues: Map<String, Any?>, operation: MappingFunctionOperation, "
    "isSnapshot: Boolean, logger: Logger"
)


def java_with(member="", body="", signature=JAVA_PREVIOUS):
    return (
        "public class UDF_x {\n"
        f"    {member}\n"
        f"    public Pair<MappingFunctionOperation, Map<String, Object>> onChange({signature}) {{\n"
        f"        {body}\n"
        "        return new Pair<>(operation, newValues);\n"
        "    }\n"
        "}\n"
    )


def kotlin_with(member="", body="", constructor="", expression_body=None, signature=KOTLIN_PREVIOUS):
    on_change = f"fun onChange({signature}): Pair<MappingFunctionOperation, Map<String, Any?>?>"
    if expression_body is not None:
        implementation = f" =\n        {expression_body}"
    else:
        implementation = f" {{\n        {body}\n        return operation to newValues\n    }}"
    return f"class UDF_x{constructor} {{\n    {member}\n    {on_change}{implementation}\n}}\n"


class AddIsSnapshotParameterTests(unittest.TestCase):
    """Previous signature -> Gluesync 2.3 signature."""

    def rewritten(self, code, udf_type):
        result = add_is_snapshot_parameter(code, udf_type)
        self.assertTrue(result.changed, result.note)
        return result.code

    def test_java_declaration_gets_the_parameter_right_before_the_logger(self):
        migrated = self.rewritten(java_with(), "Java")
        self.assertIn("MappingFunctionOperation operation, boolean isSnapshot, Logger logger)", migrated)

    def test_one_parameter_per_line_stays_that_way(self):
        code = (
            "public class UDF_x {\n"
            "    public Pair<MappingFunctionOperation, Map<String, Object>> onChange(\n"
            "            Map<String, Object> newValues,\n"
            "            Map<String, Object> oldValues,\n"
            "            MappingFunctionOperation operation,\n"
            "            Logger logger) {\n"
            "        return new Pair<>(operation, newValues);\n"
            "    }\n"
            "}\n"
        )
        migrated = self.rewritten(code, "Java")
        self.assertIn(
            "MappingFunctionOperation operation,\n            boolean isSnapshot,\n            Logger logger) {", migrated
        )

    def test_kotlin_declaration_gets_the_parameter_right_before_the_logger(self):
        migrated = self.rewritten(kotlin_with(), "Kotlin")
        self.assertIn("operation: MappingFunctionOperation, isSnapshot: Boolean, logger: Logger)", migrated)

    def test_udf_type_enum_values_are_accepted(self):
        from create_user_defined_functions import UdfFunctionType

        self.assertIn("isSnapshot: Boolean", self.rewritten(kotlin_with(), UdfFunctionType.kotlin))
        self.assertIn("boolean isSnapshot", self.rewritten(java_with(), UdfFunctionType.java))

    def test_modifiers_annotations_and_a_qualified_logger_are_recognised(self):
        code = (
            "public class UDF_x {\n"
            "    public Pair<MappingFunctionOperation, Map<String, Object>> onChange(final Map<String, Object> newValues,\n"
            "            @Deprecated Map<String, Object> oldValues, final MappingFunctionOperation op, final org.slf4j.Logger log) {\n"
            "        return new Pair<>(op, newValues);\n"
            "    }\n"
            "}\n"
        )
        migrated = self.rewritten(code, "Java")
        self.assertIn("final MappingFunctionOperation op, boolean isSnapshot, final org.slf4j.Logger log)", migrated)

    def test_only_the_declaration_changes_not_comments_strings_or_calls(self):
        code = (
            "import java.util.Map;\n"
            "// a comment mentioning onChange(a, b, c, d) and \"a string\" too\n"
            "/* onChange(Map<String, Object> a, Map<String, Object> b, MappingFunctionOperation c, Logger d) */\n"
            "public class UDF_x {\n"
            f"    public Pair<MappingFunctionOperation, Map<String, Object>> onChange({JAVA_PREVIOUS}) {{\n"
            "        String s = \"onChange(Map<String, Object> a, Map<String, Object> b, MappingFunctionOperation c, Logger d)\";\n"
            "        return delegate.onChange(newValues, oldValues, operation, logger);\n"
            "    }\n"
            "}\n"
        )
        migrated = self.rewritten(code, "Java")
        self.assertEqual(code.replace("operation, Logger logger)", "operation, boolean isSnapshot, Logger logger)"), migrated)

    def test_current_signature_is_left_alone(self):
        for code, udf_type in (
            (java_with(signature=JAVA_CURRENT), "Java"),
            (kotlin_with(signature=KOTLIN_CURRENT, expression_body="operation to newValues"), "Kotlin"),
        ):
            result = add_is_snapshot_parameter(code, udf_type)
            self.assertFalse(result.changed)
            self.assertIsNone(result.note)
            self.assertEqual(code, result.code)

    def test_helper_named_onchange_ending_with_a_logger_is_not_the_entry_point(self):
        code = java_with(member="private void onChange(String a, int b, long c, Logger logger) { }")
        migrated = self.rewritten(code, "Java")
        self.assertIn("MappingFunctionOperation operation, boolean isSnapshot, Logger logger)", migrated)
        self.assertIn("onChange(String a, int b, long c, Logger logger)", migrated)

    def test_qualified_entry_point_types_are_recognised(self):
        code = kotlin_with(
            signature=(
                "newValues: kotlin.collections.Map<String, Any?>, oldValues: MutableMap<String, Any?>, "
                "operation: com.molo17.gluesync.commons.model.api.MappingFunctionOperation, logger: Logger"
            ),
            expression_body="operation to newValues",
        )
        self.assertIn("MappingFunctionOperation, isSnapshot: Boolean, logger: Logger)", self.rewritten(code, "Kotlin"))

    def test_two_previous_declarations_are_refused_not_guessed(self):
        code = java_with(
            member=(
                "public Pair<MappingFunctionOperation, Map<String, Object>> onChange(Map<String, String> newValues, "
                "Map<String, String> oldValues, MappingFunctionOperation operation, Logger logger) { return null; }"
            )
        )
        with self.assertRaises(UdfSignatureError):
            add_is_snapshot_parameter(code, "Java")

    def test_unrecognised_source_is_left_alone_with_a_note(self):
        code = "public class UDF_x { public Object apply(Map<String, Object> newValues) { return null; } }"
        result = add_is_snapshot_parameter(code, "Java")
        self.assertFalse(result.changed)
        self.assertEqual(code, result.code)
        self.assertIn("previous signature", result.note)


class ReservedNameTests(unittest.TestCase):
    """Previous-signature code with an isSnapshot of its own is not rewritten, as the CoreHub would refuse it."""

    def assert_refused(self, code, udf_type, label):
        with self.assertRaises(UdfSignatureError, msg=label) as raised:
            add_is_snapshot_parameter(code, udf_type, "UDF_x")
        self.assertIn("UDF_x", str(raised.exception))
        self.assertIn("isSnapshot", str(raised.exception))

    def assert_rewritten(self, code, udf_type, label):
        try:
            result = add_is_snapshot_parameter(code, udf_type)
        except UdfSignatureError as exc:  # pragma: no cover - failure path
            self.fail(f"{label}: {exc}")
        self.assertTrue(result.changed, label)

    def test_java_field_of_the_udf_class(self):
        for member in (
            "private boolean isSnapshot = false;",
            "private final Boolean isSnapshot;",
            "private String[] isSnapshot = null;",
            "private java.util.List<String> isSnapshot = null;",
            "private boolean a = true, isSnapshot = false;",
        ):
            self.assert_refused(java_with(member=member), "Java", member)

    def test_java_variable_in_the_body_of_onchange(self):
        for statement in (
            "boolean isSnapshot = true;",
            "final Boolean isSnapshot;",
            "try { } catch (Exception isSnapshot) { }",
            "for (Boolean isSnapshot : java.util.List.of(true)) { }",
            "java.util.function.Predicate<Boolean> p = isSnapshot -> isSnapshot;",
            "java.util.function.BiFunction<Integer, Integer, Integer> p = (a, isSnapshot) -> a;",
            "if (oldValues instanceof java.util.HashMap isSnapshot) { }",
            # Reads an isSnapshot declared elsewhere, which the parameter would silently replace.
            "newValues.put(\"flag\", isSnapshot);",
        ):
            self.assert_refused(java_with(body=statement), "Java", statement)

    def test_java_parameters_and_locals_of_other_methods_are_fine(self):
        for member in (
            "private Map<String, Object> tag(Map<String, Object> values, boolean isSnapshot) { return values; }",
            "void f(boolean isSnapshot) { }",
            "void f() { boolean isSnapshot = true; }",
            "void f() { java.util.function.Predicate<Boolean> p = isSnapshot -> isSnapshot; }",
            "public UDF_x(boolean isSnapshot) { }",
            "static class Nested { boolean isSnapshot; }",
            "private java.util.function.Predicate<Boolean> p = isSnapshot -> isSnapshot;",
            "void f() { boolean b = Helper.isSnapshot(); }",
            "void f() { boolean b = helper.isSnapshot; }",
            "static boolean isSnapshot() { return true; }",
            "void f() { java.util.function.Supplier<Boolean> s = Helper::isSnapshot; }",
            "// boolean isSnapshot = true;",
            "void f() { String s = \"boolean isSnapshot = true;\"; }",
        ):
            self.assert_rewritten(java_with(member=member), "Java", member)

    def test_java_calls_and_member_access_in_onchange_are_fine(self):
        for statement in (
            "boolean b = Helper.isSnapshot();",
            "boolean b = helper.isSnapshot;",
            "boolean b = isSnapshot();",
            "// isSnapshot",
            "String s = \"isSnapshot\";",
        ):
            self.assert_rewritten(java_with(body=statement), "Java", statement)

    def test_kotlin_property_of_the_udf_class(self):
        for member in (
            "private val isSnapshot = false",
            "var isSnapshot: Boolean = true",
            "companion object { val isSnapshot = true }",
            "companion object Defaults { const val isSnapshot = true }",
        ):
            self.assert_refused(kotlin_with(member=member), "Kotlin", member)
        self.assert_refused(kotlin_with(constructor="(val isSnapshot: Boolean)"), "Kotlin", "constructor val")
        self.assert_refused(kotlin_with(constructor="(private var isSnapshot: Boolean = false)"), "Kotlin", "ctor var")
        self.assert_refused("val isSnapshot = true\n\n" + kotlin_with(), "Kotlin", "top-level")

    def test_kotlin_variable_in_the_body_of_onchange(self):
        for statement in (
            "val isSnapshot = true",
            "var isSnapshot: Boolean = true",
            "try { } catch (isSnapshot: Exception) { }",
            "for (isSnapshot in listOf(true)) { }",
            "for ((key, isSnapshot) in mapOf(1 to true)) { }",
            "val (a, isSnapshot) = 1 to true",
            "listOf(true).map { isSnapshot -> isSnapshot }",
            "mapOf(1 to true).forEach { key, isSnapshot -> }",
        ):
            self.assert_refused(kotlin_with(body=statement), "Kotlin", statement)
        self.assert_refused(
            kotlin_with(expression_body="newValues.let { isSnapshot -> operation to it }"), "Kotlin", "expression body"
        )

    def test_kotlin_parameters_and_locals_of_other_functions_are_fine(self):
        for member in (
            "fun f(isSnapshot: Boolean) { }",
            "private fun tag(values: Map<String, Any?>, isSnapshot: Boolean) = values + (\"snapshot\" to isSnapshot)",
            "fun f() { val isSnapshot = true }",
            "class Nested { val isSnapshot = true }",
            "fun f() = listOf(true).map { isSnapshot -> isSnapshot }",
            "fun f() { val b = Helper.isSnapshot() }",
            "fun f() { val b = helper?.isSnapshot }",
            "fun isSnapshot(): Boolean = true",
            "fun g(isSnapshotFlag: Boolean) { }",
        ):
            self.assert_rewritten(kotlin_with(member=member), "Kotlin", member)
        self.assert_rewritten(kotlin_with(constructor="(isSnapshot: Boolean)"), "Kotlin", "plain constructor param")

    def test_kotlin_named_argument_in_onchange_is_fine(self):
        self.assert_rewritten(kotlin_with(body="val tagged = tag(newValues, isSnapshot = true)"), "Kotlin", "named arg")


class RemoveIsSnapshotParameterTests(unittest.TestCase):
    """Gluesync 2.3 signature -> previous signature, for a CoreHub before 2.3."""

    def test_java_parameter_is_removed(self):
        code = java_with(signature=JAVA_CURRENT)
        result = remove_is_snapshot_parameter(code, "Java")
        self.assertTrue(result.changed)
        self.assertEqual(java_with(), result.code)

    def test_kotlin_parameter_is_removed(self):
        code = kotlin_with(signature=KOTLIN_CURRENT)
        result = remove_is_snapshot_parameter(code, "Kotlin")
        self.assertTrue(result.changed)
        self.assertEqual(kotlin_with(), result.code)

    def test_previous_signature_is_left_alone(self):
        code = java_with()
        result = remove_is_snapshot_parameter(code, "Java")
        self.assertFalse(result.changed)
        self.assertEqual(code, result.code)

    def test_code_that_reads_is_snapshot_cannot_run_on_an_older_corehub(self):
        for code, udf_type in (
            (java_with(signature=JAVA_CURRENT, body="if (isSnapshot) { newValues.put(\"o\", \"s\"); }"), "Java"),
            (kotlin_with(signature=KOTLIN_CURRENT, body="val origin = if (isSnapshot) \"s\" else \"c\""), "Kotlin"),
        ):
            with self.assertRaises(UdfSignatureError) as raised:
                remove_is_snapshot_parameter(code, udf_type, "UDF_x")
            self.assertIn("2.3", str(raised.exception))

    def test_round_trip_restores_the_original(self):
        for code, udf_type in ((java_with(), "Java"), (kotlin_with(), "Kotlin")):
            added = adapt_udf_signature(code, udf_type, supports_is_snapshot=True)
            removed = adapt_udf_signature(added.code, udf_type, supports_is_snapshot=False)
            self.assertEqual(code, removed.code)


class CoreHubVersionTests(unittest.TestCase):
    def test_versions_are_parsed(self):
        self.assertEqual((2, 3, 0), parse_corehub_version("2.3.0.0 - build 7a328c2"))
        self.assertEqual((2, 2, 6), parse_corehub_version('"2.2.6.0"'))
        self.assertEqual((2, 3, 0), parse_corehub_version("2.3.0-SNAPSHOT"))
        self.assertEqual((2, 3, 1), parse_corehub_version({"version": "2.3.1.0"}))
        self.assertIsNone(parse_corehub_version("NA"))
        self.assertIsNone(parse_corehub_version(""))
        self.assertIsNone(parse_corehub_version(None))

    def test_signature_is_chosen_by_release(self):
        self.assertTrue(corehub_supports_is_snapshot("2.3.0.0 - build 7a328c2"))
        self.assertTrue(corehub_supports_is_snapshot("2.4.0.0"))
        self.assertTrue(corehub_supports_is_snapshot("3.0.0"))
        self.assertFalse(corehub_supports_is_snapshot("2.2.6.0"))
        self.assertFalse(corehub_supports_is_snapshot("2.2.5.5"))
        self.assertFalse(corehub_supports_is_snapshot("1.9.9"))
        # Unreadable: assume a current CoreHub.
        self.assertTrue(corehub_supports_is_snapshot("NA"))
        self.assertTrue(corehub_supports_is_snapshot(None))


class FakeCoreHub:
    """Answers /version and records compile/test calls made through commons.fetch_core_hub."""

    def __init__(self, version):
        self.version = version
        self.calls = []

    def __call__(self, path, method="GET", token=None, body=None, **kwargs):
        self.calls.append((path, method, body))
        if path == "/version":
            if isinstance(self.version, Exception):
                raise self.version
            return self.version
        return {}

    def bodies(self, suffix):
        return [body for path, method, body in self.calls if path.endswith(suffix) and method == "POST"]


class CompileAndTestRequestTests(unittest.TestCase):
    def setUp(self):
        import create_user_defined_functions as udf

        self.udf = udf
        udf._IS_SNAPSHOT_SUPPORT_BY_COREHUB.clear()
        self.addCleanup(udf._IS_SNAPSHOT_SUPPORT_BY_COREHUB.clear)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(udf, "UDF_PATH", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_udf(self, name, code, ext=".java"):
        (Path(self.tmp.name) / f"{name}{ext}").write_text(code, encoding="utf-8")

    def compile(self, version, name="UDF_x", udf_type="Java"):
        corehub = FakeCoreHub(version)
        with mock.patch.object(self.udf, "fetch_core_hub", side_effect=corehub):
            self.udf.check_and_compile_udf_function("T", {"name": name, "type": udf_type}, "pipe", "tok")
        return corehub

    def compiled_code(self, corehub):
        bodies = corehub.bodies("/compile-mapping-function")
        self.assertEqual(1, len(bodies))
        return base64.b64decode(bodies[0]["code"]).decode("utf-8")

    def test_previous_signature_file_is_rewritten_for_a_2_3_corehub(self):
        self.write_udf("UDF_x", java_with())
        code = self.compiled_code(self.compile("2.3.0.0 - build 7a328c2"))
        self.assertIn("MappingFunctionOperation operation, boolean isSnapshot, Logger logger)", code)

    def test_kotlin_file_is_rewritten_for_a_2_3_corehub(self):
        self.write_udf("UDF_k", kotlin_with(), ext=".kt")
        code = self.compiled_code(self.compile("2.3.0.0", name="UDF_k", udf_type="Kotlin"))
        self.assertIn("isSnapshot: Boolean, logger: Logger)", code)

    def test_current_signature_file_is_downgraded_for_an_older_corehub(self):
        self.write_udf("UDF_x", java_with(signature=JAVA_CURRENT))
        code = self.compiled_code(self.compile("2.2.6.0"))
        self.assertNotIn("isSnapshot", code)
        self.assertEqual(java_with(), code)

    def test_previous_signature_file_is_sent_unchanged_to_an_older_corehub(self):
        self.write_udf("UDF_x", java_with())
        self.assertEqual(java_with(), self.compiled_code(self.compile("2.2.5.5")))

    def test_unreadable_version_assumes_the_current_signature(self):
        import requests

        self.write_udf("UDF_x", java_with())
        code = self.compiled_code(self.compile(requests.exceptions.ConnectionError("down")))
        self.assertIn("boolean isSnapshot", code)

    def test_clashing_variable_fails_before_anything_is_compiled(self):
        self.write_udf("UDF_x", java_with(member="private boolean isSnapshot = false;"))
        corehub = FakeCoreHub("2.3.0.0")
        with mock.patch.object(self.udf, "fetch_core_hub", side_effect=corehub):
            with self.assertRaises(UdfSignatureError):
                self.udf.check_and_compile_udf_function("T", {"name": "UDF_x", "type": "Java"}, "pipe", "tok")
        self.assertEqual([], corehub.bodies("/compile-mapping-function"))

    def test_version_is_read_once_per_corehub(self):
        self.write_udf("UDF_a", java_with())
        self.write_udf("UDF_b", java_with())
        corehub = FakeCoreHub("2.3.0.0")
        with mock.patch.object(self.udf, "fetch_core_hub", side_effect=corehub):
            for name in ("UDF_a", "UDF_b"):
                self.udf.check_and_compile_udf_function("T", {"name": name, "type": "Java"}, "pipe", "tok")
        self.assertEqual(1, sum(1 for path, _, _ in corehub.calls if path == "/version"))
        self.assertEqual(2, len(corehub.bodies("/compile-mapping-function")))

    def make_test_request(self, **overrides):
        fields = dict(
            data={"ID": 1},
            type="Java",
            udfName="UDF_x",
            targetTable={"id": 1, "name": "T"},
        )
        fields.update(overrides)
        return self.udf.UdfFunctionTestRequest(**fields)

    def run_test_request(self, version, request):
        corehub = FakeCoreHub(version)
        with mock.patch.object(self.udf, "fetch_core_hub", side_effect=corehub):
            self.udf.test_udf_function("pipe", "tok", request)
        bodies = corehub.bodies("/test-mapping-function")
        self.assertEqual(1, len(bodies))
        return bodies[0]

    def test_test_request_defaults_to_a_cdc_row(self):
        body = self.run_test_request("2.3.0.0", self.make_test_request())
        self.assertIs(False, body["isSnapshot"])
        self.assertEqual("Java", body["type"])
        self.assertEqual("Insert", body["operation"])

    def test_test_request_can_send_a_snapshot_row(self):
        body = self.run_test_request("2.3.0.0", self.make_test_request(isSnapshot=True))
        self.assertIs(True, body["isSnapshot"])

    def test_test_request_omits_is_snapshot_for_an_older_corehub(self):
        body = self.run_test_request("2.2.6.0", self.make_test_request(isSnapshot=True))
        self.assertNotIn("isSnapshot", body)


class GeneratedAndShippedUdfTests(unittest.TestCase):
    """UDFs this repository generates or ships declare the Gluesync 2.3 signature."""

    @classmethod
    def setUpClass(cls):
        converter_dir = PROJECT_ROOT / "dbmoto-converter"
        sys.path.insert(0, str(converter_dir))
        try:
            spec = importlib.util.spec_from_file_location(
                "generate_udf_import_package", converter_dir / "generate_udf_import_package.py"
            )
            cls.generator = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.generator)
        finally:
            sys.path.remove(str(converter_dir))

    def build(self, **kwargs):
        mappings = [{"target": "NAME", "expression": "Trim([NAME])", "java": "newValues.get(\"NAME\")"}]
        return self.generator.build_java_udf("UDF_S_T", "S", "T", "1", mappings, ["NAME"], ["NAME"], **kwargs)

    def test_generator_writes_the_current_signature(self):
        source = self.build()
        self.assertIn("MappingFunctionOperation operation, boolean isSnapshot, Logger logger)", source)
        self.assertFalse(add_is_snapshot_parameter(source, "Java").changed)

    def test_generator_can_write_the_previous_signature(self):
        source = self.build(is_snapshot_signature=False)
        self.assertIn("MappingFunctionOperation operation, Logger logger)", source)
        self.assertNotIn("isSnapshot", source)

    def test_shipped_samples_declare_the_current_signature(self):
        samples = sorted((PROJECT_ROOT / "udf_import_package").rglob("*.java"))
        samples.append(PROJECT_ROOT / "FHCUSMST UDF.txt")
        self.assertGreater(len(samples), 1)
        for sample in samples:
            code = sample.read_text(encoding="utf-8")
            result = add_is_snapshot_parameter(code, "Java", sample.name)
            self.assertFalse(result.changed, f"{sample} is still on the previous onChange signature")
            self.assertIsNone(result.note, f"{sample}: {result.note}")


if __name__ == "__main__":
    unittest.main()
