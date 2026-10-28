"""The log redaction of import_plan: the split key/value patterns redact exactly what the former single regex did."""

import os
import random
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import import_plan  # noqa: E402

# The single pattern used before the split (kept here only as the reference the new code is compared with).
_FORMER_KEY_VALUE = re.compile(r"""(?ix)
    (["']?\b(?:password|passwd|pwd|secret|agent_passwords)\b["']?\s*[:=]\s*)
    ("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s,;}\]]+)""")


def _former(text: str) -> str:
    return _FORMER_KEY_VALUE.sub(lambda m: m.group(1) + "***", text)


CASES = {
    "password=hunter2": "password=***",
    "PASSWORD: hunter2 next": "PASSWORD: *** next",
    'login {"password": "a \\"quoted\\" pw", "user": "u"}': 'login {"password": ***, "user": "u"}',
    "{'pwd': 'it\\'s', 'x': 1}": "{'pwd': ***, 'x': 1}",
    "secret=abc;passwd=def,other": "secret=***;passwd=***,other",
    "agent_passwords={...}": "agent_passwords=***}",
    "passwords=abc": "passwords=abc",
    "mypassword=abc": "mypassword=abc",
    "password = ": "password = ",
    "password=": "password=",
    'password="unterminated value': 'password=*** value',
    "password: , secret: x": "password: , secret: ***",
    "no secrets here": "no secrets here",
    "pwd=a pwd=b pwd=c": "pwd=*** pwd=*** pwd=***",
    "password=x]rest": "password=***]rest",
    "Password:\n  value": "Password:\n  ***",
    "password='a\\\nb'": "password=***\nb'",
}


class RedactionTests(unittest.TestCase):
    def test_known_cases(self):
        for text, expected in CASES.items():
            with self.subTest(text=text):
                self.assertEqual(import_plan.SecretRegistry().redact(text), expected)

    def test_known_cases_match_the_former_pattern(self):
        for text in CASES:
            with self.subTest(text=text):
                self.assertEqual(import_plan._redact_key_values(text), _former(text))

    def test_random_texts_match_the_former_pattern(self):
        rnd = random.Random(1234)
        pieces = ["password", "PassWd", "pwd", "secret", "agent_passwords", "passwords", "x", "_", "'", '"',
                  "\\", ":", "=", " ", "\n", "\t", ",", ";", "}", "]", "{", "abc", "1", "*"]
        for _ in range(20000):
            text = "".join(rnd.choice(pieces) for _ in range(rnd.randint(0, 12)))
            self.assertEqual(import_plan._redact_key_values(text), _former(text), repr(text))

    def test_registered_values_are_hidden(self):
        registry = import_plan.SecretRegistry()
        registry.add(1, ["s3cr3t-value", "ab"])
        self.assertEqual(registry.redact("token s3cr3t-value and ab"), "token *** and ab")
        registry.discard(1)
        self.assertEqual(registry.redact("token s3cr3t-value"), "token s3cr3t-value")


if __name__ == "__main__":
    unittest.main()
