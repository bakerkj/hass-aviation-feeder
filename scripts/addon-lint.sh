#!/usr/bin/env bash
set -euo pipefail

docker build -q -t ha-addon-linter \
  https://github.com/frenck/action-addon-linter.git#main:src >/dev/null

rc=0
for f in "$@"; do
  d=$(dirname "${f}")
  docker run --rm \
    -e INPUT_PATH=/data \
    -e INPUT_COMMUNITY=false \
    -v "$(pwd)/${d}:/data" \
    ha-addon-linter || rc=1
done
exit "${rc}"
