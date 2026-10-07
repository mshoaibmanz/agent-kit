# Sourced by the kit's shell tools in bin/: kit_root <script> prints the kit that script runs from,
# the shell twin of kit_env.py's kit_root. $AGENT_KIT_DIR when set, else the script's folder's parent.
# Links are followed only until a folder holding an agent-setup install (.install-state): a dev
# install links each kit file into its checkout, whose overlay, roles and state are not the install's.
# bash 3.2 safe.
kit_root() {
  local src=$1 link n=0 kit
  if [ -n "${AGENT_KIT_DIR:-}" ]; then
    printf '%s\n' "$AGENT_KIT_DIR"
    return 0
  fi
  while :; do
    kit=$(cd -P "${src%/*}/.." 2>/dev/null && pwd) || return 1
    if [ ! -L "$src" ] || [ -d "$kit/.install-state" ]; then
      printf '%s\n' "$kit"
      return 0
    fi
    n=$((n + 1))
    [ "$n" -le 40 ] || return 1
    link=$(readlink "$src") || return 1
    case $link in /*) src=$link ;; *) src=${src%/*}/$link ;; esac
  done
}
