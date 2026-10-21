#!/usr/bin/env bash
# Enter-driven presentation, with an optional tmux traffic pane.
set -euo pipefail
DEMO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "${DEMO_DIR}/.venv/bin/python" ]]; then
  exec "${DEMO_DIR}/.venv/bin/python" -u "${DEMO_DIR}/presenter.py" "$@"
fi
exec python3 -u "${DEMO_DIR}/presenter.py" "$@"
