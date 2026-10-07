# agent-kit

Shared working rules, review roles, hooks and selected skills for Claude Code, Codex and Cursor. Keep one source checkout and install the parts each person wants. Company policy, repository paths and credentials stay in each person's local setup.

Python 3.11 or newer and Git are required. Hooks also need `jq` and the selected provider CLI. Provider logins are reused in place: setup never opens or copies authentication files, changes permissions, or approves hook trust.

## Setup

Clone this repository to a directory you choose, then take one of three paths:

1. **Team preset**, one command for a teammate whose team keeps a preset (see below):

   ```sh
   python3 bin/agent-setup --preset gh:your-org/agent-kit-preset --apply
   ```

2. **Guided**: asks only what it cannot detect, previews, then asks before it applies:

   ```sh
   python3 bin/agent-setup --interactive
   ```

3. **Scripted**: name everything, preview, then add `--apply`:

   ```sh
   python3 bin/agent-setup \
     --components rules skills \
     --skills conventions unslop code-search \
     --root-dir "$HOME/.local/share/agent-kit" \
     --repo-roots "$HOME/Code" "$HOME/Work projects" \
     --github-owner your-org \
     --work-root "$HOME/agent-work"
   ```

Setup detects before it asks. Hosts default to the ones installed (the CLI on `PATH` under any name its installer uses, or a config directory, honouring `CLAUDE_CONFIG_DIR` and `CODEX_HOME`); with none, kit files install now, and a later run that finds a host sets it up. The guided setup (and `--yes` or `--preset`) also finds repository parents under `~/Code`, `~/src`, `~/dev` and `~/projects`, and the GitHub owner from your only org in `gh api user/orgs` or the origin remotes it finds. `--yes` takes every default and detected value without a prompt, `--interactive` included; without a terminal setup never waits for input. Repository paths use a JSON array so spaces stay intact. Fresh scripted setup has no local repository roots; pass `--repo-roots` with no values to clear saved roots and use scoped remote search.

### What setup checks

Every run prints a short summary, one line per item with the fix command for this OS, then the change count by kind (`--verbose` adds every path, `--json` prints the plan as data):

- **Ready**: found and working, such as a host CLI that is signed in.
- **Needs attention**: something to fix. Lines marked "blocks apply" stop `--apply` up front: a missing hard requirement (Python 3.11+, `git`, `jq` for hooks), unconfirmed host hooks, an enabled plugin with the same hooks, or a path that holds your own content.
- **Skipped**: a missing soft requirement (`gh` or its login, `uv` or `npx` for an MCP server, `gcloud` or `bq`) turns off only the feature that needs it. An MCP server whose runtime is missing is not registered; a rerun after you install it adds the server.

Your own content is never overwritten silently: an existing file, link, MCP server or edited managed block of yours blocks apply. The guided setup asks whether to back it up, skip it or abort, and keeps your answers for the rerun when it stops. In scripts choose `--collision backup` (replace it; the local rollback journal keeps yours) or `--collision skip` (leave it and install around it). Unrelated skills, rules, hooks and MCP entries are preserved.

### Team presets

A preset is a TOML file of non-secret team defaults: a top-level one-line `description` of what the preset and its pack are for (the dashboard shows it), `[kit]` overlay keys (`CODE_SEARCH_GH_OWNER`, `CODE_DIRS_JSON`, `CODE_SEARCH_ZOEKT_URL`, `REVIEW_BASE`, `RELEASE_BRANCH_RE`, `BQRO_PROJECT`, `GIT_AUTHOR`, `SUBAGENT_RESUME_MAX`, `TICKET_PREFIXES`; [`hooks/lib/README`](hooks/lib/README) gives their meanings), `[mcp.servers.<name>]` descriptors that name credential wrappers (`{{KIT_DIR}}/bin/sentry-mcp`, filled with each user's kit root), a server in a local clone (`{{CODE_DIR}}/<repo>/<path>`, under the first repository root; left out, and listed under `Skipped`, until the clone has it), a team pack's server (`{{PACK_DIR}}`, below) or native OAuth, `[plugins.<name>]` Claude Code plugins to enable (`marketplace`, and `source = "github:owner/repo"` to add that marketplace), `[sentry.instances.<name>]` Sentry instances (`host` and the `keychain` service of each one's token; every instance beyond the catalog's `sentry` server renders as its own server once you have its token; a token stored later appears after `agent-kit render` or `agent-setup sync` and a new session; and `bin/sentry-map find <service>` names the instance, org and project, and marks the live copy per label), `[hosts] recommended`, `[roles.<role>]` model and effort, and `[skills]` include or exclude. `gh:owner/repo[/path]` (default path `agent-kit-preset.toml`) fetches it with your own `gh` login, with its team pack (below) when it has one; a failed fetch prints one line and continues on the plain defaults. A local TOML file or pack folder works too. Setup checks that your login is a member of the preset's GitHub org. It refuses a preset holding a token format (`ghp_`, `sk-`, `xox*-`, `AKIA` and the like) or credentials in a URL. Its `[kit]` values go in `local/preset.env`, under your own `local/kit.env`, and its answers sit under yours (flags and the choices of an earlier install or prompt). A changed preset applies on the next run with `--preset`; `doctor` reports its source and hash. [`presets/example.toml`](presets/example.toml) shows every table with placeholders; keep your team's real preset in a private repository of your organization, never in this public one.

### Team packs

A team pack is a preset repository that also carries skills, a rules block and MCP servers. Each part is optional, and the pack is these four paths in the preset's folder (the repository root, or `team/` for `gh:<org>/<repo>/team/agent-kit-preset.toml`); setup reads nothing else of the repository:

```text
agent-kit-preset.toml      the preset above
skills/<name>/SKILL.md     team skills, with references/ or scripts beside them
rules.md                   a short team rules block, at most 300 words
mcp/                       a uv project (pyproject.toml, uv.lock) of the team's MCP servers
```

Only a folder, or a preset file named `agent-kit-preset.toml`, is a pack root, for `gh:` and local presets alike; a TOML file of any other name is read alone, as a preset only.

Install or update a teammate's kit with one command:

```sh
python3 bin/agent-setup --preset gh:<org>/<repo> --apply
```

Setup resolves the head commit of the default branch, downloads it with your own `gh` login, and shows what the pack installs (or, on an update, the old and new commit and which skills were added, changed or removed and whether the rules changed) before it writes anything; without `--apply` it only previews. To try a pack before you push it, point at its folder: `python3 bin/agent-setup --preset ./my-pack`. A folder's git commit is recorded when it has no uncommitted changes. A repository holding only the preset installs no pack. A rerun or `doctor` with `gh:` at the commit already installed asks `gh` only for the head commit and downloads nothing. Upgrading: a preset you already use named `agent-kit-preset.toml` (the `gh:<org>/<repo>` default) becomes a pack on its next run if its folder has `skills/` or `rules.md`; one of another name stays a preset only.

- **Skills** install for each selected host exactly like the kit's own skills, and a skill's `hosts:` frontmatter limits it the same way. They install whenever the `skills` component is selected, alongside whichever kit skills you select; the preset's `[skills] exclude` can leave one out. A pack skill named like a kit skill refuses the run; rename the pack's skill. A `{{...}}` in pack text that is not a kit placeholder (a CI or template example) is installed as written. A skill with `extends: debug` in its frontmatter (one name or an inline list) carries company data for that kit skill: setup lists every installed extension, with its description, in the extended skill's installed SKILL.md on every host, and `debug` reads them before its first query (`~/.claude/local/debug/` stays a fallback).
- **MCP servers**: a preset server names the pack's project as `{{PACK_DIR}}/mcp` (`{{PACK_DIR}}` is `<kit root>/pack`), in the shape `command = "uv"`, `args = ["run", "--project", "{{PACK_DIR}}/mcp", "--frozen", "--no-dev", "<script>"]`. Setup copies `mcp/` without `.venv`, `__pycache__`, `.pytest_cache` and `.ruff_cache`, runs `uv sync --frozen --no-dev` on the copy after `--apply`, and writes uv's absolute path as the command, since a host started from the Dock lacks `~/.local/bin` on its `PATH`. While uv is missing, the server is left out and listed under `Skipped`. The `.venv` uv creates in the copy belongs to no install record: it is neither drift nor a collision, and a rerun leaves it.
- **Rules**: `rules.md` is appended to every host's rendered rules (the Claude host rules file, Codex `AGENTS.md`, the Cursor rule), after the kit's rules. A block over 300 words is refused: put the detail in a pack skill and keep the rule as a pointer to it.
- **Precedence**: kit files come from your checkout and are never replaced by a pack; the preset's values sit under your own (`local/kit.env`, flags, earlier answers), as above.
- **Refused packs**: setup scans the pack's files before any write and refuses one that holds a credential file (`.env*` other than `.env.example`, `.envrc`, `*.tfvars`, `.pgpass`, `secrets.yml`, `id_rsa` and the like), a token format the preset check knows (one written as a run of `X` is a placeholder), a private key, Markdown that is not UTF-8, a symlink that leaves the pack or names a folder, two paths that differ only in case or Unicode form, or anything that is not a file or a link. `.git`, `.DS_Store`, `Thumbs.db` and `._*` files, and `.venv`, `__pycache__`, `.pytest_cache` and `.ruff_cache` folders, are not part of a pack.
- **Offline**: a run without `--preset`, or one whose `gh` fetch fails, reinstalls the copy setup keeps under `<kit root>/pack`, so it never drops the pack. If a file of that copy was edited since setup, the run refuses instead: rerun with `--preset` and `--collision backup` to restore it.
- **Rollback and doctor**: `agent-setup rollback <journal>` restores the previous commit's skills and rules. `agent-setup doctor` and `agent-kit doctor` report the installed commit and whether the pack's source has moved on since; `agent-setup doctor` also lists an edited kept file with every other changed path.

### Opinionated defaults

The kit root is `~/.local/share/agent-kit`, the work root `~/agent-work`. Components default to `rules skills`, plus `mcp` when a preset declares MCP servers or Sentry instances (unless you chose the components); hooks are advisory until `--blocking-hooks`, which later updates keep until `--no-blocking-hooks`. Collisions refuse, Codex's sandbox does not get gcloud's credentials (`--codex-gcloud on`), and a finished subagent is resumed only below 300K context (`SUBAGENT_RESUME_MAX` in `local/kit.env`). Hooks keep their small state files under the host's config directory (`~/.claude/state` for Claude). Every choice is saved with the install, so an update that names only some components or flags keeps the rest, including `--mcp-catalog` and a preset's values.

The kit root sits outside shared skill discovery. `AGENT_KIT_DIR` or `--root-dir` chooses another root; the shared renderer accepts the same variable. Only selected skills receive `SKILL.md` files and host links. Paths containing spaces are supported. Repository roots are stored as a JSON array in `CODE_DIRS_JSON`.

## Choose components

| Component | Result | Dependencies and activation |
| --- | --- | --- |
| `rules` | Shared guidance plus host notes, merged into a managed block | Git and Python; Cursor global rules require manual activation |
| `skills` | Links for all bundled skills or just `--skills name ...` | Each selected skill may need its own tools; code-search uses `git`, `rg`, `gh`, and optional Zoekt |
| `roles` | Configurable model and effort per role; native Claude/Codex files and CLI review routing | Provider CLI/login required when a role runs; Cursor uses CLI roles |
| `hooks` | Selected host events and shared logic | `jq`, provider CLI and a supported local runtime; manual host trust |
| `mcp` | Token-free server descriptors from the empty default or `--mcp-catalog file.json` | Configure each server's runtime dependency; use native OAuth or a secret-store wrapper |
| `commands` | Optional Claude workflow commands under the selected host directory | `gh` and provider login when invoked; Jira actions need an authorized connector |
| `data-wrappers` | Read-only `ro-mysql` and `bqro` commands | `mysql-client` (Homebrew, or the distribution package at `/usr/bin/mysql` on Linux) and SSH for `ro-mysql`, with passwords in the macOS Keychain, `secret-tool` on Linux, else (and when a store has no item) `RO_MYSQL_PASSWORD_<account>` environment variables, the account with letters and digits kept and every other byte as `_XX` hex (the summary says which store); `bq` and native Google auth for `bqro`; no connections during setup |

For Codex, `hooks` or `mcp` also names the work root in `[sandbox_workspace_write]` writable roots (its own managed block in `config.toml`) and creates it; an existing table of your own is kept and setup prints the roots to add. `--codex-gcloud on` (or the guided prompt) adds gcloud's config directory, which holds credentials, so `bq` works inside the sandbox; it is off by default, and setup's choice overrides `CODEX_SANDBOX_GCLOUD` in `local/kit.env`.

Rules and skills are the default. Hooks start with advisory events. To enable blocking safety and review gates, explicitly choose `--components hooks --blocking-hooks`; later updates keep that choice until `--no-blocking-hooks`. That selection also installs the Git dispatcher environment into the model's shell. Setup never adjusts the host's permission policy.

The Git layer (`git-hooks/`: the agent push gate and commit trailer check) reaches agent shells only. Setup puts a command-scope `core.hooksPath` into the host's environment as `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_n` and `GIT_CONFIG_VALUE_n`: Claude's `settings.json` `env`, Codex's `shell_environment_policy.set`, and the environment Cursor's `sessionStart` hook returns. Your own terminal never sees it, and setup never writes a global `core.hooksPath`. The dispatcher still runs each repository's own hooks: `.git/hooks`, or the `core.hooksPath` the repository would use without the layer (husky's `.husky/_`, a global or a local value).

- Your own `GIT_CONFIG_*` entries in Claude's `env` are kept: the layer's entry goes after them, and `--no-blocking-hooks` or a rollback leaves them as they were. Cursor's hook does the same with the entries its environment carries.
- Codex: setup never edits your lines, so a `shell_environment_policy.set` with its own `GIT_CONFIG_COUNT` is refused, with the two keys to add and the new count. Once your table carries the layer's entry, setup leaves your `GIT_CONFIG_*` keys alone.
- A `GIT_CONFIG_COUNT` exported by the shell that launches Claude or Codex is replaced by the host's value. Move such entries into the host configuration.

Codex command hooks require a local runtime exposing `hooks` in `codex features list`. Cloud command hooks are refused. Review the installed hook commands through `/hooks` and trust their current hash yourself. Cursor hook support must be checked in the installed app and confirmed with `--confirm-hook-support cursor`; the guided setup asks this question.

Cursor project rules use `--project-root /path/to/project` and are written to that project's `.cursor/rules/`. Without this flag, setup exports the rule file under the Cursor configuration directory; activate it through **Customize → Rules**. The doctor reports configuration and file drift, not proof that a model loaded the rules.

## Configure paths and models

```sh
python3 bin/agent-setup \
  --hosts codex --components roles rules \
  --root-dir "$HOME/Agent tools" \
  --host-root "$HOME/Codex configuration" \
  --codex-bin /path/to/codex \
  --role-model cross-reviewer=openai:your-model \
  --role-effort cross-reviewer=high
```

Use `--claude-bin`, `--codex-bin` and `--cursor-bin` for alternate CLI paths. `--host-root` requires one host. Model values use `anthropic:model` or `openai:model`; the renderer validates the provider's effort scale. Omitted model, path and provider choices are inherited on later component upgrades. An update that selects some components or hosts leaves what the others installed in place. Installed `KIT/bin/agent-setup --root-dir KIT` previews from the source checkout it recorded, at that checkout's current commit; to move to a newer kit, follow [Update](#update). A directly invoked checkout uses itself; explicit `--source` wins. If the origin moved or an older install lacks provenance, supply `--source /path/to/checkout`. Doctor and rollback use installed state and work without the origin. Use `--skills all` to return to every bundled skill after saving a subset; `--skills` with no values selects none. Interactive setup accepts `all` and `none` too. Edited deselected skills stay in place and block retirement by default. After reviewing the preview, `--collision backup --apply` saves those edits in the journal before retiring the skill; rolling that journal back restores them. Unchanged Cursor rules from earlier setup versions upgrade from managed text blocks to full-file ownership, with rollback preserving the original format.

When selecting `data-wrappers`, setup prints a shell-quoted `shell_activation` command that adds the chosen root’s `bin` directory to `PATH`. Run that command once in the terminal, or invoke the printed executable paths directly. Restart an app that captured an older `PATH`. Setup leaves shell startup files to you. `ro-mysql` reads passwords from the macOS Keychain, `secret-tool` on Linux, else environment variables, and runs only a client at `/opt/homebrew/opt/mysql-client/bin/mysql`, `/usr/local/opt/mysql-client/bin/mysql` or `/usr/bin/mysql`; configure SSH transport and credential access locally before running a query. `ro-mysql add --name <svc> --user <db user> --via <bastion alias> --remote <host:port> --local-port <N> [--staging]`, run in your own terminal, adds one. It refuses an alias or local port your `~/.ssh/config` or a file it Includes already has. It builds the `Host db-tunnel-<svc>` block from the `# ro-mysql: user=` line and the bastion's full `ssh -G` output (its HostName and every key that differs from what an unnamed host gets), except the keys a tunnel sets itself or must not share: the forwards, ExitOnForwardFailure, the ServerAlive keys, ControlMaster, ControlPath, ControlPersist and GatewayPorts. Every block also gets `ControlMaster no`, `ControlPath none` and `GatewayPorts no` (a tunnel never rides the bastion's multiplexing socket), a reset line for a key a wildcard block would add but the bastion lacks (`ProxyJump none`, `ProxyCommand none`, `IdentityAgent SSH_AUTH_SOCK`), then LocalForward, ExitOnForwardFailure and ServerAlive. It goes above the first wildcard `Host` or `Match`, so its first values win over a `Host *` default. It writes the new config to a temporary file and checks with `ssh -G` that the alias resolves exactly like the bastion (those keys aside) with the one new forward; a key a wildcard block still shadows gets one more line, for up to three rounds, and any difference left is named and nothing is written. Only then does it prompt for the password. Under a lock beside the config (released on SIGTERM and SIGHUP too) it re-reads the config and its Includes, builds and checks again if they changed during the prompt, backs the current file up (`config.bak-<time>`), replaces it in one rename and stores the password in the Keychain, never in argv or on the screen; cancelling the prompt writes nothing. `--dry-run` prints the block and every warning, writes nothing and exits 0. A missing-or-denied credential diagnostic calls for retrying from a trusted terminal or approved wrapper before considering rotation.

Host selection chooses configuration outputs. Each role independently chooses its provider, model and effort; preview shows that routing, provider CLI availability and fallback; preflight and `doctor` check each host login. The default review roles include both Anthropic and OpenAI providers. For a Claude-only review setup, override the OpenAI review role:

```sh
python3 bin/agent-setup --hosts claude --components roles --role-model cross-reviewer=anthropic:inherit
```

For a Codex-only review setup, choose an OpenAI model for all Anthropic roles:

```sh
python3 bin/agent-setup --hosts codex --components roles \
  --role-model main=openai:your-model \
  --role-model engineer=openai:your-model \
  --role-model researcher=openai:your-model \
  --role-model task-reviewer=openai:your-model \
  --role-model second-opinion=openai:your-model \
  --role-model bug-reviewer=openai:your-model \
  --role-model quality-reviewer=openai:your-model
```

Keep review prefixes and rounds; existing high effort is valid for both providers. Codex named role activation remains unverified. Roles without a review prefix, such as engineer/researcher/second-opinion, are unavailable through `agent-run` when native invocation is disabled; preview labels those routes. Setup does not enable native role support automatically. Role overrides route review CLIs and supported native agents. The active main-session model for Codex and Cursor stays in that provider's own settings; the `main` catalog entry describes inherited review routing.

Keep user workflow values in `local/kit.env`; generated path choices live in `local/setup-paths.env`, and a team preset's values in `local/preset.env`. The shared parser loads the preset layer first, then the user layer, then the generated path layer, so each later one wins. Setup never reads or copies the user's values into its journal. `kit.env.example` describes optional, non-secret settings. Your own Claude Code settings (permissions, a status line, env) go in `local/settings.json`, merged over `hosts/claude/settings.base.json`; setup with the hooks component and `agent-kit render` both apply it. Setup owns only the top-level keys the layer gives it: a key you add in the host's `settings.json` stays, a key you drop from the layer goes on the next run, and a layer key that would replace a value of your own is a collision. The layer refuses, and setup exits 2 naming your `local/settings.json`, when it sets a key the host UI owns (`theme`, `model`: `hosts/claude/host.json` `uiOwned`), a `hooks` key (hooks come from `hooks/registry.json`), or, with the roles component, `model`, `effortLevel` or `modelSettings` (they come from `roles.toml` `[roles.main]`); a dev install's `agent-setup doctor` lists the same refusal as a problem, and `agent-kit render` exits on it. There is no default commit author override; Git's existing identity is used.

## Search code

The bundled code-search skill refreshes eligible local clones under the configured repository roots before searching them. It only pulls clean, attached branches with an upstream and zero unpushed commits, using `pull --ff-only --no-rebase --no-autostash`. Dirty, ahead, detached, diverged or failed clones are skipped and reported as coverage gaps. It checks incoming paths against existing ignored files and symlink parents before pulling the exact fetched tip. A collision skips that refresh and reports the gap; unrelated ignored caches remain compatible. It never stashes or resets user work.

Missing coverage goes to the user's authenticated `gh` CLI, scoped to `--repo owner/name` or the configured `CODE_SEARCH_GH_OWNER`. Zoekt is optional and only used for known repositories without GitHub access. Chrome and its optional SSO helper are unnecessary for local and GitHub searches.

## Update

```sh
python3 ~/.local/share/agent-kit/bin/agent-setup update
```

`update` fast-forwards the checkout you installed from to its upstream, then reapplies every saved choice with the new commit's own installer (`agent-setup sync`), and prints the old and new kit and pack commits.

- The saved preset is read again from its source: a `gh:` pack at its new head commit.
- A `--confirm-hook-support` you gave once is kept; give it again to replace it, or `--confirm-hook-support none` to clear it.
- `--source <checkout>` updates and installs from another checkout, which the install then records. `update` also takes `--dev`/`--no-dev` and `--collision`; it refuses any other setup flag, since rerunning setup with `--apply` changes a saved choice.
- It refuses a checkout with uncommitted changes to tracked files, a detached HEAD, no upstream branch or commits its upstream lacks. Untracked files are left alone; git itself refuses a fast-forward that would overwrite one. It never stashes, resets or merges your work.
- Before the checkout moves, the upstream commit's installer previews the reapply from an export of that commit. When the preview fails or blocks, nothing changes.
- The update is not atomic: if the reapply fails after the fast-forward, the checkout stays on the new commit and `update` prints the `git reset --keep` that moves it back. A dev install already runs the new commit's linked files.
- The reapply is an ordinary install: `rollback` with its journal undoes it.

An install made before `update` existed has no such action: fast-forward its checkout once yourself, then reinstall from it.

```sh
git -C <checkout> pull --ff-only && python3 <checkout>/bin/agent-setup --source <checkout> --apply
```

## Inspect and roll back

```sh
python3 bin/agent-setup doctor --root-dir "$HOME/.local/share/agent-kit"
python3 bin/agent-setup rollback JOURNAL_ID --root-dir "$HOME/.local/share/agent-kit"
```

Setup prints the journal ID after apply. If a process stops mid-install, the next preview refuses and names the pending journal; run its printed rollback command before trying again. Rollback restores only that installation's changes and preserves later user edits by stopping on drift. Ordinary collision backups are moved locally, and managed configuration journals record only kit-owned entries. Authentication files are excluded throughout.

To see the whole setup at once, run `agent-kit dashboard` from the kit root. It writes one self-contained, light-mode HTML page to `<work root>/dashboard/index.html` (`--out` to choose another path, `--no-open` to skip opening it). It opens on Needs attention: drift per host with the command that fixes it, missing Keychain items with the command to add each, and the sources the kit does not own, each linking to its row. The sidebar switches between the other sections, one at a time (without JavaScript the page is one long scroll): hosts and their doctor drift, MCP servers from the installed catalog, unmanaged sources, overlay keys, the data wrappers, SQL instances, skills per host, roles, hooks, the preset and pack, and the work root. Each row shows what it is as a muted line under its name: the `description` of a catalog server, registry row or preset, an agent's or skill's frontmatter, the comment above a key in `kit.env.example`, a wrapper's docstring, a tunnel's remote and bastion. SQL instances lists every `db-tunnel-*` forward from `ro-mysql`'s own ssh config reader (Includes too) and its `--refresh` cache (never `ro-mysql --tunnels`): port, kind, DB user, Keychain item, cached databases and last state, plus an add-connection form that runs in the page and checks with `ro-mysql`'s own patterns: paste `mysql://user@host:3306/db?via=<bastion>&local_port=N&staging=1` (a password in it is removed before anything else reads it, never shown) or fill the fields, and copy one `ro-mysql add` command, or do it by hand: `ro-mysql add ... --dry-run` prints the block, checked with `ssh -G` against the bastion's full resolved config, and warns of anything that still differs, then the Keychain prompt and the check; a local port a known tunnel holds is flagged. `agent-kit mcp describe [--only NAME ...] [--timeout S]`, which you run yourself, starts each catalog server once (stdio servers, and http servers with static headers; an OAuth server shows "needs sign-in", and a redirect is not followed), runs initialize, notifications/initialized and tools/list within one deadline per server, and caches only the server's name and version, an excerpt of its instructions and its tool names in `<kit>/state/mcp-describe.json`, with every resolved credential (a Keychain value, a header value, the token after its auth scheme) masked out of them and a failure kept as its kind only. It connects directly and ignores `HTTPS_PROXY`, so a server reachable only through a required proxy shows "not reached". The dashboard shows the tool count and uses the instructions when a server has no `description`. The dashboard itself is read-only and starts only `security find-generic-password` (presence, never the value), `git`, `claude-account dirs` and the page opener. Catalog arguments, URLs and headers are masked by the rule below, overlay values show only for the keys `kit.env.example` documents, every change is a command to copy, and the page loads nothing from the network. `--check-updates` asks a `gh:` preset's source for a newer commit.

The work root keeps one folder per work item, with disposable files in its `tmp/`. `agent-task close <project>/<item>` marks an item closed and offers each script left in its `tmp/` that the project's `scripts/` lacks for promotion, with a `  - use:` line in the project's `INDEX.md` (`--promote <file>` or `--no-promote` without a prompt). `claude-gc` writes a report of what it would sweep from the `tmp/` of items closed or merged at least `TMP_SWEEP_DAYS` (default 14) days ago: checkouts with nothing unpushed, scratch homes, virtualenvs, `node_modules` and caches, and files over 1 MB, to delete; source and data files up to 1 MB (`.py .sh .sql .ipynb .md .csv .json .txt`), to move into the item's `out/salvage/`. `claude-gc --sweep-tmp --report-sha256 <sha256>` applies exactly that reviewed report and nothing outside those `tmp/` folders. A new work root gets `.vscode/settings.json` and `.cursorignore` that keep editors out of `tmp/`, virtualenvs and `worktrees/`, unless you already have your own.

## Claude plugin compatibility

The original plugin names remain available through the marketplace: `guard-rails`, `prod-data`, `ci-babysitter`, `auto-review`, `session-context`, `python-hygiene`, `terminal-signals`, `skills-core`, `workflow`, `docs` and `qa-e2e`. These packages are generated from the same canonical sources. Review now verifies findings directly and records the fixer's TALLY; there is no separate critic role.

```sh
claude plugin marketplace add mshoaibmanz/agent-kit
claude plugin install guard-rails@agent-kit
```

The packages are not committed to `main`. Once the `ci` workflow has passed on a push to `main`, the `dist` workflow runs `scripts/build.sh` on that commit and commits the generated `plugins/` tree to the `dist` branch as a new commit (`scripts/publish_dist.sh`); a red CI publishes nothing. Each marketplace entry installs `plugins/<name>` from `dist`; `scripts/build_plugins.py --marketplace` writes those sources into `.claude-plugin/marketplace.json` (add a plugin there with its name and description), and the release check fails when one differs. `claude plugin marketplace update agent-kit` then `claude plugin update <name>@agent-kit` picks up a newer build, including an upgrade from an install made before the packages moved to `dist`. To try a package from a checkout, run `scripts/build.sh` (it writes the ignored `plugins/` folder) and point `claude --plugin-dir` at `plugins/<name>`. If you load packages that way, run `scripts/build.sh` again after every pull: `plugins/` is no longer tracked, so a pull leaves your copy stale.

Use the setup command for host paths, model choices and Git shell activation. Avoid installing the same hook component through both setup and a plugin. Plugin hook packages activate their listed events when installed; review their manifests before choosing them.

## Develop

### Develop on the kit

```sh
python3 bin/agent-setup --dev --hosts claude codex --apply          # from your checkout
python3 ~/.local/share/agent-kit/bin/agent-kit sync [--dry-run]   # after editing settings, MCP, hooks, roles or rules
```

A dev install links each kit file (`bin/`, `hooks/`, `skills/`, `agents/`, `rules/` and the rest) into your checkout instead of copying it, so an edit there is live on the next tool call.

- What setup derives stays a copy: a skill text with the kit path filled in, the selected MCP catalog and hook rows, roles with your overrides, your overlay, and the team pack, pinned by commit (`--preset ./my-pack` reapplies a local pack folder).
- `bin/agent-setup`, `bin/lib/` and the hook libraries setup imports (`hooks/lib/hook-io`, `kit_env.py`, `host.py` and `session.py`) stay copies too, so `rollback` and `--no-dev` run from the install while the checkout is mid-rebase or broken. An edit to them reaches `agent-setup` and the bash hooks, which load the install's `hook-io`, after `agent-kit sync`. The Python tools and hooks follow their links and load the checkout's `kit_env.py` at once.
- A syntax error in a linked bash hook blocks tool calls at once, in every session that runs the hook.
- Host configs (Claude `settings.json` and `mcp.json`, Codex `config.toml` and `AGENTS.md`, Cursor's rules and hooks) are rendered as before, so a rules edit reaches Claude at once and Codex and Cursor after a sync. `agent-kit sync` runs the checkout's `agent-setup sync`: it re-renders them from the current sources with your saved choices, lists what changed, and writes nothing (no journal) when nothing did. A new file in the checkout needs it to be linked. `--dry-run` only lists, and fails when a row blocks.
- Later runs keep dev mode until `--no-dev`, which installs copies again; rolling back the dev install's journal restores the copies too.
- `agent-setup doctor` counts uncommitted changes in the checkout (`dev.dirty`) without calling them a problem. It reports a missing checkout, a link whose file left it, a link changed to point elsewhere, and changes waiting for `agent-kit sync`.

### Test

Set `TMPDIR` explicitly to an existing fixture directory in your bound task or project, outside this source checkout. The release runner refuses an unset path or a directory inside the checkout. It creates disposable homes and runs the installer, package, installed guard and GC portability suites plus hook checks. Running `hooks/tests/run-all.sh` directly requires an explicit disposable `HOME`.

`bash tests/run-tests.sh` runs the suites concurrently, one per CPU (`--jobs N` changes that). Each suite, and each file in `hooks/tests`, gets its own `HOME`, `TMPDIR` and `AGENT_WORK_ROOT` under `$TMPDIR/runs/<run>/` and its own log in that run's `logs/`. The run ends with a table of suite, seconds and result, prints the tail of each failed log, and exits non-zero when any suite failed. `tests/verify_release.py` runs first and alone.

- `--serial` runs one suite at a time with its output streamed, and stops at the first failure.
- `--only <suite>` runs a subset: a path, file name or stem, with globs, repeated or comma-separated (`--only setup_test,'hooks/tests/*'`). `--list` prints the names.
- The runner, and `tests/run_hooks.py` run on its own, hold one machine-wide lock, `/tmp/agent-kit-suite.lock` (`AGENT_KIT_SUITE_LOCK` moves it), and wait while a run from any checkout holds it. Concurrent agents queue on the lock and need no `pgrep` loop; the kernel releases it when its holder exits.

Edit canonical `bin/`, `hooks/`, `agents/`, `commands/`, `rules/`, `skills/` and `roles.toml`. Generate compatibility packages with `python3 scripts/build_plugins.py` (into the ignored `plugins/`, or `--out <dir>`); `--check` compares a fresh build with that folder. Generated plugin files are release artifacts on the `dist` branch, never on `main`; `tests/verify_release.py` builds them into a scratch folder and validates that build.

```sh
# Choose an existing fixture directory in your bound task or project, outside this checkout.
export TMPDIR="/path/to/your/project/data/fixtures"
python3 tests/setup_test.py
bash tests/run-tests.sh
python3 tests/verify_release.py
bash scripts/scan.sh
```

The repo is public. A pre-push hook runs the history steps of the CI leak scan (`scripts/scan.sh`, which needs `gitleaks`) over what each push adds: its commits (messages, authors, added lines) and annotated tags. The tree is CI's; `bash scripts/scan.sh` with no arguments checks the whole repo.

- Enable it once per clone, from the main checkout: `ln -sf ../../scripts/pre-push .git/hooks/pre-push`. git runs that link from every worktree, and the shim behind it runs the pushing worktree's own hook, so worktrees and branches from before `.githooks/` existed stay covered. A clone that already has the link needs nothing more; it keeps the old `LEAK_TERMS_FILE` setting.
- `git config core.hooksPath .githooks` suits only a fresh clone with no older branches or worktrees: the relative path resolves in each worktree, and git silently skips the hook in one that has no `.githooks/pre-push`.
- The denylist is a file outside the repo, never committed. Point `AGENT_KIT_LEAK_TERMS` at it, or keep it at `<kit root>/local/leak-terms.txt`. The kit root is `$AGENT_KIT_DIR`, default `~/.local/share/agent-kit`; the old default, `~/.config/claude-kit/leak-terms.txt`, is read last. Sources and format: `scripts/leak-check.sh`. With no list the hook refuses the push; `AGENT_KIT_LEAK_TERMS=none git push` pushes without the denylist check, which CI still runs with its `LEAK_TERMS` secret.
- Reading a hit: `term #N` is line N of your list (`sed -n Np <list>`); `commit <sha>` and `tag <sha>` are objects to `git show`; `file #K` and `file name #K` are line K of `git ls-files --cached --others --exclude-standard`. The text itself is never printed.
- After a hit, fix the unpushed commits (amend, or rebase to reword), then push again. `git push --no-verify` only moves the failure to CI, after the commits are public.
- `.scan-history-allow` lists published commits whose hits are accepted, because history is not rewritten. CI reads it from the commit it checks against (the PR's base, or the `main` a push replaces), and an entry counts only for a commit already in that commit's history, so a change can never exempt its own commits. The hook never reads it.

Hook suites require a disposable home with `HOOKS_DIR` pointing at this checkout and `KIT_ENV=/dev/null`, plus explicit wrapper paths. The release checks include fake-home installs, rollback, unsupported capabilities, collision refusal, secret-catalog refusal, generated-package drift and redacted secret scanning. Private source history and user overlays are excluded from exports.

MCP catalogs may describe command transports with `type: stdio` or HTTP URL transports with `type: http`, plus a one-line `description` (every entry the kit ships has one, and so does every `hooks/registry.json` row; a test fails one without it). The installed catalog keeps a server's description for the dashboard; setup and `agent-kit render` remove it (and a registry row's) only from what the hosts get, validate transport agreement, and render the transport type where the host requires it. Mixed transports, unknown types and malformed arguments are refused before changing configurations. Keep credentials in native OAuth or a local credential-store wrapper for authenticated services. A server may declare the Keychain items its wrapper reads, names only: `"credentials": [{"service": "<service>", "account": "<account>"}]`. The render drops the field; `agent-kit dashboard` checks each item's presence.

Two rules decide what counts as a credential in a catalog (`bin/lib/credentials.py`), and detection is not exhaustive:

- **Setup refuses** only the shapes that are almost always a secret: any value but a `${VAR}` reference after, or attached with `=` to, a flag or `NAME=` that names the secret itself (`--password`, `--token`, `--api-key`, `--client-secret=…`, `DB_PASSWORD=…`, `GITHUB_TOKEN=…` and their kin; a number or a path counts as a literal there, except a key-file path after `--key`); a header named like a credential, or with a key-shaped value (`Authorization:${AUTH_HEADER}` and `Accept: application/json` are fine), or a non-header after `--header`/`-H`; the common token formats (JWT, bearer, vendor prefixes, private keys); URL userinfo; and a URL query key named like a credential. `--max-tokens 4096`, `--token-limit=4k`, `--session-timeout 600`, `--oauth=true`, `--auth=none`, `--sort-key=name`, `USE_OAUTH=true`, `--credentials /path`, `--no-auth URL`, `--authority URL` and UUID or slug URL segments pass.
- **The dashboard masks** by default: it shows a value only when it is a flag, a path (`/`, `~`, `.` or a `{{PLACEHOLDER}}`), a `${VAR}` reference, a number, a URL (`scheme://host/path`, query values and key-shaped path segments masked), a package spec (`[@scope/]name[@version]`), a `host:port` or a short lowercase word. Everything else is masked, including JSON blobs, mixed-case words, a header value that is not a `${VAR}` reference, and any value after a credential-named flag or `-p` that is not a reference (after one that names the secret itself, only a `${VAR}` reference shows).
