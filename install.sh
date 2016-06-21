#!/bin/bash
set -e

XDG_DATA_HOME=${XDG_DATA_HOME:-~/.local/share}
VENV_NAME="letsencrypt"
VENV_PATH=${VENV_PATH:-"$XDG_DATA_HOME/$VENV_NAME"}
VENV_BIN="$VENV_PATH/bin"
VENV_PIP="$VENV_BIN/pip"
LE_PYTHON="python2.7"

command -v $LE_PYTHON >/dev/null 2>&1 || { echo "I require $LE_PYTHON but it's not installed.  Aborting." >&2; exit 1; }

virtualenv --no-site-packages --python "$LE_PYTHON" "$VENV_PATH"

if [ -d "letsencrypt" ]; then
    :
else
    echo "please run ./install.sh in the repo root dir"
fi

if [ -f "install.sh" ]; then
    :
else
    echo "please run ./install.sh in the repo root dir"
fi

$VENV_PIP install .
echo "Done."
