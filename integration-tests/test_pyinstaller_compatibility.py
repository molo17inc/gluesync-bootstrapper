#!/usr/bin/env python3
"""
PyInstaller Compatibility Tests

These tests ensure the codebase remains compatible with PyInstaller frozen executables.
PyInstaller evaluates type hints at runtime, so Python 3.10+ union type syntax (X | Y)
causes "TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'" errors.

Use Optional[X] instead of X | None for compatibility.
"""

import ast
import os
import sys
import unittest
from pathlib import Path
from typing import List, Tuple


class PyInstallerCompatibilityChecker(ast.NodeVisitor):
    """AST visitor to detect problematic type hints."""
    
    def __init__(self):
        self.violations: List[Tuple[int, str, str]] = []
        self.current_file: str = ""
    
    def visit_BinOp(self, node: ast.BinOp):
        """Detect X | Y union type syntax (Python 3.10+)."""
        if isinstance(node.op, ast.BitOr):
            # Check if either operand is a type-like expression
            left_is_type = self._is_type_expression(node.left)
            right_is_type = self._is_type_expression(node.right)
            
            if left_is_type or right_is_type:
                line = node.lineno
                code = self._get_source_line(node)
                self.violations.append((line, code.strip(), "Uses '|' union syntax instead of Optional[] or Union[]"))
        
        self.generic_visit(node)
    
    def _is_type_expression(self, node: ast.AST) -> bool:
        """Check if a node looks like a type expression."""
        if isinstance(node, ast.Name):
            # Common type names
            type_names = {'None', 'int', 'str', 'float', 'bool', 'list', 'dict', 'set', 'tuple'}
            return node.id in type_names or node.id[0].isupper()
        elif isinstance(node, ast.Constant) and node.value is None:
            return True
        elif isinstance(node, ast.Subscript):
            return True
        elif isinstance(node, ast.Attribute):
            return True
        elif isinstance(node, ast.BinOp):
            # Nested union types
            return isinstance(node.op, ast.BitOr)
        return False
    
    def _get_source_line(self, node: ast.AST) -> str:
        """Get the source line for a node."""
        try:
            with open(self.current_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                if 1 <= node.lineno <= len(lines):
                    return lines[node.lineno - 1]
        except Exception:
            pass
        return "<unable to read source>"


def check_file_for_pyinstaller_issues(filepath: Path) -> List[Tuple[int, str, str]]:
    """Check a Python file for PyInstaller incompatible syntax."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            source = f.read()
    except Exception as e:
        return [(0, f"<error reading file: {e}>", "")]
    
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [(e.lineno or 0, f"<syntax error: {e}>", "")]
    
    checker = PyInstallerCompatibilityChecker()
    checker.current_file = str(filepath)
    checker.visit(tree)
    return checker.violations


class TestPyInstallerCompatibility(unittest.TestCase):
    """Test suite for PyInstaller compatibility."""
    
    def setUp(self):
        """Set up test paths."""
        self.project_root = Path(__file__).parent.parent
        self.python_files = self._find_python_files()
    
    def _find_python_files(self) -> List[Path]:
        """Find all Python files in the project (excluding tests and build dirs)."""
        exclude_dirs = {
            '__pycache__', 'build', 'dist', '.git', '.venv', 'venv',
            'gluesync-client-sdk', 'dbmoto-converter'
        }
        
        python_files = []
        for path in self.project_root.rglob('*.py'):
            # Skip excluded directories
            if any(part in exclude_dirs for part in path.parts):
                continue
            python_files.append(path)
        
        return sorted(python_files)
    
    def test_no_union_type_syntax_in_type_hints(self):
        """
        Ensure no Python 3.10+ union type syntax (X | Y) is used.
        
        PyInstaller evaluates type hints at runtime, causing:
        TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'
        
        Use Optional[X] or Union[X, Y] instead.
        """
        all_violations = []
        
        for filepath in self.python_files:
            violations = check_file_for_pyinstaller_issues(filepath)
            for line_no, code, reason in violations:
                all_violations.append(
                    f"\n  {filepath}:{line_no}\n    {code}\n    -> {reason}"
                )
        
        if all_violations:
            message = (
                "Found PyInstaller incompatible syntax.\n"
                "Use 'Optional[X]' instead of 'X | None' or 'Union[X, Y]' instead of 'X | Y'.\n"
                "Violations:"
            ) + ''.join(all_violations)
            self.fail(message)
    
    def test_specific_known_problematic_patterns(self):
        """
        Check for specific patterns known to cause PyInstaller issues.
        """
        problematic_patterns = [
            (r':\s*\w+\s*\|\s*None', "X | None syntax"),
            (r'->\s*\w+\s*\|\s*None', "return type X | None syntax"),
        ]
        
        import re
        issues_found = []
        
        for filepath in self.python_files:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                    lines = content.split('\n')
                    
                    for line_no, line in enumerate(lines, 1):
                        for pattern, description in problematic_patterns:
                            if re.search(pattern, line):
                                issues_found.append(
                                    f"{filepath}:{line_no} - {description}\n  {line.strip()}"
                                )
            except Exception:
                continue
        
        if issues_found:
            self.fail(
                "Found problematic patterns:\n" + '\n'.join(issues_found)
            )


class TestPyInstallerBuildIntegrity(unittest.TestCase):
    """
    Tests that would run after a PyInstaller build to verify integrity.
    These are meant to be run in CI after building the executable.
    """
    
    @unittest.skipUnless(
        os.environ.get('PYINSTALLER_BUILD_TEST') == 'true',
        "Only runs in PyInstaller build test environment"
    )
    def test_imports_work_in_frozen_executable(self):
        """
        Test that critical imports work in the frozen executable.
        
        This test runs after PyInstaller build to verify the frozen
        executable can import all necessary modules without errors.
        """
        critical_modules = [
            'commons',
            'create_all_entities',
            'create_user_defined_functions',
            'main',
            'data_type_matrices',
        ]
        
        failed_imports = []
        for module_name in critical_modules:
            try:
                __import__(module_name)
            except Exception as e:
                failed_imports.append(f"{module_name}: {e}")
        
        if failed_imports:
            self.fail(f"Failed imports in frozen executable:\n" + '\n'.join(failed_imports))


def run_syntax_check():
    """Run a quick syntax check without unittest framework."""
    project_root = Path(__file__).parent.parent
    exclude_dirs = {'__pycache__', 'build', 'dist', '.git', 'gluesync-client-sdk', 'dbmoto-converter'}
    
    violations_found = False
    
    for path in project_root.rglob('*.py'):
        if any(part in exclude_dirs for part in path.parts):
            continue
        
        violations = check_file_for_pyinstaller_issues(path)
        if violations:
            violations_found = True
            print(f"\n{path}:")
            for line_no, code, reason in violations:
                print(f"  Line {line_no}: {reason}")
                print(f"    {code[:80]}")
    
    return 1 if violations_found else 0


if __name__ == '__main__':
    # Allow running as a script for quick checks
    if len(sys.argv) > 1 and sys.argv[1] == '--quick-check':
        sys.exit(run_syntax_check())
    
    # Run unittest suite
    unittest.main(verbosity=2)
