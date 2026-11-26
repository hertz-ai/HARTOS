"""Real-time Avatar Forcing at 256x256 within a 1 GB VRAM budget.

Streams the inputs block by block, the way a live call would deliver them. To output
frames [t, t+10) it uses only audio up to frame t+10+lookahead and user video up to
frame t+10. It times every block against the 400 ms of video that block covers, and it
reports the process's real dedicated GPU memory (Windows GPU counters, CUDA context
included), not only the allocator's view.

    python realtime_lowvram.py --ref data/rumi.jpg --avatar_audio data/avatar.wav \
        --user_video data/user.mp4 --out results/rumi_rt256.mp4
"""
import argparse
import json
import os
import subprocess
import sys
import time

import av
import cv2
import numpy as np
import torch
from omegaconf import OmegaConf

# HERE: the engine (lowvram/, these scripts). ROOT: the AvatarForcing checkout it runs on (models/,
# configs/, pretrained_dir/, data/) -- the same folder in a dev checkout; HARTOS ships the engine and
# names the model folder (AVATARFORCING_MODEL_DIR). The engine is first on the path.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("AVATARFORCING_MODEL_DIR") or HERE
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.chdir(ROOT)

from lowvram.engine import LowVRAMAvatar  # noqa: E402
from lowvram.session import stream_blocks  # noqa: E402

FPS, SR, SPF = 25, 16000, 640


# ---------------------------------------------------------------------------- GPU memory
def gpu_process_mem_mb(pid=None):
    """Dedicated and shared GPU memory of this process from the Windows GPU counters (MiB).

    On WDDM, nvidia-smi shows N/A per process, so this is the reliable number. It
    includes the CUDA context, cuBLAS/cuDNN workspaces and the caching allocator.
    """
    pid = pid or os.getpid()
    ps = ("$d=(Get-Counter '\\GPU Process Memory(pid_{0}_*)\\Dedicated Usage' -EA SilentlyContinue).CounterSamples;"
          "$s=(Get-Counter '\\GPU Process Memory(pid_{0}_*)\\Shared Usage' -EA SilentlyContinue).CounterSamples;"
          "'{{0}} {{1}}' -f ($d|Measure-Object CookedValue -Sum).Sum, ($s|Measure-Object CookedValue -Sum).Sum").format(pid)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60).stdout.split()
        return float(out[0]) / 2**20, float(out[1]) / 2**20
    except Exception:
        return float("nan"), float("nan")


class GpuSampler:
    """nvidia-smi every 250 ms during the run: SM clock, utilization and temperature (the laptop GPU
    is shared and thermally managed, so the clock is part of any timing result)."""

    def __init__(self):
        try:
            self.p = subprocess.Popen(["nvidia-smi", "--query-gpu=clocks.sm,utilization.gpu,temperature.gpu",
                                       "--format=csv,noheader,nounits", "-lms", "250"],
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except Exception:
            self.p = None

    def stop(self):
        if self.p is None:
            return None
        self.p.terminate()
        out = self.p.communicate(timeout=10)[0]
        rows = [[float(v) for v in ln.split(",")] for ln in out.strip().splitlines() if ln.count(",") == 2]
        if not rows:
            return None
        a = np.array(rows)
        return {"sm_clock_mhz_p10_p50_p90": [round(float(np.percentile(a[:, 0], q))) for q in (10, 50, 90)],
                "util_pct_p50": round(float(np.median(a[:, 1]))), "temp_c_max": round(float(a[:, 2].max())), "samples": len(a)}


# ---------------------------------------------------------------------------- inputs
def load_audio(path):
    """Mono float32 at 16 kHz from any file PyAV can read (wav, mp4, ...)."""
    out = []
    with av.open(path) as c:
        st = next((s for s in c.streams if s.type == "audio"), None)
        if st is None:
            return None
        rs = av.AudioResampler(format="flt", layout="mono", rate=SR)
        for frame in c.decode(st):
            for f in rs.resample(frame):
                out.append(f.to_ndarray().reshape(-1))
        for f in rs.resample(None):
            out.append(f.to_ndarray().reshape(-1))
    return np.concatenate(out).astype(np.float32) if out else None


def load_video_25fps(path):
    """RGB frames resampled to 25 fps by timestamp (nearest earlier frame)."""
    frames, times = [], []
    with av.open(path) as c:
        st = c.streams.video[0]
        for fr in c.decode(st):
            frames.append(fr.to_ndarray(format="rgb24"))
            times.append(float(fr.time) if fr.time is not None else len(times) / FPS)
    times = np.asarray(times) - times[0]
    n = int(np.floor(times[-1] * FPS)) + 1
    idx = np.searchsorted(times, np.arange(n) / FPS + 1e-6, side="right") - 1
    return [frames[max(i, 0)] for i in idx]


class FaceCropper:
    """Upstream crop math (inference.py / preprocess_user_video.py) with the S3FD detector on the CPU."""

    def __init__(self):
        from face_alignment.detection.sfd.sfd_detector import SFDDetector
        self.det = SFDDetector(device="cpu")

    def box(self, img):
        mult = 360.0 / img.shape[0]
        small = cv2.resize(img, dsize=(0, 0), fx=mult, fy=mult,
                           interpolation=cv2.INTER_AREA if mult < 1.0 else cv2.INTER_CUBIC)
        bb = self.det.detect_from_image(small)
        bb = [(int(x1 / mult), int(y1 / mult), int(x2 / mult), int(y2 / mult), s) for (x1, y1, x2, y2, s) in bb if s > 0.95]
        if not bb:
            return None
        b = bb[0]
        return (b[3] - b[1]) / 2, (b[2] - b[0]) / 2, (b[1] + b[3]) / 2, (b[0] + b[2]) / 2, mult  # bsy, bsx, my, mx

    @staticmethod
    def crop(img, bsy, bsx, my, mx, mult, pad_ratio=1.0, size=512):
        h, w = img.shape[:2]
        bs = int(max(bsy, bsx) * (1 + pad_ratio))
        x1, y1, x2, y2 = mx - bs, my - bs, mx + bs, my + bs
        x1, y1, x2, y2 = max(x1, 0), max(y1, 0), min(x2, w), min(y2, h)
        bsx, bsy = x2 - x1, y2 - y1
        mx, my = int(x1 + bsx // 2), int(y1 + bsy // 2)
        bs = int(min(bsx, bsy) // 2)
        face = img[my - bs: my + bs, mx - bs: mx + bs]
        return cv2.resize(face, dsize=(size, size), interpolation=cv2.INTER_AREA if mult < 1.0 else cv2.INTER_CUBIC)


def to_tensor(rgb):
    """Albumentations Normalize(0.5, 0.5) + ToTensorV2 on a 512x512 RGB uint8 image."""
    return torch.from_numpy(rgb).permute(2, 0, 1).float().div_(127.5).sub_(1.0)


def write_video(path, frames, audio, side=None):
    """frames: (T, R, R, 3) uint8; audio: 16 kHz mono float32 (trimmed to the video); side: optional (T, R, R, 3)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if side is not None:
        frames = np.concatenate([side, frames], axis=2)
    T, H, W, _ = frames.shape
    with av.open(path, "w") as c:
        for codec in ("libx264", "h264", "mpeg4"):
            try:
                vs = c.add_stream(codec, rate=FPS)
                break
            except Exception:
                continue
        vs.width, vs.height, vs.pix_fmt = W, H, "yuv420p"
        if codec == "mpeg4":
            vs.bit_rate = 4_000_000
        aus = c.add_stream("aac", rate=SR, layout="mono") if audio is not None else None
        for f in frames:
            for p in vs.encode(av.VideoFrame.from_ndarray(f, format="rgb24")):
                c.mux(p)
        for p in vs.encode():
            c.mux(p)
        if aus is not None:
            a = audio[: int(T / FPS * SR)].astype(np.float32)
            af = av.AudioFrame.from_ndarray(a.reshape(1, -1), format="flt", layout="mono")
            af.sample_rate = SR
            for p in aus.encode(af):
                c.mux(p)
            for p in aus.encode(None):
                c.mux(p)
    return path, codec


# ---------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="data/rumi.jpg")
    ap.add_argument("--avatar_audio", default="data/avatar.wav")
    ap.add_argument("--user_video", default="data/user.mp4", help="user video; its audio track is the user audio unless --user_audio is given")
    ap.add_argument("--user_audio", default=None)
    ap.add_argument("--out", default="results/rt256.mp4")
    ap.add_argument("--res", type=int, default=256, choices=[64, 128, 256, 512], help="output resolution")
    ap.add_argument("--decode_res", type=int, default=512, choices=[256, 512],
                    help="512 = exact (decode then area-downsample); 256 = cheaper, washed-out skip image")
    ap.add_argument("--nfe", type=int, default=10)
    ap.add_argument("--a_cfg_scale", type=float, default=2.0)
    ap.add_argument("--u_cfg_scale", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=25)
    ap.add_argument("--audio_device", default="cuda:0", help="cuda:0: wav2vec2 fp16 on the GPU (190 MB, ~10 ms, immune to CPU load); "
                    "cpu: conv front-end on the GPU, transformer on a CPU worker thread")
    ap.add_argument("--audio_window", type=int, default=50, help="frames of audio context per wav2vec2 window (50 = 2 s)")
    ap.add_argument("--lookahead", type=int, default=0, help="extra audio frames to wait for (each adds 40 ms latency)")
    ap.add_argument("--dec_chunk", type=int, default=1, help="frames per decoder pass inside the graph (1 = least memory)")
    ap.add_argument("--enc_chunk", type=int, default=1)
    ap.add_argument("--vram_mb", type=int, default=1024, help="total budget incl. CUDA context")
    ap.add_argument("--ctx_reserve_mb", type=int, default=260, help="part of the budget kept for the CUDA context; the allocator gets the rest")
    ap.add_argument("--max_seconds", type=float, default=30.0)
    ap.add_argument("--loop_to_s", type=float, default=0, help="repeat the inputs to this length (long-stream test)")
    ap.add_argument("--pace", action="store_true", help="wait for each input block in wall-clock time, like a live stream")
    ap.add_argument("--side_by_side", action="store_true")
    ap.add_argument("--no_guard", action="store_true",
                    help="disable the deadline guard (when a block finished >400 ms after its inputs, the next block "
                         "encodes every other user frame and interpolates the rest, ~35 ms less GPU work)")
    ap.add_argument("--cudnn_benchmark", action="store_true")
    ap.add_argument("--report", default=None)
    ap.add_argument("--no_ft_int8", action="store_true", help="keep the flow transformer in bf16 (+138 MB; group-128 int8 error 1.7%% vs bf16 1.3%%)")
    ap.add_argument("--no_w2v_int8", action="store_true", help="keep wav2vec2 in fp16 (+90 MB)")
    args = ap.parse_args()

    torch.backends.cudnn.benchmark = args.cudnn_benchmark
    dev = torch.device("cuda:0")
    torch.cuda.init()
    torch.zeros(1, device=dev)
    total = torch.cuda.get_device_properties(dev).total_memory
    cap_mb = args.vram_mb - args.ctx_reserve_mb
    torch.cuda.set_per_process_memory_fraction(cap_mb * 2**20 / total, dev)
    mem = {"ctx_only": gpu_process_mem_mb()}

    # ---- inputs (in a live call these arrive from the mic/camera)
    t0 = time.perf_counter()
    cropper = FaceCropper()
    ref_img = cv2.cvtColor(cv2.imread(args.ref), cv2.COLOR_BGR2RGB)
    rb = cropper.box(ref_img)
    ref = to_tensor(cropper.crop(ref_img, *rb)).unsqueeze(0)

    avatar_audio = load_audio(args.avatar_audio)
    user_frames_raw = load_video_25fps(args.user_video) if args.user_video else []
    user_audio = load_audio(args.user_audio or args.user_video) if (args.user_audio or args.user_video) else None
    if user_audio is None:
        user_audio = np.zeros_like(avatar_audio)
    if args.loop_to_s:
        reps = int(np.ceil(args.loop_to_s * SR / max(len(avatar_audio), 1)))
        avatar_audio = np.tile(avatar_audio, reps)[: int(args.loop_to_s * SR)]
        user_audio = np.tile(user_audio, int(np.ceil(args.loop_to_s * SR / len(user_audio))))[: int(args.loop_to_s * SR)]
        if user_frames_raw:
            user_frames_raw = (user_frames_raw * int(np.ceil(args.loop_to_s * FPS / len(user_frames_raw))))[: int(args.loop_to_s * FPS)]
    T = int(np.ceil(max(len(avatar_audio), len(user_audio)) * FPS / SR))
    T = min(T, int(args.max_seconds * FPS))
    # user face box: averaged over the first second (upstream averages the whole clip offline)
    boxes = [b for b in (cropper.box(f) for f in user_frames_raw[:FPS]) if b is not None]
    ub = tuple(float(np.mean([b[i] for b in boxes])) for i in range(4)) + (boxes[0][4],) if boxes else None
    prep_s = time.perf_counter() - t0

    # ---- model
    t0 = time.perf_counter()
    cfg = OmegaConf.load("configs/inference.yaml")
    cfg.rank, cfg.ngpus = 0, 1
    eng = LowVRAMAvatar(cfg, device="cuda:0", out_res=args.res, decode_res=args.decode_res, audio_device=args.audio_device,
                        dec_chunk=args.dec_chunk, enc_chunk=args.enc_chunk,
                        ft_int8=not args.no_ft_int8, w2v_int8=not args.no_w2v_int8, graph_frames=1)
    torch.cuda.empty_cache()  # drop blocks cached while loading
    load_s = time.perf_counter() - t0
    mem["after_load"] = gpu_process_mem_mb()

    def user_frame_tensor(i):
        if not user_frames_raw:
            return torch.zeros(3, 512, 512)
        f = user_frames_raw[min(i, len(user_frames_raw) - 1)]
        return to_tensor(FaceCropper.crop(f, *ub)) if ub else to_tensor(cv2.resize(f, (512, 512), interpolation=cv2.INTER_AREA))

    # ---- start-up: kernel selection and CUDA-graph capture happen once, before the stream
    eng.set_reference(ref)
    eng.reset(nfe=args.nfe, a_cfg_scale=args.a_cfg_scale, u_cfg_scale=args.u_cfg_scale)
    eng.audio_features(torch.zeros(2, args.audio_window * SPF), args.audio_window)
    eng.prepare_graphs(audio_frames=args.audio_window)
    torch.cuda.synchronize()
    mem["after_graphs"] = gpu_process_mem_mb()

    # ---- stream (lowvram/session.py: the same block loop the live stream server uses)
    eng.set_reference(ref)
    torch.cuda.reset_peak_memory_stats(dev)
    smi = GpuSampler()
    out_frames, blocks, prep = [], [], []
    for blk in stream_blocks(eng, avatar_audio, user_audio, user_frame_tensor if user_frames_raw else None, T,
                             pace=args.pace, lookahead=args.lookahead, audio_window=args.audio_window,
                             guard=not args.no_guard, nfe=args.nfe, a_cfg_scale=args.a_cfg_scale,
                             u_cfg_scale=args.u_cfg_scale, seed=args.seed, first_chunks=None):
        out_frames.append(blk.pop("frames"))
        prep += blk.pop("prep_ms")
        blocks.append(blk)
        print(f"[block] frames {blk['t']}-{blk['t_end']} gpu {blk['gpu_ms']:.0f} ms latency {blk['latency_ms']:.0f} ms",
              file=sys.stderr, flush=True)
    gpu_tel = smi.stop()

    torch.cuda.synchronize()
    mem["end"] = gpu_process_mem_mb()
    frames = np.concatenate(out_frames)[:T]

    side = None
    if args.side_by_side and user_frames_raw:
        side = np.stack([cv2.resize(user_frame_tensor(i).permute(1, 2, 0).add(1).mul(127.5).clamp(0, 255).byte().numpy(),
                                    (args.res, args.res), interpolation=cv2.INTER_AREA) for i in range(T)])
    path, codec = write_video(args.out, frames, avatar_audio, side)

    steady = [b for b in blocks if b["t"] > 0]
    iv = np.array([b["interval_ms"] for b in steady[1:]] or [0.0])
    gm = np.array([b["gpu_ms"] for b in steady] or [0.0])
    am = np.array(prep[eng.F:] or [0.0])
    cp = np.array([b["after_last_input_ms"] for b in steady] or [0.0])
    lat_ms = np.array([b["latency_ms"] for b in steady] or [0.0])
    q = max(1, len(lat_ms) // 4)
    pct = lambda x: {"p50": round(float(np.median(x)), 1), "p95": round(float(np.percentile(x, 95)), 1), "max": round(float(x.max()), 1)}
    rep = {
        "res": args.res, "decode_res": args.decode_res, "frames": int(frames.shape[0]), "seconds": frames.shape[0] / FPS,
        "audio_device": args.audio_device, "ft_int8": not args.no_ft_int8, "w2v_int8": not args.no_w2v_int8, "nfe": args.nfe, "lookahead_frames": args.lookahead, "paced": args.pace,
        "first_block_ms(50 frames = 2000 ms of video)": round(blocks[0]["gpu_ms"], 1),
        "block_interval_ms(10 frames, budget 400)": pct(iv),
        "gpu_ms_per_block(encode+audio+sample+decode)": pct(gm), "critical_path_ms(after the block's last input)": pct(cp),
        "cpu_prep_ms_per_frame(camera thread)": pct(am),
        "throughput_fps": round(10 / (np.median(iv) / 1e3), 1) if iv.any() else None,
        "blocks_over_400ms": int((iv > 400).sum()), "steady_blocks": int(len(iv)),
        "guarded_blocks(half-rate user encode)": int(sum(b["guard"] for b in blocks)),
        "latency_ms(frame t shown vs t spoken)": pct(lat_ms) if args.pace else None,
        "latency_drift_ms(last quarter - first quarter)": round(float(lat_ms[-q:].mean() - lat_ms[:q].mean()), 1) if args.pace else None,
        "gpu_during_run": gpu_tel,
        "torch_peak_allocated_mb": round(torch.cuda.max_memory_allocated(dev) / 2**20, 1),
        "torch_peak_reserved_mb": round(torch.cuda.max_memory_reserved(dev) / 2**20, 1),
        "allocator_cap_mb": cap_mb,
        "process_gpu_mb(dedicated, shared)": {k: [round(v[0], 1), round(v[1], 1)] for k, v in mem.items()},
        "prep_s": round(prep_s, 2), "load_s": round(load_s, 2),
        "out": os.path.abspath(path), "codec": codec,
    }
    print(json.dumps(rep, indent=1))
    if args.report:
        with open(args.report, "w") as f:
            json.dump({"summary": rep, "blocks": blocks}, f, indent=1)


if __name__ == "__main__":
    main()
