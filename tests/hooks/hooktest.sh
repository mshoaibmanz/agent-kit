#!/bin/bash
# Payload tests for the ~/.claude hooks. Prints PASS/FAIL per case and exits non-zero on any FAIL.
# Every case asserts the exit code as well as the decision: a hook that dies under `set -e`
# prints nothing, which reads as "allow" (test-exec-gate aborted on every path-less command
# for 13 days before this check existed).
# Run: bash tests/run-tests.sh   (it sets HOOKS_DIR; see lib.sh)
# The hooks read the per-user overlay (hooks/lib/hook-io kit_env); KIT_ENV=/dev/null runs the
# suite with none, and the overlay-specific cases then use a fixture overlay or skip.
. "${BASH_SOURCE[0]%/*}/lib.sh"
START_KIT=${KIT_ENV:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/local/kit.env}
# The suite's own loader reads an overlay's values, so a HOOKS_DIR copy without one still gets cases.
SUITE_LIB="${BASH_SOURCE[0]%/*}/../../lib/hooks"
T=$HOME/.claude/tmp/hooktest   # not /tmp or a scratchpad: context-guard skips those paths
rm -rf "$T"; mkdir -p "$T"
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
run bash-guards allow "$(pre "sed -n '1,80p' plugins/prod-data/skills/debug/references/$DB.md")"
run bash-guards allow "$(pre "grep -n tunnel plugins/prod-data/skills/debug/references/$DB.md | head")"
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
CG=$HOME/.claude/tmp/claude-context/hooktest-sid; rm -rf "$CG"
A=$T/hooktest-a.md; B=$T/hooktest-b.md; touch "$A" "$B"
printf '%s' "$(post "cat $A; wc -l $B" hooktest-sid "$T")" | "$H/context-guard" >/dev/null
run context-guard deny  "$(pre "cat $A" hooktest-sid "$T")"
run context-guard allow "$(pre "cat $B" hooktest-sid "$T")"
printf '%s' "$(post "cat > $B <<EOF
x
EOF" hooktest-sid "$T")" | "$H/context-guard" >/dev/null
run context-guard allow "$(pre "cat $B" hooktest-sid "$T")"
run context-guard allow "$(pre "sed -n '1,20p' $A" hooktest-sid "$T")"
run context-guard allow "$(pre "head -1 $A" hooktest-sid "$T")"
run context-guard allow "$(pre "cat $A # re-dump" hooktest-sid "$T")"
rm -rf "$CG"

M=$HOME/.claude/tmp/claude-tests
BG='{"tool_input":{"run_in_background":true}}'
# A fixture repo holding the overlay's gate file; hooks read the overlay named by $KIT_ENV.
REPO=$T/gate-repo; git init -q "$REPO"
gate() { run test-exec-gate "$1" "$(pre "$2" hooktest-sid "$REPO")"; }
ran()  { printf '%s' "$(post "$1" hooktest-sid "$REPO")" | "$H/tests-ran-mark" || bad "tests-ran-mark rc=$? on: $1"; }
# overlay <file>: the overlay the hooks read; its TEST_* keys land in this shell for the cases.
overlay() {
  export KIT_ENV=$1
  IFS=$'\t' read -r RUN GATEFILE REFUSED < <(
    unset _KIT_ENV_READ TEST_RUNNER TEST_GATE_FILE TEST_REFUSED_CMDS
    . "$SUITE_LIB/hook-io"; kit_env
    printf '%s\t%s\t%s\n' "${TEST_RUNNER:--}" "${TEST_GATE_FILE:--}" "${TEST_REFUSED_CMDS%%|*}")
}

# gate_cases: the gate and the markers, for the overlay loaded by `overlay`.
gate_cases() {
  touch "$REPO/$GATEFILE"
  mkdir -p "$M"; rm -f "$M"/lastrun-hooktest-sid* "$M/edit-hooktest-sid" "$M/hooktest-sid"
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
  run test-exec-gate deny "$(pre "$RUN -k foo" hooktest-sid "$REPO" "$BG")"
  run test-exec-gate deny "$(pre "$RUN tests/f.py" hooktest-sid "$REPO" "$BG")"
  run test-exec-gate allow "$(pre "$RUN tests/d.py > $T/t.log 2>&1" hooktest-sid "$REPO" "$BG")"
  run test-exec-gate allow "$(pre "$RUN tests/e.py > \"\$LOG\" 2>&1" hooktest-sid "$REPO" "$BG")"
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
  sleep 1; touch "$M/edit-hooktest-sid"
  gate allow "$RUN tests/a.py -k x"; ran "$RUN tests/a.py -k x"
  gate deny  "$RUN tests/a.py -k x"
  gate allow "$RUN tests/b.py -k z"
  # A subagent shares the parent's session_id; its run locks only itself (keyed by agent_id).
  printf '%s' "$(post "$RUN tests/g.py" hooktest-sid "$REPO" '{"agent_id":"sub1"}')" | "$H/tests-ran-mark"
  gate allow "$RUN tests/g.py"
  run test-exec-gate deny "$(pre "$RUN tests/g.py" hooktest-sid "$REPO" '{"agent_id":"sub1"}')"
  # A runner merely named does not mark a run.
  rm -f "$M/hooktest-sid"; ran "cat ~/bin/$RUN"
  [ ! -f "$M/hooktest-sid" ] && ok "tests-ran-mark ignores a named runner" || bad "tests-ran-mark marked 'cat ~/bin/$RUN' as a run"
  rm -f "$M"/lastrun-hooktest-sid* "$M/edit-hooktest-sid" "$M/hooktest-sid"
}

echo "--- test-exec-gate + tests-ran-mark: fixture overlay ---"
printf '%s\n' TEST_GATE_FILE=.hooktest-gate TEST_RUNNER=trun "TEST_RUNNER_RE='trun(-ci)?'" \
  "TEST_REFUSED_CMDS='make test|dev t'" "TEST_NOTE='Each run costs 5s.'" > "$T/kit.env"
overlay "$T/kit.env"
gate_cases
gate deny "dev t -k foo"
gate allow "trun-ci tests/z.py"
out=$(printf '%s' "$(pre "pytest -k foo" hooktest-sid "$REPO")" | "$H/test-exec-gate")
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
run test-exec-gate silent "$(pre "trun tests/a.py" hooktest-sid "$REPO" "$BG")"
rm -f "$M/hooktest-sid"; ran "trun tests/a.py"
[ ! -f "$M/hooktest-sid" ] && ok "no overlay: a would-be runner is not a test run" || bad "no overlay: tests-ran-mark marked trun"
ran "pytest tests/a.py"
[ -f "$M/hooktest-sid" ] && ok "no overlay: pytest still marks a run" || bad "no overlay: pytest did not mark a run"
rm -f "$M"/lastrun-hooktest-sid* "$M/edit-hooktest-sid" "$M/hooktest-sid"
out=$(CLAUDE_PROJECT_DIR=$REPO "$H/session-context" </dev/null)
check "no overlay: session-context has no TESTS line" "$out" '^TESTS:' absent
overlay "$T/kit.env"
out=$(CLAUDE_PROJECT_DIR=$REPO "$H/session-context" </dev/null)
check "fixture overlay: session-context TESTS line names the runner" "$out" '^TESTS: `trun a\.py.*Each run costs 5s\. `make test`, `dev t` and bare `pytest` are blocked'
export KIT_ENV=$START_KIT

echo "--- tests-ran-mark: python heredoc edit marks ---"
rm -f "$M/edit-hooktest-sid"
printf '%s' "$(post "python3 - <<PY
import pathlib
pathlib.Path(\"src/x.py\").write_text(\"a\")
PY" hooktest-sid /tmp)" | "$H/tests-ran-mark" >/dev/null
[ -f "$M/edit-hooktest-sid" ] && ok "tests-ran-mark python heredoc marks edit" || bad "tests-ran-mark python heredoc did not mark"
rm -f "$M/edit-hooktest-sid"

echo "--- unfinished-work + ci-watch statuses ---"
R=$T/uw-repo; git init -q -b hooktest-uw "$R" && git -C "$R" -c user.name=t -c user.email=t@t commit -q --allow-empty -m init
# The branch -> PR pointer's on-disk contract with ci-watch (lib/state).
PTR=$HOME/.claude/tmp/ci-branch-hooktest-uw.pr; SF=$HOME/.claude/tmp/ci-watch-999999.status
printf '999999\n' > "$PTR"
uw() { printf '%s\n' "$3" > "$SF"; run unfinished-work "$2" "$(stop "$1" "$R")"; }
uw uw1 deny  "waiting for 'tests' run on abcdef12 (20s elapsed)"
uw uw1 allow "waiting for 'tests' run on abcdef12 (40s elapsed)"
uw uw1 allow "running 123 on abcdef12"
uw uw1 deny  "RED — 3 cycles exhausted, needs a human"
reason=$(printf 'running 1\n' > "$SF"; stop uw-rx "$R" | "$H/unfinished-work" | jq -r .reason)
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
run unfinished-work allow "$(stop uw-w "$R")"
kill "$wpid" 2>/dev/null; wait "$wpid" 2>/dev/null
run unfinished-work deny "$(stop uw-w "$R")"
[ -f "${TMPDIR:-/tmp}/claude-stop-block/uw-w" ] && ok "a block marks the session for stop-chime" || bad "unfinished-work blocked without the stop-block marker"
rm -f "$PTR" "$SF" "${TMPDIR:-/tmp}"/claude-unfinished/uw* "${TMPDIR:-/tmp}"/claude-stop-block/uw*

echo "--- CI_WATCH_ACTIVE: stop-chime, session-digest ---"
SB="${TMPDIR:-/tmp}/claude-stop-block"; mkdir -p "$SB"; touch "$SB/hooktest-chime"
printf '{"session_id":"hooktest-chime"}' | CI_WATCH_ACTIVE=1 "$H/stop-chime"; rc=$?
sleep 3
[ "$rc" = 0 ] && [ -f "$SB/hooktest-chime" ] && ok "stop-chime skips under CI_WATCH_ACTIVE" || bad "stop-chime ran under CI_WATCH_ACTIVE (rc=$rc)"
# Control: without the env the (fresh) marker is consumed silently, no chime.
printf '{"session_id":"hooktest-chime"}' | "$H/stop-chime"
sleep 3
[ ! -f "$SB/hooktest-chime" ] && ok "stop-chime consumes the block marker without the env" || bad "stop-chime control: marker not consumed"
rm -f "$SB/hooktest-chime"
TR=$T/hooktestdigest.jsonl; DG=$HOME/.claude/local/session-digests/2000-01-01-hooktest.md
printf '%s\n' '{"type":"user","timestamp":"2000-01-01T00:00:00Z","cwd":"/x","message":{"content":"hello"}}' > "$TR"
rm -f "$DG"
jq -cn --arg p "$TR" '{transcript_path:$p}' | CI_WATCH_ACTIVE=1 "$H/session-digest"; rc=$?
[ "$rc" = 0 ] && [ ! -f "$DG" ] && ok "session-digest skips under CI_WATCH_ACTIVE" || bad "session-digest wrote under CI_WATCH_ACTIVE (rc=$rc)"
jq -cn --arg p "$TR" '{transcript_path:$p}' | "$H/session-digest"
[ -f "$DG" ] && ok "session-digest writes without the env" || bad "session-digest control: no digest"
rm -f "$DG"

echo "--- tab-signal ---"
out=$(printf '%s' '{"session_id":"hooktest-tab","prompt":"hello","cwd":"/tmp"}' | CI_WATCH_ACTIVE=1 "$H/tab-signal" busy)
[ -z "$out" ] && ok "tab-signal busy: no naming request under CI_WATCH_ACTIVE" || bad "tab-signal asked a headless ci-watch agent to name its tab"
out=$(printf '%s' '{"session_id":"hooktest-tab","prompt":"hello","cwd":"/tmp"}' | "$H/tab-signal" busy)
printf '%s' "$out" | grep -q 'tab-signal name' && ok "tab-signal busy: asks once in a normal session" || bad "tab-signal busy: no naming request"
rm -f "$HOME/.claude/state/tab-label/hooktest-tab"*

# comment-guard, review-agent-mark, the review gates and the missing-lib policy are covered by
# hooks/tests/review-gates.sh.

echo "--- session-context payload size (each repo with a local invariants doc under CODE_DIRS) ---"
b=$(sed -n 's/^BUDGET=\([0-9]*\).*/\1/p' "$H/session-context" | head -1)
CODE_DIRS=$(unset _KIT_ENV_READ CODE_DIRS; . "$SUITE_LIB/hook-io"; kit_env; printf '%s' "${CODE_DIRS:-}")
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
