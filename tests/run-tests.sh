#!/bin/bash
# Every test for the kit: manifests and copies, then the hook suites against the plugin copies, then
# the cases that only exist because the hooks ship as separate plugins. CI and scripts/pre-push run it.
#   bash tests/run-tests.sh
# Needs bash, git, jq, python3 (macOS or Linux). The suites read the per-user overlay the hooks
# read: $KIT_ENV, else ${CLAUDE_CONFIG_DIR:-~/.claude}/local/kit.env; KIT_ENV=/dev/null runs with none.
set -uo pipefail
KIT=$(cd "$(dirname "$0")/.." && pwd)
# Under $HOME, not /tmp: review-mark-changes and context-guard skip /tmp paths by design, so a
# fixture there tests nothing.
SCRATCH=$(mktemp -d "$HOME/.claude-kit-test.XXXXXX") || exit 1
trap 'rm -rf "$SCRATCH"' EXIT
pass=0; fail=0
ok()  { pass=$((pass + 1)); echo "PASS $1"; }
bad() { fail=$((fail + 1)); echo "FAIL $1"; [ -z "${2:-}" ] || echo "     $(printf '%s' "$2" | head -c 300)"; }
check() { if printf '%s' "$2" | grep -Eq -- "$3"; then ok "$1"; else bad "$1" "$2"; fi; }
absent() { if printf '%s' "$2" | grep -Eq -- "$3"; then bad "$1" "$2"; else ok "$1"; fi; }
suite() { # suite <name> <command...>: one line per suite; its log is printed only when it fails.
  local name=$1 log="$SCRATCH/$1.log"; shift
  local n
  if "$@" >"$log" 2>&1; then
    n=$(grep -Eo '^[0-9]+/[0-9]+ passed' "$log" | tail -1); ok "suite $name (${n:-$(grep -c '^PASS' "$log") passed})"
  else
    bad "suite $name"; grep -E '^(FAIL|     )' "$log" | head -40; tail -3 "$log"
  fi
}

echo "=== copies and manifests ==="
out=$("$KIT/scripts/build.sh" --check 2>&1) && ok "every hook-lib and shared-hook copy matches its source" || bad "copies drifted (run scripts/build.sh)" "$out"
while IFS= read -r j; do
  jq empty "$j" 2>/dev/null || bad "invalid json: ${j#"$KIT"/}"
done < <(find "$KIT" -name '*.json' -not -path '*/.git/*')
ok "json parses"
listed=$(jq -r '.plugins[].name' "$KIT/.claude-plugin/marketplace.json" | sort)
dirs=$(cd "$KIT/plugins" && ls -d */ | tr -d / | sort)
[ "$listed" = "$dirs" ] && ok "marketplace lists exactly the plugins/ dirs" || bad "marketplace vs plugins/" "$listed / $dirs"
for p in $dirs; do
  m="$KIT/plugins/$p/.claude-plugin/plugin.json"
  [ "$(jq -r .name "$m" 2>/dev/null)" = "$p" ] || bad "plugins/$p: plugin.json name"
  [ "$(jq -r --arg p "$p" '.plugins[] | select(.name == $p) | .source' "$KIT/.claude-plugin/marketplace.json")" = "./plugins/$p" ] \
    || bad "plugins/$p: marketplace source"
  hj="$KIT/plugins/$p/hooks/hooks.json"
  [ -f "$hj" ] || continue
  while IFS= read -r c; do
    f=${c#'"${CLAUDE_PLUGIN_ROOT}/'}; f=${f%%'"'*}
    [ -x "$KIT/plugins/$p/$f" ] || bad "plugins/$p: hooks.json runs $f, which is missing or not executable"
  done < <(jq -r '.hooks[][].hooks[].command' "$hj")
done
ok "plugin names, sources and hook commands resolve"
for f in "$KIT"/plugins/*/hooks/* "$KIT"/plugins/*/bin/*; do
  [ -f "$f" ] || continue
  case $f in *.json) continue ;; esac
  [ -x "$f" ] || bad "not executable: ${f#"$KIT"/}"
done
ok "hook and bin scripts are executable"
for s in "$KIT"/plugins/*/skills/*/SKILL.md; do
  d=${s%/SKILL.md}; n=$(sed -n 's/^name: *//p' "$s" | head -1)
  [ "$n" = "${d##*/}" ] || bad "skill ${d#"$KIT"/}: name '$n' does not match its dir"
done
for a in "$KIT"/plugins/*/agents/*.md; do
  n=$(sed -n 's/^name: *//p' "$a" | head -1); b=${a##*/}
  [ "$n" = "${b%.md}" ] || bad "agent ${a#"$KIT"/}: name '$n' does not match its file"
done
ok "skill and agent names match their paths"
while IFS= read -r py; do
  python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$py" 2>/dev/null || bad "python syntax: ${py#"$KIT"/}"
done < <(find "$KIT/plugins" -name '*.py' -not -path '*/__pycache__/*'; echo "$KIT/plugins/prod-data/bin/ro-mysql"; echo "$KIT/plugins/prod-data/bin/bqro")
ok "python sources parse"

# One directory holding every plugin's hooks, as ~/.claude/hooks holds them, so the suites run
# unchanged against the plugin copies. The drift check above makes the duplicate lib copies equal.
V="$SCRATCH/view"; mkdir -p "$V/hooks" "$V/bin"
for d in "$KIT"/plugins/*/hooks; do
  for f in "$d"/*; do
    [ "${f##*/}" = hooks.json ] && continue
    cp -Rp "$f" "$V/hooks/"
  done
done
cp -p "$KIT/plugins/session-context/bin/claude-gc" "$V/bin/"

# review-gates asserts the two-agent heavy tier, which needs the thermos plugin enabled, and bare
# agent names, which resolve only with user-level agents: give it a config dir holding both.
CFG="$SCRATCH/config"; mkdir -p "$CFG/agents" "$CFG/local"
cp "$KIT"/plugins/auto-review/agents/*.md "$CFG/agents/"
printf '{"enabledPlugins":{"thermos@fixture":true}}\n' > "$CFG/settings.json"
START_KIT=${KIT_ENV:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/local/kit.env}
[ -r "$START_KIT" ] && cp "$START_KIT" "$CFG/local/kit.env"

echo "=== hook suites (plugin copies) ==="
suite hooktest env HOOKS_DIR="$V/hooks" bash "$KIT/tests/hooks/hooktest.sh"
suite review-gates env HOOKS_DIR="$V/hooks" CLAUDE_CONFIG_DIR="$CFG" KIT_ENV="$CFG/local/kit.env" bash "$KIT/tests/hooks/review-gates.sh"
suite bash-guards env HOOKS_DIR="$V/hooks" python3 "$KIT/tests/hooks/bash-guards.test.py"
suite ro-mysql env HOOKS_DIR="$V/hooks" RO_MYSQL="$KIT/plugins/prod-data/bin/ro-mysql" python3 "$KIT/tests/hooks/ro-mysql.test.py"

echo "=== bash-guards: one script, two guard sets ==="
pre() { jq -cn --arg c "$1" --arg d "${2:-$SCRATCH}" '{tool_name:"Bash",hook_event_name:"PreToolUse",session_id:"kit",cwd:$d,tool_input:{command:$c}}'; }
decision() { local d; d=$(jq -r '.hookSpecificOutput.permissionDecision // empty' 2>/dev/null); echo "${d:-allow}"; }
g() { pre "$2" | "$KIT/plugins/$1/hooks/bash-guards" "$3" | decision; }
DB=my; DB="${DB}sql"
[ "$(g prod-data "$DB -e 'select 1'" db)" = deny ] && ok "db set denies a raw client" || bad "db set let a raw client through"
[ "$(g guard-rails "$DB -e 'select 1'" git)" = allow ] && ok "git set leaves DB clients alone" || bad "git set judged a DB client"
[ "$(g guard-rails 'git push origin b --force' git)" = ask ] && ok "git set asks on a force push" || bad "git set missed a force push"
[ "$(g prod-data 'git push origin b --force' db)" = allow ] && ok "db set leaves git alone" || bad "db set judged git"
[ "$(g guard-rails "git commit -m 'no trailers'" git)" = deny ] && ok "git set denies a commit without trailers" || bad "git set: trailers"
[ "$(pre "$DB -e 'select 1'" | "$KIT/plugins/guard-rails/hooks/bash-guards" | decision)" = deny ] \
  && ok "no argument runs every guard (the ~/.claude wiring)" || bad "no-argument bash-guards skipped the DB guards"

echo "=== auto-review in a plugin install ==="
R="$SCRATCH/repo"; git init -q -b main "$R"
printf 'x = 1\n' > "$R/a.py"; git -C "$R" add a.py; git -C "$R" -c user.name=t -c user.email=t@t commit -qm init
EMPTY="$SCRATCH/empty-config"; mkdir -p "$EMPTY"
rt() { # rt <sid> <lines>: edit <lines> lines, then Stop, with no user-level agents and no thermos.
  local i=0; while [ "$i" -lt "$2" ]; do echo "v_$1_$i = $i"; i=$((i + 1)); done >> "$R/$1.py"
  jq -cn --arg f "$R/$1.py" --arg s "$1" '{hook_event_name:"PostToolUse",tool_name:"Edit",session_id:$s,cwd:"/",tool_input:{file_path:$f}}' \
    | CLAUDE_CONFIG_DIR="$EMPTY" TMPDIR="$SCRATCH/tmp" "$KIT/plugins/auto-review/hooks/review-mark-changes"
  jq -cn --arg s "$1" --arg d "$R" '{hook_event_name:"Stop",session_id:$s,cwd:$d,stop_hook_active:false}' \
    | CLAUDE_CONFIG_DIR="$EMPTY" TMPDIR="$SCRATCH/tmp" "$KIT/plugins/auto-review/hooks/review-trigger"
}
mkdir -p "$SCRATCH/tmp"
out=$(rt light 60)
check "light tier names the plugin's reviewer" "$out" 'subagent_type \\"auto-review:reviewer\\"'
check "...and the plugin's critic" "$out" 'subagent_type \\"auto-review:critic\\"'
jq -cn --arg d "$R" '{hook_event_name:"SubagentStop",session_id:"light",cwd:$d,agent_id:"a1",agent_type:"auto-review:reviewer",stop_hook_active:false,last_assistant_message:"one finding"}' \
  | CLAUDE_CONFIG_DIR="$EMPTY" TMPDIR="$SCRATCH/tmp" "$KIT/plugins/auto-review/hooks/review-agent-mark"
st=$(CLAUDE_CONFIG_DIR="$EMPTY" TMPDIR="$SCRATCH/tmp" bash -c '. "$1/lib/review-state" && rv_pending light "$2" && echo pending || echo done' _ "$KIT/plugins/auto-review/hooks" "$R")
[ "$st" = done ] && ok "a namespaced reviewer's return completes the round" || bad "auto-review:reviewer return left the round pending"
out=$(rt heavy 200)
check "heavy tier without thermos: one deep reviewer" "$out" 'DEEP single-agent.*auto-review:reviewer'

echo "=== hook-io in a plugin install ==="
C="$SCRATCH/copy"; cp -R "$KIT/plugins/ci-babysitter/hooks" "$C"; rm -f "$C/lib/review-state"
out=$(pre "git push" "$R" | CLAUDE_PLUGIN_ROOT="$KIT/plugins/ci-babysitter" "$C/pre-push-gate")
check "a missing lib names the plugin, not a ~/.claude checkout" "$out" 'Reinstall or update the plugin that ships pre-push-gate'

echo "=== bqro: jobs project from the overlay ==="
FB="$SCRATCH/fakebin"; mkdir -p "$FB"
cat > "$FB/bq" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$BQ_ARGS_LOG"
case " $* " in *" --dry_run "*) echo '{"statistics":{"query":{"statementType":"SELECT","totalBytesProcessed":"0"}}}' ;; *) echo '[]' ;; esac
EOF
chmod +x "$FB/bq"
printf 'BQRO_PROJECT=%s\n' "'kit-jobs'" > "$SCRATCH/bq.env"
LOG="$SCRATCH/bq.log"; BQRO="$KIT/plugins/prod-data/bin/bqro"
# bqro <env assignments...> -- <bqro args...>, with the fake bq first on PATH and BQRO_PROJECT unset.
bqro() {
  local -a e=(); while [ "$1" != -- ]; do e+=("$1"); shift; done; shift
  rm -f "$LOG"
  env -u BQRO_PROJECT BQ_ARGS_LOG="$LOG" PATH="$FB:$PATH" ${e[@]+"${e[@]}"} python3 "$BQRO" "$@"
}
bqro KIT_ENV="$SCRATCH/bq.env" -- 'SELECT 1' >/dev/null 2>&1
check "overlay BQRO_PROJECT is the jobs project" "$(cat "$LOG" 2>/dev/null)" '--project_id=kit-jobs'
bqro KIT_ENV="$SCRATCH/bq.env" BQRO_PROJECT=env-jobs -- 'SELECT 1' >/dev/null 2>&1
check "the environment's BQRO_PROJECT wins over the overlay" "$(cat "$LOG" 2>/dev/null)" '--project_id=env-jobs'
bqro KIT_ENV="$SCRATCH/bq.env" -- 'SELECT 1' --project_id=flag-jobs >/dev/null 2>&1
check "--project_id wins over both" "$(cat "$LOG" 2>/dev/null)" '--project_id=flag-jobs'
absent "...and is passed once" "$(cat "$LOG" 2>/dev/null)" 'kit-jobs'
err=$(bqro KIT_ENV=/dev/null -- 'SELECT 1' 2>&1); rc=$?
[ "$rc" = 2 ] && [ ! -f "$LOG" ] && ok "no project anywhere: refuses before calling bq" || bad "no project: rc=$rc" "$err"
check "...and says where to set it" "$err" 'BQRO_PROJECT in ~/.claude/local/kit.env'

echo
echo "RESULT: $pass passed, $fail failed"
[ "$fail" = 0 ]
