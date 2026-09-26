#!/bin/bash
# Copy the shared hook sources into the plugins that run them. A plugin is installed on its own, so
# each carries its own copy; this script is the only writer of those copies.
#   lib/hooks/*                      -> plugins/<p>/hooks/lib/  for every plugin in LIB_PLUGINS
#   plugins/guard-rails/hooks/bash-guards -> plugins/prod-data/hooks/bash-guards (one script, two
#                                       guard sets picked by its argument; see its header)
# Usage: scripts/build.sh            write the copies
#        scripts/build.sh --check    exit 1 and name each copy that differs from its source
set -uo pipefail
KIT=$(cd "$(dirname "$0")/.." && pwd)
LIB_PLUGINS="guard-rails prod-data ci-babysitter auto-review session-context terminal-signals"
SHARED="plugins/guard-rails/hooks/bash-guards:plugins/prod-data/hooks/bash-guards"
check=0
[ "${1:-}" = --check ] && check=1
drift=0

# sync <src> <dst>: copy, or in check mode report a missing or differing copy.
sync() {
  if [ "$check" = 1 ]; then
    cmp -s "$1" "$2" || { echo "drift: ${2#"$KIT"/} differs from ${1#"$KIT"/}"; drift=1; }
    return 0
  fi
  mkdir -p "$(dirname "$2")"
  cp -p "$1" "$2"
}

for p in $LIB_PLUGINS; do
  dst="$KIT/plugins/$p/hooks/lib"
  for f in "$KIT"/lib/hooks/*; do
    sync "$f" "$dst/${f##*/}"
  done
  # A file removed from lib/hooks must not linger in a plugin's copy.
  for f in "$dst"/*; do
    [ -e "$f" ] || continue
    [ -e "$KIT/lib/hooks/${f##*/}" ] && continue
    if [ "$check" = 1 ]; then echo "drift: ${f#"$KIT"/} has no source in lib/hooks"; drift=1; else rm -f "$f"; fi
  done
done

for pair in $SHARED; do
  sync "$KIT/${pair%%:*}" "$KIT/${pair#*:}"
done

[ "$check" = 1 ] && { [ "$drift" = 0 ] && echo "build: every copy matches its source"; exit "$drift"; }
echo "build: copied lib/hooks into $(echo $LIB_PLUGINS | wc -w | tr -d ' ') plugins and the shared hooks"
