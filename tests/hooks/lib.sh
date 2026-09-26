# Shared by the hook suites (hooktest.sh, review-gates.sh): counters, assertions, payloads.
# HOOKS_DIR=<dir> runs a suite against another copy of the hooks, e.g. the pre-fix ones to prove a
# new case fails there. Hooks source their libs relative to themselves, so the copy's libs are used.
# tests/run-tests.sh sets it to one directory holding every plugin's hooks (the plugins split them).
H="${HOOKS_DIR:?run tests/run-tests.sh, which sets HOOKS_DIR}"
pass=0; fail=0

ok()  { pass=$((pass+1)); echo "PASS $1"; }
bad() { fail=$((fail+1)); echo "FAIL $1"; [ -n "${2:-}" ] && echo "     $(printf '%s' "$2" | head -c 300)"; return 0; }
# check <label> <output> <grep -E pattern> [absent]
check() {
  if [ "${4:-}" = absent ]; then
    printf '%s' "$2" | grep -Eq -- "$3" && bad "$1" "$2" || ok "$1"
  else
    printf '%s' "$2" | grep -Eq -- "$3" && ok "$1" || bad "$1" "$2"
  fi
}
empty()  { [ -z "$2" ] && ok "$1" || bad "$1" "$2"; }
finish() { echo "=== PASS $pass FAIL $fail ==="; [ "$fail" = 0 ]; }

# pre|post <command> [sid] [cwd] [JSON merged over the payload]: a Bash tool payload.
_bash_payload() {
  jq -cn --arg e "$1" --arg c "$2" --arg s "${3:-hooktest-sid}" --arg d "${4:-$HOME/.claude}" --argjson x "${5:-null}" \
    '{tool_name:"Bash",hook_event_name:$e,session_id:$s,cwd:$d,tool_input:{command:$c}} * ($x // {})'
}
pre()  { _bash_payload PreToolUse "$@"; }
post() { _bash_payload PostToolUse "$@"; }
# edit <file> <sid>: a PostToolUse(Edit) payload.
edit() { jq -cn --arg f "$1" --arg s "$2" '{hook_event_name:"PostToolUse",tool_name:"Edit",session_id:$s,cwd:"/",tool_input:{file_path:$f,old_string:"",new_string:"x"}}'; }
# stop <sid> <cwd> [stop_hook_active]: a Stop payload.
stop() { jq -cn --arg s "$1" --arg d "$2" --argjson a "${3:-false}" '{hook_event_name:"Stop",session_id:$s,cwd:$d,stop_hook_active:$a}'; }
