"""The four places that declare llama-server's n_ctx must all say the same thing.

WHY A SOURCE GUARD AND NOT A BEHAVIOURAL TEST
─────────────────────────────────────────────
feedback_no_grep_tests is explicit: behavioural tests only, and source-shape guards
are acceptable ONLY for DRY enforcement across many files where a behavioural test
for a single call site cannot catch the regression. This is that case. The value is
declared in a Nix module default, a systemd unit, an env template and a Python
constant. Python can import exactly one of them. No behavioural test can observe a
Nix `lib.mkOption { default = ...; }` — it is evaluated by nix, on a Linux builder,
at image-build time. So the invariant is asserted against the declaring text, and
this file is named `test_source_guard_*` exactly as the rule requires.

The behavioural half of this contract is already covered: llm_outbound_logger's trim
tests exercise `_trim_body_for_ctx` against `_get_budget_per_slot()`. What they
CANNOT see is that the number that budget is derived from disagrees with what
llama-server was actually launched with — which is the bug this guards.

THE BUG IT GUARDS
─────────────────
core/constants.py's own comment says LLAMA_CTX_SIZE_DEFAULT "must match the
--ctx-size cmdline". It did not. constants.py said 12288; nixos/modules/hart-llm.nix
shipped 4096; deploy/linux/systemd/hart-llm.service hardcoded 4096 while
hart.env.template set HART_LLM_CTX_SIZE=4096 that nothing read.

The consequence was not a clean truncation. The wire-layer trim believed it had
12288 tokens, trimmed to a target ~3x larger than the server would accept, and
llama-server rejected the request — so the "zero-tolerance context overflow" guard
was computing against a ceiling that did not exist. Measured over 1,407 real
requests in ~/Documents/Nunba/logs/llm_outbound.jsonl on 2026-08-07: 78.7% of
requests overflowed at 4096, 2.1% at 12288. The overflow was NOT runaway history
(~95 tokens); it was a ~2,229-token system prompt plus a ~2,029-token task — a
two-message request born over the limit with nothing left to trim.
"""
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from core.constants import LLAMA_CTX_SIZE_DEFAULT  # noqa: E402

NIX_MODULE = os.path.join(REPO, 'nixos', 'modules', 'hart-llm.nix')
UNIT = os.path.join(REPO, 'deploy', 'linux', 'systemd', 'hart-llm.service')
ENV_TEMPLATE = os.path.join(REPO, 'deploy', 'linux', 'hart.env.template')


def _read(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


class EveryDeclarationOfNCtxAgrees(unittest.TestCase):
    """One conceptual value, four files. They must not drift."""

    def test_the_nix_module_default_equals_the_python_constant(self):
        """nixos/modules/hart-llm.nix is what the SHIPPED OS launches with."""
        src = _read(NIX_MODULE)
        m = re.search(r'contextSize\s*=\s*lib\.mkOption\s*\{(.*?)\n    \};',
                      src, re.S)
        self.assertIsNotNone(
            m, "could not find the contextSize option in hart-llm.nix — if it was "
               "renamed, update this guard rather than deleting it")
        d = re.search(r'^\s*default\s*=\s*(\d+)\s*;', m.group(1), re.M)
        self.assertIsNotNone(d, "contextSize has no literal integer default")
        self.assertEqual(
            LLAMA_CTX_SIZE_DEFAULT, int(d.group(1)),
            "hart-llm.nix contextSize disagrees with "
            "core/constants.py::LLAMA_CTX_SIZE_DEFAULT. The wire-trim layer budgets "
            "against the Python constant, so a smaller n_ctx here means it trims to "
            "a size llama-server will reject — the exact 78.7%-overflow bug measured "
            "on 2026-08-07. Change BOTH or neither.")

    def test_the_systemd_unit_reads_the_env_var_instead_of_hardcoding(self):
        """HART_LLM_CTX_SIZE was set in the template but never read."""
        unit = _read(UNIT)
        exec_line = next((ln for ln in unit.splitlines()
                          if ln.startswith('ExecStart=')), '')
        self.assertTrue(exec_line, "hart-llm.service has no ExecStart")
        self.assertNotRegex(
            exec_line, r'--ctx-size\s+\d',
            "ExecStart hardcodes a literal --ctx-size, so HART_LLM_CTX_SIZE in "
            "hart.env.template is dead config: an operator who sets it gets no "
            "effect and no warning. Read the variable instead.")
        self.assertIn(
            '--ctx-size ${HART_LLM_CTX_SIZE}', exec_line,
            "ExecStart must pass --ctx-size from HART_LLM_CTX_SIZE")

    def test_the_unit_does_not_use_the_shell_default_operator(self):
        """systemd expands ${VAR} but NOT ${VAR:-default} — it is not a shell.

        The unit is Type=simple with a direct exec (no `sh -c`), so `:-` was never
        interpreted as a default. Defaults must come from Environment= lines.
        """
        unit = _read(UNIT)
        exec_line = next((ln for ln in unit.splitlines()
                          if ln.startswith('ExecStart=')), '')
        self.assertNotIn(
            ':-', exec_line,
            "ExecStart uses shell-style ${VAR:-default}, which systemd does not "
            "support — it expands ${VAR} but has no default operator, and this unit "
            "is a direct exec with no shell. Put defaults in Environment= lines "
            "(EnvironmentFile= still overrides them).")

    def test_environment_defaults_are_declared_for_every_var_execstart_reads(self):
        """Otherwise a missing hart.env silently launches with empty arguments."""
        unit = _read(UNIT)
        exec_line = next((ln for ln in unit.splitlines()
                          if ln.startswith('ExecStart=')), '')
        referenced = set(re.findall(r'\$\{([A-Z_][A-Z0-9_]*)\}', exec_line))
        declared = set(re.findall(r'^Environment=([A-Z_][A-Z0-9_]*)=',
                                  unit, re.M))
        missing = referenced - declared
        self.assertFalse(
            missing,
            "ExecStart reads %s with no Environment= default. If "
            "/etc/hart/hart.env is missing or omits it, llama-server is launched "
            "with an EMPTY argument value." % sorted(missing))

    def test_the_env_template_agrees_too(self):
        """The template is what operators copy to /etc/hart/hart.env."""
        m = re.search(r'^HART_LLM_CTX_SIZE=(\d+)', _read(ENV_TEMPLATE), re.M)
        self.assertIsNotNone(m, "hart.env.template no longer sets HART_LLM_CTX_SIZE")
        self.assertEqual(
            LLAMA_CTX_SIZE_DEFAULT, int(m.group(1)),
            "hart.env.template's HART_LLM_CTX_SIZE disagrees with "
            "core/constants.py::LLAMA_CTX_SIZE_DEFAULT — an operator deploying the "
            "template would silently reintroduce the overflow.")


class TheConstantIsBigEnoughForRealTraffic(unittest.TestCase):
    """A regression back to a too-small ceiling must fail loudly, not silently."""

    #: Measured 2026-08-07 over 1,407 requests in llm_outbound.jsonl. The average
    #: OVERFLOWING request was ~4,642 tokens (system ~2,229 + task ~2,029 + tools
    #: ~287 + history ~95). A ceiling at or below this cannot serve the pipeline's
    #: own recipe prompts at all.
    MEASURED_AVG_OVERFLOWING_REQUEST_TOKENS = 4642

    def test_n_ctx_clears_the_measured_average_request(self):
        self.assertGreater(
            LLAMA_CTX_SIZE_DEFAULT, self.MEASURED_AVG_OVERFLOWING_REQUEST_TOKENS,
            "LLAMA_CTX_SIZE_DEFAULT is at or below the measured average request "
            "size, so the recipe pipeline cannot run. This is how 4096 shipped.")

    def test_a_single_request_still_fits_after_the_safety_margin(self):
        """The usable budget is n_ctx/slots - max_tokens - safety, not n_ctx."""
        from core.constants import (LLAMA_SLOTS_DEFAULT,
                                    WIRE_TRIM_SAFETY_MARGIN_TOKENS)
        typical_max_tokens = 2048
        budget = (LLAMA_CTX_SIZE_DEFAULT // LLAMA_SLOTS_DEFAULT
                  - typical_max_tokens - WIRE_TRIM_SAFETY_MARGIN_TOKENS)
        self.assertGreater(
            budget, self.MEASURED_AVG_OVERFLOWING_REQUEST_TOKENS,
            "after reserving max_tokens + the safety margin there is not enough "
            "room for a typical request, so the trim layer would have to cut into "
            "the task itself — producing a truncated prompt rather than an honest "
            "failure.")


if __name__ == '__main__':
    unittest.main()
