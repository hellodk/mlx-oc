#!/usr/bin/env python3
"""mlx_model_info.py - small shared helper: model context length + KV-cache math.

Used by the proxy (context-length gauges) and the KV cache agent. Loads the
model's config.json from the HuggingFace cache (or an explicit path) and
computes, from the architecture dims, the approximate KV cache bytes consumed
per token of context (unquantized fp16/bf16).

Usage:
    from mlx_model_info import model_info
    info = model_info("mlx-community/Qwen3-1.7B-4bit")  # any model id works
    info.max_context_tokens   # e.g. 40960 for this example model
    info.kv_bytes_per_token   # e.g. ~114688 (fp16) for this example model

The actual cluster model lives in cluster/cluster.env (MLX_MODEL) - this
module doesn't hardcode which one is "current"; callers pass the id in.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULTS = {
    "max_context_tokens": 32768,
    "kv_bytes_per_token": 0,
    "layers": 0,
    "kv_heads": 0,
    "head_dim": 0,
}

_BYTES = 2  # fp16/bf16


def _hf_cache_root():
    root = os.environ.get("HF_HOME")
    if not root:
        root = os.environ.get("HF_HUB_CACHE")
    if not root:
        root = str(Path.home() / ".cache" / "huggingface")
    return Path(root) / "hub"


def find_config(model: str) -> Path | None:
    """Locate config.json for a model name in the HF cache, if present."""
    safe = model.replace("/", "--")
    snapshots = _hf_cache_root() / f"models--{safe}" / "snapshots"
    if not snapshots.is_dir():
        return None
    for snapshot in sorted(snapshots.iterdir(), reverse=True):
        cfg = snapshot / "config.json"
        if cfg.is_file():
            return cfg
    return None


@dataclass
class ModelInfo:
    name: str
    max_context_tokens: int
    layers: int
    kv_heads: int
    head_dim: int
    kv_bytes_per_token: int

    @property
    def kv_bytes_for_max_context(self) -> int:
        """Bytes the KV cache would use for a full-length single sequence."""
        return self.max_context_tokens * self.kv_bytes_per_token


def model_info(model: str) -> ModelInfo:
    """Return ModelInfo for `model`, falling back to Qwen3-1.7B-like defaults."""
    cfg_path = find_config(model)
    cfg = {}
    if cfg_path:
        try:
            cfg = json.loads(cfg_path.read_text())
        except (OSError, ValueError):
            cfg = {}

    max_ctx = int(
        cfg.get("max_position_embeddings") or DEFAULTS["max_context_tokens"]
    )
    layers = int(cfg.get("num_hidden_layers") or 0)
    kv_heads = int(cfg.get("num_key_value_heads") or 0)
    head_dim = int(cfg.get("head_dim") or 0)

    kv_bytes = 0
    if layers and kv_heads and head_dim:
        # K + V per layer, fp16/bf16
        kv_bytes = layers * 2 * kv_heads * head_dim * _BYTES

    return ModelInfo(
        name=model,
        max_context_tokens=max_ctx,
        layers=layers,
        kv_heads=kv_heads,
        head_dim=head_dim,
        kv_bytes_per_token=kv_bytes,
    )


if __name__ == "__main__":
    import sys

    _default_model = os.environ.get("MLX_MODEL", "mlx-community/Qwen3.5-4B-MLX-8bit")
    info = model_info(sys.argv[1] if len(sys.argv) > 1 else _default_model)
    print(
        f"model={info.name} max_context={info.max_context_tokens} "
        f"layers={info.layers} kv_heads={info.kv_heads} head_dim={info.head_dim} "
        f"kv_bytes/token={info.kv_bytes_per_token} "
        f"kv_bytes@max_context={info.kv_bytes_for_max_context}"
    )
