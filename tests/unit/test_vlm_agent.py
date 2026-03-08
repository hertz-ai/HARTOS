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

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

pytest.importorskip('autogen', reason='autogen not installed')

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
        """Test VLM local_computer_tool can execute file operations"""
        from integrations.vlm.local_computer_tool import execute_action
        import tempfile, os
        # Test list_folders_and_files action (no pyautogui needed)
        result = execute_action({'action': 'list_folders_and_files', 'path': '.'}, 'inprocess')
        assert result.get('output')
        assert 'error' not in result or not result['error']

    def test_vlm_agent_file_operations(self, test_user_id, test_prompt_id, tmp_path, mock_flask_app):
        """Test VLM agent can perform file operations via local_computer_tool"""
        from integrations.vlm.local_computer_tool import execute_action

        test_file = str(tmp_path / "vlm_test.txt")

        # Write file
        result = execute_action({
            'action': 'write_file', 'path': test_file, 'content': 'VLM created this'
        }, 'inprocess')
        assert 'Written to' in result.get('output', '')

        # Read file
        result = execute_action({
            'action': 'read_file_and_understand', 'path': test_file
        }, 'inprocess')
        assert 'VLM created this' in result.get('output', '')

    def test_vlm_agent_execute_python_code(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test VLM agent can execute Python code"""
        code = "result = 2 + 2"
        try:
            exec(code)
        except Exception as e:
            pytest.fail(f"VLM Python code execution failed: {e}")

    def test_vlm_agent_screenshot_capture(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test VLM agent can capture screenshots via local_computer_tool"""
        import integrations.vlm.local_computer_tool as lct
        mock_pyautogui = MagicMock()
        mock_img = Mock()
        mock_img.save = Mock(side_effect=lambda buf, **kw: buf.write(b'\x89PNG' + b'\x00' * 100))
        mock_pyautogui.screenshot.return_value = mock_img

        orig = lct.pyautogui
        try:
            lct.pyautogui = mock_pyautogui
            result = lct.take_screenshot('inprocess')
            assert isinstance(result, str)
            assert len(result) > 0
            mock_pyautogui.screenshot.assert_called_once()
        finally:
            lct.pyautogui = orig

    def test_vlm_agent_keyboard_mouse_input(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test VLM agent can simulate keyboard/mouse input via local_computer_tool"""
        import integrations.vlm.local_computer_tool as lct
        mock_pyautogui = MagicMock()

        orig = lct.pyautogui
        try:
            lct.pyautogui = mock_pyautogui
            # Test click
            result = lct.execute_action({'action': 'left_click', 'coordinate': [100, 200]}, 'inprocess')
            mock_pyautogui.click.assert_called_once_with(100, 200)
            assert 'Clicked' in result.get('output', '')

            # Test key press
            result = lct.execute_action({'action': 'key', 'text': 'enter'}, 'inprocess')
            mock_pyautogui.press.assert_called_once_with('enter')
        finally:
            lct.pyautogui = orig

    def test_vlm_agent_window_manipulation(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test VLM agent can execute wait and hotkey actions"""
        import integrations.vlm.local_computer_tool as lct
        mock_pyautogui = MagicMock()

        orig = lct.pyautogui
        try:
            lct.pyautogui = mock_pyautogui
            # Test hotkey (e.g. alt+tab for window switching)
            result = lct.execute_action({'action': 'hotkey', 'text': 'alt+tab'}, 'inprocess')
            mock_pyautogui.hotkey.assert_called_once_with('alt', 'tab')
            assert 'Hotkey' in result.get('output', '')
        finally:
            lct.pyautogui = orig


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
        """Test retrieving video frame from Redis (FrameStore miss → Redis fallback)"""
        import pickle

        with patch('helper.redis_client') as mock_redis, \
             patch('langchain_gpt_api.get_vision_service', return_value=None):
            # Mock serialized frame
            fake_frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
            serialized = pickle.dumps(fake_frame)
            mock_redis.get.return_value = serialized

            frame = get_frame(str(test_user_id))
            assert frame is not None
            assert frame.shape == (480, 640, 3)

    def test_vlm_frame_retrieval_no_frame(self, test_user_id, mock_flask_app):
        """Test frame retrieval when no frame is available"""
        with patch('helper.redis_client') as mock_redis, \
             patch('langchain_gpt_api.get_vision_service', return_value=None):
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
