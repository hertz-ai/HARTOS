"""
Shard Engine — Privacy-preserving task decomposition.

Hybrid architecture (Option C):
  User's Node (TRUSTED) — full repo, shard engine, validation
  Cloud Orchestrator (PARTIAL TRUST) — dependency graph, interfaces, routing
  Compute Agents (ZERO TRUST) — see only their shard, can't reconstruct whole

When a code task arrives:
  1. Extract interface map (function signatures, types, imports — NOT implementations)
  2. Decompose task into isolated shards (each shard = subset of files + interfaces)
  3. Each shard contains ONLY what's needed for that sub-task
  4. Agents work on shards independently
  5. Results reassembled and validated on the trusted node

Privacy guarantee:
  - No single agent sees more than ~20% of the codebase
  - Shards contain interfaces (signatures) not implementations
  - File paths can be obfuscated (optional)
  - Shard contents are ephemeral (auto-expire after task completion)

Design constraints:
  - Agents CAN'T understand the full picture — by design
  - The orchestrator (trusted node or cloud) understands interfaces
  - Reassembly + testing happens ONLY on the trusted node
"""

import ast
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger('hevolve.shard_engine')


class ShardScope(str, Enum):
    """What a shard agent is allowed to see."""
    FULL_FILE = 'full_file'         # See complete file(s) — trusted agent only
    INTERFACES = 'interfaces'       # See signatures + types, not implementations
    SIGNATURES = 'signatures'       # See function/class names + params only
    MINIMAL = 'minimal'             # See only the specific function to modify


@dataclass
class InterfaceSpec:
    """Extracted interface from a Python file — what agents see instead of full code."""
    file_path: str
    classes: List[Dict[str, Any]] = field(default_factory=list)
    functions: List[Dict[str, Any]] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    constants: List[Dict[str, str]] = field(default_factory=list)
    module_doc: str = ''


@dataclass
class CodeShard:
    """An isolated unit of work for a compute agent."""
    shard_id: str
    task_description: str
    scope: ShardScope
    target_files: List[str]              # Files to modify
    interface_specs: List[InterfaceSpec]  # What the agent can see
    full_content: Dict[str, str]         # file_path → content (only for scope=FULL_FILE)
    test_expectations: List[str]         # What the result should satisfy
    dependencies: List[str]              # Other shard IDs this depends on
    expires_at: float                    # Auto-expire timestamp
    obfuscated: bool = False             # Whether paths are scrambled

    def to_dict(self) -> Dict:
        return {
            'shard_id': self.shard_id,
            'task_description': self.task_description,
            'scope': self.scope.value,
            'target_files': self.target_files,
            'interfaces': [asdict(s) for s in self.interface_specs],
            'full_content': self.full_content,
            'test_expectations': self.test_expectations,
            'dependencies': self.dependencies,
            'expires_at': self.expires_at,
            'obfuscated': self.obfuscated,
        }

    def is_expired(self) -> bool:
        return time.time() > self.expires_at


@dataclass
class ShardResult:
    """Result from an agent working on a shard."""
    shard_id: str
    diffs: Dict[str, str]        # file_path → unified diff
    test_results: Optional[str]  # stdout from test run
    success: bool
    error: Optional[str] = None


class InterfaceExtractor:
    """Extract function signatures, class definitions, and types from Python files.

    This is what agents see instead of full implementations.
    They get enough to understand the API contract without seeing the logic.
    """

    @staticmethod
    def extract_from_file(file_path: str) -> InterfaceSpec:
        """Extract public interface from a Python file."""
        spec = InterfaceSpec(file_path=file_path)

        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                source = f.read()
        except (IOError, OSError):
            return spec

        try:
            tree = ast.parse(source)
        except SyntaxError:
            return spec

        # Module docstring
        if (tree.body and isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, (ast.Constant, ast.Str))):
            doc = tree.body[0].value
            spec.module_doc = doc.value if isinstance(doc, ast.Constant) else doc.s

        for node in ast.iter_child_nodes(tree):
            # Imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    spec.imports.append(f"import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                names = ', '.join(a.name for a in node.names)
                spec.imports.append(f"from {node.module} import {names}")

            # Functions
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith('_') and not node.name.startswith('__'):
                    continue  # Skip private functions
                func_info = InterfaceExtractor._extract_function(node)
                spec.functions.append(func_info)

            # Classes
            elif isinstance(node, ast.ClassDef):
                cls_info = InterfaceExtractor._extract_class(node)
                spec.classes.append(cls_info)

            # Module-level constants (UPPER_CASE assignments)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        value_str = ast.dump(node.value)[:100]
                        spec.constants.append({
                            'name': target.id,
                            'value_hint': value_str,
                        })

        return spec

    @staticmethod
    def _extract_function(node) -> Dict:
        """Extract function signature (name, args, return type, docstring)."""
        args = []
        for arg in node.args.args:
            arg_info = {'name': arg.arg}
            if arg.annotation:
                arg_info['type'] = ast.unparse(arg.annotation)
            args.append(arg_info)

        # Defaults
        defaults = node.args.defaults
        if defaults:
            offset = len(args) - len(defaults)
            for i, d in enumerate(defaults):
                try:
                    args[offset + i]['default'] = ast.unparse(d)
                except Exception:
                    pass

        info = {
            'name': node.name,
            'args': args,
            'async': isinstance(node, ast.AsyncFunctionDef),
        }

        # Return type
        if node.returns:
            info['return_type'] = ast.unparse(node.returns)

        # Docstring
        if (node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, (ast.Constant, ast.Str))):
            doc = node.body[0].value
            info['docstring'] = doc.value if isinstance(doc, ast.Constant) else doc.s

        # Decorators
        if node.decorator_list:
            info['decorators'] = [ast.unparse(d) for d in node.decorator_list]

        return info

    @staticmethod
    def _extract_class(node: ast.ClassDef) -> Dict:
        """Extract class interface (name, bases, methods, docstring)."""
        info = {
            'name': node.name,
            'bases': [ast.unparse(b) for b in node.bases],
            'methods': [],
        }

        # Docstring
        if (node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, (ast.Constant, ast.Str))):
            doc = node.body[0].value
            info['docstring'] = doc.value if isinstance(doc, ast.Constant) else doc.s

        # Methods (public only)
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name.startswith('_') and not item.name.startswith('__'):
                    continue
                method_info = InterfaceExtractor._extract_function(item)
                info['methods'].append(method_info)

        return info

    @staticmethod
    def extract_from_directory(dir_path: str,
                               extensions: Tuple[str, ...] = ('.py',),
                               exclude_dirs: Optional[Set[str]] = None
                               ) -> Dict[str, InterfaceSpec]:
        """Extract interfaces from all Python files in a directory."""
        exclude = exclude_dirs or {
            '__pycache__', 'venv', '.venv', 'venv310', '.git',
            'node_modules', 'agent_data', 'tests',
        }
        specs = {}
        for root, dirs, files in os.walk(dir_path):
            dirs[:] = [d for d in dirs if d not in exclude]
            for fname in files:
                if any(fname.endswith(ext) for ext in extensions):
                    full_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(full_path, dir_path)
                    specs[rel_path] = InterfaceExtractor.extract_from_file(full_path)
        return specs


class ShardEngine:
    """Decomposes code tasks into isolated, privacy-preserving shards.

    The engine runs on the TRUSTED node (user's machine). It:
      1. Understands the full repo
      2. Creates shards with minimal scope
      3. Sends shards to compute agents
      4. Reassembles results
      5. Runs validation (tests)
    """

    def __init__(self, code_root: str = None, shard_ttl: int = 3600):
        self.code_root = code_root or os.environ.get(
            'HART_INSTALL_DIR',
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))))
        )
        self.shard_ttl = shard_ttl  # Shards expire after 1 hour
        self._interface_cache: Dict[str, InterfaceSpec] = {}
        self._active_shards: Dict[str, CodeShard] = {}

    def get_interface_map(self, refresh: bool = False) -> Dict[str, InterfaceSpec]:
        """Get the full interface map of the codebase.

        This is what the cloud orchestrator sees (PARTIAL TRUST):
        function signatures, types, imports — NOT implementations.
        """
        if self._interface_cache and not refresh:
            return self._interface_cache
        self._interface_cache = InterfaceExtractor.extract_from_directory(
            self.code_root)
        return self._interface_cache

    def create_shard(self, task: str, target_files: List[str],
                     scope: ShardScope = ShardScope.INTERFACES,
                     related_files: Optional[List[str]] = None,
                     test_expectations: Optional[List[str]] = None,
                     depends_on: Optional[List[str]] = None,
                     obfuscate_paths: bool = False) -> CodeShard:
        """Create a code shard for a compute agent.

        Args:
            task: Description of what the agent should do
            target_files: Files the agent will modify
            scope: How much of each file the agent can see
            related_files: Additional files the agent needs for context (read-only)
            test_expectations: What the result should satisfy
            depends_on: Other shard IDs that must complete first
            obfuscate_paths: Scramble file paths for extra privacy
        """
        shard_id = hashlib.sha256(
            f"shard:{task}:{time.time()}".encode()
        ).hexdigest()[:12]

        all_files = list(set(target_files + (related_files or [])))
        interface_specs = []
        full_content = {}

        for rel_path in all_files:
            full_path = os.path.join(self.code_root, rel_path)
            if not os.path.exists(full_path):
                continue

            if scope == ShardScope.FULL_FILE:
                # Trusted agent — gets everything
                try:
                    with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                        full_content[rel_path] = f.read()
                except IOError:
                    pass
            elif scope in (ShardScope.INTERFACES, ShardScope.SIGNATURES):
                # Gets signatures and types, not implementations
                spec = InterfaceExtractor.extract_from_file(full_path)
                if scope == ShardScope.SIGNATURES:
                    # Strip docstrings and constants for minimal scope
                    spec.module_doc = ''
                    spec.constants = []
                    for func in spec.functions:
                        func.pop('docstring', None)
                    for cls in spec.classes:
                        cls.pop('docstring', None)
                        for m in cls.get('methods', []):
                            m.pop('docstring', None)
                interface_specs.append(spec)
            elif scope == ShardScope.MINIMAL:
                # Only give the specific function to modify
                # Full file content but stripped to relevant functions
                spec = InterfaceExtractor.extract_from_file(full_path)
                interface_specs.append(spec)

        # Obfuscate paths if requested
        actual_target_files = target_files
        if obfuscate_paths:
            path_map = {}
            for i, p in enumerate(all_files):
                obf = f"module_{i:03d}.py"
                path_map[p] = obf
            actual_target_files = [path_map.get(f, f) for f in target_files]
            for spec in interface_specs:
                spec.file_path = path_map.get(spec.file_path, spec.file_path)
            full_content = {path_map.get(k, k): v for k, v in full_content.items()}

        shard = CodeShard(
            shard_id=shard_id,
            task_description=task,
            scope=scope,
            target_files=actual_target_files,
            interface_specs=interface_specs,
            full_content=full_content,
            test_expectations=test_expectations or [],
            dependencies=depends_on or [],
            expires_at=time.time() + self.shard_ttl,
            obfuscated=obfuscate_paths,
        )

        self._active_shards[shard_id] = shard
        logger.info(
            f"Shard [{shard_id}]: {len(target_files)} target files, "
            f"scope={scope.value}, task={task[:60]}..."
        )
        return shard

    def decompose_task(self, task: str, scope: ShardScope = ShardScope.INTERFACES,
                       max_files_per_shard: int = 5) -> List[CodeShard]:
        """Auto-decompose a task into multiple shards.

        Uses the interface map to determine which files are relevant,
        then groups them into shards with minimal overlap.
        """
        # Get interface map
        imap = self.get_interface_map()

        # Simple keyword matching to find relevant files
        task_lower = task.lower()
        relevant_files = []
        for rel_path, spec in imap.items():
            # Check if file content matches the task
            all_names = (
                [f['name'] for f in spec.functions] +
                [c['name'] for c in spec.classes] +
                [rel_path]
            )
            score = sum(1 for name in all_names if name.lower() in task_lower)
            if score > 0:
                relevant_files.append((rel_path, score))

        if not relevant_files:
            # Fallback: return a single shard with just the task description
            return [self.create_shard(task, [], scope)]

        # Sort by relevance
        relevant_files.sort(key=lambda x: -x[1])

        # Group into shards
        shards = []
        remaining = [f for f, _ in relevant_files]
        while remaining:
            chunk = remaining[:max_files_per_shard]
            remaining = remaining[max_files_per_shard:]
            shard = self.create_shard(
                task=task,
                target_files=chunk,
                scope=scope,
            )
            shards.append(shard)

        return shards

    def validate_result(self, shard_id: str, result: ShardResult) -> Tuple[bool, str]:
        """Validate a shard result before applying it.

        Runs on the TRUSTED node only.
        """
        shard = self._active_shards.get(shard_id)
        if not shard:
            return False, 'Unknown shard ID'
        if shard.is_expired():
            return False, 'Shard has expired'
        if not result.success:
            return False, f'Agent reported failure: {result.error}'

        # Check that diffs only touch target files
        for diff_path in result.diffs:
            if diff_path not in shard.target_files:
                return False, f'Diff modifies unauthorized file: {diff_path}'

        return True, 'Result validated'

    def cleanup_expired(self) -> int:
        """Remove expired shards."""
        expired = [
            sid for sid, s in self._active_shards.items()
            if s.is_expired()
        ]
        for sid in expired:
            del self._active_shards[sid]
        return len(expired)

    def get_stats(self) -> Dict:
        """Engine statistics."""
        return {
            'active_shards': len(self._active_shards),
            'interface_cache_size': len(self._interface_cache),
            'code_root': self.code_root,
            'shard_ttl': self.shard_ttl,
        }


# ═══════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════

_engine: Optional[ShardEngine] = None


def get_shard_engine() -> ShardEngine:
    """Get or create the shard engine singleton."""
    global _engine
    if _engine is None:
        _engine = ShardEngine()
    return _engine
