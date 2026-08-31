"""hartos — the HART OS backend implementation package.

Formerly 19 flat modules at the repo root; moved here 2026-08-30 so the
root holds only entry points (hart_intelligence_entry, hart_intelligence,
asgi, embedded_main, setup). Import as `from hartos import helper` /
`from hartos.create_recipe import ...`.

Deliberately empty of re-exports: importing this package must stay
weightless (helper alone pulls autogen + langchain, ~seconds).
"""
