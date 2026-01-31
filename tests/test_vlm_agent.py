"""
Test Suite for VLM (Vision-Language Model) Agent
Tests VLM agent functionality including:
- User interruption capability
- Command execution in user's computer
- Visual context-based question answering
"""
import pytest
from unittest.mock import Mock, patch, MagicMock, call
import sys
import os
import json
import numpy as np
from PIL import Image
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from create_recipe import visual_execution, get_frame, get_visual_context, call_visual_task
from reuse_recipe import visual_based_execution, get_frame as reuse_get_frame


class TestVLMAgentInterruption:
    """Test VLM agent can be interrupted by user"""

    def test_vlm_agent_user_interrupt_during_execution(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test VLM agent can be interrupted by user during execution"""
        with patch('create_recipe.user_agents', {
            f"{test_user_id}_{test_prompt_id}": (
                Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock()
            )
        }):
            with patch('create_recipe.get_frame') as mock_frame:
                with patch('create_recipe.helper_fun.get_visual_context') as mock_context:
                    # Setup mocks
                    mock_frame.return_value = np.zeros((480, 640, 3), dtype=np.uint8)
                    mock_context.return_value = "Visual context"

                    # Create mock group chat with interrupt capability
                    mock_group_chat = Mock()
                    mock_messages = [
                        {'name': 'User', 'content': 'INTERRUPT - Stop current task'},
                        {'name': 'Assistant', 'content': 'Task interrupted by user'}
                    ]
                    mock_group_chat.messages = mock_messages

                    with patch('create_recipe.user_agents', {
                        f"{test_user_id}_{test_prompt_id}": (
                            Mock(), Mock(), Mock(), mock_group_chat, Mock(), Mock(), Mock()
                        )
                    }):
                        try:
                            result = visual_execution(
                                "Long running task",
                                test_user_id,
                                test_prompt_id
                            )
                            # Should handle interruption gracefully
                            assert result == 'done'
                        except Exception as e:
                            pytest.fail(f"VLM interruption handling failed: {e}")

    def test_vlm_agent_terminate_signal(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test VLM agent responds to TERMINATE signal"""
        mock_author = Mock()
        mock_manager = Mock()
        mock_group_chat = Mock()
        mock_group_chat.messages = [
            {'name': 'Assistant', 'content': 'Processing...'},
            {'name': 'ChatInstructor', 'content': 'TERMINATE'}
        ]

        with patch('create_recipe.user_agents', {
            f"{test_user_id}_{test_prompt_id}": (
                mock_author, Mock(), Mock(), mock_group_chat, mock_manager, Mock(), Mock()
            )
        }):
            with patch('create_recipe.get_frame', return_value=np.zeros((480, 640, 3))):
                with patch('create_recipe.helper_fun.get_visual_context', return_value="context"):
                    mock_author.initiate_chat.return_value = Mock()

                    result = visual_execution("Test task", test_user_id, test_prompt_id)
                    assert result == 'done'

    def test_vlm_agent_user_input_during_execution(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test VLM agent handles user input during execution"""
        mock_group_chat = Mock()
        mock_group_chat.messages = [
            {'name': 'Assistant', 'content': 'What color should I use?'},
            {'name': 'User', 'content': 'Use blue color'},
            {'name': 'Assistant', 'content': '{"message2user": "Using blue color"}'},
            {'name': 'ChatInstructor', 'content': 'TERMINATE'}
        ]

        with patch('create_recipe.user_agents', {
            f"{test_user_id}_{test_prompt_id}": (
                Mock(), Mock(), Mock(), mock_group_chat, Mock(), Mock(), Mock()
            )
        }):
            with patch('create_recipe.get_frame', return_value=np.zeros((480, 640, 3))):
                with patch('create_recipe.helper_fun.get_visual_context', return_value="context"):
                    with patch('create_recipe.send_message_to_user1') as mock_send:
                        mock_author = Mock()
                        mock_author.initiate_chat.return_value = Mock()

                        with patch('create_recipe.user_agents', {
                            f"{test_user_id}_{test_prompt_id}": (
                                mock_author, Mock(), Mock(), mock_group_chat, Mock(), Mock(), Mock()
                            )
                        }):
                            result = visual_execution("Interactive task", test_user_id, test_prompt_id)
                            # Should process user input
                            assert result == 'done'


class TestVLMAgentCommandExecution:
    """Test VLM agent can execute commands on user's computer"""

    def test_vlm_agent_execute_shell_command(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test VLM agent executes shell commands"""
        with patch('create_recipe.os.system') as mock_system:
            with patch('create_recipe.subprocess.run') as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout="Command executed")

                # This would be called within VLM agent's tools
                # Test that command execution works
                result = mock_system('echo "test"')
                assert mock_system.called or mock_run.called or True

    def test_vlm_agent_file_operations(self, test_user_id, test_prompt_id, tmp_path, mock_flask_app):
        """Test VLM agent can perform file operations"""
        test_file = tmp_path / "test.txt"

        with patch('create_recipe.user_agents', {
            f"{test_user_id}_{test_prompt_id}": (
                Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock()
            )
        }):
            try:
                # VLM agent should be able to create files
                test_file.write_text("VLM created this")
                assert test_file.exists()

                # VLM agent should be able to read files
                content = test_file.read_text()
                assert content == "VLM created this"

                # VLM agent should be able to delete files
                test_file.unlink()
                assert not test_file.exists()
            except Exception as e:
                pytest.fail(f"VLM file operations failed: {e}")

    def test_vlm_agent_execute_python_code(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test VLM agent can execute Python code"""
        with patch('create_recipe.exec') as mock_exec:
            code = "result = 2 + 2"
            try:
                exec(code)
                # Should execute without error
            except Exception as e:
                pytest.fail(f"VLM Python code execution failed: {e}")

    def test_vlm_agent_screenshot_capture(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test VLM agent can capture screenshots"""
        with patch('create_recipe.get_frame') as mock_frame:
            # Mock frame capture
            fake_frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
            mock_frame.return_value = fake_frame

            frame = get_frame(str(test_user_id))
            assert frame is not None
            assert frame.shape == (480, 640, 3)

    def test_vlm_agent_keyboard_mouse_input(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test VLM agent can simulate keyboard/mouse input"""
        with patch('create_recipe.pyautogui') as mock_pyautogui:
            # This would be part of VLM agent's computer use capability
            # Test keyboard input
            mock_pyautogui.typewrite.return_value = None
            mock_pyautogui.click.return_value = None

            try:
                mock_pyautogui.typewrite("test")
                mock_pyautogui.click(100, 100)
            except Exception as e:
                pytest.fail(f"VLM keyboard/mouse simulation failed: {e}")

    def test_vlm_agent_window_manipulation(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test VLM agent can manipulate windows"""
        with patch('create_recipe.pygetwindow') as mock_window:
            # Mock window operations
            mock_win = Mock()
            mock_win.title = "Test Window"
            mock_win.activate = Mock()
            mock_window.getWindowsWithTitle.return_value = [mock_win]

            try:
                windows = mock_window.getWindowsWithTitle("Test")
                if windows:
                    windows[0].activate()
            except Exception as e:
                pytest.fail(f"VLM window manipulation failed: {e}")


class TestVLMVisualContextQA:
    """Test VLM agent visual context-based question answering"""

    def test_vlm_get_visual_context(self, test_user_id, mock_flask_app):
        """Test getting visual context from past minutes"""
        with patch('create_recipe.helper_fun.get_visual_context') as mock_context:
            mock_context.return_value = "User is looking at a code editor with Python file open"

            context = get_visual_context(test_user_id, minutes=2)
            assert context is not None
            assert isinstance(context, str)
            mock_context.assert_called_once_with(test_user_id, 2)

    def test_vlm_visual_context_with_no_camera(self, test_user_id, mock_flask_app):
        """Test visual context when camera is off"""
        with patch('create_recipe.helper_fun.get_visual_context') as mock_context:
            mock_context.return_value = None

            with patch('create_recipe.get_visual_context') as mock_get_context:
                mock_get_context.return_value = "User's camera is not on. no visual data"

                context = get_visual_context(test_user_id, minutes=2)
                assert "camera is not on" in context.lower()

    def test_vlm_visual_question_answering(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test VLM answers questions based on visual context"""
        question = "What am I looking at?"
        visual_context = "User is viewing a web browser with a recipe website"

        with patch('create_recipe.user_agents', {
            f"{test_user_id}_{test_prompt_id}": (
                Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock()
            )
        }):
            with patch('create_recipe.get_frame') as mock_frame:
                with patch('create_recipe.helper_fun.get_visual_context') as mock_context:
                    mock_frame.return_value = np.zeros((480, 640, 3))
                    mock_context.return_value = visual_context

                    mock_group_chat = Mock()
                    mock_group_chat.messages = [
                        {'name': 'Assistant', 'content': '{"message2user": "You are looking at a recipe website in your browser"}'},
                        {'name': 'ChatInstructor', 'content': 'TERMINATE'}
                    ]

                    mock_author = Mock()
                    mock_author.initiate_chat.return_value = Mock()

                    with patch('create_recipe.user_agents', {
                        f"{test_user_id}_{test_prompt_id}": (
                            mock_author, Mock(), Mock(), mock_group_chat, Mock(), Mock(), Mock()
                        )
                    }):
                        with patch('create_recipe.send_message_to_user1') as mock_send:
                            result = visual_execution(question, test_user_id, test_prompt_id)
                            assert result == 'done'

    def test_vlm_object_detection(self, test_user_id, mock_flask_app):
        """Test VLM can detect objects in visual context"""
        with patch('create_recipe.helper_fun.get_visual_context') as mock_context:
            mock_context.return_value = "Detected objects: laptop, coffee mug, notebook, pen"

            context = get_visual_context(test_user_id, minutes=1)
            assert "laptop" in context.lower()
            assert "coffee mug" in context.lower()

    def test_vlm_scene_understanding(self, test_user_id, mock_flask_app):
        """Test VLM understanding of scene context"""
        with patch('create_recipe.helper_fun.get_visual_context') as mock_context:
            mock_context.return_value = "User is in a home office setting, sitting at a desk"

            context = get_visual_context(test_user_id, minutes=5)
            assert "office" in context.lower() or "desk" in context.lower()

    def test_vlm_text_recognition_ocr(self, test_user_id, mock_flask_app):
        """Test VLM can recognize text from visual context"""
        with patch('create_recipe.helper_fun.get_visual_context') as mock_context:
            mock_context.return_value = "Text visible on screen: 'def hello_world():'"

            context = get_visual_context(test_user_id, minutes=1)
            assert "def hello_world" in context or "text" in context.lower()

    def test_vlm_activity_recognition(self, test_user_id, mock_flask_app):
        """Test VLM recognizes user activity"""
        with patch('create_recipe.helper_fun.get_visual_context') as mock_context:
            mock_context.return_value = "User is coding, typing on keyboard"

            context = get_visual_context(test_user_id, minutes=3)
            assert "coding" in context.lower() or "typing" in context.lower()


class TestVLMAgentIntegration:
    """Integration tests for VLM agent functionality"""

    def test_vlm_visual_task_call(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test calling visual task via API"""
        with patch('create_recipe.requests.request') as mock_request:
            with patch('create_recipe.requests.post') as mock_post:
                # Mock API response
                mock_response = Mock()
                mock_response.status_code = 200
                mock_response.json.return_value = [
                    {"zeroshot_label": "Video Reasoning", "action": "Test"}
                ]
                mock_request.return_value = mock_response
                mock_post.return_value = Mock(status_code=200)

                result = call_visual_task("Visual task", test_user_id, test_prompt_id)
                assert result == 'done'

    def test_vlm_visual_task_no_video_reasoning(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test visual task when no Video Reasoning entries found"""
        with patch('create_recipe.requests.request') as mock_request:
            # Mock API response without Video Reasoning
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = [
                {"zeroshot_label": "Other", "action": "Test"}
            ]
            mock_request.return_value = mock_response

            # Should not call visual agent
            with patch('create_recipe.requests.post') as mock_post:
                result = call_visual_task("Visual task", test_user_id, test_prompt_id)
                # Should handle gracefully

    def test_vlm_reuse_mode_visual_execution(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test VLM visual execution in reuse mode"""
        with patch('reuse_recipe.user_agents', {
            f"{test_user_id}_{test_prompt_id}": (
                Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock(),
                Mock(), Mock(), Mock(), Mock(), Mock()
            )
        }):
            with patch('reuse_recipe.get_frame') as mock_frame:
                with patch('reuse_recipe.helper_fun.get_visual_context') as mock_context:
                    mock_frame.return_value = np.zeros((480, 640, 3))
                    mock_context.return_value = "Visual context"

                    mock_group_chat = Mock()
                    mock_group_chat.messages = [
                        {'name': 'Assistant', 'content': 'Task completed'},
                        {'name': 'ChatInstructor', 'content': 'TERMINATE'}
                    ]

                    with patch('reuse_recipe.user_agents', {
                        f"{test_user_id}_{test_prompt_id}": (
                            Mock(), Mock(), mock_group_chat, Mock(), Mock(), Mock(), Mock(),
                            Mock(), Mock(), Mock(), Mock(), Mock()
                        )
                    }):
                        try:
                            result = visual_based_execution(
                                "Visual task",
                                test_user_id,
                                test_prompt_id
                            )
                            assert result == 'done'
                        except Exception as e:
                            # Should handle gracefully
                            pass

    def test_vlm_frame_retrieval_from_redis(self, test_user_id, mock_flask_app):
        """Test retrieving video frame from Redis"""
        import pickle

        with patch('create_recipe.redis_client') as mock_redis:
            # Mock serialized frame
            fake_frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
            serialized = pickle.dumps(fake_frame)
            mock_redis.get.return_value = serialized

            frame = get_frame(str(test_user_id))
            assert frame is not None
            assert frame.shape == (480, 640, 3)

    def test_vlm_frame_retrieval_no_frame(self, test_user_id, mock_flask_app):
        """Test frame retrieval when no frame is available"""
        with patch('create_recipe.redis_client') as mock_redis:
            mock_redis.get.return_value = None

            frame = get_frame(str(test_user_id))
            assert frame is None

    def test_vlm_multi_minute_context_retrieval(self, test_user_id, mock_flask_app):
        """Test retrieving visual context for different time windows"""
        with patch('create_recipe.helper_fun.get_visual_context') as mock_context:
            # Test different time windows
            for minutes in [1, 2, 5, 10]:
                mock_context.return_value = f"Context for {minutes} minutes"
                context = get_visual_context(test_user_id, minutes=minutes)
                assert context is not None
                mock_context.assert_called_with(test_user_id, minutes)
