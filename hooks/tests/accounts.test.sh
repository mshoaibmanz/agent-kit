#!/bin/bash
# Tests for bin/claude-account, the zsh launcher's routing and claude-gc's per-account sweep, on a
# fixture HOME with a fake `claude` first on PATH: no real account dir, login or MCP config is read
# or written, and nothing logs in.
#
#   bash ~/.claude/hooks/tests/accounts.test.sh
#   ACCOUNT_BIN=<old> LAUNCHER=<old> GC_BIN=<old> bash ~/.claude/hooks/tests/accounts.test.sh   # pre-fix
set -u
. "${BASH_SOURCE[0]%/*}/lib.sh"
ACCT=${ACCOUNT_BIN:-$HOME/.claude/bin/claude-account}
LAUNCHER=${LAUNCHER:-$HOME/.claude/local/claude-launcher.zsh}
GC=${GC_BIN:-$HOME/.claude/bin/claude-gc}
FH=$(mktemp -d "$HOME/.accounts-test-XXXXXX") || exit 1
trap 'rm -rf "$FH"' EXIT
B="$FH/.claude"; W="$FH/.claude-work"; LOG="$FH/claude.log"; FAKE="$FH/fakebin"
mkdir -p "$B/bin" "$B/local" "$B/projects" "$FAKE" "$FH/tmp" "$FH/plain" "$FH/Code/other" "$FH/elsewhere"
cp "$ACCT" "$B/bin/claude-account"
ln -s "$H" "$B/hooks"
echo '# fixture' > "$B/CLAUDE.md"; echo '{}' > "$B/settings.json"

# The fake claude: one log line per call, "<CLAUDE_CONFIG_DIR, or - when unset>\t<args>", and the
# user-scope MCP edits `claude mcp` makes, on <config dir, else HOME>/.claude.json. Like the real
# CLI (probed 2026-09-26), add-json stores a stdio server given no "args" with "args": [].
cat > "$FAKE/claude" <<'EOF'
#!/bin/bash
printf '%s\t%s\n' "${CLAUDE_CONFIG_DIR--}" "$*" >> "$FAKE_LOG"
cfg=${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json
[ -f "$cfg" ] || echo '{}' > "$cfg"
has() { jq -e --arg n "$1" '(.mcpServers // {}) | has($n)' "$cfg" >/dev/null; }
case "$1 ${2:-}" in
  "mcp add-json") has "$5" && exit 1
    jq --arg n "$5" --argjson j "$6" '.mcpServers[$n] = (if ($j | has("command")) then {args: []} + $j else $j end)' \
      "$cfg" > "$cfg.t" && mv "$cfg.t" "$cfg" ;;
  "mcp remove") has "$5" || exit 1
    jq --arg n "$5" 'del(.mcpServers[$n])' "$cfg" > "$cfg.t" && mv "$cfg.t" "$cfg" ;;
  "auth status") echo '{"loggedIn":true,"email":"t@example.com"}' ;;
esac
exit 0
EOF
chmod +x "$FAKE/claude"

run_env() { env -u CLAUDE_CONFIG_DIR -u CLAUDE_ACCOUNT -u KIT_ENV HOME="$FH" PATH="$FAKE:$PATH" FAKE_LOG="$LOG" "$@"; }
ca() { run_env "$B/bin/claude-account" "$@"; }
# launch_in <dir> [env assignment]: the launcher starting `claude --version` there; its status.
launch_in() { ( cd "$1" && run_env ${2:+"$2"} zsh -fc 'source "$1"; claude --version' _ "$LAUNCHER" ) >/dev/null 2>&1; }
last_launch() { grep -- '--version' "$LOG" | tail -1; }

echo "--- help ---"
out=$(ca 2>&1)
check "no command prints the usage block" "$out" '^usage: claude-account <command>'
check "...through its last command" "$out" '^  dirs +every account'

echo "--- setup and check ---"
out=$(ca setup Work 2>&1); rc=$?
[ "$rc" != 0 ] && ! printf '%s' "$out" | grep -q linked && ok "an invalid name stops setup" || bad "an invalid name did not stop setup (rc=$rc)" "$out"
out=$(ca setup work 2>&1)
[ -f "$W/.claude-account" ] && [ -L "$W/settings.json" ] && [ -L "$W/hooks" ] && ok "setup links the shared entries" || bad "setup" "$out"
out=$(ca check work); rc=$?
[ "$rc" = 0 ] && ok "check: a fresh setup is clean" || bad "check after setup: rc=$rc" "$out"
mkdir "$B/plans"
out=$(ca check work)
check "an entry added after setup reads as not linked, fixed by setup" "$out" '^work: plans is not linked.*re-run claude-account setup work'
check "...not as rewritten" "$out" 'plans is a separate copy' absent
rm "$W/settings.json"; echo '{}' > "$W/settings.json"
out=$(ca check work)
check "a link replaced by a file reads as a copy" "$out" '^work: settings.json is a separate copy'
rm "$W/settings.json"; ca setup work >/dev/null 2>&1
out=$(ca check work); rc=$?
[ "$rc" = 0 ] && ok "setup again restores both" || bad "check after re-setup: rc=$rc" "$out"

echo "--- routing ---"
printf '%s\n' '# fixture routes' 'work ~/Code/repo' 'other ~/Code/other' > "$B/local/accounts"
REPO="$FH/Code/repo"; WT="$FH/elsewhere/repo-wt1"
git init -q "$REPO" && git -C "$REPO" -c user.name=t -c user.email=t@t commit -q --allow-empty -m init
git -C "$REPO" worktree add -q --detach "$WT" 2>/dev/null
[ "$(ca which "$REPO")" = work ] && ok "which: a dir under a routed prefix" || bad "which $REPO: $(ca which "$REPO")"
[ "$(ca which "$WT")" = work ] && ok "which: a linked worktree outside the prefix routes like its repo" || bad "which $WT: $(ca which "$WT")"
[ "$(ca which "$FH/plain")" = personal ] && ok "which: anything else is personal" || bad "which $FH/plain: $(ca which "$FH/plain")"
out=$(ca launch "$WT" 2>"$FH/err")
[ "$out" = "$W" ] && ok "launch prints the account's config dir" || bad "launch $WT printed '$out'" "$(cat "$FH/err")"
out=$(ca launch "$FH/plain" 2>/dev/null); rc=$?
[ "$rc" = 0 ] && [ -z "$out" ] && ok "launch prints nothing for personal" || bad "launch personal: rc=$rc '$out'"
out=$(run_env CLAUDE_ACCOUNT=work "$B/bin/claude-account" launch "$FH/plain" 2>/dev/null)
[ "$out" = "$W" ] && ok "CLAUDE_ACCOUNT overrides the route" || bad "CLAUDE_ACCOUNT=work launch printed '$out'"
out=$(ca launch "$FH/Code/other" 2>&1); rc=$?
[ "$rc" != 0 ] && printf '%s' "$out" | grep -q 'not set up' && ok "launch fails for an account that is not set up" || bad "launch other: rc=$rc" "$out"

echo "--- sync-mcp ---"
cat > "$B/mcp.json" <<'EOF'
{"mcpServers": {"a": {"type": "stdio", "command": "a-server", "args": [], "env": {"TOKEN": "t0"}},
                "b": {"type": "http", "url": "https://b.example/mcp"}}}
EOF
jq -n --arg r "$REPO" '{mcpServers: {stale: {type: "stdio", command: "x", args: []}},
  projects: {($r): {mcpServers: {bigquery: {type: "stdio", command: "uvx", args: []}, github: {type: "http", url: "https://g"}}}}}' > "$FH/.claude.json"
: > "$LOG"
ca sync-mcp 2>/dev/null
[ "$(jq -c '.mcpServers | keys' "$FH/.claude.json")" = '["a","b"]' ] && ok "personal's user scope matches mcp.json, a stale server removed" || bad "personal user scope: $(jq -c '.mcpServers | keys' "$FH/.claude.json")"
[ "$(jq -c '.mcpServers | keys' "$W/.claude.json" 2>/dev/null)" = '["a","b"]' ] && jq -e '.mcpServers.a.env.TOKEN == "t0"' "$W/.claude.json" >/dev/null \
  && ok "work's user scope matches mcp.json, configs included" || bad "work user scope: $(jq -c .mcpServers "$W/.claude.json" 2>/dev/null)"
grep -q $'^-\tmcp ' "$LOG" && ok "personal's CLI calls run with CLAUDE_CONFIG_DIR unset" || bad "no personal call with CLAUDE_CONFIG_DIR unset" "$(cat "$LOG")"
grep -q "^$W"$'\tmcp ' "$LOG" && ok "work's CLI calls run with its config dir" || bad "no work call with CLAUDE_CONFIG_DIR=$W" "$(cat "$LOG")"
grep -q "^$B"$'\t' "$LOG" && bad "a call ran with CLAUDE_CONFIG_DIR=~/.claude" "$(cat "$LOG")" || ok "no call runs with CLAUDE_CONFIG_DIR=~/.claude"
: > "$LOG"; ca sync-mcp 2>/dev/null
[ -s "$LOG" ] && bad "an account already in sync still ran the CLI" "$(cat "$LOG")" || ok "an account already in sync runs no CLI call"
jq '.mcpServers.b.url = "https://b2.example/mcp"' "$B/mcp.json" > "$B/mcp.json.t" && mv "$B/mcp.json.t" "$B/mcp.json"
: > "$LOG"; ca sync-mcp work 2>/dev/null
[ "$(jq -r .mcpServers.b.url "$W/.claude.json")" = https://b2.example/mcp ] && ok "a changed server is replaced" || bad "b not replaced" "$(jq -c .mcpServers "$W/.claude.json")"
grep -Eq $'\tmcp (add-json|remove) -s user a( |$)' "$LOG" && bad "an unchanged server was touched" "$(cat "$LOG")" || ok "...and no other server is touched"
# Drift on the account's side, with mcp.json unchanged: a sync keyed on mcp.json alone never saw it.
jq 'del(.mcpServers.a)' "$W/.claude.json" > "$W/j.t" && mv "$W/j.t" "$W/.claude.json"
: > "$LOG"; ca sync-mcp work 2>/dev/null
jq -e '.mcpServers.a.env.TOKEN == "t0"' "$W/.claude.json" >/dev/null && ok "a server removed from an account by hand is restored" || bad "a not restored" "$(jq -c .mcpServers "$W/.claude.json")"
grep -q $'\tmcp remove -s user a$' "$LOG" && bad "a missing server was removed before its add" "$(cat "$LOG")" || ok "...by an add alone"
jq '.mcpServers.extra = {type: "http", url: "https://x"}' "$W/.claude.json" > "$W/j.t" && mv "$W/j.t" "$W/.claude.json"
ca sync-mcp work 2>/dev/null
jq -e '.mcpServers | has("extra") | not' "$W/.claude.json" >/dev/null && ok "a server added to an account by hand is removed" || bad "extra kept" "$(jq -c '.mcpServers | keys' "$W/.claude.json")"
echo '{}' > "$W/.claude.json"
ca sync-mcp work 2>/dev/null
[ "$(jq -c '.mcpServers | keys' "$W/.claude.json")" = '["a","b"]' ] && ok "a reset .claude.json gets every server back" || bad "after a reset: $(jq -c .mcpServers "$W/.claude.json")"
jq '.mcpServers.c = {type: "stdio", command: "c-server"}' "$B/mcp.json" > "$B/mcp.json.t" && mv "$B/mcp.json.t" "$B/mcp.json"
ca sync-mcp work 2>/dev/null
: > "$LOG"; ca sync-mcp work 2>"$FH/err"
[ -s "$LOG" ] && bad "a server the CLI stores with args: [] is re-added every run" "$(cat "$LOG")" || ok "a server stored with the CLI's default args is in sync"
check "...and draws no still-differs warning" "$(cat "$FH/err")" 'still differs' absent
jq 'del(.mcpServers.c)' "$B/mcp.json" > "$B/mcp.json.t" && mv "$B/mcp.json.t" "$B/mcp.json"
ca sync-mcp work 2>/dev/null

echo "--- check: project-scope MCP servers ---"
out=$(ca check 2>&1); rc=$?
check "check names project-scope servers another account lacks" "$out" "^personal: project-scope MCP servers in $REPO that work lacks: bigquery, github\."
check "...and the fix" "$out" 'Move them to ~/.agents/mcp/servers.json and run `agent-kit render --host claude` \(every account\), or remove them'
[ "$rc" != 0 ] && ok "...and fails" || bad "check passed with a project-scope gap"
out=$(ca launch "$REPO" 2>"$FH/err"); rc=$?
check "launch reports the gap on stderr" "$(cat "$FH/err")" "^personal: project-scope MCP servers in $REPO that work lacks"
[ "$rc" = 0 ] && [ "$out" = "$W" ] && ok "...and still launches" || bad "launch with a project-scope gap: rc=$rc '$out'"

echo "--- list and dirs ---"
: > "$LOG"; ca list >/dev/null
grep -q $'^-\tauth status' "$LOG" && ok "list asks personal with CLAUDE_CONFIG_DIR unset" || bad "list, personal" "$(cat "$LOG")"
grep -q "^$B"$'\t' "$LOG" && bad "list set CLAUDE_CONFIG_DIR=~/.claude" || ok "list never sets CLAUDE_CONFIG_DIR=~/.claude"
[ "$(ca dirs | tr '\n' ' ')" = "$B $W " ] && ok "dirs: personal first, then each account" || bad "dirs: $(ca dirs | tr '\n' ' ')"

echo "--- launcher ---"
: > "$LOG"
launch_in "$WT"; line=$(last_launch)
[ "${line%%$'\t'*}" = "$W" ] && ok "a linked worktree elsewhere launches as its repo's account" || bad "launcher in $WT: '$line'"
check "the launcher passes no --mcp-config" "$line" 'mcp-config' absent
launch_in "$FH/plain"; line=$(last_launch)
[ "${line%%$'\t'*}" = - ] && ok "personal launches with CLAUDE_CONFIG_DIR unset" || bad "launcher in $FH/plain: '$line'"
n=$(grep -c -- '--version' "$LOG")
launch_in "$FH/Code/other"; rc=$?
[ "$rc" != 0 ] && [ "$(grep -c -- '--version' "$LOG")" = "$n" ] && ok "an account that is not set up stops the launch" || bad "launcher in $FH/Code/other: rc=$rc"
( cd "$FH/plain" && run_env zsh -fc 'source "$1"; cw --version' _ "$LAUNCHER" ) >/dev/null 2>&1; line=$(last_launch)
[ "${line%%$'\t'*}" = "$W" ] && ok "cw launches the work account outside a routed dir" || bad "cw in $FH/plain: '$line'"

echo "--- claude-gc: every account's session-env ---"
mkdir -p "$B/session-env/e1" "$W/session-env/e2" "$W/session-env/keep"; touch "$W/session-env/keep/f"
run_env TMPDIR="$FH/tmp" "$GC" >/dev/null 2>&1
[ ! -d "$B/session-env/e1" ] && [ ! -d "$W/session-env/e2" ] && [ -d "$W/session-env/keep" ] \
  && ok "empty session-env stubs are pruned in every account" || bad "session-env: $(cd "$FH" && find .claude/session-env .claude-work/session-env -type d | tr '\n' ' ')"

echo "--- launch with three accounts ---"
# check_project_mcp walks every account through acct_dir, which resets DIR: with a third account,
# launch printed the last one's dir (work sorts after other), and the launcher started that account.
ca setup other >/dev/null 2>&1
out=$(ca launch "$FH/Code/other" 2>/dev/null)
[ "$out" = "$FH/.claude-other" ] && ok "launch prints the routed account's dir, not the last account's" || bad "launch other printed '$out'"
: > "$LOG"
launch_in "$FH/Code/other"; line=$(last_launch)
[ "${line%%$'\t'*}" = "$FH/.claude-other" ] && ok "...and the launcher starts that account" || bad "launcher in $FH/Code/other: '$line'"

finish
