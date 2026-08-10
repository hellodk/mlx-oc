#!/bin/zsh
# Keep the Salt file-root assets in sync with observability/ (the single source
# of truth). Run this after editing observability/grafana or observability/vmalert:
#   infra/salt/sync-files.sh
# Salt file roots must be self-contained, so these files are duplicated on
# purpose — the state file.managed/file.recurse serves them from files/.
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"

rm -rf "$DIR/observability/files/grafana"
mkdir -p "$DIR/observability/files"
cp -R "$REPO/observability/grafana" "$DIR/observability/files/grafana"
mkdir -p "$DIR/observability/files/vmalert"
cp "$REPO/observability/vmalert/rules.yml" "$DIR/observability/files/vmalert/rules.yml"

echo "synced salt file roots from $REPO/observability"
