#!/usr/bin/env python3
"""
mlx_model_info.py
=================
Runtime model metadata discovery for MLX models.

There is deliberately no static model registry. The metadata is read from the
model's own ``config.json``, which is the same file the inference server loads.
The model directory is located from one of:

  1. ``MLX_MODEL_PATH`` (explicit directory or config.json path)
  2. the ``model_path`` argument
  3. ``MLX_MODEL`` / the model id returned by ``GET /v1/models``, searched under
     ``MLX_MODEL_DIRS`` (colon-separated) and the usual model cache roots:
     ``~/mlx-models``, ``~/models``, ``~/Documents/models`` and the Hugging Face
     cache (including ``models--org--name/snapshots/*``).

If the config cannot be found, ``model_info()`` returns ``None`` and the caller
skips model-specific gauges. It never guesses architecture values.

The dataclass exposes every attribute used by mlx_metrics_proxy and
mlx_kv_cache_agent. KV cache size is computed from the *attention* layers only,
which matters for hybrid models such as Qwen3.5 (8 full-attention layers out of
32) where the remaining layers keep a recurrent state instead of a KV cache.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mlx_logging import debug, degraded, info, parse_upstream_url

_DEFAULT_ROOTS = (
    "~/mlx-models",
    "~/models",
    "~/Documents/models",
    "~/.cache/huggingface/hub",
    "~/.cache/lm-studio/models",
)

_CACHE_TTL_SECONDS = 300.0
_NEGATIVE_CACHE_TTL_SECONDS = 15.0
_cache_lock = threading.Lock()
_cache: dict[tuple[str, str, str], tuple[float, "ModelInfo | None"]] = {}


@dataclass(frozen=True)
class ModelInfo:
    """Model configuration discovered from the model's config.json."""

    model_id: str
    model_path: str
    max_context_tokens: int
    num_layers: int
    hidden_size: int
    num_attention_heads: int
    num_key_value_heads: int
    head_dim: int
    attention_layers: int
    linear_layers: int
    dtype: str
    vocab_size: int
    intermediate_size: int
    quant_bits: int
    quant_group_size: int
    linear_key_heads: int
    linear_value_heads: int
    linear_key_head_dim: int
    linear_value_head_dim: int
    linear_conv_kernel_dim: int
    mtp_layers: int
    tie_word_embeddings: bool
    arch: str
    source: str
    num_local_experts: int = 0
    num_experts_per_tok: int = 0
    moe_intermediate_size: int = 0
    shared_expert_intermediate_size: int = 0
    sliding_window: int = 0
    total_weight_bytes: int = 0
    shard_file_count: int = 0
    attention_layer_indices: tuple[int, ...] = ()
    sliding_layer_indices: tuple[int, ...] = ()

    @property
    def bytes_per_element(self) -> int:
        return 4 if self.dtype == "float32" else 2

    @property
    def gqa_ratio(self) -> int:
        if self.num_attention_heads > 0 and self.num_key_value_heads > 0:
            return max(1, self.num_attention_heads // self.num_key_value_heads)
        return 0

    @property
    def is_moe(self) -> bool:
        return self.num_local_experts > 0

    @property
    def kv_bytes_per_token(self) -> int:
        """KV cache bytes per token across all full-attention layers.

        Per token: 2 (K and V) * attention_layers * kv_heads * head_dim *
        bytes_per_element. For Qwen3.5-4B-MLX-4bit this is
        2 * 8 * 4 * 256 * 2 = 32768 bytes/token.
        """
        if not (self.attention_layers and self.head_dim and self.num_key_value_heads):
            return 0
        return (
            2
            * self.attention_layers
            * self.num_key_value_heads
            * self.head_dim
            * self.bytes_per_element
        )

    @property
    def kv_bytes_for_max_context(self) -> int:
        return self.kv_bytes_for_max_context_shard(
            layer_start=0,
            layer_end=self.num_layers - 1,
            tp_size=1,
            rank=0,
        )

    def kv_bytes_per_token_for_shard(
        self,
        layer_start: int = 0,
        layer_end: int = -1,
        dim_kv_heads: int | None = None,
        tp_size: int = 1,
        rank: int = 0,
    ) -> int:
        """KV cache bytes per token on this shard (supporting PP, TP, and hybrid).

        Formula:
          KV_local = 2 * L_attention,local * H_KV,local * D_head * B_element
        """
        if not (self.num_layers and self.head_dim and self.num_key_value_heads):
            return 0
        if layer_end < 0:
            layer_end = self.num_layers - 1

        indices = self.attention_layer_indices or tuple(range(self.attention_layers))
        local_attn_layers = sum(1 for i in indices if layer_start <= i <= layer_end)

        if dim_kv_heads is not None and dim_kv_heads > 0:
            local_kv_heads = dim_kv_heads
        elif tp_size > 1:
            if self.num_key_value_heads % tp_size == 0:
                local_kv_heads = max(1, self.num_key_value_heads // tp_size)
            else:
                base = self.num_key_value_heads // tp_size
                rem = self.num_key_value_heads % tp_size
                local_kv_heads = max(1, base + (1 if rank < rem else 0))
        else:
            local_kv_heads = self.num_key_value_heads

        return (
            2
            * local_attn_layers
            * local_kv_heads
            * self.head_dim
            * self.bytes_per_element
        )

    def kv_bytes_for_max_context_shard(
        self,
        layer_start: int = 0,
        layer_end: int = -1,
        dim_kv_heads: int | None = None,
        tp_size: int = 1,
        rank: int = 0,
    ) -> int:
        """Approx KV bytes for one full-length context sequence on this shard,
        properly applying sliding window bounds to sliding-attention layers."""
        if not (self.num_layers and self.head_dim and self.num_key_value_heads):
            return 0
        if layer_end < 0:
            layer_end = self.num_layers - 1

        all_indices = self.attention_layer_indices or tuple(range(self.attention_layers))
        sliding_set = set(self.sliding_layer_indices)
        full_set = set(all_indices) - sliding_set

        local_full = sum(1 for i in full_set if layer_start <= i <= layer_end)
        local_sliding = sum(1 for i in sliding_set if layer_start <= i <= layer_end)

        if dim_kv_heads is not None and dim_kv_heads > 0:
            local_kv_heads = dim_kv_heads
        elif tp_size > 1:
            if self.num_key_value_heads % tp_size == 0:
                local_kv_heads = max(1, self.num_key_value_heads // tp_size)
            else:
                base = self.num_key_value_heads // tp_size
                rem = self.num_key_value_heads % tp_size
                local_kv_heads = max(1, base + (1 if rank < rem else 0))
        else:
            local_kv_heads = self.num_key_value_heads

        bytes_per_tok_one_layer = (
            2 * local_kv_heads * self.head_dim * self.bytes_per_element
        )
        sliding_ctx = (
            min(self.max_context_tokens, self.sliding_window)
            if self.sliding_window > 0
            else self.max_context_tokens
        )
        return bytes_per_tok_one_layer * (
            local_full * self.max_context_tokens + local_sliding * sliding_ctx
        )


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def _model_roots() -> list[Path]:
    raw = os.environ.get("MLX_MODEL_DIRS", "")
    roots = [p for p in raw.split(os.pathsep) if p.strip()]
    roots.extend(_DEFAULT_ROOTS)
    seen: set[str] = set()
    out: list[Path] = []
    for root in roots:
        p = _expand(root)
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _looks_like_path(value: str) -> bool:
    return (
        value.startswith("/")
        or value.startswith("~")
        or value.startswith("./")
        or value.startswith("../")
    )


def _candidate_configs(model_id: str | None, model_path: str | None) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        out.append(path)

    for raw in (model_path, os.environ.get("MLX_MODEL_PATH", "")):
        if not raw:
            continue
        p = _expand(raw)
        if p.is_file():
            add(p)
        elif p.is_dir():
            add(p / "config.json")

    if model_id and _looks_like_path(model_id):
        p = _expand(model_id)
        if p.is_file():
            add(p)
        elif p.is_dir():
            add(p / "config.json")

    ids = [x for x in (model_id,) if x and not _looks_like_path(x)]
    for root in _model_roots():
        for mid in ids:
            add(root / mid / "config.json")
            add(root / Path(mid).name / "config.json")
            if "/" in mid:
                org, name = mid.split("/", 1)
                hf_dir = root / f"models--{org}--{name}"
                snapshots = hf_dir / "snapshots"
                if snapshots.is_dir():
                    try:
                        snaps = sorted(snapshots.iterdir(), reverse=True)
                    except OSError as exc:
                        debug(f"unable to list {snapshots}: {exc!r}", component="model-info")
                        snaps = []
                    for snap in snaps:
                        add(snap / "config.json")
    return out


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        debug(f"failed to read {path}: {exc!r}", component="model-info")
        return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first_int(source: dict[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        raw = source.get(key)
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
    return default


def _dtype_name(source: dict[str, Any]) -> str:
    raw = str(
        source.get("dtype")
        or source.get("torch_dtype")
        or source.get("float_dtype")
        or ""
    ).lower()
    if "float32" in raw or raw in ("fp32", "f32"):
        return "float32"
    if "bfloat" in raw or raw in ("bf16", "b16"):
        return "bfloat16"
    if "float" in raw or raw in ("fp16", "f16", "half"):
        return "float16"
    return raw or "unknown"


def _attention_layer_count(text: dict[str, Any], num_layers: int) -> int:
    layer_types = text.get("layer_types")
    if isinstance(layer_types, list) and layer_types:
        attention = sum(
            1
            for item in layer_types
            if str(item) in ("full_attention", "sliding_attention", "attention")
        )
        if attention:
            return attention

    interval = _first_int(text, "full_attention_interval")
    if interval > 0:
        # full_attention_interval=N means every Nth layer is a full-attention
        # layer; e.g. interval=4 over 32 layers -> 8 attention layers.
        return min(num_layers, math.ceil(num_layers / interval))
    return num_layers


def _attention_layer_indices(text: dict[str, Any], num_layers: int) -> tuple[int, ...]:
    layer_types = text.get("layer_types")
    if isinstance(layer_types, list) and layer_types:
        indices = tuple(
            i
            for i, item in enumerate(layer_types[:num_layers])
            if str(item) in ("full_attention", "sliding_attention", "attention")
        )
        if indices:
            return indices

    interval = _first_int(text, "full_attention_interval")
    if interval > 0 and num_layers > 0:
        return tuple(i for i in range(num_layers) if (i + 1) % interval == 0)
    return tuple(range(num_layers)) if num_layers > 0 else ()


def _sliding_attention_layer_indices(text: dict[str, Any], num_layers: int) -> tuple[int, ...]:
    layer_types = text.get("layer_types")
    if isinstance(layer_types, list) and layer_types:
        return tuple(
            i
            for i, item in enumerate(layer_types[:num_layers])
            if str(item) == "sliding_attention"
        )
    sliding_win = _first_int(text, "sliding_window")
    interval = _first_int(text, "full_attention_interval")
    if interval > 0 and sliding_win > 0 and num_layers > 0:
        return tuple(i for i in range(num_layers) if (i + 1) % interval != 0)
    if sliding_win > 0 and num_layers > 0:
        return tuple(range(num_layers))
    return ()


def _parse_config(path: Path, model_id: str, data: dict[str, Any]) -> ModelInfo:
    text = data.get("text_config")
    if not isinstance(text, dict):
        text = data

    num_layers = _first_int(text, "num_hidden_layers", "num_layers")
    hidden_size = _first_int(text, "hidden_size", "n_embd", "d_model")
    num_attention_heads = _first_int(
        text, "num_attention_heads", "n_head", "num_heads"
    )
    num_key_value_heads = _first_int(
        text, "num_key_value_heads", "n_head_kv", default=num_attention_heads
    )
    head_dim = _first_int(text, "head_dim")
    if not head_dim and num_attention_heads and hidden_size:
        head_dim = hidden_size // num_attention_heads

    attn_indices = _attention_layer_indices(text, num_layers)
    sliding_indices = _sliding_attention_layer_indices(text, num_layers)
    attention_layers = len(attn_indices) if attn_indices else _attention_layer_count(text, num_layers)
    linear_layers = max(0, num_layers - attention_layers)

    quantization = {}
    for key in ("quantization", "quantization_config"):
        value = data.get(key) if isinstance(data.get(key), dict) else text.get(key)
        if isinstance(value, dict) and value:
            quantization = value
            break

    dtype = _dtype_name(text)
    if dtype == "unknown":
        dtype = _dtype_name(data)

    arch = ""
    architectures = data.get("architectures")
    if isinstance(architectures, list) and architectures:
        arch = ",".join(str(a) for a in architectures)
    else:
        arch = str(data.get("model_type") or text.get("model_type") or "")

    total_weight_bytes, shard_file_count = _safetensors_stats(path.parent)

    return ModelInfo(
        model_id=model_id,
        model_path=str(path.parent),
        max_context_tokens=_first_int(
            text,
            "max_position_embeddings",
            "max_sequence_length",
            "seq_length",
            "n_positions",
        ),
        num_layers=num_layers,
        hidden_size=hidden_size,
        num_attention_heads=num_attention_heads,
        num_key_value_heads=num_key_value_heads,
        head_dim=head_dim,
        attention_layers=attention_layers,
        linear_layers=linear_layers,
        dtype=dtype,
        vocab_size=_first_int(text, "vocab_size"),
        intermediate_size=_first_int(text, "intermediate_size", "ffn_dim"),
        quant_bits=_first_int(quantization, "bits", "quant_bits"),
        quant_group_size=_first_int(quantization, "group_size", "quant_group_size"),
        linear_key_heads=_first_int(
            text, "linear_num_key_heads", "linear_key_heads"
        ),
        linear_value_heads=_first_int(
            text, "linear_num_value_heads", "linear_value_heads"
        ),
        linear_key_head_dim=_first_int(text, "linear_key_head_dim"),
        linear_value_head_dim=_first_int(text, "linear_value_head_dim"),
        linear_conv_kernel_dim=_first_int(text, "linear_conv_kernel_dim"),
        mtp_layers=_first_int(text, "mtp_num_hidden_layers", "mtp_layers"),
        tie_word_embeddings=bool(
            text.get("tie_word_embeddings", data.get("tie_word_embeddings", False))
        ),
        arch=arch,
        source=str(path),
        num_local_experts=_first_int(
            text, "num_local_experts", "n_routed_experts", "num_experts"
        ),
        num_experts_per_tok=_first_int(
            text, "num_experts_per_tok", "experts_per_token"
        ),
        moe_intermediate_size=_first_int(text, "moe_intermediate_size"),
        shared_expert_intermediate_size=_first_int(
            text, "shared_expert_intermediate_size"
        ),
        sliding_window=_first_int(text, "sliding_window"),
        total_weight_bytes=total_weight_bytes,
        shard_file_count=shard_file_count,
        attention_layer_indices=attn_indices,
        sliding_layer_indices=sliding_indices,
    )


def _safetensors_stats(model_dir: Path) -> tuple[int, int]:
    """Return (total_weight_bytes, shard_file_count) from index or safetensors."""
    idx_path = model_dir / "model.safetensors.index.json"
    if idx_path.is_file():
        idx_data = _load_json(idx_path)
        if isinstance(idx_data, dict):
            weight_map = idx_data.get("weight_map")
            files = (
                sorted({str(v) for v in weight_map.values() if isinstance(v, str)})
                if isinstance(weight_map, dict)
                else []
            )
            meta = idx_data.get("metadata")
            total_size = 0
            if isinstance(meta, dict):
                raw_size = meta.get("total_size")
                if isinstance(raw_size, (int, float)) and raw_size > 0:
                    total_size = int(raw_size)
            if not total_size and files:
                for fname in files:
                    fp = model_dir / fname
                    try:
                        if fp.is_file():
                            total_size += fp.stat().st_size
                    except OSError:
                        pass
            if files or total_size:
                return total_size, len(files)

    try:
        st_files = sorted(model_dir.glob("*.safetensors"))
        total_size = sum(fp.stat().st_size for fp in st_files if fp.is_file())
        return total_size, len(st_files)
    except OSError:
        return 0, 0


def _pick_served_model_id(models: list[Any]) -> str | None:
    """Pick the active served model id from /v1/models (prefer non-default_model)."""
    fallback: str | None = None
    for entry in reversed(models):
        if isinstance(entry, dict):
            mid = entry.get("id")
            if isinstance(mid, str) and mid:
                if mid != "default_model":
                    return mid
                fallback = fallback or mid
    return fallback


def _discover_from_upstream(upstream_url: str) -> str | None:
    import urllib.request

    try:
        _, _, _, base_url = parse_upstream_url(upstream_url)
    except ValueError as exc:
        debug(f"invalid upstream URL ({upstream_url!r}): {exc}", component="model-info")
        return None
    url = base_url.rstrip("/") + "/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf8"))
        models = data.get("data") or []
        if isinstance(models, list):
            return _pick_served_model_id(models)
    except Exception as exc:
        debug(f"upstream model discovery failed ({url}): {exc!r}",
              component="model-info")
    return None


def _normalise_model_id(model_id: str | None, path: Path | None) -> str:
    if model_id:
        if _looks_like_path(model_id):
            return Path(model_id.rstrip("/")).name
        return model_id
    return path.parent.name if path else "unknown"


def model_info(
    model_id: str | None = None,
    model_path: str | None = None,
    upstream_url: str | None = None,
    refresh: bool = False,
) -> ModelInfo | None:
    """Return metadata parsed from the model's config.json, or None.

    Args:
        model_id: Model identifier returned by GET /v1/models (or a path).
        model_path: Explicit model directory / config.json path.
        upstream_url: mlx_lm.server base URL used to discover the model id.
        refresh: Ignore the in-process cache and re-read config.json.
    """
    effective_id = model_id
    effective_path = model_path or os.environ.get("MLX_MODEL_PATH") or None

    cache_key = (effective_id or "", effective_path or "", upstream_url or "")
    now = time.monotonic()
    if not refresh:
        with _cache_lock:
            cached = _cache.get(cache_key)
        if cached:
            ttl = _CACHE_TTL_SECONDS if cached[1] is not None else _NEGATIVE_CACHE_TTL_SECONDS
            if now - cached[0] < ttl:
                return cached[1]

    if not effective_id and upstream_url:
        effective_id = _discover_from_upstream(upstream_url)

    candidates = _candidate_configs(effective_id, effective_path)
    found: Path | None = None
    for candidate in candidates:
        if candidate.is_file():
            found = candidate
            break

    result: ModelInfo | None = None
    if found is not None:
        data = _load_json(found)
        if data is not None:
            resolved_id = _normalise_model_id(effective_id, found)
            try:
                result = _parse_config(found, resolved_id, data)
                info(
                    f"model config: {resolved_id} "
                    f"ctx={result.max_context_tokens} "
                    f"layers={result.num_layers} "
                    f"attention_layers={result.attention_layers} "
                    f"kv_bytes_per_token={result.kv_bytes_per_token}",
                    component="model-info",
                )
            except Exception as exc:
                degraded(f"failed to parse {found}: {exc!r}",
                         component="model-info")
        else:
            degraded(f"model config at {found} is not valid JSON",
                     component="model-info")
    else:
        searched = ", ".join(str(p) for p in candidates[:6])
        degraded(
            "could not locate model config.json "
            f"(model_id={effective_id or '<unset>'}, path={effective_path or '<unset>'}); "
            f"searched: {searched or '<no candidates>'}. "
            "Set MLX_MODEL_PATH or MLX_MODEL_DIRS.",
            component="model-info",
        )

    with _cache_lock:
        _cache[cache_key] = (now, result)
    return result


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Inspect runtime MLX model metadata")
    ap.add_argument("model", nargs="?", help="model id or model directory")
    ap.add_argument("--path", help="explicit model directory or config.json path")
    ap.add_argument("--upstream", help="mlx_lm.server base URL for discovery")
    args = ap.parse_args()

    info_obj = model_info(args.model, args.path, args.upstream, refresh=True)
    if info_obj is None:
        print("model config not found")
        return 1
    for field in (
        "model_id",
        "model_path",
        "max_context_tokens",
        "num_layers",
        "attention_layers",
        "linear_layers",
        "hidden_size",
        "num_attention_heads",
        "num_key_value_heads",
        "head_dim",
        "dtype",
        "vocab_size",
        "intermediate_size",
        "quant_bits",
        "quant_group_size",
        "linear_key_heads",
        "linear_value_heads",
        "linear_key_head_dim",
        "linear_value_head_dim",
        "linear_conv_kernel_dim",
        "mtp_layers",
        "tie_word_embeddings",
        "arch",
        "num_local_experts",
        "num_experts_per_tok",
        "moe_intermediate_size",
        "shared_expert_intermediate_size",
        "sliding_window",
        "gqa_ratio",
        "total_weight_bytes",
        "shard_file_count",
        "kv_bytes_per_token",
        "kv_bytes_for_max_context",
        "source",
    ):
        print(f"{field}={getattr(info_obj, field)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
