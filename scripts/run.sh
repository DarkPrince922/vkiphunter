#!/usr/bin/env bash
# Запуск бота с кредами из OpenRC-файла VK Cloud.
#   ./scripts/run.sh /root/mcs6434280924-openrc.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ $# -ge 1 ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$1"
    set +a
fi

exec .venv/bin/python -m vkiphunter
