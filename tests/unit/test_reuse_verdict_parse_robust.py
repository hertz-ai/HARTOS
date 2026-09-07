"""Guard: the reuse main-group selector parses the StatusVerifier verdict with
the canonical retrieve_json (json / repair_json / ast / regex), NOT the naive
replace("'", '"') + \\{.*?\\} + json.loads.

Live root cause 2026-09-05 (Auto Research reuse 18088688973, installed build):
the verdict JSON carried an apostrophe (e.g. "the developer's tools").  The
naive replace("'", '"') turned it into a stray quote ("developer"s"), json.loads
died with "Expecting ',' delimiter", the 'completed' status was never read, and
the action looped 21x without advancing.  repair_json survives both apostrophes
and Python single-quote dicts; the sibling parse site in the same selector was
already migrated to retrieve_json (#95 Gate-1) — this is the missed twin.

AST guard (no live llama needed): the state_transition that parses the
StatusVerifier verdict must reference retrieve_json and must NOT re-introduce the
corrupting quote-swap.
"""
import ast
import os
import unittest


SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')


def _verdict_selector_body():
    src = open(SRC, encoding='utf-8').read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'state_transition':
            body = ast.get_source_segment(src, node)
            if body and 'Process JSON responses from StatusVerifier' in body:
                return body
    return ''


class ReuseVerdictParseRobust(unittest.TestCase):
    def setUp(self):
        self.body = _verdict_selector_body()
        self.assertTrue(self.body, "verdict-parsing state_transition not found")

    def test_uses_retrieve_json(self):
        self.assertIn('retrieve_json(messages[-1]["content"])', self.body,
                      "the verdict must be parsed with the canonical "
                      "retrieve_json, not bare json.loads")

    def test_no_naive_quote_swap(self):
        self.assertNotIn('replace("\'", \'"\')', self.body,
                         "the naive single->double quote swap corrupts "
                         "apostrophes in the verdict and must not return")


if __name__ == '__main__':
    unittest.main()
