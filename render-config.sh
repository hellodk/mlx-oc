#!/bin/zsh
# Generate opencode.json from opencode.json.tmpl using cluster/cluster.env's
# MLX_MODEL and MLX_PROXY_URL. Re-run this whenever either changes.
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/cluster/cluster.env"

sed "s|__MLX_MODEL__|$MLX_MODEL|g; s|__MLX_PROXY_URL__|$MLX_PROXY_URL|g" "$DIR/opencode.json.tmpl" > "$DIR/opencode.json"

echo "wrote $DIR/opencode.json (model=$MLX_MODEL, proxy=$MLX_PROXY_URL)"
