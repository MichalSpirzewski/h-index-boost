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
  DESKTOP-77E0NH2)
    # WSL dev machine (Michal's home PC). Same networking caveat as PC29UZ3:
    # binding 0.0.0.0 only reaches WSL's virtual network unless Windows is in
    # mirrored networking mode or a portproxy rule forwards the port.
    CONDA_ENV="refbase"
    UVICORN_ARGS=(--host 0.0.0.0 --port 8000 --reload)
    ;;

  PC29UZ3)
    # WSL dev machine (Michal's laptop). Binding 0.0.0.0 only reaches WSL's
    # own virtual network unless Windows is set to mirrored networking mode
    # (Settings > Network > networkingMode=mirrored in .wslconfig) or a
    # `netsh interface portproxy` rule forwards the port from the Windows host.
    CONDA_ENV="refbase"
    UVICORN_ARGS=(--host 0.0.0.0 --port 8000 --reload)
    ;;

  PC14UZ3)
    # Linux dev workstation (Michal's desktop) — on the LAN directly, so
    # 0.0.0.0 + an open firewall port is all that's needed.
    CONDA_ENV="refbase"
    UVICORN_ARGS=(--host 0.0.0.0 --port 8000 --reload)
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
      UVICORN_ARGS=(--host 0.0.0.0 --port 8000 --reload)
    else
      echo "error: unrecognized machine '$HOSTNAME'. Add a case for it in $0" >&2
      exit 1
    fi
    ;;
esac

# Locate the conda/mamba distribution: miniforge3 first, then miniconda3.
if [[ -f ~/miniforge3/bin/activate ]]; then
  CONDA_BASE=~/miniforge3
elif [[ -f ~/miniconda3/bin/activate ]]; then
  CONDA_BASE=~/miniconda3
else
  echo "error: no miniforge3 or miniconda3 installation found in \$HOME." >&2
  exit 1
fi

source "$CONDA_BASE/bin/activate" "$CONDA_ENV"

if command -v hostname >/dev/null 2>&1; then
  LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [[ -n "${LAN_IP:-}" ]]; then
    echo "info: reachable on the LAN at http://$LAN_IP:8000 (make sure the firewall allows the port)" >&2
  fi
fi

exec uvicorn app.main:app "${UVICORN_ARGS[@]}" "$@"
