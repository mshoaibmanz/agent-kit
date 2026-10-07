#!/bin/bash
# Tests for hooks/read-streak: one researcher nudge per streak of 8 code-discovery calls in the main
# session, and a PreToolUse deny of discovery reads 12 calls later (`# solo` overrides).
#
#   bash ~/.claude/hooks/tests/read-streak.test.sh
#   HOOKS_DIR=<dir> bash ~/.claude/hooks/tests/read-streak.test.sh   # another copy of the hooks
set -u
. "${BASH_SOURCE[0]%/*}/lib.sh"
RS=$(mktemp -d "$HOME/.read-streak-test-XXXXXX") || exit 1
trap 'rm -rf "$RS"' EXIT
export CLAUDE_STATE_DIR=$RS/state

# rs <json>: run the hook; prints its output then "<rc N>".
rs() { printf '%s' "$1" | "$H/read-streak"; echo "<rc $?>"; }
# tool <session> <tool> [extra JSON merged in]: a PostToolUse payload.
tool() { jq -cn --arg s "$1" --arg t "$2" --argjson x "${3:-null}" \
  '{hook_event_name:"PostToolUse",session_id:$s,tool_name:$t,cwd:"/",tool_input:{file_path:"/x"},tool_response:{content:"\"tool_name\":\"Edit\""}} * ($x // {})'; }
# bashp <session> <command>
bashp() { jq -cn --arg s "$1" --arg c "$2" \
  '{hook_event_name:"PostToolUse",session_id:$s,tool_name:"Bash",cwd:"/",tool_input:{command:$c},tool_response:{stdout:""}}'; }
# repeat <n> <session> <tool>: n calls; prints the last output only.
nth() { local i out=; for ((i = 0; i < $1; i++)); do out=$(rs "$(tool "$2" "$3")"); done; printf '%s' "$out"; }
fires() { printf '%s' "$1" | grep -q 'additionalContext'; }

echo "--- read-streak: fires once at the 8th read ---"
out=$(nth 7 a Read)
fires "$out" && bad "7 reads are silent" "$out" || ok "7 reads are silent"
out=$(rs "$(tool a Grep)")
one=$(printf '%s' "$out" | tr -d '\n')
check "the 8th read injects additionalContext on PostToolUse" "$one" '"hookEventName": "PostToolUse".*"additionalContext": "8 code-reading calls'
check "the nudge names researcher and warns of the deny" "$one" 'researcher.*After 12 more, reads here are refused'
check "the nudge exits 0" "$out" '<rc 0>$'
out=$(rs "$(tool a Glob)")
fires "$out" && bad "the 9th read is silent" "$out" || ok "the 9th read is silent (once per streak)"
out=$(nth 20 a LSP)
fires "$out" && bad "later reads stay silent" "$out" || ok "later reads of the same streak stay silent"

echo "--- read-streak: an edit resets the streak ---"
nth 8 b Read >/dev/null
rs "$(tool b Edit)" >/dev/null
out=$(nth 7 b Read)
fires "$out" && bad "7 reads after an edit are silent" "$out" || ok "7 reads after an edit are silent"
out=$(rs "$(tool b Read)")
fires "$out" && ok "the 8th read after an edit nudges again" || bad "the 8th read after an edit nudges again" "$out"
for t in Write MultiEdit NotebookEdit Agent Task SendMessage; do
  nth 7 "r$t" Read >/dev/null
  rs "$(tool "r$t" "$t")" >/dev/null
  out=$(rs "$(tool "r$t" Read)")
  fires "$out" && bad "$t resets the streak" "$out" || ok "$t resets the streak"
done

echo "--- read-streak: a new prompt resets ---"
nth 7 c Read >/dev/null
rs "$(jq -cn '{hook_event_name:"UserPromptSubmit",session_id:"c",prompt:"hi"}')" >/dev/null
out=$(rs "$(tool c Read)")
fires "$out" && bad "a prompt resets the streak" "$out" || ok "a prompt resets the streak"

echo "--- read-streak: main session only ---"
sub=$(jq -cn '{agent_id:"agent-1",agent_type:"researcher"}')
for ((i = 0; i < 12; i++)); do out=$(rs "$(tool d Read "$sub")"); fires "$out" && break; done
fires "$out" && bad "a subagent's reads never nudge" "$out" || ok "a subagent's reads never nudge"
ls "$CLAUDE_STATE_DIR/read-streak/" 2>/dev/null | grep -qE '^d(\.|$)' && bad "a subagent leaves no streak state" || ok "a subagent leaves no streak state"
nth 7 e Read >/dev/null
rs "$(tool e Read "$sub")" >/dev/null
out=$(rs "$(tool e Read)")
fires "$out" && ok "a subagent call does not count against the main streak" || bad "a subagent call does not count against the main streak" "$out"
out=$(printf '%s' "$(nth 7 f Read >/dev/null; tool f Read)" | CI_WATCH_ACTIVE=1 "$H/read-streak"; echo "<rc $?>")
check "CI_WATCH_ACTIVE is silent" "$out" '^<rc 0>$'

echo "--- read-streak: Bash classification ---"
# A discovery read counts, a file write resets, anything else is neutral. Six Reads, the command, one
# Read: a nudge means it counted. Seven Reads, the command, one Read: a nudge means it did not reset.
bashcase() {
  local want=$1 cmd=$2 sid="bc-$RANDOM" out counted kept
  nth 6 "$sid" Read >/dev/null
  rs "$(bashp "$sid" "$cmd")" >/dev/null
  out=$(rs "$(tool "$sid" Read)")
  fires "$out" && counted=1 || counted=0
  sid="bk-$RANDOM"
  nth 7 "$sid" Read >/dev/null
  rs "$(bashp "$sid" "$cmd")" >/dev/null
  out=$(rs "$(tool "$sid" Read)")
  fires "$out" && kept=1 || kept=0
  case $want$counted$kept in
    read1?|neutral01|reset00) ok "$want: $cmd" ;;
    *) bad "should be $want (counted=$counted kept=$kept): $cmd" ;;
  esac
}
for c in 'ls -la src' 'grep -rn foo src | head -20' "sed -n '1,40p' README.md" 'cat a.md b.md 2>/dev/null' \
  'git log --oneline -5' 'git -C "$WT" diff origin/master...HEAD' 'git status --short' \
  'find src -name "*.py" | wc -l' 'jq . x.json | head' \
  'rg -n "x > y" src' 'ls src && git show HEAD:README.md' 'for f in a b; do cat $f; done' \
  'grep -rn foo src 2>&1 | head' 'cd ~/Code/x; git fetch -q origin 2>&1 | tail -2; git log -5' \
  'gh pr view 12 --json body' 'gh -R o/r pr diff 12' 'gh api repos/o/r/pulls/1/comments' 'gh run list' \
  'cd /x; python3 a.py; sed -n 1,40p src/x.py' 'L=$(tail -n +2 a.txt); echo $L'; do
  bashcase read "$c"
done
for c in 'echo hi > out.txt' 'cat a >> b' 'sed -i s/a/b/ f' 'git commit -m x' 'git add f' 'git -C "$WT" push' 'git checkout -b x' \
  'rm -f x' 'mkdir -p d' 'ls src; touch f' 'cat a | tee b' 'find . -name x -delete' 'git log && git reset --hard' \
  'tr a b < in > out' 'python3 a.py > out.tsv' 'gh pr create --title x' 'gh api -X POST repos/o/r/issues/1/comments' \
  "$(printf 'cat > f.py <<%s\nx = 1\nEOF' "'EOF'")"; do
  bashcase reset "$c"
done
# Prod-data reads belong in the main session (debug skill); tests, scripts and waits are not discovery.
for c in 'ro-mysql --tunnels 2>&1 | head -30' 'bqro "select 1"' 'pytest tests/a.py' 'npm install' 'python3 script.py' \
  'python3 a.py | head' 'until grep -q done log; do sleep 10; done' 'TEST_TIMEOUT=900 run-tests tests/a.py' \
  "$(printf 'ro-mysql -D app -e"\nSELECT a FROM b WHERE x <> 1\n" > out.tsv')" \
  "$(printf 'python3 - <<%s\nif a > b: print(1)\nEOF' "'EOF'")"; do
  bashcase neutral "$c"
done

echo "--- read-streak: deny after 12 more reads ---"
pre_tool() { jq -cn --arg s "$1" --arg t "$2" --argjson x "${3:-null}" \
  '{hook_event_name:"PreToolUse",session_id:$s,tool_name:$t,cwd:"/",tool_input:{file_path:"/x",pattern:"x"}} * ($x // {})'; }
pre_bash() { jq -cn --arg s "$1" --arg c "$2" '{hook_event_name:"PreToolUse",session_id:$s,tool_name:"Bash",cwd:"/",tool_input:{command:$c}}'; }
denies() { printf '%s' "$1" | grep -q '"permissionDecision": "deny"'; }
nth 19 dn Read >/dev/null
out=$(rs "$(pre_tool dn Read)")
check "19 reads: the next Read passes silently" "$out" '^<rc 0>$'
rs "$(tool dn Read)" >/dev/null
out=$(rs "$(pre_tool dn Read)")
denies "$out" && ok "20 reads: the 21st Read is denied" || bad "20 reads: the 21st Read is denied" "$out"
check "the deny exits 0" "$out" '<rc 0>$'
reason=$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecisionReason' 2>/dev/null)
[ "$(printf '%s\n' "$reason" | wc -l | tr -d ' ')" = 2 ] && ok "the deny reason is two lines" || bad "the deny reason is two lines" "$reason"
check "the deny reason leads with the # solo override" "$(printf '%s' "$reason" | head -1)" '^`# solo`'
check "the deny reason names researcher" "$reason" 'subagent_type "researcher"'
for t in Grep Glob LSP; do
  out=$(rs "$(pre_tool dn "$t")"); denies "$out" && ok "$t is denied too" || bad "$t is denied too" "$out"
done
out=$(rs "$(pre_bash dn 'sed -n 1,40p src/a.py')"); denies "$out" && ok "a Bash discovery read is denied" || bad "a Bash discovery read is denied" "$out"
for c in 'ro-mysql -D app -e "select 1"' 'pytest tests/a.py' 'git commit -m x'; do
  out=$(rs "$(pre_bash dn "$c")"); check "not denied: $c" "$out" '^<rc 0>$'
done
for t in Edit Write Agent WebFetch; do
  out=$(rs "$(pre_tool dn "$t")"); check "$t is never denied" "$out" '^<rc 0>$'
done
out=$(rs "$(pre_tool dn Read "$sub")")
check "a subagent sharing the session id is never denied" "$out" '^<rc 0>$'
out=$(rs "$(pre_bash dn 'true # solo')")
check "# solo on a Bash command passes" "$out" '^<rc 0>$'
out=$(rs "$(pre_tool dn Read)")
check "after # solo, Read passes" "$out" '^<rc 0>$'
out=$(rs "$(pre_bash dn 'grep -n x a.py')")
check "after # solo, a Bash read passes" "$out" '^<rc 0>$'
rs "$(tool dn Edit)" >/dev/null
nth 20 dn Read >/dev/null
out=$(rs "$(pre_tool dn Read)")
denies "$out" && ok "an edit ends the # solo pass; 20 new reads deny again" || bad "an edit ends the # solo pass" "$out"
rs "$(jq -cn '{hook_event_name:"UserPromptSubmit",session_id:"dn",prompt:"go"}')" >/dev/null
out=$(rs "$(pre_tool dn Read)")
check "a new prompt lifts the deny" "$out" '^<rc 0>$'
nth 20 dq Read >/dev/null
out=$(rs "$(pre_bash dq 'git log -m "# solo"')")
denies "$out" && ok "a quoted '# solo' is not the override" || bad "a quoted '# solo' is not the override" "$out"
out=$(printf '%s' "$(pre_tool dq Read)" | CI_WATCH_ACTIVE=1 "$H/read-streak"; echo "<rc $?>")
check "CI_WATCH_ACTIVE never denies" "$out" '^<rc 0>$'

echo "--- read-streak: a normal edit flow never nudges or denies ---"
hit=0
for ((k = 0; k < 15; k++)); do
  for ((j = 0; j < 3; j++)); do
    out=$(rs "$(pre_tool fl Read)"); denies "$out" && hit=1
    out=$(rs "$(tool fl Read)"); fires "$out" && hit=1
  done
  rs "$(bashp fl 'pytest tests/a.py')" >/dev/null
  rs "$(tool fl Edit)" >/dev/null
done
[ "$hit" = 0 ] && ok "15 rounds of read 3 files, run tests, edit: no nudge, no deny" || bad "a read-3-then-edit flow hit the hook"

echo "--- read-streak: parallel tool calls (concurrent hooks for one session) ---"
# burst <session> <n>: n Read hooks started, then released together once every one is up; prints
# how many of them nudged.
burst() {
  local i go="$RS/go-$1" pl
  pl=$(tool "$1" Read)
  for ((i = 0; i < $2; i++)); do
    { until [ -e "$go" ]; do :; done; printf '%s' "$pl"; } | "$H/read-streak" >"$RS/burst-$1.$i" &
  done
  sleep 0.3
  : >"$go"
  wait
  cat "$RS"/burst-"$1".* | grep -c additionalContext
}
for k in 1 2 3; do
  n=$(burst "par$k" 8)
  [ "$n" = 1 ] && ok "8 parallel reads: exactly one nudge (run $k)" || bad "8 parallel reads: exactly one nudge (run $k)" "nudges=$n"
done
for k in 1 2 3; do
  nth 7 "dup$k" Read >/dev/null
  n=$(burst "dup$k" 4)
  [ "$n" = 1 ] && ok "7 reads then 4 in parallel: exactly one nudge (run $k)" || bad "7 reads then 4 in parallel: exactly one nudge (run $k)" "nudges=$n"
done

echo "--- read-streak: neutral tools and odd payloads ---"
nth 7 g Read >/dev/null
for t in WebFetch TodoWrite ToolSearch; do rs "$(tool g "$t")" >/dev/null; done
out=$(rs "$(tool g Read)")
fires "$out" && ok "tools it does not know neither count nor reset" || bad "tools it does not know neither count nor reset" "$out"
for pl in '{}' 'not json' '' '{"hook_event_name":"PostToolUse"}' '{"hook_event_name":"PostToolUse","session_id":"../x","tool_name":"Read"}' '{"hook_event_name":"Stop","session_id":"h"}'; do
  out=$(rs "$pl")
  check "odd payload ${pl:0:30} is silent, rc 0" "$out" '^<rc 0>$'
done
[ ! -e "$CLAUDE_STATE_DIR/read-streak/../x" ] && ok "a session id with a slash writes nothing" || bad "a session id with a slash writes nothing"

echo "--- read-streak: overhead (it runs on every read, edit and Bash call) ---"
# Bounds are several times the measured cost (about 15 ms per PreToolUse read, 50 ms per Bash
# PostToolUse) so a loaded machine does not fail them; a jq or awk added to the hot path will.
now_ms() { python3 -c 'import time; print(int(time.time() * 1000))'; }
pre() { jq -cn --arg s "$1" '{hook_event_name:"PreToolUse",session_id:$s,tool_name:"Read",cwd:"/",tool_input:{file_path:"/x"}}'; }
p=$(pre ovh); t0=$(now_ms)
for ((i = 0; i < 20; i++)); do printf '%s' "$p" | "$H/read-streak" >/dev/null; done
avg=$((($(now_ms) - t0) / 20))
[ "$avg" -lt 100 ] && ok "PreToolUse Read below the deny threshold: ${avg} ms per call (< 100)" || bad "PreToolUse Read costs ${avg} ms per call (>= 100)"
p=$(bashp ovh2 'grep -rn foo src 2>&1 | head'); t0=$(now_ms)
for ((i = 0; i < 10; i++)); do printf '%s' "$p" | "$H/read-streak" >/dev/null; done
avg=$((($(now_ms) - t0) / 10))
[ "$avg" -lt 300 ] && ok "PostToolUse Bash classification: ${avg} ms per call (< 300)" || bad "PostToolUse Bash costs ${avg} ms per call (>= 300)"

finish
