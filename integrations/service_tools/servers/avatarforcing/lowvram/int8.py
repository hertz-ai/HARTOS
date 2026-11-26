"""Weight-only int8 for nn.Linear: symmetric scales per group of `group` input weights.

Halves the VRAM of the transformers (flow transformer 288 -> ~150 MB, wav2vec2 190 -> ~100 MB).
Activations stay bf16/fp16. The weights are dequantized on the fly, one extra kernel per layer
inside the CUDA graph. group=0 gives one scale per output channel. tests_lowvram/test_int8.py
measures the accuracy.
"""
import torch
import torch.nn as nn
import torch.nn.functional as F


class Int8Linear(nn.Module):
    def __init__(self, lin: nn.Linear, dtype, group=128):
        super().__init__()
        w = lin.weight.detach().float()
        out_f, in_f = w.shape
        g = group if group and in_f % group == 0 else in_f
        wg = w.view(out_f, in_f // g, g)
        scale = wg.abs().amax(dim=-1, keepdim=True).clamp(min=1e-12) / 127.0
        self.register_buffer("qweight", torch.round(wg / scale).clamp(-127, 127).to(torch.int8))
        self.register_buffer("scale", scale.to(dtype))
        self.register_buffer("bias", None if lin.bias is None else lin.bias.detach().to(dtype))
        self.in_features, self.out_features, self.group = in_f, out_f, g

    def forward(self, x):
        w = (self.qweight * self.scale).view(self.out_features, self.in_features)  # one kernel: int8 x scale -> scale dtype
        return F.linear(x, w, self.bias)

    def extra_repr(self):
        return f"{self.in_features}, {self.out_features}, int8, group={self.group}"


def quantize_linears_(module, dtype, group=128, min_params=65536, skip=(nn.MultiheadAttention,)):
    """Replace every nn.Linear with at least `min_params` weights by Int8Linear, in place.

    nn.MultiheadAttention reads out_proj.weight directly, so its children are left alone."""
    n = 0
    for name, child in list(module.named_children()):
        if isinstance(child, skip):
            continue
        if type(child) is nn.Linear and child.weight.numel() >= min_params:
            setattr(module, name, Int8Linear(child, dtype, group))
            n += 1
        else:
            n += quantize_linears_(child, dtype, group, min_params, skip)
    return n
