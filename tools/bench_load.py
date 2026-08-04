#!/usr/bin/env python3
"""
bench_load.py
=============
Load-test the MLX cluster through its OpenAI-compatible proxy (:8080).

Design intent: a *traffic* benchmark for serving health, complementing Opik
(per-trace quality/agent inspection) and VictoriaMetrics (time-series SLOs).
It drives real streaming requests at controlled concurrency, measures TTFT /
tokens-per-sec / latency percentiles / throughput / error rate, and — while
load runs — snapshots the cluster's own metrics (proxy :8080, hw :9102, kv
:9104, supervisor :9105) so a benchmark run is correlated with GPU memory,
KV-cache pressure and serving status.

Modes
-----
  --sweep        concurrency ramp: 1,2,4,8,16 (each for --duration)
  --sustain N    sustained load at N concurrent workers for --duration
  --profile      single-request profile matrix (short/medium/long, stream on/off)

Recommended workflow:
  python3 bench_load.py --base http://127.0.0.1:8080/v1 \
      --model mlx-community/Qwen3.5-4B-MLX-8bit --sweep --duration 30 \
      --label "qwen3.5-4b-8bit sweep"
  python3 bench_load.py --sustain 8 --duration 120 --label "soak-8w-2m"

Output: human-readable table to stdout + JSON report under tools/bench-reports/
(one file per run, tagged with --label). Every request is also traced into Opik
by the proxy automatically (X-Mlx-Trace: 1 by default), so the same run can be
inspected per-request in the Opik UI.
"""

import argparse
import json
import statistics
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

MODEL = "mlx-community/Qwen3.5-4B-MLX-8bit"
REPORT_DIR = Path(__file__).resolve().parent / "bench-reports"

PROMPTS = {
    "short": "Reply in two sentences: what is a distributed inference cluster?",
    "medium": (
        "Write a short memo for a team of backend engineers. Explain the "
        "trade-offs between tensor-parallel and pipeline-parallel model "
        "sharding across two Mac minis connected by Gigabit Ethernet, covering "
        "memory footprint, collective-communication overhead, and practical "
        "latency. Keep it concise."
    ),
    "long": (
        "Write a technical overview of running self-hosted LLM inference on "
        "Apple Silicon. Cover MLX, 4-bit/8-bit quantization with Qwen3, "
        "distributed tensor parallelism over a ring interconnect, "
        "OpenAI-compatible serving, and observability with Prometheus and "
        "OpenTelemetry. Discuss TTFT and tokens-per-second as key metrics, "
        "compare single-node versus two-node, and mention hallucination-risk "
        "heuristics and hardware telemetry. Aim for a well-structured answer "
        "with clear sections."
    ),
}

_TOTAL = 0
_TOTAL_OK = 0
_ERRORS = 0
_LOCK = threading.Lock()


def sse_parse_stream(resp):
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


def run_one(base, model, prompt, max_tokens, stream=True, timeout=600):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json",
                 "Connection": "close", "X-Mlx-Trace": "1"},
    )
    t_send = time.monotonic()
    ttft, last_chunk, first_chunk, content, usage = None, None, None, "", None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for obj in sse_parse_stream(resp):
                t = time.monotonic()
                if first_chunk is None:
                    first_chunk = t
                last_chunk = t
                if "usage" in obj:
                    usage = obj["usage"]
                for ch in (obj.get("choices") or []):
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        if ttft is None:
                            ttft = t - t_send
                        content += delta["content"]
        t_done = time.monotonic()
        comp_tokens = (usage or {}).get("completion_tokens") or len(content.split())
        gen_time = max(1e-9, t_done - (t_send + (ttft or 0)))
        global _TOTAL, _TOTAL_OK
        with _LOCK:
            _TOTAL += 1
            _TOTAL_OK += 1
        return {
            "ok": True, "ttft": ttft, "total": t_done - t_send,
            "gen_time": gen_time, "comp_tokens": comp_tokens,
            "tps": comp_tokens / gen_time, "content_chars": len(content),
            "prompt_tokens": (usage or {}).get("prompt_tokens")
                             or max(1, len(prompt) // 3),
        }
    except Exception as exc:
        global _ERRORS
        with _LOCK:
            _TOTAL += 1
            _ERRORS += 1
        return {"ok": False, "error": type(exc).__name__, "total": time.monotonic() - t_send}


def percentile(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(p / 100 * len(s)))]


def summarize(results, label):
    ok = [r for r in results if r.get("ok")]
    ttfts = [r["ttft"] for r in ok if r.get("ttft") is not None]
    tots = [r["total"] for r in ok]
    tps = [r["tps"] for r in ok]
    n = len(results)
    errs = n - len(ok)
    wall = max((r["total"] for r in results), default=0)
    total_tokens = sum(r["comp_tokens"] for r in ok)
    row = {
        "label": label, "requests": n, "errors": errs,
        "error_rate": round(errs / n, 4) if n else None,
        "ttft_p50": percentile(ttfts, 50), "ttft_p95": percentile(ttfts, 95),
        "ttft_p99": percentile(ttfts, 99),
        "total_p50": percentile(tots, 50), "total_p95": percentile(tots, 95),
        "tps_p50": percentile(tps, 50), "tps_p95": percentile(tps, 95),
        "throughput_tok_s": round(total_tokens / wall, 1) if wall else None,
        "wall_s": round(wall, 1),
    }
    print(f"  {label}: n={n} err={errs} "
          f"TTFT p50/p95/p99={row['ttft_p50']}/{row['ttft_p95']}/{row['ttft_p99']}s "
          f"total p95={row['total_p95']}s tps p50/p95={row['tps_p50']}/{row['tps_p95']} "
          f"tok/s={row['throughput_tok_s']}")
    return row


def snapshot_cluster():
    """Pull current cluster self-metrics during load, best effort."""
    out = {}
    probes = {
        "proxy_up": ("http://127.0.0.1:8080/metrics", "mlx_up", None),
        "gpu_mem": ("http://127.0.0.1:9102/metrics", "mlx_hw_gpu_mem_alloc_bytes", None),
        "kv_util": ("http://127.0.0.1:9104/metrics", "mlx_kv_cache_utilization", None),
        "server_state": ("http://127.0.0.1:9105/metrics", "mlx_server_state", None),
    }
    import re
    for key, (url, metric, _) in probes.items():
        try:
            text = urllib.request.urlopen(url, timeout=3).read().decode()
            m = re.search(rf"^{re.escape(metric)}\{{[^}}]*\}} (\S+)", text, re.M)
            out[key] = float(m.group(1)) if m else None
        except Exception:
            out[key] = None
    return out


def run_sweep(args):
    levels = [1, 2, 4, 8, 16]
    rows = []
    for c in levels:
        print(f"\n--- sweep concurrency={c} duration={args.duration}s ---")
        stop = threading.Event()
        results = []

        def worker():
            while not stop.is_set():
                results.append(run_one(args.base, args.model,
                                       PROMPTS[args.prompt], args.max_tokens))
        pool = ThreadPoolExecutor(max_workers=c)
        for _ in range(c):
            pool.submit(worker)
        time.sleep(args.duration)
        stop.set()
        pool.shutdown(wait=True)
        rows.append(summarize(results, f"conc{c}"))
        time.sleep(args.settle)
    return rows


def run_sustain(args):
    print(f"\n--- sustain concurrency={args.concurrency} duration={args.duration}s ---")
    stop = threading.Event()
    results = []
    snapshots = []

    def worker():
        while not stop.is_set():
            results.append(run_one(args.base, args.model,
                                   PROMPTS[args.prompt], args.max_tokens))
    pool = ThreadPoolExecutor(max_workers=args.concurrency)
    for _ in range(args.concurrency):
        pool.submit(worker)
    t0 = time.monotonic()
    while time.monotonic() - t0 < args.duration:
        snapshots.append(snapshot_cluster())
        time.sleep(args.snapshot_interval)
    stop.set()
    pool.shutdown(wait=True)
    rows = [summarize(results, f"sustain{args.concurrency}")]
    return rows, snapshots


def run_profile(args):
    print("\n--- profile matrix (stream on/off x short/medium/long) ---")
    rows = []
    for stream in (True, False):
        for name, prompt in PROMPTS.items():
            results = [run_one(args.base, args.model, prompt, args.max_tokens,
                               stream=stream) for _ in range(args.iters)]
            rows.append(summarize(results, f"{'stream' if stream else 'sync'}::{name}"))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8080/v1")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--prompt", default="medium", choices=list(PROMPTS))
    ap.add_argument("--max-tokens", type=int, default=192)
    ap.add_argument("--iters", type=int, default=3)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--settle", type=float, default=5,
                    help="pause between sweep levels (GPU/KV settle)")
    ap.add_argument("--snapshot-interval", type=float, default=5)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--label", default="bench-load")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--sustain", type=int, default=0,
                    help="sustained load at this concurrency")
    ap.add_argument("--profile", action="store_true")
    args = ap.parse_args()

    print(f"bench_load start={datetime.now().isoformat(timespec='seconds')} "
          f"base={args.base} model={args.model} label={args.label}")
    print("warming up...")
    run_one(args.base, args.model, "warmup", 4)

    report = {"label": args.label, "model": args.model, "base": args.base,
              "recorded_at": datetime.now().isoformat(timespec="seconds"),
              "runs": [], "snapshots": []}
    if args.sweep:
        report["runs"] = run_sweep(args)
    elif args.sustain:
        rows, snaps = run_sustain(args)
        report["runs"] = rows
        report["snapshots"] = snaps
    else:
        report["runs"] = run_profile(args)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    fname = REPORT_DIR / f"{args.label}-{datetime.now():%Y%m%d-%H%M%S}.json"
    fname.write_text(json.dumps(report, indent=2))
    print(f"\nreport -> {fname}")


if __name__ == "__main__":
    main()
