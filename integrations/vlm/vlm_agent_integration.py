"""
VLM Agent Integration Module

Bridges VLM agent's visual computer use capabilities with the agent ledger system.
Provides visual feedback context injection and Windows command execution.

The VLM agent (from OmniParser) provides:
- Screen understanding via OmniParser
- GUI interaction (click, type, scroll, etc.)
- File operations (read, write, list, copy)
- Windows command execution

This module integrates VLM feedback into the agent ledger for:
- Visual task verification
- Screen state tracking
- GUI automation context
- Computer use feedback loop
"""

import json
import logging
import os
from typing import Dict, Any, Optional, List
from datetime import datetime
from pathlib import Path
from core.http_pool import pooled_get, pooled_post

logger = logging.getLogger(__name__)


class VLMAgentContext:
    """
    Manages VLM agent context and feedback integration with the agent ledger.

    Provides methods to:
    1. Inject visual feedback from VLM agent into agent context
    2. Track screen state and GUI actions
    3. Execute Windows commands through VLM agent
    4. Update ledger with visual verification results
    """

    def __init__(self, vlm_server_url: str = None, omniparser_url: str = None):
        if vlm_server_url is None:
            vlm_server_url = f"http://localhost:{os.environ.get('VLM_GUI_PORT', '5001')}"
        if omniparser_url is None:
            omniparser_url = f"http://localhost:{os.environ.get('OMNIPARSER_PORT', '8080')}"
        """
        Initialize VLM agent context manager.

        Args:
            vlm_server_url: URL of VLM agent server (agentic_rpc.py Flask server)
            omniparser_url: URL of OmniParser server for screen parsing
        """
        self.vlm_server_url = vlm_server_url
        self.omniparser_url = omniparser_url
        self.screen_history: List[Dict[str, Any]] = []
        self.action_history: List[Dict[str, Any]] = []

    def is_vlm_available(self) -> bool:
        """Check if VLM agent server is available."""
        try:
            response = pooled_get(f"{self.vlm_server_url}/health", timeout=2)
            return response.status_code == 200
        except Exception:
            return False

    def is_omniparser_available(self) -> bool:
        """Check if OmniParser server is available."""
        try:
            response = pooled_get(f"{self.omniparser_url}/probe", timeout=2)
            return response.status_code == 200
        except Exception:
            return False

    def get_screen_context(self) -> Optional[Dict[str, Any]]:
        """
        Get current screen context from OmniParser.

        Returns:
            Dictionary with:
            - screenshot_base64: Base64 encoded screenshot
            - parsed_elements: List of detected UI elements
            - screen_info: Text description of screen
            - width, height: Screen dimensions
        """
        try:
            if not self.is_omniparser_available():
                logger.warning("OmniParser not available, skipping screen context")
                return None

            # Request screen parsing from OmniParser
            response = pooled_post(
                f"{self.omniparser_url}/parse_screen",
                json={"include_som": True},
                timeout=10
            )

            if response.status_code == 200:
                screen_data = response.json()

                # Store in history
                self.screen_history.append({
                    "timestamp": datetime.now().isoformat(),
                    "screen_info": screen_data.get("screen_info", ""),
                    "element_count": len(screen_data.get("parsed_content_list", []))
                })

                # Keep only last 10 screens
                if len(self.screen_history) > 10:
                    self.screen_history.pop(0)

                return screen_data
            else:
                logger.error(f"OmniParser returned error: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"Error getting screen context: {e}")
            return None

    def inject_visual_context_into_ledger_task(self, task_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Inject current visual screen context into a ledger task's context.

        Args:
            task_context: Existing task context dictionary

        Returns:
            Enhanced task context with visual information
        """
        screen_context = self.get_screen_context()

        if screen_context:
            task_context["visual_context"] = {
                "has_screen_info": True,
                "screen_summary": screen_context.get("screen_info", "")[:500],  # First 500 chars
                "visible_elements": len(screen_context.get("parsed_content_list", [])),
                "screen_dimensions": {
                    "width": screen_context.get("width"),
                    "height": screen_context.get("height")
                },
                "timestamp": datetime.now().isoformat()
            }
        else:
            task_context["visual_context"] = {
                "has_screen_info": False,
                "note": "VLM agent not available"
            }

        return task_context

    def execute_vlm_action(
        self,
        action: str,
        parameters: Optional[Dict[str, Any]] = None,
        user_id: str = "agent",
        prompt_id: str = "task"
    ) -> Dict[str, Any]:
        """
        Execute an action through VLM agent.

        Args:
            action: Action type (e.g., "type", "left_click", "list_folders_and_files")
            parameters: Action parameters (e.g., {"text": "Hello", "coordinate": [100, 200]})
            user_id: User identifier
            prompt_id: Prompt/task identifier

        Returns:
            Result dictionary with status and output
        """
        try:
            if not self.is_vlm_available():
                return {
                    "status": "error",
                    "message": "VLM agent server not available",
                    "action": action
                }

            # Prepare request payload
            payload = {
                "user_id": user_id,
                "prompt_id": prompt_id,
                "action": action,
                "parameters": parameters or {}
            }

            # Send action request to VLM agent
            response = pooled_post(
                f"{self.vlm_server_url}/execute_action",
                json=payload,
                timeout=30
            )

            if response.status_code == 200:
                result = response.json()

                # Track action in history
                self.action_history.append({
                    "timestamp": datetime.now().isoformat(),
                    "action": action,
                    "parameters": parameters,
                    "result": result.get("status", "unknown")
                })

                # Keep only last 50 actions
                if len(self.action_history) > 50:
                    self.action_history.pop(0)

                return result
            else:
                return {
                    "status": "error",
                    "message": f"VLM agent returned error: {response.status_code}",
                    "action": action
                }

        except Exception as e:
            logger.error(f"Error executing VLM action '{action}': {e}")
            return {
                "status": "error",
                "message": str(e),
                "action": action
            }

    def execute_windows_command(
        self,
        command: str,
        user_id: str = "agent",
        prompt_id: str = "task"
    ) -> Dict[str, Any]:
        """
        Execute a Windows command through VLM agent's computer tool.

        This uses the hotkey action to open Run dialog (Win+R) and execute commands.

        Args:
            command: Windows command to execute (e.g., "notepad", "calc", "cmd /c dir")
            user_id: User identifier
            prompt_id: Prompt/task identifier

        Returns:
            Result dictionary with status and output
        """
        try:
            # Strategy: Use Win+R to open Run dialog, then type command
            steps = [
                {
                    "action": "hotkey",
                    "parameters": {"text": "Win+R"},
                    "description": "Open Run dialog"
                },
                {
                    "action": "wait",
                    "parameters": {},
                    "description": "Wait for Run dialog"
                },
                {
                    "action": "type",
                    "parameters": {"text": command},
                    "description": f"Type command: {command}"
                },
                {
                    "action": "hotkey",
                    "parameters": {"text": "Return"},
                    "description": "Execute command"
                }
            ]

            results = []
            for step in steps:
                result = self.execute_vlm_action(
                    action=step["action"],
                    parameters=step["parameters"],
                    user_id=user_id,
                    prompt_id=prompt_id
                )
                results.append({
                    "step": step["description"],
                    "result": result
                })

                # Check if step failed
                if result.get("status") == "error":
                    return {
                        "status": "error",
                        "message": f"Failed at step: {step['description']}",
                        "command": command,
                        "results": results
                    }

            return {
                "status": "success",
                "message": f"Command executed: {command}",
                "command": command,
                "results": results
            }

        except Exception as e:
            logger.error(f"Error executing Windows command '{command}': {e}")
            return {
                "status": "error",
                "message": str(e),
                "command": command
            }

    def get_visual_feedback_for_task(self, task_description: str) -> str:
        """
        Get visual feedback about current screen state relevant to a task.

        Args:
            task_description: Description of the task being performed

        Returns:
            Text feedback about screen state
        """
        screen_context = self.get_screen_context()

        if not screen_context:
            return "Visual feedback unavailable (VLM agent not accessible)"

        feedback_parts = []
        feedback_parts.append(f"Task: {task_description}")
        feedback_parts.append(f"\nScreen Analysis:")
        feedback_parts.append(f"- Detected {len(screen_context.get('parsed_content_list', []))} UI elements")
        feedback_parts.append(f"- Screen dimensions: {screen_context.get('width')}x{screen_context.get('height')}")

        # Add summary of visible elements
        screen_info = screen_context.get("screen_info", "")
        if screen_info:
            feedback_parts.append(f"\nVisible elements:")
            feedback_parts.append(screen_info[:500])  # First 500 chars

        # Add recent action history
        if self.action_history:
            feedback_parts.append(f"\nRecent actions (last 5):")
            for action_record in self.action_history[-5:]:
                feedback_parts.append(
                    f"- {action_record['action']} -> {action_record['result']}"
                )

        return "\n".join(feedback_parts)

    def create_vlm_enabled_tool(self, tool_name: str, tool_description: str) -> Dict[str, Any]:
        """
        Create a tool definition that can be used by agents to interact with VLM.

        Args:
            tool_name: Name of the tool
            tool_description: Description of what the tool does

        Returns:
            Tool definition dictionary
        """
        return {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": tool_description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "type", "left_click", "right_click", "double_click",
                                "hover", "scroll_up", "scroll_down", "wait", "hotkey",
                                "list_folders_and_files", "Open_file_and_copy_paste",
                                "open_file_gui", "write_file", "read_file_and_understand"
                            ],
                            "description": "The action to perform"
                        },
                        "parameters": {
                            "type": "object",
                            "description": "Parameters for the action (e.g., {'text': 'Hello'}, {'coordinate': [100, 200]})"
                        },
                        "windows_command": {
                            "type": "string",
                            "description": "Optional Windows command to execute (e.g., 'notepad', 'calc')"
                        }
                    },
                    "required": ["action"]
                }
            }
        }

    def get_status_summary(self) -> Dict[str, Any]:
        """Get current status summary of VLM agent integration."""
        return {
            "vlm_available": self.is_vlm_available(),
            "omniparser_available": self.is_omniparser_available(),
            "screen_history_count": len(self.screen_history),
            "action_history_count": len(self.action_history),
            "last_screen_capture": self.screen_history[-1]["timestamp"] if self.screen_history else None,
            "last_action": self.action_history[-1] if self.action_history else None
        }


# Singleton instance
_vlm_context = None

def get_vlm_context(vlm_server_url: str = None, omniparser_url: str = None) -> VLMAgentContext:
    """Get or create the singleton VLM context instance."""
    global _vlm_context
    if _vlm_context is None:
        _vlm_context = VLMAgentContext(vlm_server_url, omniparser_url)
    return _vlm_context


if __name__ == "__main__":
    # Test the VLM integration
    print("Testing VLM Agent Integration\n")

    vlm = get_vlm_context()

    # Check availability
    print(f"VLM Agent available: {vlm.is_vlm_available()}")
    print(f"OmniParser available: {vlm.is_omniparser_available()}")

    # Get status
    status = vlm.get_status_summary()
    print(f"\nStatus: {json.dumps(status, indent=2)}")

    # Test getting screen context (if available)
    if vlm.is_omniparser_available():
        print("\nGetting screen context...")
        screen = vlm.get_screen_context()
        if screen:
            print(f"Screen dimensions: {screen.get('width')}x{screen.get('height')}")
            print(f"Detected elements: {len(screen.get('parsed_content_list', []))}")

    # Test context injection
    print("\nTesting context injection...")
    task_context = {
        "task_id": "test_task",
        "description": "Test task for VLM integration"
    }
    enhanced_context = vlm.inject_visual_context_into_ledger_task(task_context)
    print(f"Enhanced context: {json.dumps(enhanced_context, indent=2)}")
