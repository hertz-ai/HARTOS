"""The CLI launch gets a root console handler when nobody else installed one.

hart-backend.nix runs `python hart_intelligence_entry.py` under systemd and
relies on stdout reaching journald. The module attaches its handlers to the ROOT
logger only in the bundled (cx_Freeze) shape and deliberately leaves a host's
root alone (2026-08-03 incident: it once stole Nunba's gui_app.log). The OS
launch is neither: root had NO handler, so Python's lastResort handler took the
INFO wiring proofs main() emits and dropped them (it prints WARNING and above).
Measured on the hart-ota-central nixos test (2026-09-23 run, shard 0): the
subscriber bootstrap line never reached the journal and the test timed out
after 240 s on a line the process had already discarded.

These pin the three shapes of _install_root_handlers on a private Logger, not
the real root, so the pytest capture handler is never touched.
"""
import logging

import hart_intelligence_entry as hie


def _fresh():
    root = logging.Logger('root-under-test')
    fh = logging.NullHandler()
    ch = logging.StreamHandler()
    return root, fh, ch


def test_a_bare_root_gets_the_console_handler_so_journald_sees_info():
    root, fh, ch = _fresh()
    hie._install_root_handlers(root, False, fh, ch)
    assert root.handlers == [ch]
    assert root.level == logging.INFO
    assert getattr(ch, hie._HARTOS_HANDLER_TAG, False), 'handler must be tagged so a re-import removes only ours'
    assert fh not in root.handlers, 'the rotating file goes to root only in the bundle'


def test_a_root_someone_else_configured_is_left_exactly_as_found():
    root, fh, ch = _fresh()
    theirs = logging.NullHandler()
    root.addHandler(theirs)
    hie._install_root_handlers(root, False, fh, ch)
    assert root.handlers == [theirs]


def test_the_bundle_still_gets_file_and_console():
    root, fh, ch = _fresh()
    hie._install_root_handlers(root, True, fh, ch)
    assert root.handlers == [fh, ch]
    assert all(getattr(h, hie._HARTOS_HANDLER_TAG, False) for h in root.handlers)


def test_a_second_install_replaces_only_our_own_handlers():
    root, fh, ch = _fresh()
    theirs = logging.NullHandler()
    hie._install_root_handlers(root, False, fh, ch)
    root.addHandler(theirs)
    ch2 = logging.StreamHandler()
    hie._install_root_handlers(root, False, fh, ch2)
    # ours was removed; theirs remained, so root was not bare and ch2 is NOT added
    assert root.handlers == [theirs]


def test_an_info_line_on_a_bare_root_now_reaches_the_stream():
    import io
    root, fh, _ = _fresh()
    buf = io.StringIO()
    ch = logging.StreamHandler(buf)
    hie._install_root_handlers(root, False, fh, ch)
    root.info('Local subscribers bootstrapped: confirmation, ota-push')
    assert 'ota-push' in buf.getvalue()
