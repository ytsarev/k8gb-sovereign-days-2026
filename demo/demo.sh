#!/usr/bin/env bash
# Enter-driven presentation, with an optional tmux traffic pane.
set -euo pipefail
DEMO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${DEMO_DIR}/.runtime/kubeconfig" && -z "${KUBECONFIG:-}" ]]; then
  export KUBECONFIG="${DEMO_DIR}/.runtime/kubeconfig"
fi
ENTRYPOINT=presenter.py
case "${1:-}" in
  up|setup|rehearse) ENTRYPOINT=bootstrap.py ;;
  offline)
    ENTRYPOINT=offline.py
    shift
    ;;
  replay)
    ENTRYPOINT=dashboard.py
    shift
    if [[ "${1:-}" != "--help" && "${1:-}" != "-h" ]]; then
      set -- --replay "$@"
    fi
    ;;
esac
if [[ -x "${DEMO_DIR}/.venv/bin/python" ]]; then
  exec "${DEMO_DIR}/.venv/bin/python" -u "${DEMO_DIR}/${ENTRYPOINT}" "$@"
fi
exec python3 -u "${DEMO_DIR}/${ENTRYPOINT}" "$@"
