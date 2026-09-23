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


# ── the envelope (MEASURED against a live AceStep, 2026-09-22) ────────

def test_an_enveloped_answer_is_opened():
    """The exact body a live AceStep returned tonight.

    Reading the TOP level of this finds no task_id and no status. That is
    how every composition was accepted, generated, and then lost: the
    submit produced the id 'acestep_' with nothing after the underscore,
    and the poll answered 'unknown' forever. A node could compose
    perfectly and no game would ever hear a note.
    """
    from integrations.service_tools.media_agent import _unwrap_envelope

    live = {'data': {'task_id': 'bf6b362b-99a5-4694-b9b5-9aac486237d8',
                     'status': 'queued', 'queue_position': 1},
            'code': 200, 'error': None, 'timestamp': 1790017818443}

    assert live.get('task_id') is None, 'the old read, for the record'
    assert live.get('status', 'unknown') == 'unknown'

    inner = _unwrap_envelope(live)
    assert inner['task_id'] == 'bf6b362b-99a5-4694-b9b5-9aac486237d8'
    assert inner['status'] == 'queued'


def test_a_flat_answer_is_left_alone():
    """wan2gp and the TTS suite answer flat; unwrapping must not break them."""
    from integrations.service_tools.media_agent import _unwrap_envelope

    flat = {'status': 'completed', 'audio_url': 'https://node/a.mp3'}
    assert _unwrap_envelope(flat) == flat


def test_a_data_field_that_is_not_an_envelope_is_left_alone():
    """'data' is a common field name; only a task-shaped one is an envelope."""
    from integrations.service_tools.media_agent import _unwrap_envelope

    payload = {'status': 'completed', 'data': {'samples': 3, 'rate': 44100}}
    assert _unwrap_envelope(payload) == payload
    assert _unwrap_envelope({'data': 'a string'}) == {'data': 'a string'}
    assert _unwrap_envelope(None) == {}


def test_a_result_url_means_done_whatever_the_status_says():
    """MEASURED 2026-09-22: AceStep answered status=1, a numeric code.

    The completed-status list knows words. A numeric status meant a
    finished job read as unfinished and the caller polled it forever. An
    artifact is unambiguous where a status vocabulary is not.
    """
    from integrations.service_tools.media_agent import _unwrap_envelope

    # the shape the live server returned, with an artifact present
    live = {'data': [{'task_id': 'abc', 'status': 1,
                      'audio_url': 'F:/out/chime.wav'}], 'code': 200}
    inner = _unwrap_envelope(live)
    assert inner.get('status') == 1, 'the numeric status is what arrives'
    assert inner.get('audio_url'), 'and the artifact is there alongside it'


# ── the finished item, as AceStep actually shapes it (MEASURED 2026-09-22) ──

def _poll_acestep(item):
    """Run check_media_status against one live-shaped /query_result item."""
    import json as _j
    from unittest.mock import MagicMock, patch
    import integrations.service_tools.media_agent as ma
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'data': [item], 'code': 200, 'error': None}
    with patch.object(ma, '_get_tool_base_url', return_value='http://127.0.0.1:1'),             patch('core.http_pool.pooled_post', return_value=resp):
        return _j.loads(ma.check_media_status('acestep_abc'))


def test_a_finished_task_yields_its_file_from_the_nested_result(tmp_path, monkeypatch):
    """Two WAVs were saved at 10:34:30 and forty polls said composing.

    The path is not a flat url key. It is inside a JSON STRING under
    'result', at [0]['file'], beside a numeric status (1 = succeeded) --
    and that `file` is AceStep's OWN url, `/v1/audio?path=<temp file>`
    (MEASURED 2026-09-22), which only its sidecar can serve.
    """
    import json as _j
    import urllib.parse
    import integrations.service_tools.media_agent as ma
    temp = tmp_path / 'acestep_tmp' / '6ac56af6.wav'
    temp.parent.mkdir()
    temp.write_bytes(b'RIFF\x00\x00\x00\x00WAVE')
    kept_dir = tmp_path / 'acestep' / 'output'
    monkeypatch.setattr(ma, 'composer_output_dir', lambda: kept_dir)
    file_value = '/v1/audio?path=' + urllib.parse.quote(str(temp))
    item = {'task_id': 'abc', 'status': 1, 'progress_text': 'done',
            'result': _j.dumps([{'file': file_value, 'wave': '',
                                 'status': 1, 'metas': {'duration': 5}}])}
    out = _poll_acestep(item)
    assert out['status'] == 'completed', out
    assert out['results'][0]['url'] == '/api/voice/audio/6ac56af6.wav', out
    kept = kept_dir / '6ac56af6.wav'
    assert out['results'][0]['path'] == str(kept)
    assert kept.read_bytes()[:4] == b'RIFF', 'the composition was not kept'


def test_a_composition_whose_file_is_gone_is_not_reported_done(tmp_path, monkeypatch):
    """hartos-3a F1: a url nothing can fetch must not reach the memo."""
    import json as _j
    import integrations.service_tools.media_agent as ma
    monkeypatch.setattr(ma, 'composer_output_dir', lambda: tmp_path / 'out')
    item = {'task_id': 'abc', 'status': 1, 'progress_text': 'done',
            'result': _j.dumps([{'file': '/v1/audio?path=C%3A%5Cgone%5Cx.wav',
                                 'status': 1}])}
    out = _poll_acestep(item)
    assert out['status'] == 'error', out
    assert 'results' not in out or not out['results'], out


def test_the_music_path_starts_its_composer_exactly_once():
    """2475ff19e applied its auto-start block twice; one copy hid the other."""
    from unittest.mock import MagicMock, patch
    import integrations.service_tools.media_agent as ma
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'data': {'task_id': 't1', 'status': 'queued'}, 'code': 200}
    with patch.object(ma, '_start_tool', return_value={'running': True}) as start, \
            patch.object(ma, '_node_has_any', return_value=True), patch.object(ma, '_get_tool_base_url', return_value='http://127.0.0.1:1'), \
            patch('core.http_pool.pooled_post', return_value=resp):
        ma._generate_audio_music('a chime', '', 2, '')
    assert start.call_count == 1, start.call_count


def test_a_failed_task_surfaces_the_nested_error():
    import json as _j
    item = {'task_id': 'abc', 'status': 2, 'progress_text': '',
            'result': _j.dumps([{'file': '', 'status': 2,
                                 'error': 'CUDA out of memory', 'stage': 'diffusion'}])}
    out = _poll_acestep(item)
    assert out['status'] == 'error'
    assert 'CUDA out of memory' in out['error']


def test_the_submit_names_the_duration_field_acestep_reads():
    """'duration' was silently ignored; every cue came back at 60s."""
    from unittest.mock import MagicMock, patch
    import integrations.service_tools.media_agent as ma
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'data': {'task_id': 't1', 'status': 'queued'}, 'code': 200}
    with patch.object(ma, '_start_tool', return_value={'running': True}), patch.object(ma, '_node_has_any', return_value=True), patch.object(ma, '_get_tool_base_url', return_value='http://127.0.0.1:1'),             patch('core.http_pool.pooled_post', return_value=resp) as post:
        ma._generate_audio_music('a chime', '', 5, '')
    payload = post.call_args.kwargs['json']
    assert payload['audio_duration'] == 5
    assert 'duration' not in payload


def test_the_music_path_starts_its_composer_before_dialing_it():
    """TTS and video start their sidecar first; music dialed straight away.

    So an installed composer that was not up answered "not running" to
    every game, for ever -- run 8, 2026-09-22.  The proof runs had been
    starting it by hand, which is why seven of them never noticed.
    """
    from unittest.mock import MagicMock, patch
    import integrations.service_tools.media_agent as ma
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'data': {'task_id': 't1', 'status': 'queued'}, 'code': 200}
    with patch.object(ma, '_start_tool', return_value={'running': True}) as start, \
            patch.object(ma, '_node_has_any', return_value=True), patch.object(ma, '_get_tool_base_url', return_value='http://127.0.0.1:1'), \
            patch('core.http_pool.pooled_post', return_value=resp):
        ma._generate_audio_music('a chime', '', 2, '')
    assert start.call_args.args == ('acestep',)


def test_a_composer_the_runtime_will_not_start_is_unreachable_not_absent():
    """MEASURED 2026-09-22: "Refusing to start acestep: won't fit (free=4.9GB)"
    with a llama-server on the card.  Installed, so never an install offer;
    and the runtime's reason travels with the answer instead of dying in a
    bare False."""
    from unittest.mock import patch
    import integrations.service_tools.media_agent as ma
    refused = {'error': 'Insufficient VRAM for acestep (free=4.9GB); try cpu_only',
               'oom': True}
    with patch.object(ma, '_start_tool', return_value=refused), \
            patch.object(ma, '_node_has_any', return_value=True), \
            patch.object(ma, '_get_tool_base_url') as dial:
        out = ma._generate_audio_music('a chime', '', 2, '')
    assert not dial.called, 'dialed a composer the runtime had just refused to start'
    assert classify_error(out) == UNREACHABLE, out
    assert 'free=4.9GB' in out['error'], out


def test_a_composer_that_is_not_on_this_node_is_absent():
    from unittest.mock import patch
    import integrations.service_tools.media_agent as ma
    with patch.object(ma, '_start_tool',
                      return_value={'running': False, 'error': 'no such tool'}), \
            patch.object(ma, '_node_has_any', return_value=False):
        out = ma._generate_audio_music('a chime', '', 2, '')
    assert classify_error(out) == ABSENT, out


def test_start_tool_keeps_the_runtime_reason():
    """_ensure_tool_running folded 'Insufficient VRAM' into a bare False."""
    from unittest.mock import MagicMock, patch
    import integrations.service_tools.media_agent as ma
    rt = MagicMock()
    rt.get_tool_status.return_value = {'running': False}
    rt.setup_tool.return_value = {
        'error': 'Insufficient VRAM for acestep (free=4.9GB); try cpu_only', 'oom': True}
    fake = MagicMock(runtime_tool_manager=rt)
    with patch.dict(sys.modules, {'integrations.service_tools.runtime_manager': fake}):
        out = ma._start_tool('acestep')
        assert ma._ensure_tool_running('acestep') is False
    assert 'free=4.9GB' in out['error'], out


def test_only_audio_is_kept_where_the_audio_route_serves(tmp_path, monkeypatch):
    import integrations.service_tools.media_agent as ma
    monkeypatch.setattr(ma, 'composer_output_dir', lambda: tmp_path / 'out')
    secret = tmp_path / 'notes.txt'
    secret.write_text('not audio')
    assert ma._keep_composition(str(secret)) is None
    assert not (tmp_path / 'out' / 'notes.txt').exists()


def test_a_reused_temp_name_never_replaces_a_kept_take(tmp_path, monkeypatch):
    """A memo names its file for good; new bytes under an old name get a new name."""
    import integrations.service_tools.media_agent as ma
    out = tmp_path / 'out'
    monkeypatch.setattr(ma, 'composer_output_dir', lambda: out)
    src = tmp_path / 'tmp' / 'take.wav'
    src.parent.mkdir()
    src.write_bytes(b'RIFF-first')
    first = ma._keep_composition(str(src))
    src.write_bytes(b'RIFF-second-take')
    second = ma._keep_composition(str(src))
    assert first[0] == '/api/voice/audio/take.wav'
    assert second[0] == '/api/voice/audio/take-1.wav', second
    assert (out / 'take.wav').read_bytes() == b'RIFF-first'
    # the same bytes again reuse the name they already have
    assert ma._keep_composition(str(src))[0] == '/api/voice/audio/take-1.wav'


def test_listed_is_not_installed():
    """hartos-3a F4: the catalog lists every engine; only a downloaded one is here."""
    from unittest.mock import MagicMock, patch
    import integrations.service_tools.media_agent as ma
    listed = MagicMock(downloaded=False)
    here = MagicMock(downloaded=True)
    cat = MagicMock()
    cat.list_by_type.return_value = [listed]
    with patch('integrations.service_tools.model_catalog.get_catalog', return_value=cat):
        assert ma._node_has_any('audio_gen') is False
        cat.list_by_type.return_value = [listed, here]
        assert ma._node_has_any('audio_gen') is True


def test_nothing_downloaded_asks_instead_of_downloading():
    """Owner 2026-09-23: ask once, then set up. The music path must not
    start (and so download) a composer that is not on the disk; its answer
    is ABSENT, which is what brings up the consent card."""
    from unittest.mock import patch
    import integrations.service_tools.media_agent as ma
    with patch.object(ma, '_node_has_any', return_value=False),             patch.object(ma, '_start_tool') as start:
        out = ma._generate_audio_music('a chime', '', 2, '')
    assert not start.called, 'a download was started without consent'
    assert classify_error(out) == ABSENT, out

