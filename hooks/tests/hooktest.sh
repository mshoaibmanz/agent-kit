#!/bin/bash
# Payload tests for the ~/.claude hooks. Prints PASS/FAIL per case and exits non-zero on any FAIL.
# Every case asserts the exit code as well as the decision: a hook that dies under `set -e`
# prints nothing, which reads as "allow" (test-exec-gate aborted on every path-less command
# for 13 days before this check existed).
# Run: bash ~/.claude/hooks/tests/hooktest.sh   (HOOKS_DIR=<dir> tests another copy; see lib.sh)
# The hooks read the per-user overlay (hooks/lib/hook-io kit_env); KIT_ENV=/dev/null runs the
# suite with none, and the overlay-specific cases then use a fixture overlay or skip.
. "${BASH_SOURCE[0]%/*}/lib.sh"
START_KIT=${KIT_ENV:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/local/kit.env}
# The suite's own loader reads an overlay's values, so a HOOKS_DIR copy without one still gets cases.
SUITE_LIB="${BASH_SOURCE[0]%/*}/../lib"
# Per run, so two concurrent runs (parallel workers) never share a dir, a session id or a PR
# pointer. Not /tmp or a scratchpad: context-guard skips those paths.
mkdir -p "$HOME/.claude/tmp"
T=$(mktemp -d "$HOME/.claude/tmp/hooktest.XXXXXX") || exit 1
trap 'rm -rf "$T"' EXIT
SID=hooktest-$$
PRN=$((900000 + $$ % 99999))
DB=my; DB="${DB}sql"; BQ=bq; BQ="${BQ}ro"

# run <hook> <deny|allow|silent> <json>
#   deny   = JSON deny/block with rc 0, or rc 2 (stderr reason)
#   allow  = rc 0 and no deny/block
#   silent = rc 0 and no output at all
run() {
  local out rc got=allow
  out=$(printf '%s' "$3" | "$H/$1" 2>"$T/stderr"); rc=$?
  if [ "$rc" = 2 ] || { [ "$rc" = 0 ] && printf '%s' "$out" | grep -Eq '"(deny|block)"'; }; then
    got=deny
  elif [ "$rc" != 0 ]; then
    got="rc=$rc"
  elif [ -z "$out" ]; then
    got=silent
  fi
  local desc; desc=$(printf '%s' "$3" | jq -r '.tool_input.command // .session_id // empty' 2>/dev/null | head -c 100)
  if [ "$got" = "$2" ] || { [ "$2" = allow ] && [ "$got" = silent ]; }; then
    ok "$1 $2: $desc"
  else
    bad "$1 expected=$2 got=$got: $desc"
    echo "     out: $(printf '%s' "$out" | head -c 200)"
    echo "     err: $(head -c 200 "$T/stderr")"
  fi
}

echo "--- syntax ---"
# Every file, not a hand-kept list (lib/db-registry went unchecked on one). A shell shebang, or a
# lib's bare comment header (sourced, so no shebang), is bash; awk, JSON and README are skipped.
nsh=0
for f in "$H"/* "$H"/lib/*; do
  [ -f "$f" ] || continue
  IFS= read -r first < "$f" || true
  h=${f#"$H"/}
  case $first in
    '#!'*python*) python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$f" && ok "syntax $h" || bad "syntax $h" ;;
    '#!'*bash*|'#!'*/sh) nsh=$((nsh + 1)); /bin/bash -n "$f" && ok "syntax $h" || bad "syntax $h" ;;
    '#!'*) ;;
    '#'*) nsh=$((nsh + 1)); /bin/bash -n "$f" && ok "syntax $h" || bad "syntax $h" ;;
  esac
done
[ "$nsh" -ge 20 ] && ok "syntax: $nsh shell files found" || bad "syntax: only $nsh shell files under $H"
/bin/bash -n "$H/../bin/claude-gc" && ok "syntax claude-gc" || bad "syntax claude-gc"

echo "--- lib: cmd-view, notify ---"
(
  HOOK_LIB="$H/lib"; . "$H/lib/cmd-view" || exit 1
  cmd_view "git commit -m 'a # b' # separate-change"
  [ "$CMD_BARE" = "git commit -m Q" ] && cmd_flag separate-change && echo ok1
  cmd_view "git commit -m \"# separate-change\""
  cmd_flag separate-change || echo ok2
  cmd_view "echo \$'don\\'t; pytest x' > \"\$LOG\""
  [ "$CMD_BARE" = "echo Q > Q" ] && echo ok3
) > "$T/cv.out" 2>&1
[ "$(tr '\n' ' ' < "$T/cv.out")" = "ok1 ok2 ok3 " ] && ok "cmd_view masks quotes and comments; cmd_flag reads only comments" \
  || bad "cmd_view/cmd_flag: $(tr '\n' ' ' < "$T/cv.out")"
(
  . "$H/lib/notify" || exit 1
  _notify_esc 'say "hi" \ now'; echo
  long=$(printf 'é%.0s' $(seq 1 200)); _notify_esc "$long" | iconv -f UTF-8 -t UTF-8 >/dev/null && echo utf8ok
) > "$T/nt.out" 2>&1
[ "$(head -1 "$T/nt.out")" = 'say \"hi\" \\ now' ] && grep -qx utf8ok "$T/nt.out" \
  && ok "notify escapes for AppleScript and truncates to valid UTF-8" || bad "notify escaping: $(tr '\n' ' ' < "$T/nt.out")"

echo "--- bash-guards ---"
run bash-guards allow "$(pre "sed -n '1,80p' ~/.claude/skills/debug/references/$DB.md")"
run bash-guards allow "$(pre "grep -n tunnel ~/.claude/skills/debug/references/$DB.md | head")"
run bash-guards deny  "$(pre "$DB -e 'select 1'")"
run bash-guards deny  "$(pre "/opt/homebrew/bin/$DB --login-path=prod-ro -e 'select 1'")"
run bash-guards deny  "$(pre "MYSQL_PWD=x $DB -e 'select 1'")"
run bash-guards allow "$(pre "which ro-$DB $BQ")"
run bash-guards allow "$(pre "sed -n '1,45p' ~/.local/bin/$BQ")"
run bash-guards allow "$(pre "$BQ 'SELECT 1'")"
# Writes are the ro-mysql wrapper's to reject, on the real SQL; the hook lets them through.
run bash-guards allow "$(pre "ro-$DB -e 'DELETE FROM t'")"
run bash-guards deny  "$(pre "git commit -m 'no trailers here'")"
run bash-guards deny  "$(pre "git -C /tmp/x commit -m 'no trailers here'")"
run bash-guards deny  "$(pre "git -C \"\$WT\" commit -m 'no trailers here'")"
run bash-guards allow "$(pre "git -C /tmp/x commit -F /nonexistent/msg.txt")"

echo "--- context-guard ---"
CG=$HOME/.claude/tmp/claude-context/$SID; rm -rf "$CG"
A=$T/hooktest-a.md; B=$T/hooktest-b.md; touch "$A" "$B"
printf '%s' "$(post "cat $A; wc -l $B" $SID "$T")" | "$H/context-guard" >/dev/null
run context-guard deny  "$(pre "cat $A" $SID "$T")"
run context-guard allow "$(pre "cat $B" $SID "$T")"
printf '%s' "$(post "cat > $B <<EOF
x
EOF" $SID "$T")" | "$H/context-guard" >/dev/null
run context-guard allow "$(pre "cat $B" $SID "$T")"
run context-guard allow "$(pre "sed -n '1,20p' $A" $SID "$T")"
run context-guard allow "$(pre "head -1 $A" $SID "$T")"
run context-guard allow "$(pre "cat $A # re-dump" $SID "$T")"
rm -rf "$CG"

M=$HOME/.claude/tmp/claude-tests
BG='{"tool_input":{"run_in_background":true}}'
# A fixture repo holding the overlay's gate file; hooks read the overlay named by $KIT_ENV.
REPO=$T/gate-repo; git init -q "$REPO"
gate() { run test-exec-gate "$1" "$(pre "$2" $SID "$REPO")"; }
ran()  { printf '%s' "$(post "$1" $SID "$REPO")" | "$H/tests-ran-mark" || bad "tests-ran-mark rc=$? on: $1"; }
# overlay <file>: the overlay the hooks read; its TEST_* keys land in this shell for the cases.
overlay() {
  export KIT_ENV=$1
  IFS=$'\t' read -r RUN GATEFILE REFUSED < <(
    . "$SUITE_LIB/hook-io"; kit_env
    printf '%s\t%s\t%s\n' "${TEST_RUNNER:--}" "${TEST_GATE_FILE:--}" "${TEST_REFUSED_CMDS%%|*}")
}

# gate_cases: the gate and the markers, for the overlay loaded by `overlay`.
gate_cases() {
  touch "$REPO/$GATEFILE"
  mkdir -p "$M"; rm -f "$M/lastrun-$SID" "$M/lastrun-$SID".* "$M/lastrun-$SID"-* "$M/edit-$SID" "$M/$SID"
  # Not a test run: exit 0, no output (these died with rc 1 under set -e before).
  gate silent "git status"
  gate silent "ls"
  gate silent "cat ~/bin/$RUN"
  gate silent "which pytest"
  gate silent "echo \$'don\\'t; pytest x'"
  # Wrong runner / backgrounded: denied, including path-less forms.
  gate deny "pytest -k foo"
  [ -z "$REFUSED" ] || gate deny "$REFUSED -k foo"
  gate deny "uv run pytest tests/a.py"
  run test-exec-gate deny "$(pre "$RUN -k foo" $SID "$REPO" "$BG")"
  run test-exec-gate deny "$(pre "$RUN tests/f.py" $SID "$REPO" "$BG")"
  run test-exec-gate allow "$(pre "$RUN tests/d.py > $T/t.log 2>&1" $SID "$REPO" "$BG")"
  run test-exec-gate allow "$(pre "$RUN tests/e.py > \"\$LOG\" 2>&1" $SID "$REPO" "$BG")"
  # Re-run gate: scope = paths + -k, recorded by tests-ran-mark after each run.
  gate allow "$RUN tests/a.py -k x"; ran "$RUN tests/a.py -k x"
  gate deny  "$RUN tests/a.py -k x"
  gate allow "$RUN tests/a.py -k y"
  gate allow "$RUN tests/a.py -k x # rerun"
  ran "$RUN tests/b.py"
  gate deny  "$RUN tests/b.py -k z"
  gate deny  "$RUN tests/a.py tests/b.py -k x"
  ran "$RUN tests/c.py -k \"p or q\""
  gate deny  "$RUN tests/c.py -k 'p or q'"
  gate allow "$RUN tests/c.py -k \"r\""
  ran "$RUN src/m/tests/test_n.py::test_one"
  gate deny  "$RUN src/m/tests/test_n.py::test_one"
  gate allow "$RUN src/m/tests/test_n.py::test_two"
  ran "$RUN -k foo"
  gate deny  "$RUN -k foo"
  gate allow "$RUN -k bar"
  # An edit unlocks, and the next recorded run resets the scope.
  sleep 1; touch "$M/edit-$SID"
  gate allow "$RUN tests/a.py -k x"; ran "$RUN tests/a.py -k x"
  gate deny  "$RUN tests/a.py -k x"
  gate allow "$RUN tests/b.py -k z"
  # A subagent shares the parent's session_id; its run locks only itself (keyed by agent_id).
  printf '%s' "$(post "$RUN tests/g.py" $SID "$REPO" '{"agent_id":"sub1"}')" | "$H/tests-ran-mark"
  gate allow "$RUN tests/g.py"
  run test-exec-gate deny "$(pre "$RUN tests/g.py" $SID "$REPO" '{"agent_id":"sub1"}')"
  # A runner merely named does not mark a run.
  rm -f "$M/$SID"; ran "cat ~/bin/$RUN"
  [ ! -f "$M/$SID" ] && ok "tests-ran-mark ignores a named runner" || bad "tests-ran-mark marked 'cat ~/bin/$RUN' as a run"
  rm -f "$M/lastrun-$SID" "$M/lastrun-$SID".* "$M/lastrun-$SID"-* "$M/edit-$SID" "$M/$SID"
}

echo "--- test-exec-gate + tests-ran-mark: fixture overlay ---"
printf '%s\n' TEST_GATE_FILE=.hooktest-gate TEST_RUNNER=trun "TEST_RUNNER_RE='trun(-ci)?'" \
  "TEST_REFUSED_CMDS='make test|dev t'" "TEST_NOTE='Each run costs 5s.'" > "$T/kit.env"
overlay "$T/kit.env"
gate_cases
gate deny "dev t -k foo"
gate allow "trun-ci tests/z.py"
out=$(printf '%s' "$(pre "pytest -k foo" $SID "$REPO")" | "$H/test-exec-gate")
printf '%s' "$out" | grep -q 'run via `trun <path>.*`make test`, `dev t`.*Each run costs 5s\.' \
  && ok "the refusal names the overlay's runner, refused forms and note" || bad "refusal text: $(printf '%s' "$out" | head -c 300)"

echo "--- test-exec-gate + tests-ran-mark: the overlay this suite started with ---"
overlay "$START_KIT"
if [ "$RUN" != - ] && [ "$GATEFILE" != - ]; then
  gate_cases
else
  echo "SKIP no TEST_RUNNER/TEST_GATE_FILE in $KIT_ENV"
fi

echo "--- no overlay: the test gate is inert, only pytest marks a run ---"
# The repo still holds every gate file above, the started overlay's included: nothing is hardcoded.
overlay /dev/null
touch "$REPO/.hooktest-gate"
gate silent "pytest -k foo"
gate silent "dev t -k foo"
run test-exec-gate silent "$(pre "trun tests/a.py" $SID "$REPO" "$BG")"
rm -f "$M/$SID"; ran "trun tests/a.py"
[ ! -f "$M/$SID" ] && ok "no overlay: a would-be runner is not a test run" || bad "no overlay: tests-ran-mark marked trun"
ran "pytest tests/a.py"
[ -f "$M/$SID" ] && ok "no overlay: pytest still marks a run" || bad "no overlay: pytest did not mark a run"
rm -f "$M/lastrun-$SID" "$M/lastrun-$SID".* "$M/lastrun-$SID"-* "$M/edit-$SID" "$M/$SID"
out=$(CLAUDE_PROJECT_DIR=$REPO "$H/session-context" </dev/null)
check "no overlay: session-context has no TESTS line" "$out" '^TESTS:' absent
overlay "$T/kit.env"
out=$(CLAUDE_PROJECT_DIR=$REPO "$H/session-context" </dev/null)
check "fixture overlay: session-context TESTS line names the runner" "$out" '^TESTS: `trun a\.py.*Each run costs 5s\. `make test`, `dev t` and bare `pytest` are blocked'

echo "--- session-context: the session's OUT DIR, created lazily ---"
OUTR=$T/out; rm -rf "$OUTR" "$T/state" "$T/labels"
sess() { jq -cn --arg s "$1" --arg src "${3:-startup}" '{session_id:$s,hook_event_name:"SessionStart",source:$src}' | KIT_ENV=/dev/null CLAUDE_OUT_ROOT=$OUTR CLAUDE_STATE_DIR=$T/state TAB_LABEL_DIR=$T/labels ITERM_SESSION_ID=$SID-tab CLAUDE_PROJECT_DIR=${2:-$REPO} "$H/session-context"; }
out=$(sess abcdef12-0000-4000-8000-000000000000)
check "a session id gets an OUT DIR path under <root>/<repo>" "$out" "^OUT DIR: $OUTR/gate-repo/[0-9-]*-abcdef12 .created on first write.\. "
[ ! -e "$OUTR/gate-repo" ] && [ ! -e "$OUTR/latest" ] && ok "the OUT DIR is not created at start" || bad "SessionStart created the OUT DIR"
mkdir -p "$OUTR/gate-repo/2000-01-01-0000-abcdef12"
out=$(sess abcdef12-0000-4000-8000-000000000000)
check "a resume reuses the session's existing OUT DIR" "$out" "^OUT DIR: $OUTR/gate-repo/2000-01-01-0000-abcdef12 "
out=$(CLAUDE_OUT_ROOT=$OUTR CLAUDE_PROJECT_DIR=$REPO "$H/session-context" </dev/null)
check "no payload: no OUT DIR line" "$out" '^OUT DIR:' absent

echo "--- session-context + agent-task: projects, legacy task folders, /clear ---"
TR=$T/task-repo; git init -q -b DEMO-42-widget-fix "$TR" && git -C "$TR" -c user.name=t -c user.email=t@t commit -q --allow-empty -m init
out=$(sess 11111111-0000-4000-8000-000000000000 "$TR")
check "a ticket branch does not bind at start; the first prompt does" "$out" "^OUT DIR: .*the branch names DEMO-42, so the first prompt binds it"
git -C "$TR" checkout -q -b CORE-118-REL-TEST
out=$(sess 33333333-0000-4000-8000-000000000000 "$TR")
check "a release branch names no task" "$out" "No project is bound: a ticket key in a prompt binds it, or /bind"
CT="$H/../bin/claude-task"
if [ -x "$CT" ]; then
  ct() { KIT_ENV=/dev/null CLAUDE_OUT_ROOT=$OUTR CLAUDE_STATE_DIR=$T/state TAB_LABEL_DIR=$T/labels ITERM_SESSION_ID=$SID-tab "$CT" "$@"; }
  sd="$OUTR/task-repo/2000-01-01-0000-33333333"; mkdir -p "$sd"; echo note > "$sd/notes.md"
  out=$(ct bind DEMO-77 Billing fix --session 33333333-0000-4000-8000-000000000000 2>&1)
  check "claude-task (agent-task) bind of a new ticket makes a project of one" "$out" "^Bound session 33333333 to DEMO-77/DEMO-77 "
  [ -f "$OUTR/projects/DEMO-77/items/DEMO-77/out/sessions/2000-01-01-0000-33333333/notes.md" ] && [ -L "$sd" ] \
    && ok "bind moves the session folder into the item and symlinks it back" || bad "bind did not adopt the session folder" "$out"
  out=$(sess 33333333-0000-4000-8000-000000000000 "$TR")
  check "a bound session gets the project slice" "$out" "^PROJECT: DEMO-77 · item DEMO-77\. Folder: $OUTR/projects/DEMO-77/items/DEMO-77\. "
  mkdir -p "$OUTR/tasks/DEMO-42-widget-fix"; echo "# Handoff DEMO-42" > "$OUTR/tasks/DEMO-42-widget-fix/HANDOFF.md"
  out=$(ct bind DEMO-42 --session 44444444-0000-4000-8000-000000000000 2>&1)
  check "bind to a legacy ticket finds its task folder by prefix" "$out" "^Bound session 44444444 to DEMO-42-widget-fix "
  out=$(sess 44444444-0000-4000-8000-000000000000 "$TR")
  check "a legacy folder keeps its TASK DIR line" "$out" "^TASK DIR: $OUTR/tasks/DEMO-42-widget-fix\. "
  check "...and surfaces its handoff with its first lines" "$out" "^HANDOFF: $OUTR/tasks/DEMO-42-widget-fix/HANDOFF.md\. Read it"
  out=$(sess 55555555-0000-4000-8000-000000000000 "$TR" clear)
  check "/clear inherits the tab's binding" "$out" "Kept this tab's binding to DEMO-42-widget-fix across /clear"
  out=$(sess 66666666-0000-4000-8000-000000000000 "$TR" startup)
  check "a fresh startup in the same tab does not inherit" "$out" "^OUT DIR: "
  mkdir -p "$OUTR/projects/DEMO-77/scripts"
  i=0; while [ $i -lt 300 ]; do printf '"""Script %s with a long docstring that fills the index quickly and then some more words."""\n' $i > "$OUTR/projects/DEMO-77/scripts/s$i.py"; i=$((i+1)); done
  ct index DEMO-77 >/dev/null
  out=$(sess 33333333-0000-4000-8000-000000000000 "$TR" compact)
  [ "$(printf '%s' "$out" | wc -c | tr -d ' ')" -le 9500 ] && printf '%s' "$out" | grep -q "more lines" \
    && ok "session-context with a big project stays under its 9500B budget" || bad "session-context output $(printf '%s' "$out" | wc -c)B"
  check "...without the over-budget warning" "$out" '^!! session-context emitted' absent
else
  echo "SKIP claude-task: $CT not found"
fi
rm -rf "$OUTR" "$T/state" "$TR" "$T/labels"

echo "--- session-context: monthly kit-sync nudge ---"
mkdir -p "$T/state"; echo 2026-01-01 > "$T/state/kit-last-sync"; touch -t 202601010000 "$T/state/kit-last-sync"
out=$(CLAUDE_STATE_DIR=$T/state CLAUDE_PROJECT_DIR=$REPO "$H/session-context" </dev/null)
check "a stale kit sync is mentioned" "$out" '^claude-kit was last synced 2026-01-01'
touch "$T/state/kit-last-sync"
out=$(CLAUDE_STATE_DIR=$T/state CLAUDE_PROJECT_DIR=$REPO "$H/session-context" </dev/null)
check "a recent kit sync is not" "$out" 'claude-kit was last synced' absent
rm -rf "$T/state"

echo "--- test-exec-gate: literal refused forms, a gate file without a runner, the pre-filter ---"
# A refused form is literal text: `c++` is an invalid ERE, and a space around a '|' is not part of it.
printf '%s\n' TEST_GATE_FILE=.hooktest-gate TEST_RUNNER=trun "TEST_REFUSED_CMDS='c++ test | dev  t'" > "$T/kit-lit.env"
overlay "$T/kit-lit.env"
gate deny "c++ test -k foo"
gate deny "dev t -k foo"
gate silent "cc test -k foo"
printf '%s\n' TEST_GATE_FILE=.hooktest-gate > "$T/kit-norun.env"
overlay "$T/kit-norun.env"
rm -f "$M/gate-off-$SID-off"
out=$(pre "pytest -k x" $SID-off "$REPO" | "$H/test-exec-gate")
check "a gate file without a runner tells the user the gate is off" "$out" 'systemMessage.*test-exec-gate is off.*TEST_RUNNER'
out=$(pre "pytest -k x" $SID-off "$REPO" | "$H/test-exec-gate"); empty "...once per session" "$out"
rm -f "$M/gate-off-$SID-off"
# A hooks copy without lib/test-run: the pre-filter must not need it, since it runs on every Bash call.
# -H: $H is a symlink into ~/.agents; plain -R copies the link, and rm below would hit the real lib.
C="$T/hooks-copy"; cp -RH "$H" "$C"; rm -f "$C/lib/test-run"
overlay "$T/kit.env"
out=$(pre "git status" $SID "$REPO" | "$C/test-exec-gate"); empty "the pre-filter exits on a non-test command before loading lib/test-run" "$out"
out=$(pre "pytest -k x" $SID "$REPO" | "$C/test-exec-gate"); check "...while a test run without it says the gate is off" "$out" 'systemMessage'
export KIT_ENV=$START_KIT

echo "--- overlay contract: hook-io, kit_env.py and bqro read a file the same way ---"
# Declared keys only, a trailing CR dropped, the file's value winning, a key the file lacks empty
# even when the environment sets it. BQRO=<old copy> tests another bqro.
# The key list is hook-io's KIT_KEYS (the one source), so a new key needs no edit here.
KEYS=$(sed -n 's/^KIT_KEYS="\(.*\)"$/\1/p' "$H/lib/hook-io")
printf '%s\r\n' 'TEST_RUNNER=trun' 'TEST_NOTE="a note"' "RELEASE_BRANCH_RE='rel-[0-9]+'" 'PATH=/evil' 'NOT_DECLARED=x' \
  '# CODE_DIRS=~/nope' 'BQRO_PROJECT=proj-1' > "$T/kit-crlf.env"
want=
for k in $KEYS; do
  case $k in
    RELEASE_BRANCH_RE) v='rel-[0-9]+' ;; TEST_RUNNER) v=trun ;; TEST_NOTE) v='a note' ;; BQRO_PROJECT) v=proj-1 ;; *) v= ;;
  esac
  want="$want$k=$v|"
done
[ -n "$KEYS" ] && ok "hooktest reads the overlay keys from hook-io's KIT_KEYS" || bad "no KIT_KEYS line in $H/lib/hook-io"
got=$(env GIT_AUTHOR=from-env CODE_DIRS=from-env KIT_ENV="$T/kit-crlf.env" /bin/bash -c \
  '. "$1"; kit_env; for k in $2; do printf "%s=%s|" "$k" "${!k-}"; done; [ "$PATH" != /evil ] || printf evil
   /bin/bash -c "printf %s \"\${GIT_AUTHOR-unset}\""' _ "$H/lib/hook-io" "$KEYS")
[ "$got" = "${want}unset" ] && ok "hook-io kit_env: declared keys, CR dropped, the file wins, the environment's copy unset" \
  || bad "hook-io kit_env" "$got"
got=$(env GIT_AUTHOR=from-env KIT_ENV="$T/kit-crlf.env" python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
from kit_env import kit_env
e = kit_env()
print("".join(f"{k}={e.get(k)}|" for k in sys.argv[2].split()) + ",".join(sorted(set(e) - set(sys.argv[2].split()))))' \
  "$H/lib" "$KEYS" 2>&1)
[ "$got" = "$want" ] && ok "kit_env.py: the same values, and no undeclared key" || bad "kit_env.py" "$got"
BQRO=${BQRO:-$HOME/.local/bin/bqro}
mkdir -p "$T/fakebq"
printf '%s\n' '#!/bin/bash' 'case " $* " in *" --dry_run "*) echo "{\"statistics\":{\"query\":{\"statementType\":\"SELECT\",\"totalBytesProcessed\":\"0\"}}}" ;; *) echo "RAN $*" ;; esac' > "$T/fakebq/bq"
chmod +x "$T/fakebq/bq"
out=$(env BQRO_PROJECT=from-env KIT_ENV="$T/kit-crlf.env" PATH="$T/fakebq:$PATH" "$BQRO" 'SELECT 1' 2>&1)
check "bqro takes the overlay's BQRO_PROJECT over the environment's" "$out" '^RAN .*--project_id=proj-1 '
out=$(env BQRO_PROJECT=from-env KIT_ENV=/dev/null PATH="$T/fakebq:$PATH" "$BQRO" 'SELECT 1' 2>&1)
check "bqro with no overlay value ignores the environment and asks for a project" "$out" 'no jobs project'
# The one declared list: hook-io's KIT_KEYS, documented in lib/README and kit.env.example.
doc=$(sed -n 's/^  \([A-Z][A-Z0-9_]*\)  .*/\1/p' "$H/lib/README" | tr '\n' ' ')
ex=$(sed -n 's/^\([A-Z][A-Z0-9_]*\)=.*/\1/p' "$H/../kit.env.example" | tr '\n' ' ')
kk=$(sed -n 's/^KIT_KEYS="\(.*\)"$/\1/p' "$H/lib/hook-io")
[ "$doc" = "$kk " ] && [ "$ex" = "$kk " ] && ok "KIT_KEYS, lib/README and kit.env.example list the same keys" \
  || bad "declared keys differ: hook-io '$kk', README '$doc', example '$ex'"

echo "--- tests-ran-mark: python heredoc edit marks ---"
rm -f "$M/edit-$SID"
printf '%s' "$(post "python3 - <<PY
import pathlib
pathlib.Path(\"src/x.py\").write_text(\"a\")
PY" $SID /tmp)" | "$H/tests-ran-mark" >/dev/null
[ -f "$M/edit-$SID" ] && ok "tests-ran-mark python heredoc marks edit" || bad "tests-ran-mark python heredoc did not mark"
rm -f "$M/edit-$SID"
tredit() { jq -cn --arg sid "$SID" --arg f "$1" '{hook_event_name:"PostToolUse",tool_name:"Edit",session_id:$sid,tool_input:{file_path:$f}}' | "$H/tests-ran-mark" >/dev/null; }
mkdir -p "$T/claude-scratch/tasks/T-1-x/scripts" "$T/claude-scratch/tasks/T-1-x/worktrees/w"
tredit "$T/claude-scratch/tasks/T-1-x/scripts/probe.py"
[ ! -f "$M/edit-$SID" ] && ok "tests-ran-mark: a task-folder script is not a source edit" || bad "tests-ran-mark marked a task-folder script"
tredit "$T/claude-scratch/tasks/T-1-x/worktrees/w/app.py"
[ -f "$M/edit-$SID" ] && ok "tests-ran-mark: an edit in a task worktree still marks" || bad "tests-ran-mark ignored a task worktree edit"
rm -f "$M/edit-$SID"
mkdir -p "$T/aw/projects/p/scripts"; printf 'AGENT_WORK_ROOT=%s\n' "$T/aw" > "$T/kit-aw.env"
jq -cn --arg sid "$SID" --arg f "$T/aw/projects/p/scripts/x.py" '{hook_event_name:"PostToolUse",tool_name:"Edit",session_id:$sid,tool_input:{file_path:$f}}' \
  | KIT_ENV="$T/kit-aw.env" "$H/tests-ran-mark" >/dev/null
[ ! -f "$M/edit-$SID" ] && ok "tests-ran-mark: a project script under kit.env AGENT_WORK_ROOT is not a source edit" || bad "tests-ran-mark marked a work-root project script"
rm -f "$M/edit-$SID" "$T/kit-aw.env"; rm -rf "$T/aw"

echo "--- lib/cmd-repo: the repo a command acts in ---"
CR="$T/cr-repo"; rm -rf "$CR"; git init -q -b cr "$CR"; mkdir -p "$CR/sub"
git -C "$CR" config core.trustctime false   # the racy-git case below must not depend on timing
printf 'x = 1\n' > "$CR/a.py"; touch -t 202601010000.00 "$CR/a.py"; git -C "$CR" add a.py; git -C "$CR" -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -qm init
crr() { ( . "$H/lib/cmd-repo" && cmd_repo "$@" && printf '%s' "$CMD_DIR" ); }
crk() { ( . "$H/lib/state" && path_key "$1" ); }
crcase() { [ "$(crr "${@:3}")" = "$2" ] && ok "cmd-repo: $1" || bad "cmd-repo: $1" "got $(crr "${@:3}")"; }
crcase "cd <wt> && runner" "$CR" "cd $CR && project-test tests/x.py" /
crcase "quoted git -C" "$CR" "git -C \"$CR\" push -u origin HEAD" /
crcase "relative cd" "$CR/sub" "cd sub && pytest" "$CR"
crcase "~ cd" "$HOME" "cd ~ && ls" /
crcase "unexpanded \$var falls back to cwd" / 'git -C "$WT" push' /
crcase "missing dir falls back to cwd" / "cd /nonexistent-x && pytest" /
crcase "a cd after the trigger does not count" / "git push; cd $CR && ls" / 'git push'
crcase "an earlier git -C is not the trigger's" "$CR" "git -C ~/.claude log -1 && git push" "$CR" 'git push'
crcase "git -C status, cd, then the runner: the cd counts" "$HOME/.agents" "git -C ~/.claude status && cd ~/.agents && project-test x" "$CR" 'project-test'
crcase "the runner first, a cd after it: the cwd" "$CR" "project-test x && cd ~/.agents && ls" "$CR" 'project-test'
crcase "the trigger's own git -C wins" "$CR" "git -C ~/.claude log && git -C $CR push" / 'git push'
crcase "an ERE trigger" "$CR" "cd $CR && uv run pytest -q" / '~(^|[[:space:]/])pytest([[:space:]]|$)'
rm -f "$M/$SID-cr" "$M/wt-$(crk "$CR")"
printf '%s' "$(post "cd $CR && pytest -q" $SID-cr /)" | "$H/tests-ran-mark" >/dev/null
[ -f "$M/wt-$(crk "$CR")" ] && ok "tests-ran-mark: cd <wt> && pytest records the run under the worktree" \
  || bad "tests-ran-mark: cd <wt> && pytest records the run under the worktree"
rm -f "$M/$SID-cr" "$M/wt-$(crk "$CR")" "$M/lastrun-$SID" "$M/lastrun-$SID".* "$M/lastrun-$SID"-*
printf '%s' "$(post "pytest -q && cd $T && ls" $SID-cr "$CR")" | "$H/tests-ran-mark" >/dev/null
[ -f "$M/wt-$(crk "$CR")" ] && [ ! -f "$M/wt-$(crk "$T")" ] && ok "tests-ran-mark: a cd after the runner does not move the run" \
  || bad "tests-ran-mark: a cd after the runner does not move the run"
rm -f "$M/$SID-cr" "$M/wt-$(crk "$CR")" "$M/wt-$(crk "$T")" "$M/lastrun-$SID" "$M/lastrun-$SID".* "$M/lastrun-$SID"-*
HC="$T/hooks-cr"; rm -rf "$HC"; cp -RH "$H" "$HC"
printf '#!/bin/sh\necho "$1" > "%s/ci-watch.arg"\n' "$T" > "$HC/ci-watch.new"; chmod +x "$HC/ci-watch.new"; mv "$HC/ci-watch.new" "$HC/ci-watch"
rm -f "$T/ci-watch.arg"
# Its own repo: what the push hook leaves running in the background must not race the review-state
# cases below on $CR (seen once in CI as a lost "review edit marked").
CRP="$T/cr-push-repo"; rm -rf "$CRP"; git init -q -b cr "$CRP"
printf '%s' "$(post "git -C \"$CRP\" push -u origin HEAD" $SID-crp /)" | env -u CI_WATCH_ACTIVE "$HC/ci-watch-on-push"
n=0; until [ -f "$T/ci-watch.arg" ] || [ "$n" -ge 20 ]; do sleep 0.1; n=$((n+1)); done
[ "$(cat "$T/ci-watch.arg" 2>/dev/null)" = "$CRP" ] && ok "ci-watch-on-push: git -C <wt> push watches <wt>" \
  || bad "ci-watch-on-push: git -C <wt> push watches <wt>" "$(cat "$T/ci-watch.arg" 2>/dev/null)"
rm -rf "$HC" "$T/ci-watch.arg"

echo "--- tests-ran-mark: a Bash-written source edit marks the review ---"
rvk() { ( . "$H/lib/review-state" 2>/dev/null && "$@" ); }
rvk rv_clear_edits $SID-rv "$CR"
printf 'x = 2\n' > "$CR/a.py"
# Same size as the committed `x = 1` and pinned to the index's own second: git's racy-clean
# window, which a plain copy of the index closes (the edit then reads as unchanged).
touch -t 202601010000.00 "$CR/a.py" "$CR/.git/index"
printf '%s' "$(post "python3 - <<PY
open(\"a.py\", \"w\").write(\"x = 2\")
PY" $SID-rv "$CR")" | "$H/tests-ran-mark" >/dev/null
rvk rv_edited $SID-rv "$CR" && ok "heredoc open(..., 'w') on a tracked .py: review edit marked" \
  || bad "heredoc open(..., 'w') on a tracked .py: review edit marked"
rvk rv_edited $SID-other "$CR" && bad "...for that session only" || ok "...for that session only"
rvk rv_clear_edits $SID-rv "$CR"; git -C "$CR" checkout -q -- a.py
printf '%s' "$(post "python3 - <<PY
open(\"a.py\", \"w\").write(\"x = 1\")
PY" $SID-rv "$CR")" | "$H/tests-ran-mark" >/dev/null
rvk rv_edited $SID-rv "$CR" && bad "a write that left the tracked tree unchanged marks nothing" \
  || ok "a write that left the tracked tree unchanged marks nothing"
rvk rv_clear_edits $SID-rv "$CR"
printf 'y = 1\n' > "$CR/new.py"
printf '%s' "$(post "cat > new.py <<PY
y = 1
PY" $SID-rv "$CR")" | "$H/tests-ran-mark" >/dev/null
rvk rv_edited $SID-rv "$CR" && ok "cat > new.py (untracked): review edit marked" || bad "cat > new.py (untracked): review edit marked"
rvk rv_clear_edits $SID-rv "$CR"; rm -f "$CR/new.py"
printf 'x = 3\n' > "$CR/a.py"; git -C "$CR" add a.py
printf '%s' "$(post "sed -i '' 's/1/3/' a.py && git add a.py" $SID-rv "$CR")" | "$H/tests-ran-mark" >/dev/null
rvk rv_edited $SID-rv "$CR" && ok "sed -i ... && git add (staged): review edit marked" || bad "sed -i ... && git add (staged): review edit marked"
rvk rv_clear_edits $SID-rv "$CR"; git -C "$CR" reset -q -- a.py; git -C "$CR" checkout -q -- a.py
printf 'x = 9\n' > "$CR/a.py"; printf 'k: 1\n' > "$CR/conf.yaml"
printf '%s' "$(post "echo 'k: 1' > conf.yaml" $SID-rv "$CR")" | "$H/tests-ran-mark" >/dev/null
rvk rv_edited $SID-rv "$CR" && bad "another session's unstaged edit (not named by the command) marks nothing" \
  || ok "another session's unstaged edit (not named by the command) marks nothing"
git -C "$CR" checkout -q -- a.py; rm -f "$CR/conf.yaml"
rm -rf "$CR" "$M/edit-$SID-rv" "$M/edit-wt-$(crk "$CR")"

echo "--- unfinished-work + ci-watch statuses ---"
R=$T/uw-repo; git init -q -b $SID-uw "$R" && git -C "$R" -c user.name=t -c user.email=t@t commit -q --allow-empty -m init
# The branch -> PR pointer's on-disk contract with ci-watch (lib/state).
PTR=$HOME/.claude/tmp/ci-branch-$SID-uw.pr; SF=$HOME/.claude/tmp/ci-watch-$PRN.status
printf '%s\n' "$PRN" > "$PTR"
# unfinished-work and stop-chime keep their markers in the shared $TMPDIR by session id: every id
# here carries $SID, so a concurrent run neither reads nor deletes this run's markers (CX-7).
uw() { printf '%s\n' "$3" > "$SF"; run unfinished-work "$2" "$(stop "$SID-$1" "$R")"; }
uw uw1 deny  "waiting for 'tests' run on abcdef12 (20s elapsed)"
uw uw1 allow "waiting for 'tests' run on abcdef12 (40s elapsed)"
uw uw1 allow "running 123 on abcdef12"
uw uw1 deny  "RED — 3 cycles exhausted, needs a human"
reason=$(printf 'running 1\n' > "$SF"; stop "$SID-uw-rx" "$R" | "$H/unfinished-work" | jq -r .reason)
printf '%s' "$reason" | grep -q "head -1 $SF" && ok "waiter reads the status with head -1" || bad "waiter does not use head -1: $reason"
rx=$(printf '%s' "$reason" | sed -n "s/.*until grep -qE '\([^']*\)'.*/\1/p")
[ -n "$rx" ] || bad "could not extract the waiter regex"
# Every status ci-watch writes: terminal ones must end the waiter and not block; pending ones
# must keep the waiter going and block a fresh session.
n=0
while IFS='|' read -r kind st; do
  n=$((n+1))
  if [ "$kind" = pending ]; then
    printf '%s\n' "$st" | grep -qE "$rx" && bad "waiter stops on pending: $st" || ok "waiter continues: $st"
    uw "uw-p$n" deny "$st"
  else
    printf '%s\n' "$st" | grep -qE "$rx" && ok "waiter stops: $st" || bad "waiter never stops on: $st"
    [ "$kind" = red ] && uw "uw-t$n" deny "$st" || uw "uw-t$n" allow "$st"
  fi
done <<'EOF'
pending|watching PR #1 (b), workflow 'tests'
pending|waiting for 'tests' run on abcdef12 (20s elapsed)
pending|waiting for label 'run-ci' to start CI (20s elapsed)
pending|running 123 on abcdef12
pending|repairing run 123 (cycle 1/3)
pending|run 123 cancelled (superseded) — re-resolving
pending|run 123 skipped — re-resolving
done|green
done|green after 1 repair cycle(s)
red|RED — 3 cycles exhausted, needs a human (agent log: x)
red|RED — run 123 failed (no-act mode, agent not invoked)
red|gave up after 7200s (deadline)
done|idle — 'run-ci' label not set
done|idle — 'run-ci' label not set (run 123: skipped)
done|idle — run 123 skipped, no newer 'tests' run for abcdef12
done|idle — not watched, 2 watchers already live
done|no 'tests' run; workflows seen: none
EOF
# A running waiter on the status file means the session is correctly idle: no block.
printf 'running 123 on abcdef12\n' > "$SF"
/bin/bash -c "until grep -q never-matches $SF; do sleep 1; done" & wpid=$!
sleep 1
run unfinished-work allow "$(stop "$SID-uw-w" "$R")"
kill "$wpid" 2>/dev/null; wait "$wpid" 2>/dev/null
run unfinished-work deny "$(stop "$SID-uw-w" "$R")"
[ -f "${TMPDIR:-/tmp}/claude-stop-block/$SID-uw-w" ] && ok "a block marks the session for stop-chime" || bad "unfinished-work blocked without the stop-block marker"
rm -f "$PTR" "$SF" "${TMPDIR:-/tmp}/claude-unfinished/$SID"-uw* "${TMPDIR:-/tmp}/claude-stop-block/$SID"-uw*

echo "--- ci-watch: one watcher per PR ---"
# ci-watch's own lock block (from its "One watcher per PR" comment to the trap), run by six starters
# released together: exactly one may pass, and it removes the lock on exit.
CL=$T/ciwatch-lock; mkdir -p "$CL"
sed -n '/^# One watcher per PR/,/^trap /p' "$H/ci-watch" > "$CL/block.sh"
printf '%s\n' 'lock=$1/ci-watch-1.lock; tag=1' 'until [ -e "$1/go" ]; do :; done' '. "$1/block.sh"' 'echo took; sleep 1' > "$CL/starter.sh"
multi=0
for r in 1 2 3; do
  rm -f "$CL/go" "$CL"/out.*
  for i in 1 2 3 4 5 6; do /bin/bash "$CL/starter.sh" "$CL" > "$CL/out.$i" 2>&1 & done
  sleep 0.3; touch "$CL/go"; wait
  [ "$(cat "$CL"/out.* | grep -c '^took')" = 1 ] || multi=$((multi + 1))
done
[ -s "$CL/block.sh" ] && [ "$multi" = 0 ] && [ ! -e "$CL/ci-watch-1.lock" ] \
  && ok "ci-watch: of six starters released together, one takes the PR lock and releases it" \
  || bad "ci-watch lock: $multi of 3 rounds let other than one watcher start (block: $(wc -l < "$CL/block.sh") lines)"

echo "--- ci-watch: the repair agent's claude call ---"
# ci-watch's repair call (its `repair_prompt "$run_id"` line through `|| alert`), run against a fake
# claude that records its arguments. It must start no MCP server: --strict-mcp-config, no --mcp-config.
CR=$T/ciwatch-repair; mkdir -p "$CR/bin"
sed -n '/^  repair_prompt "\$run_id"/,/|| alert/p' "$H/ci-watch" > "$CR/call.sh"
printf '%s\n' '#!/bin/bash' 'printf "%s\n" "$@" > "$ARGS_OUT"' > "$CR/bin/claude"; chmod +x "$CR/bin/claude"
(
  repair_prompt() { echo prompt; }
  alert() { :; }
  run_id=1 logfile=/dev/null perms_run=/dev/null agentlog=/dev/null prnum=1 cycle=1
  export PATH="$CR/bin:$PATH" ARGS_OUT="$CR/args"
  . "$CR/call.sh"
)
args=$(cat "$CR/args" 2>/dev/null)
check "ci-watch: the repair agent runs with --strict-mcp-config" "$args" '^--strict-mcp-config$'
check "...and no --mcp-config, so it loads no MCP server" "${args:-none}" '^--mcp-config' absent

echo "--- CI_WATCH_ACTIVE: stop-chime, retro-extract ---"
SB="${TMPDIR:-/tmp}/claude-stop-block"; mkdir -p "$SB"; touch "$SB/$SID-chime"
printf '{"session_id":"%s"}' "$SID-chime" | CI_WATCH_ACTIVE=1 "$H/stop-chime"; rc=$?
sleep 3
[ "$rc" = 0 ] && [ -f "$SB/$SID-chime" ] && ok "stop-chime skips under CI_WATCH_ACTIVE" || bad "stop-chime ran under CI_WATCH_ACTIVE (rc=$rc)"
# Control: without the env the (fresh) marker is consumed silently, no chime.
printf '{"session_id":"%s"}' "$SID-chime" | "$H/stop-chime"
sleep 3
[ ! -f "$SB/$SID-chime" ] && ok "stop-chime consumes the block marker without the env" || bad "stop-chime control: marker not consumed"
rm -f "$SB/$SID-chime"
# retro-extract at SessionEnd.
TR=$T/hooktestdigest.jsonl; DG=$T/retro-root
printf '%s\n' '{"type":"system","subtype":"compact_boundary"}' > "$TR"
rm -rf "$DG"
jq -cn --arg p "$TR" --arg sid "$SID-retro" '{transcript_path:$p,session_id:$sid}' | CI_WATCH_ACTIVE=1 CLAUDE_OUT_ROOT=$DG KIT_ENV=/dev/null "$H/retro-extract"; rc=$?
[ "$rc" = 0 ] && [ ! -d "$DG/retro" ] && ok "retro-extract skips under CI_WATCH_ACTIVE" || bad "retro-extract wrote under CI_WATCH_ACTIVE (rc=$rc)"
jq -cn --arg p "$TR" --arg sid "$SID-retro" '{transcript_path:$p,session_id:$sid}' | CLAUDE_OUT_ROOT=$DG KIT_ENV=/dev/null "$H/retro-extract"
grep -qs '^- \[auto\] .* compaction 1 compaction' "$DG"/retro/*.md && ok "retro-extract writes the inbox without the env" || bad "retro-extract control: no inbox line"
rm -rf "$DG"

echo "--- tab-signal ---"
out=$(printf '{"session_id":"%s","prompt":"hello","cwd":"/tmp"}' "$SID-tab" | CI_WATCH_ACTIVE=1 "$H/tab-signal" busy)
[ -z "$out" ] && ok "tab-signal busy: no naming request under CI_WATCH_ACTIVE" || bad "tab-signal asked a headless ci-watch agent to name its tab"
out=$(printf '{"session_id":"%s","prompt":"hello","cwd":"/tmp"}' "$SID-tab" | "$H/tab-signal" busy)
[ -z "$out" ] && ok "tab-signal busy: never asks the model to name the tab (binding names it)" || bad "tab-signal busy still injects a naming request" "$out"
rm -f "$HOME/.claude/state/tab-label/$SID-tab"*

echo "--- edit-guard ---"
# eg <tool> <deny|allow|silent> <file_path> <label> [agent_id]: label goes in session_id for the report.
eg() { run edit-guard "$2" "$(jq -cn --arg t "$1" --arg f "$3" --arg s "$4" --arg a "${5:-}" \
  '{tool_name:$t,hook_event_name:"PreToolUse",session_id:$s,cwd:"/",tool_input:{file_path:$f}} + (if $a == "" then {} else {agent_id:$a} end)')"; }
mkdir -p "$T/eg-dir"; : > "$T/eg-target"; : > "$T/eg-dir/x"
ln -s eg-target "$T/eg-link"; ln -s eg-dir "$T/eg-dirlink"
eg Edit deny "$HOME/.claude/settings.json" "edit-guard settings.json (rendered)"
eg Write deny "$HOME/.claude/mcp.json" "edit-guard mcp.json (rendered)"
# An account's settings.json link, under a temp HOME that has one: the real HOME may have no
# ~/.claude-work (the public kit's CI), and the case must not depend on it.
TH=$T/eg-home; mkdir -p "$TH/.claude" "$TH/.claude-work"; : > "$TH/.claude/settings.json"
ln -s ../.claude/settings.json "$TH/.claude-work/settings.json"
HOME=$TH eg MultiEdit deny "$TH/.claude-work/settings.json" "edit-guard an account's settings.json link"
eg Edit deny "~/.claude/settings.json" "edit-guard ~ in the path"
eg Edit deny "$HOME/.claude/settings.json" "edit-guard subagent call" agent-1
eg Edit deny "$T/eg-link" "edit-guard a link under ~/.claude"
eg Write allow "$T/eg-target" "edit-guard the link's target"
eg Edit allow "$T/eg-dirlink/x" "edit-guard a file through a linked dir"
eg Edit allow "$HOME/.claude/hooks/bash-guards" "edit-guard a kit hook through ~/.claude/hooks"
eg Edit allow "$HOME/.agents/hosts/claude/settings.base.json" "edit-guard the settings source"
eg Edit allow "$HOME/Code/no-such-repo/x.py" "edit-guard outside the host dirs"
out=$(printf 'not json' | "$H/edit-guard"); rc=$?
[ "$rc" = 0 ] && [ -z "$out" ] && ok "edit-guard: bad stdin is silent, rc 0" || bad "edit-guard bad stdin: rc=$rc out=$out"
reason=$(jq -cn --arg f "$T/eg-link" '{tool_name:"Edit",hook_event_name:"PreToolUse",session_id:"t",cwd:"/",tool_input:{file_path:$f}}' | "$H/edit-guard" | jq -r '.hookSpecificOutput.permissionDecisionReason')
check "edit-guard link reason names the real target" "$reason" "Edit the target instead: .*/eg-target"
reason=$(jq -cn --arg f "$HOME/.claude/settings.json" '{tool_name:"Edit",hook_event_name:"PreToolUse",session_id:"t",cwd:"/",tool_input:{file_path:$f}}' | "$H/edit-guard" | jq -r '.hookSpecificOutput.permissionDecisionReason')
check "edit-guard settings reason names the source and render" "$reason" "settings\.base\.json.*agent-kit render"

# comment-guard, review-agent-mark, the review gates and the missing-lib policy are covered by
# hooks/tests/review-gates.sh.

echo "--- context-watch: the handoff nudge, once per band; the bounded tail read; idle notice ---"
CW=$T/cw; mkdir -p "$CW"
printf 'CONTEXT_HANDOFF_AT=600000\n' > "$CW/kit.env"
# aline <input> <cache creation> <cache read> [ts] [model] [sidechain]: an assistant line shaped like Claude Code's.
aline() {
  printf '{"parentUuid":"p","isSidechain":%s,"type":"assistant","timestamp":"%s","message":{"model":"%s","role":"assistant","content":[{"type":"text","text":"x"}],"usage":{"input_tokens":%s,"cache_creation_input_tokens":%s,"cache_read_input_tokens":%s,"output_tokens":9,"output_tokens_details":{"thinking_tokens":1},"cache_creation":{"ephemeral_1h_input_tokens":%s,"ephemeral_5m_input_tokens":0},"iterations":[{"input_tokens":1,"cache_read_input_tokens":1}]}}}\n' \
    "${6:-false}" "${4:-2026-10-03T10:00:00.000Z}" "${5:-claude-opus-5-5}" "$1" "$2" "$3" "$2"
}
filler() { head -c "$1" /dev/zero | tr '\0' 'f' | sed 's/^/{"type":"user","message":{"content":"/; s/$/"}}/'; echo; }
cwp() { jq -cn --arg s "$1" --arg t "$2" --arg e "${3:-PostToolUse}" --argjson x "${4:-null}" \
  '{session_id:$s,transcript_path:$t,cwd:"/",hook_event_name:$e,tool_name:"Read",tool_input:{file_path:"/x"},tool_response:{content:"\"usage\":{\"input_tokens\":999999}"}} * ($x // {})'; }
# flat <output>: one line, so '^<rc 0>$' means "rc 0 and nothing else", not "a line that says rc 0".
flat() { printf '%s' "$1" | tr '\n' ' '; }
cw() { printf '%s' "$1" | PATH=${CW_PATH:-$PATH} KIT_ENV=${CW_KIT:-$CW/kit.env} CLAUDE_STATE_DIR=$CW/state CLAUDE_OUT_ROOT=$CW/root CLAUDE_PROJECT_DIR= CONTEXT_WATCH_NOW=${CW_NOW:-} "$H/context-watch"; echo "<rc $?>"; }
{ aline 1 1 1; aline 2 10347 602000; } > "$CW/t1.jsonl"
out=$(cw "$(cwp cw-s1 "$CW/t1.jsonl")")
check "context-watch: 612K crosses 600K: additionalContext names the context and HANDOFF.md" "$out" '"additionalContext": "CONTEXT 612K: past the 600K handoff point.*HANDOFF\.md.*agent-task retro'
check "...rc 0" "$out" '<rc 0>$'
check "...an unbound session is told to bind or write the handoff in its OUT DIR" "$out" "agent-task bind <project>.*--session cw-s1.*HANDOFF\.md .state, next steps, decisions. in $CW/root/"
out=$(cw "$(cwp cw-s1 "$CW/t1.jsonl")")
check "context-watch: the next tool call in the same band is silent" "$(flat "$out")" '^<rc 0>$'
out=$(cw "$(cwp cw-s1 "$CW/t1.jsonl" Stop)")
check "...and so is Stop" "$(flat "$out")" '^<rc 0>$'
aline 0 0 712000 >> "$CW/t1.jsonl"
out=$(cw "$(cwp cw-s1 "$CW/t1.jsonl")")
check "context-watch: the next 100K band (712K) nudges once more" "$out" 'CONTEXT 712K'
aline 0 0 300000 >> "$CW/t1.jsonl"
out=$(cw "$(cwp cw-s1 "$CW/t1.jsonl")")
[ "$out" = "<rc 0>" ] && [ ! -e "$CW/state/context-watch/cw-s1" ] && ok "context-watch: under the threshold (a compaction) is silent and re-arms" || bad "context-watch re-arm: $out"
aline 0 0 605000 >> "$CW/t1.jsonl"
out=$(cw "$(cwp cw-s1 "$CW/t1.jsonl")")
check "...so crossing again after it nudges again" "$out" 'CONTEXT 605K'
out=$(cw "$(cwp cw-s2 "$CW/t1.jsonl" Stop '{"stop_hook_active":true}')")
check "context-watch: Stop while stop_hook_active defers" "$(flat "$out")" '^<rc 0>$'
out=$(cw "$(cwp cw-s2 "$CW/t1.jsonl" Stop)")
check "context-watch: Stop at the threshold blocks once" "$out" '"decision": "block"'
check "...with the handoff text" "$out" '"reason": "CONTEXT 605K: past the 600K'
out=$(cw "$(cwp cw-s3 "$CW/t1.jsonl" PostToolUse '{"agent_id":"a1","agent_type":"engineer"}')")
check "context-watch: a subagent's tool call is silent" "$(flat "$out")" '^<rc 0>$'
out=$(CW_KIT=/dev/null cw "$(cwp cw-s4 "$CW/t1.jsonl")")
check "context-watch: no overlay value falls back to 600000" "$out" 'CONTEXT 605K: past the 600K'
printf 'CONTEXT_HANDOFF_AT=0\n' > "$CW/off.env"
out=$(CW_KIT=$CW/off.env cw "$(cwp cw-s5 "$CW/t1.jsonl")")
check "context-watch: CONTEXT_HANDOFF_AT=0 turns it off" "$(flat "$out")" '^<rc 0>$'
out=$(CW_KIT=$CW/off.env CW_NOW=$(date -j -u -f '%Y-%m-%dT%H:%M:%S' 2026-10-03T11:30:00 +%s) cw "$(cwp cw-s5 "$CW/t1.jsonl" UserPromptSubmit)")
check "...but not the idle-resume notice (90 min at 605K)" "$out" '"systemMessage": "Idle 90 min at 605K'
printf 'CONTEXT_HANDOFF_AT=700000\n' > "$CW/high.env"
out=$(CW_KIT=$CW/high.env cw "$(cwp cw-s6 "$CW/t1.jsonl")")
check "context-watch: the overlay's threshold is used (605K under 700K)" "$(flat "$out")" '^<rc 0>$'
{ aline 0 0 650000; aline 0 0 640000 2026-10-03T10:00:00.000Z claude-opus-5-5 true; aline 0 0 0; } > "$CW/t2.jsonl"
out=$(cw "$(cwp cw-s7 "$CW/t2.jsonl")")
check "context-watch: a sidechain line and a zero (synthetic) usage are skipped" "$out" 'CONTEXT 650K'
{ aline 0 0 620000; filler 200000; } > "$CW/t3.jsonl"
out=$(cw "$(cwp cw-s8 "$CW/t3.jsonl")")
check "context-watch: past 32K of tool output, the one wider read finds the usage" "$out" 'CONTEXT 620K'
{ aline 0 0 900000; filler 1100000; } > "$CW/t4.jsonl"
out=$(cw "$(cwp cw-s9 "$CW/t4.jsonl")")
check "context-watch: a usage beyond the bounded tail (1M) is not read" "$(flat "$out")" '^<rc 0>$'
# A tail with no usage line (ctx 0) is no reading: it must not re-arm the band like a compaction.
aline 0 0 620000 > "$CW/t7.jsonl"
out=$(cw "$(cwp cw-s12 "$CW/t7.jsonl")")
check "context-watch: 620K nudges (setup for the unreadable tail)" "$out" 'CONTEXT 620K'
filler 1100000 >> "$CW/t7.jsonl"
out=$(cw "$(cwp cw-s12 "$CW/t7.jsonl")")
[ "$out" = "<rc 0>" ] && [ -f "$CW/state/context-watch/cw-s12" ] && ok "context-watch: a tail with no usage line is silent and keeps the band marker" || bad "context-watch ctx 0 re-arm: $out"
aline 0 0 622000 >> "$CW/t7.jsonl"
out=$(cw "$(cwp cw-s12 "$CW/t7.jsonl")")
check "...so 622K in the same band stays silent" "$(flat "$out")" '^<rc 0>$'
# The band marker is written only once the output exists: a failing jq loses no nudge.
mkdir -p "$CW/nojq"; printf '#!/bin/sh\nexit 1\n' > "$CW/nojq/jq"; chmod +x "$CW/nojq/jq"
out=$(CW_PATH=$CW/nojq:$PATH cw "$(cwp cw-s13 "$CW/t1.jsonl")")
[ "$out" = "<rc 0>" ] && [ ! -e "$CW/state/context-watch/cw-s13" ] && ok "context-watch: no jq output -> silent, the band is not used up" || bad "context-watch failed emit: $out"
out=$(cw "$(cwp cw-s13 "$CW/t1.jsonl")")
check "...so the next call with jq nudges" "$out" 'CONTEXT 605K'
out=$(printf '%s' "$(cwp cw-s14 "$CW/t1.jsonl" Stop)" | PATH=$CW/nojq:$PATH KIT_ENV=$CW/kit.env CLAUDE_STATE_DIR=$CW/state CLAUDE_OUT_ROOT=$CW/root CLAUDE_PROJECT_DIR= "$H/context-watch" 2>&1; echo "<rc $?>")
check "context-watch: Stop with no jq blocks by exit 2" "$out" '^<rc 2>$'
check "...the reason on stderr" "$out" '^CONTEXT 605K: past the 600K'
[ -f "$CW/state/context-watch/cw-s14" ] && ok "...and that block uses up the band" || bad "context-watch Stop no-jq marker missing"
for p in '' 'not json' '{}' '{"hook_event_name":"PostToolUse","session_id":"x","transcript_path":"/nonexistent"}' "$(cwp cw-s10 "$CW")"; do
  out=$(cw "$p")
  check "context-watch: odd payload ${p:0:40} is silent, rc 0" "$(flat "$out")" '^<rc 0>$'
done
# Idle: the last assistant message at 10:00Z; 612,349 tokens x $8/MTok = 489 cents.
now=$(date -j -u -f '%Y-%m-%dT%H:%M:%S' 2026-10-03T11:30:00 +%s)
out=$(CW_NOW=$now cw "$(cwp cw-i1 "$CW/t1.jsonl" UserPromptSubmit)")
check "context-watch idle: 90 min at 605K is a user-only systemMessage with the cache cost" "$out" '"systemMessage": "Idle 90 min at 605K context: this resume re-writes about \$4\.84 of expired cache \(605K x \$8/MTok\)'
check "...and never additionalContext or a block" "$out" 'additionalContext|"block"' absent
{ aline 2 10347 602000 2026-10-03T10:00:00.000Z claude-sonnet-5-5; } > "$CW/t5.jsonl"
out=$(CW_NOW=$now cw "$(cwp cw-i2 "$CW/t5.jsonl" UserPromptSubmit)")
check "context-watch idle: Sonnet prices the write at \$4/MTok (612,349 -> \$2.44)" "$out" 'about \$2\.44 .*612K x \$4/MTok'
out=$(CW_NOW=$((now - 31 * 60)) cw "$(cwp cw-i3 "$CW/t1.jsonl" UserPromptSubmit)")
check "context-watch idle: 59 minutes is silent" "$(flat "$out")" '^<rc 0>$'
{ aline 0 0 150000; } > "$CW/t6.jsonl"
out=$(CW_NOW=$now cw "$(cwp cw-i4 "$CW/t6.jsonl" UserPromptSubmit)")
check "context-watch idle: 150K is silent" "$(flat "$out")" '^<rc 0>$'

echo "--- pre-compact: HANDOFF.auto.md only when HANDOFF.md is stale ---"
PC=$CW/pcrepo; mkdir -p "$PC"
{ printf '%s\n' '{"type":"user","message":{"role":"user","content":"first ask"}}' \
    '{"type":"user","isMeta":true,"message":{"role":"user","content":"meta line"}}' \
    '{"type":"user","message":{"role":"user","content":"<command-name>/model</command-name>"}}' \
    '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Edit","input":{"file_path":"/w/a.py","old_string":"","new_string":"x"}}]}}' \
    '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"ok"}]}}'
  # The task tools as Claude Code 2.1.288 writes them (TodoWrite is gone): the id is in the result line.
  n=0
  for s in 'ship it' 'done one' 'dropped' 'later'; do
    n=$((n + 1))
    printf '{"parentUuid":"p","isSidechain":false,"message":{"model":"claude-opus-5-5","type":"message","role":"assistant","content":[{"type":"tool_use","id":"tc%s","name":"TaskCreate","input":{"subject":"%s","description":"%s task"},"caller":{"type":"direct"}}]},"type":"assistant"}\n' "$n" "$s" "$s"
    printf '{"parentUuid":"p","isSidechain":false,"type":"user","message":{"role":"user","content":[{"tool_use_id":"tc%s","type":"tool_result","content":"Task #%s created successfully: %s"}]},"toolUseResult":{"task":{"id":"%s","subject":"%s"}}}\n' "$n" "$n" "$s" "$n" "$s"
  done
  for u in '1 in_progress' '2 completed' '3 deleted'; do
    printf '{"parentUuid":"p","isSidechain":false,"message":{"model":"claude-opus-5-5","type":"message","role":"assistant","content":[{"type":"tool_use","id":"tu%s","name":"TaskUpdate","input":{"taskId":"%s","status":"%s"},"caller":{"type":"direct"}}]},"type":"assistant"}\n' "${u% *}" "${u% *}" "${u#* }"
    printf '{"parentUuid":"p","isSidechain":false,"type":"user","message":{"role":"user","content":[{"tool_use_id":"tu%s","type":"tool_result","content":"Updated task #%s status"}]},"toolUseResult":{"success":true,"taskId":"%s","updatedFields":["status"]}}\n' "${u% *}" "${u% *}" "${u% *}"
  done
  printf '%s\n' \
    '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"second ask"}]}}' \
    '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t3","name":"Write","input":{"file_path":"/w/b.md","content":"y"}}]}}'; } > "$CW/pc.jsonl"
pcp() { jq -cn --arg s "$1" --arg t "$CW/pc.jsonl" --arg d "$PC" --argjson x "${2:-null}" '{session_id:$s,transcript_path:$t,cwd:$d,hook_event_name:"PreCompact",trigger:"auto"} * ($x // {})'; }
pc() { printf '%s' "$1" | KIT_ENV=/dev/null CLAUDE_STATE_DIR=$CW/state CLAUDE_OUT_ROOT=$CW/root CLAUDE_PROJECT_DIR=$PC "$H/pre-compact"; echo "<rc $?>"; }
out=$(pc "$(pcp pcsess01)")
# The OUT DIR is <root>/<repo>/<date>-<sid8>; $T sits inside ~/.claude's repo, so <repo> is that one.
auto=$(find "$CW/root" -path '*-pcsess01/HANDOFF.auto.md' 2>/dev/null | head -1)
check "pre-compact: no HANDOFF.md -> HANDOFF.auto.md in the OUT DIR" "$out" "wrote $CW/root/[^/]*/[0-9-]*-pcsess01/HANDOFF\.auto\.md"
[ -n "$auto" ] || auto=$CW/missing/HANDOFF.auto.md
check "...rc 0" "$out" '<rc 0>$'
body=$(cat "$auto" 2>/dev/null)
check "...with the last user prompts (harness lines left out)" "$body" '^- first ask$'
check "...the second prompt from a text block" "$body" '^- second ask$'
check "...no meta or command lines" "$body" 'meta line|command-name' absent
check "...the files Edit/Write touched, most recent last" "$(printf '%s' "$body" | grep -A3 '^## Files' | tr '\n' ' ')" '/w/a\.py - /w/b\.md'
check "...the open tasks from TaskCreate/TaskUpdate" "$(printf '%s' "$body" | grep -A3 '^## Open todos' | tr '\n' ' ')" '^## Open todos - \[in_progress\] ship it - \[pending\] later $'
check "...and the git section" "$body" '^- branch: '
dir=${auto%/HANDOFF.auto.md}
rm -f "$auto"; printf 'state\n' > "$dir/HANDOFF.md"
out=$(pc "$(pcp pcsess01)")
[ "$out" = "<rc 0>" ] && [ ! -e "$auto" ] && ok "pre-compact: HANDOFF.md written in the last 30 minutes, no nudge -> nothing written" || bad "pre-compact fresh handoff: $out"
mkdir -p "$CW/state/context-watch"; touch "$CW/state/context-watch/pcsess01"
touch -t 202601010000 "$dir/HANDOFF.md"
out=$(pc "$(pcp pcsess01)")
check "pre-compact: HANDOFF.md older than the nudge -> written" "$out" 'not updated after the context handoff nudge; wrote'
rm -f "$auto"; touch "$dir/HANDOFF.md"
out=$(pc "$(pcp pcsess01)")
[ "$out" = "<rc 0>" ] && [ ! -e "$auto" ] && ok "pre-compact: HANDOFF.md updated after the nudge -> nothing written" || bad "pre-compact updated handoff: $out"
printf 'old snapshot\n' > "$auto"; touch -t 202601010000 "$auto"; rm -f "$dir/HANDOFF.auto.prev.md"
out=$(pc "$(pcp pcsess01)")
[ "$out" = "<rc 0>" ] && [ ! -e "$auto" ] && grep -q 'old snapshot' "$dir/HANDOFF.auto.prev.md" 2>/dev/null \
  && ok "pre-compact: HANDOFF.md fresh -> an earlier HANDOFF.auto.md is renamed HANDOFF.auto.prev.md" || bad "pre-compact stale auto kept: $out"
out=$(pc "$(pcp pcsess02 '{"agent_id":"a1"}')")
check "pre-compact: a subagent's compaction writes nothing" "$(flat "$out")" '^<rc 0>$'
out=$(pc 'not json')
check "pre-compact: bad stdin is silent, rc 0" "$(flat "$out")" '^<rc 0>$'
printf 'auto snapshot\n' > "$dir/HANDOFF.auto.md"
out=$(jq -cn '{session_id:"pcsess01",hook_event_name:"SessionStart",source:"compact"}' | KIT_ENV=/dev/null CLAUDE_OUT_ROOT=$CW/root CLAUDE_STATE_DIR=$CW/state TAB_LABEL_DIR=$CW/labels CLAUDE_PROJECT_DIR=$PC "$H/session-context")
check "session-context on compact re-injects HANDOFF.md" "$out" "^HANDOFF\.md \($dir/HANDOFF\.md\):$"
check "...and HANDOFF.auto.md" "$out" "^HANDOFF\.auto\.md \($dir/HANDOFF\.auto\.md\):$"
touch -t 202601010000 "$dir/HANDOFF.auto.md"
out=$(jq -cn '{session_id:"pcsess01",hook_event_name:"SessionStart",source:"compact"}' | KIT_ENV=/dev/null CLAUDE_OUT_ROOT=$CW/root CLAUDE_STATE_DIR=$CW/state TAB_LABEL_DIR=$CW/labels CLAUDE_PROJECT_DIR=$PC "$H/session-context")
check "session-context on compact: a HANDOFF.auto.md older than HANDOFF.md is left out" "$out" 'HANDOFF\.auto\.md \(' absent
check "...HANDOFF.md still comes back" "$out" "^HANDOFF\.md \($dir/HANDOFF\.md\):$"

echo "--- status line: context tokens with cache, absolute colours, cost, prompt cache ---"
# Beside a HOOKS_DIR copy (textual parent, so a symlinked copy can carry its own), else the live one.
SL="${H%/}"; SL="${SL%/*}/hosts/claude/statusline-command.sh"; [ -f "$SL" ] || SL=$HOME/.claude/statusline-command.sh
ESC=$(printf '\033')
N=1790000000
# sl <prompt_cache JSON|none> [extra JSON]: the rendered line, colour codes kept.
sl() {
  local pc=${1:-none}
  jq -cn --argjson pc "$([ "$pc" = none ] && echo null || echo "$pc")" --argjson x "${2:-null}" \
    '({model:{id:"claude-opus-5-5",display_name:"Opus 5.5"},cwd:"/",
      context_window:{used_percentage:45,total_input_tokens:450000,context_window_size:1000000,
        current_usage:{input_tokens:2,cache_creation_input_tokens:0,cache_read_input_tokens:449998}}}
     + (if $pc == null then {} else {prompt_cache:$pc} end)) * ($x // {})' \
    | KIT_ENV=/dev/null STATUSLINE_NOW=$N bash "$SL"
}
plain() { printf '%s' "$1" | sed "s/${ESC}\[[0-9;]*m//g"; }
warm() { printf '{"warm":true,"caching_observed":true,"ttl":"%s","expires_at":%s}' "$1" "$2"; }
out=$(sl none)
[ "$(plain "$out")" = "Opus 5.5 | / | ctx:45% 450K/1000K" ] && ok "status line: no prompt_cache shows no cache segment" || bad "status line absent: $(plain "$out")"
check "...450K (input + cache creation + cache read) is yellow" "$out" "${ESC}\[33m45%"
out=$(sl none '{"context_window":{"current_usage":{"input_tokens":5,"cache_read_input_tokens":611995}},"cost":{"total_cost_usd":12.345}}')
[ "$(plain "$out")" = "Opus 5.5 | / | ctx:45% 612K/1000K | \$12.35" ] && ok "status line: 612K with the session cost" || bad "status line cost: $(plain "$out")"
check "...612K is red" "$out" "${ESC}\[31m45%"
out=$(sl "$(warm 1h $((N + 47 * 60 + 20)))")
[ "$(plain "$out")" = "Opus 5.5 | / | ctx:45% 450K/1000K | cache ● 47m" ] && ok "status line: warm 1h with 47m left" || bad "status line warm 47m: $(plain "$out")"
check "...green" "$out" "${ESC}\[32mcache ● 47m"
out=$(sl "$(warm 1h $((N + 9 * 60 + 5)))")
[ "$(plain "$out")" = "Opus 5.5 | / | ctx:45% 450K/1000K | cache ● 9m" ] && ok "status line: warm 1h with 9m left" || bad "status line warm 9m: $(plain "$out")"
check "...yellow (at or under a quarter of the TTL)" "$out" "${ESC}\[33mcache ● 9m"
out=$(sl "$(warm 5m $((N + 40)))")
[ "$(plain "$out")" = "Opus 5.5 | / | ctx:45% 450K/1000K | cache ● 0:40" ] && ok "status line: warm 5m with 40s left shows m:ss" || bad "status line warm 0:40: $(plain "$out")"
check "...yellow" "$out" "${ESC}\[33mcache ● 0:40"
out=$(sl '{"warm":false,"caching_observed":true,"ttl":"1h","expires_at":null}')
[ "$(plain "$out")" = "Opus 5.5 | / | ctx:45% 450K/1000K | cache ○ cold · \$3.60 to rewarm" ] && ok "status line: cold at 450K on Opus 5.5 costs \$3.60 to rewarm" || bad "status line cold: $(plain "$out")"
check "...red" "$out" "${ESC}\[31mcache ○ cold"
out=$(sl '{"warm":false,"caching_observed":true,"ttl":"5m","expires_at":null}' '{"model":{"id":"claude-sonnet-5-5"}}')
check "status line: cold 5m on Sonnet 5.5 is \$2.5/MTok (450K -> \$1.13)" "$(plain "$out")" 'cache ○ cold · \$1\.13 to rewarm$'
out=$(sl '{"warm":false,"caching_observed":true,"ttl":"1h","expires_at":null}' '{"model":{"id":"some-gateway-model"}}')
check "status line: cold on an unknown model shows no \$" "$(plain "$out")" '\| cache ○ cold$'
out=$(sl "$(warm 1h $((N - 5)))")
check "status line: warm with expires_at already past reads cold" "$(plain "$out")" 'cache ○ cold · \$3\.60 to rewarm$'
out=$(sl '{"warm":false,"caching_observed":false,"ttl":"1h","expires_at":null}')
check "status line: caching never observed shows no cache segment" "$(plain "$out")" 'cache' absent

echo "--- session-context payload size (each repo with a local invariants doc under CODE_DIRS) ---"
b=$(sed -n 's/^BUDGET=\([0-9]*\).*/\1/p' "$H/session-context" | head -1)
CODE_DIRS=$(. "$SUITE_LIB/hook-io"; kit_env; printf '%s' "$CODE_DIRS")
nrepo=0
for inv in "$HOME"/.claude/local/*-invariants.md; do
  [ -f "$inv" ] || continue
  name=${inv##*/}; name=${name%-invariants.md}
  for d in $CODE_DIRS; do
    case $d in '~/'*) d="$HOME/${d#'~/'}" ;; esac
    [ -d "$d/$name" ] || continue
    nrepo=$((nrepo + 1))
    n=$(CLAUDE_PROJECT_DIR="$d/$name" "$H/session-context" </dev/null | wc -c | tr -d ' ')
    [ "$n" -le "${b:-0}" ] && ok "session-context bytes for $name: $n (budget $b)" || bad "session-context over budget in $name: $n bytes (budget ${b:-unset})"
  done
done
[ "$nrepo" -gt 0 ] || echo "SKIP no repo with a local invariants doc under CODE_DIRS"

rm -rf "$T"
finish
