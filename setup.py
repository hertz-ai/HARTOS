#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Setup script for hevolve-backend package.

This allows the LLM-langchain_Chatbot-Agent project to be pip-installable.
"""

from setuptools import setup, find_packages
import os

# Read the README for long description
here = os.path.abspath(os.path.dirname(__file__))
try:
    with open(os.path.join(here, "README.md"), encoding="utf-8") as f:
        long_description = f.read()
except FileNotFoundError:
    long_description = "Hevolve Backend - LangChain-based AI Agent Server"

# Core dependencies required for the server to run
install_requires = [
    # Web framework
    "Flask>=2.3.0",
    "waitress>=2.1.0",
    "fastapi>=0.98.0",
    "uvicorn>=0.22.0",
    "starlette>=0.27.0",

    # Database
    "SQLAlchemy>=2.0.0",
    "redis>=4.6.0",

    # LangChain ecosystem
    "langchain>=0.0.230",
    "langchain-core>=0.1.0",
    "langchain-groq>=0.1.0",
    "langsmith>=0.1.0",

    # LLM providers
    "openai>=0.27.0",
    "groq>=0.5.0",

    # ML/Vector stores
    "chromadb>=0.3.0",
    "faiss-cpu>=1.7.0",
    "sentence-transformers>=2.2.0",

    # Core utilities
    "python-dotenv>=1.0.0",
    "pydantic>=1.10.0",
    "aiohttp>=3.9.0",
    "aiofiles>=23.2.0",
    "requests>=2.31.0",
    "httpx>=0.24.0",

    # Data processing
    "numpy>=1.25.0",
    "pandas>=2.0.0",
    "beautifulsoup4>=4.12.0",
    "PyPDF2>=3.0.0",

    # Tokenization
    "tiktoken>=0.5.0",
    "transformers>=4.30.0",

    # Security
    "cryptography>=41.0.0",
    "PyJWT>=2.7.0",

    # Communication
    "crossbarhttp3>=1.1",
    "websockets>=11.0.0",

    # Image processing
    "Pillow>=9.5.0",
    "opencv-python",

    # Async utilities
    "asyncio-compat>=0.1.0;python_version<'3.10'",

    # Other utilities
    "pytz>=2023.3",
    "python-multipart>=0.0.6",
    "tenacity>=8.2.0",
    "tqdm>=4.65.0",
    "coloredlogs>=15.0.0",
    "PyYAML>=6.0",
]

# Optional dependencies for specific features
extras_require = {
    "telegram": ["python-telegram-bot>=21.0"],
    "discord": ["discord.py>=2.3.0"],
    "torch": [
        "torch>=2.1.0",
        "torchvision>=0.16.0",
    ],
    "google": [
        "google-cloud-aiplatform>=1.36.0",
        "google-cloud-bigquery>=3.13.0",
        "google-cloud-storage>=2.13.0",
        "google-api-python-client>=2.90.0",
    ],
    "memory": ["simplemem>=0.1.0"],
    "dev": [
        "pytest>=7.0.0",
        "pytest-asyncio>=0.21.0",
        "pytest-cov>=4.0.0",
        "black>=23.0.0",
        "flake8>=6.0.0",
        "mypy>=1.0.0",
    ],
    "all": [
        "python-telegram-bot>=21.0",
        "discord.py>=2.3.0",
        "torch>=2.1.0",
        "torchvision>=0.16.0",
        "google-cloud-aiplatform>=1.36.0",
        "google-cloud-bigquery>=3.13.0",
        "google-cloud-storage>=2.13.0",
        "google-api-python-client>=2.90.0",
        "simplemem>=0.1.0",
    ],
}

setup(
    name="hevolve-backend",
    version="1.0.0",
    author="Hevolve Team",
    author_email="contact@hevolve.ai",
    description="LangChain-based AI Agent Server with multi-agent orchestration",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/hevolve/hevolve-backend",
    license="MIT",

    # Package discovery
    packages=find_packages(
        include=[
            "core",
            "core.*",
            "integrations",
            "integrations.*",
            "security",
            "security.*",
        ],
        exclude=[
            "venv",
            "venv*",
            "tests",
            "tests.*",
            "docs",
            "*.tests",
            "*.tests.*",
            "__pycache__",
        ],
    ),

    # Include main modules at root level
    py_modules=[
        "langchain_gpt_api",
        "helper",
        "helper_func",
        "helper_ledger",
        "models",
        "config",
        "threadlocal",
        "crossbar_server",
        "create_recipe",
        "reuse_recipe",
        "gather_agentdetails",
        "lifecycle_hooks",
        "tools_and_prompt",
    ],

    # Include non-Python files
    include_package_data=True,
    package_data={
        "": ["*.yaml", "*.yml", "*.json", "*.txt", "*.md"],
    },

    # Python version requirement
    python_requires=">=3.9",

    # Dependencies
    install_requires=install_requires,
    extras_require=extras_require,

    # Entry points for console scripts
    entry_points={
        "console_scripts": [
            "hevolve-server=langchain_gpt_api:main",
            "hevolve-crossbar=crossbar_server:main",
        ],
    },

    # Classification metadata
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],

    # Keywords for PyPI search
    keywords=[
        "langchain",
        "ai",
        "chatbot",
        "agent",
        "llm",
        "openai",
        "groq",
        "flask",
        "multi-agent",
        "orchestration",
    ],

    # Project URLs
    project_urls={
        "Bug Reports": "https://github.com/hevolve/hevolve-backend/issues",
        "Source": "https://github.com/hevolve/hevolve-backend",
        "Documentation": "https://docs.hevolve.ai",
    },
)
