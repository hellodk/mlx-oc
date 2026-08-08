#!/bin/zsh
# Generate vm-scrape.yml from vm-scrape.tmpl.yml.
# Usage:  ./setup.sh [MLX_IP [RANK1_IP]]
# Defaults: MLX_IP=192.168.1.64 (Mac mini A / rank 0), RANK1_IP=10.0.0.2 (Mac mini B, ring on Ethernet)
DIR="$(cd "$(dirname "$0")" && pwd)"
MLX_IP="${1:-${MLX_IP:-192.168.1.64}}"
RANK1_IP="${2:-${RANK1_IP:-10.0.0.2}}"

sed -e "s/__MLX_IP__/$MLX_IP/g" -e "s/__RANK1_IP__/$RANK1_IP/g" \
  "$DIR/vm-scrape.tmpl.yml" > "$DIR/vm-scrape.yml"

echo "wrote $DIR/vm-scrape.yml (rank0=$MLX_IP rank1=$RANK1_IP)"
echo "next:  cd $DIR && podman compose up -d"
