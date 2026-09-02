#!/usr/bin/env bash
set -Eeuo pipefail

# Dataset is a bind mount, so image-time ownership does not apply to it.
# Fix the mounted directory before dropping privileges to appuser.
mkdir -p /app/dataset /app/dataset/logs
chown -R appuser:appuser /app/dataset

exec gosu appuser "$@"
