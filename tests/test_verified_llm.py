"""FT — core.verified_llm deep-verification contract.

Stage-A Symptom #4 guard: the verified_ready pattern extended to LLM.
"""

import io
import json
import unittest
from unittest.mock import MagicMock, patch

from core.verified_llm import (
    DEFAULT_BASE_URL,
    is_llm_inference_verified,
    verify_llm,
)


def _fake_resp(status: int, body: dict):
    """Build a urlopen()-compatible context manager returning body bytes."""
    mock = MagicMock()
    mock.status = status
    mock.read.return_value = json.dumps(body).encode("utf-8")
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


class VerifyLLMSuccessTests(unittest.TestCase):
    def test_verified_on_non_empty_content(self):
        ok_body = {
            "choices": [
                {"message": {"role": "assistant", "content": "Hi there."}}
            ]
        }
        with patch("urllib.request.urlopen", return_value=_fake_resp(200, ok_body)):
            out = verify_llm()
            self.assertTrue(out["ok"])
            self.assertEqual(out["reason"], "verified")
            self.assertIn("Hi there", out["content_snippet"])
            self.assertEqual(out["http_status"], 200)

    def test_bool_wrapper_returns_true(self):
        ok_body = {"choices": [{"message": {"content": "yes"}}]}
        with patch("urllib.request.urlopen", return_value=_fake_resp(200, ok_body)):
            self.assertTrue(is_llm_inference_verified())

    def test_legacy_completions_text_field(self):
        legacy = {"choices": [{"text": "ok"}]}
        with patch("urllib.request.urlopen", return_value=_fake_resp(200, legacy)):
            self.assertTrue(verify_llm()["ok"])

    def test_reasoning_model_empty_content_but_reasoning(self):
        """Reasoning models (Qwen3.5 thinking-mode, the shipped default) burn
        the whole max_tokens budget on reasoning_content with finish_reason=
        'length', leaving content=''.  A non-empty reasoning stream is still
        proof the model is loaded and generating, so the probe MUST verify it.

        Regression guard for the live P0 (2026-06-10): a healthy reasoning
        model was reported available:false, which latched the React chat
        send-gate engineReady=false and stranded every message in the queue.
        """
        reasoning_body = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "Let me think about this",
                    },
                    "finish_reason": "length",
                }
            ]
        }
        with patch("urllib.request.urlopen", return_value=_fake_resp(200, reasoning_body)):
            out = verify_llm()
            self.assertTrue(out["ok"])
            self.assertEqual(out["reason"], "verified")
            self.assertIn("think", out["content_snippet"])
        with patch("urllib.request.urlopen", return_value=_fake_resp(200, reasoning_body)):
            self.assertTrue(is_llm_inference_verified())


class VerifyLLMFailureTests(unittest.TestCase):
    def test_empty_content_flagged(self):
        empty_body = {"choices": [{"message": {"content": ""}}]}
        with patch("urllib.request.urlopen", return_value=_fake_resp(200, empty_body)):
            out = verify_llm()
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "empty_content")

    def test_no_choices(self):
        with patch("urllib.request.urlopen", return_value=_fake_resp(200, {"choices": []})):
            out = verify_llm()
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "empty_content")

    def test_malformed_json(self):
        bad = MagicMock()
        bad.status = 200
        bad.read.return_value = b"<html>oops</html>"
        bad.__enter__ = MagicMock(return_value=bad)
        bad.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=bad):
            out = verify_llm()
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "malformed_json")

    def test_unreachable(self):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("connection refused")):
            out = verify_llm()
            self.assertFalse(out["ok"])
            self.assertTrue(out["reason"].startswith("unreachable"))

    def test_http_500(self):
        import urllib.error
        err = urllib.error.HTTPError(
            url="http://x",
            code=500,
            msg="server error",
            hdrs=None,
            fp=io.BytesIO(b""),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            out = verify_llm()
            self.assertFalse(out["ok"])
            self.assertEqual(out["http_status"], 500)
            self.assertEqual(out["reason"], "http_500")

    def test_bool_wrapper_returns_false_on_unreachable(self):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("refused")):
            self.assertFalse(is_llm_inference_verified())


class VerifyLLMPayloadTests(unittest.TestCase):
    def test_probe_posts_to_v1_chat_completions(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = req.data
            return _fake_resp(200, {"choices": [{"message": {"content": "hi"}}]})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            verify_llm(url="http://127.0.0.1:8080")

        self.assertEqual(captured["url"], "http://127.0.0.1:8080/v1/chat/completions")
        self.assertEqual(captured["method"], "POST")
        body = json.loads(captured["body"].decode("utf-8"))
        self.assertEqual(body["messages"][0]["content"], "hi")
        self.assertIn("max_tokens", body)


if __name__ == "__main__":
    unittest.main()
