#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Setup script for hart-backend package (formerly hevolve-backend).

HART OS - Hevolve Agentic Runtime.
"""

from setuptools import setup, find_packages
import os

# Read the README for long description
here = os.path.abspath(os.path.dirname(__file__))
try:
    with open(os.path.join(here, "README.md"), encoding="utf-8") as f:
        long_description = f.read()
except FileNotFoundError:
    long_description = "HART OS - Hevolve Agentic Runtime"

# Core dependencies required for the server to run
# Pin versions with upper bounds to prevent pip backtracking.
# Updated 2026-03-03 to match installed versions.
install_requires = [
    # Web framework
    "Flask>=3.0.0,<4.0.0",
    "waitress>=3.0.0,<4.0.0",
    "fastapi>=0.100.0,<1.0.0",
    "uvicorn>=0.30.0,<1.0.0",
    "starlette>=0.40.0,<1.0.0",

    # Database
    "SQLAlchemy>=2.0.0,<3.0.0",
    "redis>=7.0.0,<8.0.0",

    # LangChain ecosystem — pinned to avoid pip backtracking (40+ version checks)
    "langchain-classic>=1.0.0,<2.0.0",
    "langchain-core>=1.2.0,<2.0.0",
    "langchain-text-splitters>=1.0.0,<2.0.0",
    "langsmith>=0.3.0,<1.0.0",

    # LLM providers
    "openai>=2.0.0,<3.0.0",
    "groq>=0.5.0,<1.0.0",

    # ML/Vector stores (optional heavy deps — install separately if needed)
    # "chromadb>=0.3.0",
    # "faiss-cpu>=1.7.0",
    # "sentence-transformers>=2.2.0",

    # Core utilities
    "python-dotenv>=1.0.0,<2.0.0",
    "pydantic>=2.0.0,<3.0.0",
    "aiohttp>=3.9.0,<4.0.0",
    "aiofiles>=23.2.0,<26.0.0",
    "requests>=2.31.0,<3.0.0",
    "httpx>=0.27.0,<1.0.0",

    # Data processing
    "numpy>=1.25.0,<2.0.0",
    "pandas>=2.0.0,<4.0.0",
    "beautifulsoup4>=4.12.0,<5.0.0",
    "PyPDF2>=3.0.0,<4.0.0",

    # Tokenization
    "tiktoken>=0.5.0,<1.0.0",
    "transformers>=5.0.0,<6.0.0",

    # Security
    "cryptography>=41.0.0,<47.0.0",
    "PyJWT>=2.7.0,<3.0.0",

    # Communication
    "crossbarhttp3>=1.1,<2.0",
    "websockets>=11.0.0,<17.0",

    # Image processing
    "Pillow>=9.5.0,<13.0.0",
    "opencv-python",

    # Speech-to-text (ONNX runtime — no PyTorch needed, CPU-optimized)
    "sherpa-onnx>=1.11.0",

    # Other utilities
    "pytz>=2023.3",
    "python-multipart>=0.0.6,<1.0.0",
    "tenacity>=8.2.0,<10.0.0",
    "tqdm>=4.65.0,<5.0.0",
    "coloredlogs>=15.0.0,<16.0.0",
    "PyYAML>=6.0,<7.0",
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
    # biometrics: ML deps (insightface, speechbrain) belong in HevolveAI, not HARTOS
    "remote-desktop": [
        "mss>=9.0.0",
        "websockets>=12.0",
        "av>=12.0.0",
        "pynput>=1.7.0",
    ],
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
    name="hart-backend",
    # version derived from git tags via setuptools-scm (configured in pyproject.toml)
    setup_requires=["setuptools-scm>=8.0"],
    author="HART Team",
    author_email="contact@hevolve.ai",
    description="HART OS - Hevolve Agentic Runtime",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/hevolve/hart",
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
        "hart_version",
        "langchain_gpt_api",
        "helper",
        "helper_ledger",
        "threadlocal",
        "crossbar_server",
        "create_recipe",
        "reuse_recipe",
        "gather_agentdetails",
        "lifecycle_hooks",
        "cultural_wisdom",
        "exception_collector",
        "recipe_experience",
        "embedded_main",
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
            "hart=hart_cli:hart",
            "hart-server=langchain_gpt_api:main",
            "hart-crossbar=crossbar_server:main",
        ],
    },

    # Classification metadata
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
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
        "Bug Reports": "https://github.com/hevolve/hart/issues",
        "Source": "https://github.com/hevolve/hart",
        "Documentation": "https://docs.hevolve.ai",
    },
)
