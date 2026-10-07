#!/usr/bin/env bash
# Run the release suites: concurrently by default, each in its own HOME, TMPDIR and work root,
# queued behind any other run on this machine. --serial, --jobs N, --only <suite>, --list; see --help.
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

# Run first, one at a time, and stop on a failure: a broken manifest or package build fails fast.
first=(
  tests/verify_release.py
)
# One suite per line, run concurrently. hooks/tests is one suite per hook-test file in it.
suites=(
  tests/setup_test.py
  tests/setup_review_test.py
  tests/installer_ux_test.py
  tests/team_pack_test.py
  tests/dev_install_test.py
  tests/dashboard_test.py
  tests/plugin_portability_test.py
  tests/installed_guard_test.py
  tests/gc_portability_test.py
  tests/leak_check_test.py
  tests/run_suites_test.py
  hooks/tests
)

args=()
for suite in "${first[@]}"; do args+=(--first "$suite"); done
for suite in "${suites[@]}"; do args+=(--suite "$suite"); done
python3 "$ROOT/tests/run_suites.py" "${args[@]}" "$@"
