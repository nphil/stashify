#!/usr/bin/env bash
# Stashify thin-coordinator entrypoint. GPU tools + models live in the compute
# runner container (see lada_entrypoint.sh) — nothing to provision here.
set -uo pipefail

case "${RUN_MODE:-server}" in
    worker) echo "[entrypoint] stashify: starting batch worker (tag-driven)"; exec python worker.py ;;
    *)      echo "[entrypoint] stashify: starting on-demand HTTP server"; exec python server.py ;;
esac
