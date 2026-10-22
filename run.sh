#!/usr/bin/env bash
# Run thumbmux without Docker.  Needs: python3 (3.9+), tmux, ttyd.
set -euo pipefail
cd "$(dirname "$0")"
for bin in python3 tmux ttyd; do
  command -v "$bin" >/dev/null || { echo "missing dependency: $bin" >&2; exit 1; }
done
if [ -f .env ]; then set -a; . ./.env; set +a; fi
export THUMBMUX_TMUX_CONF="${THUMBMUX_TMUX_CONF:-$PWD/deploy/tmux.conf}"
exec python3 server/app.py
