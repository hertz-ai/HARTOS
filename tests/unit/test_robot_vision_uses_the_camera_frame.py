"""The robot's camera frame must actually reach the VLM.

THE DEFECT THIS PINS, measured 2026-09-11 against the real producer.

``RobotIntelligenceAPI._invoke_vision`` built this message::

    {'type': 'describe', 'image': camera, 'prompt': '...'}

and handed it to ``execute_vlm_instruction``, which forwards it verbatim
to ``run_local_agentic_loop``.  That function reads exactly five keys off
the message (local_loop.py:246-250)::

    instruction_to_vlm_agent   enhanced_instruction   user_id
    prompt_id                  max_ETA_in_seconds

``type``, ``image`` and ``prompt`` are none of them.  So:

  * the camera frame was DISCARDED — never decoded, never looked at;
  * ``instruction`` defaulted to ``''``;
  * ``run_local_agentic_loop`` is a DESKTOP-CONTROL loop — it takes its
    own screenshot and drives mouse/keyboard.  A robot asking "what do
    you see?" was pointed at the operator's monitor with an empty task.

The response side was fiction in the same way.  The producer returns
``{status, exit_reason, extracted_responses, execution_time_seconds}``;
``_invoke_vision`` read ``result['objects']`` and ``result['obstacles']``,
keys no producer has ever emitted, so both were ALWAYS ``[]`` — which is
why ``_extract_target`` (:1094) never once derived a navigation target
from vision and ``plan['obstacles_detected']`` (:730) was never set.

THE CANONICAL CAPABILITY ALREADY EXISTED: ``Qwen3VLBackend.describe_scene
(image_b64, prompt)`` (qwen3vl_backend.py:1049) is the one entry point
that accepts a CALLER-SUPPLIED image.  The fix routes there instead of
inventing a second path, and parses the reply with the canonical
``integrations.vlm.parser.extract_json``.

These tests are behavioural: they run the real ``_invoke_vision`` against
a stubbed backend and assert on what the backend was HANDED and what the
caller got BACK.  A grep-shaped test would have passed on the broken code.
"""

import os
import sys
import unittest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _HARTOS not in sys.path:
    sys.path.insert(0, _HARTOS)

# A 1x1 PNG.  Content is irrelevant — what matters is that this exact
# string is what the backend receives.
CAMERA_FRAME = (
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8'
    'z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
)


class _StubBackend:
    """Records what it was handed; replies with whatever was configured."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def describe_scene(self, screenshot_b64, prompt='Describe what you see in this image'):
        self.calls.append({'image': screenshot_b64, 'prompt': prompt})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class _VisionCase(unittest.TestCase):

    def _run(self, reply):
        """Call the REAL _invoke_vision with the backend stubbed out."""
        from integrations.robotics import intelligence_api as mod
        import integrations.vlm.qwen3vl_backend as qb

        stub = _StubBackend(reply)
        real_getter = qb.get_qwen3vl_backend
        qb.get_qwen3vl_backend = lambda: stub
        try:
            api = mod.RobotIntelligenceAPI.__new__(mod.RobotIntelligenceAPI)
            out = mod.RobotIntelligenceAPI._invoke_vision(
                api, {'camera': CAMERA_FRAME})
        finally:
            qb.get_qwen3vl_backend = real_getter
        return stub, out


class TestTheCameraFrameReachesTheModel(_VisionCase):

    def test_the_frame_is_handed_to_the_backend_verbatim(self):
        """Pre-fix this was 0 calls — the frame went to a key nobody reads."""
        stub, _ = self._run('{"scene": "a hallway", "objects": [], "obstacles": []}')
        self.assertEqual(
            len(stub.calls), 1,
            'the camera frame never reached a model that can look at it')
        self.assertEqual(
            stub.calls[0]['image'], CAMERA_FRAME,
            'the frame was altered or substituted on the way in')

    def test_the_scene_is_the_models_words_not_a_repr(self):
        stub, out = self._run(
            '{"scene": "a hallway with a door on the left", '
            '"objects": [], "obstacles": []}')
        self.assertEqual(out['scene'], 'a hallway with a door on the left')

    def test_prose_reply_still_becomes_the_scene(self):
        """No JSON is a normal VLM outcome, not an error."""
        stub, out = self._run('I can see a hallway with a door on the left.')
        self.assertEqual(out['scene'],
                         'I can see a hallway with a door on the left.')
        self.assertEqual(out['objects'], [])
        self.assertEqual(out['obstacles'], [])


class TestObjectsAndObstaclesComeFromTheModel(_VisionCase):
    """Both keys were ALWAYS [] — no producer ever emitted them."""

    def test_objects_survive_into_the_return(self):
        _, out = self._run(
            '{"scene": "kitchen", '
            '"objects": [{"label": "glass", "x": 120, "y": 200}], '
            '"obstacles": ["chair"]}')
        self.assertEqual(out['objects'], [{'label': 'glass', 'x': 120, 'y': 200}])

    def test_objects_reach_the_consumer_that_picks_a_target(self):
        """_extract_target:1094 is the reason `objects` exists at all."""
        from integrations.robotics.intelligence_api import _extract_target
        _, out = self._run(
            '{"scene": "kitchen", '
            '"objects": [{"label": "glass", "x": 120, "y": 200}], '
            '"obstacles": []}')
        target = _extract_target('fetch the glass', out)
        self.assertEqual(
            target, {'x': 120, 'y': 200, 'label': 'glass'},
            'vision-derived navigation target is still unreachable')

    def test_obstacles_reach_the_fusion_step(self):
        """_fuse_results:729 sets plan['obstacles_detected'] off this key."""
        _, out = self._run(
            '{"scene": "hall", "objects": [], "obstacles": ["chair", "box"]}')
        self.assertEqual(out['obstacles'], ['chair', 'box'])

    def test_a_fenced_json_block_is_parsed(self):
        """Instruction-tuned models fence JSON; extract_json handles it."""
        _, out = self._run(
            'Here is what I see:\n```json\n'
            '{"scene": "lab", "objects": ["robot arm"], "obstacles": []}\n'
            '```\n')
        self.assertEqual(out['scene'], 'lab')
        self.assertEqual(out['objects'], ['robot arm'])


class TestTheDegradedPathsStayHonest(_VisionCase):

    def test_no_camera_is_reported_as_no_camera(self):
        from integrations.robotics import intelligence_api as mod
        api = mod.RobotIntelligenceAPI.__new__(mod.RobotIntelligenceAPI)
        out = mod.RobotIntelligenceAPI._invoke_vision(api, {})
        self.assertEqual(out['scene'], 'no_camera')

    def test_a_backend_failure_does_not_claim_an_empty_scene(self):
        """The robot must not read 'nothing is there' as 'I looked and it
        was empty'."""
        _, out = self._run(RuntimeError('connection refused'))
        self.assertEqual(out['scene'], 'vlm_unavailable')
        self.assertIn('note', out)


class TestTheDeadContractIsGone(unittest.TestCase):
    """Drift guard: the phantom keys must not come back."""

    def setUp(self):
        import io
        p = os.path.join(_HARTOS, 'integrations', 'robotics',
                         'intelligence_api.py')
        self.src = io.open(p, encoding='utf-8', errors='replace').read()

    def test_no_message_is_built_from_keys_the_producer_ignores(self):
        self.assertNotIn(
            "'type': 'describe'", self.src,
            "rebuilt the message shape run_local_agentic_loop ignores; it "
            "reads only instruction_to_vlm_agent/enhanced_instruction/"
            "user_id/prompt_id/max_ETA_in_seconds (local_loop.py:246)")

    def test_objects_are_not_read_off_the_desktop_loops_return(self):
        self.assertNotIn(
            "result.get('objects'", self.src,
            "run_local_agentic_loop returns {status, exit_reason, "
            "extracted_responses, execution_time_seconds} — never 'objects'")
        self.assertNotIn(
            "result.get('obstacles'", self.src,
            "same: 'obstacles' is emitted by no producer in either repo")


if __name__ == '__main__':
    unittest.main()
