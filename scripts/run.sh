#!/usr/bin/env bash
# Detects which machine this is running on, activates the matching
# conda environment, and launches the app with the right uvicorn flags.
#
# Usage: scripts/run.sh [extra uvicorn args...]
# Add a new machine by adding a case below.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

HOSTNAME="$(hostname)"
CONDA_ENV=""
UVICORN_ARGS=()

case "$HOSTNAME" in
  PC29UZ3)
    # WSL dev machine (Michal's laptop)
    CONDA_ENV="refbase"
    UVICORN_ARGS=(--host 127.0.0.1 --port 8000 --reload)
    ;;

  # refbase-vps)
  #   # Production VPS — fill in real hostname once provisioned.
  #   CONDA_ENV="refbase"
  #   UVICORN_ARGS=(--host 0.0.0.0 --port 8000)
  #   ;;

  *)
    if grep -qi microsoft /proc/version 2>/dev/null; then
      echo "warning: unrecognized hostname '$HOSTNAME' but this looks like WSL — falling back to the dev profile." >&2
      CONDA_ENV="refbase"
      UVICORN_ARGS=(--host 127.0.0.1 --port 8000 --reload)
    else
      echo "error: unrecognized machine '$HOSTNAME'. Add a case for it in $0" >&2
      exit 1
    fi
    ;;
esac

source ~/miniforge3/bin/activate "$CONDA_ENV"

exec uvicorn app.main:app "${UVICORN_ARGS[@]}" "$@"
