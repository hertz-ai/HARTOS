"""/api/shell/gpu exposes the CANONICAL detector, and degrades honestly (#25).

WHY THE ROUTE EXISTS
────────────────────
vram_manager.detect_gpu() is the single-source GPU detector (nvidia-smi ->
torch -> Metal, cached) that the model bus and the tier ladder already use. It
was reachable only from INSIDE the process, so an agent asking "can I load a 7B
here?" had no surface to ask — on an OS whose premise is that agents drive it.
The route EXPOSES that detector; it does not add a second one, which is the
whole point (task #25 is union-not-reinvention).

WHAT THESE TESTS PIN
────────────────────
The three answers a caller must be able to tell apart, because collapsing any
two of them is how a surface starts lying:

    available=True,  present=True   -> a GPU, with its numbers
    available=True,  present=False  -> genuinely no GPU (a CPU-only box)
    available=False, 503            -> could not LOOK (detector missing/raised)

"No GPU" and "could not look" are opposite facts. Reporting a fabricated 0 GB
for the second reads as "a GPU with no memory", which is the false-healthy
shape this repo keeps finding.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


def _client():
    """A Flask app carrying ONLY the shell system routes."""
    from flask import Flask
    from integrations.agent_engine import shell_system_apis
    app = Flask(__name__)
    app.config['TESTING'] = True
    shell_system_apis.register_shell_system_routes(app)
    return app.test_client()


class GpuRouteTellsTheTruth(unittest.TestCase):

    def test_reports_a_present_gpu_with_its_numbers(self):
        fake = {'name': 'NVIDIA GeForce RTX 4090', 'total_gb': 24.0,
                'free_gb': 21.5, 'cuda_available': True}
        with patch('integrations.service_tools.vram_manager.detect_gpu',
                   return_value=fake):
            r = _client().get('/api/shell/gpu')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['available'])
        self.assertTrue(body['present'])
        self.assertEqual(body['name'], 'NVIDIA GeForce RTX 4090')
        self.assertEqual(body['total_gb'], 24.0)
        self.assertTrue(body['cuda_available'])

    def test_a_cpu_only_box_is_present_False_not_an_error(self):
        """No GPU is a REAL answer, not a failure."""
        none_found = {'name': None, 'total_gb': 0.0, 'free_gb': 0.0,
                      'cuda_available': False}
        with patch('integrations.service_tools.vram_manager.detect_gpu',
                   return_value=none_found):
            r = _client().get('/api/shell/gpu')
        self.assertEqual(r.status_code, 200,
                         "a CPU-only box is not an error condition")
        body = r.get_json()
        self.assertTrue(body['available'], "the detector RAN; it just found none")
        self.assertFalse(body['present'])

    def test_a_raising_detector_is_503_not_a_fabricated_zero(self):
        """Could-not-look must NOT be reported as a GPU with no memory."""
        with patch('integrations.service_tools.vram_manager.detect_gpu',
                   side_effect=RuntimeError('nvidia-smi timed out')):
            r = _client().get('/api/shell/gpu')
        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertFalse(body['available'])
        self.assertIn('nvidia-smi timed out', body['error'],
                      "the degraded answer must name the cause, not just fail")

    def test_the_failure_is_logged(self):
        """A silent degrade is the thing the shell ratchets exist to stop."""
        from integrations.agent_engine import shell_system_apis
        with patch('integrations.service_tools.vram_manager.detect_gpu',
                   side_effect=RuntimeError('boom-gpu')), \
                patch.object(shell_system_apis.logger, 'warning') as warn:
            _client().get('/api/shell/gpu')
        self.assertTrue(warn.called, "detector failure was swallowed silently")
        self.assertIn('boom-gpu', " ".join(str(a) for a in warn.call_args[0]))

    def test_it_reuses_the_canonical_detector(self):
        """The route must CALL vram_manager, not re-implement detection.

        Asserted by observing the call: if someone replaces this with a second
        nvidia-smi parser, the canonical detector stops being consulted and
        this fails — which is exactly the parallel path #25 forbids.
        """
        called = {}

        def _spy():
            called['yes'] = True
            return {'name': None, 'total_gb': 0.0, 'free_gb': 0.0,
                    'cuda_available': False}

        with patch('integrations.service_tools.vram_manager.detect_gpu', _spy):
            _client().get('/api/shell/gpu')
        self.assertTrue(called.get('yes'),
                        "the route did not consult vram_manager.detect_gpu — "
                        "a second detector is a parallel path")


if __name__ == '__main__':
    unittest.main()
