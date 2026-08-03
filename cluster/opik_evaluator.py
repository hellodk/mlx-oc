#!/usr/bin/env python3
"""
opik_evaluator.py
=================
Online evaluation / feedback loop for Opik using the MLX cluster as the judge.

Every pass:
  1. reads recent traces in the Opik "mlx" project,
  2. for each trace that has parseable input/output and no judge scores yet,
     asks the live MLX cluster (an OpenAI-compatible /v1/chat/completions
     endpoint) to rate the answer with three 0-1 scores,
  3. writes those scores back to Opik as feedback scores via the batch REST
     endpoint (PUT /v1/private/traces/feedback-scores).

This closes the loop the other direction: the same cluster that produced an
answer also scores it, and the scores show up next to the heuristic scores the
proxy logs in-band (hallucination_quality / hallucination_flagged).

Usage:
  opik_evaluator.py --once
  opik_evaluator.py --opik-base http://192.168.1.10:32173 --judge-url http://127.0.0.1:8080/v1
  OPIK_BASE=... JUDGE_URL=... opik_evaluator.py --interval 15
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

JUDGE_SCORE_NAMES = ("correctness", "helpfulness", "hallucination_free")

JUDGE_PROMPT = (
    "You are an evaluator. Score the assistant's answer to the user's question "
    "using three numbers between 0 and 1.\n\n"
    "Question: {question}\n\n"
    "Assistant answer: {answer}\n\n"
    "Return ONLY a JSON object with exactly these keys:\n"
    '- "correctness": how factually correct and complete the answer is\n'
    '- "helpfulness": how well the answer addresses the request\n'
    '- "hallucination_free": 1.0 if the answer is fully grounded in the '
    'question and invents nothing, 0.0 if it fabricates\n'
    'Example: {{"correctness": 0.9, "helpfulness": 0.8, "hallucination_free": 1.0}}'
)


def get(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def put(url, payload, timeout=15):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def post_chat(url, model, prompt, timeout=120):
    req = urllib.request.Request(
        url,
        data=json.dumps(
            {
                "model": model,
                "temperature": 0.0,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Mlx-Trace": "0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        obj = json.load(r)
    return (obj.get("choices") or [{}])[0].get("message", {}).get("content", "")


def parse_scores(text):
    """Extract a JSON object of 0-1 scores from a (possibly noisy) LLM reply."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    scores = {}
    for name in JUDGE_SCORE_NAMES:
        raw = data.get(name)
        if raw is None:
            return None
        try:
            scores[name] = max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError):
            return None
    return scores


def trace_question_answer(trace):
    """Return (question, answer) from a trace, or (None, None)."""
    inp = trace.get("input")
    out = trace.get("output")
    if not isinstance(inp, dict) or not isinstance(out, dict):
        return None, None
    messages = inp.get("messages")
    if not isinstance(messages, list) or not messages:
        return None, None
    question = ""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user" and msg.get("content"):
            question = str(msg["content"])
            break
    answer = out.get("content") or ""
    return (question.strip(), str(answer).strip())


def already_scored(trace):
    fs = trace.get("feedback_scores") or []
    names = {s.get("name") for s in fs}
    return all(n in names for n in JUDGE_SCORE_NAMES)


def evaluate_once(opik_base, project, judge_url, model, limit, dry_run):
    api = opik_base.rstrip("/") + "/api/v1/private"
    traces = get(
        f"{api}/traces?project_name={urllib.parse.quote(project)}&page=1&size={limit}"
    ).get("content", [])

    pending = []
    for t in traces:
        q, a = trace_question_answer(t)
        if q and a and not already_scored(t):
            pending.append((t, q, a))

    print(f"[evaluator] {len(traces)} traces, {len(pending)} pending judge scores",
          flush=True)
    if not pending:
        return 0

    scores_batch = []
    for t, q, a in pending[:limit]:
        prompt = JUDGE_PROMPT.format(question=q, answer=a)
        try:
            reply = post_chat(judge_url.rstrip("/") + "/chat/completions", model, prompt)
        except Exception as exc:
            print(f"[evaluator] judge call failed for {t['id'][:12]} ({exc!r})",
                  flush=True)
            continue
        scores = parse_scores(reply)
        if not scores:
            print(f"[evaluator] unparseable judge reply for {t['id'][:12]}: "
                  f"{reply[:80]!r}", flush=True)
            continue
        reason = "LLM-as-judge via the MLX cluster (0..1, higher is better)"
        for name, value in scores.items():
            scores_batch.append({
                "id": t["id"],
                "name": name,
                "value": value,
                "category_name": "judge",
                "reason": reason,
                "project_name": project,
                "source": "sdk",
            })
        print(f"[evaluator] scored {t['id'][:12]} -> "
              f"{json.dumps({k: round(v, 3) for k, v in scores.items()})}",
              flush=True)

    if scores_batch and not dry_run:
        put(f"{api}/traces/feedback-scores", {"scores": scores_batch})
        print(f"[evaluator] wrote {len(scores_batch)} feedback scores", flush=True)
    return len(pending)


def main():
    ap = argparse.ArgumentParser(description="Opik evaluation feedback loop")
    ap.add_argument(
        "--opik-base",
        default=os.environ.get("OPIK_BASE", "http://192.168.1.10:32173"),
        help="Opik frontend base URL (NodePort)",
    )
    ap.add_argument("--project", default="mlx")
    ap.add_argument(
        "--judge-url",
        default=os.environ.get("JUDGE_URL", "http://127.0.0.1:8080/v1"),
        help="OpenAI-compatible endpoint used as the judge (the MLX proxy)",
    )
    ap.add_argument(
        "--model",
        default=os.environ.get("JUDGE_MODEL", "mlx-community/Qwen3-1.7B-4bit"),
    )
    ap.add_argument("--interval", type=int, default=10, help="seconds between passes")
    ap.add_argument("--limit", type=int, default=20, help="traces to scan per pass")
    ap.add_argument("--once", action="store_true", help="run a single pass and exit")
    ap.add_argument("--dry-run", action="store_true", help="score but do not write")
    args = ap.parse_args()

    while True:
        try:
            evaluate_once(
                args.opik_base, args.project, args.judge_url, args.model,
                args.limit, args.dry_run,
            )
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            print(f"[evaluator] opik/judge unreachable ({exc!r})", flush=True)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
