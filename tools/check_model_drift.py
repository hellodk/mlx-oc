#!/usr/bin/env python3
"""check_model_drift.py - fail if any MLX_MODEL fallback default has drifted
from cluster/cluster.env, the single source of truth.

Every script that reads os.environ.get("MLX_MODEL", "<literal>") is allowed
its own last-known-good literal fallback (for standalone invocation without
cluster.env sourced first) - but that literal must always equal whatever
cluster.env currently says, or it's a stale value waiting to cause the exact
class of bug this check exists to catch (see suggestions.md / blog post
history: metric-drift bugs from stale hardcoded config).

This intentionally does NOT scan docstrings/prose/READMEs - those are
allowed to show an arbitrary example model id. It only checks the actual
os.environ.get("MLX_MODEL", ...) fallback-default call sites, plus the
cluster.env line itself.

Usage:
  python3 tools/check_model_drift.py
Exit 0 and silent-ish on success; exit 1 with a diff-style report on drift.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLUSTER_ENV = REPO / "cluster" / "cluster.env"

# os.environ.get("MLX_MODEL", "<literal>") - matches whether it's the direct
# default= value or nested inside another os.environ.get(...) fallback chain
# (e.g. opik_evaluator.py's JUDGE_MODEL -> MLX_MODEL -> literal chain).
FALLBACK_RE = re.compile(r'os\.environ\.get\(\s*["\']MLX_MODEL["\']\s*,\s*["\']([^"\']+)["\']\s*\)')

SCAN_DIRS = ["cluster", "tools"]
SCAN_SUFFIXES = {".py"}


def canonical_model():
    text = CLUSTER_ENV.read_text(encoding="utf-8")
    m = re.search(r"^MLX_MODEL=(.+)$", text, re.MULTILINE)
    if not m:
        print(f"ERROR: {CLUSTER_ENV} has no MLX_MODEL= line", file=sys.stderr)
        sys.exit(2)
    return m.group(1).strip()


def find_fallbacks():
    hits = []
    self_path = Path(__file__).resolve()
    for d in SCAN_DIRS:
        for path in sorted((REPO / d).rglob("*")):
            if path.suffix not in SCAN_SUFFIXES or not path.is_file():
                continue
            if path.resolve() == self_path:
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for m in FALLBACK_RE.finditer(line):
                    hits.append((path.relative_to(REPO), lineno, m.group(1)))
    return hits


def main():
    canonical = canonical_model()
    hits = find_fallbacks()
    drifted = [(p, ln, val) for p, ln, val in hits if val != canonical]

    print(f"cluster/cluster.env MLX_MODEL = {canonical}")
    print(f"checked {len(hits)} os.environ.get(\"MLX_MODEL\", ...) fallback site(s)")

    if drifted:
        print("\nDRIFT DETECTED - these fallbacks no longer match cluster.env:", file=sys.stderr)
        for p, ln, val in drifted:
            print(f"  {p}:{ln}  has {val!r}", file=sys.stderr)
        print(f"\nExpected: {canonical!r}", file=sys.stderr)
        sys.exit(1)

    print("no drift")


if __name__ == "__main__":
    main()
