"""send_message_to_user1 must work on a thread that has no Flask app context.

It is called from tool and scheduler threads.  Its log calls went through
current_app.logger, so after the POST succeeded the success log raised
RuntimeError("Working outside of application context").  The except branch's
own current_app.logger call then raised again, and the caller was told the
send failed for a message that had been sent.  Measured on central,
2026-09-14: 33 of the 34 "Working outside of application context" errors
came from this function.

The module already has the context-safe logger for exactly this
(_ctx_safe_log, used by create_schedule and the prompt-narrow path); the
function must use it rather than a bare current_app.logger.
"""
import inspect
from unittest.mock import patch

import pytest

pytest.importorskip('autogen', reason='autogen not installed')


def _scheduler_stub(rr):
    sched = patch.object(rr, 'scheduler')
    return sched


def test_a_sent_message_is_reported_sent_without_an_app_context():
    from hartos import reuse_recipe as rr
    with patch.object(rr, 'pooled_post') as post, _scheduler_stub(rr) as sched:
        sched.get_job.return_value = None
        out = rr.send_message_to_user1('ctx-user-1', 'hello', 'inp', 'ctx-prompt')
    assert post.called, 'the message must still be POSTed'
    assert out.startswith('Message sent successfully'), out


def test_a_failed_send_is_reported_not_raised_without_an_app_context():
    from hartos import reuse_recipe as rr
    with patch.object(rr, 'pooled_post', side_effect=OSError('endpoint down')), \
         _scheduler_stub(rr) as sched:
        sched.get_job.return_value = None
        out = rr.send_message_to_user1('ctx-user-2', 'hello again', 'inp', 'ctx-prompt')
    assert out.startswith('Failed to send message'), out


def test_the_function_logs_only_through_the_context_safe_logger():
    """Source guard: a current_app.logger access here is the defect itself.

    Walks the AST, so the comment in the function that names the old call
    cannot trip it -- a text match could not tell the two apart.
    """
    import ast
    import textwrap
    from hartos import reuse_recipe as rr
    tree = ast.parse(textwrap.dedent(inspect.getsource(rr.send_message_to_user1)))
    bare = [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == 'logger'
            and isinstance(node.value, ast.Name) and node.value.id == 'current_app']
    assert not bare, (
        'send_message_to_user1 runs outside the app context; log through '
        '_ctx_safe_log (current_app.logger at function lines %s)' % bare)
