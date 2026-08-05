#!/usr/bin/env python3
"""Snapshot a cluster's normalized state for later drift comparison.

Writes a JSON file that can be passed to server.py as --cluster-a/--cluster-b
(or both). This is how you compare against a cluster that is not currently
reachable: snapshot it once, then run the server against the file.

Secret values are stored as SHA-256 digests only — the snapshot never
contains plaintext secrets.
"""

import argparse
import datetime
import json
import os

import drift


def main():
    parser = argparse.ArgumentParser(description="Snapshot cluster state for drift comparison")
    parser.add_argument("--context", default=None,
                        help="kubectl context (default: current context)")
    parser.add_argument("--kubeconfig", default=None)
    parser.add_argument("--out", required=True, help="output JSON path")
    args = parser.parse_args()

    ctx = args.context or drift.current_context(args.kubeconfig)
    state = drift.fetch_cluster(ctx, args.kubeconfig)["state"]
    doc = {
        "kind": "DriftSnapshot",
        "source": ctx,
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "state": state,
    }
    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    total = sum(len(v) for v in state.values())
    print(f"snapshot of '{ctx}' written to {out} ({total} resources)")


if __name__ == "__main__":
    main()
