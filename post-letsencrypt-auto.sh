#! /bin/bash
set -e
PROJECT_ENV="${HOME}/.local/share/letsencrypt/"
ENV_PIP="${PROJECT_ENV}/bin/pip"

$ENV_PIP uninstall -y letsencrypt
$ENV_PIP install .
