# This program is part of Gluesync.
#
# Automator is dual-licensed under the following licenses:
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

"""How a GS1 configuration value reads, as Gluesync 2's JSON library reads it.

A configuration is parsed into plain dicts and lists. A JSON primitive is a string, a number, a boolean or
null; its *content* is the text the converter reads: the string itself, the number as written, ``true`` or
``false``, and ``null`` for null. :func:`load_config` keeps the literal of every number, so a ``1.50`` or a
``1e3`` reads back as written.
"""

import json
import re
from typing import Any, Dict, List, Optional

_INT32_MIN = -(2 ** 31)
_INT32_MAX = 2 ** 31 - 1
_INT_TEXT = re.compile(r"-?[0-9]+")


class JsonInt(int):
    """An integer that remembers how the configuration wrote it."""

    literal: str

    def __new__(cls, literal: str) -> "JsonInt":
        value = super().__new__(cls, int(literal))
        value.literal = literal
        return value


class JsonFloat(float):
    """A decimal number that remembers how the configuration wrote it."""

    literal: str

    def __new__(cls, literal: str) -> "JsonFloat":
        value = super().__new__(cls, float(literal))
        value.literal = literal
        return value


def load_config(text: str) -> Any:
    """Parses a GS1 configuration, keeping the literal of every number."""
    return json.loads(text, parse_int=JsonInt, parse_float=JsonFloat)


def is_primitive(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool, int, float))


def content(value: Any) -> Optional[str]:
    """The text of a primitive, None for an object or an array."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        literal = getattr(value, "literal", None)
        if literal is not None:
            return literal
        if isinstance(value, int):
            return str(value)
        return _float_text(value)
    return None


def _float_text(value: float) -> str:
    text = repr(value)
    return text[:-2] + ".0" if text.endswith(".0") else text


def as_int(value: Any) -> Optional[int]:
    """The primitive as a 32-bit integer, None when its content is not one."""
    text = content(value)
    if text is None or not _INT_TEXT.fullmatch(text):
        return None
    number = int(text)
    return number if _INT32_MIN <= number <= _INT32_MAX else None


def as_bool(value: Any) -> Optional[bool]:
    """The primitive as a boolean: its content is exactly ``true`` or ``false``."""
    text = content(value)
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def obj(value: Any) -> Dict[str, Any]:
    """The value when it is an object, else an empty one."""
    return value if isinstance(value, dict) else {}


def string(container: Dict[str, Any], name: str) -> str:
    """The content of a primitive member, empty when the member is missing or not a primitive."""
    if name not in container:
        return ""
    text = content(container[name])
    return "" if text is None else text


def optional_string(container: Dict[str, Any], name: str) -> Optional[str]:
    if name not in container:
        return None
    return content(container[name])


def strings(container: Dict[str, Any], name: str) -> List[str]:
    """The contents of the primitives of an array member; anything else reads as no item."""
    items = container.get(name)
    if not isinstance(items, list):
        return []
    return [text for text in (content(item) for item in items) if text is not None]


def int_member(container: Dict[str, Any], name: str) -> Optional[int]:
    if name not in container:
        return None
    return as_int(container[name])


def bool_member(container: Dict[str, Any], name: str) -> Optional[bool]:
    if name not in container:
        return None
    return as_bool(container[name])


def kotlin_list(items: List[Any]) -> str:
    """A list as Kotlin prints it in a message: ``[a, b]``, null as ``null``."""
    return "[" + ", ".join("null" if item is None else str(item) for item in items) + "]"


def json_text(value: Any) -> str:
    """A value as compact JSON, numbers as written."""
    if isinstance(value, dict):
        return "{" + ",".join(f"{_quoted(key)}:{json_text(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(json_text(item) for item in value) + "]"
    if isinstance(value, str):
        return _quoted(value)
    return content(value)


_ESCAPES = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}


def _quoted(text: str) -> str:
    out = []
    for char in text:
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif ord(char) < 0x20:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def take_utf16(text: str, count: int) -> str:
    """The first ``count`` UTF-16 code units of a text, as Kotlin's ``take``."""
    units = text.encode("utf-16-le")
    if len(units) <= 2 * count:
        return text
    return units[: 2 * count].decode("utf-16-le", errors="surrogatepass")


def utf16_sort_key(text: str) -> bytes:
    """Orders texts by UTF-16 code unit, as Kotlin's ``String.compareTo``."""
    return text.encode("utf-16-be", errors="surrogatepass")
