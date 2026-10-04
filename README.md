# agent-kit

Shared working rules, review roles, hooks and selected skills for Claude Code, Codex and Cursor. Keep one source checkout and install the parts each person wants. Company policy, repository paths and credentials stay in each person's local setup.

Python 3.11 or newer and Git are required. Hooks also need `jq` and the selected provider CLI. Provider logins are reused in place: setup never opens or copies authentication files, changes permissions, or approves hook trust.

## Setup

Clone this repository to a directory you choose, then run the guided preview:

```sh
python3 bin/agent-setup --interactive
```

For a scripted setup, start with rules and a few skills:

```sh
python3 bin/agent-setup \
  --hosts claude \
  --components rules skills \
  --skills conventions unslop code-search \
  --root-dir "$HOME/.local/share/agent-kit" \
  --repo-roots "$HOME/Code" "$HOME/Work projects" \
  --github-owner your-org \
  --work-root "$HOME/agent-work"
```

The guided setup asks for kit, repository, work and host paths, GitHub owner, CLI paths and selected role choices. Repository paths use a JSON array so spaces stay intact. Fresh scripted setup has no local repository roots; supply `--repo-roots` to search local clones, or pass that flag with no values to clear saved roots and use scoped remote search. This prints the component selection, dependencies and destination paths. Add `--apply` to install the previewed selection. Existing unrelated skills, rules, hooks and MCP entries are preserved. A conflicting file stops the entire preflight; choose a different destination or explicitly use `--collision backup` to move that file into the local rollback journal.

The default source root is `~/.local/share/agent-kit`, outside shared skill discovery. `AGENT_KIT_DIR` or `--root-dir` chooses another root; the shared renderer accepts the same variable. Only selected skills receive `SKILL.md` files and host links. Paths containing spaces are supported. Repository roots are stored as a JSON array in `CODE_DIRS_JSON`.

## Choose components

| Component | Result | Dependencies and activation |
| --- | --- | --- |
| `rules` | Shared guidance plus host notes, merged into a managed block | Git and Python; Cursor global rules require manual activation |
| `skills` | Links for all bundled skills or just `--skills name ...` | Each selected skill may need its own tools; code-search uses `git`, `rg`, `gh`, and optional Zoekt |
| `roles` | Configurable model and effort per role; native Claude/Codex files and CLI review routing | Provider CLI/login required when a role runs; Cursor uses CLI roles |
| `hooks` | Selected host events and shared logic | `jq`, provider CLI and a supported local runtime; manual host trust |
| `mcp` | Token-free server descriptors from the empty default or `--mcp-catalog file.json` | Configure each server's runtime dependency; use native OAuth or a secret-store wrapper |
| `commands` | Optional Claude workflow commands under the selected host directory | `gh` and provider login when invoked; Jira actions need an authorized connector |
| `data-wrappers` | Read-only `ro-mysql` and `bqro` commands | macOS/Homebrew `mysql-client` and SSH/Keychain for `ro-mysql`; `bq` and native Google auth for `bqro`; no connections during setup |

Rules and skills are the default. Hooks start with advisory events. To enable blocking safety and review gates, explicitly choose `--components hooks --blocking-hooks`. That selection also installs the Git dispatcher environment into the model's shell. Setup never adjusts the host's permission policy.

Codex command hooks require a local runtime exposing `hooks` in `codex features list`. Cloud command hooks are refused. Review the installed hook commands through `/hooks` and trust their current hash yourself. Cursor hook support must be checked in the installed app and confirmed with `--confirm-hook-support cursor`.

Cursor project rules use `--project-root /path/to/project` and are written to that project's `.cursor/rules/`. Without this flag, setup exports the rule file under the Cursor configuration directory; activate it through **Customize → Rules**. The doctor reports configuration and file drift, not proof that a model loaded the rules.

## Configure paths and models

```sh
python3 bin/agent-setup \
  --hosts codex --components roles rules \
  --root-dir "$HOME/Agent tools" \
  --host-root "$HOME/Codex configuration" \
  --codex-bin /path/to/codex \
  --role-model review-cross=openai:your-model \
  --role-effort review-cross=high
```

Use `--claude-bin`, `--codex-bin` and `--cursor-bin` for alternate CLI paths. `--host-root` requires one host. Model values use `anthropic:model` or `openai:model`; the renderer validates the provider's effort scale. Omitted model, path and provider choices are inherited on later component upgrades. Installed `KIT/bin/agent-setup --root-dir KIT` previews from its recorded original source checkout. A directly invoked newer checkout uses itself; explicit `--source` wins. If the origin moved or an older install lacks provenance, supply `--source /path/to/checkout`. Doctor and rollback use installed state and work without the origin. Use `--skills all` to return to every bundled skill after saving a subset; `--skills` with no values selects none. Interactive setup accepts `all` and `none` too. Edited deselected skills stay in place and block retirement by default. After reviewing the preview, `--collision backup --apply` saves those edits in the journal before retiring the skill; rolling that journal back restores them. Unchanged Cursor rules from earlier setup versions upgrade from managed text blocks to full-file ownership, with rollback preserving the original format.

When selecting `data-wrappers`, setup prints a shell-quoted `shell_activation` command that adds the chosen root’s `bin` directory to `PATH`. Run that command once in the terminal, or invoke the printed executable paths directly. Restart an app that captured an older `PATH`. Setup leaves shell startup files to you. `ro-mysql` currently uses macOS Keychain and a Homebrew `mysql-client` installation at `/opt/homebrew/opt/mysql-client/bin/mysql` or `/usr/local/opt/mysql-client/bin/mysql`; configure SSH transport and credential access locally before running a query. A missing-or-denied credential diagnostic calls for retrying from a trusted terminal or approved wrapper before considering rotation.

Host selection chooses configuration outputs. Each role independently chooses its provider, model and effort; preview shows that routing, provider CLI availability and fallback. Login status is not checked. The default review roles include both Anthropic and OpenAI providers. For a Claude-only review setup, override the OpenAI review role:

```sh
python3 bin/agent-setup --hosts claude --components roles --role-model review-cross=anthropic:inherit
```

For a Codex-only review setup, choose an OpenAI model for all Anthropic roles:

```sh
python3 bin/agent-setup --hosts codex --components roles \
  --role-model main=openai:your-model \
  --role-model worker=openai:your-model \
  --role-model scout=openai:your-model \
  --role-model reviewer=openai:your-model \
  --role-model adversary=openai:your-model \
  --role-model thermo-bugs=openai:your-model \
  --role-model thermo-quality=openai:your-model
```

Keep review prefixes and rounds; existing high effort is valid for both providers. Codex named role activation remains unverified. Roles without a review prefix, such as worker/scout/adversary, are unavailable through `agent-run` when native invocation is disabled; preview labels those routes. Setup does not enable native role support automatically. Role overrides route review CLIs and supported native agents. The active main-session model for Codex and Cursor stays in that provider's own settings; the `main` catalog entry describes inherited review routing.

Keep user workflow values in `local/kit.env`; generated path choices live in `local/setup-paths.env`. The shared parser loads the user layer first, then the generated path layer. Setup never reads or copies the user's values into its journal. `kit.env.example` describes optional, non-secret settings. There is no default commit author override; Git's existing identity is used.

## Search code

The bundled code-search skill refreshes eligible local clones under the configured repository roots before searching them. It only pulls clean, attached branches with an upstream and zero unpushed commits, using `pull --ff-only --no-rebase --no-autostash`. Dirty, ahead, detached, diverged or failed clones are skipped and reported as coverage gaps. It checks incoming paths against existing ignored files and symlink parents before pulling the exact fetched tip. A collision skips that refresh and reports the gap; unrelated ignored caches remain compatible. It never stashes or resets user work.

Missing coverage goes to the user's authenticated `gh` CLI, scoped to `--repo owner/name` or the configured `CODE_SEARCH_GH_OWNER`. Zoekt is optional and only used for known repositories without GitHub access. Chrome and its optional SSO helper are unnecessary for local and GitHub searches.

## Inspect and roll back

```sh
python3 bin/agent-setup doctor --root-dir "$HOME/.local/share/agent-kit"
python3 bin/agent-setup rollback JOURNAL_ID --root-dir "$HOME/.local/share/agent-kit"
```

Setup prints the journal ID after apply. If a process stops mid-install, the next preview refuses and names the pending journal; run its printed rollback command before trying again. Rollback restores only that installation's changes and preserves later user edits by stopping on drift. Ordinary collision backups are moved locally, and managed configuration journals record only kit-owned entries. Authentication files are excluded throughout.

## Claude plugin compatibility

The original plugin names remain available through the marketplace: `guard-rails`, `prod-data`, `ci-babysitter`, `auto-review`, `session-context`, `python-hygiene`, `terminal-signals`, `skills-core`, `workflow`, `docs` and `qa-e2e`. These packages are generated from the same canonical sources. Review now verifies findings directly and records the fixer's TALLY; there is no separate critic role.

Use the setup command for host paths, model choices and Git shell activation. Avoid installing the same hook component through both setup and a plugin. Plugin hook packages activate their listed events when installed; review their manifests before choosing them.

## Develop

Set `TMPDIR` explicitly to an existing fixture directory in your bound task or project, outside this source checkout. The release runner refuses an unset path or a directory inside the checkout. It creates disposable homes and runs the installer, package, installed guard and GC portability suites plus hook checks. Running `hooks/tests/run-all.sh` directly requires an explicit disposable `HOME`.

Edit canonical `bin/`, `hooks/`, `agents/`, `commands/`, `rules/`, `skills/` and `roles.toml`. Generate compatibility packages with `python3 scripts/build_plugins.py`; verify them with `--check`. Generated plugin files are release artifacts.

```sh
# Choose an existing fixture directory in your bound task or project, outside this checkout.
export TMPDIR="/path/to/your/project/data/fixtures"
python3 tests/setup_test.py
bash tests/run-tests.sh
python3 scripts/build_plugins.py --check
bash scripts/scan.sh
```

Hook suites require a disposable home with `HOOKS_DIR` pointing at this checkout and `KIT_ENV=/dev/null`, plus explicit wrapper paths. The release checks include fake-home installs, rollback, unsupported capabilities, collision refusal, secret-catalog refusal, generated-package drift and redacted secret scanning. Private source history and user overlays are excluded from exports.

MCP catalogs may describe command transports with `type: stdio` or HTTP URL transports with `type: http`, plus a string `description`. Setup validates transport agreement, removes the description before host rendering, and renders the transport type where the host requires it. Mixed transports, unknown types and malformed arguments are refused before changing configurations. Common inline credential patterns are also refused; detection is not exhaustive. Keep credentials in native OAuth or a local credential-store wrapper for authenticated services.
