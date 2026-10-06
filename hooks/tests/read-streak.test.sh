#!/bin/bash
# Tests for hooks/read-streak: one researcher nudge per streak of 8 read-only tool calls in the main session.
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
check "the 8th read injects additionalContext on PostToolUse" "$one" '"hookEventName": "PostToolUse".*"additionalContext": "8 read-only calls'
check "the nudge names researcher and the 300-word contract" "$one" 'researcher.*at most 300 words'
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
for t in Write MultiEdit NotebookEdit Agent; do
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
# A read-only Bash command counts; one that may write resets. Six Reads, then the command, then one Read:
# nudge on the closing Read means the command counted as a read.
bashcase() {
  local want=$1 cmd=$2 sid="bc-$RANDOM" out
  nth 6 "$sid" Read >/dev/null
  rs "$(bashp "$sid" "$cmd")" >/dev/null
  out=$(rs "$(tool "$sid" Read)")
  if [ "$want" = read ]; then
    fires "$out" && ok "read: $cmd" || bad "should count as a read: $cmd" "$out"
  else
    fires "$out" && bad "should reset the streak: $cmd" "$out" || ok "reset: $cmd"
  fi
}
for c in 'ls -la src' 'grep -rn foo src | head -20' "sed -n '1,40p' README.md" 'cat a.md b.md 2>/dev/null' \
  'git log --oneline -5' 'git -C "$WT" diff origin/master...HEAD' 'git status --short' \
  'find src -name "*.py" | wc -l' 'jq . x.json | head' \
  'rg -n "x > y" src' 'ls src && git show HEAD:README.md'; do
  bashcase read "$c"
done
for c in 'echo hi > out.txt' 'cat a >> b' 'sed -i s/a/b/ f' 'git commit -m x' 'git add f' 'git -C "$WT" push' 'git checkout -b x' \
  'pytest tests/a.py' 'rm -f x' 'mkdir -p d' 'python3 script.py' 'npm install' 'ls src; touch f' 'cat a | tee b' \
  'for f in a b; do cat $f; done' 'find . -name x -delete''git log && git reset --hard' 'tr a b < in > out'; do
  bashcase reset "$c"
done

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

finish
