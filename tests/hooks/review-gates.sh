#!/bin/bash
# Payload tests for the review and commit gates: review-mark-changes, review-trigger,
# review-agent-mark, pre-push-gate (lib/review-state), commit-cohesion, comment-guard, and the
# missing-lib policy. Prints PASS/FAIL per case and exits non-zero on any failure.
#
#   bash tests/run-tests.sh   (it sets HOOKS_DIR; see lib.sh)
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
TESTS="$HOME/.claude/tmp/claude-tests/$SID"
cleanup() { rm -rf "$FX"; rm -f "$PRFILE" "$HOME/.claude/tmp/ci-branch-$BR-3.pr" "$TESTS" "$HOME/.claude/tmp/claude-commit/turn-$SID" "$HOME/.claude/tmp/claude-commit/last-$SID"; }
trap cleanup EXIT

REPO="$FX/repo"; REMOTE="$FX/remote.git"
git init -q --bare "$REMOTE"
git init -q -b "$BR" "$REPO"
g() { git -C "$REPO" -c user.name=t -c user.email=t@t -c commit.gpgsign=false "$@"; }
printf 'x = 1\n' > "$REPO/a.py"
g add a.py; g commit -qm init; g remote add origin "$REMOTE"; g push -q -u origin "$BR" 2>/dev/null
lines() { i=0; while [ "$i" -lt "$2" ]; do echo "v_$1_$i = $i"; i=$((i+1)); done; }

# The sequence below completes five agent rounds in one session; the default budget has its own
# section, which unsets this.
export REVIEW_MAX_ROUNDS=9
# rs <rv_fn> <args>: a lib/review-state accessor, so no case depends on how the state is stored.
rs() { ( . "$H/lib/review-state" 2>/dev/null && "$@" ); }
# ...in <repo> <sid>: the payload helpers for a section's own repo and session.
workin()  { lines "$3" "$4" >> "$1/$3.py"; edit "$1/$3.py" "$2" | "$H/review-mark-changes"; }
stopin()  { stop "$2" "$1" "${3:-false}" | "$H/review-trigger"; }
sstopin() { jq -cn --arg d "$1" --arg s "$2" --arg t "$3" --arg m "${4-findings}" '{hook_event_name:"SubagentStop",session_id:$s,cwd:$d,agent_id:"a1",agent_type:$t,stop_hook_active:false,last_assistant_message:$m}' | "$H/review-agent-mark"; }
sstop() { sstopin "$REPO" "$SID" "$@"; }
agent() { jq -cn --arg s "$SID" --arg d "$REPO" --arg t "$1" --arg st "$2" '{hook_event_name:"PostToolUse",tool_name:"Agent",session_id:$s,cwd:$d,tool_input:{subagent_type:$t,prompt:"x"},tool_response:{status:$st}}'; }
work()  { workin "$REPO" "$SID" "$@"; }
# mkrepo <dir> [remote]: a one-commit repo on main, pushed to <remote> when given.
mkrepo() {
  git init -q -b main "$1"; printf 'x = 1\n' > "$1/a.py"
  git -C "$1" add a.py; git -C "$1" -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -qm init
  [ -z "${2:-}" ] || { git -C "$1" remote add origin "$2"; git -C "$1" push -q -u origin main 2>/dev/null; }
}

echo "--- review-mark-changes ---"
work b 5
rs rv_edited "$REPO" && [ "$(rs rv_base "$SID" "$REPO")" = "$(g rev-parse HEAD)" ] \
  && ok "edit marks the worktree + the session's per-root base" || bad "edit marks the worktree + the session's per-root base"
rs rv_clear_edits "$REPO"
edit "/private/tmp/claude-1/x.py" "$SID" | "$H/review-mark-changes"
mkdir -p "$REPO/prototypes"; edit "$REPO/prototypes/p.py" "$SID" | "$H/review-mark-changes"
rs rv_edited "$REPO" && bad "scratch or prototype path marked" || ok "scratch and prototype paths not marked"
edit "$REPO/b.py" "$SID" | "$H/review-mark-changes"

echo "--- review-trigger: sizing and scope ---"
out=$(stop "$SID" "$REPO" | "$H/review-trigger"); empty "5-line delta is under the floor: silent" "$out"
rs rv_edited "$REPO" && ok "a Stop that ran no review keeps the marker" || bad "a Stop that ran no review keeps the marker"
work b 20
out=$(stop "$SID" "$REPO" | "$H/review-trigger"); check "25 lines -> tier-0 self-check" "$out" 'SELF-CHECK'
check "round 1 scope is the session diff" "$out" "git diff [0-9a-f]{12} [0-9a-f]{12}.: this session"
out=$(stop "$SID" "$REPO" | "$H/review-trigger"); empty "unchanged tree: silent" "$out"
out=$(stop "$SID" "$REPO" true | "$H/review-trigger"); empty "chain end: silent" "$out"
rs rv_edited "$REPO" && bad "chain end of a self-check clears markers" || ok "chain end of a self-check clears markers"
work c 60
out=$(stop "$SID" "$REPO" | "$H/review-trigger"); check "60 lines -> one reviewer" "$out" 'ONE subagent_type \\"reviewer\\"'
check "light tier carries the mandatory critic" "$out" 'MANDATORY CRITIC'
check "round 2 scope is only the delta" "$out" 'ONLY what changed since the last review round'
check "round 2 delta is 60, not the session total" "$out" '~60 line delta'
rs rv_pending "$SID" "$REPO" && ok "agent round is pending" || bad "agent round is pending"
[ -f "$TMPDIR/claude-stop-block/$SID" ] && ok "a review block marks the session for stop-chime" || bad "review block left no stop-block marker"
out=$(stop "$SID" "$REPO" true | "$H/review-trigger")
rs rv_edited "$REPO" && ok "chain end keeps markers while agents are out" || bad "chain end keeps markers while agents are out"

echo "--- review-agent-mark: completion, not spawn ---"
agent reviewer async_launched | "$H/review-agent-mark"
rs rv_pending "$SID" "$REPO" && ok "background spawn completes nothing" || bad "background spawn completes nothing"
agent reviewer completed | "$H/review-agent-mark"
rs rv_pending "$SID" "$REPO" && ok "PostToolUse(Agent) completes nothing: SubagentStop does" || bad "PostToolUse(Agent) completes nothing: SubagentStop does"
sstop reviewer ""
rs rv_pending "$SID" "$REPO" && ok "interrupted agent completes nothing" || bad "interrupted agent completes nothing"
sstop Explore
rs rv_pending "$SID" "$REPO" && ok "non-review agent completes nothing" || bad "non-review agent completes nothing"
sstop reviewer
rs rv_pending "$SID" "$REPO" && bad "SubagentStop(reviewer) promotes the round" || ok "SubagentStop(reviewer) promotes the round"
rs rv_edited "$REPO" && bad "unchanged tree after completion clears markers" || ok "unchanged tree after completion clears markers"
rs rv_critic_owed "$SID" && ok "a critic is owed" || bad "a critic is owed"
out=$(stop "$SID" "$REPO" | "$H/review-trigger"); check "Stop blocks once for the critic" "$out" 'no critic has run'
check "critic reminder reuses the verdict grammar" "$out" 'Act only on CONFIRMED; report UNPROVEN as unverified'
out=$(stop "$SID" "$REPO" true | "$H/review-trigger"); empty "critic reminder fires once" "$out"
sstop reviewer
rs rv_critic_owed "$SID" && bad "unrelated reviewer (no round pending) owes nothing" || ok "unrelated reviewer (no round pending) owes nothing"

echo "--- review-trigger: heavy rounds ---"
work d 200
out=$(stop "$SID" "$REPO" | "$H/review-trigger"); check "200 lines -> heavy" "$out" 'thermo-bugs'
check "first heavy round spawns thermo-quality" "$out" 'subagent_type \\"thermo-quality\\"'
sstop thermo-bugs
rs rv_pending "$SID" "$REPO" && bad "SubagentStop(thermo-bugs) completes the round" || ok "SubagentStop(thermo-bugs) completes the round"
sstop thermo-quality
agent critic async_launched | "$H/review-agent-mark"
rs rv_critic_owed "$SID" && bad "spawning the critic settles it" || ok "spawning the critic settles it"
stop "$SID" "$REPO" true | "$H/review-trigger" >/dev/null
work e 200
out=$(stop "$SID" "$REPO" | "$H/review-trigger"); check "second heavy round: thermo-bugs" "$out" 'ONE subagent_type \\"thermo-bugs\\"'
check "second heavy round skips thermo-quality" "$out" 'thermo-quality already ran'
check "second heavy round sized on its own delta" "$out" '~200 line delta'
sstop thermo-bugs; agent critic completed | "$H/review-agent-mark"
stop "$SID" "$REPO" true | "$H/review-trigger" >/dev/null

echo "--- pre-push-gate ---"
# The gate only runs when the push carries code: tracked or staged, since untracked files never ship.
mkdir -p "$(dirname "$TESTS")"; touch "$TESTS"; g add -A
out=$(pre "git push" "$SID" "$REPO" | "$H/pre-push-gate"); empty "reviewed tree: push passes silently" "$out"
work f 20; g add -A
out=$(pre "git push" "$SID" "$REPO" | "$H/pre-push-gate")
check "20 unreviewed lines: no deny" "$out" '"deny"' absent
check "20 unreviewed lines: self-check note" "$out" 'additionalContext.*self-check'
check "note has no permissionDecision (never auto-allows)" "$out" 'permissionDecision' absent
work f 40; g add -A
out=$(pre "git push" "$SID" "$REPO" | "$H/pre-push-gate")
check "60 unreviewed lines: deny" "$out" '"deny"'
check "deny is the review trigger" "$out" 'This deny is the review trigger'
check "deny carries the tier instruction" "$out" 'subagent_type'
check "deny names the override first" "$out" 'push-now.*review trigger'
out=$(pre "git push" "$SID" "$REPO" | "$H/pre-push-gate"); check "round in flight: wait" "$out" 'has not returned'
out=$(pre "git push # push-now" "$SID" "$REPO" | "$H/pre-push-gate"); check "push-now overrides" "$out" '"deny"' absent
sstop critic
out=$(pre "git push" "$SID" "$REPO" | "$H/pre-push-gate"); check "a critic's return does not close the round" "$out" 'has not returned'
sstop reviewer "No findings."
rs rv_critic_owed "$SID" && bad "a zero-finding round owes no critic" || ok "a zero-finding round owes no critic"
out=$(pre "git push" "$SID" "$REPO" | "$H/pre-push-gate"); check "after completion the push passes" "$out" '"deny"' absent
out=$(pre "gh pr create --body \"then git push\"" "$SID" "$REPO" | "$H/pre-push-gate"); empty "quoted git push is not a push" "$out"
rm -f "$TESTS"
out=$(pre "git push" "$SID" "$REPO" | "$H/pre-push-gate"); check "no test run: ask" "$out" '"ask"'
out=$(pre "echo done # then git push" "$SID" "$REPO" | "$H/pre-push-gate"); empty "git push in a comment is not a push" "$out"

echo "--- missing-lib policy ---"
# A hooks copy with one lib removed: guards of pushes and prod data refuse, advisory hooks say so.
C="$FX/hooks-copy"; cp -R "$H" "$C"; rm -f "$C/lib/review-state"
touch "$TESTS"
out=$(pre "git push" "$SID" "$REPO" | "$C/pre-push-gate"); check "push gate without lib/review-state: deny" "$out" 'Reason": "pre-push-gate: hooks/lib/review-state'
out=$(pre "git status" "$SID" "$REPO" | "$C/pre-push-gate"); empty "push gate without lib/review-state: a non-push passes" "$out"
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

echo "--- pre-push-gate: ticket sprawl ---"
# Once a PR is open the repo forbids amend, so each push adds a commit and the count must not apply.
g checkout -q -b "$BR-3"
for n in 1 2 3; do echo "s$n = $n" >> "$REPO/a.py"; g commit -qam "PROJ-1: part $n"; done
g push -q -u origin "$BR-3" 2>/dev/null
echo "s4 = 4" >> "$REPO/a.py"; g commit -qam "PROJ-1: part 4"
mkdir -p "$(dirname "$TESTS")"; touch "$TESTS"
PR3="$HOME/.claude/tmp/ci-branch-$BR-3.pr"
out=$(pre "git push" "$SID" "$REPO" | "$H/pre-push-gate"); check "4th ticket commit, no PR: sprawl deny" "$out" 'PROJ-1 already has 3 commits'
touch "$PR3"
out=$(pre "git push" "$SID" "$REPO" | "$H/pre-push-gate"); check "4th ticket commit, PR open: no sprawl deny" "$out" 'already has 3 commits' absent
rm -f "$PR3" "$TESTS"

echo "--- pre-push-gate: committed work in a second worktree ---"
# The base is per root: work committed in a worktree other than the session's first edit is sized.
REPO2="$FX/repo2"; git clone -q -b "$BR" "$REMOTE" "$REPO2" 2>/dev/null
g2() { git -C "$REPO2" -c user.name=t -c user.email=t@t -c commit.gpgsign=false "$@"; }
g2 checkout -q -b "$BR-4"; g2 push -q -u origin "$BR-4" 2>/dev/null
lines z 80 >> "$REPO2/z.py"; edit "$REPO2/z.py" "$SID" | "$H/review-mark-changes"
g2 add -A; g2 commit -qm "PROJ-2: z"
touch "$TESTS"
out=$(pre "git push" "$SID" "$REPO2" | "$H/pre-push-gate")
check "80 committed lines in a second worktree: deny" "$out" '"deny"'
rm -f "$TESTS"

echo "--- review budget: a round counts when its agent returns ---"
unset REVIEW_MAX_ROUNDS
# The model deferred each round while its own edit agents ran. Counting the blocks spent the budget
# and marked thermo-quality done; the deferred changes also fell out of the next round's scope.
RD="$FX/rd"; SD="$SID-d"; mkrepo "$RD"
workin "$RD" "$SD" a 200
out=$(stopin "$RD" "$SD"); check "heavy round issued" "$out" 'subagent_type \\"thermo-quality\\"'
stopin "$RD" "$SD" true >/dev/null
workin "$RD" "$SD" b 200
out=$(stopin "$RD" "$SD"); check "deferred round: thermo-quality still asked for" "$out" 'subagent_type \\"thermo-quality\\"'
check "deferred round: sized from the last completed review" "$out" '~400 line delta'
stopin "$RD" "$SD" true >/dev/null
workin "$RD" "$SD" c 200
out=$(stopin "$RD" "$SD"); check "three deferred rounds spend no budget" "$out" 'churn brake' absent
check "...and still spawn agents" "$out" 'subagent_type \\"thermo-bugs\\"'
RB="$FX/rb"; SB="$SID-b"; mkrepo "$RB"
for n in 1 2 3; do
  workin "$RB" "$SB" "r$n" 60
  out=$(stopin "$RB" "$SB"); check "completed round $n of the default 3: reviewer" "$out" 'ONE subagent_type \\"reviewer\\"'
  sstopin "$RB" "$SB" reviewer "No findings."
  stopin "$RB" "$SB" true >/dev/null
done
workin "$RB" "$SB" r4 60
out=$(stopin "$RB" "$SB"); check "budget spent: self-review" "$out" 'churn brake'
check "...with no agents" "$out" 'subagent_type' absent

echo "--- review-trigger: merged-in upstream is not this session's delta ---"
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
workin "$RA" "$SM" own2 50; ga add -A; ga commit -qm own2; teammate team2 100; merge_up
out=$(stopin "$RA" "$SM"); check "work committed before a merge still counts" "$out" '~50 line delta'
check "...at the light tier, not heavy for the teammate's 100" "$out" 'ONE subagent_type \\"reviewer\\"'

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
rs rv_edited "$PX" && bad "overlay PROTOTYPE_DIRS path marked for review" || ok "overlay PROTOTYPE_DIRS path not marked"
edit "$PX/playground/p.py" "$SID-px" | KIT_ENV=/dev/null "$H/review-mark-changes"
rs rv_edited "$PX" && ok "no overlay: the same path is code and marked" || bad "no overlay: the same path not marked"
out=$(cedit "$PX/playground/m.py" "a=1" "$six
a=1" | KIT_ENV="$FX/kit.env" cg); empty "overlay PROTOTYPE_DIRS path gets no comment nudge" "$out"
out=$(cedit "$PX/playground/m.py" "a=1" "$six
a=1" | KIT_ENV=/dev/null cg); check "no overlay: the same path nudges" "$out" 'added 6 comment'

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

finish
