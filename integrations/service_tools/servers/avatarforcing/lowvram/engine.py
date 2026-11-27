"""Streaming, low-VRAM Avatar Forcing engine (256x256 by default).

The denoising loop is upstream AvatarForcing.sample() split into a stateful
first_block() / next_block() pair. It calls the model's own prepare_cfg_condition,
solve_cfg and update_kv_cache, so with the same inputs and seed it yields the same
latents (tests_lowvram/test_parity.py checks this against G.sample).

Memory plan:
  * weights load on the CPU and reach the GPU already cast (the flow transformer in
    bf16, the motion autoencoder in fp16 with scales folded), so the fp32 copy never
    touches VRAM;
  * the decoder runs to 512 and the frame is area-downsampled to out_res (exact: 66.6 dB
    against upstream). decode_res=256 stops at the 256 skip image instead, which is
    cheaper but washed out (19.6 dB), because the last ToRGB carries most of the contrast;
  * frames are encoded and decoded in small micro-batches;
  * wav2vec2 can run on the CPU (audio_device='cpu') to keep its 190 MB off the GPU.
"""
import gc
import math

import numpy as np
import torch
import torch.nn.functional as F

from . import fast_ops


def _load_params(G, path):
    """Same rule as upstream inference.py: copy every named parameter found in the file."""
    try:  # memory-mapped: the file's pages back the tensors, no second copy charged to RAM
        sd = torch.load(path, map_location="cpu", weights_only=True, mmap=True)
    except Exception:
        sd = torch.load(path, map_location="cpu", weights_only=True)
    n = 0
    with torch.no_grad():
        for name, p in G.named_parameters():
            if name in sd:
                p.copy_(sd[name])
                n += 1
    del sd
    gc.collect()
    return n


def _append_kv_inplace(kv_cache, k_new, v_new):
    """CrossAttention.append_kv_to_buffer, writing into the existing cache tensors."""
    kv_cache["k"].copy_(k_new[:, :, -4:-2, :])
    kv_cache["v"].copy_(v_new[:, :, -4:-2, :])


def _cast_float_params_(module, dtype):
    """Cast float parameters only. Module.to(bf16) would also cast the complex RoPE table
    (freqs_cis) to a real dtype and drop its imaginary part."""
    with torch.no_grad():
        for m in module.modules():
            for name, p in list(m._parameters.items()):
                if p is not None and p.is_floating_point():
                    m._parameters[name] = torch.nn.Parameter(p.data.to(dtype), requires_grad=False)
    return module


class LowVRAMAvatar:
    def __init__(self, opt, device="cuda:0", out_res=256, decode_res=512, audio_device="cuda:0",
                 audio_dtype=torch.float16, mae_dtype=torch.float16, ft_dtype=torch.bfloat16,
                 dec_chunk=2, enc_chunk=2, mae_ckpt="pretrained_dir/motion_autoencoder.pth",
                 ckpt="pretrained_dir/flow_transformer.pth", audio_conv_on_gpu=True, use_graph=True,
                 rebase=True, amp=True, grid_dtype=torch.float16, ft_int8=True, w2v_int8=True,
                 graph_frames=1):
        """audio_conv_on_gpu: with audio_device='cpu', run wav2vec2's conv front-end (4M params,
        17 MB, but about 2/3 of its CPU time) on the GPU and only the transformer on the CPU.
        use_graph: replay each 10-frame block (9 denoising steps + KV update) as one CUDA graph.
        rebase: keep the RoPE position of every block constant by rotating the cached keys back
        10 positions after each block. Attention scores depend only on relative positions, so
        this is exact, and it removes upstream's 1024-position limit (about 41 s).
        """
        from models.avatarforcing.AvatarForcing import AvatarForcing
        from models.avatarforcing import flow_transformer as ftm

        fast_ops.enable()
        # In-place cross-attention cache update (upstream rebinds the dict entry to a view of
        # k_new). The values are identical; the cache keeps a fixed address, which CUDA graphs need.
        ftm.CrossAttention.append_kv_to_buffer = staticmethod(_append_kv_inplace)
        self.use_graph = use_graph
        self.rebase = rebase or use_graph
        fast_ops.GRID_SAMPLE_DTYPE = grid_dtype
        self.grid_dtype = grid_dtype
        self._fgraphs = None  # per-frame encoder / decoder graphs (built on first use)
        # frames per encoder/decoder graph replay. Every captured kernel keeps device memory
        # outside the allocator, so capturing 1 frame and replaying it is the low-VRAM choice.
        self.gf = graph_frames
        self.amp = amp  # bf16 autocast for the sampler, as upstream; False + ft_dtype=float32 is the exactness check
        self._graph = None
        self.opt = opt
        self.device = torch.device(device)
        self.audio_device = torch.device(audio_device)
        self.audio_dtype = audio_dtype if self.audio_device.type == "cuda" else torch.float32
        self.mae_dtype = mae_dtype
        self.ft_dtype = ft_dtype
        self.out_res = out_res
        self.decode_res = decode_res
        self.dec_chunk = dec_chunk
        self.enc_chunk = enc_chunk

        # 1. build and load on the CPU in fp32 (exactly the upstream modules)
        opt.rank = "cpu"
        G = AvatarForcing(opt)
        self.n_loaded = (_load_params(G, mae_ckpt), _load_params(G, ckpt))
        G.eval()

        # 2. direction basis Q (upstream recomputes the QR on every call; it is constant)
        with torch.no_grad():
            Q, _ = torch.linalg.qr(G.motion_autoencoder.dec.direction.weight.float() + 1e-8)
        self.Q = Q.to(self.device)  # (512, 20)

        # 3. motion autoencoder: fold, cast, truncate, move
        mae = G.motion_autoencoder
        fast_ops.fold_motion_autoencoder(mae, mae_dtype)
        fast_ops.truncate_decoder(mae.dec, decode_res)
        mae.to(self.device)

        # 4. flow transformer + audio projection in bf16 (params only; masks/RoPE untouched),
        #    big linears optionally weight-only int8
        if ft_int8:
            from .int8 import quantize_linears_
            self.n_int8_ft = quantize_linears_(G.flow_transformer, ft_dtype)
        _cast_float_params_(G.flow_transformer, ft_dtype).to(self.device)
        _cast_float_params_(G.audio_encoder.audio_projection, ft_dtype).to(self.device)

        # 5. wav2vec2 on the chosen device (conv front-end optionally on the GPU)
        if w2v_int8 and self.audio_device.type == "cuda":
            from .int8 import quantize_linears_
            self.n_int8_w2v = quantize_linears_(G.audio_encoder.wav2vec2.encoder, self.audio_dtype)
        G.audio_encoder.wav2vec2.to(device=self.audio_device, dtype=self.audio_dtype)
        self.audio_split = audio_conv_on_gpu and self.audio_device.type == "cpu"
        if self.audio_split:
            G.audio_encoder.wav2vec2.feature_extractor.to(device=self.device, dtype=torch.float32)
            # TF32 would round these fp32 convs to a 10-bit mantissa (cos 0.998 vs the CPU result);
            # the motion autoencoder is fp16 and the transformer bf16, so nothing else is affected
            torch.backends.cudnn.allow_tf32 = False

        # 6. point the upstream sampler at the GPU
        opt.rank = self.device.index if self.device.index is not None else 0
        G.rank = opt.rank
        G.scheduler.timesteps = G.scheduler.timesteps.to(self.device)
        G.scheduler.sigmas = G.scheduler.sigmas.to(self.device)
        self.G = G
        gc.collect()

        self.F = G.num_frames_for_clip      # 50
        self.blk = G.num_frames_per_block   # 10
        self.spf = G.samples_per_frame      # 640

    # ------------------------------------------------------------------ encoders
    @torch.inference_mode()
    def set_reference(self, img):
        """img: (1, 3, 512, 512) in [-1, 1]."""
        mae = self.G.motion_autoencoder
        x = img.to(self.device, self.mae_dtype)
        s_r, feats = mae.enc.net_app(x)
        s_r_lambda = mae.enc.fc(s_r)
        n = mae.dec.n_stages
        self.s_r = s_r.float()                                # (1, 512)
        self.r_s = s_r_lambda.float() @ self.Q.t()            # (1, 512), = Direction(s_r_lambda)
        new = [f.to(self.grid_dtype) for f in feats[:n]]
        if self._fgraphs is not None:      # captured decoder reads these addresses: update in place
            for dst, src in zip(self.feats, new):
                dst.copy_(src)
        else:
            self.feats = new
        del feats
        return self.r_s

    @torch.inference_mode()
    def encode_user_frames(self, frames):
        """frames: (N, 3, 512, 512) in [-1, 1] -> (N, 512) user motion r_d."""
        if self.use_graph and frames.shape[0] % self.gf == 0:
            g = self._frame_graphs()
            outs = []
            for i in range(0, frames.shape[0], self.gf):
                g["enc_in"].copy_(frames[i:i + self.gf], non_blocking=True)
                g["enc"].replay()
                outs.append(g["enc_out"].clone())
            return torch.cat(outs, 0)
        return self._encode_eager(frames)

    def _encode_eager(self, frames):
        mae = self.G.motion_autoencoder
        out = []
        for i in range(0, frames.shape[0], self.enc_chunk):
            x = frames[i:i + self.enc_chunk].to(self.device, self.mae_dtype, non_blocking=True)
            lam = fast_ops.encode_motion_only(mae.enc, x)
            out.append(lam.float() @ self.Q.t())
        return torch.cat(out, 0)

    @torch.inference_mode()
    def audio_features(self, wave, n_frames):
        """wave: (B, n_frames*640) normalized 16 kHz audio -> (B, n_frames, 512) in ft dtype.

        Same ops as AudioEncoder.inference: the wav2vec2 conv features are interpolated to
        n_frames, all 12 hidden states are stacked, then the projection runs.
        """
        ae = self.G.audio_encoder
        need = n_frames * self.spf
        if wave.shape[1] < need:
            wave = F.pad(wave, (0, need - wave.shape[1]), mode="replicate")
        ag = getattr(self, "_agraph", None)
        if ag is not None and tuple(wave.shape) == tuple(ag["in"].shape):
            ag["in"].copy_(wave)
            ag["g"].replay()
            return ag["out"].clone()
        if self.audio_split:
            h = self._wav2vec2_split(wave, n_frames)
            with torch.autocast("cuda", dtype=torch.bfloat16):
                return ae.audio_projection(h.to(self.device, non_blocking=True))
        a = wave.to(self.audio_device, self.audio_dtype)
        if self.audio_device.type == "cuda":
            with torch.autocast("cuda", enabled=False):  # one window at a time: halves the transient peak
                h = torch.cat([ae.get_wav2vec2_feature(a[b:b + 1], seq_len=n_frames) for b in range(a.shape[0])], 0)
        else:
            h = ae.get_wav2vec2_feature(a, seq_len=n_frames)
        h = h.to(self.device, non_blocking=True)
        with torch.autocast("cuda", dtype=torch.bfloat16):
            return ae.audio_projection(h)

    def _wav2vec2_split(self, wave, n_frames):
        """Wav2VecModel.forward + AudioEncoder.get_wav2vec2_feature, with the conv front-end on the GPU."""
        from models.wav2vec2 import linear_interpolation

        w2v = self.G.audio_encoder.wav2vec2
        exts = []
        for b in range(wave.shape[0]):  # one window at a time: halves the conv's transient GPU memory
            e = w2v.feature_extractor(wave[b:b + 1].to(self.device, torch.float32)).transpose(1, 2)
            exts.append(linear_interpolation(e, seq_len=n_frames).to(self.audio_device))
        ext = torch.cat(exts, 0)
        hidden, _ = w2v.feature_projection(ext)
        hidden = w2v._mask_hidden_states(hidden, mask_time_indices=None, attention_mask=None)
        enc = w2v.encoder(hidden, attention_mask=None, output_attentions=None, output_hidden_states=True, return_dict=True)
        a = torch.stack(enc.hidden_states[1:], dim=1).permute(0, 2, 1, 3)
        return a.reshape(a.shape[0], a.shape[1], -1)

    # ------------------------------------------------------------------ sampler
    def reset(self, nfe=10, a_cfg_scale=2.0, u_cfg_scale=1.0):
        G = self.G
        G.denoising_step_list = torch.tensor(np.linspace(self.opt.num_train_timestep, 0, nfe - 1).tolist())
        key = (nfe, a_cfg_scale, u_cfg_scale)
        if getattr(G, "kv_cache", None) is not None and getattr(self, "_cfg_key", None) == key:
            with torch.no_grad():  # zero in place: a captured graph keeps these addresses
                for self_kv, cross_kv in G.kv_cache:
                    for d in (self_kv, cross_kv):
                        d["k"].zero_()
                        d["v"].zero_()
        else:
            G.initialize_kv_cache(batch_size=1, dtype=torch.float32, device=G.rank)
            self._graph = None
        self._cfg_key = key
        self.a_cfg, self.u_cfg = a_cfg_scale, u_cfg_scale
        self.x_t = None
        self.t = 0
        self.P0 = self.F - 2  # RoPE start position of every steady block when rebasing (48)

    @torch.inference_mode()
    def first_block(self, avatar_wa, user_wa, user_rd, noise=None):
        """Frames [0, 50). avatar_wa/user_wa: (1, 50, 512); user_rd: (1, 50, 512)."""
        G = self.G
        with torch.autocast("cuda", dtype=torch.bfloat16, enabled=self.amp):
            x_t = noise if noise is not None else torch.randn(1, self.F, self.opt.dim_w, device=G.rank)
            pc, pw, pa = G.prepare_cfg_condition(avatar_wa, user_wa, user_rd, self.r_s, seq_len=self.F, context_len=0)
            steps = G.denoising_step_list
            for index, ts in enumerate(steps):
                x_t = G.solve_cfg(B=1, index=index, current_timestep=ts, x_t=x_t, precomputed_c=pc,
                                  precomputed_wr=pw, precomputed_adaLN=pa, start_pos=0, context_len=0,
                                  use_kv_cache=False, a_cfg_scale=self.a_cfg, u_cfg_scale=self.u_cfg)
                if index == len(steps) - 1:
                    G.update_kv_cache(final_latents=x_t, precomputed_c=pc, precomputed_wr=pw,
                                      precomputed_adaLN=pa, start_pos=0)
        self.x_t = x_t
        self.t = self.F
        return x_t

    @torch.inference_mode()
    def next_block(self, avatar_wa, user_wa, user_rd, noise=None):
        """Frames [t, t+10). Conditions cover [t-2, t+10): (1, 12, 512) each."""
        G = self.G
        t = self.t
        noise_t = noise if noise is not None else torch.randn(1, self.blk, self.opt.dim_w, device=G.rank)
        if self.use_graph:
            x_t = self._graph_block(avatar_wa, user_wa, user_rd, noise_t)
        else:
            start_pos = self.P0 if self.rebase else t - 2
            with torch.autocast("cuda", dtype=torch.bfloat16, enabled=self.amp):
                x_t = torch.cat([self.x_t[:, -2:], noise_t], dim=1)
                pc, pw, pa = G.prepare_cfg_condition(avatar_wa, user_wa, user_rd, self.r_s, seq_len=self.blk, context_len=2)
                steps = G.denoising_step_list
                for index, ts in enumerate(steps):
                    x_t = G.solve_cfg(B=1, index=index, current_timestep=ts, x_t=x_t, precomputed_c=pc,
                                      precomputed_wr=pw, precomputed_adaLN=pa, start_pos=start_pos, context_len=2,
                                      use_kv_cache=True, a_cfg_scale=self.a_cfg, u_cfg_scale=self.u_cfg)
                    if index == len(steps) - 1:
                        G.update_kv_cache(final_latents=x_t, precomputed_c=pc, precomputed_wr=pw,
                                          precomputed_adaLN=pa, start_pos=start_pos)
            if self.rebase:
                self._rotate_self_cache()
        self.x_t = x_t
        self.t = t + self.blk
        return x_t[:, -self.blk:]

    # ------------------------------------------------------------------ RoPE rebase + CUDA graph
    def _rotate_self_cache(self):
        """Move every cached self-attention key back `blk` positions: k * e^{-i*blk*theta}.

        The cached keys are already rotated (RoPE is applied before caching) and the score
        q_m . k_n depends only on m - n, so the next block can reuse start position P0."""
        rot = self.G.flow_transformer.blocks[0].attn.freqs_cis[self.blk].conj()
        for self_kv, _ in self.G.kv_cache:
            k = self_kv["k"]
            kc = torch.view_as_complex(k.view(*k.shape[:-1], -1, 2))
            k.copy_(torch.view_as_real(kc * rot).view_as(k))

    def _graph_setup(self):
        G, ft, dev, L = self.G, self.G.flow_transformer, self.device, self.blk + 2
        D = self.opt.dim_w
        self._s = {
            "prev2": torch.zeros(1, 2, D, device=dev), "noise": torch.zeros(1, self.blk, D, device=dev),
            "awa": torch.zeros(1, L, D, device=dev, dtype=torch.bfloat16 if self.amp else torch.float32),
            "uwa": torch.zeros(1, L, D, device=dev, dtype=torch.bfloat16 if self.amp else torch.float32),
            "urd": torch.zeros(1, L, D, device=dev), "r_s": torch.zeros(1, D, device=dev),
            "out": torch.zeros(1, L, D, device=dev),
        }
        steps = G.denoising_step_list
        with torch.autocast("cuda", dtype=torch.bfloat16, enabled=self.amp):
            # constant for every steady block: the same 9 timesteps, 2 context + 10 new frames
            self._pa = ft.precompute_timestep_adaLN(timestep_list=[t.item() for t in steps], batch_size=3,
                                                    seq_len=self.blk, context_len=2, device=dev)
            # AvatarForcing.update_kv_cache's t=0 modulation (its timestep embedding builds a CPU
            # table and copies it to the GPU, which a graph capture cannot contain)
            t_zero = torch.zeros(3, L, dtype=torch.float32, device=dev)
            t0e = ft.t_embedder(t_zero.flatten()).unflatten(0, t_zero.shape)
            self._pa_t0 = (ft.adaLN_modulation(t0e).chunk(6, dim=-1), t0e,
                           ft.final_layer.adaLN_modulation(t0e).chunk(2, dim=-1))
        # upstream solve_cfg/solve_closed for use_kv_cache=True, with the timestep tensors precomputed
        # and the boolean-mask assignment written as torch.where (same values, no host sync)
        tt, sg = G.scheduler.timesteps.to(dev, torch.double), G.scheduler.sigmas.to(dev, torch.double)
        self._coef = []
        for i, cur in enumerate(steps):
            nxt = steps[i + 1] if i < len(steps) - 1 else cur
            t_cur = torch.cat([torch.zeros((1, 2)), torch.full((1, self.blk), cur)], dim=1).to(torch.float32).to(dev)
            t_nxt = torch.cat([torch.zeros((1, 2)), torch.full((1, self.blk), nxt)], dim=1).to(torch.float32).to(dev)
            s_t = sg[(tt.view(1, 1, -1) - t_cur.double().unsqueeze(-1)).abs().argmin(-1)]
            s_tp = sg[(tt.view(1, 1, -1) - t_nxt.double().unsqueeze(-1)).abs().argmin(-1)]
            z_t, z_tp = t_cur == 0, t_nxt == 0
            s_t = torch.where(z_t, torch.zeros_like(s_t), s_t)
            s_tp = torch.where(z_tp, torch.zeros_like(s_tp), s_tp)
            ratio = torch.where(s_t > 0, s_tp / torch.clamp(s_t, min=1e-8), torch.zeros_like(s_tp))
            coef_b = (1.0 - s_tp) - ratio * (1.0 - s_t)
            self._coef.append((s_t.unsqueeze(-1), ratio.unsqueeze(-1), coef_b.unsqueeze(-1), (z_t & z_tp).unsqueeze(-1)))

    def _body(self):
        G, ft, s = self.G, self.G.flow_transformer, self._s
        x_t = torch.cat([s["prev2"], s["noise"]], dim=1)
        with torch.autocast("cuda", dtype=torch.bfloat16, enabled=self.amp, cache_enabled=False):
            wa, wau, mu, wr = s["awa"], s["uwa"], s["urd"], s["r_s"]
            z, zu, zm = torch.zeros_like(wa), torch.zeros_like(wau), torch.zeros_like(mu)
            pc = ft.compute_condition(wa=torch.cat([z, wa, z]), wa_user=torch.cat([zu, zu, wau]),
                                      motion_user=torch.cat([zm, zm, mu]), train=False)
            pw = ft.compute_wr_embedded(wr=torch.cat([wr, wr, wr]), seq_len=self.blk + 2, train=False)
            for i, (s_t, coef_a, coef_b, hist) in enumerate(self._coef):
                v = ft.forward_with_precomputed(x=x_t.repeat(3, 1, 1), precomputed_c=pc, precomputed_adaLN=self._pa[i],
                                                precomputed_wr=pw, start_pos=self.P0, kv_cache_list=G.kv_cache,
                                                update_kv_cache=False)
                v_un, v_au, v_us = v.chunk(3, dim=0)
                v_t = v_un + self.a_cfg * (v_au - v_un) + self.u_cfg * (v_us - v_un)
                x, vv = x_t.double(), v_t.double()
                x0 = x - s_t * vv
                x_t = torch.where(hist, x, coef_a * x + coef_b * x0).to(x_t.dtype)
            ft.forward_with_precomputed(x=torch.cat([x_t, x_t, x_t], dim=0), precomputed_c=pc,
                                        precomputed_adaLN=self._pa_t0, precomputed_wr=pw, start_pos=self.P0,
                                        kv_cache_list=G.kv_cache, update_kv_cache=True)
        self._rotate_self_cache()
        s["out"].copy_(x_t)

    def _pool(self):
        if getattr(self, "_mempool", None) is None:
            self._mempool = torch.cuda.graph_pool_handle()  # one pool for all graphs: replayed in turn, never together
        return self._mempool

    def _capture(self, body):
        """Capture `body` into the shared pool. The caller has already run it eagerly once (kernel
        loading and selection), and before any graph holds pool memory, so warm-up peaks and
        graph pools never coexist under the allocator cap."""
        torch.cuda.synchronize()
        torch.cuda.empty_cache()
        g = torch.cuda.CUDAGraph()
        with torch.cuda.graph(g, pool=self._pool()):
            body()
        torch.cuda.synchronize()
        torch.cuda.empty_cache()
        return g

    @torch.inference_mode()
    def prepare_graphs(self, audio_frames=50):
        """Capture all three graphs at start-up (after set_reference). The sampler graph needs a
        block of state, so it runs one dummy block, then resets."""
        if not self.use_graph:
            return
        torch.cuda.empty_cache()
        self.reset(*getattr(self, "_cfg_key", (10, 2.0, 1.0)))
        z = torch.zeros(1, self.F, self.opt.dim_w, device=self.device)
        zb = z.to(torch.bfloat16 if self.amp else torch.float32)
        self.first_block(zb, zb, z)
        self.next_block(zb[:, :self.blk + 2], zb[:, :self.blk + 2], z[:, :self.blk + 2])
        self.reset(*self._cfg_key)
        torch.cuda.synchronize()
        torch.cuda.empty_cache()
        if audio_frames:
            self._audio_graph(audio_frames)
        self._frame_graphs()
        torch.cuda.synchronize()
        torch.cuda.empty_cache()

    def _audio_graph(self, n_frames, batch=2):
        """wav2vec2 (+ projection) for one fixed window shape as a CUDA graph (GPU audio only)."""
        if self.audio_device.type != "cuda" or self.audio_split:
            return None
        ag = {"in": torch.zeros(batch, n_frames * self.spf, device=self.device, dtype=self.audio_dtype)}

        ag["out"] = self._audio_eager_gpu(ag["in"], n_frames)  # eager warm-up; also allocates the static output
        ag["g"] = self._capture(lambda: ag["out"].copy_(self._audio_eager_gpu(ag["in"], n_frames)))
        self._agraph = ag
        return ag

    def _audio_eager_gpu(self, a, n_frames):
        ae = self.G.audio_encoder
        with torch.autocast("cuda", enabled=False):
            h = torch.cat([ae.get_wav2vec2_feature(a[b:b + 1], seq_len=n_frames) for b in range(a.shape[0])], 0)
        with torch.autocast("cuda", dtype=torch.bfloat16):
            return ae.audio_projection(h)

    def _frame_graphs(self):
        """Encoder (10 user frames) and decoder (10 latents) as CUDA graphs: one replay per block
        instead of about 1,400 small kernel launches each."""
        if self._fgraphs is not None:
            return self._fgraphs
        dev, n = self.device, self.gf
        g = {"enc_in": torch.zeros(n, 3, 512, 512, device=dev, dtype=self.mae_dtype),
             "dec_in": torch.zeros(n, self.s_r.shape[-1], device=dev)}
        g["enc_out"] = torch.zeros(n, self.Q.shape[0], device=dev)
        g["dec_out"] = torch.zeros(n, 3, self.out_res, self.out_res, device=dev, dtype=self.mae_dtype)

        def enc_body():
            g["enc_out"].copy_(self._encode_eager(g["enc_in"]))

        def dec_body():
            g["dec_out"].copy_(self._decode_eager(g["dec_in"]))

        enc_body()  # eager warm-ups first, while no graph holds pool memory
        dec_body()
        g["enc"] = self._capture(enc_body)
        g["dec"] = self._capture(dec_body)
        self._fgraphs = g
        return g

    def _graph_block(self, awa, uwa, urd, noise):
        first = self._graph is None
        if first:
            self._graph_setup()
        s = self._s
        s["prev2"].copy_(self.x_t[:, -2:])
        s["noise"].copy_(noise)
        s["awa"].copy_(awa)
        s["uwa"].copy_(uwa)
        s["urd"].copy_(urd)
        s["r_s"].copy_(self.r_s)
        if first:
            G = self.G
            saved = [tuple(d[n].clone() for d in kv for n in ("k", "v")) for kv in G.kv_cache]
            self._body()  # eager warm-up (mutates the caches; restored below)
            g = self._capture(self._body)
            for kv, vals in zip(G.kv_cache, saved):  # warm-up ran the block for real: undo it
                kv[0]["k"].copy_(vals[0]); kv[0]["v"].copy_(vals[1])
                kv[1]["k"].copy_(vals[2]); kv[1]["v"].copy_(vals[3])
            self._graph = g
        self._graph.replay()
        return s["out"].clone()

    # ------------------------------------------------------------------ decoder
    @torch.inference_mode()
    def decode(self, r_d):
        """r_d: (1, T, 512) motion latents -> (T, 3, R, R) float in [-1, 1] (fp16 on GPU)."""
        wa = (self.s_r.unsqueeze(1) + r_d.float()).reshape(-1, self.s_r.shape[-1])
        if self.use_graph and wa.shape[0] % self.gf == 0:
            g = self._frame_graphs()
            outs = []
            for i in range(0, wa.shape[0], self.gf):
                g["dec_in"].copy_(wa[i:i + self.gf])
                g["dec"].replay()
                outs.append(g["dec_out"].clone())
            return torch.cat(outs, 0)
        return self._decode_eager(wa)

    def _decode_eager(self, wa):
        dec = self.G.motion_autoencoder.dec
        outs = []
        for i in range(0, wa.shape[0], self.dec_chunk):
            w = wa[i:i + self.dec_chunk].to(self.mae_dtype)
            img = fast_ops.synthesize(dec, w, self.feats)
            if img.shape[-1] != self.out_res:
                img = F.interpolate(img, size=self.out_res, mode="area")
            outs.append(img)
        return torch.cat(outs, 0)

    @torch.inference_mode()
    def decode_to_cpu(self, r_d):
        """r_d: (1, T, 512) -> (T, R, R, 3) uint8 numpy. Each finished frame is copied straight into
        a reused pinned host buffer, so the GPU never holds the block's frames (39 MB for the
        50-frame first block at 512)."""
        wa = (self.s_r.unsqueeze(1) + r_d.float()).reshape(-1, self.s_r.shape[-1])
        n, R = wa.shape[0], self.out_res
        pins = self.__dict__.setdefault("_pinned", {})
        buf = pins.get((n, R))
        if buf is None:
            buf = pins[(n, R)] = torch.empty(n, R, R, 3, dtype=torch.uint8, pin_memory=True)
        for i in range(0, n, self.gf):
            if self.use_graph and wa[i:i + self.gf].shape[0] == self.gf:
                g = self._frame_graphs()
                g["dec_in"].copy_(wa[i:i + self.gf])
                g["dec"].replay()
                img = g["dec_out"]
            else:
                img = self._decode_eager(wa[i:i + self.gf])
            buf[i:i + img.shape[0]].copy_(self.to_uint8(img), non_blocking=True)
        torch.cuda.current_stream().synchronize()
        return buf.numpy().copy()

    @staticmethod
    def to_uint8(img):
        return ((img.float().clamp(-1, 1) + 1) * 127.5).round().to(torch.uint8).permute(0, 2, 3, 1)


class RunningNorm:
    """Streaming stand-in for Wav2Vec2FeatureExtractor(do_normalize=True).

    Upstream normalizes each whole clip to zero mean and unit variance. A live stream has
    no whole clip, so this keeps running sums over everything heard so far. The std has
    a floor, so a silent start is not amplified into noise.
    """

    def __init__(self, std_floor=0.02):
        self.n = 0
        self.s = 0.0
        self.ss = 0.0
        self.floor = std_floor

    def update(self, chunk):
        c = np.asarray(chunk, dtype=np.float64)
        self.n += c.size
        self.s += c.sum()
        self.ss += (c * c).sum()

    def __call__(self, x):
        if self.n == 0:
            return np.asarray(x, dtype=np.float32)
        m = self.s / self.n
        v = max(self.ss / self.n - m * m, 0.0)
        std = max(math.sqrt(v + 1e-7), self.floor)
        return ((np.asarray(x, dtype=np.float64) - m) / std).astype(np.float32)
