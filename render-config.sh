#!/bin/zsh
# Generate opencode.json from opencode.json.tmpl using cluster/cluster.env's
# MLX_MODEL. Re-run this whenever the model in cluster.env changes.
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/cluster/cluster.env"

sed "s|__MLX_MODEL__|$MLX_MODEL|g" "$DIR/opencode.json.tmpl" > "$DIR/opencode.json"

echo "wrote $DIR/opencode.json (model=$MLX_MODEL)"
