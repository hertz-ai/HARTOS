#!/usr/bin/env python3
"""
setup_vlm.py — Download VLM models for HART OS.

Downloads the 0.8B caption model (+ mmproj) from HuggingFace.
The 4B action model is managed by Nunba or model_lifecycle.

Server launch is handled by model_lifecycle._restart_llm() —
this script only handles model download (infrastructure layer).

Usage:
  python scripts/setup_vlm.py              # Download 0.8B caption model
  python scripts/setup_vlm.py --benchmark  # Quick FPS benchmark (server must be running)
"""
import argparse
import os
import sys
import time

MODELS_DIR = os.path.expanduser('~/.nunba/models')

CAPTION_MODEL = {
    'hf_repo': 'unsloth/Qwen3.5-0.8B-GGUF',
    'model_file': 'Qwen3.5-0.8B-UD-Q4_K_XL.gguf',
    'mmproj_file': 'mmproj-F16.gguf',
    'mmproj_subdir': 'qwen08b',
}


def find_file(filename, subdir=''):
    for base in [MODELS_DIR, os.path.expanduser('~/.trueflow/models')]:
        path = os.path.join(base, subdir, filename) if subdir else os.path.join(base, filename)
        if os.path.isfile(path):
            return path
    return None


def download(hf_repo, filename, subdir=''):
    existing = find_file(filename, subdir)
    if existing:
        print(f'  Found: {existing} ({os.path.getsize(existing) / 1024 / 1024:.0f} MB)')
        return existing
    from huggingface_hub import hf_hub_download
    local_dir = os.path.join(MODELS_DIR, subdir) if subdir else MODELS_DIR
    os.makedirs(local_dir, exist_ok=True)
    print(f'  Downloading {hf_repo}/{filename}...')
    path = hf_hub_download(repo_id=hf_repo, filename=filename, local_dir=local_dir)
    print(f'  Saved: {path} ({os.path.getsize(path) / 1024 / 1024:.0f} MB)')
    return path


def benchmark(port):
    import requests, base64, io
    from PIL import ImageGrab, Image
    img = ImageGrab.grab().resize((256, 144), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=20)
    b64 = base64.b64encode(buf.getvalue()).decode()

    def call():
        return requests.post(f'http://127.0.0.1:{port}/v1/chat/completions', json={
            'model': 'local', 'max_tokens': 5, 'temperature': 0,
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': '3 words: what is this?'},
                {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}}
            ]}]
        }, timeout=30)

    call()
    times = []
    for _ in range(5):
        t0 = time.time()
        call()
        times.append(time.time() - t0)
    avg = sum(times) / 5
    print(f'  port {port}: avg={avg:.2f}s fps={1 / avg:.1f}  [{", ".join(f"{t:.2f}" for t in times)}]')


def main():
    p = argparse.ArgumentParser(description='Download 0.8B caption model for HART OS')
    p.add_argument('--benchmark', action='store_true', help='Run FPS benchmark (server must be running)')
    args = p.parse_args()

    m = CAPTION_MODEL
    print('=== Qwen3.5-0.8B caption model ===')
    download(m['hf_repo'], m['model_file'])
    download(m['hf_repo'], m['mmproj_file'], m['mmproj_subdir'])

    if args.benchmark:
        port = int(os.environ.get('HEVOLVE_VLM_CAPTION_PORT', 8081))
        print('\n=== Benchmark ===')
        benchmark(port)

    print('\nDone. Server launch: model_lifecycle._restart_llm()')


if __name__ == '__main__':
    main()
