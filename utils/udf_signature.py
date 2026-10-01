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

"""The ``onChange`` signature of Gluesync UDFs, and the rewrite between its two versions.

From Gluesync 2.3 the entry point of a JVM UDF is

    onChange(newValues, oldValues, operation, isSnapshot, logger)

where ``isSnapshot`` is true for a row read by the snapshot and false for a CDC row. A
2.3 CoreHub refuses to compile a UDF still written for the previous signature
``onChange(newValues, oldValues, operation, logger)``, and a CoreHub before 2.3 cannot
call one written for the new signature.

UDF sources handed to the bootstrapper (``UDF_PATH``, import packages, pipeline
duplication) are therefore brought to the signature the target CoreHub expects before
they are compiled. Only the ``onChange`` declaration is rewritten, never a call or a
comment: the source is read with comments and string literals blanked out. The
detection mirrors ``MappingFunctionSignature`` in gluesync-kotlin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

METHOD_NAME = "onChange"
IS_SNAPSHOT_PARAMETER = "isSnapshot"

# First CoreHub release whose UDFs receive isSnapshot.
IS_SNAPSHOT_MIN_COREHUB_VERSION = (2, 3)

JAVA = "Java"
KOTLIN = "Kotlin"

EXPECTED_DECLARATION = {
    JAVA: (
        "public Pair<MappingFunctionOperation, Map<String, Object>> onChange(Map<String, Object> newValues, "
        "Map<String, Object> oldValues, MappingFunctionOperation operation, boolean isSnapshot, Logger logger)"
    ),
    KOTLIN: (
        "fun onChange(newValues: Map<String, Any?>, oldValues: Map<String, Any?>, "
        "operation: MappingFunctionOperation, isSnapshot: Boolean, logger: Logger): "
        "Pair<MappingFunctionOperation, Map<String, Any?>?>"
    ),
}

PREVIOUS_DECLARATION = {
    JAVA: (
        "public Pair<MappingFunctionOperation, Map<String, Object>> onChange(Map<String, Object> newValues, "
        "Map<String, Object> oldValues, MappingFunctionOperation operation, Logger logger)"
    ),
    KOTLIN: (
        "fun onChange(newValues: Map<String, Any?>, oldValues: Map<String, Any?>, "
        "operation: MappingFunctionOperation, logger: Logger): "
        "Pair<MappingFunctionOperation, Map<String, Any?>?>"
    ),
}

_METHOD_OPENING = re.compile(r"\b" + METHOD_NAME + r"\s*\(")
_RESERVED_NAME = re.compile(r"\b" + IS_SNAPSHOT_PARAMETER + r"\b")
_ANNOTATION = re.compile(r"@[\w.$]+(?:\s*\([^)]*\))?")
_FINAL_MODIFIER = re.compile(r"\bfinal\b")
_TRAILING_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*$")
_LOGGER_TYPE = re.compile(r"(?:[A-Za-z_$][\w$]*\.)*Logger\??")
_MAP_TYPE = re.compile(r"(?:java\.util\.|kotlin\.collections\.)?(?:Mutable)?Map")
_OPERATION_TYPE = re.compile(r"(?:[A-Za-z_$][\w$]*\.)*MappingFunctionOperation")
_COMPANION_OBJECT = re.compile(r"\bcompanion\s+object(?:\s+[\w$]+)?\s*$")
_CLASS_KEYWORD = re.compile(r"\bclass\b")
_KOTLIN_MEMBER_START = re.compile(
    r"\n\s*(?:(?:private|public|internal|protected|override|open|final|inline|suspend|abstract|@[\w.]+)\s+)*"
    r"(?:fun|val|var|class|object|init|companion|constructor)\b"
)
_COREHUB_VERSION = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)(?:$|[.\s+-].*)", re.DOTALL)

# Words that can stand right before a name without declaring it: ``return isSnapshot;``.
_EXPRESSION_KEYWORDS = {
    "return", "throw", "case", "else", "yield", "assert", "do", "new", "instanceof", "extends",
    "implements", "super", "this", "default", "import", "package", "break", "continue", "throws",
}


class UdfSignatureError(ValueError):
    """A UDF source that cannot be brought to the signature the CoreHub expects."""


@dataclass(frozen=True)
class SignatureAdaptation:
    """The source to compile, and what was done to it."""

    code: str
    changed: bool
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# CoreHub version
# ---------------------------------------------------------------------------

def parse_corehub_version(raw) -> Optional[Tuple[int, int, int]]:
    """``(major, minor, patch)`` from what ``GET /version`` answers, or None.

    CoreHub answers a bare string such as ``2.3.0.0 - build 7a328c2`` (possibly JSON
    quoted), or ``NA`` when the build carries no version.
    """
    if isinstance(raw, dict):
        raw = raw.get("version") or raw.get("appVersion")
    if not isinstance(raw, str):
        return None
    match = _COREHUB_VERSION.match(raw.strip().strip('"'))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def corehub_supports_is_snapshot(raw_version) -> bool:
    """Whether a CoreHub reporting ``raw_version`` expects the signature with ``isSnapshot``.

    A version that cannot be read is taken as a current CoreHub.
    """
    parsed = parse_corehub_version(raw_version)
    if parsed is None:
        return True
    return parsed[:2] >= IS_SNAPSHOT_MIN_COREHUB_VERSION


# ---------------------------------------------------------------------------
# Source scanning
# ---------------------------------------------------------------------------

def _language(udf_type) -> Optional[str]:
    value = getattr(udf_type, "value", udf_type)
    if not isinstance(value, str):
        return None
    lowered = value.lower()
    if lowered == "java":
        return JAVA
    if lowered == "kotlin":
        return KOTLIN
    return None


def _skip_block_comment(code: str, i: int, length: int) -> int:
    depth = 0
    j = i
    while j < length:
        if code.startswith("/*", j):
            depth += 1
            j += 2
        elif code.startswith("*/", j):
            depth -= 1
            j += 2
            if depth == 0:
                break
        else:
            j += 1
    return j


def _skip_triple_quote(code: str, i: int, length: int) -> int:
    end = code.find('"""', i + 3)
    end = length if end == -1 else end + 3
    while end < length and code[end] == '"':
        end += 1
    return end


def _skip_quoted_literal(code: str, i: int, length: int, ch: str) -> int:
    j = i + 1
    while j < length and code[j] != ch and code[j] != "\n":
        j += 2 if code[j] == "\\" else 1
    return min(j + 1, length)


def blank_comments_and_literals(code: str) -> str:
    """``code`` with comments, string and char literals replaced by spaces (newlines kept).

    Offsets stay the same, so a position found in the blanked copy is valid in ``code``.
    Kotlin's nested block comments and raw strings are handled, as are Java text blocks.
    """
    out = list(code)
    length = len(code)
    i = 0

    def blank(start: int, end: int) -> None:
        for k in range(start, min(end, length)):
            if out[k] != "\n":
                out[k] = " "

    while i < length:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < length else ""
        if ch == "/" and nxt == "/":
            end = code.find("\n", i)
            end = length if end == -1 else end
            blank(i, end)
            i = end
        elif ch == "/" and nxt == "*":
            j = _skip_block_comment(code, i, length)
            blank(i, j)
            i = j
        elif code.startswith('"""', i):
            end = _skip_triple_quote(code, i, length)
            blank(i, end)
            i = end
        elif ch in ('"', "'"):
            end = _skip_quoted_literal(code, i, length, ch)
            blank(i, end)
            i = end
        else:
            i += 1
    return "".join(out)


@dataclass(frozen=True)
class _Parameter:
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _Declaration:
    open: int
    close: int
    parameters: List[_Parameter]


def _matching(text: str, open_index: int, opening: str, closing: str) -> Optional[int]:
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def _parameters_between(text: str, open_index: int, close_index: int) -> List[_Parameter]:
    """The parameters of a list, split on top-level commas: ``Map<String, Object>`` is one."""
    parameters: List[_Parameter] = []
    depth = 0
    segment_start = open_index + 1

    def close_segment(end: int) -> None:
        segment = text[segment_start:end]
        if segment.strip():
            leading = len(segment) - len(segment.lstrip())
            trailing = len(segment) - len(segment.rstrip())
            parameters.append(_Parameter(segment_start + leading, end - trailing, segment.strip()))

    for index in range(open_index + 1, close_index):
        ch = text[index]
        if ch in "<([":
            depth += 1
        elif ch == ">":
            if index == 0 or text[index - 1] != "-":  # `->` in a Kotlin function type
                depth -= 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            close_segment(index)
            segment_start = index + 1
    close_segment(close_index)
    return parameters


def _type_of(parameter: str, language: str) -> Optional[str]:
    if language == KOTLIN:
        if ":" not in parameter:
            return None
        return parameter.split(":", 1)[1].strip() or None
    without_modifiers = _FINAL_MODIFIER.sub(" ", _ANNOTATION.sub(" ", parameter)).strip()
    name = _TRAILING_IDENTIFIER.search(without_modifiers)
    if not name:
        return None
    return without_modifiers[: name.start()].strip() or None


def _raw_type(type_text: str) -> str:
    compact = re.sub(r"\s", "", type_text)
    if compact.endswith("?"):
        compact = compact[:-1]
    return compact.split("<", 1)[0]


def _is_boolean(type_text: str, language: str) -> bool:
    if language == KOTLIN:
        return type_text in ("Boolean", "kotlin.Boolean")
    return type_text == "boolean"


def _types(declaration: _Declaration, language: str) -> Optional[List[str]]:
    types = []
    for parameter in declaration.parameters:
        type_text = _type_of(parameter.text, language)
        if type_text is None:
            return None
        types.append(type_text)
    return types


def _has_entry_point_head(types: List[str]) -> bool:
    return (
        _MAP_TYPE.fullmatch(_raw_type(types[0])) is not None
        and _MAP_TYPE.fullmatch(_raw_type(types[1])) is not None
        and _OPERATION_TYPE.fullmatch(_raw_type(types[2])) is not None
    )


def _is_logger(type_text: str) -> bool:
    return _LOGGER_TYPE.fullmatch(re.sub(r"\s", "", type_text)) is not None


def _is_previous(declaration: _Declaration, language: str) -> bool:
    types = _types(declaration, language)
    return types is not None and len(types) == 4 and _has_entry_point_head(types) and _is_logger(types[3])


def _is_current(declaration: _Declaration, language: str) -> bool:
    types = _types(declaration, language)
    return (
        types is not None
        and len(types) == 5
        and _has_entry_point_head(types)
        and _is_boolean(types[3], language)
        and _is_logger(types[4])
    )


def _declarations(stripped: str, language: str) -> List[_Declaration]:
    declarations = []
    for opening in _METHOD_OPENING.finditer(stripped):
        open_index = opening.end() - 1
        close_index = _matching(stripped, open_index, "(", ")")
        if close_index is None:
            continue
        declaration = _Declaration(open_index, close_index, _parameters_between(stripped, open_index, close_index))
        # A call passes bare names; every parameter of a declaration carries a type.
        if all(_type_of(p.text, language) is not None for p in declaration.parameters):
            declarations.append(declaration)
    return declarations


def _body_of(stripped: str, declaration: _Declaration, language: str) -> Optional[Tuple[int, int]]:
    """The body of a declaration: its braces, or a Kotlin expression body up to the next member."""
    index = declaration.close + 1
    depth = 0
    while index < len(stripped):
        ch = stripped[index]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "{" and depth == 0:
            close = _matching(stripped, index, "{", "}")
            return index, (len(stripped) - 1 if close is None else close)
        elif ch in ";}" and depth == 0:
            return None
        elif ch == "=" and depth == 0 and language == KOTLIN and stripped[index + 1: index + 2] != "=":
            return index, _kotlin_expression_body_end(stripped, index)
        index += 1
    return None


def _kotlin_expression_body_end(stripped: str, equals: int) -> int:
    depth = 0
    index = equals + 1
    while index < len(stripped):
        ch = stripped[index]
        if ch in "{(":
            depth += 1
        elif ch in "})":
            depth -= 1
            if depth < 0:
                return index - 1
        elif ch == "\n" and depth == 0 and _KOTLIN_MEMBER_START.match(stripped, index):
            return index - 1
        index += 1
    return len(stripped) - 1


def _enclosing_block(stripped: str, position: int) -> Optional[Tuple[int, int]]:
    depth = 0
    index = position - 1
    while index >= 0:
        ch = stripped[index]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth == 0:
                close = _matching(stripped, index, "{", "}")
                return index, (len(stripped) - 1 if close is None else close)
            depth -= 1
        index -= 1
    return None


def _directly_inside(stripped: str, start: int, position: int) -> bool:
    """Nothing opened after ``start`` is still open at ``position``: no brace, no parenthesis."""
    braces = parentheses = 0
    for ch in stripped[start + 1: position]:
        if ch == "{":
            braces += 1
        elif ch == "}":
            braces -= 1
        elif ch == "(":
            parentheses += 1
        elif ch == ")":
            parentheses -= 1
    return braces == 0 and parentheses == 0


def _previous_non_blank(stripped: str, before: int) -> int:
    while before >= 0 and stripped[before].isspace():
        before -= 1
    return before


def _next_non_blank(stripped: str, start: int) -> int:
    while start < len(stripped) and stripped[start].isspace():
        start += 1
    return start


def _is_identifier_part(ch: str) -> bool:
    return ch.isalnum() or ch in "_$"


def _word_ending_at(stripped: str, end: int) -> str:
    start = end
    while start > 0 and _is_identifier_part(stripped[start - 1]):
        start -= 1
    return stripped[start: end + 1]


def _is_bare_name(stripped: str, start: int, end: int, language: str) -> bool:
    """``isSnapshot`` used as a variable: not ``x.isSnapshot``, ``X::isSnapshot``, ``isSnapshot()``,
    nor a Kotlin named argument ``helper(isSnapshot = true)``."""
    following = _next_non_blank(stripped, end)
    if stripped[following: following + 1] == "(":
        return False
    previous = _previous_non_blank(stripped, start - 1)
    previous_char = stripped[previous] if previous >= 0 else ""
    if previous_char == ".":
        return False
    if previous >= 1 and stripped[previous - 1: previous + 1] == "::":
        return False
    is_assignment = stripped[following: following + 1] == "=" and stripped[following + 1: following + 2] != "="
    return not (language == KOTLIN and previous_char in "(," and previous_char and is_assignment)


def _is_member_declaration(stripped: str, start: int, end: int, language: str) -> bool:
    """Whether ``isSnapshot`` at ``start`` declares a field (Java) or a property (Kotlin)."""
    if stripped[_next_non_blank(stripped, end): _next_non_blank(stripped, end) + 1] == "(":
        return False
    previous = _previous_non_blank(stripped, start - 1)
    if previous < 0:
        return False
    previous_char = stripped[previous]
    if language == KOTLIN:
        return _is_identifier_part(previous_char) and _word_ending_at(stripped, previous) in ("val", "var")
    if _is_identifier_part(previous_char):
        return _word_ending_at(stripped, previous) not in _EXPRESSION_KEYWORDS
    if previous_char == "]":
        return True
    if previous_char == ">":
        return stripped[previous - 1: previous + 1] != "->"
    if previous_char == ",":
        # A further declarator of the same field: `boolean a = true, isSnapshot;`.
        following = _next_non_blank(stripped, end)
        next_char = stripped[following: following + 1]
        return next_char == ";" or (next_char == "=" and stripped[following + 1: following + 2] != "=")
    return False


def _is_udf_class_member(stripped: str, start: int, end: int, class_body, language: str) -> bool:
    if class_body is not None and class_body[0] < start <= class_body[1]:
        if _directly_inside(stripped, class_body[0], start):
            return _is_member_declaration(stripped, start, end, language)
        if language != KOTLIN:
            return False
        companion = _enclosing_block(stripped, start)
        return (
            companion is not None
            and _enclosing_block(stripped, companion[0]) == class_body
            and _COMPANION_OBJECT.search(stripped[: companion[0]]) is not None
            and _directly_inside(stripped, companion[0], start)
            and _is_member_declaration(stripped, start, end, language)
        )
    if language != KOTLIN:
        return False
    # The primary constructor: between the `class` keyword and the class body.
    if class_body is not None and start < class_body[0]:
        headers = list(_CLASS_KEYWORD.finditer(stripped[: class_body[0]]))
        if headers and start > headers[-1].start():
            return _is_member_declaration(stripped, start, end, language)
    # A top-level property.
    return (
        _enclosing_block(stripped, start) is None
        and _directly_inside(stripped, -1, start)
        and _is_member_declaration(stripped, start, end, language)
    )


def _uses_reserved_name(stripped: str, declaration: _Declaration, language: str) -> bool:
    """Whether code written for the previous signature already has its own ``isSnapshot``.

    In the body of ``onChange`` any bare use counts: it reads a local or a field, and
    either would clash with (or be silently replaced by) the added parameter. Elsewhere
    only a field or property of the UDF class counts; parameters and locals of other
    methods live in a scope of their own.
    """
    body = _body_of(stripped, declaration, language)
    class_body = _enclosing_block(stripped, declaration.open)
    for match in _RESERVED_NAME.finditer(stripped):
        start, end = match.start(), match.end()
        if declaration.open <= start <= declaration.close:
            continue
        if body is not None and body[0] <= start <= body[1]:
            if _is_bare_name(stripped, start, end, language):
                return True
        elif _is_udf_class_member(stripped, start, end, class_body, language):
            return True
    return False


def _reads_is_snapshot(stripped: str, declaration: _Declaration, language: str) -> bool:
    body = _body_of(stripped, declaration, language)
    if body is None:
        return False
    return any(
        body[0] <= m.start() <= body[1] and _is_bare_name(stripped, m.start(), m.end(), language)
        for m in _RESERVED_NAME.finditer(stripped)
    )


# ---------------------------------------------------------------------------
# Rewrites
# ---------------------------------------------------------------------------

def _single(declarations: List[_Declaration], udf_name: str, kind: str) -> Optional[_Declaration]:
    if len(declarations) > 1:
        raise UdfSignatureError(
            f"UDF '{udf_name}' declares {len(declarations)} {METHOD_NAME} methods with the {kind} signature, "
            f"and which one Gluesync calls cannot be told apart. Keep a single {METHOD_NAME} entry point."
        )
    return declarations[0] if declarations else None


def add_is_snapshot_parameter(code: str, udf_type, udf_name: str = "UDF") -> SignatureAdaptation:
    """``code`` with ``isSnapshot`` added before the logger of its ``onChange`` declaration.

    Code already on the current signature, or with no recognisable ``onChange``, is
    returned unchanged (the CoreHub compilation then reports what is wrong with it).
    Raises :class:`UdfSignatureError` when the code already has a variable of its own
    named ``isSnapshot`` that the parameter would clash with.
    """
    language = _language(udf_type)
    if language is None:
        return SignatureAdaptation(code, False, f"{udf_type} UDFs are not compiled; signature left as is")
    stripped = blank_comments_and_literals(code)
    declarations = _declarations(stripped, language)
    if any(_is_current(d, language) for d in declarations):
        return SignatureAdaptation(code, False)

    previous = _single([d for d in declarations if _is_previous(d, language)], udf_name, "previous")
    if previous is None:
        return SignatureAdaptation(
            code, False,
            f"no {METHOD_NAME} declaration with the previous signature (newValues, oldValues, operation, logger) "
            f"was found; source left as is",
        )

    if _uses_reserved_name(stripped, previous, language):
        raise UdfSignatureError(
            f"UDF '{udf_name}' is written for the previous {METHOD_NAME} signature and already uses its own variable "
            f"named '{IS_SNAPSHOT_PARAMETER}' (a field/property of the class, or a variable in the body of "
            f"{METHOD_NAME}). From Gluesync 2.3 '{IS_SNAPSHOT_PARAMETER}' is the {METHOD_NAME} parameter that tells "
            f"whether the row comes from the snapshot, so the bootstrapper cannot add it for you. Rename your "
            f"variable and declare the method as: {EXPECTED_DECLARATION[language]}"
        )

    logger_parameter = previous.parameters[-1]
    comma = stripped.rfind(",", 0, logger_parameter.start)
    spacing = stripped[comma + 1: logger_parameter.start]
    if spacing.strip():
        spacing = " "
    parameter = f"boolean {IS_SNAPSHOT_PARAMETER}" if language == JAVA else f"{IS_SNAPSHOT_PARAMETER}: Boolean"
    rewritten = code[: logger_parameter.start] + parameter + "," + spacing + code[logger_parameter.start:]
    return SignatureAdaptation(
        rewritten, True, f"added '{parameter}' before the logger of {METHOD_NAME} (Gluesync 2.3 signature)"
    )


def remove_is_snapshot_parameter(code: str, udf_type, udf_name: str = "UDF") -> SignatureAdaptation:
    """``code`` with ``isSnapshot`` removed from its ``onChange`` declaration, for a CoreHub before 2.3.

    Raises :class:`UdfSignatureError` when the body of ``onChange`` reads ``isSnapshot``:
    a CoreHub before 2.3 cannot tell the function where the row comes from.
    """
    language = _language(udf_type)
    if language is None:
        return SignatureAdaptation(code, False, f"{udf_type} UDFs are not compiled; signature left as is")
    stripped = blank_comments_and_literals(code)
    declarations = _declarations(stripped, language)
    if any(_is_previous(d, language) for d in declarations):
        return SignatureAdaptation(code, False)

    current = _single([d for d in declarations if _is_current(d, language)], udf_name, "current")
    if current is None:
        return SignatureAdaptation(
            code, False, f"no {METHOD_NAME} declaration with a recognised signature was found; source left as is"
        )

    if _reads_is_snapshot(stripped, current, language):
        raise UdfSignatureError(
            f"UDF '{udf_name}' reads '{IS_SNAPSHOT_PARAMETER}', which only CoreHub 2.3 and later pass to "
            f"{METHOD_NAME}. The target CoreHub is older: upgrade it, or remove '{IS_SNAPSHOT_PARAMETER}' from the "
            f"UDF and declare the method as: {PREVIOUS_DECLARATION[language]}"
        )

    is_snapshot_parameter, logger_parameter = current.parameters[3], current.parameters[4]
    rewritten = code[: is_snapshot_parameter.start] + code[logger_parameter.start:]
    return SignatureAdaptation(
        rewritten, True, f"removed '{is_snapshot_parameter.text}' from {METHOD_NAME} (CoreHub before 2.3)"
    )


def adapt_udf_signature(code: str, udf_type, *, supports_is_snapshot: bool, udf_name: str = "UDF") -> SignatureAdaptation:
    """``code`` with the ``onChange`` signature the target CoreHub expects."""
    if supports_is_snapshot:
        return add_is_snapshot_parameter(code, udf_type, udf_name)
    return remove_is_snapshot_parameter(code, udf_type, udf_name)


__all__ = [
    "EXPECTED_DECLARATION",
    "IS_SNAPSHOT_MIN_COREHUB_VERSION",
    "IS_SNAPSHOT_PARAMETER",
    "PREVIOUS_DECLARATION",
    "SignatureAdaptation",
    "UdfSignatureError",
    "adapt_udf_signature",
    "add_is_snapshot_parameter",
    "blank_comments_and_literals",
    "corehub_supports_is_snapshot",
    "parse_corehub_version",
    "remove_is_snapshot_parameter",
]
