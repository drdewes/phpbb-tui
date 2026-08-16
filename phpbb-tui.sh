#!/bin/sh
# Starter für phpbb-tui – üblicherweise als ~/.local/bin/phpbb-tui verlinkt.
ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m phpbb_tui "$@"
