#!/bin/bash
# Disposable HOME and Git worktrees; never reads actual accounts or contacts a provider.
set -u
. "${BASH_SOURCE[0]%/*}/lib.sh"
GC=${GC_BIN:-${BASH_SOURCE[0]%/*}/../../bin/claude-gc}
FH=$(mktemp -d "${TMPDIR:-/tmp}/claude-gc-test.XXXXXX") || exit 1
trap 'rm -rf "$FH"' EXIT
mkdir -p "$FH/.claude/hooks/lib" "$FH/.claude/state" "$FH/.claude/file-history" "$FH/tmp"
printf 'kit_env() { CODE_DIRS=""; }\n' > "$FH/.claude/hooks/lib/hook-io"
printf 'CODE_DIRS=""\n' > "$FH/synthetic-kit.env"
isolated() { env -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE HOME="$FH" TMPDIR="$FH/tmp" KIT_ENV="$FH/synthetic-kit.env" GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$FH/.gitconfig" GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_COUNT=0 "$@"; }
run_gc() { isolated bash "$GC" "$@"; }
fgit() { isolated git -c core.hooksPath=/dev/null -c commit.gpgsign=false "$@"; }
report_hash() { python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$1"; }
candidate_report() {
  head=$(fgit -C "$2" rev-parse HEAD)
  fgit -C "$2" update-ref refs/heads/fixture-proof-base "$head"
  printf '%s\t%s\tsynthetic ancestor candidate\t1K\t%s\trefs/heads/fixture-proof-base\t%s\tancestor\t-\n' "$2" "$3" "$head" "$head" > "$1"
}
default="$FH/.claude/state/prunable-worktrees.txt"
report="$FH/reviewed reports/selected.txt"
printf '# Shared report must remain untouched.\n' > "$default"
cp "$default" "$FH/default-before"

out=$(run_gc --report-file "$report" 2>&1); rc=$?
[ "$rc" = 0 ] && [ -f "$report" ] && cmp -s "$default" "$FH/default-before" \
  && ok "explicit generation leaves the shared report intact" || bad "explicit generation" "$out"
check "report records its explicit consume command" "$(cat "$report" 2>/dev/null)" '--report-file'
[ ! -e "$report.tmp" ] && [ -z "$(find "$FH/reviewed reports" -name '*.tmp.*' -print)" ] \
  && ok "report temporary files are removed" || bad "report temporary files remain"

mkdir -p "$FH/.claude/file-history"
printf 'retain\n' > "$FH/.claude/file-history/old"
touch -t 202001010000 "$FH/.claude/file-history/old"
out=$(run_gc --report-file 2>&1); rc=$?
[ "$rc" = 2 ] && [ -f "$FH/.claude/file-history/old" ] \
  && ok "missing report argument refuses before housekeeping" || bad "missing argument" "$out"
out=$(run_gc --report-file "$FH/reviewed reports" 2>&1); rc=$?
[ "$rc" = 1 ] && [ -f "$FH/.claude/file-history/old" ] \
  && ok "directory report refuses before housekeeping" || bad "directory report" "$out"
out=$(run_gc --prune-worktrees --report-file "$FH/missing.txt" --report-sha256 "$(printf '%064d' 0)" 2>&1); rc=$?
[ "$rc" = 1 ] && cmp -s "$default" "$FH/default-before" \
  && ok "missing explicit consume report never falls back" || bad "missing consume report" "$out"

fgit init -q "$FH/repo"
fgit -C "$FH/repo" -c user.name=Fixture -c user.email=fixture@example.invalid commit --allow-empty -qm fixture
fgit -C "$FH/repo" worktree add -qb selected "$FH/selected" >/dev/null
fgit -C "$FH/repo" worktree add -qb shared "$FH/shared" >/dev/null
find "$FH/selected" "$FH/shared" -exec touch -t 202001010000 {} +
mkdir -p "$(dirname "$report")"
candidate_report "$report" "$FH/selected" selected
candidate_report "$default" "$FH/shared" shared
cp "$default" "$FH/default-before"
out=$(run_gc --report-file "$report" --prune-worktrees --report-sha256 "$(report_hash "$report")" 2>&1); rc=$?
[ "$rc" = 0 ] && [ ! -d "$FH/selected" ] && [ -d "$FH/shared" ] && cmp -s "$default" "$FH/default-before" \
  && ok "explicit consume removes only its reviewed candidate" || bad "explicit consume" "$out"

candidate_report "$report" "$FH/shared" shared
sleep 1
touch "$FH/shared/.git"
out=$(run_gc --prune-worktrees --report-file "$report" --report-sha256 "$(report_hash "$report")" 2>&1); rc=$?
[ "$rc" = 0 ] && [ -d "$FH/shared" ] \
  && ok "explicit report retains a worktree touched after review" || bad "freshness refusal" "$out"
out=$(run_gc --report-file "$report" --report-file "$FH/other" 2>&1); rc=$?
[ "$rc" = 2 ] && [ -d "$FH/shared" ] && [ ! -e "$FH/other" ] \
  && ok "duplicate report option refuses before mutation" || bad "duplicate option" "$out"

run_gc --report-file "$FH/concurrent-a" > "$FH/a.log" 2>&1 & a=$!
run_gc --report-file "$FH/concurrent-b" > "$FH/b.log" 2>&1 & b=$!
wait "$a"; ra=$?; wait "$b"; rb=$?
[ "$ra" = 0 ] && [ "$rb" = 0 ] && [ -f "$FH/concurrent-a" ] && [ -f "$FH/concurrent-b" ] \
  && cmp -s "$default" "$FH/default-before" \
  && ok "concurrent explicit generation keeps reports independent" || bad "concurrent generation"
finish
