#!/usr/bin/env python3
"""
mlx_sbom_collector.py
=====================
Hourly SBOM (Software Bill of Materials) and Drift Telemetry Collector
for the MLX cluster.

Scans:
  1. Homebrew formulae and casks (brew list --versions)
  2. Python virtual environments (pip packages, versions, exact paths, hashes)
  3. System binaries and developer utilities (git, curl, python3, zsh, etc.)
  4. MLX model directories and weight checkpoints (~/mlx-models, weights.npz)

Computes Drift:
  Compares local node inventory against baseline reference node ($base_device).
  Flags DEVIATED / IN_SYNC status, package-level version mismatches, and missing dependencies.

Exposes Prometheus metrics on HTTP (default 127.0.0.1:9108) for:
  - mac_sbom_package_info
  - mac_sbom_total_packages
  - mac_sbom_last_scan_timestamp_seconds
  - mac_sbom_drift_status
  - mac_sbom_drift_mismatch_count
  - mac_sbom_missing_package_count
  - mac_sbom_drift_diff
  - mac_sbom_collector_up

Stores historical JSON snapshots in logs/sbom_snapshot.json.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

from mlx_logging import debug, degraded, error, info, setup_logging

# ---------------------------------------------------------------------------
# Default Constants
# ---------------------------------------------------------------------------
DEFAULT_LISTEN = "127.0.0.1:9108"
DEFAULT_INTERVAL = 3600.0  # Hourly scan
DEFAULT_BASE_DEVICE = "mm"

SCRIPT_DIR = Path(__file__).resolve().parent
EDGE_DIR = SCRIPT_DIR.parent
LOG_DIR = EDGE_DIR / "logs"
CONFIG_DIR = EDGE_DIR / "config"

LOG_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Metric Registry & Isolated Gauges
# ---------------------------------------------------------------------------
REGISTRY = CollectorRegistry()

G_SBOM_UP = Gauge(
    "mac_sbom_collector_up",
    "1 if the SBOM inventory collector is running",
    ["device"],
    registry=REGISTRY,
)
G_SBOM_LAST_SCAN = Gauge(
    "mac_sbom_last_scan_timestamp_seconds",
    "Unix epoch timestamp of the most recent SBOM scan",
    ["device"],
    registry=REGISTRY,
)
G_SBOM_TOTAL_PACKAGES = Gauge(
    "mac_sbom_total_packages",
    "Total cataloged SBOM packages by ecosystem",
    ["device", "ecosystem"],
    registry=REGISTRY,
)
G_SBOM_PACKAGE_INFO = Gauge(
    "mac_sbom_package_info",
    "Detailed package metadata and installation path",
    ["device", "package", "version", "ecosystem", "path", "hash"],
    registry=REGISTRY,
)
G_SBOM_DRIFT_STATUS = Gauge(
    "mac_sbom_drift_status",
    "Cluster configuration drift status (IN_SYNC or DEVIATED)",
    ["device", "base", "status"],
    registry=REGISTRY,
)
G_SBOM_DRIFT_MISMATCH_COUNT = Gauge(
    "mac_sbom_drift_mismatch_count",
    "Count of package version mismatches compared to baseline device",
    ["device", "base"],
    registry=REGISTRY,
)
G_SBOM_MISSING_PACKAGE_COUNT = Gauge(
    "mac_sbom_missing_package_count",
    "Count of missing packages compared to baseline device",
    ["device", "base"],
    registry=REGISTRY,
)
G_SBOM_DRIFT_DIFF = Gauge(
    "mac_sbom_drift_diff",
    "Granular package delta against baseline device",
    ["base", "target", "package", "base_version", "target_version", "ecosystem", "path"],
    registry=REGISTRY,
)
G_SBOM_DEVICE_DRIFT_SUMMARY = Gauge(
    "mac_sbom_device_drift_summary",
    "Summary table of fleet device drift state against baseline",
    ["device", "base", "status"],
    registry=REGISTRY,
)
G_SBOM_DRIFT_DETAILS = Gauge(
    "mac_sbom_drift_details",
    "Granular package delta details against baseline",
    ["device", "base", "package", "base_version", "target_version", "ecosystem", "path"],
    registry=REGISTRY,
)
G_SBOM_LICENSE_COUNT = Gauge(
    "mac_sbom_license_count",
    "Total cataloged SBOM packages by license policy category",
    ["device", "category"],
    registry=REGISTRY,
)

_SCAN_LOCK = threading.Lock()
_CURRENT_INVENTORY: List[Dict[str, Any]] = []


def _compute_sha256(file_path: Path, max_bytes: int = 1048576) -> str:
    """Compute sha256 prefix for a file without reading massive binaries."""
    if not file_path.is_file():
        return "none"
    try:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            chunk = f.read(max_bytes)
            h.update(chunk)
        return h.hexdigest()[:16]
    except Exception:
        return "unreadable"


# ---------------------------------------------------------------------------
# Scanners: Homebrew
# ---------------------------------------------------------------------------
def _scan_homebrew() -> List[Dict[str, Any]]:
    """Scan Homebrew packages and casks with exact versions and paths."""
    items: List[Dict[str, Any]] = []
    brew_bin = shutil.which("brew")
    if not brew_bin:
        for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
            if os.path.isfile(p):
                brew_bin = p
                break
    if not brew_bin:
        return items

    # 1. Formulae
    try:
        res = subprocess.run([brew_bin, "list", "--versions"], capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2:
                    pkg = parts[0]
                    ver = parts[1]
                    cellar = Path(brew_bin).resolve().parent.parent / "Cellar" / pkg / ver
                    h = _compute_sha256(cellar / "INSTALL_RECEIPT.json") if cellar.exists() else "brew-pkg"
                    items.append({
                        "package": pkg,
                        "version": ver,
                        "ecosystem": "brew",
                        "path": str(cellar),
                        "hash": h,
                    })
    except Exception as exc:
        debug(f"brew formulae scan failed: {exc!r}", component="sbom")

    # 2. Casks
    try:
        res = subprocess.run([brew_bin, "list", "--cask", "--versions"], capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2:
                    pkg = parts[0]
                    ver = parts[1]
                    items.append({
                        "package": pkg,
                        "version": ver,
                        "ecosystem": "brew",
                        "path": f"/Applications/{pkg}.app",
                        "hash": "cask",
                    })
    except Exception as exc:
        debug(f"brew cask scan failed: {exc!r}", component="sbom")

    return items


# ---------------------------------------------------------------------------
# Scanners: Python pip / Virtual Environments
# ---------------------------------------------------------------------------
def _scan_pip_venvs() -> List[Dict[str, Any]]:
    """Scan Python packages across current interpreter and discovered virtualenvs."""
    items: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str]] = set()

    pythons = [sys.executable]
    for env_var in ("VENV_PYTHON", "MLX_VENV_PYTHON"):
        val = os.environ.get(env_var)
        if val and os.path.isfile(val) and val not in pythons:
            pythons.append(val)

    # Common search paths
    for cand in (
        Path.home() / ".venv" / "bin" / "python",
        Path.home() / "venvs" / "mlx" / "bin" / "python",
        Path.home() / "bench" / "venv-mon" / "bin" / "python",
    ):
        if cand.is_file() and str(cand) not in pythons:
            pythons.append(str(cand))

    for py in pythons:
        try:
            res = subprocess.run(
                [py, "-m", "pip", "list", "--format=json"],
                capture_output=True,
                text=True,
                timeout=12,
            )
            if res.returncode == 0:
                pkgs = json.loads(res.stdout)
                # Query site-packages path
                sp_res = subprocess.run(
                    [py, "-c", "import site; print(site.getsitepackages()[0] if site.getsitepackages() else '')"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                site_path = sp_res.stdout.strip() or os.path.dirname(py)

                for p in pkgs:
                    name = p.get("name", "")
                    ver = p.get("version", "")
                    if not name or not ver:
                        continue
                    key = (name.lower(), ver, site_path)
                    if key in seen:
                        continue
                    seen.add(key)

                    dist_info = Path(site_path) / f"{name.replace('-', '_')}-{ver}.dist-info"
                    record_file = dist_info / "RECORD"
                    h = _compute_sha256(record_file) if record_file.is_file() else "pypi"
                    lic = _read_pip_license(dist_info)

                    items.append({
                        "package": name,
                        "version": ver,
                        "ecosystem": "pip",
                        "path": site_path,
                        "hash": h,
                        "licenses": [lic] if lic else [],
                        "purl": f"pkg:pypi/{name.lower()}@{ver}",
                        "type": "library",
                    })
        except Exception as exc:
            debug(f"pip scan failed for {py}: {exc!r}", component="sbom")

    return items


# ---------------------------------------------------------------------------
# Scanners: System Binaries
# ---------------------------------------------------------------------------
def _scan_system_binaries() -> List[Dict[str, Any]]:
    """Scan critical system binaries, developer tools, and engines."""
    items: List[Dict[str, Any]] = []
    targets = [
        ("git", ["git", "--version"]),
        ("curl", ["curl", "--version"]),
        ("python3", ["python3", "--version"]),
        ("node", ["node", "--version"]),
        ("zsh", ["zsh", "--version"]),
        ("bash", ["bash", "--version"]),
        ("ssh", ["ssh", "-V"]),
        ("rsync", ["rsync", "--version"]),
        ("vmagent", ["vmagent", "--version"]),
        ("docker", ["docker", "--version"]),
        ("podman", ["podman", "--version"]),
    ]

    for tool, ver_cmd in targets:
        bin_path = shutil.which(tool)
        if not bin_path:
            continue
        ver = "unknown"
        try:
            res = subprocess.run(ver_cmd, capture_output=True, text=True, timeout=3)
            out = (res.stdout or res.stderr).splitlines()
            if out:
                first_line = out[0].strip()
                # Extract first semantic version like 1.2.3 or commit
                m = re.search(r"(\d+\.\d+(?:\.\d+)?)", first_line)
                ver = m.group(1) if m else first_line[:24]
        except Exception:
            pass

        items.append({
            "package": tool,
            "version": ver,
            "ecosystem": "system",
            "path": bin_path,
            "hash": _compute_sha256(Path(bin_path)),
        })

    return items


# ---------------------------------------------------------------------------
# Scanners: MLX Weights & Models
# ---------------------------------------------------------------------------
def _scan_mlx_models() -> List[Dict[str, Any]]:
    """Scan MLX model directories and weight checkpoints."""
    items: List[Dict[str, Any]] = []
    candidates = [
        Path.home() / "mlx-models",
        Path("/Users/dk/mlx-models"),
        Path.home() / ".cache" / "huggingface" / "hub",
    ]
    model_path_env = os.environ.get("MLX_MODEL_PATH")
    if model_path_env:
        candidates.insert(0, Path(model_path_env).expanduser())

    seen_dirs: set[str] = set()

    for base in candidates:
        if not base.exists():
            continue
        try:
            # If base itself has model weights
            if (base / "weights.npz").is_file() or (base / "config.json").is_file():
                dirs = [base]
            else:
                dirs = [d for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")]

            for d in dirs:
                if str(d) in seen_dirs:
                    continue
                seen_dirs.add(str(d))

                cfg_file = d / "config.json"
                weights_file = d / "weights.npz"
                if not weights_file.exists():
                    safetensors = list(d.glob("*.safetensors"))
                    if safetensors:
                        weights_file = safetensors[0]

                if not (cfg_file.exists() or weights_file.exists()):
                    continue

                h = _compute_sha256(cfg_file) if cfg_file.exists() else _compute_sha256(weights_file)
                # Model version: git rev or modification date
                ver = "4bit-mlx"
                try:
                    mtime = os.path.getmtime(d)
                    ver = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y.%m.%d")
                except Exception:
                    pass

                items.append({
                    "package": d.name,
                    "version": ver,
                    "ecosystem": "model",
                    "path": str(d),
                    "hash": h,
                })
        except Exception as exc:
            debug(f"mlx model scan failed for {base}: {exc!r}", component="sbom")

    return items


# ---------------------------------------------------------------------------
# Drift & Delta Engine
# ---------------------------------------------------------------------------
def _load_or_save_baseline_reference(
    node_name: str,
    base_device: str,
    current_items: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Load baseline reference inventory; if current node is base, update reference file."""
    ref_file = CONFIG_DIR / "sbom_base_reference.json"
    ref_map: Dict[str, Dict[str, Any]] = {}

    if node_name == base_device:
        # We are the baseline authority; write reference
        try:
            ref_data = {item["package"]: item for item in current_items}
            ref_file.write_text(json.dumps(ref_data, indent=2), encoding="utf8")
            return ref_data
        except Exception as exc:
            debug(f"failed to write base reference: {exc!r}", component="sbom")

    if ref_file.is_file():
        try:
            ref_data = json.loads(ref_file.read_text(encoding="utf8"))
            if isinstance(ref_data, dict):
                return ref_data
        except Exception as exc:
            debug(f"failed to read base reference: {exc!r}", component="sbom")

    # If no reference exists, build default baseline mapping from current items
    return {item["package"]: item for item in current_items}


def _evaluate_drift(
    node_name: str,
    base_device: str,
    current_items: List[Dict[str, Any]],
) -> Tuple[str, int, int, List[Dict[str, Any]]]:
    """Compare local items against base device reference to compute delta."""
    baseline = _load_or_save_baseline_reference(node_name, base_device, current_items)
    curr_map = {item["package"]: item for item in current_items}

    diffs: List[Dict[str, Any]] = []
    missing_count = 0
    mismatch_count = 0

    for pkg_name, base_item in baseline.items():
        if pkg_name not in curr_map:
            missing_count += 1
            diffs.append({
                "package": pkg_name,
                "base_version": base_item.get("version", "unknown"),
                "target_version": "MISSING",
                "ecosystem": base_item.get("ecosystem", "unknown"),
                "path": base_item.get("path", "none"),
            })
        else:
            curr_item = curr_map[pkg_name]
            if curr_item.get("version") != base_item.get("version"):
                mismatch_count += 1
                diffs.append({
                    "package": pkg_name,
                    "base_version": base_item.get("version", "unknown"),
                    "target_version": curr_item.get("version", "unknown"),
                    "ecosystem": curr_item.get("ecosystem", "unknown"),
                    "path": curr_item.get("path", "none"),
                })

    status = "DEVIATED" if (missing_count > 0 or mismatch_count > 0) else "IN_SYNC"
    return status, mismatch_count, missing_count, diffs


# ---------------------------------------------------------------------------
# Core Collection & Metrics Update
# ---------------------------------------------------------------------------
_COPYLEFT_LICENSES = frozenset({
    "GPL-2.0", "GPL-2.0-only", "GPL-2.0-or-later",
    "GPL-3.0", "GPL-3.0-only", "GPL-3.0-or-later",
    "LGPL-2.0", "LGPL-2.0-only", "LGPL-2.0-or-later",
    "LGPL-2.1", "LGPL-2.1-only", "LGPL-2.1-or-later",
    "LGPL-3.0", "LGPL-3.0-only", "LGPL-3.0-or-later",
    "AGPL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later",
    "EUPL-1.1", "EUPL-1.2", "OSL-3.0",
    "CC-BY-SA-4.0", "CC-BY-NC-SA-4.0",
})


def _classify_license(licenses: List[str]) -> str:
    if not licenses:
        return "unknown"
    for lic in licenses:
        upper = lic.upper()
        if lic in _COPYLEFT_LICENSES or "GPL" in upper or "AGPL" in upper:
            return "copyleft"
    for lic in licenses:
        upper = lic.upper()
        if any(tok in upper for tok in ("MIT", "APACHE", "BSD", "ISC", "PSF", "UNLICENSE", "0BSD")):
            return "permissive"
    return "other"


def _read_pip_license(dist_info: Path) -> str:
    """Extract License or License-Expression from wheel METADATA if present."""
    for fname in ("METADATA", "PKG-INFO"):
        fpath = dist_info / fname
        if not fpath.is_file():
            continue
        try:
            for line in fpath.read_text(encoding="utf8", errors="ignore").splitlines():
                if line == "":
                    break
                if line.startswith("License-Expression:"):
                    val = line.split(":", 1)[1].strip()
                    if val and val.upper() != "UNKNOWN":
                        return val
                elif line.startswith("License:"):
                    val = line.split(":", 1)[1].strip()
                    if val and val.upper() != "UNKNOWN":
                        return val[:120]
        except Exception:
            pass
    return ""


def _enrich_item_cyclonedx(item: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure purl, type, licenses, and license_category exist on every SBOM item."""
    eco = item.get("ecosystem") or "unknown"
    pkg = item.get("package") or ""
    ver = item.get("version") or ""
    if not item.get("purl"):
        if eco == "brew":
            item["purl"] = f"pkg:brew/{pkg}@{ver}" if ver else f"pkg:brew/{pkg}"
        elif eco == "pip":
            item["purl"] = f"pkg:pypi/{pkg.lower()}@{ver}" if ver else f"pkg:pypi/{pkg.lower()}"
        else:
            item["purl"] = f"pkg:generic/{pkg}@{ver}" if ver else f"pkg:generic/{pkg}"
    item.setdefault("type", "application" if eco in ("system", "model") else "library")
    lics = item.get("licenses") or []
    item["licenses"] = lics if isinstance(lics, list) else ([str(lics)] if lics else [])
    item["license_category"] = _classify_license(item["licenses"])
    return item


def _find_syft_binary() -> Optional[str]:
    for cand in (shutil.which("syft"), "/usr/local/bin/syft", "/opt/homebrew/bin/syft"):
        if cand and Path(cand).is_file():
            return cand
    return None


def _scan_syft(syft_bin: str) -> Optional[Dict[str, Any]]:
    """Run Syft CycloneDX scan when available (mirrors kri sbom_scan.sls)."""
    try:
        res = subprocess.run(
            [syft_bin, "packages", "--scope", "all-layers", "--output", "cyclonedx-json", "/"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout)
    except Exception as exc:
        debug(f"syft scan failed, falling back to native scanner: {exc!r}", component="sbom")
    return None


def collect_and_update_metrics(
    node_name: str,
    base_device: str,
    ingest_url: str = "",
    scanner_mode: str = "auto",
) -> None:
    """Run full SBOM scan, update Prometheus gauges, dump CycloneDX JSON, and push to central."""
    global _CURRENT_INVENTORY

    start_time = time.time()
    all_items: List[Dict[str, Any]] = []
    syft_bom: Optional[Dict[str, Any]] = None
    scanner_used = "native"

    if scanner_mode in ("auto", "syft"):
        syft_bin = _find_syft_binary()
        if syft_bin:
            syft_bom = _scan_syft(syft_bin)
            if syft_bom and isinstance(syft_bom.get("components"), list):
                scanner_used = "syft"
                for comp in syft_bom["components"]:
                    if not isinstance(comp, dict) or not comp.get("name"):
                        continue
                    lics: List[str] = []
                    for entry in comp.get("licenses") or []:
                        if isinstance(entry, dict):
                            lic_obj = entry.get("license") if isinstance(entry.get("license"), dict) else entry
                            lid = lic_obj.get("id") or lic_obj.get("name") or ""
                            if lid:
                                lics.append(str(lid))
                    purl = str(comp.get("purl") or "")
                    eco = (
                        purl.split(":", 2)[1].split("/", 1)[0]
                        if purl.startswith("pkg:") and "/" in purl
                        else "syft"
                    )
                    all_items.append({
                        "package": str(comp["name"]),
                        "version": str(comp.get("version") or "unknown"),
                        "ecosystem": eco,
                        "path": str(comp.get("purl") or "/"),
                        "hash": "syft",
                        "purl": purl,
                        "type": str(comp.get("type") or "library"),
                        "licenses": lics,
                    })

    if not all_items:
        # 1. Collect all ecosystems
        all_items.extend(_scan_pip_venvs())
        all_items.extend(_scan_homebrew())
        all_items.extend(_scan_system_binaries())
        all_items.extend(_scan_mlx_models())

    # Fallback sanity: if empty in minimal container/CI environment, populate base package
    if not all_items:
        all_items.append({
            "package": "python3",
            "version": sys.version.split()[0],
            "ecosystem": "system",
            "path": sys.executable,
            "hash": "ci-base",
        })

    all_items = [_enrich_item_cyclonedx(item) for item in all_items]
    _CURRENT_INVENTORY = all_items

    # 2. Reset gauges to prevent stale packages
    G_SBOM_PACKAGE_INFO.clear()
    G_SBOM_TOTAL_PACKAGES.clear()
    G_SBOM_DRIFT_STATUS.clear()
    G_SBOM_DRIFT_MISMATCH_COUNT.clear()
    G_SBOM_MISSING_PACKAGE_COUNT.clear()
    G_SBOM_DRIFT_DIFF.clear()
    G_SBOM_LICENSE_COUNT.clear()

    # 3. Populate package inventory gauges
    counts_by_eco: Dict[str, int] = {}
    counts_by_lic: Dict[str, int] = {"copyleft": 0, "permissive": 0, "other": 0, "unknown": 0}
    for item in all_items:
        eco = item["ecosystem"]
        counts_by_eco[eco] = counts_by_eco.get(eco, 0) + 1
        cat = item.get("license_category", "unknown")
        counts_by_lic[cat] = counts_by_lic.get(cat, 0) + 1

        G_SBOM_PACKAGE_INFO.labels(
            device=node_name,
            package=item["package"],
            version=item["version"],
            ecosystem=eco,
            path=item["path"],
            hash=item["hash"],
        ).set(1.0)

    for eco, count in counts_by_eco.items():
        G_SBOM_TOTAL_PACKAGES.labels(device=node_name, ecosystem=eco).set(count)
    for cat, count in counts_by_lic.items():
        G_SBOM_LICENSE_COUNT.labels(device=node_name, category=cat).set(count)

    # 4. Evaluate Drift against Base Device
    status, mismatch_cnt, missing_cnt, diffs = _evaluate_drift(node_name, base_device, all_items)

    G_SBOM_DRIFT_STATUS.labels(device=node_name, base=base_device, status=status).set(1.0)
    G_SBOM_DEVICE_DRIFT_SUMMARY.labels(device=node_name, base=base_device, status=status).set(1.0)
    G_SBOM_DRIFT_MISMATCH_COUNT.labels(device=node_name, base=base_device).set(mismatch_cnt)
    G_SBOM_MISSING_PACKAGE_COUNT.labels(device=node_name, base=base_device).set(missing_cnt)

    for d in diffs:
        G_SBOM_DRIFT_DIFF.labels(
            base=base_device,
            target=node_name,
            package=d["package"],
            base_version=d["base_version"],
            target_version=d["target_version"],
            ecosystem=d["ecosystem"],
            path=d["path"],
        ).set(1.0)
        G_SBOM_DRIFT_DETAILS.labels(
            device=node_name,
            base=base_device,
            package=d["package"],
            base_version=d["base_version"],
            target_version=d["target_version"],
            ecosystem=d["ecosystem"],
            path=d["path"],
        ).set(1.0)

    now = time.time()
    G_SBOM_UP.labels(device=node_name).set(1.0)
    G_SBOM_LAST_SCAN.labels(device=node_name).set(now)

    # 5. Persist CycloneDX-compatible JSON snapshot & push to central telemetry-ingest
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.fromtimestamp(now, tz=timezone.utc).astimezone(ist_tz).strftime("%d %b %Y, %H:%M:%S IST")
    cyclonedx_components = (
        syft_bom["components"]
        if syft_bom and isinstance(syft_bom.get("components"), list)
        else [
            {
                "type": item["type"],
                "name": item["package"],
                "version": item["version"],
                "purl": item["purl"],
                "licenses": [{"license": {"name": lic}} for lic in item.get("licenses", [])],
                "properties": [
                    {"name": "ecosystem", "value": item["ecosystem"]},
                    {"name": "path", "value": item["path"]},
                    {"name": "hash", "value": item["hash"]},
                ],
            }
            for item in all_items
        ]
    )
    snapshot = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "device": node_name,
        "macbook": node_name,
        "scanner": scanner_used,
        "scanned_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "scanned_at_ist": now_ist,
        "scanned_at_epoch": int(now),
        "total_packages": len(all_items),
        "ecosystem_counts": counts_by_eco,
        "license_counts": counts_by_lic,
        "drift_status": status,
        "base_device": base_device,
        "mismatches": mismatch_cnt,
        "missing": missing_cnt,
        "components": cyclonedx_components,
        "packages": all_items,
    }
    snapshot_path = LOG_DIR / "sbom_snapshot.json"
    try:
        snapshot_path.write_text(json.dumps(snapshot, indent=2), encoding="utf8")
    except Exception as exc:
        debug(f"failed to save sbom snapshot: {exc!r}", component="sbom")

    if ingest_url:
        target = f"{ingest_url.rstrip('/')}/api/v1/ingest/sbom/{node_name}"
        try:
            req = urllib.request.Request(
                target,
                data=json.dumps(snapshot).encode("utf8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                resp.read()
        except Exception as exc:
            debug(f"sbom ingest push failed ({target}): {exc!r}", component="sbom")

    duration = time.time() - start_time
    info(
        f"SBOM scan complete in {duration:.2f}s: {len(all_items)} packages cataloged "
        f"(status={status}, mismatches={mismatch_cnt}, missing={missing_cnt})",
        component="sbom",
    )


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------
class SbomHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/metrics", "/metrics/"):
            data = generate_latest(REGISTRY)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path in ("/snapshot", "/api/sbom"):
            snap_file = LOG_DIR / "sbom_snapshot.json"
            if snap_file.is_file():
                content = snap_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path in ("/health", "/healthz", "/"):
            resp = b'{"status":"healthy","service":"mlx-sbom-collector"}\n'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return


# ---------------------------------------------------------------------------
# Daemon Scan Loop
# ---------------------------------------------------------------------------
def _scan_loop(
    node_name: str,
    base_device: str,
    interval: float,
    stop_event: threading.Event,
    ingest_url: str = "",
    scanner_mode: str = "auto",
) -> None:
    while not stop_event.is_set():
        stop_event.wait(interval)
        if stop_event.is_set():
            break
        try:
            with _SCAN_LOCK:
                collect_and_update_metrics(
                    node_name,
                    base_device,
                    ingest_url=ingest_url,
                    scanner_mode=scanner_mode,
                )
        except Exception as exc:
            error(f"scheduled sbom scan error: {exc!r}", component="sbom")


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="MLX SBOM Fleet Inventory & Cross-Node Drift Telemetry Collector"
    )
    parser.add_argument(
        "--node-name",
        default=os.environ.get("MLX_DEVICE_ID") or os.environ.get("MLX_MACBOOK_ID") or socket.gethostname(),
        help="device / node identifier (default: MLX_DEVICE_ID or hostname)",
    )
    parser.add_argument(
        "--listen",
        default=os.environ.get("SBOM_LISTEN", DEFAULT_LISTEN),
        help=f"address to listen on (default: {DEFAULT_LISTEN})",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("SBOM_SCAN_INTERVAL", DEFAULT_INTERVAL)),
        help=f"hourly rescan interval in seconds (default: {DEFAULT_INTERVAL}s)",
    )
    parser.add_argument(
        "--base-device",
        default=os.environ.get("SBOM_BASE_DEVICE", DEFAULT_BASE_DEVICE),
        help=f"baseline reference device for drift comparison (default: {DEFAULT_BASE_DEVICE})",
    )
    parser.add_argument(
        "--ingest-url",
        default=os.environ.get("CENTRAL_TELEMETRY_INGEST", ""),
        help="central telemetry-ingest base URL (e.g. http://127.0.0.1:8088)",
    )
    parser.add_argument(
        "--scanner",
        choices=("auto", "syft", "native"),
        default=os.environ.get("SBOM_SCANNER", "auto"),
        help="SBOM scanner backend (default: auto)",
    )
    parser.add_argument(
        "--scan-once",
        action="store_true",
        help="execute a single synchronous scan, write snapshot, and exit",
    )

    args = parser.parse_args()

    setup_logging(service="mlx-sbom-collector", macbook=args.node_name)

    # Initial synchronous scan
    with _SCAN_LOCK:
        collect_and_update_metrics(
            args.node_name,
            args.base_device,
            ingest_url=args.ingest_url,
            scanner_mode=args.scanner,
        )

    if args.scan_once:
        print(f"SBOM scan complete. Snapshot written to {LOG_DIR / 'sbom_snapshot.json'}")
        return 0

    stop_event = threading.Event()
    loop_thread = threading.Thread(
        target=_scan_loop,
        args=(
            args.node_name,
            args.base_device,
            max(10.0, args.interval),
            stop_event,
            args.ingest_url,
            args.scanner,
        ),
        daemon=True,
    )
    loop_thread.start()

    host, port_str = args.listen.rsplit(":", 1) if ":" in args.listen else ("127.0.0.1", args.listen)
    port = int(port_str)

    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((host, port), SbomHandler)
    info(f"sbom collector listening on {host}:{port}", component="sbom")

    def _shutdown_handler(signum: int, frame: Any) -> None:
        info("received termination signal, stopping sbom collector...", component="sbom")
        stop_event.set()
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
