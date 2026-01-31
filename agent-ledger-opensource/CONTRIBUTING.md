# Contributing to Agent Ledger

Thank you for your interest in contributing to Agent Ledger! This document provides guidelines for contributing to the project.

## Code of Conduct

Please be respectful and constructive in all interactions. We're building software to help the AI agent community.

## How to Contribute

### Reporting Bugs

1. Check if the bug has already been reported in Issues
2. If not, create a new issue with:
   - Clear title describing the bug
   - Steps to reproduce
   - Expected vs actual behavior
   - Python version and OS
   - Relevant code snippets

### Suggesting Features

1. Check existing issues for similar suggestions
2. Create a new issue with:
   - Clear description of the feature
   - Use case / why it's needed
   - Proposed implementation (optional)

### Pull Requests

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature-name`
3. Make your changes
4. Add tests for new functionality
5. Run the test suite: `pytest`
6. Format code: `black . && isort .`
7. Commit with clear messages
8. Push to your fork
9. Open a Pull Request

## Development Setup

```bash
# Clone your fork
git clone https://github.com/YOUR_USERNAME/agent-ledger.git
cd agent-ledger

# Create virtual environment
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Format code
black .
isort .

# Type checking
mypy agent_ledger
```

## Code Style

- Follow PEP 8 guidelines
- Use Black for formatting (line length 100)
- Use isort for import sorting
- Add type hints for all public functions
- Write docstrings for all public classes and functions
- Keep functions focused and under 50 lines when possible

## Testing

- Write tests for all new functionality
- Maintain test coverage above 80%
- Use pytest fixtures for common setup
- Test edge cases and error conditions

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=agent_ledger

# Run specific test file
pytest tests/test_core.py

# Run specific test
pytest tests/test_core.py::test_task_creation
```

## Documentation

- Update README.md for user-facing changes
- Add docstrings to new functions/classes
- Update type hints
- Include examples for new features

## Commit Messages

Use clear, descriptive commit messages:

```
feat: Add MongoDB backend support
fix: Handle empty task list in get_next_task
docs: Update README with Redis examples
test: Add tests for task state transitions
refactor: Simplify task priority sorting
```

## Areas for Contribution

- **New Backends**: SQLite, DynamoDB, Firebase, etc.
- **Performance**: Optimization for large task sets
- **Testing**: Increase test coverage
- **Documentation**: Examples, tutorials, API docs
- **Integrations**: Examples for LangChain, CrewAI, etc.
- **Bug Fixes**: Check open issues

## Questions?

Open an issue or start a discussion. We're happy to help!

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
