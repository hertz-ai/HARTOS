"""A sing request sends its words to the composer, so they are sung.

Demo 11-17 on the local 4B: the rhyme agent did not sing.  The one route the
framework has for singing is a <sing> or <lyrics> segment in spoken text
(a reply, or synthesize_multilingual_audio): Nunba speaks it by calling
generate_media('audio_music') with the single text "Singing: <the words>"
(Nunba tts/tts_engine.py _synth_sing_segment), and generate_media sends it
on to AceStep.  AceStep sings only what its 'lyrics' field holds and makes
an instrumental when that field is empty (its server_utils.is_instrumental).
The words went out only as the description ('prompt'), so a sung segment
could only ever come back as a tune with no voice, whatever the agent did.

Every music caller that is not a sing request (game sounds, the kids
background music, the API) must still send exactly the request it sent
before, with no lyrics, so it still gets an instrumental.

Nothing here reaches a real composer, port or model: the submit, the
composer start, the capability checks and the feedback bridge are faked.

    python -m pytest tests/unit/test_a_sing_request_sends_its_words_to_the_composer.py -q
"""
import contextlib
import json
import os
import sys
import types
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

import integrations.service_tools.media_agent as ma  # noqa: E402
from core.game_sound_memo import GAME_STATES  # noqa: E402

# What Nunba's sing segment puts in front of the words it wants sung.
NUNBA_SING_PREFIX = 'Singing: '

RHYME = ("Red ball, red ball, bounce so high\n"
         "Rolling round so bright and red\n"
         "Bouncing up and down so free\n"
         "Happy and smiling all the while")


_BRIDGE = 'integrations.agent_engine.world_model_bridge'


@contextlib.contextmanager
def _feedback_kept_inside():
    """generate_media reports every result to the world-model bridge; a
    stand-in module keeps that report inside the test.  Only this one entry
    of sys.modules is swapped and put back, so modules imported meanwhile
    stay imported."""
    fake = types.ModuleType(_BRIDGE)
    fake.get_world_model_bridge = lambda: MagicMock()
    had, before = _BRIDGE in sys.modules, sys.modules.get(_BRIDGE)
    sys.modules[_BRIDGE] = fake
    try:
        yield
    finally:
        if had:
            sys.modules[_BRIDGE] = before
        else:
            sys.modules.pop(_BRIDGE, None)


def _submit(call):
    """Run ``call`` against a composer that is installed, up and answering;
    return the JSON body sent to /release_task and what the call returned."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'data': {'task_id': 't1', 'status': 'queued'},
                              'code': 200}
    with patch.object(ma, '_can_do', return_value=True), \
            patch.object(ma, '_node_has_any', return_value=True), \
            patch.object(ma, '_start_tool', return_value={'running': True}), \
            patch.object(ma, '_get_tool_base_url',
                         return_value='http://127.0.0.1:1'), \
            _feedback_kept_inside(), \
            patch('core.http_pool.pooled_post', return_value=resp) as post:
        out = call()
    assert post.call_count == 1, 'the composer was asked exactly once'
    assert post.call_args.args[0] == 'http://127.0.0.1:1/release_task'
    if isinstance(out, str):
        out = json.loads(out)
    return post.call_args.kwargs['json'], out


def _sing_segment(words, style=''):
    """The call Nunba makes for a <sing> segment: _synth_sing_segment hands
    "Singing: <words>" to _synth_media_segment, which passes it as both the
    context and the input text, with the tag's duration and no genre."""
    text = f'{NUNBA_SING_PREFIX}{words}'
    return lambda: ma.generate_media(
        context=text, output_modality='audio_music',
        input_text=text, duration=30, style=style)


# -- the defect: the words of a sing request are sent as lyrics -------------

def test_a_sung_segment_sends_its_words_as_the_lyrics_acestep_sings():
    payload, out = _submit(_sing_segment(RHYME))
    assert payload.get('lyrics') == RHYME, (
        'AceStep sings only its lyrics field; without the words it makes an '
        'instrumental, which is how the rhyme agent never sang')
    assert out['status'] == 'pending'
    assert out['task_id'] == 'acestep_t1'


def test_the_words_are_read_before_the_style_is_put_in_front():
    payload, _ = _submit(_sing_segment('Twinkle twinkle little star',
                                       style='lullaby'))
    assert payload.get('lyrics') == 'Twinkle twinkle little star'
    assert payload['genre'] == 'lullaby'
    assert payload['prompt'] == '[lullaby] Singing: Twinkle twinkle little star'


def test_a_rhyme_in_tamil_is_sent_word_for_word():
    words = 'சிவப்பு பந்து'
    payload, _ = _submit(_sing_segment(words))
    assert payload.get('lyrics') == words


def test_a_lyrics_segment_takes_the_same_road_as_a_sing_segment():
    """Nunba speaks <lyrics> through _synth_sing_segment too, so it arrives
    with the same prefix and must be sung the same way."""
    payload, _ = _submit(lambda: ma._generate_audio_music(
        f'{NUNBA_SING_PREFIX}La la la, the sun is up',
        f'{NUNBA_SING_PREFIX}La la la, the sun is up', 30, ''))
    assert payload.get('lyrics') == 'La la la, the sun is up'


# -- guards: every request that is not a sing request is unchanged ----------

def test_a_game_sound_sends_the_same_request_as_before():
    """bind_game_sound (core/agent_tools.py) passes its state prompt as both
    context and input text; it wants a tune and must not grow lyrics."""
    prompt = GAME_STATES['bgm'].format(what='Maths Quest', mood='calm')
    payload, out = _submit(lambda: ma.generate_media(
        context=prompt, output_modality='audio_music', input_text=prompt,
        duration=30, style='calm'))
    assert payload == {
        'prompt': f'[calm] {prompt}',
        'audio_duration': 30,
        'audio_format': 'wav',
        'genre': 'calm',
    }
    assert out['status'] == 'pending'


def test_the_direct_music_call_sends_the_same_request_as_before():
    payload, _ = _submit(lambda: ma._generate_audio_music('a chime', '', 5, ''))
    assert payload == {'prompt': 'a chime', 'audio_duration': 5,
                       'audio_format': 'wav'}


def test_a_description_that_mentions_singing_is_not_a_sing_request():
    text = 'children singing in a choir, warm and slow'
    payload, _ = _submit(lambda: ma._generate_audio_music(text, text, 20, ''))
    assert 'lyrics' not in payload
    assert payload['prompt'] == text


def test_a_sing_request_with_no_words_asks_for_no_lyrics():
    payload, _ = _submit(lambda: ma._generate_audio_music(
        'Singing:   ', 'Singing:   ', 10, ''))
    assert 'lyrics' not in payload


def test_the_reader_answers_words_only_for_a_sing_request():
    assert ma._words_to_sing('Singing: one two three') == 'one two three'
    assert ma._words_to_sing('  singing:\nhello there  ') == 'hello there'
    assert ma._words_to_sing('a happy tune') == ''
    assert ma._words_to_sing('') == ''
    assert ma._words_to_sing(None) == ''
