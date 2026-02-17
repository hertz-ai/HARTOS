"""
Speaker Diarization Server — standalone WebSocket server for the sidecar.

Derived from speaker_diarization/main.py but with:
- Security fixes (no eval, proper JSON)
- Configurable via CLI args and env vars
- Buffer cleanup on disconnect
- GPU/CPU auto-detection

Usage:
    python -m integrations.audio.diarization_server --port 8004
"""
import argparse
import asyncio
import ast
import io
import json
import logging
import os
import sys

import numpy as np

logger = logging.getLogger('hevolve_diarization')

# Audio parameters (16kHz, 16-bit, mono)
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
CHANNELS = 1
SECONDS = 1
EXPECTED_BYTES = SAMPLE_RATE * BYTES_PER_SAMPLE * CHANNELS * SECONDS  # 32KB

# Per-user audio stream buffers
audio_streams = {}


def _parse_message(raw):
    """Parse incoming WebSocket message safely.

    Handles both JSON format and Python dict format (single quotes)
    sent by Android SpeechService.java for backward compatibility.
    """
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8')
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ast.literal_eval(raw)


async def diarization(websocket, diarize_model, output_dir, device):
    """Handle a single WebSocket connection for speaker diarization."""
    import torch

    user_id = None
    try:
        logging.info("Waiting for audio data...")
        async for message in websocket:
            parsed = _parse_message(message)
            user_id = parsed['user_id']
            pcm_bytes = parsed['chunk']

            if user_id not in audio_streams:
                audio_streams[user_id] = io.BytesIO()

            # Handle hex-encoded or binary bytes
            if isinstance(pcm_bytes, str):
                pcm_bytes = bytes.fromhex(pcm_bytes)

            audio_streams[user_id].write(pcm_bytes)

            if audio_streams[user_id].getbuffer().nbytes >= EXPECTED_BYTES:
                audio_streams[user_id].seek(0)
                audio_data_bytes = audio_streams[user_id].read()

                try:
                    with torch.no_grad():
                        logging.info(
                            f'Processing audio for user_id {user_id}')
                        audios = (
                            np.frombuffer(audio_data_bytes, np.int16)
                            .flatten()
                            .astype(np.float32) / 32768.0
                        )
                        diarize_segments = diarize_model(audios)
                        unique_speakers = diarize_segments['speaker'].unique()
                        no_of_speakers = len(unique_speakers)
                        logging.info(
                            f'Speakers: {unique_speakers} '
                            f'for user_id {user_id}')

                        # Export/append MP3 for audit trail
                        _export_audio(
                            audio_data_bytes, user_id, output_dir)

                        res = {
                            "no_of_speaker": no_of_speakers,
                            "stop_mic": no_of_speakers > 1,
                        }
                        await websocket.send(json.dumps(res))
                        logging.info(f"Result: {res}")

                except Exception as e:
                    logging.error(
                        f'Diarization error at line '
                        f'{e.__traceback__.tb_lineno}: {e}')
                finally:
                    _cleanup_stream(user_id)
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()

    except Exception as e:
        logging.debug(f"Connection ended: {e}")
    finally:
        # Cleanup buffer on disconnect (prevents memory leak)
        if user_id:
            _cleanup_stream(user_id)


def _cleanup_stream(user_id):
    """Close and remove a user's audio buffer."""
    if user_id in audio_streams:
        try:
            audio_streams[user_id].close()
        except Exception:
            pass
        del audio_streams[user_id]


def _export_audio(audio_data_bytes, user_id, output_dir):
    """Export audio chunk as MP3, appending to existing file."""
    try:
        from pydub import AudioSegment
    except ImportError:
        return

    try:
        audio_segment = AudioSegment(
            data=audio_data_bytes,
            sample_width=BYTES_PER_SAMPLE,
            frame_rate=SAMPLE_RATE,
            channels=CHANNELS,
        )
        mp3_path = os.path.join(output_dir, f'{user_id}.mp3')
        if os.path.exists(mp3_path):
            existing = AudioSegment.from_mp3(mp3_path)
            audio_segment = existing + audio_segment
        audio_segment.export(mp3_path, format='mp3')
    except Exception as e:
        logging.error(f"Failed to export audio: {e}")


async def main(port, device, output_dir, hf_token):
    """Start diarization model and WebSocket server."""
    import torch
    import websockets

    # Load diarization model
    logging.info(f"Loading diarization model on {device}...")
    try:
        from whisperx.diarize import DiarizationPipeline
        diarize_model = DiarizationPipeline(
            use_auth_token=hf_token, device=device)
    except Exception as e:
        logging.error(f"Failed to load diarization model: {e}")
        sys.exit(1)

    logging.info("Diarization model loaded")

    os.makedirs(output_dir, exist_ok=True)

    # Bind with dynamic port support
    server = await websockets.serve(
        lambda ws, path=None: diarization(
            ws, diarize_model, output_dir, device),
        '0.0.0.0', port,
    )

    actual_port = port
    if server.sockets:
        actual_port = server.sockets[0].getsockname()[1]

    # Readiness signal (DiarizationService reads this from stdout)
    print(f"DIARIZATION_READY:{actual_port}", flush=True)
    logging.info(
        f"Speaker diarization server on port {actual_port}")

    try:
        await asyncio.Future()  # run forever
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Speaker Diarization Sidecar')
    parser.add_argument(
        '--port', type=int,
        default=int(os.environ.get('HEVOLVE_DIARIZATION_PORT', 8004)))
    parser.add_argument(
        '--device',
        default=os.environ.get('HEVOLVE_DIARIZATION_DEVICE', None))
    parser.add_argument(
        '--output_dir',
        default=os.path.join(
            os.path.expanduser('~'), '.hevolve', 'audio'))
    args = parser.parse_args()

    # Auto-detect device
    if args.device is None:
        try:
            import torch
            args.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        except ImportError:
            args.device = 'cpu'

    # HuggingFace token
    hf_token = os.environ.get('HEVOLVE_HF_TOKEN', '')
    if not hf_token:
        # Fallback: try config.json in original location
        for cfg_path in [
            'config.json',
            os.path.join(os.path.expanduser('~'), '.hevolve', 'config.json'),
        ]:
            if os.path.isfile(cfg_path):
                try:
                    with open(cfg_path) as f:
                        cfg = json.load(f)
                    hf_token = cfg.get('huggingface', '')
                    if hf_token:
                        break
                except Exception:
                    pass

    if not hf_token:
        print("ERROR: HEVOLVE_HF_TOKEN env var or config.json "
              "'huggingface' key required", file=sys.stderr)
        sys.exit(1)

    # Logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )

    asyncio.run(main(args.port, args.device, args.output_dir, hf_token))
