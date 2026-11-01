#!/usr/bin/env bash
# build_and_push.sh
# =================
# Build and push the quay.io/hellodk/hydra central image.
#
# The edge bundle is native Python (no container), so only the central
# observability stack image is built here.
#
# Usage:
#   ./build_and_push.sh                    # build & push central image
#   ./build_and_push.sh --dry-run          # build locally without pushing
#
# Requires docker (or podman) and quay login for pushing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CENTRAL_DIR="${BUNDLE_DIR}/central"

REGISTRY="quay.io/hellodk"
REPO="hydra"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

ENGINE="docker"
if command -v podman >/dev/null 2>&1 && ! command -v docker >/dev/null 2>&1; then
  ENGINE="podman"
fi

info() { printf '[build] %s\n' "$*" >&2; }
err()  { printf '[build][ERR] %s\n' "$*" >&2; }

login() {
  if [[ "${DRY_RUN}" == "1" ]]; then
    info "dry-run: skipping quay login"
    return 0
  fi
  if [[ -n "${QUAY_USERNAME:-}" && -n "${QUAY_PASSWORD:-}" ]]; then
    printf '%s' "${QUAY_PASSWORD}" | "${ENGINE}" login quay.io -u "${QUAY_USERNAME}" --password-stdin
    return
  fi
  if [[ -n "${QUAY_TOKEN:-}" ]]; then
    printf '%s' "${QUAY_TOKEN}" | "${ENGINE}" login quay.io -u "${QUAY_USERNAME:-hellodk}" --password-stdin
    return
  fi
  info "no QUAY credentials found; attempting existing quay login"
  "${ENGINE}" login quay.io || true
}

full="${REGISTRY}/${REPO}:central"
info "building ${full} from ${CENTRAL_DIR}/Dockerfile.central"
"${ENGINE}" build --tag "${full}" \
  --file "${CENTRAL_DIR}/Dockerfile.central" \
  "${CENTRAL_DIR}"

if [[ "${DRY_RUN}" == "1" ]]; then
  info "dry-run: built ${full} (not pushed)"
  exit 0
fi

info "pushing ${full}"
"${ENGINE}" push "${full}"
info "pushed ${full}"
