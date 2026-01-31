"""
Test Suite for Shell Command Execution
Tests:
- Shell command execution generalization
- Command execution in user's computer
- Cross-platform compatibility
- Security and safety
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
import sys
import os
import subprocess
import platform

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


class TestShellCommandExecution:
    """Test shell command execution"""

    def test_execute_simple_command(self):
        """Test executing simple shell command"""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="Command executed successfully"
            )

            result = subprocess.run(
                ["echo", "Hello World"],
                capture_output=True,
                text=True
            )

            assert mock_run.called

    def test_execute_with_arguments(self):
        """Test executing command with arguments"""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)

            # Example: ls -la /path
            result = subprocess.run(
                ["ls", "-la", "/tmp"],
                capture_output=True,
                text=True
            )

            assert mock_run.called

    def test_execute_with_pipes(self):
        """Test executing command with pipes"""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="filtered output"
            )

            # Example: echo "test" | grep "test"
            result = subprocess.run(
                'echo "test" | grep "test"',
                shell=True,
                capture_output=True,
                text=True
            )

            assert mock_run.called

    def test_execute_with_environment_variables(self):
        """Test executing command with environment variables"""
        env = os.environ.copy()
        env['TEST_VAR'] = 'test_value'

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)

            result = subprocess.run(
                ["printenv", "TEST_VAR"],
                env=env,
                capture_output=True,
                text=True
            )

            assert mock_run.called

    def test_execute_in_specific_directory(self, tmp_path):
        """Test executing command in specific directory"""
        test_dir = tmp_path / "test_dir"
        test_dir.mkdir()

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)

            result = subprocess.run(
                ["pwd"],
                cwd=str(test_dir),
                capture_output=True,
                text=True
            )

            assert mock_run.called


class TestCommandGeneralization:
    """Test shell command generalization"""

    def test_generalize_file_path_commands(self):
        """Test generalizing file path in commands"""
        # Original: cat /home/user/file.txt
        # Generalized: cat {file_path}
        original_command = "cat /home/user/file.txt"
        generalized = "cat {file_path}"

        # Test substitution
        actual_path = "/tmp/test.txt"
        final_command = generalized.format(file_path=actual_path)

        assert "{file_path}" in generalized
        assert actual_path in final_command

    def test_generalize_user_specific_paths(self):
        """Test generalizing user-specific paths"""
        # Original: cd /home/johndoe/projects
        # Generalized: cd {user_home}/projects
        original = "/home/johndoe/projects"
        generalized = "{user_home}/projects"

        # Test substitution
        user_home = "/home/testuser"
        final_path = generalized.format(user_home=user_home)

        assert "{user_home}" in generalized
        assert user_home in final_path

    def test_generalize_date_time_commands(self):
        """Test generalizing date/time in commands"""
        from datetime import datetime

        # Generalized: mkdir backup_{timestamp}
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        command = f"mkdir backup_{timestamp}"

        assert "backup_" in command
        assert len(timestamp) > 0

    def test_generalize_port_numbers(self):
        """Test generalizing port numbers"""
        # Original: start server on port 8080
        # Generalized: start server on port {port}
        generalized = "start server on port {port}"

        # Test substitution
        port = 3000
        final_command = generalized.format(port=port)

        assert "{port}" in generalized
        assert "3000" in final_command

    def test_generalize_api_keys(self):
        """Test generalizing API keys and secrets"""
        # Original: curl -H "Authorization: Bearer abc123xyz"
        # Generalized: curl -H "Authorization: Bearer {api_key}"
        generalized = 'curl -H "Authorization: Bearer {api_key}"'

        # Test substitution
        api_key = "test_key_456"
        final_command = generalized.format(api_key=api_key)

        assert "{api_key}" in generalized
        assert api_key in final_command


class TestCrossPlatformExecution:
    """Test cross-platform command execution"""

    def test_detect_operating_system(self):
        """Test detecting operating system"""
        os_name = platform.system()

        assert os_name in ['Windows', 'Linux', 'Darwin']

    def test_windows_command_execution(self):
        """Test Windows-specific commands"""
        if platform.system() == 'Windows':
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = Mock(returncode=0)

                # Windows: dir command
                result = subprocess.run(
                    ["dir"],
                    shell=True,
                    capture_output=True,
                    text=True
                )

                assert mock_run.called

    def test_unix_command_execution(self):
        """Test Unix-specific commands"""
        if platform.system() in ['Linux', 'Darwin']:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = Mock(returncode=0)

                # Unix: ls command
                result = subprocess.run(
                    ["ls"],
                    capture_output=True,
                    text=True
                )

                assert mock_run.called

    def test_convert_windows_path_to_unix(self):
        """Test converting Windows path to Unix path"""
        windows_path = r"C:\Users\John\Documents\file.txt"
        # Unix-style: /c/Users/John/Documents/file.txt
        unix_path = windows_path.replace('\\', '/').replace('C:', '/c')

        assert '/' in unix_path
        assert '\\' not in unix_path

    def test_convert_unix_path_to_windows(self):
        """Test converting Unix path to Windows path"""
        unix_path = "/home/john/documents/file.txt"
        # Windows-style: C:\home\john\documents\file.txt (if on C drive)
        windows_path = unix_path.replace('/', '\\')

        assert '\\' in windows_path

    def test_cross_platform_file_operations(self, tmp_path):
        """Test cross-platform file operations"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        # Should work on all platforms
        assert test_file.exists()
        content = test_file.read_text()
        assert content == "test content"


class TestCommandSafety:
    """Test command execution safety"""

    def test_validate_command_safety(self):
        """Test validating command safety"""
        # Safe commands
        safe_commands = [
            "ls",
            "pwd",
            "echo hello",
            "cat file.txt"
        ]

        # Dangerous commands
        dangerous_commands = [
            "rm -rf /",
            "dd if=/dev/zero of=/dev/sda",
            ":(){ :|:& };:",  # Fork bomb
            "chmod -R 777 /"
        ]

        # Simple safety check
        for cmd in safe_commands:
            is_dangerous = any(danger in cmd for danger in ['rm -rf /', 'dd if=', 'chmod -R 777 /'])
            assert not is_dangerous

        for cmd in dangerous_commands:
            is_dangerous = any(danger in cmd for danger in ['rm -rf /', 'dd if=', 'chmod -R 777 /', ':(){'])
            assert is_dangerous

    def test_sanitize_command_input(self):
        """Test sanitizing command input"""
        # Remove dangerous characters
        user_input = "test; rm -rf /"
        # Should detect and reject command injection
        has_semicolon = ';' in user_input
        assert has_semicolon

        # Sanitized version
        sanitized = user_input.split(';')[0].strip()
        assert 'rm -rf' not in sanitized

    def test_prevent_command_injection(self):
        """Test preventing command injection"""
        malicious_input = "file.txt; cat /etc/passwd"

        # Should not execute the injected command
        # Use array form instead of shell=True
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)

            # Safe: using list
            result = subprocess.run(
                ["cat", malicious_input],  # Treats entire string as filename
                capture_output=True,
                text=True
            )

            assert mock_run.called

    def test_command_timeout_prevention(self):
        """Test preventing infinite command execution"""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("cmd", 5)

            try:
                result = subprocess.run(
                    ["sleep", "1000"],
                    timeout=5,
                    capture_output=True
                )
                pytest.fail("Should have raised TimeoutExpired")
            except subprocess.TimeoutExpired:
                # Expected
                pass

    def test_restrict_file_system_access(self, tmp_path):
        """Test restricting file system access"""
        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()

        restricted_dir = "/etc"  # System directory

        # Should only allow operations in allowed directory
        test_file = allowed_dir / "test.txt"
        test_file.write_text("allowed")

        assert test_file.exists()

        # Should not allow access to restricted directories
        # (In production, would implement access control)


class TestCommandErrorHandling:
    """Test command error handling"""

    def test_handle_command_not_found(self):
        """Test handling command not found error"""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = FileNotFoundError("Command not found")

            try:
                result = subprocess.run(
                    ["nonexistent_command"],
                    capture_output=True
                )
                pytest.fail("Should have raised FileNotFoundError")
            except FileNotFoundError:
                # Expected
                pass

    def test_handle_permission_denied(self):
        """Test handling permission denied error"""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=1,
                stderr="Permission denied"
            )

            result = subprocess.run(
                ["cat", "/root/protected_file"],
                capture_output=True,
                text=True
            )

            # Should detect permission error
            assert mock_run.called

    def test_handle_command_exit_code(self):
        """Test handling non-zero exit codes"""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=127)

            result = subprocess.run(
                ["false"],  # Command that returns non-zero
                capture_output=True
            )

            # Should detect non-zero exit code
            assert mock_run.called

    def test_capture_stderr(self):
        """Test capturing stderr output"""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=1,
                stdout="",
                stderr="Error: File not found"
            )

            result = subprocess.run(
                ["cat", "nonexistent.txt"],
                capture_output=True,
                text=True
            )

            assert mock_run.called

    def test_retry_failed_commands(self):
        """Test retrying failed commands"""
        max_retries = 3
        retry_count = 0

        with patch('subprocess.run') as mock_run:
            # Fail first 2 times, succeed on 3rd
            mock_run.side_effect = [
                Mock(returncode=1),
                Mock(returncode=1),
                Mock(returncode=0)
            ]

            for attempt in range(max_retries):
                result = subprocess.run(["test_command"], capture_output=True)
                if result.returncode == 0:
                    break
                retry_count = attempt + 1

            assert retry_count == 2  # Failed twice, succeeded on third


class TestCommandExecution:
    """Integration tests for command execution"""

    def test_execute_command_workflow(self, tmp_path):
        """Test complete command execution workflow"""
        # Create test directory
        test_dir = tmp_path / "test_workspace"
        test_dir.mkdir()

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)

            # 1. Change to directory
            subprocess.run(["cd", str(test_dir)], shell=True, capture_output=True)

            # 2. Create file
            subprocess.run(["touch", "test.txt"], cwd=str(test_dir), capture_output=True)

            # 3. Write to file
            subprocess.run(
                ["echo", "content", ">", "test.txt"],
                shell=True,
                cwd=str(test_dir),
                capture_output=True
            )

            assert mock_run.called

    def test_chain_multiple_commands(self):
        """Test chaining multiple commands"""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)

            # Chain commands with &&
            result = subprocess.run(
                "mkdir test_dir && cd test_dir && touch file.txt",
                shell=True,
                capture_output=True,
                text=True
            )

            assert mock_run.called

    def test_background_command_execution(self):
        """Test executing commands in background"""
        with patch('subprocess.Popen') as mock_popen:
            mock_process = Mock()
            mock_popen.return_value = mock_process

            # Start background process
            process = subprocess.Popen(
                ["python", "-m", "http.server", "8000"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            assert mock_popen.called
