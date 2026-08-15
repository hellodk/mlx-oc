#!/bin/zsh
# Stop the distributed MLX stack on the serving node and every ring peer.
# Thin wrapper around the Ansible lifecycle playbook (--tags stop): the kill /
# cleanup / verification is driven entirely by cluster/mlx-stack.json, so
# anything Ansible started is stopped, cleaned up and verified here.
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$DIR")"
ANSIBLE="$(command -v ansible-playbook || echo /opt/homebrew/bin/ansible-playbook)"
exec "$ANSIBLE" -i "$REPO/infra/ansible/inventories/generated/hosts.yml" "$REPO/infra/ansible/cluster.yml" --tags stop
