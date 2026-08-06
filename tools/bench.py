#!/usr/bin/env python3
"""
bench.py
========
Benchmark the MLX cluster through its OpenAI-compatible proxy.

Measures real streaming TTFT (time to first content token), generation
time, completion tokens, tokens/sec and end-to-end latency, then a small
concurrency burst. Output is a human-readable table plus JSON.

Usage:
  python3 bench.py --base http://127.0.0.1:8080/v1 \
      --model mlx-community/Qwen3-1.7B-4bit \
      --max-tokens 128 --iters 3 --label "2-node ring"
  python3 bench.py --concurrency 4 --max-tokens 256 --iters 5 --label "4-way burst"
"""

import argparse
import json
import os
import statistics
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

MODEL = os.environ.get("MLX_MODEL", "mlx-community/Qwen3.5-4B-MLX-8bit")

SHORT = "Reply in two sentences: what is a distributed inference cluster?"
MEDIUM = (
    "Write a short memo for a team of backend engineers. Explain the trade-offs "
    "between tensor-parallel and pipeline-parallel model sharding across two "
    "Mac minis connected by Gigabit Ethernet, covering memory footprint, "
    "collective-communication overhead, and practical latency. Keep it concise."
)
LONG = (
    "Write a technical overview of running self-hosted LLM inference on Apple "
    "Silicon. Cover the MLX framework, 4-bit weight quantization with Qwen3, "
    "distributed tensor parallelism over a ring interconnect, OpenAI-compatible "
    "serving, and observability with Prometheus and OpenTelemetry. Discuss "
    "TTFT and tokens-per-second as the key latency and throughput metrics, "
    "compare single-node versus two-node configurations, and mention "
    "hallucination-risk heuristics, hardware telemetry, and how an agentic "
    "coding tool can drive the whole system through a single HTTP endpoint. "
    "Aim for a well-structured answer with clear sections."
)


def sse_parse_stream(resp):
    """Yield parsed SSE data objects; mirror mlx_metrics_proxy._StreamParser."""
    # readline(): SSE events are newline-delimited, so each line arrives as
    # soon as it is produced (read(65536) would block until EOF).
    for line in iter(resp.readline, b""):
        line = line.decode("utf-8", "replace").rstrip("\r\n")
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def run_one(base, model, prompt, max_tokens, concurrency=0):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }).encode()

    t_send = time.monotonic()
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Connection": "close"},
    )
    ttft = None
    first_chunk_at = None
    last_chunk_at = None
    content_parts = []
    usage = None
    with urllib.request.urlopen(req, timeout=600) as resp:
        for obj in sse_parse_stream(resp):
            t = time.monotonic()
            if first_chunk_at is None:
                first_chunk_at = t
            last_chunk_at = t
            if "usage" in obj:
                usage = obj["usage"]
            choices = obj.get("choices") or []
            delta = (choices[0].get("delta") or {}) if choices else {}
            if delta.get("content"):
                if ttft is None:
                    ttft = time.monotonic() - t_send
                content_parts.append(delta["content"])

    content = "".join(content_parts)
    t_done = time.monotonic()
    gen_time = t_done - (t_send + (ttft or 0.0))
    comp_tokens = (usage or {}).get("completion_tokens") or len(content.split())
    return {
        "ttft": ttft,
        "gen_time": gen_time,
        "total": t_done - t_send,
        "comp_tokens": comp_tokens,
        "content_chars": len(content),
        "prompt_tokens": (usage or {}).get("prompt_tokens") or max(1, len(prompt) // 3),
        "tps": comp_tokens / gen_time if comp_tokens and gen_time > 0 else None,
    }


def bench_scenario(args, prompt, label):
    print(f"\n--- {label}  (max_tokens={args.max_tokens}, iters={args.iters}) ---")
    results = [run_one(args.base, args.model, prompt, args.max_tokens) for _ in range(args.iters)]

    def avg(key, fmt="{:.3f}"):
        vals = [r[key] for r in results if r.get(key) is not None]
        return (fmt.format(statistics.mean(vals)) if vals else "n/a", statistics.pstdev(vals) if len(vals) > 1 else 0)

    t, s = avg("ttft")
    tot, s2 = avg("total")
    tps, s3 = avg("tps", "{:.1f}")
    comp = statistics.mean(r["comp_tokens"] for r in results)
    prompt_t = statistics.mean(r["prompt_tokens"] for r in results)
    print(f"  prompt_tokens~{int(prompt_t):>4}  TTFT {t}s (+/-{s:.3f})  "
          f"total {tot}s (+/-{s2:.3f})  tok/s {tps}  comp_tokens~{comp:.0f}")

    for r in results:
        r["_label"] = label
    return results


def bench_concurrency(args):
    print(f"\n--- concurrency={args.concurrency} x iters={args.iters} (max_tokens={args.max_tokens}) ---")
    results = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = [
            ex.submit(run_one, args.base, args.model, MEDIUM, args.max_tokens)
            for _ in range(args.concurrency * args.iters)
        ]
        for f in futures:
            results.append(f.result())
    wall = time.monotonic() - t0
    latencies = [r["total"] for r in results]
    total_tokens = sum(r["comp_tokens"] for r in results)
    print(f"  requests={len(results)}  wall={wall:.2f}s  "
          f"mean_latency={statistics.mean(latencies):.2f}s  "
          f"p95={sorted(latencies)[int(0.95*len(latencies))-1]:.2f}s  "
          f"throughput={total_tokens/wall:.1f} tok/s")
    for r in results:
        r["_label"] = f"conc{args.concurrency}"
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8080/v1")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--iters", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=0,
                    help="if >0, run a concurrency burst instead of the prompt matrix")
    ap.add_argument("--label", default="bench", help="tag recorded under _label")
    ap.add_argument("--json", action="store_true", help="also emit JSON summary")
    args = ap.parse_args()

    print(f"bench start={datetime.now().isoformat(timespec='seconds')} "
          f"base={args.base} model={args.model} label={args.label}")
    print("warming up...")
    run_one(args.base, args.model, "warmup", 4)

    all_results = []
    if args.concurrency:
        all_results += bench_concurrency(args)
    else:
        for prompt, name in ((SHORT, "short"), (MEDIUM, "medium"), (LONG, "long")):
            all_results += bench_scenario(args, prompt, f"{args.label} :: {name}")

    if args.json:
        summary = {
            "label": args.label,
            "base": args.base,
            "max_tokens": args.max_tokens,
            "concurrency": args.concurrency,
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
        }
        print("\n--- json ---")
        print(json.dumps({"summary": summary, "results": all_results}, indent=2))


if __name__ == "__main__":
    main()
