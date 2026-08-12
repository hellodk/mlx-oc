#!/usr/bin/env python3
"""Load test: OpenAI-compatible chat completions, streaming.

Usage:
  python3 exo_vs_mlx.py http://127.0.0.1:52415 mlx-community/Qwen3.5-9B-4bit
Measures TTFT, tokens/sec, and total latency per request; supports concurrency.
"""
import sys
import time
import threading
import json
from concurrent.futures import ThreadPoolExecutor

import requests

PROMPT = """Write a detailed explanation of how distributed inference across multiple
Apple Silicon machines works, covering memory requirements, model sharding,
communication topologies, and the tradeoffs between pipeline and tensor parallelism.
Be specific and technical."""
MAX_TOKENS = 128
WARMUP = 1
RUNS = 5


def stream_one(base_url, model, n):
    started = time.perf_counter()
    first = None
    n_tokens = 0
    text = []
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": MAX_TOKENS,
        "stream": True,
    }
    try:
        with requests.post(url, json=payload, stream=True, timeout=120) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                piece = delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning")
                if piece:
                    text.append(piece)
                    n_tokens += 1
                    if first is None:
                        first = time.perf_counter() - started
        total = time.perf_counter() - started
        return {
            "n": n,
            "ok": True,
            "ttft_s": round(first, 3) if first is not None else None,
            "total_s": round(total, 3),
            "tokens": n_tokens,
            "tok_per_s": round(n_tokens / total, 2) if total > 0 else 0,
            "preview": "".join(text)[:40],
        }
    except Exception as e:
        return {"n": n, "ok": False, "error": str(e)}


def run(base_url, model, concurrency):
    print(f"\n--- {concurrency} concurrent, {WARMUP} warmup + {RUNS} timed ---")
    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for i in range(WARMUP):
            stream_one(base_url, model, i)
        futs = [ex.submit(stream_one, base_url, model, i) for i in range(RUNS)]
        for f in futs:
            results.append(f.result())
    ok = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    if failed:
        print(f"  {len(failed)} FAILED: {failed[0].get('error')}")
    if not ok:
        return
    ttfts = sorted(r["ttft_s"] for r in ok if r["ttft_s"] is not None)
    totals = sorted(r["total_s"] for r in ok)
    tps = [r["tok_per_s"] for r in ok]
    wall = sum(r["total_s"] for r in ok)
    toks = sum(r["tokens"] for r in ok)
    print(f"  reqs OK: {len(ok)}/{len(results)}")
    print(f"  TTFT   p50 {ttfts[len(ttfts)//2]:.3f}s  p95 {ttfts[min(len(ttfts)-1, int(len(ttfts)*0.95))]:.3f}s")
    print(f"  total  p50 {totals[len(totals)//2]:.3f}s  p95 {totals[min(len(totals)-1, int(len(totals)*0.95))]:.3f}s")
    print(f"  tokens/req {sum(r['tokens'] for r in ok)//len(ok)}  throughput {toks/wall:.1f} tok/s (aggregate)")
    print(f"  sample: {ok[0].get('preview')!r}")


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:52415"
    model = sys.argv[2] if len(sys.argv) > 2 else "mlx-community/Qwen3.5-9B-4bit"
    print(f"target: {base}  model: {model}")
    for c in (1, 4):
        run(base, model, c)
