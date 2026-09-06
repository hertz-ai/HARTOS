"""Guard: the VLM-agent scan reads the CANONICAL prompts dir, never a
CWD-relative literal.

Live root cause 2026-09-06 (agent 89555447799, installed build, reuse turn
13:47-14:06).  `execute_windows_or_android_command` executed 38 times and
failed 38 times — 100% — always at the same line:

    reuse_recipe.py:1629  for file in os.listdir(prompts_dir)
    FileNotFoundError: [WinError 3] The system cannot find the path
                       specified: 'prompts'

because :1622 hardcoded `prompts_dir = "prompts"`, a CWD-RELATIVE path.  The
installed app's CWD is C:\\Program Files (x86)\\HevolveAI\\Nunba\\, which has
no `prompts` subdirectory; the real recipe store is the deployment-aware
~/Documents/Nunba/data/prompts (2,144 files at the time of measurement).

The canonical resolver was already in scope in BOTH files — module-level
PROMPTS_DIR (reuse_recipe.py:94 imports it straight from hartos.helper, which
resolves it via core.platform_paths.get_recipe_prompts_dir) — and
reuse_recipe.py itself calls helper_fun.safe_prompt_path() 68 lines below the
broken literal.  So this was a second, wrong, path implementation living
inside a function that already used the right one (Gate-2/Gate-4).

Both faces of the same defect:
  * reuse_recipe.py:1629 — no exists() guard, so it RAISES; the model received
    "{'error': FileNotFoundError(2, 'The system cannot find the path
    specified')}" 32 times on the wire and rewrote-and-retried for 19 minutes
    because the message named no path it could act on.
  * create_recipe.py:1258 — guarded by os.path.exists(), so it does not raise;
    it silently finds ZERO vlm_agent files and moves on.  Silent wrong answer.

AST guard (no live llama needed): fail on ANY assignment binding a
*prompts-dir* name to a relative string constant, in either pipeline.  Written
generally so the next relative literal is caught too, not just this one.
"""
import ast
import os
import unittest


HARTOS = os.path.join(os.path.dirname(__file__), '..', '..')
TARGETS = {
    'reuse_recipe': os.path.join(HARTOS, 'hartos', 'reuse_recipe.py'),
    'create_recipe': os.path.join(HARTOS, 'hartos', 'create_recipe.py'),
    # helper.py owns load_vlm_agent_files -- the function reuse/create call on
    # the line AFTER their own scan -- and carried the SAME literal twice.
    'helper': os.path.join(HARTOS, 'hartos', 'helper.py'),
}

# Calls whose FIRST argument is a filesystem path, plus os.path.join, which is
# how the second helper.py site built its path.
_PATH_CALLS = {'listdir', 'exists', 'isdir', 'isfile', 'open', 'makedirs',
               'remove', 'walk', 'glob', 'scandir', 'rmtree', 'unlink', 'join'}
_MODES = {'r', 'w', 'a', 'rb', 'wb', 'ab', 'r+', 'w+', 'utf-8', 'utf8'}


def _call_name(node):
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ''


def _defines_the_resolver(call):
    """True when the call is the PROMPTS_DIR fallback itself.

    The three legitimate `'prompts'` literals are the code-relative fallbacks
    inside the PROMPTS_DIR definitions, which anchor on __file__.  Those are the
    resolver; everything else must consume PROMPTS_DIR.
    """
    return any(isinstance(n, ast.Name) and n.id == '__file__'
               for n in ast.walk(call))


def _relative_path_literals(path):
    """Every bare relative string literal handed to a path call, as (line, fn, value)."""
    tree = ast.parse(open(path, encoding='utf-8').read())
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = _call_name(node)
        if fn not in _PATH_CALLS or not node.args:
            continue
        if _defines_the_resolver(node):
            continue
        arg = node.args[0]
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            continue
        v = arg.value
        if not v or os.path.isabs(v) or v in _MODES or v.startswith(('http', '~')):
            continue
        out.add((arg.lineno, fn, v))
    return sorted(out)


class VlmPromptsDirCanonical(unittest.TestCase):
    def test_no_cwd_relative_path_literal(self):
        """A relative path literal resolves against the process CWD.

        In the frozen install that CWD is Program Files, so the path does not
        exist and the VLM scan either raises (reuse), silently finds nothing
        (create), or is swallowed by a broad except and returns [] (helper's
        load_vlm_agent_files -- measured: 45 swallowed errors, and ZERO
        'Found VLM agent recipe' lines, ever).

        Checks the whole call surface, not just assignments: the helper.py
        pair was an inline literal in os.listdir(...) and os.path.join(...),
        which an assignment-only guard would have missed.
        """
        for label, path in TARGETS.items():
            with self.subTest(module=label):
                bad = _relative_path_literals(path)
                self.assertEqual(
                    bad, [],
                    f"{label}: CWD-relative path literal(s) at {bad} — use the "
                    f"module-level canonical PROMPTS_DIR (hartos.helper, via "
                    f"core.platform_paths.get_recipe_prompts_dir) instead")

    def test_vlm_scan_uses_canonical_constant(self):
        """The scan must name PROMPTS_DIR, so there is ONE prompts-dir authority."""
        for label, path in TARGETS.items():
            with self.subTest(module=label):
                src = open(path, encoding='utf-8').read()
                self.assertIn(
                    'os.listdir(PROMPTS_DIR)', src,
                    f"{label}: the VLM-agent scan must list the canonical "
                    f"PROMPTS_DIR, not a locally-invented directory")

    def test_canonical_prompts_dir_is_absolute_and_exists(self):
        """PROMPTS_DIR is absolute and self-creating, so listdir cannot ENOENT.

        helper.py resolves it via core.platform_paths.get_recipe_prompts_dir()
        and calls os.makedirs(..., exist_ok=True) at import — which is exactly
        the property the relative literal lacked.
        """
        from hartos.helper import PROMPTS_DIR
        self.assertTrue(os.path.isabs(PROMPTS_DIR),
                        f"PROMPTS_DIR must be absolute, got {PROMPTS_DIR!r}")
        self.assertTrue(os.path.isdir(PROMPTS_DIR),
                        f"PROMPTS_DIR must exist after import, got {PROMPTS_DIR!r}")


if __name__ == '__main__':
    unittest.main()
