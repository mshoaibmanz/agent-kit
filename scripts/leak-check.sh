#!/bin/bash
# The org denylist check: the one matcher for scripts/scan.sh (CI and local) and .githooks/pre-push.
#   scripts/leak-check.sh [--paths FILE [--tree DIR]] [--allow FILE] [-- REV-LIST-ARGS...]
#     --paths FILE  file names, one per line
#     --tree DIR    a copy of the --paths files; their text is checked
#     --allow FILE  commit ids to skip, one per line (# comments). Honoured only for commits already in
#                   the public history: ancestors of origin/HEAD, else origin/main.
#     -- ARGS       a `git rev-list` range. Each commit's message, author, committer and added diff lines
#                   (binary content as text) are checked, plus the annotated tags named in ARGS (every
#                   tag when ARGS has --all or --tags).
#
# The denylist, first one set wins:
#   LEAK_TERMS            the terms themselves (the CI secret)
#   AGENT_KIT_LEAK_TERMS  a file
#   <kit root>/local/leak-terms.txt, kit root = $AGENT_KIT_DIR, else ~/.local/share/agent-kit
# Never commit the list. One term per line, case-insensitive; blank lines and # comments are skipped.
#   - A line of only letters, digits and . _ - is a plain term. One of 6 characters or fewer matches
#     only at the start of a word: `abc` finds abc, abcApi and x.abc, not xabc or x_abc. A longer one
#     matches anywhere.
#   - Any other line is an ERE, used as written. An invalid one stops the check (exit 2).
# Output names a hit by the term's line number in the list and a file's line number in --paths, never
# by the term, the matching text or a file name: CI logs of a public repo are public.
# Exit 0 clean, 1 on a hit, 2 on a usage or git error, 3 with no list (one warning line on stderr).
set -uo pipefail
KIT=$(cd "$(dirname "$0")/.." && pwd)
tree='' paths='' allow='' history=0
while [ $# -gt 0 ]; do
  case $1 in
    --tree) tree=${2:?--tree needs a directory}; shift 2 ;;
    --paths) paths=${2:?--paths needs a file}; shift 2 ;;
    --allow) allow=${2:?--allow needs a file}; shift 2 ;;
    --) history=1; shift; break ;;
    *) echo "leak-check: unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -z "$tree" ] || [ -n "$paths" ] || { echo "leak-check: --tree needs --paths" >&2; exit 2; }
TMP=$(mktemp -d) || exit 2
trap 'rm -rf "$TMP"' EXIT
g() { git -C "$KIT" "$@"; }

if [ -n "${LEAK_TERMS:-}" ]; then
  printf '%s\n' "$LEAK_TERMS" > "$TMP/raw"
else
  list=${AGENT_KIT_LEAK_TERMS:-${AGENT_KIT_DIR:-$HOME/.local/share/agent-kit}/local/leak-terms.txt}
  if [ ! -r "$list" ]; then
    echo "leak-check: WARNING: no denylist at $list (set AGENT_KIT_LEAK_TERMS to your list); skipped" >&2
    exit 3
  fi
  cp "$list" "$TMP/raw"
fi

# "<line number><TAB><ERE>" per term.
i=0
: > "$TMP/terms"
while IFS= read -r line || [ -n "$line" ]; do
  i=$((i + 1))
  line=$(printf '%s' "$line" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
  case $line in ''|'#'*) continue ;; esac
  case $line in
    *[!A-Za-z0-9._-]*) ere=$line ;;
    *)
      ere=$(printf '%s' "$line" | sed 's/\./\\./g')
      if [ ${#line} -le 6 ]; then
        case $line in [A-Za-z0-9]*) ere="(^|[^[:alnum:]_])$ere" ;; esac
      fi ;;
  esac
  grep -E -e "$ere" < /dev/null > /dev/null 2>&1
  [ $? != 2 ] || { echo "leak-check: line $i of the denylist is not a valid ERE" >&2; exit 2; }
  printf '%s\t%s\n' "$i" "$ere" >> "$TMP/terms"
done < "$TMP/raw"
nterms=$(grep -c . "$TMP/terms")
if [ "$nterms" = 0 ]; then
  echo "leak-check: WARNING: the denylist has no terms; skipped" >&2
  exit 3
fi

# One file per commit (<sha>.commit) and per tag object (<sha>.tag) in $TMP/h.
mkdir "$TMP/h"
ncommits=0 ntags=0 nskip=0
if [ "$history" = 1 ]; then
  g rev-list "$@" > "$TMP/revs" || { echo "leak-check: git rev-list failed" >&2; exit 2; }
  public=''
  for ref in refs/remotes/origin/HEAD refs/remotes/origin/main; do
    public=$(g rev-parse -q --verify "$ref^{commit}") && break
  done
  [ -n "$allow" ] && [ -r "$allow" ] && grep -vE '^(#|$)' "$allow" > "$TMP/allow" || : > "$TMP/allow"
  while IFS= read -r c; do
    if [ -n "$public" ] && grep -qxF "$c" "$TMP/allow" && g merge-base --is-ancestor "$c" "$public"; then
      nskip=$((nskip + 1)); continue
    fi
    ncommits=$((ncommits + 1))
    g log -1 --format='%an <%ae>%n%cn <%ce>%n%B' "$c" > "$TMP/h/$c.commit" \
      && g show --format= --text --no-color --no-ext-diff "$c" > "$TMP/diff" \
      || { echo "leak-check: cannot read commit $c" >&2; exit 2; }
    # Deleted lines only remove text an ancestor added; that ancestor is checked or already public.
    grep -avE '^-([^-]|$)' "$TMP/diff" >> "$TMP/h/$c.commit"
  done < "$TMP/revs"
  {
    case " $* " in *" --all "*|*" --tags "*)
      g for-each-ref --format='%(objecttype) %(objectname)' refs/tags | sed -n 's/^tag //p' ;;
    esac
    negative=0
    for arg in "$@"; do
      case $arg in
        --not) negative=$((1 - negative)) ;;
        -*|^*) ;;
        *) [ "$negative" = 1 ] || [ "$(g cat-file -t "$arg" 2>/dev/null)" != tag ] || g rev-parse "$arg" ;;
      esac
    done
  } | sort -u > "$TMP/tags"
  while IFS= read -r t; do
    ntags=$((ntags + 1))
    g cat-file tag "$t" > "$TMP/h/$t.tag" || { echo "leak-check: cannot read tag $t" >&2; exit 2; }
  done < "$TMP/tags"
fi

fail=0
while IFS="$(printf '\t')" read -r n ere; do
  {
    [ -z "$paths" ] || grep -niE -e "$ere" "$paths" | sed "s/:.*//; s/^/term #$n in file name #/"
    if [ -n "$tree" ]; then
      grep -rlIiE -e "$ere" "$tree" | sed "s|^$tree/||" \
        | awk -v n="$n" 'NR == FNR { hit[$0] = 1; next } $0 in hit { print "term #" n " in file #" FNR }' - "$paths"
    fi
    grep -rlaiE -e "$ere" "$TMP/h" | sed -E "s#.*/([0-9a-f]{12})[0-9a-f]*\.(commit|tag)\$#term \#$n in \2 \1#"
  } > "$TMP/hits"
  [ -s "$TMP/hits" ] && fail=1
  cat "$TMP/hits"
done < "$TMP/terms"

printf '%s term(s) checked over %s file name(s), %s commit(s), %s tag(s)' \
  "$nterms" "$( [ -z "$paths" ] && echo 0 || grep -c '' "$paths")" "$ncommits" "$ntags"
[ "$nskip" = 0 ] && echo || echo "; $nskip public commit(s) skipped by --allow"
exit "$fail"
