# Optional shell functions; account routing remains in the shared account helper.
claude() {
  local helper="${AGENT_KIT_DIR:-$HOME/.agents}/bin/claude-account"
  [[ -x "$helper" ]] || helper="$HOME/.claude/bin/claude-account"
  local target cli=${CLAUDE_BIN:-claude}
  target=$("$helper" launch "$PWD") || return $?
  if [[ -z "$target" || "$target" == "$HOME/.claude" ]]; then
    env -u CLAUDE_CONFIG_DIR "$cli" "$@"
  else
    CLAUDE_CONFIG_DIR="$target" command "$cli" "$@"
  fi
}

cw() {
  CLAUDE_ACCOUNT=work claude "$@"
}
