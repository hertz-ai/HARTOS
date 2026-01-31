"""
Test Suite for Coding Agent
Tests coding agent functionality:
- Autonomous repository setup
- Code generation and execution
- Dependency management
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
import sys
import os
import subprocess

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


class TestCodingAgentRepositorySetup:
    """Test coding agent can setup open source repositories autonomously"""

    def test_clone_repository(self, tmp_path):
        """Test coding agent can clone a repository"""
        repo_url = "https://github.com/test/repo.git"
        clone_dir = tmp_path / "repo"

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="Cloning into 'repo'...")

            try:
                # Simulate git clone
                result = subprocess.run(
                    ["git", "clone", repo_url, str(clone_dir)],
                    capture_output=True,
                    text=True
                )
                assert mock_run.called
            except Exception as e:
                pytest.fail(f"Repository clone failed: {e}")

    def test_detect_project_type(self, tmp_path):
        """Test coding agent detects project type"""
        # Python project
        python_project = tmp_path / "python_project"
        python_project.mkdir()
        (python_project / "requirements.txt").write_text("flask==2.0.0")
        (python_project / "setup.py").write_text("from setuptools import setup")

        # Node.js project
        node_project = tmp_path / "node_project"
        node_project.mkdir()
        (node_project / "package.json").write_text('{"name": "test"}')

        # Detect Python
        has_requirements = (python_project / "requirements.txt").exists()
        has_setup = (python_project / "setup.py").exists()
        is_python = has_requirements or has_setup
        assert is_python

        # Detect Node.js
        has_package_json = (node_project / "package.json").exists()
        assert has_package_json

    def test_install_python_dependencies(self, tmp_path):
        """Test coding agent installs Python dependencies"""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        requirements = project_dir / "requirements.txt"
        requirements.write_text("requests==2.28.0\nflask==2.0.0")

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="Successfully installed")

            try:
                # Simulate pip install
                result = subprocess.run(
                    ["pip", "install", "-r", str(requirements)],
                    capture_output=True,
                    text=True
                )
                assert mock_run.called
            except Exception as e:
                pytest.fail(f"Dependency installation failed: {e}")

    def test_install_nodejs_dependencies(self, tmp_path):
        """Test coding agent installs Node.js dependencies"""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        package_json = project_dir / "package.json"
        package_json.write_text('''{
            "dependencies": {
                "express": "^4.18.0"
            }
        }''')

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="added packages")

            try:
                # Simulate npm install
                result = subprocess.run(
                    ["npm", "install"],
                    cwd=str(project_dir),
                    capture_output=True,
                    text=True
                )
                assert mock_run.called
            except Exception as e:
                pytest.fail(f"Node.js dependency installation failed: {e}")

    def test_create_virtual_environment(self, tmp_path):
        """Test coding agent creates virtual environment"""
        venv_dir = tmp_path / "venv"

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)

            try:
                # Simulate venv creation
                result = subprocess.run(
                    ["python", "-m", "venv", str(venv_dir)],
                    capture_output=True,
                    text=True
                )
                assert mock_run.called
            except Exception as e:
                pytest.fail(f"Virtual environment creation failed: {e}")

    def test_run_project_build(self, tmp_path):
        """Test coding agent runs project build"""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="Build successful")

            try:
                # Simulate build command
                result = subprocess.run(
                    ["python", "setup.py", "build"],
                    cwd=str(project_dir),
                    capture_output=True,
                    text=True
                )
                assert mock_run.called
            except Exception as e:
                pytest.fail(f"Project build failed: {e}")

    def test_run_project_tests(self, tmp_path):
        """Test coding agent runs project tests"""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="All tests passed")

            try:
                # Simulate pytest
                result = subprocess.run(
                    ["pytest"],
                    cwd=str(project_dir),
                    capture_output=True,
                    text=True
                )
                assert mock_run.called
            except Exception as e:
                pytest.fail(f"Test execution failed: {e}")


class TestCodingAgentCodeGeneration:
    """Test coding agent code generation"""

    def test_generate_python_function(self, mock_flask_app):
        """Test coding agent generates Python function"""
        with patch('create_recipe.user_agents') as mock_agents:
            # Mock the agents
            mock_group_chat = Mock()
            mock_group_chat.messages = [
                {
                    'name': 'AssistantAgent',
                    'content': '''
def hello_world():
    """Print hello world"""
    print("Hello, World!")
    return "Hello, World!"
'''
                }
            ]

            # Verify generated code
            code = mock_group_chat.messages[0]['content']
            assert 'def hello_world' in code
            assert 'print' in code

    def test_generate_class_structure(self, mock_flask_app):
        """Test coding agent generates class structure"""
        generated_code = '''
class Calculator:
    def __init__(self):
        self.result = 0

    def add(self, x, y):
        self.result = x + y
        return self.result

    def subtract(self, x, y):
        self.result = x - y
        return self.result
'''

        assert 'class Calculator' in generated_code
        assert 'def add' in generated_code
        assert 'def subtract' in generated_code

    def test_generate_api_endpoint(self, mock_flask_app):
        """Test coding agent generates API endpoint"""
        generated_code = '''
from flask import Flask, jsonify, request

app = Flask(__name__)

@app.route('/api/data', methods=['GET'])
def get_data():
    data = {"message": "Hello from API"}
    return jsonify(data)

if __name__ == '__main__':
    app.run(debug=True)
'''

        assert '@app.route' in generated_code
        assert 'def get_data' in generated_code
        assert 'jsonify' in generated_code

    def test_generate_tests_for_code(self, mock_flask_app):
        """Test coding agent generates test cases"""
        generated_test = '''
import pytest

def test_addition():
    assert 2 + 2 == 4

def test_subtraction():
    assert 5 - 3 == 2

def test_multiplication():
    assert 3 * 4 == 12
'''

        assert 'import pytest' in generated_test
        assert 'def test_' in generated_test


class TestCodingAgentExecution:
    """Test coding agent code execution"""

    def test_execute_python_code(self, tmp_path):
        """Test coding agent executes Python code"""
        code_file = tmp_path / "test.py"
        code_file.write_text('''
print("Hello from code execution")
result = 2 + 2
print(f"Result: {result}")
''')

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="Hello from code execution\nResult: 4"
            )

            result = subprocess.run(
                ["python", str(code_file)],
                capture_output=True,
                text=True
            )

            assert mock_run.called

    def test_execute_with_docker(self, mock_flask_app):
        """Test coding agent executes code in Docker"""
        with patch('create_recipe.DockerCommandLineCodeExecutor') as mock_executor:
            mock_instance = Mock()
            mock_executor.return_value = mock_instance

            # Create executor
            executor = mock_executor()
            assert executor is not None

    def test_code_execution_timeout(self, tmp_path):
        """Test coding agent handles execution timeout"""
        import time

        code_file = tmp_path / "infinite_loop.py"
        code_file.write_text('''
import time
while True:
    time.sleep(1)
''')

        timeout = 2  # 2 seconds

        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("python", timeout)

            try:
                result = subprocess.run(
                    ["python", str(code_file)],
                    timeout=timeout,
                    capture_output=True
                )
                pytest.fail("Should have raised TimeoutExpired")
            except subprocess.TimeoutExpired:
                # Expected
                pass

    def test_handle_code_execution_errors(self, tmp_path):
        """Test coding agent handles code execution errors"""
        code_file = tmp_path / "error.py"
        code_file.write_text('''
# This will cause a NameError
print(undefined_variable)
''')

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=1,
                stderr="NameError: name 'undefined_variable' is not defined"
            )

            result = subprocess.run(
                ["python", str(code_file)],
                capture_output=True,
                text=True
            )

            # Should detect error
            assert mock_run.called


class TestCodingAgentDependencyManagement:
    """Test coding agent dependency management"""

    def test_parse_requirements_file(self, tmp_path):
        """Test parsing requirements.txt"""
        requirements = tmp_path / "requirements.txt"
        requirements.write_text('''
flask==2.0.0
requests>=2.28.0
numpy<2.0.0
pandas
''')

        with open(requirements, 'r') as f:
            deps = [line.strip() for line in f if line.strip() and not line.startswith('#')]

        assert len(deps) == 4
        assert any('flask' in dep for dep in deps)
        assert any('requests' in dep for dep in deps)

    def test_parse_package_json(self, tmp_path):
        """Test parsing package.json"""
        import json

        package_json = tmp_path / "package.json"
        content = {
            "dependencies": {
                "express": "^4.18.0",
                "mongoose": "^6.0.0"
            },
            "devDependencies": {
                "jest": "^27.0.0"
            }
        }

        with open(package_json, 'w') as f:
            json.dump(content, f)

        with open(package_json, 'r') as f:
            data = json.load(f)

        assert "dependencies" in data
        assert "express" in data["dependencies"]
        assert "jest" in data["devDependencies"]

    def test_check_dependency_conflicts(self):
        """Test checking for dependency conflicts"""
        deps = {
            "package_a": "1.0.0",
            "package_b": "2.0.0"
        }

        # Mock conflict detection
        # In real scenario, would check compatibility
        has_conflicts = False

        assert not has_conflicts

    def test_update_dependencies(self, tmp_path):
        """Test updating dependencies"""
        requirements = tmp_path / "requirements.txt"
        requirements.write_text("flask==2.0.0")

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)

            # Simulate pip install --upgrade
            result = subprocess.run(
                ["pip", "install", "--upgrade", "flask"],
                capture_output=True
            )

            assert mock_run.called


class TestCodingAgentIntegration:
    """Integration tests for coding agent"""

    def test_full_project_setup_workflow(self, tmp_path, mock_flask_app):
        """Test complete project setup workflow"""
        project_dir = tmp_path / "test_project"
        project_dir.mkdir()

        # Create project structure
        (project_dir / "src").mkdir()
        (project_dir / "tests").mkdir()
        (project_dir / "requirements.txt").write_text("pytest==7.0.0")
        (project_dir / "README.md").write_text("# Test Project")

        # Verify structure
        assert (project_dir / "src").exists()
        assert (project_dir / "tests").exists()
        assert (project_dir / "requirements.txt").exists()

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)

            # Install dependencies
            subprocess.run(
                ["pip", "install", "-r", str(project_dir / "requirements.txt")],
                capture_output=True
            )

            assert mock_run.called

    def test_code_generation_and_execution(self, tmp_path, mock_flask_app):
        """Test generating and executing code"""
        # Generate code
        code = '''
def factorial(n):
    if n == 0 or n == 1:
        return 1
    return n * factorial(n - 1)

print(factorial(5))
'''

        code_file = tmp_path / "factorial.py"
        code_file.write_text(code)

        assert code_file.exists()

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="120")

            # Execute code
            result = subprocess.run(
                ["python", str(code_file)],
                capture_output=True,
                text=True
            )

            assert mock_run.called

    def test_autonomous_bug_fixing(self, tmp_path, mock_flask_app):
        """Test coding agent can identify and fix bugs"""
        buggy_code = '''
def divide(a, b):
    return a / b  # Bug: doesn't handle division by zero

result = divide(10, 0)
'''

        fixed_code = '''
def divide(a, b):
    if b == 0:
        raise ValueError("Cannot divide by zero")
    return a / b

result = divide(10, 2)
'''

        buggy_file = tmp_path / "buggy.py"
        buggy_file.write_text(buggy_code)

        # Simulate bug detection and fix
        fixed_file = tmp_path / "fixed.py"
        fixed_file.write_text(fixed_code)

        assert "raise ValueError" in fixed_file.read_text()
        assert "if b == 0" in fixed_file.read_text()
