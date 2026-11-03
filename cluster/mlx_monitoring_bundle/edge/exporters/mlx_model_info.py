#!/usr/bin/env python3
"""
mlx_model_info.py
=================
Static model metadata for MLX-compatible models.

Provides context length, KV cache bytes/token, and full-context KV estimate.
Used by mlx_kv_cache_agent and mlx_metrics_proxy for model-specific gauges.

All values derived from published model configs (Hugging Face, MLX community).
If a model is not in the registry, falls back to heuristic based on model ID.
"""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ModelInfo:
    """Static model configuration attributes."""
    model_id: str
    max_context_tokens: int
    num_layers: int
    hidden_size: int
    num_attention_heads: int
    num_key_value_heads: int | None = None
    head_dim: int | None = None
    dtype: str = "fp16"  # fp16 | bf16 | fp32

    @property
    def kv_bytes_per_token(self) -> int:
        """KV cache bytes per token for full model (fp16 = 2 bytes per param)."""
        # Per token: 2 * num_layers * num_kv_heads * head_dim * 2 bytes (fp16)
        # For GQA: num_kv_heads = num_key_value_heads, else = num_attention_heads
        kv_heads = self.num_key_value_heads or self.num_attention_heads
        hd = self.head_dim or (self.hidden_size // self.num_attention_heads)
        bytes_per_param = 2 if self.dtype == "fp16" else (2 if self.dtype == "bf16" else 4)
        return 2 * self.num_layers * kv_heads * hd * bytes_per_param

    @property
    def kv_bytes_for_max_context(self) -> int:
        """KV cache bytes for one full-length context sequence."""
        return self.kv_bytes_per_token * self.max_context_tokens


# Known MLX model registry (subset; add more as needed)
_MODEL_REGISTRY = {
    # Qwen 2.5 / 3.5 family
    "mlx-community/Qwen2.5-0.5B-MLX-4bit": ModelInfo(
        "mlx-community/Qwen2.5-0.5B-MLX-4bit", 32768, 24, 896, 14, 2, 64),
    "mlx-community/Qwen2.5-1.5B-MLX-4bit": ModelInfo(
        "mlx-community/Qwen2.5-1.5B-MLX-4bit", 32768, 28, 1536, 12, 2, 128),
    "mlx-community/Qwen2.5-3B-MLX-4bit": ModelInfo(
        "mlx-community/Qwen2.5-3B-MLX-4bit", 32768, 36, 2048, 16, 2, 128),
    "mlx-community/Qwen2.5-7B-MLX-4bit": ModelInfo(
        "mlx-community/Qwen2.5-7B-MLX-4bit", 32768, 28, 3584, 28, 4, 128),
    "mlx-community/Qwen2.5-14B-MLX-4bit": ModelInfo(
        "mlx-community/Qwen2.5-14B-MLX-4bit", 32768, 48, 5120, 40, 8, 128),
    "mlx-community/Qwen2.5-32B-MLX-4bit": ModelInfo(
        "mlx-community/Qwen2.5-32B-MLX-4bit", 32768, 64, 5120, 40, 8, 128),

    "mlx-community/Qwen3.5-4B-MLX-4bit": ModelInfo(
        "mlx-community/Qwen3.5-4B-MLX-4bit", 32768, 36, 2560, 20, 4, 128),
    "mlx-community/Qwen3.5-4B-MLX-8bit": ModelInfo(
        "mlx-community/Qwen3.5-4B-MLX-8bit", 32768, 36, 2560, 20, 4, 128),
    "mlx-community/Qwen3.5-7B-MLX-4bit": ModelInfo(
        "mlx-community/Qwen3.5-7B-MLX-4bit", 32768, 32, 3584, 28, 4, 128),
    "mlx-community/Qwen3.5-7B-MLX-8bit": ModelInfo(
        "mlx-community/Qwen3.5-7B-MLX-8bit", 32768, 32, 3584, 28, 4, 128),

    # Llama 3 / 3.1 / 3.2
    "mlx-community/Llama-3.2-1B-MLX-4bit": ModelInfo(
        "mlx-community/Llama-3.2-1B-MLX-4bit", 131072, 16, 2048, 32, 8, 64),
    "mlx-community/Llama-3.2-3B-MLX-4bit": ModelInfo(
        "mlx-community/Llama-3.2-3B-MLX-4bit", 131072, 28, 3072, 24, 8, 128),
    "mlx-community/Llama-3.1-8B-MLX-4bit": ModelInfo(
        "mlx-community/Llama-3.1-8B-MLX-4bit", 131072, 32, 4096, 32, 8, 128),
    "mlx-community/Llama-3.1-70B-MLX-4bit": ModelInfo(
        "mlx-community/Llama-3.1-70B-MLX-4bit", 131072, 80, 8192, 64, 8, 128),

    # Mistral / Mixtral
    "mlx-community/Mistral-7B-v0.3-MLX-4bit": ModelInfo(
        "mlx-community/Mistral-7B-v0.3-MLX-4bit", 32768, 32, 4096, 32, 8, 128),
    "mlx-community/Mixtral-8x7B-MLX-4bit": ModelInfo(
        "mlx-community/Mixtral-8x7B-MLX-4bit", 32768, 32, 4096, 32, 8, 128),
    "mlx-community/Mixtral-8x22B-MLX-4bit": ModelInfo(
        "mlx-community/Mixtral-8x22B-MLX-4bit", 65536, 56, 6144, 48, 8, 128),

    # Phi-3 / Phi-4
    "mlx-community/Phi-3-mini-4k-MLX-4bit": ModelInfo(
        "mlx-community/Phi-3-mini-4k-MLX-4bit", 4096, 32, 3072, 32, None, None),
    "mlx-community/Phi-3-mini-128k-MLX-4bit": ModelInfo(
        "mlx-community/Phi-3-mini-128k-MLX-4bit", 131072, 32, 3072, 32, None, None),
    "mlx-community/Phi-3-medium-4k-MLX-4bit": ModelInfo(
        "mlx-community/Phi-3-medium-4k-MLX-4bit", 4096, 40, 4096, 32, None, None),
    "mlx-community/Phi-3-medium-128k-MLX-4bit": ModelInfo(
        "mlx-community/Phi-3-medium-128k-MLX-4bit", 131072, 40, 4096, 32, None, None),

    # Gemma 2
    "mlx-community/gemma-2-2b-MLX-4bit": ModelInfo(
        "mlx-community/gemma-2-2b-MLX-4bit", 8192, 26, 2304, 8, 4, 256),
    "mlx-community/gemma-2-9b-MLX-4bit": ModelInfo(
        "mlx-community/gemma-2-9b-MLX-4bit", 8192, 42, 3584, 16, 8, 256),
    "mlx-community/gemma-2-27b-MLX-4bit": ModelInfo(
        "mlx-community/gemma-2-27b-MLX-4bit", 8192, 46, 4608, 32, 16, 144),

    # CodeLlama
    "mlx-community/CodeLlama-7b-MLX-4bit": ModelInfo(
        "mlx-community/CodeLlama-7b-MLX-4bit", 16384, 32, 4096, 32, 8, 128),
    "mlx-community/CodeLlama-13b-MLX-4bit": ModelInfo(
        "mlx-community/CodeLlama-13b-MLX-4bit", 16384, 40, 5120, 40, 8, 128),
    "mlx-community/CodeLlama-34b-MLX-4bit": ModelInfo(
        "mlx-community/CodeLlama-34b-MLX-4bit", 16384, 48, 8192, 64, 8, 128),

    # Yi
    "mlx-community/Yi-1.5-6B-MLX-4bit": ModelInfo(
        "mlx-community/Yi-1.5-6B-MLX-4bit", 32768, 32, 4096, 32, 8, 128),
    "mlx-community/Yi-1.5-9B-MLX-4bit": ModelInfo(
        "mlx-community/Yi-1.5-9B-MLX-4bit", 32768, 32, 4096, 32, 8, 128),
    "mlx-community/Yi-1.5-34B-MLX-4bit": ModelInfo(
        "mlx-community/Yi-1.5-34B-MLX-4bit", 32768, 48, 8192, 64, 8, 128),
}


def _heuristic_from_id(model_id: str) -> ModelInfo:
    """Best-effort heuristic for unknown model IDs."""
    mid = model_id.lower()

    # Context length hints
    ctx = 32768
    if "128k" in mid or "131k" in mid:
        ctx = 131072
    elif "32k" in mid:
        ctx = 32768
    elif "16k" in mid:
        ctx = 16384
    elif "8k" in mid:
        ctx = 8192
    elif "4k" in mid:
        ctx = 4096

    # Size hints (params B -> rough layers/hidden)
    layers, hidden, heads = 32, 4096, 32
    if "0.5b" in mid or "500m" in mid:
        layers, hidden, heads = 24, 896, 14
    elif "1b" in mid or "1.5b" in mid:
        layers, hidden, heads = 28, 1536, 12
    elif "2b" in mid or "2.7b" in mid:
        layers, hidden, heads = 26, 2304, 8
    elif "3b" in mid:
        layers, hidden, heads = 36, 2048, 16
    elif "4b" in mid:
        layers, hidden, heads = 36, 2560, 20
    elif "6b" in mid:
        layers, hidden, heads = 32, 4096, 32
    elif "7b" in mid or "8b" in mid:
        layers, hidden, heads = 32, 4096, 32
    elif "9b" in mid:
        layers, hidden, heads = 32, 4096, 32
    elif "13b" in mid or "14b" in mid:
        layers, hidden, heads = 40, 5120, 40
    elif "22b" in mid or "27b" in mid:
        layers, hidden, heads = 46, 4608, 32
    elif "32b" in mid or "34b" in mid:
        layers, hidden, heads = 48, 8192, 64
    elif "70b" in mid:
        layers, hidden, heads = 80, 8192, 64

    # GQA hint
    kv_heads = 8 if "gemma" in mid or "llama" in mid or "phi" in mid else None

    return ModelInfo(model_id, ctx, layers, hidden, heads, kv_heads)


def model_info(model_id: str) -> ModelInfo:
    """
    Return ModelInfo for a given model ID.

    Args:
        model_id: Full model identifier (e.g., "mlx-community/Qwen3.5-4B-MLX-4bit")

    Returns:
        ModelInfo with context length, KV bytes/token, etc.
        If unknown, returns heuristic with degraded log (caller should log).
    """
    if model_id in _MODEL_REGISTRY:
        return _MODEL_REGISTRY[model_id]

    # Try prefix match (e.g., org/model-base-4bit)
    for k, v in _MODEL_REGISTRY.items():
        if k.startswith(model_id.rsplit("-", 1)[0]):
            return v

    return _heuristic_from_id(model_id)


if __name__ == "__main__":
    import sys
    for m in sys.argv[1:] or ["mlx-community/Qwen3.5-4B-MLX-4bit"]:
        info = model_info(m)
        print(f"{m}: ctx={info.max_context_tokens:,} "
              f"kv_per_token={info.kv_bytes_per_token:,} "
              f"kv_max_ctx={info.kv_bytes_for_max_context/1e9:.2f} GB")