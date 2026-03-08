"""
Extension Sandbox — AST-based static analysis for extension safety.

Analyzes extension Python source BEFORE import to block dangerous patterns:
subprocess, eval, exec, os.system, __import__, ctypes.

Zero external dependencies — uses only stdlib ast + hashlib.

Usage:
    from core.platform.extension_sandbox import ExtensionSandbox
    safe, violations = ExtensionSandbox.analyze_file('extensions/my_ext.py')
    if not safe:
        raise SecurityError(f"Blocked: {violations}")
"""

import ast
import hashlib
import logging
from typing import List, Tuple

logger = logging.getLogger('hevolve.platform.sandbox')

# ── Blocked patterns ─────────────────────────────────────────────
# Built-in functions that allow arbitrary code execution
BLOCKED_CALLS = frozenset({
    'eval', 'exec', 'compile', '__import__',
})

# Modules that provide process/memory escape hatches
BLOCKED_MODULES = frozenset({
    'subprocess', 'ctypes', 'multiprocessing',
})

# Specific attribute chains that perform dangerous operations
BLOCKED_ATTRIBUTES = frozenset({
    'os.system',
    'os.popen',
    'os.execl',
    'os.execle',
    'os.execv',
    'os.execve',
    'os.spawnl',
    'os.spawnle',
    'subprocess.run',
    'subprocess.call',
    'subprocess.Popen',
    'subprocess.check_output',
    'subprocess.check_call',
    'shutil.rmtree',
})


class _BlockedNodeVisitor(ast.NodeVisitor):
    """AST visitor that collects violations of the sandbox policy."""

    def __init__(self):
        self.violations: List[str] = []

    # ── Blocked built-in calls ────────────────────────────────
    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Name):
            if node.func.id in BLOCKED_CALLS:
                self.violations.append(
                    f"Blocked call: {node.func.id}() at line {node.lineno}")
        elif isinstance(node.func, ast.Attribute):
            dotted = _reconstruct_dotted(node.func)
            if dotted and dotted in BLOCKED_ATTRIBUTES:
                self.violations.append(
                    f"Blocked attribute call: {dotted}() at line {node.lineno}")
        self.generic_visit(node)

    # ── Blocked imports: import X ─────────────────────────────
    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            top = alias.name.split('.')[0]
            if top in BLOCKED_MODULES:
                self.violations.append(
                    f"Blocked import: {alias.name} at line {node.lineno}")
        self.generic_visit(node)

    # ── Blocked imports: from X import Y ──────────────────────
    def visit_ImportFrom(self, node: ast.ImportFrom):
        module = node.module or ''
        top = module.split('.')[0]
        if top in BLOCKED_MODULES:
            self.violations.append(
                f"Blocked import: from {module} at line {node.lineno}")
        else:
            # Check for specific attributes like 'from os import system'
            for alias in (node.names or []):
                full = f"{module}.{alias.name}" if module else alias.name
                if full in BLOCKED_ATTRIBUTES:
                    self.violations.append(
                        f"Blocked attribute import: {full} at line {node.lineno}")
        self.generic_visit(node)

    # ── Blocked attribute access ──────────────────────────────
    def visit_Attribute(self, node: ast.Attribute):
        dotted = _reconstruct_dotted(node)
        if dotted and dotted in BLOCKED_ATTRIBUTES:
            self.violations.append(
                f"Blocked attribute access: {dotted} at line {node.lineno}")
        self.generic_visit(node)


def _reconstruct_dotted(node: ast.Attribute) -> str:
    """Reconstruct a dotted name from an ast.Attribute chain.

    E.g., os.system -> 'os.system', subprocess.Popen -> 'subprocess.Popen'.
    Returns empty string if the chain contains non-Name/non-Attribute nodes.
    """
    parts = [node.attr]
    current = node.value
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return '.'.join(reversed(parts))
    return ''


class ExtensionSandbox:
    """Static analysis sandbox for extension Python source code."""

    @staticmethod
    def analyze_source(source: str) -> Tuple[bool, List[str]]:
        """Analyze Python source code for blocked patterns.

        Args:
            source: Python source code string.

        Returns:
            (is_safe, violations) — True if no violations, else list of
            human-readable violation descriptions.
        """
        if not source or not source.strip():
            return (True, [])

        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return (False, [f"SyntaxError: {e}"])

        visitor = _BlockedNodeVisitor()
        visitor.visit(tree)
        return (len(visitor.violations) == 0, visitor.violations)

    @staticmethod
    def analyze_file(file_path: str) -> Tuple[bool, List[str]]:
        """Analyze a Python file for blocked patterns.

        Args:
            file_path: Path to a .py file.

        Returns:
            (is_safe, violations).
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source = f.read()
        except FileNotFoundError:
            return (False, [f"FileNotFoundError: {file_path}"])
        except (UnicodeDecodeError, OSError) as e:
            return (False, [f"ReadError: {e}"])

        return ExtensionSandbox.analyze_source(source)

    @staticmethod
    def verify_signature(file_path: str, signature_hex: str) -> bool:
        """Verify an Ed25519 signature on a file using the master public key.

        The signature is verified against the raw file bytes.

        Args:
            file_path: Path to the file.
            signature_hex: Hex-encoded Ed25519 signature.

        Returns:
            True if the signature is valid.
        """
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
            content_hash = hashlib.sha256(content).digest()
            from security.master_key import get_master_public_key
            pub = get_master_public_key()
            sig = bytes.fromhex(signature_hex)
            pub.verify(sig, content_hash)
            return True
        except Exception:
            return False

    @staticmethod
    def compute_source_hash(source: str) -> str:
        """Compute SHA-256 hex digest of source code.

        Args:
            source: Python source code string.

        Returns:
            64-character hex digest.
        """
        return hashlib.sha256(source.encode('utf-8')).hexdigest()

    @staticmethod
    def check_permission_declarations(source: str) -> List[str]:
        """Extract EXTENSION_PERMISSIONS declarations from source.

        Looks for top-level assignments like:
            EXTENSION_PERMISSIONS = ['events.theme.*', 'config.read']

        Args:
            source: Python source code string.

        Returns:
            List of permission strings, or empty list if not found.
        """
        if not source or not source.strip():
            return []

        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []

        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == 'EXTENSION_PERMISSIONS':
                    if isinstance(node.value, ast.List):
                        perms = []
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                perms.append(elt.value)
                            # Python 3.7 compat: ast.Str
                            elif isinstance(elt, ast.Str):
                                perms.append(elt.s)
                        return perms
        return []
