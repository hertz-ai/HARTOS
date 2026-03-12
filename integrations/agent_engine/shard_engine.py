"""
Shard Engine — Context-scoped task decomposition.

When a code task arrives:
  1. Identify target function(s) to modify
  2. Build call-chain context: upstream callers + downstream callees (FULL source)
  3. Everything else: interfaces only (signatures + types + docstrings)
  4. Agent sees enough to code accurately, exposure proportional to task

Context model (CALL_CHAIN scope):
  Target function:     FULL implementation (what you're modifying)
  Upstream callers:    FULL implementation (who calls it, input contracts)
  Downstream callees:  FULL implementation (what it calls, output contracts)
  Everything else:     Interfaces only (signatures + types)

Security model:
  - Exposure proportional to task, not whole codebase
  - E2E encrypted between nodes
  - Trust-based peer access (SAME_USER / autotrust)
  - Validation on trusted node before applying diffs
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
    CALL_CHAIN = 'call_chain'       # Target func + upstream/downstream FULL, rest interfaces
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


class CallGraphExtractor:
    """Extract upstream callers and downstream callees for a target function.

    Primary: Trueflow MCP (port 5681) — has runtime call tree + coverage.
    Fallback: AST-based static analysis — walks function bodies for Name references.
    """

    @staticmethod
    def get_call_chain(file_path: str, function_name: str,
                       code_root: str = '') -> Dict[str, List[str]]:
        """Get upstream (callers) and downstream (callees) for a function.

        Returns:
            {'target': 'func_name',
             'upstream': ['caller1', 'caller2'],
             'downstream': ['callee1', 'callee2'],
             'source': 'trueflow' | 'ast'}
        """
        # Try Trueflow MCP first (richer: runtime coverage + dynamic call tree)
        try:
            chain = CallGraphExtractor._from_trueflow(file_path, function_name)
            if chain.get('upstream') or chain.get('downstream'):
                return chain
        except Exception:
            pass

        # Fallback: static AST analysis
        return CallGraphExtractor._from_ast(file_path, function_name, code_root)

    @staticmethod
    def _from_trueflow(file_path: str, function_name: str) -> Dict:
        """Get call chain from Trueflow MCP (IDE must be running)."""
        from core.http_pool import pooled_post
        trueflow_url = os.environ.get('TRUEFLOW_MCP_URL',
                                       'http://localhost:5681')
        resp = pooled_post(
            f'{trueflow_url}/analyze_call_tree',
            json={'file': file_path, 'function': function_name},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                'target': function_name,
                'upstream': data.get('callers', []),
                'downstream': data.get('callees', []),
                'source': 'trueflow',
            }
        return {'target': function_name, 'upstream': [], 'downstream': [],
                'source': 'trueflow'}

    @staticmethod
    def _from_ast(file_path: str, function_name: str,
                  code_root: str = '') -> Dict:
        """Static call graph via AST. Scans target file + importers."""
        result = {
            'target': function_name,
            'upstream': [],
            'downstream': [],
            'source': 'ast',
        }

        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                source = f.read()
            tree = ast.parse(source)
        except (IOError, SyntaxError):
            return result

        all_functions = {}  # name → ast node
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                all_functions[node.name] = node

        # Downstream: what does the target function call?
        target_node = all_functions.get(function_name)
        if target_node:
            for node in ast.walk(target_node):
                if isinstance(node, ast.Call):
                    callee = None
                    if isinstance(node.func, ast.Name):
                        callee = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        callee = node.func.attr
                    if callee and callee != function_name:
                        if callee not in result['downstream']:
                            result['downstream'].append(callee)

        # Upstream: which functions in this file call the target?
        for fname, fnode in all_functions.items():
            if fname == function_name:
                continue
            for node in ast.walk(fnode):
                if isinstance(node, ast.Call):
                    callee = None
                    if isinstance(node.func, ast.Name):
                        callee = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        callee = node.func.attr
                    if callee == function_name:
                        if fname not in result['upstream']:
                            result['upstream'].append(fname)
                        break

        # Cross-file upstream: scan other files that import from this module
        if code_root:
            target_module = os.path.relpath(file_path, code_root)
            target_module = target_module.replace(os.sep, '.').rstrip('.py')
            result['upstream'].extend(
                CallGraphExtractor._find_cross_file_callers(
                    code_root, target_module, function_name))

        return result

    @staticmethod
    def _find_cross_file_callers(code_root: str, target_module: str,
                                  function_name: str) -> List[str]:
        """Find functions in other files that import and call the target."""
        callers = []
        exclude = {'__pycache__', 'venv', '.venv', 'venv310', '.git',
                    'node_modules', 'agent_data', 'tests', 'build'}
        for root, dirs, files in os.walk(code_root):
            dirs[:] = [d for d in dirs if d not in exclude]
            for fname in files:
                if not fname.endswith('.py'):
                    continue
                full_path = os.path.join(root, fname)
                try:
                    with open(full_path, 'r', encoding='utf-8',
                              errors='ignore') as f:
                        src = f.read()
                    tree = ast.parse(src)
                except (IOError, SyntaxError):
                    continue

                # Check if this file imports our target function
                imports_target = False
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        if node.module and target_module in node.module:
                            for alias in node.names:
                                if alias.name == function_name:
                                    imports_target = True
                                    break
                    if imports_target:
                        break
                if not imports_target:
                    continue

                # Find which functions call it
                rel = os.path.relpath(full_path, code_root)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for child in ast.walk(node):
                            if isinstance(child, ast.Call):
                                cname = None
                                if isinstance(child.func, ast.Name):
                                    cname = child.func.id
                                elif isinstance(child.func, ast.Attribute):
                                    cname = child.func.attr
                                if cname == function_name:
                                    caller_id = f"{rel}:{node.name}"
                                    if caller_id not in callers:
                                        callers.append(caller_id)
                                    break
                if len(callers) >= 20:  # Cap cross-file search
                    break
        return callers

    @staticmethod
    def extract_function_source(file_path: str, function_name: str) -> str:
        """Extract the full source code of a specific function from a file."""
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            source = ''.join(lines)
            tree = ast.parse(source)
        except (IOError, SyntaxError):
            return ''

        for node in ast.walk(tree):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == function_name):
                start = node.lineno - 1
                end = getattr(node, 'end_lineno', start + 1)
                return ''.join(lines[start:end])
        return ''


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
    """Context-scoped task decomposition engine.

    Runs on the TRUSTED node (user's machine). Creates shards with
    context proportional to the task — not the whole codebase.

    CALL_CHAIN scope (default for distributed work):
      Target function: FULL source
      Upstream callers: FULL source (input contracts)
      Downstream callees: FULL source (output contracts)
      Everything else: Interfaces only (signatures + types)

    Call graph from Trueflow MCP (when IDE running) or AST fallback.
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

    def create_call_chain_shard(self, task: str, target_file: str,
                                target_function: str) -> CodeShard:
        """Create a shard with call-chain context for a specific function.

        Context model:
          Target function:    FULL source
          Upstream callers:   FULL source (who calls it)
          Downstream callees: FULL source (what it calls)
          Same-file others:   Interfaces only
          Other files:        Interfaces only (imported modules)

        Uses Trueflow MCP (analyze_call_tree) when IDE is running,
        falls back to AST-based static analysis on headless nodes.
        """
        shard_id = hashlib.sha256(
            f"cc:{target_file}:{target_function}:{time.time()}".encode()
        ).hexdigest()[:12]

        full_path = os.path.join(self.code_root, target_file)

        # Get call chain (Trueflow → AST fallback)
        chain = CallGraphExtractor.get_call_chain(
            full_path, target_function, self.code_root)

        # Build context: full source for call chain, interfaces for rest
        full_content = {}
        interface_specs = []

        # 1. Target function — FULL source
        target_src = CallGraphExtractor.extract_function_source(
            full_path, target_function)
        if target_src:
            full_content[f'{target_file}::{target_function}'] = target_src

        # 2. Upstream callers — FULL source
        for caller in chain.get('upstream', []):
            if ':' in caller:  # Cross-file: "path/file.py:func_name"
                cfile, cfunc = caller.rsplit(':', 1)
                cfull = os.path.join(self.code_root, cfile)
                src = CallGraphExtractor.extract_function_source(cfull, cfunc)
                if src:
                    full_content[f'{cfile}::{cfunc}'] = src
            else:  # Same file
                src = CallGraphExtractor.extract_function_source(
                    full_path, caller)
                if src:
                    full_content[f'{target_file}::{caller}'] = src

        # 3. Downstream callees — FULL source (same file only;
        #    cross-file callees get interfaces via imports)
        for callee in chain.get('downstream', []):
            src = CallGraphExtractor.extract_function_source(
                full_path, callee)
            if src:
                full_content[f'{target_file}::{callee}'] = src

        # 4. Same-file interfaces (everything NOT in the call chain)
        spec = InterfaceExtractor.extract_from_file(full_path)
        call_chain_names = (
            {target_function} |
            set(chain.get('upstream', [])) |
            set(chain.get('downstream', []))
        )
        # Strip cross-file references from the set
        call_chain_names = {
            n.split(':')[-1] if ':' in n else n for n in call_chain_names
        }
        # Keep only interface entries for functions NOT in call chain
        spec.functions = [
            f for f in spec.functions if f['name'] not in call_chain_names
        ]
        interface_specs.append(spec)

        # 5. Imported module interfaces (downstream cross-file context)
        for imp in spec.imports:
            # Resolve "from X import Y" to file path
            if imp.startswith('from '):
                parts = imp.split()
                if len(parts) >= 2:
                    mod_path = parts[1].replace('.', os.sep) + '.py'
                    mod_full = os.path.join(self.code_root, mod_path)
                    if os.path.exists(mod_full):
                        mod_spec = InterfaceExtractor.extract_from_file(mod_full)
                        interface_specs.append(mod_spec)

        shard = CodeShard(
            shard_id=shard_id,
            task_description=task,
            scope=ShardScope.CALL_CHAIN,
            target_files=[target_file],
            interface_specs=interface_specs,
            full_content=full_content,
            test_expectations=[],
            dependencies=[],
            expires_at=time.time() + self.shard_ttl,
        )
        self._active_shards[shard_id] = shard

        logger.info(
            f"Call-chain shard [{shard_id}]: {target_file}::{target_function}, "
            f"{len(chain.get('upstream', []))} upstream, "
            f"{len(chain.get('downstream', []))} downstream, "
            f"source={chain.get('source', 'unknown')}")
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
