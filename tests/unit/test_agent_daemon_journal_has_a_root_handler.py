"""The systemd entrypoint gives the journal a root handler when nobody else has.

hart-agent.nix runs `python -c "... AgentDaemon().run_forever()"`. That process
configured no logging, so its INFO lines (ticks, yield reasons, governor
transitions) went to Python's lastResort handler and were dropped. Measured on
the Samsung node, generations 11 and 12 (2026-09-24): one journal line per boot.
These pin the helper run_forever calls first, on a private Logger.
"""
import logging

from integrations.agent_engine import agent_daemon as ad


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
