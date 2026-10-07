---
name: code-search
description: Search code and read files across repos, starting with refreshed local clones under the configured CODE_DIRS roots, then the GitHub org, and using Zoekt only for repos without GitHub access. Use when the answer lives in another repo, to find callers or consumers, or before claiming "nothing else uses X".
---

# code-search

Pull the latest changes in local clones before searching them, then use GitHub for missing coverage. Use the optional configured Zoekt site only for repos the current GitHub login cannot read. A missing search result does not establish missing access.

## 1. Local clones

Discover clone roots from `CODE_DIRS_JSON` in the setup overlay: a JSON array of parent directories that preserves spaces. Expand leading `~/`. The legacy `CODE_DIRS` list remains supported for roots without spaces. Do not assume everyone uses `~/Code/`. If roots are unset or missing, ask for their location during setup and record `CODE_DIRS_JSON`; there is no assumed local root.

```sh
export AGENT_KIT_DIR="${CLAUDE_PLUGIN_ROOT}/kit"
. "${AGENT_KIT_DIR}/hooks/lib/hook-io"
kit_env
printf '%s\n' "${CODE_DIRS_JSON:-[]}" | jq -r ' .[]'
repo='<configured-root>/<repo>'
git -C "$repo" remote get-url origin
git -C "$repo" status --porcelain
git -C "$repo" symbolic-ref --quiet --short HEAD
git -C "$repo" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}'
upstream_remote=$(git -C "$repo" for-each-ref --format='%(upstream:remotename)' "$(git -C "$repo" symbolic-ref --quiet HEAD)")
git -C "$repo" fetch --no-recurse-submodules "$upstream_remote"
upstream_tip=$(git -C "$repo" rev-parse '@{upstream}')
git -C "$repo" rev-list --left-right --count "HEAD...$upstream_tip"
# Continue only after the eligibility checks above pass; inspect incoming additions.
if ! python3 - "$repo" "$upstream_tip" <<'PY_COLLISIONS'
import os
import subprocess
import sys
from pathlib import Path

repo, tip = Path(sys.argv[1]), sys.argv[2]
added = subprocess.check_output([
    'git', '-C', str(repo), 'diff', '--name-only', '--diff-filter=A',
    '--no-renames', '-z', 'HEAD', tip, '--',
])
collisions = []
for raw in added.split(b'\0'):
    if not raw:
        continue
    relative = Path(os.fsdecode(raw))
    if os.path.lexists(repo / relative) or any(
        (repo / parent).is_symlink()
        for parent in relative.parents if parent != Path('.')
    ):
        collisions.append(str(relative))
for path in collisions:
    print(f'skip refresh: incoming path collides with local file or symlink: {path!r}')
raise SystemExit(bool(collisions))
PY_COLLISIONS
then
  printf '%s\n' 'Use GitHub for this repo and report the local collision.' >&2
  exit 1
fi
git -C "$repo" status --porcelain
# Require the checkout still clean, then pull the exact fetched/preflighted tip.
git -C "$repo" -c merge.autoStash=false -c rebase.autoStash=false pull --ff-only --no-rebase --no-autostash . "$upstream_tip"
git -C "$repo" rev-parse HEAD
rg -n -F -- 'endpoint_or_field' '<refreshed-repo-1>' '<refreshed-repo-2>'
rg -n -F -g '*.py' -g '!tests/**' -- 'endpoint_or_field' '<refreshed-repo>'
```

Refresh every clone you will search, even if it already appears current. Require a clean checkout and an attached branch with an upstream before fetching. Fetch that configured upstream, capture its SHA, then require zero local commits ahead of the fetched tip; the first `rev-list` count is local commits ahead. Before pulling, inspect the added paths in `HEAD..upstream_tip` with renames disabled and NUL-delimited names. Refuse refresh if an incoming path already exists locally, including an ignored file or dangling symlink, or any parent is a symlink. This checks only incoming paths; unrelated `.venv` or `node_modules` files do not disqualify the clone. Clean porcelain output alone is insufficient: Git can overwrite ignored files when the remote starts tracking those paths. Recheck cleanliness immediately before pulling the exact inspected SHA from `.`; this still runs `pull --ff-only` for every eligible clone and avoids fetching a different remote tip between the collision check and application. If the clone has edits, unpushed commits, a detached HEAD, no upstream, collisions, divergence, or any fetch/preflight/pull failure, skip searching it and use GitHub for that repo. Report the freshness or coverage gap. Never stash, reset, rebase, delete a collision or discard changes automatically. `--no-autostash` prevents a user's git configuration from stashing edits implicitly.

Start with the likely repo when known; otherwise refresh the clones under the configured roots, then search only the refreshed repo paths in one `rg` call. Use the session's LSP symbol tools for a selected refreshed clone, and `rg` for cross-repo searches, literals and config. Verify repo identity from its remote rather than assuming its directory name is the GitHub name. `rg` respects each clone's ignore rules; read relevant hits in their files.

Report the refreshed checkout's path, branch and SHA. A successful pull updates the checked-out branch, which may differ from the remote's default branch. For an explicit ref, confirm it resolves to the requested current remote ref before using `git show '<ref>:<path>'`; otherwise use GitHub `file --ref`. Do not switch the user's checkout to answer a search question.

If local files answer the question, stop. Continue to GitHub for missing files, default-branch confirmation, repos absent locally, or a claim that requires coverage beyond the checked-out clones.

A repo you need to read in depth but have no clone of gets a full `gh repo clone` into the first configured clone root, never the scratchpad or a temp directory, and is then searched like any other clone.

## 2. GitHub org

The existing helper uses the user's authenticated `gh` CLI. Pass `--backend gh` explicitly. Its legacy `auto` mode prefers configured Zoekt, so omit neither the backend nor the local step.

```sh
export AGENT_KIT_DIR="${CLAUDE_PLUGIN_ROOT}/kit"
"${AGENT_KIT_DIR}/skills/code-search/scripts/code_search.py" search 'plain terms' --backend gh --repo owner/name --file filename --lang python
"${AGENT_KIT_DIR}/skills/code-search/scripts/code_search.py" search 'plain terms' --backend gh
"${AGENT_KIT_DIR}/skills/code-search/scripts/code_search.py" file owner/name path/to/file --backend gh --ref branch --lines 20:80
"${AGENT_KIT_DIR}/skills/code-search/scripts/code_search.py" repos 'repo-name' --backend gh
gh repo view owner/name --json nameWithOwner,defaultBranchRef
```

Omit `--repo` to search `CODE_SEARCH_GH_OWNER`, the configured org. `REPO` can be `name`, `owner/name` or `github.com/owner/name`; bare names use that same owner. Do not use an unscoped org search if no owner is configured.

GitHub code search covers default branches of accessible repos. Its index is partial, fragments lack line numbers, and it allows about ten code searches per minute. Batch the question into a narrow query, then read files rather than repeatedly searching. Use plain terms, with `--repo`, `--file` and `--lang` filters. Zoekt operators are not valid GitHub syntax.

An empty result calls for alternate spellings, a file read or a repo access check. A successful `gh repo view` means GitHub access exists, even if code search returns nothing. Do not fall back to Zoekt for that repo. A network error, rate limit or expired GitHub login also does not establish missing repo access; report or resolve that error first.

## 3. Zoekt for repos without GitHub access

Use this only after GitHub denies access to a known repo, or the session already establishes that the current GitHub login cannot read it. Confirm the repo identity before treating a GitHub 404 as inaccessible; a typo produces the same status. Scope Zoekt calls to those repos. Do not search both backends across the whole org by default.

Before the first Zoekt call in a session:

- Tell the user a macOS keychain dialog for "Chrome Safe Storage" may appear. They should click **Allow**, not **Always Allow**. Always Allow lets any process decrypt every Chrome cookie.
- Run `auth` alone first. Parallel calls without a cached cookie each raise a prompt.
- The helper caches the decrypted session cookie in the login keychain until expiry. On a rejected cookie it re-reads Chrome once.
- For "session expired" or "no cookie", the user opens the site in their own Chrome and logs in; retry once. Playwright uses a separate profile and cannot use their login.

```sh
export AGENT_KIT_DIR="${CLAUDE_PLUGIN_ROOT}/kit"
"${AGENT_KIT_DIR}/skills/code-search/scripts/code_search.py" auth
"${AGENT_KIT_DIR}/skills/code-search/scripts/code_search.py" search '"exact phrase"' --backend zoekt --repo '^github.com/owner/name$' -C 10
"${AGENT_KIT_DIR}/skills/code-search/scripts/code_search.py" file owner/name path/to/file --backend zoekt --lines 20:80
"${AGENT_KIT_DIR}/skills/code-search/scripts/code_search.py" repos '^github.com/owner/name$' --backend zoekt
```

The helper uses Python 3. Local and GitHub paths require no Python packages. Zoekt additionally requires `cryptography` in the interpreter environment. Preserve its auth wrappers. Never read, print or copy cookies, tokens, Chrome storage or keychain values yourself. Exit 2 indicates an auth problem and explains the user action needed.

Zoekt serves the indexed default-branch HEAD, supports regex, returns line numbers and allows any `-C` context or whole-file reads. Output is grep-like: `line:` is a match, `line-` is context, and each file carries its indexed sha `@...`.

## Query syntax (zoekt)

Terms are ANDed per file, and every occurrence of each term is printed. So quote anything with a space: `def foo` matches every `def` in a file that also contains `foo`. Use `"exact phrase"`, `/regex/`, `or`, `-term`, and:
- `r:repo`: regex on the repo name;
- `f:path` / `-f:tests`: regex on the path;
- `lang:python`;
- `sym:name`: definitions only;
- `case:yes`.

## Traps (each one has cost a wrong conclusion)

- **"No caller" needs four checks:**
  1. relevant local clones, then the GitHub org, and scoped Zoekt for known repos without GitHub access; state remaining coverage gaps;
  2. every spelling of the path (`/public/client/x`, `/client/x`, the bare tail);
  3. the name split across an f-string;
  4. reading each hit. A commented-out call, or one after an early `return []`, still matches.
- **Forks and copies.** The same module often lives in several repos. The live one is the repo the deploy manifest names (search the service name in the infra repo for `gitrepo:`).
- **The web UI caps context at 5 lines and its file view may not render.** The script's API calls have neither limit.
- **A surprising absence may be index lag.** Record the indexed sha and coverage limit when GitHub cannot confirm it.
- **Code shows the rule; prod data shows what ran.** When consumer behaviour carries a production conclusion, use the `debug` skill to confirm that consumer's output.

## Config

The keys live in the kit overlay `${AGENT_KIT_DIR}/local/setup-paths.env`, documented in `${AGENT_KIT_DIR}/kit.env.example`. Let the helper load them; do not display the overlay.

- `CODE_SEARCH_ZOEKT_URL`, `CODE_SEARCH_SSO_COOKIE` and `CODE_SEARCH_CHROME_PROFILE` drive zoekt. With no URL, zoekt is off.
- `CODE_SEARCH_GH_OWNER` sets the default org.
- `CODE_DIRS_JSON` sets the local clone parent directories. Configure it during setup; missing roots are a coverage gap, not a reason to assume `~/Code/`.
