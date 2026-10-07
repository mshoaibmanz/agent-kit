#!/bin/bash
# The git-hooks layer (~/.agents/git-hooks): dispatcher chaining, the agent pre-push gate
# (lib/push-gate) and the agent commit-msg trailer check, driven by real `git push`/`git commit`
# in a scratch repo with a bare remote. Prints PASS/FAIL per case, exits non-zero on any failure.
#
#   bash ~/.agents/hooks/tests/git-hooks.test.sh   (GIT_HOOKS=<dir> tests another copy)
#
# The layer is injected through GIT_CONFIG_COUNT/KEY/VALUE, exactly as the host env gives it to agent
# shells (command scope, so it beats a repo-local core.hooksPath). Fixtures live under $HOME:
# review-mark-changes skips scratch paths by design. Removed on exit.
set -u
. "${BASH_SOURCE[0]%/*}/lib.sh"
G="${GIT_HOOKS:-$HOME/.agents/git-hooks}"
FX=$(mktemp -d "$HOME/.git-hooks-test-XXXXXX") || exit 1
export TMPDIR="$FX/tmp"; mkdir -p "$TMPDIR"
SID="gh-$$"
TESTS="$HOME/.claude/tmp/claude-tests/$SID"; mkdir -p "${TESTS%/*}"
cleanup() { rm -rf "$FX"; rm -f "$TESTS"; }
trap cleanup EXIT

export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0="$G"
export CLAUDECODE=1 CLAUDE_CODE_SESSION_ID="$SID" AGENT_GIT_HOOKS_ENFORCE=1
unset AGENT_PUSH_NOW CI_WATCH_ACTIVE AGENT_GIT_HOOKS REVIEW_MAX_ROUNDS
# No Codex CLI: round 1 is bug-reviewer alone, so its one return completes the round.
export CODEX_BIN="$FX/no-codex"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
TRAILER=$'\n\nCo-authored-by: Claude <noreply@anthropic.com>'
human() { env -u CLAUDECODE -u AI_AGENT -u AGENT_HOST "$@"; }
# mkhook <path> <printf format> [args]: a fresh file renamed into place. Rewriting an executed script in
# place gets the next exec SIGKILLed on macOS (rc 137).
mkhook() { local f=$1; shift; printf "$@" > "$f.new"; chmod +x "$f.new"; mv -f "$f.new" "$f"; }

REMOTE="$FX/remote.git"; WT="$FX/wt"
git init -q --bare "$REMOTE"
git init -q -b feat "$WT"
g() { git -C "$WT" -c commit.gpgsign=false "$@"; }
lines() { i=0; while [ "$i" -lt "$2" ]; do echo "v_$1_$i = $i"; i=$((i+1)); done; }
work() { lines "$1" "$2" >> "$WT/$1.py"; edit "$WT/$1.py" "$SID" | "$H/review-mark-changes"; }
commit() { g add -A; g commit -qm "$1$TRAILER" 2>&1; }
push() { "$@" 2>&1; echo "rc=$?"; }
printf 'x = 1\n' > "$WT/a.py"; commit init >/dev/null
g remote add origin "$REMOTE"
human git -C "$WT" push -q -u origin feat 2>/dev/null

echo "--- pre-push: review gate ---"
work b 60; commit "b" >/dev/null; touch "$TESTS"
out=$(push git -C "$WT" push -q)
check "unreviewed 60 lines, git -C push: refused" "$out" 'rc=1'
check "...the refusal is the review trigger" "$out" 'This refusal is the review trigger'
check "...and names AGENT_PUSH_NOW" "$out" 'AGENT_PUSH_NOW="<reason>"'
out=$(cd "$WT" && push git push -q)
check "cd <wt> && git push: same gate (round in flight)" "$out" 'has not returned'
out=$(cd "$WT" && AGENT_PUSH_NOW="" push git push -q)
check "empty AGENT_PUSH_NOW: refused" "$out" 'AGENT_PUSH_NOW is set but empty'
jq -cn --arg d "$WT" --arg s "$SID" '{hook_event_name:"SubagentStop",session_id:$s,cwd:$d,agent_id:"g1",agent_type:"bug-reviewer",last_assistant_message:"No findings."}' | "$H/review-agent-mark"
out=$(push git -C "$WT" push -q)
check "reviewed push: allowed" "$out" 'rc=0'

echo "--- pre-push: small delta, tests, sprawl ---"
work c 12; commit "c" >/dev/null
out=$(push git -C "$WT" push -q)
check "12-line delta: allowed" "$out" 'rc=0'
check "...with a self-check note" "$out" 'SELF-CHECK|Self-check'
rm -f "$TESTS"
work d 5; commit "d" >/dev/null
out=$(push git -C "$WT" push -q)
check "no recorded test run: refused" "$out" 'no local test run'
out=$(AGENT_PUSH_NOW="infra down, watching CI" push git -C "$WT" push -q)
check "AGENT_PUSH_NOW with a reason: allowed" "$out" 'rc=0'
check "...and says what it skipped" "$out" 'checks skipped'
touch "$TESTS"
for n in 1 2 3; do printf '# %s\n' "$n" >> "$WT/a.py"; commit "PROJ-1: s$n" >/dev/null; human git -C "$WT" push -q 2>/dev/null; done
printf '# 4\n' >> "$WT/a.py"; commit "PROJ-1: s4" >/dev/null
out=$(push git -C "$WT" push -q)
check "4th ticket commit, no PR: sprawl refused" "$out" 'PROJ-1 already has 3 commits'
human git -C "$WT" push -q 2>/dev/null

echo "--- pre-push: force-push ---"
printf '# amend\n' >> "$WT/a.py"; g add -A; g commit -q --amend -m "PROJ-1: s4 amended$TRAILER"
out=$(push git -C "$WT" push -q -f)
check "force-push: refused" "$out" 'Never force-push'
out=$(AGENT_PUSH_NOW="please" push git -C "$WT" push -q -f)
check "force-push: AGENT_PUSH_NOW does not override" "$out" 'rc=1'
out=$(human git -C "$WT" push -q -f 2>&1; echo "rc=$?")
check "human force-push: the agent layer stays out" "$out" 'rc=0'

echo "--- pre-push: human push, scratch scope ---"
work e 80; commit "e" >/dev/null
out=$(human git -C "$WT" push -q 2>&1; echo "rc=$?")
check "human push of 80 unreviewed lines: allowed" "$out" 'rc=0'
work f 80; commit "f" >/dev/null
out=$(AGENT_GIT_HOOKS_ENFORCE= push git -C "$WT" push -q)
check "agent push to a local bare remote (scratch scope): allowed" "$out" 'rc=0'

echo "--- dispatcher: the repo's own hooks ---"
mkdir -p "$WT/.git/hooks"
mkhook "$WT/.git/hooks/pre-push" '#!/bin/sh\nread l; echo "repo-hook $1 $l" >> "%s/chain.log"\nexit 0\n' "$FX"
printf '# h\n' >> "$WT/a.py"; commit "h" >/dev/null
human git -C "$WT" push -q 2>/dev/null
check "human push runs the repo's .git/hooks/pre-push" "$(cat "$FX/chain.log" 2>/dev/null)" 'repo-hook origin refs/heads/feat'
check "...with pre-push's ref list on stdin" "$(cat "$FX/chain.log" 2>/dev/null)" 'refs/heads/feat [0-9a-f]{40}'
mkhook "$WT/.git/hooks/pre-push" '#!/bin/sh\necho repo-hook-refused >&2\nexit 1\n'
printf '# i\n' >> "$WT/a.py"; commit "i" >/dev/null
out=$(AGENT_PUSH_NOW="chain test" push git -C "$WT" push -q)
check "agent push: a repo hook's refusal stands" "$out" 'repo-hook-refused'
mkhook "$WT/.git/hooks/pre-push" '#!/bin/sh\necho repo-hook-2 >> "%s/chain.log"\n' "$FX"
git -C "$WT" config core.hooksPath "$G"
printf '# j\n' >> "$WT/a.py"; commit "j" >/dev/null
env -u GIT_CONFIG_COUNT -u GIT_CONFIG_KEY_0 -u GIT_CONFIG_VALUE_0 env -u CLAUDECODE -u AI_AGENT -u AGENT_HOST git -C "$WT" push -q 2>/dev/null
check "local core.hooksPath naming the layer: chains to .git/hooks" "$(cat "$FX/chain.log" 2>/dev/null)" 'repo-hook-2'
git -C "$WT" config --unset core.hooksPath
rm -f "$WT/.git/hooks/pre-push"
HK="$FX/husky"; git init -q -b main "$HK"; mkdir -p "$HK/.husky/_"
mkhook "$HK/.husky/_/commit-msg" '#!/bin/sh\necho husky-ran >> "%s/husky.log"\nexit 0\n' "$FX"
git -C "$HK" config core.hooksPath .husky/_
git -C "$HK" remote add origin git@github.com:x/y.git
printf 'x\n' > "$HK/a.txt"; git -C "$HK" add a.txt
out=$(git -C "$HK" -c commit.gpgsign=false commit -qm "no trailer" 2>&1; echo "rc=$?")
check "husky repo (local core.hooksPath), agent: trailer enforced" "$out" 'lacks the trailer'
git -C "$HK" -c commit.gpgsign=false commit -qm "with$TRAILER" >/dev/null 2>&1
check "...and the husky hook still runs" "$(cat "$FX/husky.log" 2>/dev/null)" 'husky-ran'
# A global core.hooksPath is chained too: the layer replaces it, as it replaces a local one.
GHD="$FX/global-hooks"; mkdir -p "$GHD"
mkhook "$GHD/pre-commit" '#!/bin/sh\necho global-pre-commit >> "%s/global.log"\n' "$FX"
printf '[core]\n\thooksPath = %s\n' "$GHD" > "$FX/gitconfig-global"
GR="$FX/gr"; git init -q -b main "$GR"; printf 'x\n' > "$GR/a.txt"; git -C "$GR" add a.txt
GIT_CONFIG_GLOBAL="$FX/gitconfig-global" git -C "$GR" -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -qm "g$TRAILER" >/dev/null 2>&1
check "global core.hooksPath: its pre-commit still runs under the layer" "$(cat "$FX/global.log" 2>/dev/null)" 'global-pre-commit'
# A user's own GIT_CONFIG_* hooksPath entry, merged before the layer's (git_layer_env), is chained.
UHD="$FX/user-hooks"; mkdir -p "$UHD"
mkhook "$UHD/pre-commit" '#!/bin/sh\necho user-pre-commit >> "%s/user.log"\n' "$FX"
printf 'y\n' >> "$GR/a.txt"; git -C "$GR" add a.txt
out=$(env GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0="$UHD" GIT_CONFIG_KEY_1=core.hooksPath GIT_CONFIG_VALUE_1="$G" \
  git -C "$GR" -c commit.gpgsign=false commit -qm "no trailer" 2>&1; echo "rc=$?")
check "user's own env hooksPath entry before the layer's: the agent layer runs" "$out" 'lacks the trailer'
env GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0="$UHD" GIT_CONFIG_KEY_1=core.hooksPath GIT_CONFIG_VALUE_1="$G" \
  git -C "$GR" -c commit.gpgsign=false commit -qm "u$TRAILER" >/dev/null 2>&1
check "...then the user's own pre-commit" "$(cat "$FX/user.log" 2>/dev/null)" 'user-pre-commit'
mkhook "$HK/.husky/_/pre-push" '#!/bin/sh\nread l; echo "husky-pre-push $l" >> "%s/husky.log"\n' "$FX"
HKR="$FX/husky-remote.git"; git init -q --bare "$HKR"; git -C "$HK" remote add local "$HKR"
printf 'x = 1\n' > "$HK/m.py"; git -C "$HK" add m.py; git -C "$HK" -c commit.gpgsign=false commit -qm "code$TRAILER" >/dev/null 2>&1
out=$(AGENT_PUSH_NOW="chain test" push git -C "$HK" push -q local main)
check "env-injected layer, agent push: the agent gate runs" "$out" 'agent pre-push: AGENT_PUSH_NOW'
check "...then the husky pre-push, with the ref list" "$(cat "$FX/husky.log" 2>/dev/null)" 'husky-pre-push refs/heads/main [0-9a-f]{40}'

echo "--- dispatcher: every hook name ---"
for n in applypatch-msg pre-applypatch post-applypatch pre-commit pre-merge-commit prepare-commit-msg \
  commit-msg post-commit pre-rebase post-checkout post-merge pre-push pre-receive update proc-receive \
  post-receive post-update push-to-checkout pre-auto-gc post-rewrite \
  sendemail-validate fsmonitor-watchman p4-changelist p4-prepare-changelist p4-post-changelist \
  p4-pre-submit; do
  [ "$(readlink "$G/$n")" = dispatch ] || miss="${miss:-} $n"
done
empty "a dispatcher link for every githooks(5) name" "${miss:-}"
# No link for the two hooks that fire on every ref update and index write (a fork per event).
for n in reference-transaction post-index-change; do [ -e "$G/$n" ] && extra="${extra:-} $n"; done
empty "no reference-transaction or post-index-change link" "${extra:-}"
PT="$FX/ptc"; git init -q -b main "$PT"; printf 'x\n' > "$PT/a.txt"
git -C "$PT" add a.txt; git -C "$PT" -c commit.gpgsign=false commit -qm "p$TRAILER"
git -C "$PT" config receive.denyCurrentBranch updateInstead
PC="$FX/ptc-clone"; git clone -q "$PT" "$PC" 2>/dev/null
printf 'y\n' >> "$PC/a.txt"; git -C "$PC" -c commit.gpgsign=false commit -qam "p2$TRAILER"
out=$(push git -C "$PC" push -q origin main)
check "push into an updateInstead checkout with no own hook: accepted" "$out" 'rc=0'
check "...and its worktree updated (the default push-to-checkout)" "$(cat "$PT/a.txt")" '^y$'
printf 'dirty\n' >> "$PT/a.txt"; printf 'z\n' >> "$PC/a.txt"; git -C "$PC" -c commit.gpgsign=false commit -qam "p3$TRAILER"
out=$(push git -C "$PC" push -q origin main)
check "...a dirty target worktree is refused, as git's default does" "$out" 'unstaged changes'
git -C "$PT" checkout -q -- a.txt
mkhook "$PT/.git/hooks/push-to-checkout" '#!/bin/sh\necho "own-ptc $1" >> "%s/ptc.log"\ngit read-tree -u -m "$1"\n' "$FX"
out=$(push git -C "$PC" push -q origin main)
check "the repo's own push-to-checkout replaces the default" "$(cat "$FX/ptc.log" 2>/dev/null)" 'own-ptc [0-9a-f]{40}'

echo "--- commit-msg ---"
printf 'y\n' >> "$HK/a.txt"; git -C "$HK" add a.txt
out=$(AGENT_GIT_HOOKS_ENFORCE= git -C "$HK" -c commit.gpgsign=false commit -qm "agent, network remote, no trailer" 2>&1; echo "rc=$?")
check "network remote, no trailer: refused without the test override" "$out" 'rc=1'
out=$(human git -C "$HK" -c commit.gpgsign=false commit -qm "human, no trailer" 2>&1; echo "rc=$?")
check "human commit without a trailer: allowed" "$out" 'rc=0'
out=$(commit "scratch repo, no trailer"; AGENT_GIT_HOOKS_ENFORCE= g commit -q --allow-empty -m "no trailer, local remote" 2>&1; echo "rc=$?")
check "scratch repo (local remote only): allowed" "$out" 'rc=0'
git -C "$HK" checkout -q -b side; printf 'z\n' > "$HK/b.txt"; git -C "$HK" add b.txt
git -C "$HK" -c commit.gpgsign=false commit -qm "side$TRAILER" >/dev/null 2>&1
git -C "$HK" checkout -q main; printf 'w\n' > "$HK/c.txt"; git -C "$HK" add c.txt
git -C "$HK" -c commit.gpgsign=false commit -qm "main$TRAILER" >/dev/null 2>&1
out=$(git -C "$HK" -c commit.gpgsign=false merge -q --no-ff --no-edit side 2>&1; echo "rc=$?")
check "a merge's generated message is exempt" "$out" 'rc=0'

finish
