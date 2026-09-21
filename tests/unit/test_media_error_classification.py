"""Why a media call failed is answered by ONE reader, beside the writer.

A node with no music model should be OFFERED one; a node whose composer is
installed and merely refused a prompt should NOT be, because asking someone
to install what they already have is its own defect. That is a real branch,
and it hung on matching media_agent's prose from another module -- so the day
someone reworded a message, the reader would silently stop working and the
person rewording would have no way to know.

classify_error lives in media_agent, next to every return that produces those
words, so producer and reader move together. These tests feed it the EXACT
shapes the module emits, taken from its own return statements, so a reworded
message breaks a test here rather than a caller's behaviour in the field.

    python -m pytest tests/unit/test_media_error_classification.py -q
"""
import json
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from integrations.service_tools.media_agent import (  # noqa: E402
    ABSENT, REFUSED, UNKNOWN, UNREACHABLE, classify_error)


def err(message, modality='audio_music'):
    """The exact shape media_agent returns on failure."""
    return {'status': 'error', 'error': message, 'output_modality': modality}


class TestNothingIsInstalled:
    """ABSENT is the only kind that may lead to an install offer."""

    @pytest.mark.parametrize('message', [
        'AceStep not registered',                     # _get_tool_base_url None
        'TTS-Audio-Suite not registered',
        'Wan2GP not registered',
        'TTS-Audio-Suite not available and auto-start failed',
        'audio_music not available on this node',
        'no tool for audio_music',
    ])
    def test_absent(self, message):
        assert classify_error(err(message)) == ABSENT

    def test_not_available_beats_a_transport_read(self):
        """'not available' contains no transport word, but a naive matcher
        ordered the other way round would still mislabel some of these. The
        absent check runs FIRST on purpose."""
        assert classify_error(err('AceStep not available')) == ABSENT


class TestInstalledButTheCallDidNotArrive:
    """UNREACHABLE must NEVER offer an install: it IS installed."""

    @pytest.mark.parametrize('message', [
        'HTTPConnectionPool(host=\'localhost\', port=8001): Max retries '
        'exceeded',
        'Connection refused',
        '[WinError 10061] No connection could be made because the target '
        'machine actively refused it',
        'Read timed out.',
        'Failed to establish a new connection',
        'ConnectionError',
    ])
    def test_unreachable(self, message):
        assert classify_error(err(message)) == UNREACHABLE

    def test_unreachable_is_not_absent(self):
        """The distinction that matters: a composer that is installed but
        not running must not be re-installed."""
        v = classify_error(err('Connection refused'))
        assert v == UNREACHABLE and v != ABSENT


class TestItAnsweredAndSaidNo:
    @pytest.mark.parametrize('message', [
        'AceStep HTTP 500', 'AceStep HTTP 503',
        'TTS HTTP 400', 'Wan2GP HTTP 422', 'LTX2 HTTP 429',
    ])
    def test_refused(self, message):
        assert classify_error(err(message)) == REFUSED

    def test_refused_is_not_absent(self):
        v = classify_error(err('AceStep HTTP 503'))
        assert v == REFUSED and v != ABSENT


class TestItRefusesToGuess:
    def test_a_success_is_not_an_error(self):
        assert classify_error(
            {'status': 'pending', 'task_id': 'acestep_1'}) == UNKNOWN
        assert classify_error({'status': 'success'}) == UNKNOWN

    def test_unrecognised_prose_is_unknown_not_absent(self):
        """The safe default. Guessing ABSENT would offer an install for any
        error nobody had classified yet."""
        assert classify_error(err('something nobody anticipated')) == UNKNOWN

    @pytest.mark.parametrize('junk', [None, 42, [], '', 'not json',
                                      {'no': 'status'}, {'status': 'error'}])
    def test_junk_is_unknown_and_never_raises(self, junk):
        assert classify_error(junk) == UNKNOWN


class TestItTakesWhatCallersActuallyHold:
    def test_a_json_string_works_too(self):
        """generate_media is exposed as a tool and its result reaches some
        callers as JSON text."""
        assert classify_error(json.dumps(err('AceStep not registered'))) == ABSENT
        assert classify_error(json.dumps(err('AceStep HTTP 500'))) == REFUSED


class TestTheWordingsAreTheModulesOwn:
    """If a return statement is reworded, a test here fails -- which is the
    whole reason the reader lives in this module."""

    def test_the_not_registered_wording_still_exists_in_the_module(self):
        import inspect

        from integrations.service_tools import media_agent
        src = inspect.getsource(media_agent)
        assert "not registered" in src, (
            'media_agent no longer emits "not registered"; classify_error\'s '
            'ABSENT markers must be updated in the same change')
        assert "HTTP {resp.status_code}" in src, (
            'media_agent no longer emits "<Tool> HTTP <code>"; '
            "classify_error's REFUSED rule must be updated with it")


# ── the modality gate (found by rn-1) ─────────────────────────────────

def test_a_modality_this_node_cannot_do_reads_as_absent():
    """generate_media's own gate answers status='unavailable', not 'error'.

    This is the principal absent case: asking for music on a machine with
    no music engine.  A reader that looked only at status=='error' called
    it UNKNOWN, and the caller could not offer to install what is plainly
    not installed.
    """
    gate = {'status': 'unavailable',
            'error': 'audio_music not available on this node right now.',
            'modality': 'audio_music'}

    assert classify_error(gate) == ABSENT
    assert classify_error(json.dumps(gate)) == ABSENT


def test_the_speech_gate_reads_as_absent_too():
    assert classify_error({
        'status': 'unavailable',
        'error': 'Audio synthesis not available on this node (text-only mode).',
    }) == ABSENT


def test_the_gate_wording_still_exists_in_the_source():
    """Guard, in the same spirit as the two already here."""
    import inspect
    import integrations.service_tools.media_agent as module
    source = inspect.getsource(module)
    assert "'status': 'unavailable'" in source, (
        "the modality gate no longer answers unavailable; "
        "classify_error's status allow-list needs revisiting")


def test_installed_but_out_of_memory_is_not_an_install_offer():
    """The gate's OTHER meaning, and the one that would misfire.

    orchestrator.can_do() is "loaded OR can_load", and can_load drops any
    model that will not fit in the memory free at that instant.  So a fully
    installed engine reads as unavailable whenever the GPU is busy.  Reading
    that as ABSENT would ask the owner to install what is already installed
    -- the very defect this module's reader exists to prevent.
    """
    busy = {'status': 'unavailable',
            'error': ('audio_music is installed on this node but cannot run '
                      'right now (not enough free memory).'),
            'modality': 'audio_music'}

    assert classify_error(busy) == UNREACHABLE
    assert classify_error(busy) != ABSENT


def test_the_two_gate_wordings_still_exist_in_the_source():
    import inspect
    import integrations.service_tools.media_agent as module
    source = inspect.getsource(module)
    assert 'cannot run right ' in source, (
        'the gate no longer distinguishes installed-but-busy from absent; '
        'classify_error would send an install offer to a busy node')
    assert 'not available on this node right now' in source


def test_a_sidecar_with_no_registered_port_is_not_an_install_offer():
    """Ports are assigned at start; a blind dial is not a capability check.

    MEASURED 2026-09-21: this node started AceStep on port 51168 while the
    music path dialed a literal 8001 and reported connection refused. The
    honest answer is that the service is not running -- and it must NOT read
    as absent, because AceStep was downloaded the whole time.
    """
    down = {'status': 'error',
            'error': 'AceStep service is not running (no port registered on '
                     'this node).'}

    assert classify_error(down) == UNREACHABLE
    assert classify_error(down) != ABSENT


def test_no_pinned_port_survives_in_the_media_path():
    """The rule is absolute: never pin an IP:port, derive it."""
    import inspect
    import integrations.service_tools.media_agent as module
    source = inspect.getsource(module)
    for pinned in ('http://localhost:8001', 'http://localhost:5002'):
        assert pinned not in source, (
            f'{pinned} is pinned again; sidecar ports are OS-assigned and '
            f'a blind dial reaches nothing, or something else')


def test_a_busy_speech_engine_is_not_an_install_offer():
    """The gate that carries the voice of every spoken turn.

    hartos-94's generalisation, applied: a capability answer that folds
    "not present" together with "not possible right now" cannot be the
    input to an install decision. TTS is the one that would hurt most --
    the dialer speaks through it on every turn.
    """
    busy = {'status': 'unavailable',
            'error': ('Audio synthesis is installed on this node but cannot '
                      'run right now (not enough free memory).')}

    assert classify_error(busy) == UNREACHABLE
    assert classify_error(busy) != ABSENT


def test_every_capability_gate_distinguishes_busy_from_absent():
    """A guard against the next gate someone adds folding them back.

    Each _can_do() gate that reports to a caller must branch on
    _node_has_any(), or its wording becomes an install offer for something
    already installed.
    """
    import inspect
    import integrations.service_tools.media_agent as module
    source = inspect.getsource(module)
    # every gate that reports outward consults the installed-or-not reader
    assert source.count('_node_has_any(') >= 4, (
        'a capability gate stopped distinguishing installed-but-busy from '
        'absent; see classify_error and the modality gate'
    )
