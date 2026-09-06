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
}


def _relative_prompts_dir_assignments(path):
    """Every `*prompt*dir* = "<relative literal>"` in the file, as (line, value)."""
    tree = ast.parse(open(path, encoding='utf-8').read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            continue
        for tgt in node.targets:
            name = getattr(tgt, 'id', '')
            low = name.lower()
            if 'prompt' in low and 'dir' in low:
                if not os.path.isabs(node.value.value):
                    out.append((node.lineno, name, node.value.value))
    return out


class VlmPromptsDirCanonical(unittest.TestCase):
    def test_no_cwd_relative_prompts_dir_literal(self):
        """A relative prompts-dir literal resolves against the process CWD.

        In the frozen install that CWD is Program Files, so the path does not
        exist and every VLM scan either raises (reuse) or silently finds
        nothing (create).
        """
        for label, path in TARGETS.items():
            with self.subTest(module=label):
                bad = _relative_prompts_dir_assignments(path)
                self.assertEqual(
                    bad, [],
                    f"{label}: prompts dir bound to a CWD-relative literal at "
                    f"{bad} — use the module-level canonical PROMPTS_DIR "
                    f"(hartos.helper, via core.platform_paths."
                    f"get_recipe_prompts_dir) instead")

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
