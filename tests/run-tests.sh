#!/usr/bin/env bash
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
python3 "$ROOT/tests/verify_release.py"
python3 "$ROOT/tests/setup_test.py"
python3 "$ROOT/tests/setup_review_test.py"
python3 "$ROOT/tests/installer_ux_test.py"
python3 "$ROOT/tests/team_pack_test.py"
python3 "$ROOT/tests/plugin_portability_test.py"
python3 "$ROOT/tests/installed_guard_test.py"
python3 "$ROOT/tests/gc_portability_test.py"
python3 "$ROOT/tests/leak_check_test.py"
python3 "$ROOT/tests/run_hooks.py"
