#!/usr/bin/env python3
"""
Unit tests for the token-confidence layer in cluster/mlx_metrics_proxy.py.

These are pure-math and pure-parsing tests - no network, no upstream server.
The point is that a malformed or missing logprobs payload can never break a
request, and that the numbers mean what the metric help text says they mean.

Run:
  pytest tests/test_confidence.py -q
  python3 -m pytest tests/test_confidence.py -q
"""

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cluster"))

import mlx_metrics_proxy as p  # noqa: E402


def lp(prob):
    """logprob for a probability."""
    return math.log(prob)


def entry(prob, top=None):
    """One logprobs.content[] entry."""
    e = {"token": "x", "logprob": lp(prob)}
    if top is not None:
        e["top_logprobs"] = [{"token": f"t{i}", "logprob": lp(q)}
                             for i, q in enumerate(top)]
    return e


def payload(entries):
    return {"content": entries}


# -- perplexity ------------------------------------------------------------
def test_perplexity_of_certain_tokens_is_one():
    assert p.perplexity([1.0, 1.0, 1.0]) == 1.0


def test_perplexity_of_uniform_half_is_two():
    assert abs(p.perplexity([0.5, 0.5, 0.5]) - 2.0) < 1e-9


def test_perplexity_of_empty_is_zero():
    # 0 == "not scored", distinct from 1.0 == "perfectly confident"
    assert p.perplexity([]) == 0.0


def test_perplexity_rises_with_uncertainty():
    assert p.perplexity([0.9, 0.9]) < p.perplexity([0.4, 0.4])


# -- entropy ---------------------------------------------------------------
def test_entropy_of_one_hot_is_zero():
    entries = [entry(1.0, top=[1.0])]
    assert p.mean_token_entropy(entries) < 1e-9


def test_entropy_of_fifty_fifty_is_ln2():
    entries = [entry(0.5, top=[0.5, 0.5])]
    assert abs(p.mean_token_entropy(entries) - math.log(2)) < 1e-9


def test_entropy_renormalises_truncated_distribution():
    # top-k only covers 0.6 of the mass; renormalised it is still 50/50,
    # so entropy must be ln 2, not something smaller.
    entries = [entry(0.3, top=[0.3, 0.3])]
    assert abs(p.mean_token_entropy(entries) - math.log(2)) < 1e-9


def test_entropy_without_top_logprobs_is_zero():
    assert p.mean_token_entropy([entry(0.5)]) == 0.0


# -- margin ----------------------------------------------------------------
def test_margin_of_dominant_token():
    entries = [entry(0.9, top=[0.9, 0.05])]
    assert abs(p.mean_top2_margin(entries) - 0.85) < 1e-9


def test_margin_of_coin_flip_is_zero():
    entries = [entry(0.5, top=[0.5, 0.5])]
    assert p.mean_top2_margin(entries) == 0.0


def test_margin_needs_two_candidates():
    assert p.mean_top2_margin([entry(1.0, top=[1.0])]) == 0.0


# -- confidence stats ------------------------------------------------------
def test_confidence_stats_mean_std_min():
    mean, std, lowest = p.confidence_stats([0.2, 0.8])
    assert abs(mean - 0.5) < 1e-9
    assert abs(std - 0.42426406871) < 1e-6   # sample std of [0.2, 0.8]
    assert lowest == 0.2


def test_confidence_stats_single_token_has_no_spread():
    mean, std, lowest = p.confidence_stats([0.7])
    assert (mean, std, lowest) == (0.7, 0.0, 0.7)


# -- score_confidence ------------------------------------------------------
def test_score_confidence_full_summary():
    obj = payload([
        entry(0.9, top=[0.9, 0.05]),
        entry(0.3, top=[0.3, 0.3]),
    ])
    out = p.score_confidence(obj, threshold=0.5)
    assert out["tokens"] == 2
    assert out["confidence_min"] == 0.3
    assert abs(out["low_confidence_ratio"] - 0.5) < 1e-9
    assert 1.0 < out["perplexity"] < 3.0
    assert 0.0 < out["entropy"] < math.log(2) + 1e-9


def test_low_confidence_ratio_respects_threshold():
    obj = payload([entry(0.9), entry(0.4), entry(0.2)])
    assert abs(p.score_confidence(obj, 0.5)["low_confidence_ratio"] - 2 / 3) < 1e-9
    assert p.score_confidence(obj, 0.1)["low_confidence_ratio"] == 0.0


def test_score_confidence_returns_empty_when_nothing_is_scorable():
    # Anything the upstream could plausibly hand back, plus things it could not.
    for junk in (None, {}, [], "logprobs", 42,
                 {"content": None},
                 {"content": []},
                 {"content": [{}]},
                 {"content": [{"logprob": "not-a-number"}]}):
        assert p.score_confidence(junk, 0.5) == {}, junk


def test_score_confidence_degrades_when_only_top_logprobs_are_broken():
    # A usable `logprob` with an unusable `top_logprobs` still yields
    # confidence and perplexity; only the top-k derived numbers go to zero.
    for broken in ("nope", [], [{}], [{"logprob": "x"}]):
        out = p.score_confidence(
            {"content": [{"logprob": -0.5, "top_logprobs": broken}]}, 0.5
        )
        assert out["tokens"] == 1, broken
        assert abs(out["confidence_mean"] - math.exp(-0.5)) < 1e-9, broken
        assert out["entropy"] == 0.0, broken
        assert out["margin_mean"] == 0.0, broken


def test_logprob_tokens_skips_entries_without_logprob():
    obj = {"content": [{"token": "a"}, {"token": "b", "logprob": -0.1}]}
    assert len(p.logprob_tokens(obj)) == 1


# -- response rewriting ----------------------------------------------------
def _completion(content="hello", tool_calls=None, finish="stop"):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "test-model",
        "system_fingerprint": "test-fp",
        "choices": [{
            "index": 0,
            "finish_reason": finish,
            "message": msg,
            "logprobs": payload([entry(0.9, top=[0.9, 0.05])]),
        }],
    }


def test_choice_logprobs_extracts_then_strip_removes():
    obj = _completion()
    got = p.choice_logprobs(obj)
    assert got is not None and len(got["content"]) == 1
    assert p.strip_logprobs(obj) is True
    assert "logprobs" not in obj["choices"][0]
    # the extracted object survives the strip - scoring happens after
    assert len(got["content"]) == 1


def test_strip_logprobs_is_a_noop_when_absent():
    obj = {"choices": [{"message": {"content": "hi"}}]}
    assert p.strip_logprobs(obj) is False


def test_choice_logprobs_tolerates_malformed_responses():
    for junk in (None, {}, {"choices": []}, {"choices": "x"}, {"choices": [None]}):
        assert p.choice_logprobs(junk) is None


# -- SSE synthesis round-trip ---------------------------------------------
def _parse_frames(frames):
    parser = p._StreamParser()
    parser.feed(b"".join(frames).decode("utf-8"))
    return parser


def test_sse_frames_round_trip_content():
    frames = p.sse_frames(_completion(content="hello world"), "fallback")
    parser = _parse_frames(frames)
    assert parser.content == "hello world"
    assert parser.finish_reason == "stop"
    assert frames[-1] == b"data: [DONE]\n\n"


def test_sse_frames_round_trip_tool_calls():
    calls = [
        {"id": "call_1", "type": "function",
         "function": {"name": "read_file", "arguments": "{}"}},
        {"id": "call_2", "type": "function",
         "function": {"name": "list_dir", "arguments": "{}"}},
    ]
    frames = p.sse_frames(
        _completion(content="", tool_calls=calls, finish="tool_calls"), "fallback"
    )
    parser = _parse_frames(frames)
    assert parser.tool_calls == 2
    assert parser.finish_reason == "tool_calls"
    assert parser.tools == [
        {"type": "function", "name": "read_file"},
        {"type": "function", "name": "list_dir"},
    ]


def test_stream_parser_reassembles_split_tool_names():
    parser = p._StreamParser()
    parser.feed(
        (b"data: " + json.dumps({
            "choices": [{"delta": {"tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read", "arguments": ""}},
            ]}}],
        }).encode() + b"\n\n").decode("utf-8")
    )
    parser.feed(
        (b"data: " + json.dumps({
            "choices": [{"delta": {"tool_calls": [
                {"id": "c1", "function": {"name": "_file", "arguments": ""}},
            ]}}],
        }).encode() + b"\n\n").decode("utf-8")
    )
    parser.feed(
        (b"data: " + json.dumps({
            "choices": [{"delta": {"tool_calls": [
                {"id": "c1", "function": {"name": "", "arguments": "{}"}},
            ]}}],
        }).encode() + b"\n\n").decode("utf-8")
    )
    assert parser.tool_calls == 1
    assert parser.tools == [{"type": "function", "name": "read_file"}]


def test_stream_parser_falls_back_when_name_missing():
    parser = p._StreamParser()
    parser.feed(
        (b"data: " + json.dumps({
            "choices": [{"delta": {"tool_calls": [{"id": "c1"}]}}],
        }).encode() + b"\n\n").decode("utf-8")
    )
    assert parser.tools == [{"type": "unknown", "name": "unknown"}]


def test_sse_frames_index_tool_calls_for_streaming_clients():
    calls = [{"id": "call_1", "type": "function",
              "function": {"name": "f", "arguments": "{}"}}]
    frames = p.sse_frames(_completion(tool_calls=calls), "fallback")
    deltas = [json.loads(f[6:])["choices"][0]["delta"]
              for f in frames if f != b"data: [DONE]\n\n"]
    tc = [d for d in deltas if "tool_calls" in d][0]["tool_calls"]
    assert tc[0]["index"] == 0
    assert tc[0]["id"] == "call_1"


def test_sse_frames_preserve_identity_fields():
    frames = p.sse_frames(_completion(), "fallback")
    obj = json.loads(frames[0][6:])
    assert obj["id"] == "chatcmpl-1"
    assert obj["model"] == "test-model"
    assert obj["system_fingerprint"] == "test-fp"
    assert obj["object"] == "chat.completion.chunk"


def test_sse_frames_fall_back_to_the_proxy_model_label():
    obj = {"choices": [{"finish_reason": "stop",
                        "message": {"content": "hi"}}]}
    frames = p.sse_frames(obj, "fallback-model")
    assert json.loads(frames[0][6:])["model"] == "fallback-model"


# -- trace metadata --------------------------------------------------------
def test_conf_metadata_is_empty_when_unscored():
    assert p._conf_metadata({}, "injected") == {}


def test_conf_metadata_is_json_serialisable():
    conf = p.score_confidence(payload([entry(0.9, top=[0.9, 0.05])]), 0.5)
    meta = p._conf_metadata(conf, "destreamed")
    assert meta["logprobs_source"] == "destreamed"
    assert meta["scored_tokens"] == 1
    json.dumps(meta)   # must not raise - it goes into an OTel attribute
