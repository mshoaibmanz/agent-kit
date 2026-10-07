#!/usr/bin/env bash
# Run the release suites (tests/run_suites.py lists them): concurrently by default, each in its own
# HOME, TMPDIR and work root, queued behind any other run on this machine. --serial, --jobs N,
# --only <suite>, --list; see --help.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
if [ -z "${TMPDIR:-}" ]; then
  echo "Set TMPDIR to a fixture directory in your bound task or project, outside this source checkout." >&2
  exit 2
fi
TMPDIR=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$TMPDIR")
case "$TMPDIR/" in
  "$ROOT/"*)
    echo "TMPDIR must be outside this source checkout; use your bound task or project fixture directory." >&2
    exit 2 ;;
esac
mkdir -p "$TMPDIR"
export TMPDIR
exec python3 "$ROOT/tests/run_suites.py" "$@"
