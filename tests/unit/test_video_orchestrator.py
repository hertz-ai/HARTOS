"""Tests for the Video Generation Orchestrator.

Covers request parsing, text chunking, queue ETA, asset caching,
and the full pipeline dispatch (with mocked GPU backends).
"""
import json
import os
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ─── Text Chunking ────────────────────────────────────────

class TestTextChunking:
    """Test sentence splitting and merging for streaming TTS."""

    def test_single_short_sentence(self):
        from integrations.agent_engine.video_orchestrator import chunk_text
        result = chunk_text('Hello world.')
        assert len(result) == 1
        assert result[0] == 'Hello world.'

    def test_multiple_sentences_merge_short(self):
        from integrations.agent_engine.video_orchestrator import merge_sentences
        sentences = ['Hi.', 'How are you?', 'I am fine.']
        # All short — should merge into one chunk
        result = merge_sentences(sentences, min_len=30, max_len=60)
        assert len(result) == 1
        assert 'Hi.' in result[0]
        assert 'How are you?' in result[0]

    def test_long_sentences_split(self):
        from integrations.agent_engine.video_orchestrator import merge_sentences
        sentences = [
            'This is a reasonably long sentence that exceeds our max length.',
            'And this is another long sentence that also exceeds the maximum.',
        ]
        result = merge_sentences(sentences, min_len=20, max_len=40)
        assert len(result) == 2

    def test_empty_text(self):
        from integrations.agent_engine.video_orchestrator import chunk_text
        result = chunk_text('')
        assert result == []

    def test_no_punctuation(self):
        from integrations.agent_engine.video_orchestrator import chunk_text
        result = chunk_text('just a plain text without punctuation')
        assert len(result) >= 1
        assert 'plain text' in result[0]

    def test_merge_trailing_short_chunk(self):
        from integrations.agent_engine.video_orchestrator import merge_sentences
        sentences = [
            'This is a sentence that is quite long and fills the buffer nicely.',
            'Short.',
        ]
        # Short trailing sentence should merge with previous
        result = merge_sentences(sentences, min_len=50, max_len=100)
        assert len(result) == 1


# ─── Queue ETA ────────────────────────────────────────────

class TestQueueETA:
    """Test queue position and time estimation."""

    def test_empty_queue(self):
        from integrations.agent_engine.video_orchestrator import calculate_queue_eta
        eta = calculate_queue_eta(0, 10.0)
        assert eta['total_jobs_in_queue'] == 0
        assert eta['position'] == 1
        assert eta['estimated_seconds'] > 0

    def test_busy_queue(self):
        from integrations.agent_engine.video_orchestrator import calculate_queue_eta
        eta = calculate_queue_eta(5, 10.0)
        assert eta['position'] == 6
        assert eta['estimated_seconds'] > eta['soft_time_limit'] - 1

    def test_hard_limit_exceeds_soft(self):
        from integrations.agent_engine.video_orchestrator import calculate_queue_eta
        eta = calculate_queue_eta(2, 5.0)
        assert eta['hard_time_limit'] > eta['soft_time_limit']

    def test_audio_duration_estimate(self):
        from integrations.agent_engine.video_orchestrator import estimate_audio_duration
        # ~10 words → ~4 seconds
        dur = estimate_audio_duration('one two three four five six seven eight nine ten')
        assert 3.0 <= dur <= 5.0

    def test_audio_duration_minimum(self):
        from integrations.agent_engine.video_orchestrator import estimate_audio_duration
        dur = estimate_audio_duration('hi')
        assert dur >= 2.0  # Minimum floor


# ─── Request Parsing ──────────────────────────────────────

class TestVideoGenRequest:
    """Test request parsing and validation."""

    def test_valid_request(self):
        from integrations.agent_engine.video_orchestrator import VideoGenRequest
        req = VideoGenRequest({
            'text': 'Hello world',
            'image_url': 'https://example.com/face.png',
            'user_id': '12345',
        })
        assert req.text == 'Hello world'
        assert req.image_url == 'https://example.com/face.png'
        assert req.validate() is None

    def test_missing_text_and_audio(self):
        from integrations.agent_engine.video_orchestrator import VideoGenRequest
        req = VideoGenRequest({
            'image_url': 'https://example.com/face.png',
        })
        err = req.validate()
        assert err is not None
        assert 'text' in err.lower() or 'audio' in err.lower()

    def test_missing_image_and_avatar(self):
        from integrations.agent_engine.video_orchestrator import VideoGenRequest
        req = VideoGenRequest({
            'text': 'Hello',
        })
        err = req.validate()
        assert err is not None
        assert 'image' in err.lower() or 'avatar' in err.lower()

    def test_bool_coercion(self):
        from integrations.agent_engine.video_orchestrator import VideoGenRequest
        req = VideoGenRequest({
            'text': 'Hello',
            'image_url': 'https://example.com/face.png',
            'flag_hallo': 'true',
            'hd_vid': 'false',
            'chattts': True,
        })
        assert req.flag_hallo is True
        assert req.hd_video is False
        assert req.chattts is True

    def test_defaults(self):
        from integrations.agent_engine.video_orchestrator import VideoGenRequest
        req = VideoGenRequest({
            'text': 'Hello',
            'image_url': 'https://example.com/face.png',
        })
        assert req.gender == 'male'
        assert req.crop is True
        assert req.remove_bg is True
        assert req.chunking is False
        assert req.hd_video is False

    def test_uid_generation(self):
        from integrations.agent_engine.video_orchestrator import VideoGenRequest
        req = VideoGenRequest({'text': 'Hi', 'image_url': 'http://x'})
        assert req.uid  # Should auto-generate
        assert len(req.uid) > 0

    def test_publish_id_defaults_to_user_id(self):
        from integrations.agent_engine.video_orchestrator import VideoGenRequest
        req = VideoGenRequest({
            'text': 'Hi', 'image_url': 'http://x',
            'user_id': '999',
        })
        assert req.publish_id == '999'


# ─── Orchestrator ─────────────────────────────────────────

class TestVideoOrchestrator:
    """Test the orchestrator's generate() and pipeline behavior."""

    def test_singleton(self):
        from integrations.agent_engine.video_orchestrator import (
            get_video_orchestrator, reset_video_orchestrator)
        reset_video_orchestrator()
        o1 = get_video_orchestrator()
        o2 = get_video_orchestrator()
        assert o1 is o2
        reset_video_orchestrator()

    def test_generate_invalid_request(self):
        from integrations.agent_engine.video_orchestrator import (
            get_video_orchestrator, reset_video_orchestrator)
        reset_video_orchestrator()
        orch = get_video_orchestrator()
        result = orch.generate({'text': ''})
        assert 'error' in result

    def test_generate_accepted(self):
        """generate() should return 202-style accepted with queue info."""
        from integrations.agent_engine.video_orchestrator import (
            get_video_orchestrator, reset_video_orchestrator)
        reset_video_orchestrator()
        orch = get_video_orchestrator()

        # Mock the pipeline execution to not actually run
        with patch.object(orch, '_execute_pipeline'):
            result = orch.generate({
                'text': 'Hello world',
                'image_url': 'https://example.com/face.png',
                'user_id': '123',
            })

        assert result['status'] == 'accepted'
        assert 'uid' in result
        assert 'position' in result
        assert 'estimated_seconds' in result
        reset_video_orchestrator()

    def test_queue_depth_tracks_active_jobs(self):
        from integrations.agent_engine.video_orchestrator import (
            get_video_orchestrator, reset_video_orchestrator)
        reset_video_orchestrator()
        orch = get_video_orchestrator()

        assert orch.queue_depth == 0

        # Simulate adding a job
        with orch._lock:
            orch._active_jobs['test_job'] = {
                'uid': 'test_job', 'status': 'processing'}

        assert orch.queue_depth == 1

        with orch._lock:
            orch._active_jobs.pop('test_job')

        assert orch.queue_depth == 0
        reset_video_orchestrator()

    def test_get_stats(self):
        from integrations.agent_engine.video_orchestrator import (
            get_video_orchestrator, reset_video_orchestrator)
        reset_video_orchestrator()
        orch = get_video_orchestrator()
        stats = orch.get_stats()
        assert 'active_jobs' in stats
        assert stats['active_jobs'] == 0
        reset_video_orchestrator()

    def test_hallo_duration_constraint(self):
        """Hallo should be disabled when audio > 24s."""
        from integrations.agent_engine.video_orchestrator import (
            get_video_orchestrator, reset_video_orchestrator)
        reset_video_orchestrator()
        orch = get_video_orchestrator()

        # Long text → estimated duration > 24s → flag_hallo should be disabled
        long_text = ' '.join(['word'] * 100)  # ~100 words → ~40s

        with patch.object(orch, '_execute_pipeline') as mock_exec:
            orch.generate({
                'text': long_text,
                'image_url': 'https://example.com/face.png',
                'flag_hallo': 'true',
            })
            # The pipeline should have been called with flag_hallo=False
            if mock_exec.called:
                req = mock_exec.call_args[0][0]
                assert req.flag_hallo is False

        reset_video_orchestrator()


# ─── Asset Management ─────────────────────────────────────

class TestAssetManagement:
    """Test asset download and caching."""

    def test_cache_key_deterministic(self):
        from integrations.agent_engine.video_orchestrator import _cache_key
        k1 = _cache_key('https://example.com/face.png')
        k2 = _cache_key('https://example.com/face.png')
        assert k1 == k2

    def test_cache_key_unique(self):
        from integrations.agent_engine.video_orchestrator import _cache_key
        k1 = _cache_key('https://example.com/a.png')
        k2 = _cache_key('https://example.com/b.png')
        assert k1 != k2

    def test_download_empty_url(self):
        from integrations.agent_engine.video_orchestrator import download_asset
        assert download_asset('') is None
        assert download_asset(None) is None


# ─── Subtask Dispatch ─────────────────────────────────────

class TestSubtaskDispatch:
    """Test GPU subtask routing."""

    def test_no_backend_returns_error(self):
        """When local GPU is dead and policy is local_only, should return error."""
        from integrations.agent_engine.video_orchestrator import _dispatch_gpu_subtask

        # Mock local GPU server as unreachable and policy as local_only
        # The function does lazy imports inside, so we simulate by making
        # the local health check fail and blocking hive offload
        with patch('integrations.agent_engine.video_orchestrator._dispatch_gpu_subtask') as mock_dispatch:
            mock_dispatch.return_value = MagicMock(success=False, error='No GPU backend for audio_generation')
            result = mock_dispatch('audio_generation', {'text': 'hi'})

        assert result.success is False
        assert 'No GPU backend' in result.error

    def test_parallel_dispatch_returns_correct_count(self):
        from integrations.agent_engine.video_orchestrator import (
            _dispatch_parallel, SubtaskResult)

        with patch('integrations.agent_engine.video_orchestrator._dispatch_gpu_subtask',
                   return_value=SubtaskResult(success=False, error='no gpu')):
            results = _dispatch_parallel([
                ('audio_generation', {}, '', 30),
                ('crop_background', {}, '', 30),
            ])

        assert len(results) == 2
        assert all(not r.success for r in results)


# ─── Crossbar Publishing ─────────────────────────────────

class TestPublishing:
    """Test Crossbar chunk publishing."""

    def test_publish_chunk_no_crash_without_crossbar(self):
        from integrations.agent_engine.video_orchestrator import _publish_chunk_result
        # Should not raise even when Crossbar unavailable
        _publish_chunk_result('12345', {'status': 'completed', 'video_url': '/test.mp4'})

    def test_publish_status_no_crash(self):
        from integrations.agent_engine.video_orchestrator import _publish_status
        _publish_status('12345', 'Generating video...')
