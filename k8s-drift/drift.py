#!/usr/bin/env python3
"""Drift detection between two Kubernetes clusters.

Compares Deployments, ServiceAccounts, ConfigMaps and Secrets across two
cluster sources. A source is either a kubectl context name or a snapshot
JSON file (see snapshot.py). Fetching is one `kubectl get -A -o json` call
per source. Comparison normalizes out volatile registry fields
(managedFields, resourceVersion, uid, generation, status,
last-applied-configuration) and diffs matching names field-by-field.

Secret values are compared by SHA-256 digest, so real values are never
required to detect drift. A live cluster fetched with reveal=True keeps
decoded values in a side table so the web UI can display them on demand;
snapshot files store digests only.
"""

import base64
import hashlib
import json
import os
import subprocess

KINDS = ("deployments", "serviceaccounts", "configmaps", "secrets")

KIND_LABEL = {
    "deployments": "Deployments",
    "serviceaccounts": "ServiceAccounts",
    "configmaps": "ConfigMaps",
    "secrets": "Secrets",
}

_KIND_KEY = {
    "deployment": "deployments",
    "serviceaccount": "serviceaccounts",
    "configmap": "configmaps",
    "secret": "secrets",
}

VOLATILE = frozenset({
    "managedFields", "resourceVersion", "uid", "generation",
    "creationTimestamp", "status", "observedGeneration",
})

DROP_ANNOTATIONS = frozenset({
    "kubectl.kubernetes.io/last-applied-configuration",
    "deployment.kubernetes.io/revision",
})


class DriftError(Exception):
    pass


def current_context(kubeconfig=None):
    cmd = ["kubectl", "config", "current-context"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except subprocess.SubprocessError:
        return "default"
    return out.strip() or "default"


def fetch_cluster(source, kubeconfig=None, timeout=60, reveal=False):
    """Return {"state": <normalized>, "revealed": {...}} for one source."""
    if os.path.isfile(source):
        with open(source, encoding="utf-8") as fh:
            doc = json.load(fh)
        return {"state": doc.get("state", doc), "revealed": {}}
    cmd = ["kubectl", "--context", source, "get",
           ",".join(KINDS), "-A", "-o", "json"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DriftError(f"kubectl timed out after {timeout}s for '{source}'") from exc
    if proc.returncode != 0:
        raise DriftError(f"kubectl failed for '{source}': "
                         f"{proc.stderr.strip()[:500]}")
    return build_state(json.loads(proc.stdout).get("items", []), reveal)


def build_state(items, reveal=False):
    state = {kind: {} for kind in KINDS}
    revealed = {}
    for item in items:
        key_kind = _KIND_KEY.get((item.get("kind") or "").lower())
        if not key_kind:
            continue
        meta = item.get("metadata") or {}
        ns = meta.get("namespace", "")
        name = meta.get("name", "")
        if not name:
            continue
        state[key_kind][f"{ns}/{name}"] = _normalize_item(
            key_kind, item, reveal, revealed)
    return {"state": state, "revealed": revealed}


def _normalize_item(kind, item, reveal, revealed):
    obj = _clean(item)
    if kind == "secrets":
        data = {}
        for key, val in (obj.get("data") or {}).items():
            raw = _b64decode(val)
            if reveal:
                revealed.setdefault((obj.get("metadata", {}).get("namespace"),
                                     obj.get("metadata", {}).get("name")),
                                    {})[key] = raw
            data[key] = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()[:16]
        obj["data"] = data
    elif kind == "configmaps":
        data = dict(obj.get("data") or {})
        for key, val in (obj.get("binaryData") or {}).items():
            data[key] = _b64decode(val)
        obj["data"] = data
        obj.pop("binaryData", None)
    return obj


def _b64decode(val):
    try:
        return base64.b64decode(val.encode()).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return str(val)


def _clean(obj):
    if isinstance(obj, dict):
        out = {}
        for key, val in obj.items():
            if key in VOLATILE:
                continue
            if key == "annotations" and isinstance(val, dict):
                val = {k: v for k, v in val.items()
                       if k not in DROP_ANNOTATIONS}
                if not val:
                    continue
            out[key] = _clean(val)
        return out
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def compare(state_a, state_b):
    report = {"kinds": {}, "counts": {}}
    for kind in KINDS:
        rows = {}
        keys = sorted(set(state_a[kind]) | set(state_b[kind]))
        counts = {"ok": 0, "diff": 0, "only_a": 0, "only_b": 0, "total": len(keys)}
        for key in keys:
            ns, name = key.split("/", 1)
            in_a, in_b = key in state_a[kind], key in state_b[kind]
            if in_a and not in_b:
                status, diff = "ONLY_A", []
                counts["only_a"] += 1
            elif in_b and not in_a:
                status, diff = "ONLY_B", []
                counts["only_b"] += 1
            elif state_a[kind][key] == state_b[kind][key]:
                status, diff = "OK", []
                counts["ok"] += 1
            else:
                status = "DIFF"
                diff = field_diff(state_a[kind][key], state_b[kind][key])
                counts["diff"] += 1
            rows[key] = {
                "namespace": ns,
                "name": name,
                "status": status,
                "diff": [{"path": p, "a": _plain(a), "b": _plain(b)}
                         for p, a, b in diff],
                "a": state_a[kind].get(key),
                "b": state_b[kind].get(key),
            }
        report["kinds"][kind] = rows
        report["counts"][kind] = counts
    return report


def _plain(val):
    if isinstance(val, (dict, list)):
        return json.dumps(val, sort_keys=True, ensure_ascii=False)
    return val


def field_diff(a, b, path="", out=None):
    if out is None:
        out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            child = f"{path}.{key}" if path else key
            if key not in a:
                out.append((child, None, b[key]))
            elif key not in b:
                out.append((child, a[key], None))
            else:
                field_diff(a[key], b[key], child, out)
    elif isinstance(a, list) and isinstance(b, list):
        a, b = _sort_named(a, b)
        for i in range(max(len(a), len(b))):
            ai = a[i] if i < len(a) else None
            bi = b[i] if i < len(b) else None
            if i >= len(a) or i >= len(b):
                out.append((f"{path}[{i}]", ai, bi))
            else:
                field_diff(ai, bi, f"{path}[{i}]", out)
    else:
        if a != b:
            out.append((path, a, b))
    return out


def _sort_named(a, b):
    if (a and b and all(isinstance(x, dict) for x in a + b)
            and all("name" in x for x in a + b)):
        key = lambda x: x.get("name", "")
        return sorted(a, key=key), sorted(b, key=key)
    return a, b


def namespaces(state_a, state_b):
    ns = set()
    for kind in KINDS:
        for key in state_a[kind]:
            ns.add(key.split("/", 1)[0])
        for key in state_b[kind]:
            ns.add(key.split("/", 1)[0])
    return sorted(n for n in ns if n)
