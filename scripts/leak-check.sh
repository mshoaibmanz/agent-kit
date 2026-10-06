#!/bin/bash
# The org denylist check: the one matcher for scripts/scan.sh (CI and local) and .githooks/pre-push.
#   scripts/leak-check.sh [--paths FILE [--tree DIR]] [--allow REV] [-- REV-LIST-ARGS...]
#     --paths FILE  file names, one per line
#     --tree DIR    a copy of the --paths files; their text is checked
#     --allow REV   history exceptions: the commit ids in .scan-history-allow as committed at REV (one
#                   per line, # comments; the checkout's copy only if REV has none), each honoured only
#                   when REV already contains that commit. REV must be a commit the checked change did
#                   not add (CI: the PR's base or the previous main), so a change cannot exempt its own
#                   commits. Empty or unknown REV: no exceptions.
#     -- ARGS       a `git rev-list` range. Each commit's message, author, committer and added diff lines
#                   (binary content as text; removed lines, context and hunk headers are not) are
#                   checked, plus the annotated tags ARGS names (every tag for --all or --tags) and any
#                   tag those tags point to.
#
# The denylist, first one set wins:
#   LEAK_TERMS            the terms themselves (the CI secret)
#   AGENT_KIT_LEAK_TERMS  a file; `none` turns the check off (exit 3)
#   <kit root>/local/leak-terms.txt, kit root = $AGENT_KIT_DIR, else ~/.local/share/agent-kit
#   ~/.config/claude-kit/leak-terms.txt, the old default
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
tree='' paths='' allow='' base='' history=0
die() { echo "leak-check: $*" >&2; exit 2; }
while [ $# -gt 0 ]; do
  case $1 in
    --tree) [ $# -ge 2 ] && [ -n "$2" ] || die "--tree needs a directory"; tree=$2; shift 2 ;;
    --paths) [ $# -ge 2 ] && [ -n "$2" ] || die "--paths needs a file"; paths=$2; shift 2 ;;
    --allow) [ $# -ge 2 ] || die "--allow needs a revision (empty for none)"; allow=$2; shift 2 ;;
    --) history=1; shift; break ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ -z "$tree" ] || [ -n "$paths" ] || die "--tree needs --paths"
TMP=$(mktemp -d) || exit 2
trap 'rm -rf "$TMP"' EXIT
g() { git -C "$KIT" "$@"; }

if [ -n "${LEAK_TERMS:-}" ]; then
  printf '%s\n' "$LEAK_TERMS" > "$TMP/raw"
elif [ "${AGENT_KIT_LEAK_TERMS:-}" = none ]; then
  echo "leak-check: WARNING: the denylist check is off (AGENT_KIT_LEAK_TERMS=none); skipped" >&2
  exit 3
else
  list=${AGENT_KIT_LEAK_TERMS:-}
  if [ -z "$list" ]; then
    list=${AGENT_KIT_DIR:-$HOME/.local/share/agent-kit}/local/leak-terms.txt
    old=$HOME/.config/claude-kit/leak-terms.txt
    [ -r "$list" ] || [ ! -r "$old" ] || list=$old
  fi
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
  g rev-list "$@" > "$TMP/revs" || die "git rev-list failed"
  : > "$TMP/allow"
  if [ -n "$allow" ] && base=$(g rev-parse -q --verify "${allow}^{commit}"); then
    # The checkout's copy only while REV predates the file (the change that adds it); either way an
    # entry must be in REV's history, which a change under check can never add to.
    if g cat-file -e "${base}:.scan-history-allow" 2>/dev/null; then
      g show "${base}:.scan-history-allow"
    else
      cat "$KIT/.scan-history-allow" 2>/dev/null
    fi | grep -vE '^(#|$)' > "$TMP/allow"
  fi
  while IFS= read -r c; do
    if [ -s "$TMP/allow" ] && grep -qxF "$c" "$TMP/allow" && g merge-base --is-ancestor "$c" "$base"; then
      nskip=$((nskip + 1)); continue
    fi
    ncommits=$((ncommits + 1))
    g log -1 --format='%an <%ae>%n%cn <%ce>%n%B' "$c" > "$TMP/h/$c.commit" \
      && g show -U0 --format= --text --no-color --no-ext-diff "$c" > "$TMP/diff" \
      || die "cannot read commit $c"
    # Only what the commit adds: a removed line (and the --- header) takes away text an ancestor added,
    # which is checked or already public; a hunk header repeats a nearby unchanged line.
    grep -avE '^(-|@@)' "$TMP/diff" >> "$TMP/h/$c.commit"
  done < "$TMP/revs"
  # The tag objects ARGS names (rev-parse expands --all and --tags), then any tag they point to: a tag
  # of a tag publishes both messages even when the inner one has no ref.
  g rev-parse --revs-only "$@" > "$TMP/named" || die "git rev-parse failed"
  grep -v '^\^' "$TMP/named" | g cat-file --batch-check='%(objecttype) %(objectname)' \
    | sed -n 's/^tag //p' > "$TMP/queue"
  : > "$TMP/tags"
  while [ -s "$TMP/queue" ]; do
    mv "$TMP/queue" "$TMP/batch"
    : > "$TMP/queue"
    while IFS= read -r t; do
      ! grep -qxF "$t" "$TMP/tags" || continue
      echo "$t" >> "$TMP/tags"
      g cat-file tag "$t" > "$TMP/h/$t.tag" || die "cannot read tag $t"
      [ "$(sed -n 2p "$TMP/h/$t.tag")" != 'type tag' ] || sed -n '1s/^object //p' "$TMP/h/$t.tag" >> "$TMP/queue"
    done < "$TMP/batch"
  done
  ntags=$(grep -c . "$TMP/tags")
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
