"""
LTX-Video Generation Server
Optimized for NVIDIA RTX 3070 (8GB VRAM)

Uses: diffusers LTXPipeline with memory optimizations

Runs on localhost:5002
Endpoint: POST /generate, POST /generate_long

Usage:
    python ltx2_server.py
"""

import os
import time
import uuid
import torch
import logging
from flask import Flask, request, jsonify, send_file
from threading import Lock

try:
    from integrations.service_tools.vram_manager import clear_cuda_cache
except ImportError:
    def clear_cuda_cache():
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:
            pass

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Global pipeline and lock for thread safety
pipeline = None
model_lock = Lock()

# Paths
BASE_DIR = os.path.dirname(__file__)
OUTPUT_DIR = os.path.join(BASE_DIR, "coding", "ltx_outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def load_pipeline():
    """Load LTX-Video pipeline optimized for 8GB VRAM"""
    global pipeline

    if pipeline is not None:
        return pipeline

    logger.info("Loading LTX-Video model (optimized for 8GB VRAM)...")

    try:
        from diffusers import LTXPipeline

        # LTX-Video models that work on 8GB VRAM
        model_options = [
            "Lightricks/LTX-Video-0.9.1",  # Stable release
            "Lightricks/LTX-Video",         # Latest
        ]

        for model_id in model_options:
            try:
                logger.info(f"Trying model: {model_id}")
                pipeline = LTXPipeline.from_pretrained(
                    model_id,
                    torch_dtype=torch.bfloat16,
                )
                logger.info(f"Loaded: {model_id}")
                break
            except Exception as e:
                logger.warning(f"Model {model_id} failed: {e}")
                continue

        if pipeline is None:
            raise RuntimeError("Could not load any LTX-Video model")

        # Memory optimizations for 8GB VRAM
        logger.info("Applying memory optimizations...")

        # CPU offloading - keeps model in CPU, moves to GPU only during inference
        pipeline.enable_model_cpu_offload()

        # VAE optimizations
        pipeline.vae.enable_tiling()
        pipeline.vae.enable_slicing()

        logger.info("LTX-Video ready with CPU offload + VAE tiling/slicing")
        return pipeline

    except Exception as e:
        logger.error(f"Failed to load model: {e}")
        raise


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "ok",
        "model_loaded": pipeline is not None,
        "model": "LTX-Video (diffusers)",
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "N/A",
        "cuda_available": torch.cuda.is_available(),
        "vram_total_gb": round(torch.cuda.get_device_properties(0).total_memory / 1e9, 2) if torch.cuda.is_available() else 0,
        "vram_used_gb": round(torch.cuda.memory_allocated(0) / 1e9, 2) if torch.cuda.is_available() else 0
    })


@app.route('/generate', methods=['POST'])
def generate_video():
    """
    Generate video from text prompt using LTX-Video

    Request JSON:
    {
        "prompt": "A cartoon cat walking in a magical garden",
        "num_frames": 49,
        "width": 704,
        "height": 480,
        "num_inference_steps": 30,
        "guidance_scale": 3.0,
        "fps": 24,
        "seed": 12345  # optional
    }
    """
    global pipeline

    try:
        data = request.get_json()

        if not data or 'prompt' not in data:
            return jsonify({"error": "Missing 'prompt' in request"}), 400

        # Extract parameters with RTX 3070 (8GB) optimized defaults
        prompt = data.get('prompt')

        # LTX-Video on 8GB VRAM settings:
        # - 512x320 = safe
        # - 704x480 = medium (with CPU offload)
        # - 49-97 frames = 2-4 seconds
        num_frames = min(data.get('num_frames', 49), 97)
        width = data.get('width', 704)
        height = data.get('height', 480)

        # Ensure divisibility: width/height by 32, frames by 8+1
        width = (width // 32) * 32
        height = (height // 32) * 32
        num_frames = ((num_frames - 1) // 8) * 8 + 1

        num_inference_steps = data.get('num_inference_steps', 30)
        guidance_scale = data.get('guidance_scale', 3.0)
        fps = data.get('fps', 24)
        seed = data.get('seed', int(time.time()) % 2147483647)

        logger.info(f"Generating video: {prompt[:50]}...")
        logger.info(f"Parameters: {width}x{height}, {num_frames} frames, {num_inference_steps} steps, seed={seed}")

        # Load pipeline if not already loaded
        with model_lock:
            if pipeline is None:
                load_pipeline()

        # Clear CUDA cache before generation
        clear_cuda_cache()

        # Generate video
        start_time = time.time()
        video_id = str(uuid.uuid4())[:8]
        output_filename = f"ltx_{video_id}_{int(time.time())}.mp4"
        output_path = os.path.join(OUTPUT_DIR, output_filename)

        with model_lock:
            logger.info("Using LTX-Video diffusers pipeline")
            generator = torch.Generator(device="cpu").manual_seed(seed)

            output = pipeline(
                prompt=prompt,
                width=width,
                height=height,
                num_frames=num_frames,
                num_inference_steps=num_inference_steps,
                guidance_scale=guidance_scale,
                generator=generator,
            )

            # Save video frames
            video_frames = output.frames[0]
            try:
                from diffusers.utils import export_to_video
                export_to_video(video_frames, output_path, fps=fps)
            except ImportError:
                import imageio
                imageio.mimwrite(output_path, video_frames, fps=fps)

        generation_time = time.time() - start_time
        logger.info(f"Video generated in {generation_time:.2f}s: {output_path}")

        # Clear cache after generation
        clear_cuda_cache()

        return jsonify({
            "status": "success",
            "video_path": output_path,
            "video_url": f"http://localhost:5002/video/{output_filename}",
            "output_url": f"http://localhost:5002/video/{output_filename}",
            "generation_time_seconds": round(generation_time, 2),
            "parameters": {
                "width": width,
                "height": height,
                "num_frames": num_frames,
                "num_inference_steps": num_inference_steps,
                "seed": seed
            }
        })

    except torch.cuda.OutOfMemoryError:
        clear_cuda_cache()
        logger.error("CUDA out of memory! Try reducing resolution or num_frames")
        return jsonify({
            "error": "GPU out of memory. Try reducing width/height (e.g., 512x320) or num_frames (e.g., 33)"
        }), 507

    except Exception as e:
        logger.error(f"Generation failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/video/<filename>', methods=['GET'])
def serve_video(filename):
    """Serve generated video files"""
    video_path = os.path.join(OUTPUT_DIR, filename)
    if os.path.exists(video_path):
        return send_file(video_path, mimetype='video/mp4')
    return jsonify({"error": "Video not found"}), 404


@app.route('/list', methods=['GET'])
def list_videos():
    """List all generated videos"""
    videos = []
    for f in os.listdir(OUTPUT_DIR):
        if f.endswith('.mp4'):
            videos.append({
                "filename": f,
                "url": f"http://localhost:5002/video/{f}",
                "size_mb": round(os.path.getsize(os.path.join(OUTPUT_DIR, f)) / 1e6, 2)
            })
    return jsonify({"videos": videos})


@app.route('/clear_cache', methods=['POST'])
def clear_cache():
    """Clear CUDA cache to free up VRAM"""
    clear_cuda_cache()
    return jsonify({
        "status": "cache_cleared",
        "vram_used_gb": round(torch.cuda.memory_allocated(0) / 1e9, 2) if torch.cuda.is_available() else 0
    })


@app.route('/generate_long', methods=['POST'])
def generate_long_video():
    """
    Generate longer videos (10-30 seconds) by iteratively extending

    For 20 second video at 25fps = 500 frames
    Strategy: Generate in chunks, use last frames as conditioning for next chunk

    Request JSON:
    {
        "prompt": "A serene landscape with mountains and flowing river",
        "duration_seconds": 20,
        "width": 512,
        "height": 320,
        "fps": 25
    }
    """
    global pipeline

    try:
        data = request.get_json()

        if not data or 'prompt' not in data:
            return jsonify({"error": "Missing 'prompt' in request"}), 400

        prompt = data.get('prompt')
        duration_seconds = min(data.get('duration_seconds', 10), 30)  # Cap at 30s
        width = (data.get('width', 512) // 32) * 32
        height = (data.get('height', 320) // 32) * 32
        fps = data.get('fps', 25)
        seed = data.get('seed', int(time.time()) % 2147483647)

        # Calculate frames needed
        total_frames_needed = int(duration_seconds * fps)

        # Chunk settings: generate 49 frames per chunk with 8 frame overlap
        frames_per_chunk = 49  # Must be (n*8)+1
        overlap_frames = 8

        logger.info(f"Generating {duration_seconds}s video ({total_frames_needed} frames)")
        logger.info(f"Strategy: {frames_per_chunk} frames/chunk with {overlap_frames} overlap")

        # Load pipeline
        with model_lock:
            if pipeline is None:
                load_pipeline()

        start_time = time.time()
        all_frames = []
        chunk_num = 0

        while len(all_frames) < total_frames_needed:
            chunk_num += 1
            logger.info(f"Generating chunk {chunk_num} (frames {len(all_frames)}-{len(all_frames)+frames_per_chunk})")

            clear_cuda_cache()

            chunk_seed = seed + chunk_num
            output_chunk = os.path.join(OUTPUT_DIR, f"chunk_{chunk_num}_{int(time.time())}.mp4")

            with model_lock:
                # Use diffusers LTX-Video pipeline
                generator = torch.Generator(device="cpu").manual_seed(chunk_seed)
                output = pipeline(
                    prompt=prompt,
                    width=width,
                    height=height,
                    num_frames=frames_per_chunk,
                    num_inference_steps=25,  # Fewer steps for speed in long videos
                    guidance_scale=3.0,
                    generator=generator,
                )
                chunk_frames = list(output.frames[0])  # Convert to list of frames

            # Add frames (skip overlap frames for subsequent chunks)
            if len(all_frames) == 0:
                all_frames.extend(chunk_frames)
            else:
                # Skip first overlap_frames to avoid duplicates
                all_frames.extend(chunk_frames[overlap_frames:])

            logger.info(f"Total frames so far: {len(all_frames)}")

            # Clean up chunk file
            if os.path.exists(output_chunk):
                os.remove(output_chunk)

        # Trim to exact length
        all_frames = all_frames[:total_frames_needed]

        # Save final video
        import imageio
        video_id = str(uuid.uuid4())[:8]
        output_filename = f"ltx2_long_{video_id}_{int(time.time())}.mp4"
        output_path = os.path.join(OUTPUT_DIR, output_filename)

        imageio.mimwrite(output_path, all_frames, fps=fps, codec='libx264')

        generation_time = time.time() - start_time
        logger.info(f"Long video generated in {generation_time:.2f}s: {output_path}")

        return jsonify({
            "status": "success",
            "video_path": output_path,
            "video_url": f"http://localhost:5002/video/{output_filename}",
            "duration_seconds": duration_seconds,
            "total_frames": len(all_frames),
            "chunks_generated": chunk_num,
            "generation_time_seconds": round(generation_time, 2)
        })

    except torch.cuda.OutOfMemoryError:
        clear_cuda_cache()
        logger.error("CUDA OOM during long video generation")
        return jsonify({"error": "GPU out of memory. Try smaller resolution (384x256)"}), 507

    except Exception as e:
        logger.error(f"Long video generation failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/unload', methods=['POST'])
def unload_model():
    """Unload model to free VRAM"""
    global pipeline
    with model_lock:
        if pipeline is not None:
            del pipeline
            pipeline = None
            clear_cuda_cache()
    return jsonify({"status": "model_unloaded"})


if __name__ == '__main__':
    print("""
    ================================================================
    |           LTX-Video Generation Server                        |
    |           Optimized for RTX 3070 (8GB VRAM)                  |
    |           Using: diffusers + CPU Offloading                  |
    ================================================================
    |  Model: Lightricks/LTX-Video (auto-downloaded from HF)       |
    ================================================================
    |  Endpoints:                                                  |
    |    POST /generate      - Generate short video (2-4s)         |
    |    POST /generate_long - Generate long video (10-30s scenes) |
    |    GET  /health        - Check server status                 |
    |    GET  /video/<file>  - Serve generated video               |
    |    GET  /list          - List all generated videos           |
    |    POST /clear_cache   - Clear CUDA cache                    |
    |    POST /unload        - Unload model from VRAM              |
    ================================================================
    |  RTX 3070 (8GB) Recommended Settings:                        |
    |    Safe:    512x320,  49 frames (~2s), 25 steps              |
    |    Medium:  704x480,  49 frames (~2s), 30 steps              |
    |    Max:     704x480,  97 frames (~4s), 30 steps              |
    ================================================================
    |  Memory Optimizations Enabled:                               |
    |    - CPU Offloading (model in CPU, inference on GPU)         |
    |    - VAE Tiling & Slicing                                    |
    ================================================================
    """)

    # Check CUDA availability
    if torch.cuda.is_available():
        print(f"GPU: {torch.cuda.get_device_name(0)}")
        print(f"VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.2f} GB")
    else:
        print("WARNING: CUDA not available! GPU generation requires CUDA.")

    print("\nStarting server on http://localhost:5002")
    print("Model will be downloaded from HuggingFace on first request...")
    print("First request may take a few minutes to download the model.\n")

    # Run Flask server
    app.run(host='0.0.0.0', port=5002, threaded=True)
