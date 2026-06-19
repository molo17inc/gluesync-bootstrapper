# Copyright (c) 2026 MOLO17
# Author: MOLO17
#
# Decodes DbMoto field-mapping expressions (MakeDateTime / LowDate / Int / field
# renames) from a DbMoto metadata XML export and produces an Automator
# import-ready package:
#   - one pipeline YAML per (source connection -> target connection) pair,
#     containing only the entities that require a field-mapping-to-function,
#     each wired to a dedicated Java UDF (unlockedSchema + customProperties.target.udf)
#   - one generated Java UDF per entity, modeled on the master template
#     ("FHCUSMST UDF.txt") but with the put() calls wired to the decoded
#     expressions discovered in DBMMFieldMappings
#   - an agents-config.yaml skeleton (credentials must be filled in before import)
#   - a ZIP archive laid out exactly like an Automator "Export all" backup so it
#     can be imported via the Automator "Import all" functionality (UDF sources
#     are placed under a udf-* folder so they are compiled automatically).
#
# Usage:
#   python3 generate_udf_import_package.py <metadata.xml> [--output-dir DIR] [--no-zip]

import argparse
import hashlib
import os
import re
import sys
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import yaml

# Make sure we can import the existing DbMoto parser from the same folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_dbmoto_metadata_xml as dbmoto  # noqa: E402

# ----------------------------------------------------------------------------
# Expression parsing (DbMoto VB-like expressions)
# ----------------------------------------------------------------------------

class ExprNode:
    pass


class Call(ExprNode):
    def __init__(self, name: str, args: List[ExprNode]):
        self.name = name
        self.args = args

    def __repr__(self):
        return f"{self.name}({', '.join(map(repr, self.args))})"


class FieldRef(ExprNode):
    def __init__(self, name: str):
        self.name = name

    def __repr__(self):
        return f"[{self.name}]"


class StrLit(ExprNode):
    def __init__(self, value: str):
        self.value = value

    def __repr__(self):
        return f'"{self.value}"'


class NumLit(ExprNode):
    def __init__(self, value: str):
        self.value = value

    def __repr__(self):
        return self.value


class BinOp(ExprNode):
    def __init__(self, op: str, left: ExprNode, right: ExprNode):
        self.op = op
        self.left = left
        self.right = right

    def __repr__(self):
        return f"({self.left!r} {self.op} {self.right!r})"


_TOKEN_RE = re.compile(
    r"""
    \s*(
        \[[^\]]+\]            |   # field reference [NAME]
        "[^"]*"               |   # string literal
        [A-Za-z_][A-Za-z0-9_]* |  # identifier
        \d+(?:\.\d+)?         |   # number
        [(),/*+\-.]               # punctuation / operators
    )
    """,
    re.VERBOSE,
)


def tokenize(expression: str) -> List[str]:
    tokens = []
    pos = 0
    while pos < len(expression):
        match = _TOKEN_RE.match(expression, pos)
        if not match:
            if expression[pos].isspace():
                pos += 1
                continue
            raise ValueError(f"Unexpected character {expression[pos]!r} in expression: {expression}")
        tokens.append(match.group(1))
        pos = match.end()
    return tokens


class ExpressionParser:
    def __init__(self, tokens: List[str]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Optional[str]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> str:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def parse(self) -> ExprNode:
        node = self.parse_expr()
        if self.pos != len(self.tokens):
            raise ValueError(f"Trailing tokens after expression: {self.tokens[self.pos:]}")
        return node

    def parse_expr(self) -> ExprNode:
        node = self.parse_term()
        while self.peek() in ("/", "*", "+", "-"):
            op = self.next()
            right = self.parse_term()
            node = BinOp(op, node, right)
        return node

    def parse_term(self) -> ExprNode:
        node = self.parse_primary()
        # VB-style method calls: [Field].ToUpper()
        while self.peek() == ".":
            self.next()
            method = self.next()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", method):
                raise ValueError(f"Expected method name after '.', got {method!r}")
            if self.next() != "(":
                raise ValueError(f"Expected '(' after method name {method!r}")
            args: List[ExprNode] = [node]
            if self.peek() != ")":
                args.append(self.parse_expr())
                while self.peek() == ",":
                    self.next()
                    args.append(self.parse_expr())
            if self.next() != ")":
                raise ValueError("Expected closing parenthesis after method args")
            node = Call(f".{method}", args)
        return node

    def parse_primary(self) -> ExprNode:
        token = self.peek()
        if token is None:
            raise ValueError("Unexpected end of expression")
        if token == "(":
            self.next()
            node = self.parse_expr()
            if self.next() != ")":
                raise ValueError("Expected closing parenthesis")
            return node
        if token.startswith("["):
            self.next()
            return FieldRef(token[1:-1].strip())
        if token.startswith('"'):
            self.next()
            return StrLit(token[1:-1])
        if re.fullmatch(r"\d+(?:\.\d+)?", token):
            self.next()
            return NumLit(token)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
            name = self.next()
            if self.peek() == "(":
                self.next()
                args: List[ExprNode] = []
                if self.peek() != ")":
                    args.append(self.parse_expr())
                    while self.peek() == ",":
                        self.next()
                        args.append(self.parse_expr())
                if self.next() != ")":
                    raise ValueError("Expected closing parenthesis after call args")
                return Call(name, args)
            # bare identifier (unexpected in DbMoto expressions)
            return FieldRef(name)
        raise ValueError(f"Unexpected token: {token}")


def parse_expression(expression: str) -> ExprNode:
    return ExpressionParser(tokenize(expression)).parse()


def collect_field_refs(node: ExprNode) -> List[str]:
    refs: List[str] = []

    def visit(n: ExprNode):
        if isinstance(n, FieldRef):
            if n.name not in refs:
                refs.append(n.name)
        elif isinstance(n, Call):
            for a in n.args:
                visit(a)
        elif isinstance(n, BinOp):
            visit(n.left)
            visit(n.right)

    visit(node)
    return refs


# ----------------------------------------------------------------------------
# Java code generation
# ----------------------------------------------------------------------------

def classify_field_type(raw_type: str) -> str:
    t = (raw_type or "").strip().upper()
    if t == "DATE":
        return "date"
    if t == "TIME":
        return "time"
    if t in ("TIMESTAMP", "TIMESTMP", "DATETIME"):
        return "timestamp"
    return "decimal"


class JavaCodegen:
    """Translates a decoded DbMoto expression AST into a Java expression that
    relies on the helper methods emitted with every generated UDF."""

    def __init__(self, field_types: Dict[str, Dict[str, Any]], warnings: List[str], context_label: str):
        # field_types: source field name (upper) -> {"type": classified, "precision": int}
        self.field_types = field_types
        self.warnings = warnings
        self.context_label = context_label

    def field_meta(self, name: str) -> Dict[str, Any]:
        meta = self.field_types.get(name.upper())
        if meta is None:
            self.warnings.append(
                f"{self.context_label}: source field [{name}] not found in source table metadata; assuming DECIMAL"
            )
            return {"type": "decimal", "precision": 0}
        return meta

    def gen(self, node: ExprNode) -> Tuple[str, str]:
        """Returns (java_expression, kind) where kind is one of:
        'decimal', 'date', 'time', 'dateobj' (Object holding LocalDate),
        'object', 'string'."""
        if isinstance(node, StrLit):
            return f'"{node.value}"', "string"
        if isinstance(node, NumLit):
            return f"new BigDecimal({node.value})", "decimal"
        if isinstance(node, FieldRef):
            meta = self.field_meta(node.name)
            kind = meta["type"]
            getter = f'newValues.get("{node.name}")'
            if kind == "decimal":
                return f"toBigDecimal({getter})", "decimal"
            if kind == "date":
                return f"toLocalDate({getter})", "date"
            if kind == "time":
                return f"toLocalTime({getter})", "time"
            if kind == "timestamp":
                return f"toLocalDate({getter})", "date"
            return f"toBigDecimal({getter})", "decimal"
        if isinstance(node, BinOp):
            left, _ = self.gen(node.left)
            right, _ = self.gen(node.right)
            if node.op == "/":
                return f"intDivide({left}, {right})", "decimal"
            self.warnings.append(f"{self.context_label}: unsupported operator '{node.op}'")
            return f"null /* unsupported operator {node.op} */", "object"
        if isinstance(node, Call):
            return self.gen_call(node)
        raise ValueError(f"Unsupported AST node: {node!r}")

    def gen_call(self, node: Call) -> Tuple[str, str]:
        name = node.name.lower()
        if name == "makedatetime":
            return self.gen_makedatetime(node)
        if name == "lowdate":
            inner, _ = self.gen(node.args[0])
            return f"lowDate({inner})", "object"
        if name == "int":
            arg = node.args[0]
            if isinstance(arg, BinOp) and arg.op == "/":
                left, _ = self.gen(arg.left)
                right, _ = self.gen(arg.right)
                return f"intDivide({left}, {right})", "decimal"
            inner, _ = self.gen(arg)
            return f"truncateToInt({inner})", "decimal"
        if name.startswith("."):
            method = name[1:].lower()
            receiver = node.args[0]
            if isinstance(receiver, FieldRef):
                recv_expr = f'newValues.get("{receiver.name}")'
            else:
                recv_expr, _ = self.gen(receiver)
            if method == "toupper":
                return f"toUpperString({recv_expr})", "object"
            if method == "tolower":
                return f"toLowerString({recv_expr})", "object"
            if method == "trim":
                return f"trimString({recv_expr})", "object"
            self.warnings.append(f"{self.context_label}: unsupported method '.{name[1:]}()'")
            return f"null /* unsupported method .{name[1:]}() */", "object"
        self.warnings.append(f"{self.context_label}: unsupported function '{node.name}'")
        return f"null /* unsupported function {node.name} */", "object"

    def as_local_date(self, expr: str, kind: str) -> str:
        if kind == "date":
            return expr
        if kind in ("dateobj", "object"):
            return f"(LocalDate){expr}"
        return f"(LocalDate){expr}"

    def decimal_date_format_heuristic(self, field_name: str) -> str:
        precision = self.field_meta(field_name)["precision"] or 0
        if precision >= 8:
            return "YYYYMMDD"
        if precision == 7:
            return "CYYMMDD"
        # 6-digit decimal date: cannot distinguish MMDDYY from YYMMDD
        self.warnings.append(
            f"{self.context_label}: decimal date field [{field_name}] has precision {precision}; "
            "assumed format MMDDYY - PLEASE REVIEW"
        )
        return "MMDDYY"

    def gen_makedatetime(self, node: Call) -> Tuple[str, str]:
        args = node.args
        if not args:
            raise ValueError("MakeDateTime with no arguments")

        # Variant with explicit format string as first argument
        if isinstance(args[0], StrLit):
            fmt = args[0].value.upper()
            rest = args[1:]
            if len(rest) == 1:
                inner, kind = self.gen(rest[0])
                if kind != "decimal":
                    self.warnings.append(
                        f"{self.context_label}: MakeDateTime(\"{fmt}\", x) where x is not DECIMAL; review"
                    )
                return f'makeDateTime("{fmt}", {inner})', "dateobj"
            if len(rest) == 2:
                # MakeDateTime("MMDDYY", [fullDate], [century])
                full, _ = self.gen(rest[0])
                century, _ = self.gen(rest[1])
                return f'makeDateTime("{fmt}", {full}, {century})', "dateobj"
            if len(rest) == 3:
                # MakeDateTime("YY"|"YYYY", [month], [day], [year])
                month, _ = self.gen(rest[0])
                day, _ = self.gen(rest[1])
                year, _ = self.gen(rest[2])
                return f'makeDateTime("{fmt}", {month}, {day}, {year})', "dateobj"
            raise ValueError(f"Unsupported MakeDateTime arity with format: {node!r}")

        # Variants without a format string
        if len(args) == 1:
            inner_node = args[0]
            if isinstance(inner_node, FieldRef):
                meta = self.field_meta(inner_node.name)
                if meta["type"] in ("date", "timestamp"):
                    inner, _ = self.gen(inner_node)
                    return f"makeDateTime({inner})", "dateobj"
                # decimal date without an explicit format: use a precision heuristic
                fmt = self.decimal_date_format_heuristic(inner_node.name)
                inner, _ = self.gen(inner_node)
                return f'makeDateTime("{fmt}", {inner})', "dateobj"
            inner, kind = self.gen(inner_node)
            return f"makeDateTime({self.as_local_date(inner, kind)})", "dateobj"

        if len(args) == 2:
            first, second = args
            second_expr, second_kind = self.gen(second)
            if isinstance(first, Call):
                inner, kind = self.gen_call(first)
                date_expr = self.as_local_date(inner, kind)
            elif isinstance(first, FieldRef):
                meta = self.field_meta(first.name)
                if meta["type"] in ("date", "timestamp"):
                    date_expr, _ = self.gen(first)
                else:
                    fmt = self.decimal_date_format_heuristic(first.name)
                    inner, _ = self.gen(first)
                    date_expr = f'(LocalDate)makeDateTime("{fmt}", {inner})'
            else:
                inner, kind = self.gen(first)
                date_expr = self.as_local_date(inner, kind)

            if second_kind == "time":
                return f"makeDateTime({date_expr}, {second_expr})", "object"
            return f"makeDateTime({date_expr}, {second_expr})", "object"

        if len(args) == 4:
            # MakeDateTime([month],[day],[shortYear],[century]) - all decimals
            parts = [self.gen(a)[0] for a in args]
            return f"makeDateTime({', '.join(parts)})", "dateobj"

        raise ValueError(f"Unsupported MakeDateTime arity: {node!r}")


# ----------------------------------------------------------------------------
# Java UDF assembly
# ----------------------------------------------------------------------------

JAVA_HELPERS = r'''
    /* ------------------------------------------------------------------
     * Helper functions ported from the DbMoto VB.NET scripting library.
     * Generated from the Gluesync UDF master template.
     * ------------------------------------------------------------------ */

    public static BigDecimal toBigDecimal(Object value) {
        if (value == null) {
            return null;
        }
        if (value instanceof BigDecimal) {
            return (BigDecimal) value;
        }
        return new BigDecimal(value.toString());
    }

    public static LocalDate toLocalDate(Object value) {
        if (value == null) {
            return null;
        }
        if (value instanceof LocalDate) {
            return (LocalDate) value;
        }
        if (value instanceof LocalDateTime) {
            return ((LocalDateTime) value).toLocalDate();
        }
        String s = value.toString().trim();
        if (s.isEmpty()) {
            return null;
        }
        if (s.length() >= 10) {
            return LocalDate.parse(s.substring(0, 10));
        }
        return LocalDate.parse(s);
    }

    public static LocalTime toLocalTime(Object value) {
        if (value == null) {
            return null;
        }
        if (value instanceof LocalTime) {
            return (LocalTime) value;
        }
        if (value instanceof LocalDateTime) {
            return ((LocalDateTime) value).toLocalTime();
        }
        String s = value.toString().trim();
        if (s.isEmpty()) {
            return null;
        }
        int tIdx = s.indexOf('T');
        if (tIdx >= 0) {
            s = s.substring(tIdx + 1);
        }
        if (s.length() > 8) {
            s = s.substring(0, 8);
        }
        return LocalTime.parse(s);
    }

    public static String toUpperString(Object value) {
        return value == null ? null : value.toString().toUpperCase();
    }

    public static String toLowerString(Object value) {
        return value == null ? null : value.toString().toLowerCase();
    }

    public static String trimString(Object value) {
        return value == null ? null : value.toString().trim();
    }

    /* DbMoto Int(x / y): integral part of the division */
    public static BigDecimal intDivide(BigDecimal value, BigDecimal divisor) {
        if (value == null || divisor == null) {
            return null;
        }
        return value.divideToIntegralValue(divisor);
    }

    /* DbMoto Int(x): truncates the decimal part */
    public static BigDecimal truncateToInt(BigDecimal value) {
        if (value == null) {
            return null;
        }
        return new BigDecimal(value.toBigInteger());
    }

    /* DbMoto LowDate(x): nulls out dates below the SQL Server low boundary */
    public static Object lowDate(Object value) {
        if (value == null) {
            return null;
        }
        LocalDate d = null;
        if (value instanceof LocalDateTime) {
            d = ((LocalDateTime) value).toLocalDate();
        } else if (value instanceof LocalDate) {
            d = (LocalDate) value;
        }
        if (d != null && d.compareTo(LocalDate.parse("1753-01-01")) < 0) {
            return null;
        }
        return value;
    }

    /* Decimal date in MMDDYY format -> DateTime */
    public static Object getDateMMDDYY(BigDecimal fullDate) throws Exception {
        if (fullDate == null) {
            return null;
        }
        int divDate = fullDate.abs().intValue();
        int day = (divDate % 10000) / 100;
        int month = divDate / 10000;
        int year = divDate % 100;
        if (year < 40) {
            year = 2000 + year;
        } else {
            year = 1900 + year;
        }
        return makeDateTime("YYYY", new BigDecimal(month), new BigDecimal(day), new BigDecimal(year));
    }

    /* Decimal date in CYYMMDD format -> DateTime */
    public static Object getDateCYYMMDD(BigDecimal fullDate) throws Exception {
        if (fullDate == null) {
            return null;
        }
        int divDate = fullDate.abs().intValue();
        int month = (divDate % 10000) / 100;
        int day = divDate % 100;
        int year;
        int century = divDate / 1000000;
        switch (century) {
            case 0:
                year = 1900 + (divDate / 10000);
                break;
            case 1:
                year = 2000 + ((divDate / 10000) - 100);
                break;
            default:
                return null;
        }
        return makeDateTime("YYYY", new BigDecimal(month), new BigDecimal(day), new BigDecimal(year));
    }

    /* Decimal date in YYMMDD format -> DateTime */
    public static Object getDateYYMMDD(BigDecimal fullDate) throws Exception {
        if (fullDate == null) {
            return null;
        }
        int divDate = fullDate.abs().intValue();
        int month = (divDate % 10000) / 100;
        int day = divDate % 100;
        int year = divDate / 10000;
        if (year < 40) {
            year = 2000 + year;
        } else {
            year = 1900 + year;
        }
        return makeDateTime("YYYY", new BigDecimal(month), new BigDecimal(day), new BigDecimal(year));
    }

    /* Decimal date in YYYYMMDD format -> DateTime */
    public static Object getDateYYYYMMDD(BigDecimal fullDate) throws Exception {
        if (fullDate == null) {
            return null;
        }
        int divDate = fullDate.abs().intValue();
        int month = (divDate % 10000) / 100;
        int day = divDate % 100;
        int year = divDate / 10000;
        return makeDateTime("YYYY", new BigDecimal(month), new BigDecimal(day), new BigDecimal(year));
    }

    /* MakeDateTime(formatYear, month, day, year) */
    public static Object makeDateTime(String formatYear, BigDecimal month, BigDecimal day, BigDecimal year) throws Exception {
        if (month == null || day == null || year == null) {
            return null;
        }
        int fullYear;
        int intMonth = month.intValue();
        int intDay = day.intValue();
        int intYear = year.intValue();

        if ("YY".equals(formatYear.toUpperCase())) {
            if (intYear <= 39) {
                fullYear = 2000 + intYear;
            } else {
                fullYear = 1900 + intYear;
            }
        } else if ("YYYY".equals(formatYear.toUpperCase())) {
            fullYear = intYear;
        } else {
            throw new Exception("Valid year formats are YY or YYYY");
        }

        if ((intMonth < 1) || (intMonth > 12)) {
            return null;
        }
        if ((intDay < 1) || (intDay > 31)) {
            return null;
        }
        if (fullYear < 1753) {
            return null;
        }
        if (((intMonth == 4) || (intMonth == 6) || (intMonth == 9) || (intMonth == 11)) && (intDay > 30)) {
            return null;
        } else if (intMonth == 2) {
            if ((fullYear % 4) == 0) {
                if (intDay > 29) {
                    return null;
                }
            } else {
                if (intDay > 28) {
                    return null;
                }
            }
        }
        return LocalDate.of(fullYear, intMonth, intDay);
    }

    /* MakeDateTime(month, day, shortYear, century) */
    public static Object makeDateTime(BigDecimal month, BigDecimal day, BigDecimal shortYear, BigDecimal century) throws Exception {
        if (month == null || day == null || shortYear == null || century == null) {
            return null;
        }
        BigDecimal fullYear;
        int intCentury = century.intValue();
        int intShortYear = shortYear.intValue();
        if (intCentury < 19) {
            fullYear = new BigDecimal(1900 + (100 * intCentury) + intShortYear);
        } else {
            fullYear = new BigDecimal((100 * intCentury) + intShortYear);
        }
        return makeDateTime("YYYY", month, day, fullYear);
    }

    /* MakeDateTime(formatDate, fullDate, century) */
    public static Object makeDateTime(String formatDate, BigDecimal fullDate, BigDecimal century) throws Exception {
        if (fullDate == null || century == null) {
            return null;
        }
        int divDate = fullDate.intValue();
        int day;
        int month;
        int year;
        int fullYear;
        int intCentury = century.intValue();

        if ("MMDDYY".equals(formatDate.toUpperCase())) {
            month = divDate / 10000;
            day = (divDate % 10000) / 100;
            year = divDate % 100;
        } else {
            throw new Exception("Valid format is MMDDYY");
        }

        if (intCentury < 19) {
            fullYear = 1900 + (100 * intCentury) + year;
        } else {
            fullYear = (100 * intCentury) + year;
        }
        return makeDateTime("YYYY", new BigDecimal(month), new BigDecimal(day), new BigDecimal(fullYear));
    }

    /* MakeDateTime(formatDate, fullDate) */
    public static Object makeDateTime(String formatDate, BigDecimal fullDate) throws Exception {
        if (fullDate == null) {
            return null;
        }
        switch (formatDate.toUpperCase()) {
            case "CYYMMDD":
                return getDateCYYMMDD(fullDate);
            case "MMDDYY":
                return getDateMMDDYY(fullDate);
            case "YYMMDD":
                return getDateYYMMDD(fullDate);
            case "YYYYMMDD":
                return getDateYYYYMMDD(fullDate);
            default:
                throw new Exception("Valid formats are CYYMMDD, MMDDYY, YYMMDD, and YYYYMMDD.");
        }
    }

    /* MakeDateTime(fullDate) - DATE passthrough with low-boundary validation */
    public static Object makeDateTime(LocalDate fullDate) {
        if (fullDate == null) {
            return fullDate;
        }
        if (fullDate.compareTo(LocalDate.parse("1753-01-01")) < 0) {
            return null;
        }
        return fullDate;
    }

    /* MakeDateTime(fullDate, fullTime) - DATE + TIME combination */
    public static Object makeDateTime(LocalDate fullDate, LocalTime fullTime) {
        if (fullDate == null) {
            return fullDate;
        }
        if (fullDate.compareTo(LocalDate.parse("1753-01-01")) < 0) {
            return null;
        }
        if (fullTime == null) {
            return fullDate;
        }
        return LocalDateTime.of(fullDate, fullTime);
    }

    /* MakeDateTime(fullDate, decimalTime) - DATE + decimal HHMMSS combination */
    public static Object makeDateTime(LocalDate fullDate, BigDecimal fullTime) {
        if (fullDate == null) {
            return fullDate;
        }
        if (fullDate.compareTo(LocalDate.parse("1753-01-01")) < 0) {
            return null;
        }
        if (fullTime == null) {
            return fullDate;
        }
        int tempTime = fullTime.abs().intValue();
        int hours = tempTime / 10000;
        int minutes = (tempTime % 10000) / 100;
        int seconds = tempTime % 100;
        if ((hours > 23) || (minutes > 59) || (seconds > 59)) {
            return fullDate;
        }
        return LocalDateTime.of(fullDate, LocalTime.of(hours, minutes, seconds));
    }
'''


def build_java_udf(
    udf_name: str,
    schema_name: str,
    table_name: str,
    repl_id: str,
    mappings: List[Dict[str, Any]],
    source_columns: List[str],
    target_columns: List[str],
) -> str:
    """Assemble the full Java UDF source for one entity."""
    mapping_comments = []
    delete_puts = []
    puts = []
    for m in mappings:
        mapping_comments.append(f" *   {m['target']} <- {m['expression']}")
        delete_puts.append(f'            modified_values.put("{m["target"]}", null);')
        if m.get("todo"):
            puts.append(f"                // TODO: {m['todo']}")
        if m["target"].upper() == "RECORDID":
            puts.append('                modified_values.put("RECORDID", newValues.get("_RRN"));')
        else:
            puts.append(f'                modified_values.put("{m["target"]}", {m["java"]});')

    # If RECORDID is present in the target columns but had no explicit DbMoto
    # mapping, wire it from the built-in _RRN source field.
    target_cols_upper = [c.upper() for c in target_columns]
    if "RECORDID" in target_cols_upper and not any(m["target"].upper() == "RECORDID" for m in mappings):
        delete_puts.append('            modified_values.put("RECORDID", null);')
        puts.append('                modified_values.put("RECORDID", newValues.get("_RRN"));')

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"""import java.util.Map;
import kotlin.Pair;
import com.molo17.gluesync.commons.model.api.MappingFunctionOperation;
import org.slf4j.Logger;
import java.lang.String;
import java.math.BigDecimal;
import java.time.LocalTime;
import java.time.LocalDate;
import java.time.LocalDateTime;

/*
 * That's a Gluesync UDF
 * Generated by generate_udf_import_package.py on {timestamp}
 * Entity: {schema_name}.{table_name} (DbMoto ReplicationID: {repl_id})
 *
 * Decoded DbMoto field-mapping expressions:
{chr(10).join(mapping_comments)}
 *
 * The function is invoked by Gluesync whatever the operation is (INSERT, UPDATE, DELETE),
 * as well as while performing the Snapshot task, and must return a Pair of the
 * (possibly changed) operation and the new values for the target row.
 */
"""

    body = f"""public class {udf_name} {{

    public Pair<MappingFunctionOperation, Map<String, Object>> onChange(Map<String, Object> newValues, Map<String, Object> oldValues, MappingFunctionOperation operation, Logger logger) {{
        // Source columns: {', '.join(source_columns)}
        // Target columns: {', '.join(target_columns)}

        Map<String, Object> modified_values = newValues;

        if (operation == MappingFunctionOperation.Delete) {{
{chr(10).join(delete_puts)}
            return new Pair<>(operation, modified_values);
        }}

        try {{
{chr(10).join(puts)}
        }} catch (Exception e) {{
            throw new RuntimeException(e);
        }}

        return new Pair<>(operation, modified_values);
    }}
{JAVA_HELPERS}
}}
"""
    return header + body


# ----------------------------------------------------------------------------
# Main generation logic
# ----------------------------------------------------------------------------

def _sanitize_identifier(value: str) -> str:
    """Turn an arbitrary name into a valid Java identifier fragment."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value).strip("_")
    if cleaned and cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned or "X"


# signature -> assigned UDF name, and name -> signature (collision detection)
_UDF_NAME_BY_SIGNATURE: Dict[str, str] = {}
_UDF_SIGNATURE_BY_NAME: Dict[str, str] = {}


def make_udf_name(schema_name: str, table_name: str, decoded: List[Dict[str, Any]]) -> str:
    """Easy-to-recall UDF name (UDF_<SCHEMA>_<TABLE>) that Automator picks up
    and injects as-is (class name == UDF name == file name).

    Identical replications (same table to multiple targets) share one UDF;
    if the same table appears with a diverging mapping set, a numeric suffix
    is appended to keep names unique."""
    signature = f"{schema_name}.{table_name}::" + "|".join(
        sorted(f"{m['target']}={m['java']}" for m in decoded)
    )
    if signature in _UDF_NAME_BY_SIGNATURE:
        return _UDF_NAME_BY_SIGNATURE[signature]

    base = f"UDF_{_sanitize_identifier(schema_name.upper())}_{_sanitize_identifier(table_name.upper())}"
    name = base
    suffix = 2
    while name in _UDF_SIGNATURE_BY_NAME and _UDF_SIGNATURE_BY_NAME[name] != signature:
        name = f"{base}_{suffix}"
        suffix += 1

    _UDF_NAME_BY_SIGNATURE[signature] = name
    _UDF_SIGNATURE_BY_NAME[name] = signature
    return name


def make_pipeline_id(src_conn: str, tgt_conn: str) -> str:
    return hashlib.md5(f"{src_conn}->{tgt_conn}".encode("utf-8")).hexdigest()[:8]


def main():
    parser = argparse.ArgumentParser(
        description="Generate an Automator import package (YAML + Java UDFs) from DbMoto field-mapping expressions."
    )
    parser.add_argument("xml_path", type=str, help="Path to the DbMoto metadata XML file")
    parser.add_argument("--output-dir", type=str, default="udf_import_package",
                        help="Directory for the generated package (default: udf_import_package)")
    parser.add_argument("--no-zip", action="store_true", help="Do not create the import ZIP archive")
    cli = parser.parse_args()

    xml_path = os.path.expanduser(cli.xml_path)
    output_dir = cli.output_dir
    os.makedirs(output_dir, exist_ok=True)
    udf_dir = os.path.join(output_dir, "udf-generated")
    os.makedirs(udf_dir, exist_ok=True)

    # Wire the reusable parser to our XML file and run it
    os.environ["XML_PATH"] = xml_path
    dbmoto.args.xml_path = xml_path
    (connections, groups, chains, replications, source_to_target_schemas,
     field_mappings, field_id_to_name, record_id_mappings,
     journal_checkpoints, refresh_filters) = dbmoto.parse_xml()

    # ------------------------------------------------------------------
    # Lookup structures
    # ------------------------------------------------------------------
    table_lookup: Dict[str, Dict[str, Any]] = {}
    for conn in connections.values():
        for schema in conn["schemas"].values():
            for table_id, table in schema["tables"].items():
                table_lookup[table_id] = {
                    "table": table,
                    "schema_name": schema["name"],
                    "connection_name": conn["name"],
                    "is_source": conn["is_source"],
                }

    # field name (upper) -> metadata, per table id
    fields_by_table: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for info in table_lookup.values():
        table = info["table"]
        fields_by_table[table["id"]] = {
            f["name"].upper(): {
                "type": classify_field_type(f.get("type")),
                "raw_type": f.get("type"),
                "precision": f.get("numeric_precision", 0) or f.get("data_length", 0),
                "data_length": f.get("data_length", 0),
                "numeric_precision": f.get("numeric_precision", 0),
                "numeric_scale": f.get("numeric_scale", 0),
                "name": f["name"],
            }
            for f in table.get("fields", [])
        }

    # group/chain assignments (same logic as export_as_yaml)
    table_assignments: Dict[str, Dict[str, str]] = {}
    for repl_id, repl in replications.items():
        group_id = repl["group_id"]
        table_id = repl["src_table_id"]
        if group_id in groups:
            table_assignments.setdefault(table_id, {})["groupId"] = groups[group_id]["name"]
        elif group_id in chains:
            table_assignments.setdefault(table_id, {})["chainId"] = chains[group_id]["name"]

    # ------------------------------------------------------------------
    # Decode expression mappings per replication
    # ------------------------------------------------------------------
    all_warnings: List[str] = []
    stats = {"replications_with_udf": 0, "expressions_decoded": 0, "expressions_skipped": 0, "udf_files": 0}

    # pipelines[(src_conn, tgt_conn)] = {schemas: {src_schema: {target, tables: {...}}}}
    pipelines: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for repl_id, repl in sorted(replications.items(), key=lambda kv: int(kv[0])):
        mappings_for_repl = field_mappings.get(repl_id, {})
        expression_entries = [
            (key, m) for key, m in mappings_for_repl.items()
            if m.get("type") == "expression" and m.get("src_expression")
            and "[!RecordID]" not in m["src_expression"]
        ]
        if not expression_entries:
            continue

        src_table_id = repl.get("src_table_id")
        trg_table_id = repl.get("trg_table_id")
        src_info = table_lookup.get(src_table_id)
        trg_info = table_lookup.get(trg_table_id)
        if not src_info or not trg_info:
            all_warnings.append(
                f"Replication {repl_id} ({repl.get('name')}): source/target table not found in metadata; skipped"
            )
            continue

        src_table = src_info["table"]
        schema_name = src_info["schema_name"]
        src_conn = src_info["connection_name"]
        tgt_conn = trg_info["connection_name"]
        tgt_schema_name = trg_info["schema_name"]
        table_name = src_table["name"]
        context_label = f"{schema_name}.{table_name} (repl {repl_id})"

        source_field_types = fields_by_table.get(src_table_id, {})
        target_field_types = fields_by_table.get(trg_table_id, {})
        codegen = JavaCodegen(source_field_types, all_warnings, context_label)

        decoded: List[Dict[str, Any]] = []
        for key, m in sorted(expression_entries, key=lambda kv: kv[0]):
            expression = m["src_expression"].strip()
            trg_field_id = m.get("target_field_id")
            target_field_name = field_id_to_name.get((trg_table_id, trg_field_id))
            if not target_field_name:
                all_warnings.append(f"{context_label}: target field id {trg_field_id} not resolvable; skipped expression {expression!r}")
                stats["expressions_skipped"] += 1
                continue
            try:
                ast = parse_expression(expression)
            except Exception as exc:
                all_warnings.append(f"{context_label}: failed to parse expression {expression!r}: {exc}")
                stats["expressions_skipped"] += 1
                continue

            # Pure field reference -> simple rename, pass value through unchanged
            if isinstance(ast, FieldRef):
                java_expr = f'newValues.get("{ast.name}")'
            else:
                try:
                    java_expr, _ = codegen.gen(ast)
                except Exception as exc:
                    all_warnings.append(f"{context_label}: failed to translate expression {expression!r}: {exc}")
                    stats["expressions_skipped"] += 1
                    continue

            decoded.append({
                "target": target_field_name,
                "expression": expression,
                "java": java_expr,
                "source_fields": collect_field_refs(ast),
            })
            stats["expressions_decoded"] += 1

        if not decoded:
            continue

        stats["replications_with_udf"] += 1

        # ------------------------------------------------------------------
        # Build YAML table entry (reuse the proven helper from the parser)
        # ------------------------------------------------------------------
        whitelist_name, table_config, is_disabled, _, _ = dbmoto._build_table_entry_helper(
            src_table, table_lookup, replications, field_mappings, field_id_to_name,
            record_id_mappings, refresh_filters, table_assignments, trg_table_id, repl_id
        )

        udf_name = make_udf_name(schema_name, table_name, decoded)

        table_config["entityName"] = f"{schema_name}.{table_name}"
        table_config["unlockedSchema"] = True

        # Target-only columns: expression targets that do not exist among the
        # source columns (the UDF populates them on the target side).
        existing_targets = {
            (col.get("name") or "").upper() for col in table_config.get("columns", [])
        }
        target_only = []
        for m in decoded:
            if m["target"].upper() in existing_targets:
                continue
            tgt_meta = target_field_types.get(m["target"].upper())
            entry: Dict[str, Any] = {"name": m["target"]}
            if tgt_meta:
                entry["type"] = tgt_meta["raw_type"] or "TIMESTAMP"
            else:
                entry["type"] = "TIMESTAMP"
            target_only.append(entry)
            existing_targets.add(m["target"].upper())
        if target_only:
            table_config["targetOnlyColumns"] = target_only

        custom_props = table_config.setdefault("customProperties", {})
        target_props = custom_props.setdefault("target", {})
        target_props["udf"] = [{"name": udf_name, "type": "Java"}]

        if is_disabled:
            table_config["_disabledInDbMoto"] = True

        # ------------------------------------------------------------------
        # Generate the Java UDF source
        # ------------------------------------------------------------------
        source_columns = [f["name"] for f in src_table.get("fields", [])]
        target_columns = [
            meta["name"] for meta in target_field_types.values()
        ] or source_columns
        java_source = build_java_udf(
            udf_name, schema_name, table_name, repl_id, decoded, source_columns, target_columns
        )
        java_path = os.path.join(udf_dir, f"{udf_name}.java")
        if not os.path.exists(java_path):
            with open(java_path, "w", encoding="utf-8") as f:
                f.write(java_source)
            stats["udf_files"] += 1
            print(f"  Generated UDF {udf_name}.java for {schema_name}.{table_name} ({len(decoded)} mappings)")
        else:
            print(f"  Reusing UDF {udf_name}.java for {schema_name}.{table_name} (identical mapping set)")

        # ------------------------------------------------------------------
        # Register the table in the pipeline / schema buckets
        # ------------------------------------------------------------------
        pipeline_key = (src_conn, tgt_conn)
        pipeline = pipelines.setdefault(pipeline_key, {"schemas": {}})
        schema_bucket = pipeline["schemas"].setdefault(schema_name, {
            "target": tgt_schema_name,
            "sourceType": "SQL",
            "targetType": "SQL",
            "tables": {"whitelist": [], "custom": {}},
        })
        if schema_bucket["target"] != tgt_schema_name:
            all_warnings.append(
                f"Schema {schema_name}: multiple target schemas detected "
                f"({schema_bucket['target']} vs {tgt_schema_name}); keeping {schema_bucket['target']}"
            )
        if whitelist_name not in schema_bucket["tables"]["whitelist"]:
            schema_bucket["tables"]["whitelist"].append(whitelist_name)
        schema_bucket["tables"]["custom"][table_name] = table_config

    # ------------------------------------------------------------------
    # Write pipeline YAMLs + agents-config skeleton + ZIP
    # ------------------------------------------------------------------
    if not pipelines:
        print("No entities with field-mapping-to-function expressions were found. Nothing to generate.")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    yaml_files: List[str] = []
    pipelines_meta: List[Dict[str, Any]] = []

    for (src_conn, tgt_conn), pipeline in sorted(pipelines.items()):
        pipeline_id = make_pipeline_id(src_conn, tgt_conn)
        pipeline_name = f"{src_conn} to {tgt_conn} (UDF entities)"
        yaml_data: Dict[str, Any] = {
            "exportMetadata": {
                "pipelineId": pipeline_id,
                "pipelineName": pipeline_name,
            },
            "schemas": pipeline["schemas"],
        }

        safe_name = "".join(c for c in pipeline_name if c.isalnum() or c in (" ", "-", "_")).rstrip()
        filename = f"backup_{safe_name}_{pipeline_id}_{timestamp}.yaml"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("# ---------------------------------------------------------------\n")
            f.write("# Gluesync Automator - Pipeline Configuration (generated)\n")
            f.write("# ---------------------------------------------------------------\n")
            f.write(f"# Generated from DbMoto metadata: {os.path.basename(xml_path)}\n")
            f.write(f"# Pipeline ID     : {pipeline_id}\n")
            f.write(f"# Pipeline name   : {pipeline_name}\n")
            f.write(f"# Generated at    : {datetime.now().isoformat()}\n")
            f.write("# Contains only the entities that require a field-mapping-to-function UDF.\n")
            f.write("# ---------------------------------------------------------------\n")
            yaml.dump(yaml_data, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
        yaml_files.append(filepath)
        n_tables = sum(len(s["tables"]["whitelist"]) for s in pipeline["schemas"].values())
        print(f"  Written pipeline YAML: {filename} ({n_tables} entities)")

        pipelines_meta.append({
            "pipelineId": pipeline_id,
            "pipelineName": pipeline_name,
            "_sourceConnection": src_conn,
            "_targetConnection": tgt_conn,
        })

    # agents-config.yaml skeleton (credentials must be filled in before import)
    agents_config = {
        "agents": [
            {
                "agentType": "SOURCE",
                "agentTag": "CHANGE_ME-source-agent-tag",
                "hostCredentials": {
                    "host": "CHANGE_ME",
                    "port": 0,
                    "username": "CHANGE_ME",
                    "password": "CHANGE_ME",
                },
                "customHostCredentials": {},
                "specificConfiguration": {},
            },
            {
                "agentType": "TARGET",
                "agentTag": "CHANGE_ME-target-agent-tag",
                "hostCredentials": {
                    "host": "CHANGE_ME",
                    "port": 0,
                    "username": "CHANGE_ME",
                    "password": "CHANGE_ME",
                },
                "customHostCredentials": {},
                "specificConfiguration": {},
            },
        ],
        "pipelines": pipelines_meta,
    }
    agents_path = os.path.join(output_dir, "agents-config.yaml")
    with open(agents_path, "w", encoding="utf-8") as f:
        f.write("# Skeleton agents configuration - EDIT BEFORE IMPORTING.\n")
        f.write("# Replace every CHANGE_ME value with the real agent tags and credentials\n")
        f.write("# of the Gluesync agents that will serve these pipelines.\n")
        yaml.dump(agents_config, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
    print(f"  Written agents-config skeleton: {agents_path}")

    # Warnings report
    report_path = os.path.join(output_dir, "udf_generation_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("DbMoto field-mapping-to-function UDF generation report\n")
        f.write("=" * 70 + "\n")
        f.write(f"Source XML: {xml_path}\n")
        f.write(f"Generated at: {datetime.now().isoformat()}\n\n")
        f.write(f"Entities with UDF: {stats['replications_with_udf']}\n")
        f.write(f"Expressions decoded: {stats['expressions_decoded']}\n")
        f.write(f"Expressions skipped: {stats['expressions_skipped']}\n")
        f.write(f"UDF Java files: {stats['udf_files']}\n\n")
        if all_warnings:
            f.write(f"WARNINGS ({len(all_warnings)}) - review before importing:\n")
            f.write("-" * 70 + "\n")
            for w in all_warnings:
                f.write(f"- {w}\n")
        else:
            f.write("No warnings.\n")
    print(f"  Written report: {report_path}")

    # ZIP package for Automator import
    if not cli.no_zip:
        zip_path = os.path.join(output_dir, f"automator_udf_import_{timestamp}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(agents_path, "agents-config.yaml")
            for yf in yaml_files:
                zf.write(yf, os.path.basename(yf))
            for java_file in sorted(os.listdir(udf_dir)):
                if java_file.endswith(".java"):
                    zf.write(os.path.join(udf_dir, java_file), f"udf-generated/{java_file}")
        print(f"\nImport package ready: {zip_path}")
        print("Edit agents-config.yaml inside the package (or regenerate after editing) before importing via Automator 'Import all'.")

    print(f"\nDone. {stats['udf_files']} UDFs, {stats['expressions_decoded']} expressions decoded, "
          f"{stats['expressions_skipped']} skipped, {len(all_warnings)} warnings.")
    if all_warnings:
        print(f"Review {report_path} for the warnings (format heuristics on 6-digit decimal dates etc.).")


if __name__ == "__main__":
    main()
