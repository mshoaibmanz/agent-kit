#!/usr/bin/env bash
# Commit the generated plugins/ tree onto the dist branch as one new commit and push it, never forcing.
# Usage: scripts/publish_dist.sh <source sha> [remote]   (run scripts/build.sh first)
# The commit is built from a scratch index, so the checkout, its index and its branches stay untouched;
# git records the exec bits and symlinks the build wrote. No change since the last dist commit: no commit.
set -euo pipefail
source_sha=$1
remote=${2:-origin}
root=$(cd "$(dirname "$0")/.." && pwd)
[ -d "$root/plugins" ] || { echo "publish_dist: no plugins/ to publish; run scripts/build.sh first" >&2; exit 1; }
index=$(mktemp "${TMPDIR:-/tmp}/publish-dist-index.XXXXXX")
trap 'rm -f "$index"' EXIT
rm -f "$index"
GIT_INDEX_FILE=$index git -C "$root" add --force -- plugins
tree=$(GIT_INDEX_FILE=$index git -C "$root" write-tree)
parent=()
if git -C "$root" ls-remote --exit-code --heads "$remote" dist >/dev/null; then
  git -C "$root" fetch --quiet --no-tags "$remote" refs/heads/dist
  head=$(git -C "$root" rev-parse FETCH_HEAD)
  if [ "$(git -C "$root" rev-parse "$head^{tree}")" = "$tree" ]; then
    echo "publish_dist: dist already holds this build ($head)"
    exit 0
  fi
  parent=(-p "$head")
fi
commit=$(git -C "$root" commit-tree "$tree" ${parent[@]+"${parent[@]}"} -m "Build the plugin packages from $source_sha")
git -C "$root" push --quiet "$remote" "$commit:refs/heads/dist"
echo "publish_dist: dist -> $commit (from $source_sha)"
