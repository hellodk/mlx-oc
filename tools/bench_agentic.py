#!/usr/bin/env python3
"""
bench_agentic.py
================
Benchmark *agentic* workloads against the MLX cluster — not single prompts,
but multi-turn tool-calling loops like a coding agent or an operator would
generate.

A run executes a scenario as an OpenAI chat session with `tools` declared.
Each turn the model may emit `tool_calls`; the harness "executes" them with a
deterministic stub tool registry (calculator / weather / time), appends the
tool results, and continues until the model answers without tool calls or the
turn budget is exhausted.

Metrics recorded per run (and per turn):
  turns            number of model turns (prompt -> tool call -> ... -> answer)
  per-turn TTFT    time to first token of each model response
  wall             total time to complete the task
  tool_calls       total tool calls emitted
  valid_tool_calls calls that parsed into {name, arguments} JSON
  completion_tokens
  done             True if the model produced a final (non-tool) answer in budget

Why this matters for capacity: agentic traffic has long contexts (every turn
re-sends the whole transcript), bursts of tool calls, and KV-cache churn. A
single-prompt load test underestimates pressure by a lot.

Every request is also traced into Opik by the proxy automatically, so the same
run can be inspected per-turn/per-tool-call in the Opik UI.

Usage:
  python3 bench_agentic.py --base http://127.0.0.1:8080/v1 \
      --model mlx-community/Qwen3.5-4B-MLX-8bit \
      --scenario calculator --runs 5
  python3 bench_agentic.py --scenario planner --agents 3 --runs 10 \
      --label "agentic-sweep"
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

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "Evaluate a simple arithmetic expression (+, -, *, /).",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get the current time for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    },
]

SCENARIOS = {
    "calculator": {
        "task": (
            "Work through this step by step and use the calculator tool for "
            "each arithmetic step: ((17 * 43) - 129) / 2. Then double the "
            "result and report the final number."
        ),
        "tools": ["calculator"],
        "budget": 8,
    },
    "tri_tool": {
        "task": (
            "A user asks: 'What's the weather and time in Berlin, and what is "
            "231 * 57?' Use the available tools for each piece of information, "
            "then give one consolidated answer."
        ),
        "tools": ["get_weather", "get_time", "calculator"],
        "budget": 10,
    },
    "planner": {
        "task": (
            "Plan a 4-step rollout of a new self-hosted LLM feature on two Mac "
            "minis. For each step, use the calculator tool to estimate total "
            "effort in hours (assume 6h, 10h, 4h, 8h) and then present the "
            "final total."
        ),
        "tools": ["calculator"],
        "budget": 12,
    },
}


def exec_tool(name, arguments):
    """Deterministic stub 'execution' of a tool call."""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        return "ERROR: could not parse arguments"
    if name == "calculator":
        expr = str(args.get("expression", "")).strip()
        try:
            # simple evaluator: numbers + binary + - * /
            import re
            if re.fullmatch(r"[0-9+\-*/().\s]+", expr):
                val = eval(expr, {"__builtins__": {}}, {})
                return f"{expr} = {val}"
            return f"cannot evaluate {expr!r}"
        except Exception as exc:
            return f"calculator error: {exc}"
    if name == "get_weather":
        return f"weather in {args.get('city', '?')}: 14C, light rain"
    if name == "get_time":
        return f"time in {args.get('city', '?')}: 14:30 local"
    return f"unknown tool {name}"


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


def chat(base, model, messages, tools, max_tokens=256):
    """One streaming chat turn; returns content, tool_calls, usage, ttft."""
    body = json.dumps({
        "model": model, "messages": messages, "tools": tools,
        "stream": True, "max_tokens": max_tokens, "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Connection": "close", "X-Mlx-Trace": "1"},
    )
    t_send = time.monotonic()
    ttft, content, tool_calls, usage = None, "", [], None
    with urllib.request.urlopen(req, timeout=600) as resp:
        for obj in sse_parse_stream(resp):
            t = time.monotonic()
            if "usage" in obj:
                usage = obj["usage"]
            for ch in (obj.get("choices") or []):
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    if ttft is None:
                        ttft = t - t_send
                    content += delta["content"]
                for tc in delta.get("tool_calls") or []:
                    # incremental tool-call deltas: fold index/function parts
                    idx = tc.get("index", 0)
                    while len(tool_calls) <= idx:
                        tool_calls.append({"name": "", "arguments": ""})
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        tool_calls[idx]["name"] += fn["name"]
                    if fn.get("arguments"):
                        tool_calls[idx]["arguments"] += fn["arguments"]
    return {
        "ttft": ttft,
        "content": content,
        "tool_calls": [tc for tc in tool_calls if tc.get("name")],
        "usage": usage or {},
        "wall": time.monotonic() - t_send,
    }


def run_agent(base, model, scenario):
    task = SCENARIOS[scenario]["task"]
    tools = [t for t in TOOLS if t["function"]["name"] in SCENARIOS[scenario]["tools"]]
    budget = SCENARIOS[scenario]["budget"]

    messages = [{"role": "user", "content": task}]
    turns, calls, valid, done = 0, 0, 0, False
    per_turn_ttft, per_turn_wall = [], []
    t0 = time.monotonic()
    try:
        for _ in range(budget):
            turn = chat(base, model, messages, tools)
            turns += 1
            per_turn_ttft.append(turn["ttft"])
            per_turn_wall.append(turn["wall"])
            if turn["tool_calls"]:
                calls += len(turn["tool_calls"])
                for tc in turn["tool_calls"]:
                    try:
                        json.loads(tc["arguments"])
                        valid += 1
                    except json.JSONDecodeError:
                        pass
                    messages.append({
                        "role": "assistant",
                        "content": turn["content"],
                        "tool_calls": [{
                            "id": f"call_{turns}_{valid}",
                            "type": "function",
                            "function": {"name": tc["name"],
                                         "arguments": tc["arguments"]},
                        }],
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": f"call_{turns}_{valid}",
                        "content": exec_tool(tc["name"], tc["arguments"]),
                    })
            else:
                done = True
                break
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    ttfts = [x for x in per_turn_ttft if x is not None]
    walls = [x for x in per_turn_wall if x is not None]
    return {
        "ok": True, "scenario": scenario, "turns": turns,
        "tool_calls": calls, "valid_tool_calls": valid, "done": done,
        "wall": round(time.monotonic() - t0, 2),
        "ttft_mean": statistics.mean(ttfts) if ttfts else None,
        "ttft_max": max(ttfts) if ttfts else None,
        "turn_wall_mean": statistics.mean(walls) if walls else None,
        "per_turn_ttft": [round(x, 3) if x else None for x in per_turn_ttft],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8080/v1")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--scenario", default="calculator", choices=list(SCENARIOS))
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--agents", type=int, default=1,
                    help="concurrent agent loops (realistic agentic load)")
    ap.add_argument("--label", default="bench-agentic")
    args = ap.parse_args()

    print(f"bench_agentic start={datetime.now().isoformat(timespec='seconds')} "
          f"base={args.base} model={args.model} scenario={args.scenario} "
          f"agents={args.agents} runs={args.runs}")
    run_agent(args.base, args.model, "calculator")  # warm up

    results = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.agents) as ex:
        futures = [ex.submit(run_agent, args.base, args.model, args.scenario)
                   for _ in range(args.runs)]
        for f in futures:
            results.append(f.result())
    wall = time.monotonic() - t0

    ok = [r for r in results if r.get("ok")]
    print(f"\n--- {args.scenario}: agents={args.agents} runs={args.runs} "
          f"wall={wall:.1f}s ---")
    print(f"  completed={len(ok)}/{len(results)} "
          f"done_in_budget={sum(1 for r in ok if r['done'])}")
    if ok:
        turns = [r["turns"] for r in ok]
        calls = [r["tool_calls"] for r in ok]
        valid = [r["valid_tool_calls"] for r in ok]
        ttf = [r["ttft_mean"] for r in ok if r["ttft_mean"]]
        print(f"  turns mean={statistics.mean(turns):.1f} "
              f"tool_calls mean={statistics.mean(calls):.1f} "
              f"valid_rate={sum(valid)/sum(calls) if sum(calls) else 0:.2f}")
        print(f"  per-turn TTFT mean={statistics.mean(ttf):.3f}s "
              f"max={max(r['ttft_max'] for r in ok if r['ttft_max']):.3f}s")
        print(f"  run wall mean={statistics.mean(r['wall'] for r in ok):.2f}s")

    report = {
        "label": args.label, "model": args.model, "scenario": args.scenario,
        "agents": args.agents, "runs": args.runs,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "wall_s": round(wall, 1), "results": results,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    fname = REPORT_DIR / f"{args.label}-{datetime.now():%Y%m%d-%H%M%S}.json"
    fname.write_text(json.dumps(report, indent=2))
    print(f"report -> {fname}")


if __name__ == "__main__":
    main()
