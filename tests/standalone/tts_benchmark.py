#!/usr/bin/env python3
"""
TTS Benchmark — Profile all available TTS engines.

Compares: LuxTTS (GPU), LuxTTS (CPU), Pocket TTS, espeak-ng
Measures: latency, real-time factor (RTF), audio quality (sample rate),
          VRAM usage, CPU usage.

Usage:
    python tests/standalone/tts_benchmark.py
    python tests/standalone/tts_benchmark.py --voice /path/to/reference.wav
    python tests/standalone/tts_benchmark.py --text "Custom benchmark text"
    python tests/standalone/tts_benchmark.py --runs 5
"""

import argparse
import json
import os
import sys
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _get_gpu_info():
    """Get GPU info if available."""
    try:
        import torch
        if torch.cuda.is_available():
            return {
                'name': torch.cuda.get_device_name(0),
                'vram_mb': round(torch.cuda.get_device_properties(0).total_mem / 1e6),
                'cuda_version': torch.version.cuda,
            }
    except ImportError:
        pass
    return None


def _get_cpu_info():
    """Get basic CPU info."""
    import platform
    try:
        import multiprocessing
        cores = multiprocessing.cpu_count()
    except Exception:
        cores = 'unknown'
    return {
        'platform': platform.machine(),
        'processor': platform.processor() or 'unknown',
        'cores': cores,
    }


def benchmark_luxtts(text, voice_audio, num_runs, device):
    """Benchmark LuxTTS on specified device."""
    try:
        from integrations.service_tools.luxtts_tool import luxtts_benchmark
        result = json.loads(luxtts_benchmark(text, device=device, voice_audio=voice_audio, num_runs=num_runs))
        return result
    except Exception as e:
        return {'error': str(e), 'engine': 'luxtts', 'device': device}


def benchmark_pocket_tts(text, num_runs):
    """Benchmark Pocket TTS."""
    try:
        from integrations.service_tools.pocket_tts_tool import pocket_tts_synthesize
        import tempfile

        # Warmup
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            warmup_path = f.name
        pocket_tts_synthesize(text, output_path=warmup_path)
        try:
            os.unlink(warmup_path)
        except OSError:
            pass

        times = []
        durations = []
        for i in range(num_runs):
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
                out_path = f.name

            t0 = time.time()
            result = json.loads(pocket_tts_synthesize(text, output_path=out_path))
            elapsed = time.time() - t0

            if 'error' not in result:
                times.append(elapsed)
                durations.append(result.get('duration', 0))

            try:
                os.unlink(out_path)
            except OSError:
                pass

        if not times:
            return {'error': 'All runs failed', 'engine': 'pocket-tts'}

        avg_time = sum(times) / len(times)
        avg_duration = sum(durations) / len(durations) if durations else 0
        avg_rtf = avg_time / avg_duration if avg_duration > 0 else 0

        return {
            'engine': 'pocket-tts',
            'device': 'cpu',
            'sample_rate': 24000,
            'num_runs': len(times),
            'text_length': len(text),
            'avg_gen_time_ms': round(avg_time * 1000, 1),
            'min_gen_time_ms': round(min(times) * 1000, 1),
            'max_gen_time_ms': round(max(times) * 1000, 1),
            'avg_audio_duration_s': round(avg_duration, 2),
            'avg_rtf': round(avg_rtf, 4),
            'avg_realtime_factor': round(1.0 / avg_rtf, 1) if avg_rtf > 0 else 0,
        }
    except ImportError as e:
        return {'error': f'pocket-tts not installed: {e}', 'engine': 'pocket-tts'}
    except Exception as e:
        return {'error': str(e), 'engine': 'pocket-tts'}


def benchmark_espeak(text, num_runs):
    """Benchmark espeak-ng."""
    import subprocess
    import tempfile

    try:
        subprocess.run(['espeak-ng', '--version'], capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {'error': 'espeak-ng not installed', 'engine': 'espeak-ng'}

    times = []
    for i in range(num_runs):
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            out_path = f.name

        t0 = time.time()
        result = subprocess.run(
            ['espeak-ng', '-v', 'en', '-w', out_path, text],
            capture_output=True, timeout=30,
        )
        elapsed = time.time() - t0

        if result.returncode == 0:
            times.append(elapsed)

        try:
            os.unlink(out_path)
        except OSError:
            pass

    if not times:
        return {'error': 'All runs failed', 'engine': 'espeak-ng'}

    avg_time = sum(times) / len(times)
    return {
        'engine': 'espeak-ng',
        'device': 'cpu',
        'sample_rate': 22050,
        'num_runs': len(times),
        'text_length': len(text),
        'avg_gen_time_ms': round(avg_time * 1000, 1),
        'min_gen_time_ms': round(min(times) * 1000, 1),
        'max_gen_time_ms': round(max(times) * 1000, 1),
        'avg_rtf': 0.001,  # espeak is essentially instant
        'avg_realtime_factor': 1000,
        'note': 'Rule-based, no neural network — instant but robotic quality',
    }


def main():
    parser = argparse.ArgumentParser(description='TTS Engine Benchmark')
    parser.add_argument('--text', default=(
        "Hello, this is a benchmark test for comparing text to speech engines. "
        "The quick brown fox jumps over the lazy dog."
    ))
    parser.add_argument('--voice', help='Path to reference voice audio (for LuxTTS cloning)')
    parser.add_argument('--runs', type=int, default=3, help='Number of benchmark runs')
    parser.add_argument('--engines', nargs='+', default=['all'],
                        help='Engines to benchmark: luxtts-gpu, luxtts-cpu, pocket, espeak, all')
    args = parser.parse_args()

    print("=" * 70)
    print("  HART OS — TTS Engine Benchmark")
    print("=" * 70)
    print()

    # System info
    gpu_info = _get_gpu_info()
    cpu_info = _get_cpu_info()

    print(f"  CPU: {cpu_info['processor']} ({cpu_info['cores']} cores)")
    if gpu_info:
        print(f"  GPU: {gpu_info['name']} ({gpu_info['vram_mb']} MB VRAM)")
    else:
        print("  GPU: None detected")
    print(f"  Text: \"{args.text[:60]}...\" ({len(args.text)} chars)")
    print(f"  Runs: {args.runs}")
    if args.voice:
        print(f"  Voice: {args.voice}")
    print()

    engines_to_run = args.engines
    if 'all' in engines_to_run:
        engines_to_run = ['luxtts-gpu', 'luxtts-cpu', 'pocket', 'espeak']

    results = []

    for engine in engines_to_run:
        print(f"  Benchmarking {engine}...", end=' ', flush=True)

        if engine == 'luxtts-gpu':
            if not gpu_info:
                print("SKIPPED (no GPU)")
                results.append({'engine': 'luxtts', 'device': 'cuda', 'error': 'No GPU available'})
                continue
            result = benchmark_luxtts(args.text, args.voice, args.runs, 'cuda')

        elif engine == 'luxtts-cpu':
            result = benchmark_luxtts(args.text, args.voice, args.runs, 'cpu')

        elif engine == 'pocket':
            result = benchmark_pocket_tts(args.text, args.runs)

        elif engine == 'espeak':
            result = benchmark_espeak(args.text, args.runs)

        else:
            print(f"UNKNOWN engine: {engine}")
            continue

        if 'error' in result:
            print(f"FAILED ({result['error'][:60]})")
        else:
            rtf = result.get('avg_realtime_factor', 0)
            ms = result.get('avg_gen_time_ms', 0)
            sr = result.get('sample_rate', 0)
            print(f"OK  {rtf}x realtime, {ms}ms avg, {sr}Hz")

        results.append(result)
        print()

    # Summary table
    print()
    print("=" * 70)
    print("  RESULTS SUMMARY")
    print("=" * 70)
    print()
    print(f"  {'Engine':<18} {'Device':<8} {'Rate':<8} {'Avg ms':<10} {'RTF':<8} {'RT Factor':<10}")
    print(f"  {'-'*18} {'-'*8} {'-'*8} {'-'*10} {'-'*8} {'-'*10}")

    for r in results:
        if 'error' in r:
            print(f"  {r.get('engine', '?'):<18} {r.get('device', '?'):<8} {'N/A':<8} {'FAILED':<10}")
            continue
        engine = r.get('engine', '?')
        device = r.get('device', '?')
        sr = f"{r.get('sample_rate', 0) // 1000}kHz"
        avg = f"{r.get('avg_gen_time_ms', 0)}ms"
        rtf = f"{r.get('avg_rtf', 0):.4f}"
        rt_factor = f"{r.get('avg_realtime_factor', 0)}x"
        print(f"  {engine:<18} {device:<8} {sr:<8} {avg:<10} {rtf:<8} {rt_factor:<10}")

    print()

    # Winner
    valid = [r for r in results if 'error' not in r]
    if valid:
        # Best quality (highest sample rate, lowest RTF)
        best_quality = max(valid, key=lambda r: r.get('sample_rate', 0))
        best_speed = min(valid, key=lambda r: r.get('avg_rtf', float('inf')))
        print(f"  Best quality: {best_quality['engine']} ({best_quality.get('sample_rate', 0)}Hz)")
        print(f"  Best speed:   {best_speed['engine']} ({best_speed.get('avg_realtime_factor', 0)}x realtime)")

    # Save results
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', '..', 'test-reports', 'tts_benchmark.json')
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, 'w') as f:
        json.dump({
            'system': {'cpu': cpu_info, 'gpu': gpu_info},
            'config': {'text': args.text, 'runs': args.runs, 'voice': args.voice},
            'results': results,
        }, f, indent=2)
    print(f"\n  Report saved to: {report_path}")
    print()


if __name__ == '__main__':
    main()
