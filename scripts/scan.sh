#!/bin/bash
# Leak scan for this public repo. Exit 1 on any finding. CI and .githooks/pre-push run it.
#   1. self-test: every custom .gitleaks.toml rule fires on a canary built at run time
#   2. gitleaks over the publishable tree (tracked + untracked-not-ignored files)
#   3. gitleaks over the commits in the range
#   4. the org denylist (scripts/leak-check.sh) over the tree, file names, and the range's commits and
#      annotated tags
#   5. commit identities: every author and committer email in the range is a GitHub noreply address
#   6. images: any image or PDF in the tree or added in the range must be listed in .scan-images-allow
#
#   scripts/scan.sh [--report FILE] [-- REV-LIST-ARGS...]
# The range is a `git rev-list` range, default --all (CI); .githooks/pre-push passes what a push adds.
# gitleaks: $GITLEAKS, else `gitleaks` on PATH (CI downloads a pinned release).
# Denylist sources and format: scripts/leak-check.sh. With no list step 4 is skipped with one warning,
# and REQUIRE_LEAK_TERMS=1 (set in CI on pushes) turns that skip into a failure.
set -uo pipefail
KIT=$(cd "$(dirname "$0")/.." && pwd)
REPORT=/dev/null
while [ $# -gt 0 ]; do
  case $1 in
    --report) REPORT=${2:?--report needs a file}; shift 2 ;;
    --) shift; break ;;
    *) echo "scan: unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ $# -gt 0 ] || set -- --all
: > "$REPORT"
GL=${GITLEAKS:-$(command -v gitleaks || true)}
TMP=$(mktemp -d) || exit 1
trap 'rm -rf "$TMP"' EXIT
fail=0
say() { printf '%s\n' "$*" | tee -a "$REPORT"; }
hit() { fail=1; say "  FINDING $*"; }

[ -x "$GL" ] || { say "scan: gitleaks not found (set GITLEAKS or put it on PATH)"; exit 2; }
git -C "$KIT" rev-list "$@" > "$TMP/range" || { say "scan: git rev-list $* failed"; exit 2; }
ncommits=$(grep -c . "$TMP/range")
say "scan: $("$GL" version 2>/dev/null | head -1) over $ncommits commit(s) ($*)"

# gl <dir|git> <target> <json out> [extra args]: run gitleaks from the target so allowlist paths match.
gl() {
  local mode=$1 target=$2 out=$3; shift 3
  (cd "$target" && "$GL" "$mode" . -c "$KIT/.gitleaks.toml" --no-banner --redact --exit-code 0 \
    -f json -r "$out" "$@" >/dev/null 2>"$out.log") || { say "scan: gitleaks $mode failed: $(tail -2 "$out.log")"; exit 2; }
}
# findings <json>: one line per finding, secret redacted by gitleaks.
findings() { jq -r '.[] | "\(.RuleID) \(.File):\(.StartLine)\(if .Commit != "" then " commit " + .Commit[0:12] else "" end)"' "$1"; }

say "== 1. rule self-test =="
C="$TMP/canary"; mkdir -p "$C"
{
  printf 'host %s.%s.%s.%s\n' 10 10 20 30
  printf 'mail %s@%s\n' jane.doe acme-corp.com
  printf 'site %s.%s\n' acme atlassian.net
  printf 'id %s-%s-%s-%s-%s\n' 123e4567 e89b 42d3 a456 426614174000
  printf 'order %s%s%s\n' AWB 1234567890 X
  printf 'doc %s%s\n' 1 Xk9vQz2LmN4pR7sT0uW3yA5bC8dE6fG1h
  printf 'bq %s=%s\n' --project_id acme-prod-42
  printf 'db %s.%s\n' primary-db corp
  printf 'token %s%s\n' ghp_ 8f3Kq9Lm2Np7Rs4Tv6Wx1Yz0Ab5Cd3Ef9Gh2
} > "$C/canary.txt"
gl dir "$C" "$TMP/canary.json"
# The custom rules, plus one built-in rule to prove useDefault still loads gitleaks' own set.
for rule in $(sed -n 's/^id = "\(.*\)"$/\1/p' "$KIT/.gitleaks.toml") github-pat; do
  n=$(jq --arg r "$rule" '[.[] | select(.RuleID == $r)] | length' "$TMP/canary.json")
  if [ "$n" -gt 0 ]; then say "  rule $rule: fires on its canary"; else hit "rule $rule did not fire on its canary"; fi
done

say "== 2. gitleaks: publishable tree =="
T="$TMP/tree"; mkdir -p "$T"
(cd "$KIT" && git ls-files -z --cached --others --exclude-standard | while IFS= read -r -d '' f; do
  [ -f "$f" ] && mkdir -p "$T/$(dirname "$f")" && cp -p "$f" "$T/$f"; done)
say "  $(find "$T" -type f | wc -l | tr -d ' ') files"
gl dir "$T" "$TMP/tree.json"
n=$(jq length "$TMP/tree.json"); say "  findings: $n"
[ "$n" = 0 ] || while IFS= read -r l; do hit "$l"; done < <(findings "$TMP/tree.json")

say "== 3. gitleaks: history =="
if [ "$ncommits" != 0 ]; then
  gl git "$KIT" "$TMP/hist.json" --log-opts="$*"
  n=$(jq length "$TMP/hist.json"); say "  findings: $n"
  [ "$n" = 0 ] || while IFS= read -r l; do hit "$l"; done < <(findings "$TMP/hist.json")
else
  say "  no commits in the range"
fi

say "== 4. org denylist =="
# File name #N is line N of `git ls-files --cached --others --exclude-standard`.
(cd "$KIT" && git ls-files --cached --others --exclude-standard) > "$TMP/paths"
"$BASH" "$KIT/scripts/leak-check.sh" --tree "$T" --paths "$TMP/paths" --allow "$KIT/.scan-history-allow" -- "$@" \
  > "$TMP/leak" 2>&1
rc=$?
while IFS= read -r l; do say "  $l"; done < "$TMP/leak"
case $rc in
  0) ;;
  1) fail=1 ;;
  3) [ "${REQUIRE_LEAK_TERMS:-0}" = 1 ] && hit "no denylist: set the LEAK_TERMS secret" ;;
  *) hit "leak-check failed (exit $rc)" ;;
esac

say "== 5. commit identities =="
bad_ids=$(git -C "$KIT" log --format='%ae%n%ce' "$@" 2>/dev/null | sort -u | grep -vE '@users\.noreply\.github\.com$|^noreply@github\.com$' || true)
if [ -n "$bad_ids" ]; then
  hit "$(printf '%s\n' "$bad_ids" | wc -l | tr -d ' ') author/committer address(es) are not GitHub noreply (git log --format='%ae %ce' $*)"
else
  say "  every author and committer is a GitHub noreply address"
fi

say "== 6. images =="
allow="$KIT/.scan-images-allow"
img_re='\.(png|jpe?g|gif|webp|bmp|tiff?|ico|heic|avif|svg|pdf|psd)$'
{
  grep -iE "$img_re" "$TMP/paths"
  git -C "$KIT" log --diff-filter=A --name-only --format= "$@" 2>/dev/null | grep -iE "$img_re"
  # Content, not only the name: an image renamed to .txt still ships.
  find "$T" -type f -exec file --mime-type {} + 2>/dev/null | grep -E ': (image/|application/pdf)' | sed "s|^$T/||; s|: .*||"
} | sort -u > "$TMP/images"
nimg=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  nimg=$((nimg + 1))
  grep -qxF -- "$f" "$allow" 2>/dev/null || hit "image not in .scan-images-allow: $f"
done < "$TMP/images"
nallow=$(grep -cvE '^(#|$)' "$allow" 2>/dev/null) || true
say "  $nimg image(s) found, ${nallow:-0} allowlisted"

if [ "$fail" = 0 ]; then say "scan: clean"; else say "scan: FINDINGS above"; fi
exit "$fail"
