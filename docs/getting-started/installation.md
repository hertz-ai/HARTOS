# Installation

Complete installation guide for HART OS.

---

## System Requirements

| Requirement | Details |
|-------------|---------|
| **Python** | 3.10 (required -- pydantic 1.10.9 is incompatible with 3.12+) |
| **OS** | Windows, Linux, macOS |
| **RAM** | 4 GB minimum, 8 GB recommended |
| **GPU** | Optional -- required only for local model inference |
| **Disk** | 2 GB for base install, more for local models |

---

## Step 1: Python Environment

```bash
# Create virtual environment
python3.10 -m venv venv310

# Activate (Linux/macOS)
source venv310/bin/activate

# Activate (Windows)
venv310\Scripts\activate.bat
```

---

## Step 2: Install Dependencies

```bash
pip install -r requirements.txt
```

### Critical Pinned Versions

These versions are pinned for compatibility and must not be upgraded without testing:

| Package | Version | Reason |
|---------|---------|--------|
| `langchain` | 0.0.230 | API compatibility |
| `pydantic` | 1.10.9 | Requires Python 3.10 |
| `autogen` | latest | Multi-agent framework |
| `chromadb` | 0.3.23 | Vector store |

---

## Step 3: API Keys

### .env File

Create `.env` in the project root:

```
OPENAI_API_KEY=your-openai-key
GROQ_API_KEY=your-groq-key
LANGCHAIN_API_KEY=your-langchain-key
```

### config.json

Create `config.json` in the project root for additional service keys:

```json
{
  "OPENAI_API_KEY": "your-key",
  "GROQ_API_KEY": "your-key",
  "GOOGLE_CSE_ID": "your-custom-search-engine-id",
  "GOOGLE_API_KEY": "your-google-api-key",
  "NEWS_API_KEY": "your-newsapi-key",
  "SERPAPI_API_KEY": "your-serpapi-key"
}
```

Not all keys are required. At minimum, provide either `OPENAI_API_KEY` or `GROQ_API_KEY`.

---

## Step 4: Verify Installation

```bash
# Start the server
python langchain_gpt_api.py

# In another terminal, check health
curl http://localhost:6777/status
```

---

## Optional: GPU Setup for Local Models

HART OS can run local models (LLaMA, Mistral, Phi, Qwen) for zero-Spark inference via the budget gate.

### NVIDIA GPU

```bash
# Install CUDA toolkit (11.8 or 12.x)
# Then install PyTorch with CUDA support
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
```

### AMD GPU (ROCm)

```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/rocm5.6
```

### Verify GPU Detection

HART OS uses a single source of truth for GPU detection via `vram_manager.detect_gpu()`:

```python
from integrations.service_tools.vram_manager import detect_gpu
gpu_info = detect_gpu()
print(gpu_info)
```

---

## Optional: Docker Deployment

```bash
# Build
docker build -t hart-os .

# Run
docker run -p 6777:6777 \
  -e OPENAI_API_KEY=your-key \
  -e GROQ_API_KEY=your-key \
  hart-os
```

---

## Running Tests

```bash
# All tests
pytest tests/ -v

# Unit tests only
pytest tests/unit/ -v

# Integration tests only
pytest tests/integration/ -v

# Specific test suites
pytest tests/unit/test_agent_creation.py -v
pytest tests/unit/test_recipe_generation.py -v
pytest tests/unit/test_reuse_mode.py -v

# Standalone suites
python tests/standalone/test_master_suite.py
python tests/standalone/test_autonomous_agent_suite.py
```

!!! note
    Use the `--noconftest` flag if you encounter fixture conflicts. Use `-p no:capture` for federation tests.

---

## Next Steps

- [Deployment Modes](deployment-modes.md) -- flat, regional, central configurations
- [Configuration Reference](configuration.md) -- all environment variables and settings
