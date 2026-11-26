"""One streaming session: the block loop shared by realtime_lowvram.py (benchmark, mp4) and
stream_server.py (live fragmented-MP4 stream into a browser <video>).

stream_blocks() yields every finished 10-frame block (the first block is 50 frames) as soon as
it exists. To output frames [t, t+10) it uses only audio up to frame t+10+lookahead and user
video up to frame t+10. With pace=True those inputs appear in wall-clock time, the way a live
call delivers them.
"""
import queue
import threading
import time

import numpy as np
import torch

from .engine import RunningNorm

FPS, SPF = 25, 640


def stream_blocks(eng, avatar_audio, user_audio, user_frame, T, *, pace=False, lookahead=0,
                  audio_window=50, guard=True, nfe=10, a_cfg_scale=2.0, u_cfg_scale=1.0, seed=25,
                  first_chunks=(1, 4, 5, 10, 10, 10, 10)):
    """eng: a LowVRAMAvatar with set_reference() and prepare_graphs() already done.
    avatar_audio / user_audio: 16 kHz mono float32 arrays. user_frame(i) -> (3, 512, 512) tensor in
    [-1, 1] for user frame i, or None for no user video (the model's own null condition).
    Yields dicts: t, t_end, frames (n, R, R, 3) uint8, plus timings."""
    dev = eng.device
    sched = [(0, eng.F)] + [(t, t + eng.blk) for t in range(eng.F, T, eng.blk)]
    n_total = sched[-1][1] + lookahead + eng.blk

    def pad(a):
        return np.pad(a, (0, max(0, n_total * SPF - len(a))), mode="edge")[: n_total * SPF]

    A, U = pad(avatar_audio), pad(user_audio)
    nA, nU = RunningNorm(), RunningNorm()
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    eng.reset(nfe=nfe, a_cfg_scale=a_cfg_scale, u_cfg_scale=u_cfg_scale)

    frame_q = queue.Queue(maxsize=2 * eng.F)
    stop = threading.Event()
    start = time.perf_counter()

    def camera():
        """Stand-in for the webcam: frame i exists from (i+1)/25 s when paced; it is prepared on
        this CPU thread as it arrives, like a capture callback. Frames up to the last block's end
        (past the clip, the last frame repeats)."""
        for i in range(sched[-1][1]):
            if stop.is_set():
                return
            if pace:
                wait = start + (i + 1) / FPS - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)
            p0 = time.perf_counter()
            fr = user_frame(i) if user_frame is not None else None
            frame_q.put((fr, (time.perf_counter() - p0) * 1e3))

    threading.Thread(target=camera, daemon=True).start()
    user_rd = torch.zeros(0, eng.Q.shape[0], device=dev)
    heard, prev_done, last_lag = 0, None, 0.0
    try:
        for k, (t, t_end) in enumerate(sched):
            # 1. user frames, encoded one by one as they arrive (in the GPU's idle time). Deadline
            #    guard: after a block that came out more than a block late, encode every other frame
            #    and interpolate the motion code for the rest.
            f0 = 0 if k == 0 else sched[k - 1][1]
            guarded = guard and k > 0 and last_lag > eng.blk / FPS * 1e3
            enc_ms, new, prep_ms = 0.0, [], []
            for j in range(f0, t_end):
                fr, p_ms = frame_q.get()
                prep_ms.append(p_ms)
                e0 = time.perf_counter()
                if fr is None:
                    new.append(torch.zeros(eng.Q.shape[0], device=dev))  # no user: null motion condition
                elif guarded and (j - f0) % 2 == 1 and j < t_end - 1:
                    new.append(None)
                else:
                    new.append(eng.encode_user_frames(fr[None])[0])
                enc_ms += (time.perf_counter() - e0) * 1e3
            for j, r in enumerate(new):
                if r is None:
                    new[j] = 0.5 * (new[j - 1] + new[j + 1])
            user_rd = torch.cat([user_rd, torch.stack(new)], 0)  # frames [t-2, t_end) (or [0, 50))
            # 2. this block's audio exists only once frame `need` has been spoken
            need = t_end + lookahead
            if pace:
                wait = start + need / FPS - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)
            g0 = time.perf_counter()  # every input of the block exists from here: critical path
            nA.update(A[heard * SPF: need * SPF])
            nU.update(U[heard * SPF: need * SPF])
            heard = need
            lo = 0 if t == 0 else t - 2
            ws = min(max(0, need - audio_window), lo)
            wave = np.stack([nA(A[ws * SPF: need * SPF]), nU(U[ws * SPF: need * SPF])])
            f = eng.audio_features(torch.from_numpy(wave), need - ws)
            awa, uwa = f[0:1, lo - ws: t_end - ws], f[1:2, lo - ws: t_end - ws]
            lat = eng.first_block(awa, uwa, user_rd[None]) if t == 0 else eng.next_block(awa, uwa, user_rd[None])
            user_rd = user_rd[-2:]  # the next block's 2 context frames; history stays bounded
            if t == 0 and first_chunks:
                # Time to first frame: the opening block's 50 latents exist at once, but decode and hand
                # them out progressively (1 frame, then 4, 5, 10, ...) so the first frame leaves at once.
                s0 = 0
                for n in first_chunks:
                    if s0 >= t_end:
                        break
                    s1 = min(t_end, s0 + n)
                    part = eng.decode_to_cpu(lat[:, s0:s1])
                    now = time.perf_counter()
                    yield {"t": s0, "t_end": s1, "frames": part, "guard": False, "gpu_ms": (now - g0) * 1e3 + enc_ms,
                           "after_last_input_ms": (now - g0) * 1e3, "encode_ms": enc_ms, "prep_ms": prep_ms if s0 == 0 else [],
                           "interval_ms": None, "budget_ms": (s1 - s0) / FPS * 1e3,
                           "latency_ms": (now - (start + s0 / FPS)) * 1e3, "wall_s": now - start, "partial": True}
                    s0 = s1
                done = time.perf_counter()
                last_lag = (done - (start + need / FPS)) * 1e3 if pace else (done - g0) * 1e3 + enc_ms
                prev_done = done
                continue
            imgs = eng.decode_to_cpu(lat)
            done = time.perf_counter()
            last_lag = (done - (start + need / FPS)) * 1e3 if pace else (done - g0) * 1e3 + enc_ms
            yield {"t": t, "t_end": t_end, "frames": imgs, "guard": guarded,
                   "gpu_ms": (done - g0) * 1e3 + enc_ms, "after_last_input_ms": (done - g0) * 1e3,
                   "encode_ms": enc_ms, "prep_ms": prep_ms,
                   "interval_ms": (done - prev_done) * 1e3 if prev_done else None,
                   "budget_ms": imgs.shape[0] / FPS * 1e3,
                   "latency_ms": (done - (start + t / FPS)) * 1e3, "wall_s": done - start}
            prev_done = done
    finally:
        stop.set()
        try:  # a camera thread blocked on a full queue (the caller stopped early) wakes and exits
            while True:
                frame_q.get_nowait()
        except queue.Empty:
            pass
