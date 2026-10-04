#!/usr/bin/env bash
# Run every suite in this directory: hooktest.sh, review-gates.sh and each *.test.sh / *.test.py.
# One line per suite with its exit code; the full output of a failing suite follows. Exits 1 when
# any suite fails.
#   bash ~/.agents/hooks/tests/run-all.sh [-v]     (-v prints every suite's output)
set -u
here=$(cd "$(dirname "$0")" && pwd)
verbose=${1:-}
failed=0
for suite in "$here"/hooktest.sh "$here"/review-gates.sh "$here"/*.test.sh "$here"/*.test.py; do
  [ -f "$suite" ] || continue
  case $suite in
    *.py) runner=python3 ;;
    *) runner=bash ;;
  esac
  log=$(mktemp)
  "$runner" "$suite" >"$log" 2>&1
  rc=$?
  printf '%-24s rc %s  %s\n' "${suite##*/}" "$rc" "$(grep -iE '(pass|fail)' "$log" | tail -1)"
  if [ "$rc" != 0 ] || [ "$verbose" = -v ]; then
    sed 's/^/    /' "$log"
  fi
  [ "$rc" = 0 ] || failed=1
  rm -f "$log"
done
exit "$failed"
