#!/bin/bash
set -e

XDG_DATA_HOME=${XDG_DATA_HOME:-~/.local/share}
VENV_NAME="letsencrypt"
VENV_PATH=${VENV_PATH:-"$XDG_DATA_HOME/$VENV_NAME"}
VENV_BIN="$VENV_PATH/bin"
LE_PYTHON="python2.7"

command -v $LE_PYTHON >/dev/null 2>&1 || { echo "I require $LE_PYTHON but it's not installed.  Aborting." >&2; exit 1; }

virtualenv --no-site-packages --python "$LE_PYTHON" "$VENV_PATH"

