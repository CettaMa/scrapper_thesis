#!/usr/bin/env bash
set -Eeuo pipefail

# Dataset is a bind mount, so image-time ownership does not apply to it.
# Fix the mounted directory before dropping privileges to appuser.
mkdir -p /app/dataset
chown appuser:appuser /app/dataset

# gosu rebuilds the supplementary group list from /etc/group, so the GIDs Compose
# adds via group_add do not survive the privilege drop. Grant appuser whichever
# groups actually own the render nodes on this host, rather than baking one host's
# GID into the image.
if [[ -d /dev/dri ]]; then
    for node in /dev/dri/*; do
        [[ -c "$node" ]] || continue
        node_gid="$(stat -c '%g' "$node")"
        if ! getent group "$node_gid" >/dev/null; then
            groupadd -g "$node_gid" "dri$node_gid"
        fi
        usermod -aG "$(getent group "$node_gid" | cut -d: -f1)" appuser
    done
fi

exec gosu appuser "$@"
