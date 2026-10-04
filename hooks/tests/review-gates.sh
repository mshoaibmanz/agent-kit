#!/bin/bash
# Payload tests for the review and commit gates: review-mark-changes, review-trigger,
# review-agent-mark, the git pre-push hook (lib/push-gate, lib/review-state: review on push, the
# branch review record), commit-cohesion, comment-guard, and the
# missing-lib policy. Prints PASS/FAIL per case and exits non-zero on any failure.
#
#   bash ~/.claude/hooks/tests/review-gates.sh   (HOOKS_DIR=<dir> tests another copy; see lib.sh)
#
# Fixtures live under $HOME, not the scratchpad: every hook here skips /tmp and scratchpad paths
# by design, so a fixture there tests nothing. Removed on exit.
set -u
. "${BASH_SOURCE[0]%/*}/lib.sh"
FX=$(mktemp -d "$HOME/.review-gates-XXXXXX") || exit 1
export TMPDIR="$FX/tmp"
mkdir -p "$TMPDIR"
R="$TMPDIR/claude-review"
SID="rg-$$"
BR="rgtest-$$"
# The branch -> PR pointer's on-disk contract with ci-watch (lib/state).
PRFILE="$HOME/.claude/tmp/ci-branch-$BR.pr"
TESTS="$HOME/.claude/tmp/claude-tests/$SID"; TESTS2="$TESTS-2"
cleanup() { rm -rf "$FX"; rm -f "$TESTS2" "$PRFILE" "$HOME/.claude/tmp/ci-branch-$BR-3.pr" "$TESTS" "$HOME/.claude/tmp/claude-commit/turn-$SID" "$HOME/.claude/tmp/claude-commit/last-$SID"; }
trap cleanup EXIT

REPO="$FX/repo"; REMOTE="$FX/remote.git"
git init -q --bare "$REMOTE"
git init -q -b "$BR" "$REPO"
g() { git -C "$REPO" -c user.name=t -c user.email=t@t -c commit.gpgsign=false "$@"; }
printf 'x = 1\n' > "$REPO/a.py"
g add a.py; g commit -qm init; g remote add origin "$REMOTE"; g push -q -u origin "$BR" 2>/dev/null
lines() { i=0; while [ "$i" -lt "$2" ]; do echo "v_$1_$i = $i"; i=$((i+1)); done; }

# Some sequences complete several agent rounds on one branch; the budget case sets its own.
export REVIEW_MAX_ROUNDS=9
# No Codex CLI unless a case names one (CODEX_BIN=$FK): round 1 is then thermo-bugs alone.
export CODEX_BIN="$FX/no-codex"
export CLAUDE_BIN="$FX/no-claude"  # Only explicit synthetic Anthropic cases may run a CLI.
# The review roles as the kit renders them from roles.toml; a case that switches a role writes its own.
export AGENT_ROLES_SH="$FX/roles.sh"
"$H/../bin/agent-kit" roles --format sh > "$AGENT_ROLES_SH" 2>/dev/null || rm -f "$AGENT_ROLES_SH"
# rs <rv_fn> <args>: a lib/review-state accessor, so no case depends on how the state is stored.
rs() { ( . "$H/lib/review-state" 2>/dev/null && "$@" ); }
# ...in <repo> <sid>: the payload helpers for a section's own repo and session.
workin()  { lines "$3" "$4" >> "$1/$3.py"; edit "$1/$3.py" "$2" | "$H/review-mark-changes"; }
stopin()  { stop "$2" "$1" "${3:-false}" | "$H/review-trigger"; }
# Each return gets its own agent_id: review-agent-mark counts an agent once.
AN=0
sstopin() { AN=$((AN + 1)); jq -cn --arg d "$1" --arg s "$2" --arg t "$3" --arg m "${4-findings}" --arg i "a$AN" '{hook_event_name:"SubagentStop",session_id:$s,cwd:$d,agent_id:$i,agent_type:$t,stop_hook_active:false,last_assistant_message:$m}' | "$H/review-agent-mark"; }
sstop() { sstopin "$REPO" "$SID" "$@"; }
agent() { jq -cn --arg s "$SID" --arg d "$REPO" --arg t "$1" --arg st "$2" '{hook_event_name:"PostToolUse",tool_name:"Agent",session_id:$s,cwd:$d,tool_input:{subagent_type:$t,prompt:"x"},tool_response:{status:$st}}'; }
work()  { workin "$REPO" "$SID" "$@"; }
# mkrepo <dir> [remote]: a one-commit repo on main, pushed to <remote> (created bare) when given.
mkrepo() {
  git init -q -b main "$1"; printf 'x = 1\n' > "$1/a.py"
  git -C "$1" add a.py; git -C "$1" -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -qm init
  [ -z "${2:-}" ] || { [ -d "$2" ] || git init -q --bare "$2"; git -C "$1" remote add origin "$2"; git -C "$1" push -q -u origin main 2>/dev/null; }
}
# The agent push gate is the git pre-push hook (~/.agents/git-hooks); gpush runs a real
# `git push --dry-run` through the layer in an agent shell, injected the way Claude's settings env
# does it. Only commits reach a pre-push hook. Default: the git-hooks beside the hooks under test, so
# HOOKS_DIR=<worktree>/hooks alone tests that worktree's push gate, not the live kit's.
GH="${GIT_HOOKS:-$(cd -P "$H/.." 2>/dev/null && pwd)/git-hooks}"
[ -d "$GH" ] || GH="$HOME/.agents/git-hooks"
gpush() { local s=$1 d=$2; shift 2; ( cd "$d" && env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0="${GHX:-$GH}" CLAUDECODE=1 CLAUDE_CODE_SESSION_ID="$s" AGENT_GIT_HOOKS_ENFORCE=1 "$@" git push --dry-run -q 2>&1; echo "rc=$?" ); }
# issue <repo> <sid> [VAR=value...]: commit everything and push; a refusal issues the branch's round.
issue() { git -C "$1" add -A; git -C "$1" -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -qm "w$AN" >/dev/null 2>&1; gpush "$2" "$1" "${@:3}"; }
# rec <repo> <branch> <jq>: a field of the branch's review record.
rec() { jq -r "$3" "$(git -C "$1" rev-parse --path-format=absolute --git-common-dir)/agent-review/$2.json" 2>/dev/null; }
mkdir -p "$(dirname "$TESTS")"

echo "--- review-mark-changes ---"
work b 5
rs rv_edited "$SID" "$REPO" && [ "$(rs rv_base "$SID" "$REPO")" = "$(g rev-parse HEAD)" ] \
  && ok "edit marks the worktree + the session's per-root base" || bad "edit marks the worktree + the session's per-root base"
rs rv_clear_edits "$SID" "$REPO"
edit "/private/tmp/claude-1/x.py" "$SID" | "$H/review-mark-changes"
mkdir -p "$REPO/prototypes"; edit "$REPO/prototypes/p.py" "$SID" | "$H/review-mark-changes"
rs rv_edited "$SID" "$REPO" && bad "scratch or prototype path marked" || ok "scratch and prototype paths not marked"
edit "$REPO/b.py" "$SID" | "$H/review-mark-changes"
( . "$H/lib/review-state" && [ -z "${RV_DISCIPLINE:-}${RV_POLICY:-}" ] ) \
  && ok "sourcing review-state reads no fix-policy file" || bad "sourcing review-state reads no fix-policy file"

echo "--- review-trigger: the self-check tier only ---"
out=$(stop "$SID" "$REPO" | "$H/review-trigger"); empty "5-line delta is under the floor: silent" "$out"
rs rv_edited "$SID" "$REPO" && ok "a Stop that ran no review keeps the marker" || bad "a Stop that ran no review keeps the marker"
work b 20
out=$(stop "$SID" "$REPO" | "$H/review-trigger"); check "25 lines -> tier-0 self-check" "$out" 'SELF-CHECK'
check "round 1 scope is the session diff" "$out" "git diff [0-9a-f]{12} [0-9a-f]{12}.: this session"
check "...and says agent review happens at push" "$out" 'Agent review happens at push'
out=$(stop "$SID" "$REPO" | "$H/review-trigger"); empty "unchanged tree: silent" "$out"
out=$(stop "$SID" "$REPO" true | "$H/review-trigger"); empty "chain end: silent" "$out"
rs rv_edited "$SID" "$REPO" && bad "chain end of a self-check clears markers" || ok "chain end of a self-check clears markers"
work c 60
out=$(stop "$SID" "$REPO" | "$H/review-trigger"); empty "60 lines at a Stop: silent, the push asks for the review" "$out"
rs rv_pending "$SID" "$REPO" && bad "a Stop issues no agent round" || ok "a Stop issues no agent round"

echo "--- git pre-push: round 1 on push ---"
touch "$TESTS"
out=$(issue "$REPO" "$SID")
check "unreviewed lines on the branch: refused" "$out" 'rc=1'
check "refusal is the review trigger" "$out" 'This refusal is the review trigger'
check "refusal names the override first" "$out" 'AGENT_PUSH_NOW.*review trigger'
check "round 1: thermo-bugs and thermo-quality, one message, foreground" "$out" 'subagent_type "thermo-bugs" and subagent_type "thermo-quality".*FOREGROUND|ONE message, in the FOREGROUND.*subagent_type "thermo-bugs" and subagent_type "thermo-quality"'
check "...with reproduce-first and the TALLY line" "$out" 'REPRODUCE, THEN FIX.*TALLY <id>=FIXED\|NOT_REPRODUCED\|OPTION\|SKIPPED'
check "...and no critic" "$out" 'critic' absent
check "...and the one fix policy text" "$out" 'FIX POLICY.*reason'
[ "$(rec "$REPO" "$BR" '.pending_tree | length')" = 40 ] && ok "the round is pending on the branch record" || bad "the round is pending on the branch record" "$(rec "$REPO" "$BR" .)"
rs rv_pending "$SID" "$REPO" && ok "...and in the session" || bad "...and in the session"
out=$(gpush "$SID" "$REPO"); check "round in flight: wait" "$out" 'has not returned'
check "...naming the agents to spawn" "$out" 'thermo-bugs thermo-quality'
out=$(gpush "$SID" "$REPO" AGENT_PUSH_NOW="WIP share"); check "AGENT_PUSH_NOW overrides" "$out" 'rc=0'

echo "--- review-agent-mark: completion, not spawn ---"
agent thermo-bugs async_launched | "$H/review-agent-mark"
rs rv_pending "$SID" "$REPO" && ok "background spawn completes nothing" || bad "background spawn completes nothing"
agent thermo-bugs completed | "$H/review-agent-mark"
rs rv_pending "$SID" "$REPO" && ok "PostToolUse(Agent) without a report completes nothing" || bad "PostToolUse(Agent) without a report completes nothing"
sstop thermo-bugs ""
rs rv_pending "$SID" "$REPO" && ok "interrupted agent completes nothing" || bad "interrupted agent completes nothing"
sstop Explore
rs rv_pending "$SID" "$REPO" && ok "non-review agent completes nothing" || bad "non-review agent completes nothing"
sstop worker
out=$(gpush "$SID" "$REPO"); check "a worker's return does not close the round" "$out" 'has not returned'
ptree=$(rec "$REPO" "$BR" .pending_tree)
sstop thermo-bugs
rs rv_pending "$SID" "$REPO" && bad "SubagentStop(thermo-bugs) completes the round" || ok "SubagentStop(thermo-bugs) completes the round"
[ "$(rec "$REPO" "$BR" '"\(.rounds) \(.reviewed_tree) \(.pending_tree)"')" = "1 $ptree null" ] \
  && ok "...and writes the branch record: round 1, the issued tree reviewed" || bad "...and writes the branch record: round 1, the issued tree reviewed" "$(rec "$REPO" "$BR" .)"
rs rv_edited "$SID" "$REPO" && bad "unchanged tree after completion clears markers" || ok "unchanged tree after completion clears markers"
rs rv_tally_owed "$SID" && ok "a TALLY is owed" || bad "a TALLY is owed"
out=$(stop "$SID" "$REPO" | "$H/review-trigger"); check "tally owed: a Stop without a TALLY line blocks" "$out" 'thermo-bugs review returned findings and no TALLY line closed them'
check "...with the reproduce-first instruction" "$out" 'Reproduce each finding before fixing it'
out=$(stop "$SID" "$REPO" true | "$H/review-trigger"); empty "tally reminder fires once" "$out"
sstop thermo-bugs
rs rv_tally_owed "$SID" && bad "unrelated reviewer (no round pending) owes nothing" || ok "unrelated reviewer (no round pending) owes nothing"
sstop thermo-quality
[ "$(rec "$REPO" "$BR" .quality_done)" = true ] && ok "thermo-quality's return marks the branch quality_done" || bad "thermo-quality's return marks the branch quality_done" "$(rec "$REPO" "$BR" .)"
( . "$H/lib/review-state" && rv_tally_owed "$SID" && [ "$RV_OWED" = thermo-quality ] ) \
  && ok "...and its findings owe the TALLY (RC-CX-4)" || bad "...and its findings owe the TALLY (RC-CX-4)"
rs rv_tally_settle "$SID"
out=$(gpush "$SID" "$REPO"); check "reviewed tree: push passes" "$out" 'rc=0'
check "...silently" "$out" 'agent pre-push' absent

echo "--- git pre-push: fix-ups and round 2 ---"
work f 20; g add -A; g commit -qm pp1
out=$(gpush "$SID" "$REPO")
check "20 lines since the round: allowed" "$out" 'rc=0'
check "...with a self-check note" "$out" 'agent pre-push: .*[Ss]elf-check'
work f 40; out=$(issue "$REPO" "$SID")
check "60 lines since the round: refused for round 2" "$out" 'Review round 2 of'
check "round 2 is thermo-bugs only (thermo-quality ran on this branch)" "$out" 'thermo-quality' absent
check "round 2 is sized on its own delta" "$out" '~60 line delta'
check "round 2 scope is only the delta" "$out" 'ONLY what changed since the last review round'
sstop thermo-bugs "No findings."
rs rv_tally_owed "$SID" && bad "a zero-finding round owes no TALLY" || ok "a zero-finding round owes no TALLY"
out=$(gpush "$SID" "$REPO"); check "after completion the push passes" "$out" 'rc=0'
rm -f "$TESTS"
out=$(gpush "$SID" "$REPO"); check "no test run: refused" "$out" 'no local test run'

echo "--- git pre-push: the budget is the branch's, kept until merge ---"
S2="$SID-2"; touch "$TESTS2"
work g 60; out=$(issue "$REPO" "$S2" REVIEW_MAX_ROUNDS=3)
check "a new session on the branch continues its count: round 3 of 3" "$out" 'Review round 3 of 3'
sstopin "$REPO" "$S2" thermo-bugs "No findings."
work h 60; out=$(issue "$REPO" "$S2" REVIEW_MAX_ROUNDS=3)
check "past the budget: allowed" "$out" 'rc=0'
check "...with a warning and no new round" "$out" 'WARNING: this branch has used its 3 agent review rounds'
check "...and no agents" "$out" 'subagent_type' absent
env -u CLAUDECODE -u AI_AGENT -u AGENT_HOST git -C "$REPO" push -q origin "HEAD:refs/heads/main" 2>/dev/null; g fetch -q origin 2>/dev/null
work i 60; out=$(issue "$REPO" "$S2" REVIEW_MAX_ROUNDS=3)
check "a record whose reviewed commit reached a base branch is stale: round 1 again" "$out" 'Review round 1 of 3'
sstopin "$REPO" "$S2" thermo-bugs "No findings."

echo "--- git pre-push: what is not a push ---"
work j 60; g add -A; g commit -qm pp-np
ash() { ( cd "$REPO" && env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0="$GH" CLAUDECODE=1 CLAUDE_CODE_SESSION_ID="$SID" AGENT_GIT_HOOKS_ENFORCE=1 bash -c "$1" 2>&1; echo "rc=$?" ); }
out=$(ash 'body="then git push"; printf "%s\n" "$body" >/dev/null; git status -s >/dev/null')
check "a quoted git push is not a push" "$out" 'agent pre-push' absent
out=$(ash 'echo done # then git push')
check "git push in a comment is not a push" "$out" 'agent pre-push' absent
g reset -q --soft HEAD~1

echo "--- missing-lib policy ---"
# A hooks copy with one lib removed: guards of pushes and prod data refuse, advisory hooks say so.
# -H: $H is a symlink into ~/.agents; plain -R copies the link, and rm below would hit the real lib.
C="$FX/hooks-copy"; cp -RH "$H" "$C"; rm -f "$C/lib/review-state"
touch "$TESTS"
mkdir -p "$FX/kit"; GHX="$FX/kit/git-hooks"; cp -RP "$GH" "$GHX"; cp -RH "$C" "$FX/kit/hooks"
g add -A; g commit -qm pp-ml
out=$(gpush "$SID" "$REPO"); check "push gate without lib/review-state: refused" "$out" 'push-gate \(or a lib it reads\) is missing'
out=$( cd "$REPO" && env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0="$GHX" CLAUDECODE=1 AGENT_GIT_HOOKS_ENFORCE=1 git -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -q --allow-empty -m "no trailer" 2>&1; echo "rc=$?")
check "...a non-push git command (commit-msg, advisory) passes" "$out" 'rc=0'
unset GHX
g reset -q --soft HEAD~2
out=$(edit "$REPO/b.py" "$SID" | "$C/review-mark-changes"); check "marker hook without lib/review-state: tells the user" "$out" 'systemMessage'
rm -f "$C/lib/cmd-tokens"
out=$(pre "ls" "$SID" "$REPO" | "$C/bash-guards"); check "bash-guards without lib/cmd-tokens: deny" "$out" '"deny"'
rm -f "$TESTS"
# No jq: a PATH holding only what the hooks need before hook_lib looks for it.
NJ="$FX/nojq"; mkdir -p "$NJ"; ln -s /bin/bash /bin/cat "$NJ/"
out=$(pre "ls" "$SID" "$REPO" | PATH="$NJ" "$H/bash-guards"); check "no jq: bash-guards refuses a Bash call" "$out" '"deny"'
out=$(jq -cn --arg s "$SID" '{hook_event_name:"UserPromptSubmit",session_id:$s,prompt:"commit"}' | PATH="$NJ" "$H/commit-cohesion")
check "no jq: a UserPromptSubmit gets no PreToolUse deny" "$out" 'permissionDecision' absent
check "...but the user is told the gate is off" "$out" 'systemMessage.*commit-cohesion is off for UserPromptSubmit'

echo "--- commit-cohesion ---"
jq -cn --arg s "$SID" '{hook_event_name:"UserPromptSubmit",session_id:$s}' | "$H/commit-cohesion"
g add -A; g commit -qm "one"
out=$(pre "git commit -m two" "$SID" "$REPO" | "$H/commit-cohesion")
check "second unpushed commit: deny, amend remedy" "$out" 'One commit per push: fold this in with'
check "deny text drops the old instruction rule" "$out" 'does not earn a new commit' absent
out=$(pre "git -C \"$REPO\" commit -m two" "$SID" / | "$H/commit-cohesion")
check "quoted git -C path: same deny" "$out" 'One commit per push: fold this in with'
touch "$PRFILE"
out=$(pre "git commit -m two" "$SID" "$REPO" | "$H/commit-cohesion"); check "PR open: never amend" "$out" 'open PR.*never amend or rebase'
out=$(pre "git commit -m two" "$SID" "$REPO" '{"agent_id":"agent-1"}' | "$H/commit-cohesion"); check "subagent (agent_id) variant" "$out" 'you are a subagent'
out=$(pre "git commit --amend --no-edit" "$SID" "$REPO" | "$H/commit-cohesion"); check "PR open: amend of an unpushed commit denied" "$out" 'forbids --amend once a PR is open'
out=$(pre "git commit -m two # separate-change" "$SID" "$REPO" | "$H/commit-cohesion"); empty "separate-change passes" "$out"
out=$(pre "git commit -m 'two # separate-change'" "$SID" "$REPO" | "$H/commit-cohesion"); check "separate-change inside the message does not" "$out" '"deny"'
rm -f "$PRFILE"
out=$(pre "git commit --amend --no-edit" "$SID" "$REPO" | "$H/commit-cohesion"); empty "no PR: amend of an unpushed commit passes" "$out"
touch "$PRFILE"
g push -q 2>/dev/null
out=$(pre "git commit -m two" "$SID" "$REPO" | "$H/commit-cohesion"); empty "after a push a follow-up commit passes" "$out"
out=$(pre "git commit --amend --no-edit" "$SID" "$REPO" | "$H/commit-cohesion"); check "amend of a pushed commit: deny" "$out" 'already on a remote'
# bash-guards' amend guard compares HEAD with @{u}; a branch pushed without -u has none.
g checkout -q -b "$BR-2"; echo "y = 2" >> "$REPO/a.py"; g commit -qam three
g push -q origin "HEAD:refs/heads/$BR-2" 2>/dev/null
g rev-parse -q --verify '@{u}' >/dev/null 2>&1 && bad "fixture: $BR-2 should have no upstream"
out=$(pre "git commit --amend --no-edit" "$SID" "$REPO" | "$H/commit-cohesion"); check "amend of a commit pushed without upstream: deny" "$out" 'already on a remote'

echo "--- commit-cohesion and bash-guards: a repo with no remote (~/.agents, ~/.claude) ---"
LR="$FX/local"; git init -q -b main "$LR"
lg() { git -C "$LR" -c user.name=t -c user.email=t@t -c commit.gpgsign=false "$@"; }
echo a > "$LR/f"; lg add f; lg commit -qm one
jq -cn --arg s "$SID" '{hook_event_name:"UserPromptSubmit",session_id:$s}' | "$H/commit-cohesion"
post "git commit -m one" "$SID" "$LR" | "$H/commit-cohesion"
out=$(pre "git commit -m two" "$SID" "$LR" | "$H/commit-cohesion"); empty "no remote: a second commit in one turn passes" "$out"
out=$(pre "git commit --amend --no-edit" "$SID" "$LR" | "$H/commit-cohesion"); empty "no remote: an amend passes" "$out"
out=$(pre "git commit -m two" "$SID" "$LR" '{"agent_id":"agent-1"}' | "$H/commit-cohesion")
check "no remote: a subagent still may not commit onto the parent's HEAD" "$out" 'you are a subagent'
# B-8: `git -C "$WT"` names a repo cmd-repo cannot resolve; the no-remote cwd is only a guess.
out=$(pre 'git -C "$WT" commit -m two' "$SID" "$LR" | "$H/commit-cohesion"); check "no remote, \$var repo: a second commit in one turn denies" "$out" 'hides which repo'
out=$(pre "git -C \"$LR\" commit -m two" "$SID" / | "$H/commit-cohesion"); empty "...a literal no-remote repo still passes" "$out"
out=$(pre 'git -C "$WT" commit --amend --no-edit' "$SID" "$LR" | "$H/commit-cohesion")
check "R3-B-1: unknown amend repo denies" "$out" 'amend repository is unresolved'
out=$(pre 'git -C "$WT" commit -m two' "$SID" "$LR" | "$H/commit-cohesion")
check "R3-B-1: unknown repo remedy does not recommend amend" "$out" 'fold this in|commit --amend' absent
out=$(pre "cd \"\$X\" && git -C \"$LR\" commit -m two" "$SID" "$LR" | "$H/commit-cohesion")
empty "R3-B-2: absolute -C resolves an earlier unknown cd" "$out"
# A branch tracking a LOCAL branch has an @{u} that was never pushed anywhere.
lg branch -q --track feat main; lg checkout -q feat
out=$(pre "git commit --amend --no-edit" "$SID" "$LR" | "$H/bash-guards"); check "bash-guards: amend on a branch tracking a local branch passes" "$out" 'upstream tip' absent
g checkout -q "$BR"; out=$(pre "git commit --amend --no-edit" "$SID" "$REPO" | "$H/bash-guards")
check "bash-guards: amend at a remote upstream's tip still denies" "$out" 'upstream tip'
# B-6: the repo a `cd` moves to, not the session cwd's, decides.
out=$(pre "cd \"$LR\" && git commit --amend --no-edit" "$SID" "$REPO" | "$H/bash-guards"); check "bash-guards: cd <no-remote repo> && amend, from a cwd at its upstream tip, passes" "$out" 'upstream tip' absent
out=$(pre "cd \"$REPO\" && git commit --amend --no-edit" "$SID" "$LR" | "$H/bash-guards"); check "bash-guards: cd <repo at its upstream tip> && amend denies" "$out" 'upstream tip'
# R3-CX-1: quoted examples, an earlier plain commit, or a first local amend cannot select the repo.
for prefix in 'echo "git commit"' "git -C $LR commit --no-edit" "git -C $LR commit --amend --no-edit"; do
  out=$(pre "$prefix && git -C $REPO commit --amend --no-edit" "$SID" "$LR" | "$H/bash-guards")
  check "R3-CX-1: later pushed amend after $prefix denies" "$out" 'upstream tip'
done
out=$(pre "echo \"git -C $REPO commit --amend --no-edit\"" "$SID" "$LR" | "$H/bash-guards")
empty "R3-CX-1: a printed amend command is data" "$out"
g checkout -q "$BR-2"

echo "--- git pre-push: ticket sprawl ---"
# Once a PR is open the repo forbids amend, so each push adds a commit and the count must not apply.
g checkout -q -b "$BR-3"
for n in 1 2 3; do echo "s$n = $n" >> "$REPO/a.py"; g commit -qam "PROJ-1: part $n"; done
g push -q -u origin "$BR-3" 2>/dev/null
echo "s4 = 4" >> "$REPO/a.py"; g commit -qam "PROJ-1: part 4"
mkdir -p "$(dirname "$TESTS")"; touch "$TESTS"
PR3="$HOME/.claude/tmp/ci-branch-$BR-3.pr"
out=$(gpush "$SID" "$REPO"); check "4th ticket commit, no PR: sprawl deny" "$out" 'PROJ-1 already has 3 commits'
touch "$PR3"
out=$(gpush "$SID" "$REPO"); check "4th ticket commit, PR open: no sprawl deny" "$out" 'already has 3 commits' absent
rm -f "$PR3" "$TESTS"

echo "--- git pre-push: committed work in a second worktree ---"
# The base is per root: work committed in a worktree other than the session's first edit is sized.
REPO2="$FX/repo2"; git clone -q -b "$BR" "$REMOTE" "$REPO2" 2>/dev/null
g2() { git -C "$REPO2" -c user.name=t -c user.email=t@t -c commit.gpgsign=false "$@"; }
g2 checkout -q -b "$BR-4"; g2 push -q -u origin "$BR-4" 2>/dev/null
lines z 80 >> "$REPO2/z.py"; edit "$REPO2/z.py" "$SID" | "$H/review-mark-changes"
g2 add -A; g2 commit -qm "PROJ-2: z"
touch "$TESTS"
out=$(gpush "$SID" "$REPO2")
check "80 committed lines in a second worktree: refused" "$out" 'rc=1'
rm -f "$TESTS"
echo "--- git pre-push: a deferred round ---"
# The model may defer a round while its own edit agents run: the next push waits on it rather than
# stacking a second round, until REVIEW_PENDING_TTL says the round was abandoned.
RD="$FX/rd"; SD="$SID-d"; mkrepo "$RD" "$FX/rd.git"
workin "$RD" "$SD" a 200
out=$(issue "$RD" "$SD"); check "heavy round issued" "$out" 'subagent_type "thermo-quality"'
workin "$RD" "$SD" b 200
out=$(issue "$RD" "$SD"); check "deferred round: the next push waits for it" "$out" 'has not returned'
check "...and asks no second round" "$out" 'This refusal is the review trigger' absent
out=$(issue "$RD" "$SD" REVIEW_PENDING_TTL=0); check "an abandoned round (past the TTL) is re-issued" "$out" 'Review round 1 of'
check "...sized from the fork point" "$out" '~400 line delta'

echo "--- review-agent-mark: a reviewer that cd'd to another checkout ---"
RX="$FX/rx"; RY="$FX/ry"; SX="$SID-x"; mkrepo "$RX" "$FX/rx.git"; mkrepo "$RY"
workin "$RX" "$SX" c 60
out=$(issue "$RX" "$SX"); check "cd case: 60 lines -> a round" "$out" 'This refusal is the review trigger'
sstopin "$RY" "$SX" thermo-bugs
rs rv_pending "$SX" "$RX" && bad "a reviewer returning from another checkout completes the session's one round" \
  || ok "a reviewer returning from another checkout completes the session's one round"
out=$(gpush "$SX" "$RX"); check "...so the next push asks for nothing" "$out" 'review trigger|has not returned' absent

echo "--- review-agent-mark: handback reports, agent map, foreground results ---"
# Since ~2026-09-29 review agents end with a SubagentHandback tool call: SubagentStop then carries
# agent_type "" and an empty (or interim) last_assistant_message, and no round ever completed.
RH="$FX/rh"; SH="$SID-h"; mkrepo "$RH" "$FX/rh.git"
TR="$FX/transcripts"; mkdir -p "$TR"
# sstopx <sid> <agent_id> <agent_type> <message> <transcript>: a SubagentStop with a transcript path,
# returning from $XD (default $RH).
sstopx() { jq -cn --arg d "${XD:-$RH}" --arg s "$1" --arg i "$2" --arg t "$3" --arg m "$4" --arg p "$5" '{hook_event_name:"SubagentStop",session_id:$s,cwd:$d,agent_id:$i,agent_type:$t,stop_hook_active:false,last_assistant_message:$m,agent_transcript_path:$p}' | "$H/review-agent-mark"; }
# spawn <sid> <type> <status> <agent_id> [report]: PostToolUse(Agent), content blocks as the tool returns them.
spawn() { jq -cn --arg d "${XD:-$RH}" --arg s "$1" --arg t "$2" --arg st "$3" --arg i "$4" --arg r "${5:-}" '{hook_event_name:"PostToolUse",tool_name:"Agent",session_id:$s,cwd:$d,tool_input:{subagent_type:$t,prompt:"x"},tool_response:({status:$st,agentId:$i} + (if $r == "" then {} else {content:[{type:"text",text:$r}]} end))}' | "$H/review-agent-mark"; }
handback() { jq -cn --arg m "$2" '{type:"assistant",message:{content:[{type:"tool_use",name:"SubagentHandback",input:{message:$m}}]}}' > "$TR/$1.jsonl"; }
rounds_of() { ( . "$H/lib/review-state" 2>/dev/null && _rv_load "$1" && _rv_get r rounds; echo "${r:-0}" ); }
issue_round() { workin "${3:-$RH}" "$1" "$2" 60; issue "${3:-$RH}" "$1" >/dev/null; }
issue_round "$SH" h1
spawn "$SH" thermo-bugs async_launched ah1
handback ah1 "1. a.py:3 off by one"
sstopx "$SH" ah1 "" "" "$TR/ah1.jsonl"
rs rv_pending "$SH" "$RH" && bad "empty message + handback in the transcript completes the round" \
  || ok "empty message + handback in the transcript completes the round"
rs rv_tally_owed "$SH" && ok "...and the handback's findings owe a TALLY" || bad "...and the handback's findings owe a TALLY"
rs rv_tally_settle "$SH"
issue_round "$SH" h2
spawn "$SH" thermo-bugs async_launched ah2
printf '%s\n' '{"type":"user"}' > "$TR/ah2.jsonl"
sstopx "$SH" ah2 "" "No findings." "$TR/ah2.jsonl"
rs rv_pending "$SH" "$RH" && bad "empty agent_type resolved from the spawn map" || ok "empty agent_type resolved from the spawn map"
issue_round "$SH" h3
spawn "$SH" thermo-bugs async_launched ah3
printf '%s\n' '{"type":"user"}' > "$TR/ah3.jsonl"
sstopx "$SH" ah3 "" "" "$TR/ah3.jsonl"
rs rv_pending "$SH" "$RH" && ok "interrupted agent (no message, no handback) completes nothing" \
  || bad "interrupted agent (no message, no handback) completes nothing"
sstopx "$SH" ah9 "" "No findings." "$TR/ah3.jsonl"
rs rv_pending "$SH" "$RH" && ok "unknown agent id with no type completes nothing" || bad "unknown agent id with no type completes nothing"
before=$(rounds_of "$SH")
spawn "$SH" thermo-bugs completed ah4 "No findings."
rs rv_pending "$SH" "$RH" && bad "foreground completion from the tool result" || ok "foreground completion from the tool result"
sstopx "$SH" ah4 thermo-bugs "No findings." "$TR/ah3.jsonl"
[ "$(rounds_of "$SH")" = $((before + 1)) ] && ok "...counted once though SubagentStop fires too" \
  || bad "...counted once though SubagentStop fires too" "rounds $(rounds_of "$SH"), was $before"
# SubagentStop can arrive before PostToolUse(Agent): no spawn map entry, agent_type "", so the type
# comes from the transcript's .meta.json.
issue_round "$SH" h4
handback ah8 "No findings."; printf '%s\n' '{"agentType":"thermo-bugs"}' > "$TR/ah8.meta.json"
sstopx "$SH" ah8 "" "" "$TR/ah8.jsonl"
rs rv_pending "$SH" "$RH" && bad "SubagentStop before the spawn's PostToolUse: type from .meta.json" \
  || ok "SubagentStop before the spawn's PostToolUse: type from .meta.json"
before=$(rounds_of "$SH")
spawn "$SH" thermo-bugs completed ah8 "No findings."
[ "$(rounds_of "$SH")" = "$before" ] && ok "...and the late PostToolUse counts nothing more" || bad "...and the late PostToolUse counts nothing more"
RH2="$FX/rh2"; mkrepo "$RH2" "$FX/rh2.git"
issue_round "$SH" h5; issue_round "$SH" h5 "$RH2"
{ rs rv_pending "$SH" "$RH" && rs rv_pending "$SH" "$RH2"; } && ok "two rounds pending" || bad "two rounds pending"
spawn "$SH" thermo-bugs completed ah5 "No findings."
{ ! rs rv_pending "$SH" "$RH" && rs rv_pending "$SH" "$RH2"; } && ok "a return closes only its own root's round" \
  || bad "a return closes only its own root's round"
issue_round "$SH" h6
XD="$RY" spawn "$SH" thermo-bugs completed ah10 "No findings."
{ rs rv_pending "$SH" "$RH" && rs rv_pending "$SH" "$RH2"; } && ok "two pending, a return from a third root: neither closes" \
  || bad "two pending, a return from a third root: neither closes"
DBG="$FX/agent-state"
AGENT_STATE_DIR="$DBG" REVIEW_DEBUG=1 sstopx "$SH" ah6 "" "" "$TR/ah3.jsonl"
grep -q '"agent_id":"ah6"' "$DBG/review-debug.log" 2>/dev/null && ok "REVIEW_DEBUG=1 logs the payload" || bad "REVIEW_DEBUG=1 logs the payload"
AGENT_STATE_DIR="$DBG" sstopx "$SH" ah7 "" "" "$TR/ah3.jsonl"
grep -q ah7 "$DBG/review-debug.log" 2>/dev/null && bad "no log without REVIEW_DEBUG" || ok "no log without REVIEW_DEBUG"

echo "--- merged-in upstream is not this session's delta ---"
RMT="$FX/rm.git"; RA="$FX/ra"; RT="$FX/rt"; SM="$SID-m"
git init -q --bare "$RMT"; mkrepo "$RA" "$RMT"; git clone -q -b main "$RMT" "$RT" 2>/dev/null
ga() { git -C "$RA" -c user.name=t -c user.email=t@t -c commit.gpgsign=false "$@"; }
gt() { git -C "$RT" -c user.name=u -c user.email=u@u -c commit.gpgsign=false "$@"; }
teammate() { lines "$1" "$2" > "$RT/$1.py"; gt add -A; gt commit -qm "$1"; gt push -q origin main 2>/dev/null; }
merge_up() { ga fetch -q origin; ga merge -q --no-ff --no-edit origin/main; }
workin "$RA" "$SM" own 25
out=$(stopin "$RA" "$SM"); check "merge fixture: own work self-checked" "$out" 'SELF-CHECK'
stopin "$RA" "$SM" true >/dev/null
ga add -A; ga commit -qm own; teammate team 120; merge_up
workin "$RA" "$SM" post 20
out=$(stopin "$RA" "$SM"); check "after a merge only the session's 20 lines count" "$out" '~20 line delta'
check "...a self-check, not a reviewer for the teammate's 120" "$out" 'SELF-CHECK'
check "...and the scope names the merged-in side" "$out" 'merged-in [0-9a-f]{12} applied'
stopin "$RA" "$SM" true >/dev/null
ga add -A; ga commit -qm post
( . "$H/lib/review-state" && rv_snapshot "$RA" && rv_rec_update "$RA" main '.rounds = 1 | .reviewed_tree = $t | .reviewed_head = $h' --arg t "$RV_TREE" --arg h "$RV_HEAD" )
workin "$RA" "$SM" own2 50; ga add -A; ga commit -qm own2; teammate team2 100; merge_up
out=$(gpush "$SM" "$RA"); check "push after a merge: work committed before it still counts" "$out" '~50 line delta'
check "...a round for those 50, not the teammate's 100 too" "$out" 'This refusal is the review trigger'
check "...and the scope names the merged-in side" "$out" 'merged-in [0-9a-f]{12} applied'

echo "--- comment-guard ---"
cg() { python3 "$H/comment-guard"; }
cedit() { jq -cn --arg f "$1" --arg o "$2" --arg n "$3" '{hook_event_name:"PostToolUse",tool_name:"Edit",tool_input:{file_path:$f,old_string:$o,new_string:$n}}'; }
cwrite() { jq -cn --arg f "$1" --arg c "$2" '{hook_event_name:"PostToolUse",tool_name:"Write",tool_input:{file_path:$f,content:$c}}'; }
P="$REPO/m.py"
six=$(printf '# c%s\n' 1 2 3 4 5 6)
out=$(cedit "$P" "a=1" "$six
# noqa
a=1" | cg)
check "6 comments nudge, noqa excluded" "$out" 'added 6 comment'
check "nudge states its thresholds" "$out" 'first 6 of each docstring; this fires at 6'
check "nudge drops the one-line-docstring rule" "$out" 'docstring is ONE line' absent
out=$(cedit "$P" "a=1" "# one
# two
a=1" | cg); empty "2 comments are under the threshold" "$out"
out=$(cedit "$FX/x.yaml" "a: 1" "$six
a: 1" | cg); empty "yaml is ignored" "$out"
out=$(cedit "/private/tmp/claude-1/scratchpad/x.py" "a=1" "$six
a=1" | cg); empty "scratchpad path is skipped" "$out"
out=$(cedit "$FX/cs/tasks/T-1-x/scripts/x.py" "a=1" "$six
a=1" | CLAUDE_OUT_ROOT=$FX/cs cg); empty "task folder script is skipped" "$out"
out=$(cedit "$FX/cs/tasks/T-1-x/worktrees/w/m.py" "a=1" "$six
a=1" | CLAUDE_OUT_ROOT=$FX/cs cg); check "task folder worktree is still checked" "$out" 'added 6 comment'
out=$(cedit "$TMPDIR/x.py" "a=1" "$six
a=1" | cg); empty "TMPDIR path is skipped" "$out"
svg=$(printf 'ICON = """<svg>\n'; i=0; while [ $i -lt 20 ]; do echo "  <path d=\"M$i\"/>"; i=$((i+1)); done; printf '</svg>"""\n')
out=$(cwrite "$P" "$svg" | cg); empty "20-line SVG literal is data, not a docstring" "$out"
sql=$(printf '    q = """\n'; i=0; while [ $i -lt 15 ]; do echo "    SELECT c$i"; i=$((i+1)); done; printf '    """\n')
out=$(cedit "$P" "    q = 1" "$sql" | cg); empty "SQL in a fragment is data" "$out"
doc=$(printf 'def f():\n    """Line.\n'; i=0; while [ $i -lt 12 ]; do echo "    more $i"; i=$((i+1)); done; printf '    """\n    return 1\n')
out=$(cwrite "$P" "$doc" | cg); check "14-line function docstring nudges" "$out" 'added 8 comment'
frag=$(printf '    def f(self):\n        """Line.\n'; i=0; while [ $i -lt 12 ]; do echo "        more $i"; i=$((i+1)); done; printf '        """\n')
out=$(cedit "$P" "    def f(self):" "$frag" | cg); check "unparseable fragment docstring after a def header nudges" "$out" 'added 8 comment'
short=$(printf 'def f():\n    """One.\n\n    Two.\n    Three.\n    Four.\n    """\n')
out=$(cwrite "$P" "$short" | cg); empty "6-line docstring is free" "$out"

echo "--- overlay PROTOTYPE_DIRS: review-mark-changes, comment-guard ---"
# The overlay's extra sandbox dir behaves like prototypes/; with no overlay it is ordinary code.
PX="$FX/proto"; mkrepo "$PX"; mkdir -p "$PX/playground"
printf 'PROTOTYPE_DIRS=playground\n' > "$FX/kit.env"
edit "$PX/playground/p.py" "$SID-px" | KIT_ENV="$FX/kit.env" "$H/review-mark-changes"
rs rv_edited "$SID-px" "$PX" && bad "overlay PROTOTYPE_DIRS path marked for review" || ok "overlay PROTOTYPE_DIRS path not marked"
edit "$PX/playground/p.py" "$SID-px" | KIT_ENV=/dev/null "$H/review-mark-changes"
rs rv_edited "$SID-px" "$PX" && ok "no overlay: the same path is code and marked" || bad "no overlay: the same path not marked"
out=$(cedit "$PX/playground/m.py" "a=1" "$six
a=1" | KIT_ENV="$FX/kit.env" cg); empty "overlay PROTOTYPE_DIRS path gets no comment nudge" "$out"
out=$(cedit "$PX/playground/m.py" "a=1" "$six
a=1" | KIT_ENV=/dev/null cg); check "no overlay: the same path nudges" "$out" 'added 6 comment'
# The missing-lib policy for the Python hooks: without kit_env.py they say they are off, never
# silently run without the overlay.
rm -f "$C/lib/kit_env.py"
out=$(cedit "$REPO/m.py" "a=1" "$six
a=1" | python3 "$C/comment-guard"); check "comment-guard without lib/kit_env.py: tells the user it is off" "$out" 'systemMessage.*comment-guard is off'
out=$(jq -cn --arg p "$FX/no-transcript.jsonl" '{transcript_path:$p}' | python3 "$C/session-digest")
check "session-digest without lib/kit_env.py: tells the user it is off" "$out" 'systemMessage.*session-digest is off'

echo "--- python-format ---"
PY="$REPO/fmt.py"; echo "x=1" > "$PY"
rm -f "$HOME/.claude/tmp/pyright-$SID.txt"
jq -cn --arg f "$PY" --arg s "$SID" '{hook_event_name:"PostToolUse",tool_name:"Write",session_id:$s,tool_input:{file_path:$f}}' | "$H/python-format"
[ -f "$HOME/.claude/tmp/pyright-$SID.txt" ] && bad "python-format no longer logs paths for pyright" || ok "python-format no longer logs paths for pyright"
rm -f "$HOME/.claude/tmp/pyright-$SID.txt"

echo "--- review-state: concurrent writers ---"
# SubagentStop (rv_complete) and Stop or pre-push (_rv_round, rv_chain_*) write one session file
# at once. Separate processes, as the hooks are: a subshell shares $$ and so the writers' tmp file.
SC="$SID-c"
writer() {
  /bin/bash -c '. "$1" || exit 1; i=0; while [ $i -lt 30 ]; do _rv_set "$2" "$3$i=1"; i=$((i+1)); done' \
    _ "$H/lib/review-state" "$SC" "$1"
}
writer a & writer b & writer c & wait
n=$(grep -c '=1$' "$R/session-$SC" 2>/dev/null)
[ "${n:-0}" = 90 ] && ok "three concurrent writers keep all 90 keys" || bad "three concurrent writers kept ${n:-0} of 90 keys"
[ -e "$R/session-$SC.lock" ] && bad "a writer left its lock behind" || ok "every writer released its lock"
SL="$SID-l"; /bin/bash -c 'exit 0' & dead=$!; wait "$dead"
mkdir -p "$R/session-$SL.lock"; echo "$dead" > "$R/session-$SL.lock/pid"
rs _rv_set "$SL" k=1
grep -qx 'k=1' "$R/session-$SL" 2>/dev/null && [ ! -e "$R/session-$SL.lock" ] \
  && ok "a lock left by a dead writer is broken" || bad "a lock left by a dead writer wedged the write"
# Two waiters judge the same dead lock stale; the first breaks and retakes it before the second acts.
# The liveness check's `kill` stages the first waiter's retake (owner: this live shell), so the
# second must leave that lock alone rather than remove it and take a second copy.
SR="$SID-r"
mkdir -p "$R/session-$SR.lock"; echo "$dead" > "$R/session-$SR.lock/pid"
got=$(/bin/bash -c '. "$1" || exit 1; l="$RV_DIR/session-$2.lock" owner=$3
  kill() { if [ -z "${staged:-}" ]; then staged=1; rm -rf "$l"; mkdir "$l"; echo "$owner" > "$l/pid"; fi; builtin kill "$@"; }
  if _rv_lock "$2"; then echo took; else echo waited; fi' _ "$H/lib/review-state" "$SR" "$$")
[ "$got" = waited ] && [ "$(cat "$R/session-$SR.lock/pid" 2>/dev/null)" = "$$" ] && [ ! -e "$R/session-$SR.break.lock" ] \
  && ok "a stale-lock break leaves a lock another waiter just retook" \
  || bad "a stale-lock break removed a retaken lock: $got, owner $(cat "$R/session-$SR.lock/pid" 2>/dev/null)"
rm -rf "$R/session-$SR.lock"
# The same race unstaged: four writers queue behind a dead lock and reach the break together.
SC="$SID-s"
mkdir -p "$R/session-$SC.lock"; echo "$dead" > "$R/session-$SC.lock/pid"
qw() { /bin/bash -c '. "$1" || exit 1; i=0; while [ $i -lt 10 ]; do _rv_set "$2" "$3$i=1"; i=$((i+1)); done' _ "$H/lib/review-state" "$SC" "$1"; }
qw a & qw b & qw c & qw d & wait
n=$(grep -c '=1$' "$R/session-$SC" 2>/dev/null)
[ "${n:-0}" = 40 ] && ok "four writers behind a dead lock keep all 40 keys" || bad "four writers behind a dead lock kept ${n:-0} of 40 keys"

echo "--- review-cross: the second reviewer, through agent-run ---"
# Round 1 is thermo-bugs plus review-cross; later rounds review-cross on the delta, thermo-bugs back
# on a sensitive path. bin/agent-run runs a stand-in codex (CODEX_BIN) here: the real CLI spends
# quota, and its real run is in the codex-reviewer phase notes.
CR="$H/../bin/agent-run"; FK="$FX/fake-codex"
printf '%s\n' '#!/bin/bash' 'o=; while [ $# -gt 0 ]; do case $1 in -o) o=$2; shift 2 ;; *) shift ;; esac; done' \
  'cat >/dev/null' '[ -z "${FAKE_JSON:-}" ] || cat "$FAKE_JSON" > "$o"' 'exit "${FAKE_RC:-0}"' > "$FK"; chmod +x "$FK"
printf '%s\n' '{"verdict":"needs-attention","summary":"s","findings":[{"id":"x","severity":"high","title":"t1","body":"b","file":"a.py","line_start":1,"line_end":1,"confidence":0.9,"recommendation":"r"},{"id":"y","severity":"low","title":"t2","body":"b","file":"a.py","line_start":1,"line_end":1,"confidence":0.5,"recommendation":"r"}],"next_steps":[]}' > "$FX/cx2.json"
printf '%s\n' '{"verdict":"approve","summary":"ok","findings":[],"next_steps":[]}' > "$FX/cx0.json"
# cxr <sid> [VAR=value...]: run agent-run review-cross on $RC with the stand-in; its output and rc on one line.
# CRR: the cross reviewer's command, and CXT its agent type, as this copy of the kit names them
# (codex-review and codex before the roles rename), so the RC-CX cases run against the old hooks too.
if [ -x "$CR" ]; then CRR=("$CR" review-cross --timeout 10); CXT=review-cross; else CRR=("$H/../bin/codex-review"); CXT=codex; fi
cxr() { local s=$1; shift; { env CLAUDE_CODE_SESSION_ID="$s" AGENT_SESSION_ID= CODEX_BIN="$FK" "$@" "${CRR[@]}" "$RC" 2>&1; echo "rc=$?"; } | tr '\n' ' '; }
# A new branch, never pushed: round 1 is the whole PR, from the fork point.
RC="$FX/rc"; SC2="$SID-c"; BC=cxb; mkrepo "$RC" "$FX/rc.git"; git -C "$RC" checkout -qb "$BC"; git -C "$RC" config push.default current
workin "$RC" "$SC2" a 60
out=$(issue "$RC" "$SC2" CODEX_BIN="$FK" | tr '\n' ' ')
check "round 1: thermo-bugs and review-cross in one message" "$out" 'ONE message, in the FOREGROUND.*subagent_type "thermo-bugs".*agent-run review-cross'
check "...review-cross through Bash in the background, on the whole PR" "$out" 'run Codex through Bash with run_in_background: true \(up to ~15 min[^`]*`[^`]*/bin/agent-run review-cross [^ `]+ --objective'
check "...its findings come from its file with their CX- ids, and the round waits for every reviewer" "$out" 'completes when every correctness reviewer has returned.*CX- ids'
check "...the fixer instruction: round 1 is thermo-bugs' AND Codex's" "$out" "round 1: thermo-bugs' AND Codex's\)"
[ "$(rec "$RC" "$BC" '.agents | join(" ")')" = "thermo-bugs thermo-quality review-cross" ] \
  && ok "...the record lists review-cross among the round's agents" || bad "...the record lists review-cross among the round's agents" "$(rec "$RC" "$BC" .)"
out=$(gpush "$SC2" "$RC" CODEX_BIN="$FK"); check "round in flight: the wait names the agent-run command" "$out" 'review-cross is not an Agent: run `[^`]*/bin/agent-run review-cross'
out=$(cxr "$SC2" FAKE_JSON="$FX/cx2.json")
check "agent-run review-cross: every finding gets a CX- id" "$out" 'CX-1 \[high 0.9\] t1 .*CX-2 \[low 0.5\] t2 .*rc=0'
check "...and prints the findings file for whoever fixes" "$out" 'verbatim for whoever fixes[^:]*: [^ ]+\.md'
cxout=$(printf '%s' "$out" | grep -oE 'JSON: [^ ]+' | sed 's/^JSON: //')
[ "$(jq -c '[.findings[].id]' "$cxout" 2>/dev/null)" = '["CX-1","CX-2"]' ] && ok "...in the JSON too" || bad "...in the JSON too" "$(cat "$cxout" 2>/dev/null)"
rs rv_pending "$SC2" "$RC" && ok "review-cross first: the round waits for thermo-bugs" || bad "review-cross first: the round waits for thermo-bugs"
[ "$(rec "$RC" "$BC" '"\(.rounds // 0) \(.pending_tree | length) \(.reviews[-1].agent) \(.reviews[-1].findings)"')" = "0 40 review-cross 2" ] \
  && ok "...the record keeps the round pending and logs the codex review, 2 findings" || bad "...the record keeps the round pending and logs the codex review, 2 findings" "$(rec "$RC" "$BC" .)"
rs rv_tally_owed "$SC2" && ok "...and its findings owe the TALLY" || bad "...and its findings owe the TALLY"
sstopin "$RC" "$SC2" thermo-bugs
[ "$(rec "$RC" "$BC" '"\(.rounds) \(.pending_tree)"')" = "1 null" ] && ok "thermo-bugs returning second completes round 1" || bad "thermo-bugs returning second completes round 1" "$(rec "$RC" "$BC" .)"
( . "$H/lib/review-state" && rv_tally_owed "$SC2" && [ "$RV_OWED" = "review-cross thermo-bugs" ] ) && ok "round 1: the TALLY owed covers both reports" || bad "round 1: the TALLY owed covers both reports"
rs rv_tally_settle "$SC2"
sstopin "$RC" "$SC2" thermo-quality
workin "$RC" "$SC2" b 60
out=$(issue "$RC" "$SC2" CODEX_BIN="$FK")
check "round 2: review-cross on the delta" "$out" 'Review round 2 of.*/bin/agent-run review-cross [^ `]+ --since [0-9a-f]{40} '
check "...with no review Agent (non-sensitive delta, thermo-quality done)" "$out" 'subagent_type "(thermo|reviewer)' absent
out=$(cxr "" FAKE_JSON="$FX/cx0.json")
check "agent-run with no session (another host): no findings" "$out" 'approve, 0 finding\(s\).*round 2'
[ "$(rec "$RC" "$BC" '"\(.rounds) \(.pending_tree)"')" = "2 null" ] \
  && ok "...completes the branch's pending round in the record" || bad "...completes the branch's pending round in the record" "$(rec "$RC" "$BC" .)"
out=$(gpush "$SC2" "$RC" CODEX_BIN="$FK"); check "...so the push asks for no review" "$out" 'review trigger|has not returned' absent
out=$(cxr "" FAKE_JSON="$FX/cx0.json")
[ "$(rec "$RC" "$BC" '"\(.rounds) \(.reviews | length)"')" = "2 3" ] \
  && ok "a second codex run on a reviewed tree adds no round" || bad "a second codex run on a reviewed tree adds no round" "$(rec "$RC" "$BC" .)"
out=$(cxr "" FAKE_RC=1); check "Codex failing: non-zero, says nothing was recorded" "$out" 'nothing recorded.*rc=4'
out=$(cxr "" CODEX_BIN="$FX/no-codex"); check "no Codex CLI: exit 3, names the fallback" "$out" 'not on PATH.*thermo-bugs.*rc=3'
printf '%s\n' '{"verdict":"maybe"}' > "$FX/cxbad.json"
out=$(cxr "" FAKE_JSON="$FX/cxbad.json"); check "an answer off the schema: exit 5" "$out" 'does not match.*rc=5'
[ "$(rec "$RC" "$BC" '"\(.rounds) \(.reviews | length)"')" = "2 3" ] && ok "...and failures record nothing" || bad "...and failures record nothing" "$(rec "$RC" "$BC" .)"
mkdir -p "$RC/models"; workin "$RC" "$SC2" models/tables 60
out=$(issue "$RC" "$SC2" CODEX_BIN="$FK")
check "a sensitive delta (models/tables.py): thermo-bugs joins review-cross" "$out" 'Review round 3 of.*subagent_type "thermo-bugs".*agent-run review-cross'
out=$(cxr "$SC2" FAKE_JSON="$FX/cx0.json")
out=$(gpush "$SC2" "$RC" CODEX_BIN="$FK"); check "codex returned, thermo-bugs still out: the round waits" "$out" 'has not returned'
sstopin "$RC" "$SC2" thermo-bugs "No findings."
workin "$RC" "$SC2" c 60
out=$(issue "$RC" "$SC2" CODEX_BIN="$FX/no-codex")
check "no Codex CLI on the push: thermo-bugs takes review-cross's place" "$out" 'Review round 4 of.*subagent_type "thermo-bugs"'
check "...and no agent-run command" "$out" 'agent-run' absent
# _rv_sensitive by file name: schema files and fee modules, not their look-alikes.
RS="$FX/rs"; mkrepo "$RS"
for f in q.sql models/tables.py tables_v2.py svc/shipping_fee.py domain/Fees.ts feed.py FeedbackForm.tsx tablet.py; do
  case $f in */*) mkdir -p "$RS/${f%/*}" ;; esac
  printf 'x\n' > "$RS/$f"
  got=$( ( . "$H/lib/review-state" && _rv_sensitive "$RS" HEAD HEAD && echo yes || echo no ) )
  case $f in feed.py|FeedbackForm.tsx|tablet.py) want=no ;; *) want=yes ;; esac
  [ "$got" = "$want" ] && ok "_rv_sensitive $f: $want" || bad "_rv_sensitive $f: want $want, got $got"
  rm -f "$RS/$f"
done

echo "--- tally owed: a TALLY line settles it ---"
# The critic is retired: a review agent's findings owe a TALLY line, which the final message (or a
# fix worker's report) carries; without one the Stop blocks once.
mkfeat() { mkrepo "$1" "$1.git"; git -C "$1" checkout -qb "$2"; git -C "$1" config push.default current; }
# stopmsg <sid> <cwd> <final message> [transcript]: a Stop payload as Claude sends it.
stopmsg() { jq -cn --arg s "$1" --arg d "$2" --arg m "$3" --arg p "${4:-}" '{hook_event_name:"Stop",session_id:$s,cwd:$d,stop_hook_active:false,last_assistant_message:$m,transcript_path:$p}' | "$H/review-trigger"; }
# realpush <repo> <refspec>: a push outside the agent layer (the remote moves; nothing is gated).
realpush() { env -u CLAUDECODE -u AI_AGENT -u AGENT_HOST git -C "$1" push -q origin "$2" 2>/dev/null; git -C "$1" fetch -q origin 2>/dev/null; }
RT2="$FX/rt2"; ST="$SID-t"; mkfeat "$RT2" ft
issue_round "$ST" t1 "$RT2"; sstopin "$RT2" "$ST" thermo-bugs "B-1 a.py:1 off by one"
out=$(stopmsg "$ST" "$RT2" "Fixed B-1.
- TALLY B-1=FIXED B-2=NOT_REPRODUCED")
empty "tally owed: a final message with a TALLY line settles it, no block" "$out"
out=$(stopmsg "$ST" "$RT2" "done"); empty "...and nothing is owed after it" "$out"
issue_round "$ST" t2 "$RT2"; sstopin "$RT2" "$ST" thermo-bugs "B-1 a.py:1 off by one"
printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"text","text":"TALLY B-1=OPTION"}]}}' > "$FX/tt2.jsonl"
out=$(stopmsg "$ST" "$RT2" "" "$FX/tt2.jsonl"); empty "...read from the transcript when the payload has no final message" "$out"
issue_round "$ST" t3 "$RT2"; sstopin "$RT2" "$ST" thermo-bugs "B-1 a.py:1 off by one"
out=$(stopmsg "$ST" "$RT2" "TALLY: 1 confirmed, 0 false positive"); check "the critic's old TALLY: summary is not a TALLY line: blocks" "$out" 'no TALLY line closed them'
issue_round "$ST" t4 "$RT2"; sstopin "$RT2" "$ST" thermo-bugs "B-1 a.py:1 off by one"
jq -cn --arg s "$ST" --arg d "$RT2" '{hook_event_name:"PostToolUse",tool_name:"Agent",session_id:$s,cwd:$d,tool_input:{subagent_type:"worker",prompt:"x"},tool_response:{status:"completed",content:[{type:"text",text:"Fixed.\nTALLY B-1=FIXED"}]}}' | "$H/review-agent-mark"
out=$(stopmsg "$ST" "$RT2" "done"); empty "a fix worker's report with a TALLY line settles it" "$out"

echo "--- a Stop during a pending push round ---"
R4="$FX/r4"; S4="$SID-4"; mkfeat "$R4" f4
workin "$R4" "$S4" a 25; stopin "$R4" "$S4" >/dev/null; stopin "$R4" "$S4" true >/dev/null
workin "$R4" "$S4" b 20; out=$(issue "$R4" "$S4"); check "fixture: 45 lines on the branch: refused" "$out" 'This refusal is the review trigger'
workin "$R4" "$S4" c 15; out=$(stopin "$R4" "$S4")
empty "a Stop while the push round is pending asks no self-check" "$out"
sstopin "$R4" "$S4" thermo-bugs "No findings."
out=$(gpush "$S4" "$R4"); check "...so its review's return completes the round: the push does not wait" "$out" 'has not returned' absent

echo "--- a pending round taken on by another session ---"
R5="$FX/r5"; A5="$SID-5a"; B5="$SID-5b"; mkfeat "$R5" f5
workin "$R5" "$A5" a 60; out=$(issue "$R5" "$A5"); check "fixture: session A refused" "$out" 'This refusal is the review trigger'
out=$(gpush "$B5" "$R5"); check "session B (a handoff) waits on A's round" "$out" 'has not returned'
sstopin "$R5" "$B5" thermo-bugs "No findings."
out=$(gpush "$B5" "$R5"); check "...and B's own review's return completes it" "$out" 'has not returned' absent
sstopin "$R5" "$A5" thermo-bugs "No findings."
[ "$(rec "$R5" f5 .rounds)" = 1 ] && ok "...A's late return adds no second round" || bad "...A's late return adds no second round" "$(rec "$R5" f5 .)"

echo "--- a feature merged into origin/staging has not merged ---"
R6="$FX/r6"; S6="$SID-6"; mkfeat "$R6" f6; realpush "$R6" main:staging
workin "$R6" "$S6" a 60; out=$(issue "$R6" "$S6"); check "fixture: round 1 on f6" "$out" 'Review round 1 of'
sstopin "$R6" "$S6" thermo-bugs "No findings."; sstopin "$R6" "$S6" thermo-quality "No findings."
realpush "$R6" f6:staging
workin "$R6" "$S6" b 60; out=$(issue "$R6" "$S6")
check "after f6 merged into origin/staging: round 2, the count kept" "$out" 'Review round 2 of'
check "...and thermo-quality is not asked again" "$out" 'thermo-quality' absent

echo "--- the anchor: small pushes before any round add up ---"
R3="$FX/r3"; S3="$SID-3"; mkfeat "$R3" f3
workin "$R3" "$S3" a 30; out=$(issue "$R3" "$S3"); check "fixture: a 30-line first push asks no review round" "$out" 'review trigger|has not returned' absent
realpush "$R3" f3
workin "$R3" "$S3" b 30; out=$(issue "$R3" "$S3")
check "the second 30-line push is sized from the anchor, not the moved remote: round 1" "$out" 'Review round 1 of'
check "...on all 60 lines" "$out" '~60 line delta'
R3b="$FX/r3b"; mkfeat "$R3b" f3b
workin "$R3b" "$S3" a 30; out=$(issue "$R3b" "$S3" AGENT_PUSH_NOW="wip share"); check "fixture: AGENT_PUSH_NOW push passes" "$out" 'rc=0'
realpush "$R3b" f3b
workin "$R3b" "$S3" b 30; out=$(issue "$R3b" "$S3")
check "after an AGENT_PUSH_NOW push the next push still counts its lines: round 1 on 60" "$out" 'Review round 1 of.*~60 line delta|~60 line delta.*Review round 1 of'

echo "--- thermo-quality returning to another checkout's cwd ---"
R11="$FX/r11"; W11="$FX/w11"; S11="$SID-11"; mkrepo "$R11" "$FX/r11.git"
git -C "$R11" worktree add -q -b f11 "$W11" 2>/dev/null; git -C "$W11" config push.default current
workin "$W11" "$S11" a 60; out=$(issue "$W11" "$S11"); check "fixture: a round in the worktree" "$out" 'subagent_type "thermo-quality"'
sstopin "$R11" "$S11" thermo-bugs "No findings."; sstopin "$R11" "$S11" thermo-quality "No findings."
[ "$(rec "$W11" f11 .quality_done)" = true ] && ok "thermo-quality after thermo-bugs, cwd in the main checkout: quality_done kept" \
  || bad "thermo-quality after thermo-bugs, cwd in the main checkout: quality_done kept" "$(rec "$W11" f11 .)"

echo "--- review-cross: completion is per reviewer and per snapshot ---"
R7="$FX/r7"; S7="$SID-7"; mkfeat "$R7" f7
workin "$R7" "$S7" a 60; out=$(issue "$R7" "$S7" CODEX_BIN="$FK"); check "fixture: round 1 with review-cross" "$out" 'agent-run review-cross'
sstopin "$R7" "$S7" thermo-bugs "No findings."; sstopin "$R7" "$S7" thermo-quality "No findings."
out=$(gpush "$S7" "$R7" CODEX_BIN="$FK"); check "thermo-bugs alone does not complete a round Codex is in" "$out" 'has not returned'
out=$(RC="$R7" cxr "$S7" FAKE_JSON="$FX/cx0.json")
out=$(gpush "$S7" "$R7" CODEX_BIN="$FK"); check "...Codex returning completes it" "$out" 'has not returned' absent
workin "$R7" "$S7" b 60; out=$(issue "$R7" "$S7" CODEX_BIN="$FK"); check "fixture: round 2, review-cross only" "$out" 'Review round 2 of'
out=$(RC="$R7" cxr "$S7" FAKE_RC=1); check "Codex failing in a session's round: thermo-bugs takes its place" "$out" 'thermo-bugs.*rc=4'
sstopin "$R7" "$S7" thermo-bugs "No findings."
out=$(gpush "$S7" "$R7" CODEX_BIN="$FK"); check "...and its return completes the round" "$out" 'has not returned' absent
R8="$FX/r8"; S8="$SID-8"; mkfeat "$R8" f8
workin "$R8" "$S8" a 60; issue "$R8" "$S8" CODEX_BIN="$FK" >/dev/null
t1=$(rec "$R8" f8 .pending_tree)
workin "$R8" "$S8" b 60; out=$(issue "$R8" "$S8" CODEX_BIN="$FK" REVIEW_PENDING_TTL=0); check "fixture: a second snapshot's round issued" "$out" 'This refusal is the review trigger'
sstopin "$R8" "$S8" thermo-bugs "No findings."
rs rv_complete "$S8" "$R8" review-cross "agent-run review-cross reported 0 findings" "" "$t1"
rs rv_pending "$S8" "$R8" && ok "a Codex result for an older snapshot leaves the new round pending" \
  || bad "a Codex result for an older snapshot leaves the new round pending"
R9="$FX/r9"; mkfeat "$R9" f9
lines z 5 > "$R9/z.py"; git -C "$R9" add -A; git -C "$R9" -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -qm z
realpush "$R9" f9:main
( . "$H/lib/review-state" && rv_snapshot "$R9" && rv_rec_update "$R9" f9 '.rounds = 3 | .quality_done = true | .reviewed_tree = $t | .reviewed_head = $h' --arg t "$RV_TREE" --arg h "$RV_HEAD" )
lines y 60 > "$R9/y.py"
out=$(RC="$R9" cxr "" FAKE_JSON="$FX/cx0.json")
[ "$(rec "$R9" f9 '"\(.rounds) \(.quality_done)"')" = "1 null" ] && ok "a standalone agent-run on a merged branch's record starts it over: round 1" \
  || bad "a standalone agent-run on a merged branch's record starts it over: round 1" "$(rec "$R9" f9 .)"
mkdir -p "$FX/od"
( cd "$FX/od" && env CLAUDE_CODE_SESSION_ID= AGENT_SESSION_ID= CODEX_BIN="$FK" FAKE_JSON="$FX/cx2.json" "${CRR[@]}" "$R9" --out review.json >/dev/null 2>&1 )
[ -f "$FX/od/review.json" ] && [ "$(jq -r '.findings[0].id' "$FX/od/review.json" 2>/dev/null)" = CX-1 ] \
  && ok "--out with a bare file name writes that file" || bad "--out with a bare file name writes that file" "$(ls -la "$FX/od")"


echo "--- roles: the rounds and the invocation come from roles.toml ---"
# review-cross moved to an anthropic model (one edit in roles.toml): round 1 is Agent spawns only.
sed 's/"openai:gpt-6.1-sol"/"anthropic:opus"/' "$H/../roles.toml" > "$FX/anth.toml"
AGENT_KIT_ROLES="$FX/anth.toml" "$H/../bin/agent-kit" roles --format sh > "$FX/anth.sh" 2>/dev/null
RA="$FX/ra"; SA="$SID-ra"; mkfeat "$RA" fa
workin "$RA" "$SA" a 60
out=$(issue "$RA" "$SA" CODEX_BIN="$FK" AGENT_ROLES_SH="$FX/anth.sh" | tr '\n' ' ')
check "review-cross on an anthropic model: round 1 is Agent spawns, review-cross among them" "$out" 'spawn 3 Agent call\(s\): subagent_type "thermo-bugs" and subagent_type "review-cross" and subagent_type "thermo-quality"'
check "...with no agent-run and no Codex findings file" "$out" 'agent-run|Codex findings' absent
check "B-5: ...and the fixer instruction names round 1 as configured, not Codex" "$out" "round 1: thermo-bugs' AND review-cross'\)"
AGENT_ROLES_SH="$FX/anth.sh"
sstopin "$RA" "$SA" thermo-bugs "B-1 a.py:1 off by one"
rs rv_pending "$SA" "$RA" && ok "...thermo-bugs alone leaves it pending" || bad "...thermo-bugs alone leaves it pending"
sstopin "$RA" "$SA" review-cross "CX-1 a.py:2 wrong bound"
AGENT_ROLES_SH="$FX/roles.sh"
[ "$(rec "$RA" fa '"\(.rounds) \(.pending_tree)"')" = "1 null" ] && ok "...the review-cross agent's return completes the round" \
  || bad "...the review-cross agent's return completes the round" "$(rec "$RA" fa .)"
( . "$H/lib/review-state" && rv_tally_owed "$SA" && [ "$RV_OWED" = "thermo-bugs review-cross" ] ) \
  && ok "...and both reports owe the TALLY" || bad "...and both reports owe the TALLY"

echo "--- agent-run: the provider command line per role (dry run) ---"
out=$(CODEX_BIN=codex "$CR" review-cross "$RA" --dry-run 2>&1)
check "openai role: codex exec with the role's model and effort, read-only" "$out" 'codex exec -C [^ ]+ -m gpt-6.1-sol -c model_reasoning_effort=high -s read-only'
check "...nothing run" "$out" 'nothing run or recorded'
out=$(AGENT_ROLES_SH="$FX/anth.sh" CLAUDE_BIN=claude "$CR" review-cross "$RA" --dry-run 2>&1)
check "anthropic role: claude -p with its model and effort (its body is the prompt)" "$out" 'claude -p --model opus --effort high --output-format json --json-schema'
check "...its --json-schema without the draft-2020-12 \$schema id, which claude -p rejects" "$out" 'draft/2020-12' absent
out=$(CLAUDE_BIN=claude "$CR" thermo-bugs "$RA" --dry-run 2>&1)
check "a role that inherits takes main's model" "$out" 'claude -p --model opus\\\[1m\\\] --effort high'
out=$({ CODEX_BIN=codex "$CR" review-cross "$RA" --effort max --dry-run 2>&1; echo "rc=$?"; } | tr '\n' ' ')
check "an effort off the provider's scale: exit 2" "$out" 'not on the openai scale.*rc=2'
sed -n '1,3p' "$(git -C "$RA" rev-parse --path-format=absolute --git-common-dir)"/agent-review-runs/review-cross-fa-*.prompt.md 2>/dev/null | grep -q 'cross-model reviewer' \
  && ok "the prompt is the review-cross agent body" || bad "the prompt is the review-cross agent body"

echo "--- review-cross: rounds issued before the rename (codex, codex-review) ---"
RL="$FX/rl"; SL="$SID-rl"; mkfeat "$RL" fl
workin "$RL" "$SL" a 60; issue "$RL" "$SL" CODEX_BIN="$FK" >/dev/null
rkl=$(rs path_key "$RL")
rs _rv_set "$SL" "ragents.$rkl=thermo-bugs thermo-quality codex"
( . "$H/lib/review-state" && rv_rec_update "$RL" fl '.agents = ["thermo-bugs","thermo-quality","codex-review"]' )
out=$(gpush "$SID-rl2" "$RL" CODEX_BIN="$FK"); check "a legacy record's wait names agent-run review-cross" "$out" 'review-cross is not an Agent: run `[^`]*/bin/agent-run review-cross'
out=$(RC="$RL" cxr "$SL" FAKE_JSON="$FX/cx0.json")
rs rv_pending "$SL" "$RL" && ok "...review-cross returning to a legacy round leaves thermo-bugs out" || bad "...review-cross returning to a legacy round leaves thermo-bugs out"
sstopin "$RL" "$SL" thermo-bugs "No findings."
[ "$(rec "$RL" fl '"\(.rounds) \(.pending_tree)"')" = "1 null" ] && ok "...and thermo-bugs completes it" || bad "...and thermo-bugs completes it" "$(rec "$RL" fl .)"

echo "--- review rounds shared across sessions (RC-CX-1..4) ---"
# RC-CX-1: session B adopts A's round T1, completes it and issues T2; A's late return for T1 must
# not complete T2 or spend its budget.
RX="$FX/x1"; SXA="$SID-x1a"; SXB="$SID-x1b"; mkfeat "$RX" fx1
workin "$RX" "$SXA" a 60; issue "$RX" "$SXA" >/dev/null
out=$(gpush "$SXB" "$RX"); check "fixture: session B adopts A's round" "$out" 'has not returned'
sstopin "$RX" "$SXB" thermo-bugs "No findings."
workin "$RX" "$SXB" b 60; out=$(issue "$RX" "$SXB"); check "fixture: B issues round 2" "$out" 'Review round 2 of'
t2=$(rec "$RX" fx1 .pending_tree)
sstopin "$RX" "$SXA" thermo-bugs "No findings."
[ "$(rec "$RX" fx1 '"\(.rounds) \(.pending_tree)"')" = "1 $t2" ] && ok "RC-CX-1: a late return for an older round leaves the newer one pending" \
  || bad "RC-CX-1: a late return for an older round leaves the newer one pending" "$(rec "$RX" fx1 .)"
# RC-CX-2: thermo-bugs and the cross reviewer returning at the same moment both count.
stuck=0
for i in 1 2 3 4 5 6; do
  Rr="$FX/x2-$i"; Sr="$SID-x2-$i"; mkfeat "$Rr" "fx2$i"
  workin "$Rr" "$Sr" a 60; issue "$Rr" "$Sr" CODEX_BIN="$FK" >/dev/null
  rs rv_complete "$Sr" "$Rr" thermo-bugs "No findings." "b$i" &
  rs rv_complete "$Sr" "$Rr" "$CXT" "No findings." "c$i" &
  wait
  ! rs rv_pending "$Sr" "$Rr" || stuck=$((stuck + 1))
done
[ "$stuck" = 0 ] && ok "RC-CX-2: two reviewers returning at once complete the round (6 of 6)" \
  || bad "RC-CX-2: two reviewers returning at once complete the round" "$stuck of 6 stuck pending"
# RC-CX-3: thermo-bugs returns in a session, the cross reviewer on another host (no session).
RX3="$FX/x3"; SX3="$SID-x3"; mkfeat "$RX3" fx3
workin "$RX3" "$SX3" a 60; issue "$RX3" "$SX3" CODEX_BIN="$FK" >/dev/null
sstopin "$RX3" "$SX3" thermo-bugs "No findings."; sstopin "$RX3" "$SX3" thermo-quality "No findings."
out=$(RC="$RX3" cxr "" FAKE_JSON="$FX/cx0.json")
[ "$(rec "$RX3" fx3 '"\(.rounds) \(.pending_tree)"')" = "1 null" ] && ok "RC-CX-3: returns split across a session and another host complete the round" \
  || bad "RC-CX-3: returns split across a session and another host complete the round" "$(rec "$RX3" fx3 .)"
# RC-CX-4: thermo-quality's findings owe the TALLY like a correctness reviewer's.
RX4="$FX/x4"; SX4="$SID-x4"; mkfeat "$RX4" fx4
workin "$RX4" "$SX4" a 60; issue "$RX4" "$SX4" >/dev/null
sstopin "$RX4" "$SX4" thermo-bugs "No findings."
sstopin "$RX4" "$SX4" thermo-quality "Q-1 a.py:3 the retry charges twice"
( . "$H/lib/review-state" && rv_tally_owed "$SX4" && [ "$RV_OWED" = thermo-quality ] ) \
  && ok "RC-CX-4: thermo-quality findings owe the TALLY" || bad "RC-CX-4: thermo-quality findings owe the TALLY"

echo "--- roles review findings (B-1, B-2, B-7, B-9, CX-1) ---"
# ppre <repo> <sid>: pre-push-gate's PreToolUse answer to a plain `git push`: decision, then reason.
ppre() { jq -cn --arg s "$2" --arg d "$1" '{hook_event_name:"PreToolUse",tool_name:"Bash",session_id:$s,cwd:$d,tool_input:{command:"git push"}}' \
  | "$H/pre-push-gate" | jq -r '(.hookSpecificOutput.permissionDecision // "allow") + ": " + (.hookSpecificOutput.permissionDecisionReason // "")' | tr '\n' ' '; }
# B-1: pre-push-gate's deny issues a session round; its completion must reach the branch record, or
# the git pre-push asks for round 1 again.
RB1="$FX/b1"; SB1="$SID-b1"; mkfeat "$RB1" fb1
workin "$RB1" "$SB1" a 300; g1() { git -C "$RB1" -c user.name=t -c user.email=t@t -c commit.gpgsign=false "$@"; }
g1 add -A; g1 commit -qm w1
out=$(ppre "$RB1" "$SB1"); check "B-1 fixture: pre-push-gate denies with the review trigger" "$out" '^deny: .*review trigger'
sstopin "$RB1" "$SB1" thermo-bugs "No findings."; sstopin "$RB1" "$SB1" thermo-quality "No findings."
[ "$(rec "$RB1" fb1 .rounds)" = 1 ] && ok "B-1: the session round's completion counts in the branch record" || bad "B-1: the session round's completion counts in the branch record" "$(rec "$RB1" fb1 .)"
out=$(gpush "$SB1" "$RB1"); check "B-1: ...so the git pre-push asks for no second round 1" "$out" 'Review round|review trigger' absent
# B-2: thermo-bugs returns in session A, review-cross runs in another session: the record completes,
# and A's copy of the round must not stay pending (pre-push-gate denied every push from A).
RB2="$FX/b2"; SB2="$SID-b2"; mkfeat "$RB2" fb2
workin "$RB2" "$SB2" a 60; out=$(issue "$RB2" "$SB2" CODEX_BIN="$FK"); check "B-2 fixture: round 1 with review-cross" "$out" 'agent-run review-cross'
sstopin "$RB2" "$SB2" thermo-bugs "No findings."; sstopin "$RB2" "$SB2" thermo-quality "No findings."
out=$(RC="$RB2" cxr "$SID-b2-other" FAKE_JSON="$FX/cx0.json")
[ "$(rec "$RB2" fb2 '"\(.rounds) \(.pending_tree)"')" = "1 null" ] && ok "B-2 fixture: another session's review-cross completes the record" || bad "B-2 fixture: another session's review-cross completes the record" "$(rec "$RB2" fb2 .)"
rs rv_pending "$SB2" "$RB2" && bad "B-2: session A's copy of the round is no longer pending" || ok "B-2: session A's copy of the round is no longer pending"
out=$(ppre "$RB2" "$SB2"); check "B-2: ...and pre-push-gate does not deny A's push as not returned" "$out" 'has not returned' absent
# B-9: the CLI gone when agent-run runs: its fallback takes its place, so the round can complete.
RB9="$FX/b9"; SB9="$SID-b9"; mkfeat "$RB9" fb9
workin "$RB9" "$SB9" a 60; issue "$RB9" "$SB9" CODEX_BIN="$FK" >/dev/null
sstopin "$RB9" "$SB9" thermo-bugs "No findings."; sstopin "$RB9" "$SB9" thermo-quality "No findings."
out=$(RC="$RB9" cxr "$SB9" CODEX_BIN="$FX/no-codex"); check "B-9 fixture: no Codex CLI at run time: exit 3" "$out" 'not on PATH.*rc=3'
sstopin "$RB9" "$SB9" thermo-bugs "No findings."
[ "$(rec "$RB9" fb9 '"\(.rounds) \(.pending_tree)"')" = "1 null" ] && ok "B-9: ...thermo-bugs took its place, and its return completes the round" \
  || bad "B-9: ...thermo-bugs took its place, and its return completes the round" "$(rec "$RB9" fb9 .)"
# CX-1: a later round with no correctness reviewer (later = [], Codex taken out) asks for thermo-bugs.
sed "s/^RV_LATER=.*/RV_LATER=''/" "$FX/roles.sh" > "$FX/nolater.sh"
RX1="$FX/cx1"; SX1="$SID-cx1"; mkfeat "$RX1" fcx1
workin "$RX1" "$SX1" a 60; issue "$RX1" "$SX1" AGENT_ROLES_SH="$FX/nolater.sh" >/dev/null
AGENT_ROLES_SH="$FX/nolater.sh" sstopin "$RX1" "$SX1" thermo-bugs "No findings."
AGENT_ROLES_SH="$FX/nolater.sh" sstopin "$RX1" "$SX1" thermo-quality "No findings."
workin "$RX1" "$SX1" b 60; out=$(issue "$RX1" "$SX1" AGENT_ROLES_SH="$FX/nolater.sh" | tr '\n' ' ')
check "CX-1: an empty later list: round 2 asks for thermo-bugs" "$out" 'Review round 2 of.*spawn 1 Agent call\(s\): subagent_type "thermo-bugs"'
check "CX-1: ...not an empty instruction" "$out" 'in ONE message, \.' absent
# B-7: claude -p's answer is its stdout; a warning on stderr must not make it unparseable.
FC="$FX/fake-claude"
printf '%s\n' '#!/bin/bash' 'cat >/dev/null' 'echo "warning: a hook printed this" >&2' \
  'jq -c "{type: \"result\", structured_output: .}" "$FAKE_JSON"' > "$FC"; chmod +x "$FC"
RB7="$FX/b7"; mkfeat "$RB7" fb7; workin "$RB7" "$SID-b7" a 60
out=$( { env CLAUDE_CODE_SESSION_ID= AGENT_SESSION_ID= AGENT_ROLES_SH="$FX/anth.sh" CLAUDE_BIN="$FC" FAKE_JSON="$FX/cx0.json" "$CR" review-cross "$RB7" --timeout 10 2>&1; echo "rc=$?"; } | tr '\n' ' ')
check "B-7: an anthropic run with stderr noise still parses its answer" "$out" 'approve, 0 finding\(s\).*rc=0'
# The first real claude -p run (2026-10-03): with verbose on, stdout is every message as an array,
# and --agent narrowed the tools to the agent's list, so the result carried no structured_output.
FV="$FX/fake-claude-verbose"
printf '%s\n' '#!/bin/bash' 'cat >/dev/null' 'so=.; case " $* " in *" --agent "*) so=null ;; esac' \
  'jq -c "[{type: \"system\", subtype: \"init\"}, {type: \"result\", subtype: \"success\", structured_output: $so}]" "$FAKE_JSON"' > "$FV"; chmod +x "$FV"
out=$( { env CLAUDE_CODE_SESSION_ID= AGENT_SESSION_ID= AGENT_ROLES_SH="$FX/anth.sh" CLAUDE_BIN="$FV" FAKE_JSON="$FX/cx2.json" "$CR" review-cross "$RB7" --timeout 10 2>&1; echo "rc=$?"; } | tr '\n' ' ')
check "claude -p as the real CLI answers (array stdout, no --agent): the findings parse" "$out" 'needs-attention, 2 finding\(s\).*rc=0'
finish
