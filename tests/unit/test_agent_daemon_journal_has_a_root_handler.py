"""The systemd entrypoint gives the journal a root handler when nobody else has.

hart-agent.nix runs `python -c "... AgentDaemon().run_forever()"`. That process
configured no logging, so its INFO lines (ticks, yield reasons, governor
transitions) went to Python's lastResort handler and were dropped. Measured on
the Samsung node, generations 11 and 12 (2026-09-24): one journal line per boot.
These pin the helper run_forever calls first, on a private Logger.
"""
import logging

from integrations.agent_engine import agent_daemon as ad


def test_the_daemon_and_the_entry_share_one_rule():
    import inspect
    import core.log_handlers as lh
    src = inspect.getsource(ad._give_the_journal_a_root_handler)
    assert 'install_console_handler_if_bare' in src, 'the daemon must call the shared rule, not carry its own copy'
    assert ad._HARTOS_HANDLER_TAG == lh.HARTOS_HANDLER_TAG
    import hart_intelligence_entry as hie
    assert hie._HARTOS_HANDLER_TAG == lh.HARTOS_HANDLER_TAG
    assert 'install_console_handler_if_bare' in inspect.getsource(hie._install_root_handlers)


def test_a_bare_root_gets_one_tagged_stdout_handler_at_info():
    root = logging.Logger('daemon-root-under-test')
    h = ad._give_the_journal_a_root_handler(root)
    assert h is not None
    assert root.handlers == [h]
    assert getattr(h, ad._HARTOS_HANDLER_TAG, False)
    assert root.level == logging.INFO


def test_a_root_someone_else_configured_is_left_alone():
    root = logging.Logger('daemon-root-under-test-2')
    theirs = logging.NullHandler()
    root.addHandler(theirs)
    assert ad._give_the_journal_a_root_handler(root) is None
    assert root.handlers == [theirs]


def test_an_info_line_reaches_the_stream():
    import io
    root = logging.Logger('daemon-root-under-test-3')
    h = ad._give_the_journal_a_root_handler(root)
    buf = io.StringIO()
    h.setStream(buf)
    root.info('yield gate CLOSED: user_present')
    assert 'user_present' in buf.getvalue()


def test_run_forever_installs_it_before_anything_else():
    import inspect
    src = inspect.getsource(ad.AgentDaemon.run_forever)
    body = src.split('"""')[2]  # after the docstring
    assert body.lstrip().startswith('_give_the_journal_a_root_handler()'), body[:120]


def test_the_shell_server_applies_the_same_rule_first():
    """hart-liquid-ui.nix starts the shell as `python -c "...
    LiquidUIService(...).serve_forever()"`, the third launch shape with no
    logging configured. Measured on the Samsung node, generations 16 and 17
    (2026-10-11): the unit's journal held WARNING lines only; the INFO line
    "shell chat -> Nunba over the socket as <id>" never appeared although the
    turn reached the Nunba daemon. Read as text: importing the module pulls
    in flask and the whole shell."""
    import os
    import re
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, '..', '..', 'integrations', 'agent_engine', 'liquid_ui_service.py')
    with open(path, encoding='utf-8') as fh:
        src = fh.read()
    m = re.search(r'\n    def serve_forever\(self\):\n(.*?)\n    def ', src, re.S)
    assert m, 'LiquidUIService.serve_forever not found'
    body = m.group(1)
    # Drop the docstring and comment lines; the first statement must be the rule.
    body = body.split('"""', 2)[2] if body.lstrip().startswith('"""') else body
    statements = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith('#')]
    assert statements[:2] == ['from core.log_handlers import install_console_handler_if_bare',
                              'install_console_handler_if_bare()'], statements[:3]
