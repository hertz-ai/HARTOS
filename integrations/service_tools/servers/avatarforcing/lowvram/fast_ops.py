"""Exact, cheaper drop-ins for the motion autoencoder (LIA/FLOAT style), plus 256x256 truncation.

Every replacement computes the same function as the upstream code; only the kernel choice
and memory layout change. tests_lowvram/test_parity.py checks each one against the
original forward in fp32.

- ModulatedConv2d: upstream builds a per-sample weight and runs a grouped conv
  (groups=batch). Here the style scales the input channels, one shared-weight conv
  runs for the whole batch, and demodulation scales the output channels:
  conv(x * s, w) * d == conv(x, w * s * d). It is linear per channel, so it commutes
  with the depthwise blur.
- Blur: upstream's single-channel conv on (B*C, 1, H, W) is kept (fastest measured);
  only the flipped kernel is cached.
- ToFlow: upstream rebuilds the identity grid in numpy and copies it to the GPU on
  every call; here it is cached per size and device.
- Equalized-lr layers: the scale (and lr_mul) is folded into the weights once,
  instead of allocating weight * scale on every call.
"""
import numpy as np
import torch
import torch.nn.functional as F

from models.avatarforcing import styledecoder as sd
from models.avatarforcing import encoder as en

_ORIG = {}
_GRID = {}
# Upstream samples in fp32 (autocast promotes grid_sampler). fp16 differs by 67 dB PSNR and halves
# both the reference features (63 -> 31 MB) and the sampling traffic. The engine sets this.
GRID_SAMPLE_DTYPE = torch.float32


# ----------------------------------------------------------------------------- ops
def _blur_kernel(mod, dtype, device):
    cache = mod.__dict__.setdefault("_k_cache", {})
    k = cache.get((dtype, device))
    if k is None:
        k = torch.flip(mod.kernel, [0, 1]).to(device=device, dtype=dtype).view(1, 1, *mod.kernel.shape)
        cache[(dtype, device)] = k
    return k


def _blur_fwd(self, x):
    # Upstream's own formulation (pad, fold channels into the batch, one single-channel conv),
    # with the flipped kernel cached instead of rebuilt per call. On this GPU it beat both a
    # depthwise conv (cuDNN falls back to a slow native kernel for fp16 4x4 depthwise) and
    # 3x avg_pool2d (the binomial [1,3,3,1] factorization).
    p0, p1 = self.pad
    b, c, h, w = x.shape
    y = F.pad(x, [p0, p1, p0, p1]).reshape(b * c, 1, h + p0 + p1, w + p0 + p1)
    y = F.conv2d(y, _blur_kernel(self, x.dtype, x.device))
    return y.reshape(b, c, y.shape[-2], y.shape[-1])


def _eqlinear_fwd(self, x):
    if "_wf" not in self.__dict__ and "_wf" not in self._buffers:
        return _ORIG[type(self), "forward"](self, x)
    if self.activation:
        return sd.fused_leaky_relu(F.linear(x, self._wf), self._bf)
    return F.linear(x, self._wf, self._bf)


def _eqconv_fwd(self, x):
    if "_wf" not in self._buffers:
        return _ORIG[type(self), "forward"](self, x)
    return F.conv2d(x, self._wf, bias=self._bf, stride=self.stride, padding=self.padding)


def _modconv_fwd(self, x, style):
    if "_wf" not in self._buffers:
        return _ORIG[sd.ModulatedConv2d, "forward"](self, x, style)
    b, cin = x.shape[:2]
    s = self.modulation(style)  # (B, Cin)
    x = x * s.view(b, cin, 1, 1).to(x.dtype)
    if self.upsample:
        out = F.conv_transpose2d(x, self._wt, stride=2, padding=0)
    elif self.downsample:
        out = F.conv2d(self.blur(x), self._wf, stride=2)
    else:
        out = F.conv2d(x, self._wf, padding=self.padding)
    if self.demodulate:
        d = torch.rsqrt(s.float().pow(2) @ self._w2.t() + 1e-8)  # (B, Cout), fp32
        out = out * d.to(out.dtype).view(b, -1, 1, 1)
    if self.upsample:
        out = self.blur(out)
    return out


def _base_grid(n, device):
    key = (n, device)
    g = _GRID.get(key)
    if g is None:
        xs = np.linspace(-1, 1, n)
        xs = np.stack(np.meshgrid(xs, xs), 2)
        g = torch.tensor(xs).float().unsqueeze(0).to(device)  # (1, n, n, 2), same values as upstream
        _GRID[key] = g
    return g


def _toflow_fwd(self, input, style, feat, skip=None):
    out = self.conv(input, style)
    out = out + self.bias
    if skip is not None:
        out = out + self.upsample(skip)
    sampler = torch.tanh(out[:, 0:2, :, :])
    mask = torch.sigmoid(out[:, 2:3, :, :])
    flow = sampler.permute(0, 2, 3, 1).float() + _base_grid(input.size(2), input.device)
    gdt = GRID_SAMPLE_DTYPE
    if feat.shape[0] != flow.shape[0]:
        feat = feat.expand(flow.shape[0], -1, -1, -1)
    feat_warp = F.grid_sample(feat.to(gdt), flow.to(gdt), align_corners=False).to(input.dtype) * mask
    return feat_warp, feat_warp + input * (1.0 - mask), out, flow


_PATCHES = [
    (sd.Blur, "forward", _blur_fwd),
    (en.Blur, "forward", _blur_fwd),
    (sd.EqualLinear, "forward", _eqlinear_fwd),
    (en.EqualLinear, "forward", _eqlinear_fwd),
    (sd.EqualConv2d, "forward", _eqconv_fwd),
    (en.EqualConv2d, "forward", _eqconv_fwd),
    (sd.ModulatedConv2d, "forward", _modconv_fwd),
    (sd.ToFlow, "forward", _toflow_fwd),
]


def enable():
    for cls, name, fn in _PATCHES:
        _ORIG.setdefault((cls, name), cls.__dict__[name])
        setattr(cls, name, fn)


def disable():
    for cls, name, _ in _PATCHES:
        if (cls, name) in _ORIG:
            setattr(cls, name, _ORIG[cls, name])


# ----------------------------------------------------------------------------- preparation
@torch.no_grad()
def fold_motion_autoencoder(mae, dtype):
    """Fold equalized-lr scales into the weights, store them in `dtype`, drop the fp32 originals.

    Works on any device; call before moving to the GPU so the fp32 copy never lands there.
    """
    for m in mae.modules():
        if isinstance(m, (sd.EqualLinear, en.EqualLinear)):
            wf = (m.weight.float() * m.scale).to(dtype)
            bf = None if m.bias is None else (m.bias.float() * m.lr_mul).to(dtype)
            del m.weight
            if m.bias is not None:
                del m.bias
            m.register_buffer("_wf", wf)
            m.register_buffer("_bf", bf)
        elif isinstance(m, (sd.EqualConv2d, en.EqualConv2d)):
            wf = (m.weight.float() * m.scale).to(dtype)
            bf = None if m.bias is None else m.bias.to(dtype)
            del m.weight
            if m.bias is not None:
                del m.bias
            m.register_buffer("_wf", wf)
            m.register_buffer("_bf", bf)
        elif isinstance(m, sd.ModulatedConv2d):
            w = m.weight.float()[0] * m.scale  # (Cout, Cin, k, k)
            del m.weight
            m.register_buffer("_wf", w.to(dtype))
            m.register_buffer("_wt", w.transpose(0, 1).contiguous().to(dtype) if m.upsample else None)
            m.register_buffer("_w2", w.pow(2).sum([2, 3]))  # fp32 (Cout, Cin); kept fp32 below
    # remaining float params/buffers (biases, constant input, blur kernels) -> dtype, except _w2
    for m in mae.modules():
        for name, p in list(m._parameters.items()):
            if p is not None and p.is_floating_point():
                m._parameters[name] = torch.nn.Parameter(p.data.to(dtype), requires_grad=False)
        for name, b in list(m._buffers.items()):
            if b is not None and b.is_floating_point() and name != "_w2":
                m._buffers[name] = b.to(dtype)
    return mae


def truncate_decoder(dec, out_res):
    """Keep the synthesis stages up to `out_res` (8, 16, ..., out_res); drop the rest."""
    n = int(np.log2(out_res)) - 2  # stages 8..out_res
    assert 1 <= n <= len(dec.to_rgbs), (out_res, len(dec.to_rgbs))
    dec.convs = torch.nn.ModuleList(list(dec.convs)[: 2 * n])
    dec.to_rgbs = torch.nn.ModuleList(list(dec.to_rgbs)[:n])
    dec.to_flows = torch.nn.ModuleList(list(dec.to_flows)[:n])
    dec.n_stages = n
    return dec


def synthesize(dec, wa, feats):
    """Synthesis.forward with alpha=None, stopping after the kept stages.

    Upstream repeats `wa` into n_latent identical rows and indexes them per layer; every
    row is the same vector, so it is passed directly. The image at stage k is the
    StyleGAN2 skip image at that resolution.
    """
    out = dec.input(wa)
    out = dec.conv1(out, wa)
    skip = skip_flow = None
    for k in range(len(dec.to_rgbs)):
        out = dec.convs[2 * k](out, wa)
        out = dec.convs[2 * k + 1](out, wa)
        if k == 0:
            out_warp, out, skip_flow, _ = dec.to_flows[k](out, wa, feats[k])
            skip = dec.to_rgbs[k](out_warp)
        else:
            out_warp, out, skip_flow, _ = dec.to_flows[k](out, wa, feats[k], skip_flow)
            skip = dec.to_rgbs[k](out_warp, skip)
    return skip


def encode_motion_only(enc, x):
    """enc.enc_motion without keeping every intermediate feature map alive."""
    h = x
    for conv in enc.net_app.convs:
        h = conv(h)
    return enc.fc(h.squeeze(-1).squeeze(-1))
