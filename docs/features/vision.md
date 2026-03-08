# Vision / VLM

Vision sidecar for visual understanding and embodied AI learning.

## Model Selection

The vision sidecar automatically selects the appropriate model based on available hardware:

| Hardware | Model | Format |
|----------|-------|--------|
| **GPU nodes** | MiniCPM | Native PyTorch; requires VRAM managed by `vram_manager.detect_gpu()`. |
| **CPU nodes** | MobileVLM | ONNX runtime; no GPU required. |

## API Endpoint

```
POST /visual_agent
```

Accepts an image (base64 or URL) and a text prompt. Returns the model's visual analysis as structured JSON.

## Embodied AI Learning

The vision sidecar integrates with the embodied AI learning pipeline:

- Visual observations are fed into the agent's context during CREATE mode.
- Recipes can include vision steps that are replayed in REUSE mode with cached visual features.
- Supports iterative refinement where the agent observes, acts, and observes again.

## GPU Management

GPU detection and VRAM allocation are handled by `vram_manager.detect_gpu()` and `vram_manager.clear_cuda_cache()`. These are the single sources of truth for GPU state -- do not call `torch.cuda.empty_cache()` directly.

## Source Files

- `integrations/vision/` (vision sidecar implementation)
- `integrations/service_tools/vram_manager.py`
- `langchain_gpt_api.py` (`/visual_agent` route)
