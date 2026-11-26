"""AvatarForcing within a 1 GB VRAM budget: the engine HARTOS's talking-avatar sidecar runs.

engine.LowVRAMAvatar (int8 flow transformer, fp16 motion autoencoder, CUDA graphs),
session.stream_blocks (block-by-block generation), fast_ops (patched forwards), int8
(Int8Linear). A regular package, so this copy wins over any lowvram/ in the model folder.
"""
