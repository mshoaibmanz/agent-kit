# claude-kit

A Claude Code setup split into pick-and-choose plugins: Bash guard rails, a tiered review loop,
a push-to-green CI watcher, read-only production data access, ticket-to-PR workflow commands, and
a set of skills. Install only what you want. Nothing here edits your `settings.json`, your
`CLAUDE.md` or any file you own: plugins are additive and namespaced, and uninstalling one removes
what it added.

Everything company-specific (hosts, schemas, test runners, Jira ids, screenshots) lives outside
this repo in a per-user overlay under `~/.claude/local/`. With no overlay the hooks stay inert or
refuse safely; see [Overlay config](#overlay-config).

## Install

```bash
claude plugin marketplace add mshoaibmanz/claude-kit     # or a local clone: /path/to/claude-kit
claude plugin install guard-rails@claude-kit             # then any others from the matrix
```

Or inside a session: `/plugin marketplace add mshoaibmanz/claude-kit`, then `/plugin` to browse,
install and disable. To try a plugin without installing it: `claude --plugin-dir ./plugins/<name>`.
To see what one adds before enabling it: `claude --plugin-dir ./plugins/<name> plugin details <name>`.

Suggested order: `skills-core` and `terminal-signals` (no risk), then `session-context`, then
`guard-rails`, then the ones that can block (`auto-review`, `ci-babysitter`, `prod-data`).

## What each plugin does

Read **Can block?** before installing. "No" means the plugin cannot stop anything you do today.

| Plugin | Adds | Fires on | Can block? | Needs |
|---|---|---|---|---|
| **skills-core** | 10 skills: session-review, grilling, grill-with-docs, domain-modeling, pr-study, prototype, handoff, sentry-fix-issues, unslop, typescript-best-practices | only when invoked | No | Sentry MCP for sentry-fix-issues |
| **terminal-signals** | Tab title per task, busy/idle/attention glyphs, macOS chime and banner | prompt, stop, notification, session start | No, cosmetic | macOS, VS Code or iTerm2 |
| **session-context** | Per-repo pointers to your `~/.claude/local` docs, the invariants TL;DR, CONTEXT-MAP navigation, the test-runner and prod-data lines, the branch's handoff; a session digest at SessionEnd; `claude-gc` | session start and end | No, context only | — |
| **python-hygiene** | `ruff` on every edited `.py` (only in repos with a ruff config); the `conventions` skill | `.py` edits | No | `ruff` on PATH |
| **guard-rails** | Bash safety net | every Bash call, edit and prompt | **Yes.** Denies uncapped file dumps, re-dumps, commits without the Claude trailers, `--amend` on pushed history, a second unpushed commit. Asks before force-push, `reset --hard`, `clean -f`, branch delete, stash drop, worktree remove, two-dot diffs | `jq` |
| **auto-review** | Tiered review at Stop sized by the delta since the last review: self-check, one `reviewer`, or a parallel `thermo-bugs` + `thermo-quality` audit, each followed by a `critic`. Ships those agents plus `adversary` and `worker` | Stop after code changes | **Yes.** Blocks the stop once per round to run the review | `jq`; the [thermos](https://github.com/cursor/plugins/tree/main/thermos) plugin enables the two-agent tier (without it the heavy tier is one deep `reviewer`) |
| **ci-babysitter** | Background CI watcher after `git push`, with a scoped repair agent; test and review gates on push; optional one-runner test gate | push, Bash, Stop | **Yes.** Denies a push carrying unreviewed code, asks when no test ran, blocks ending the session while CI is red or pending | `gh` authenticated, `jq` |
| **prod-data** | `ro-mysql` and `bqro` on PATH, DB guards, the `debug` skill, a nudge to load it | every Bash call, prod-data prompts | **Yes.** Denies raw MySQL clients by any spelling, MySQL drivers from scripts, Keychain secret reads | Homebrew `mysql-client`, ssh tunnels, `bq` + `gcloud` auth; macOS Keychain for credentials |
| **workflow** | `/kickoff` (ticket, worktree, one commit, PR, CI and review to green), `/jira`, `/address-review` | only when invoked | No | Atlassian MCP, `gh` |
| **docs** | `process-doc`: a standalone HTML process guide with a reusable screenshot catalog | only when invoked | No | `uv` (Pillow for screenshots) |
| **qa-e2e** | `qa-e2e`: an end-to-end QA pass on a feature, on an emulator or the web, with an ops SOP | only when invoked | No | Android SDK or Playwright MCP |

Hook plugins fail closed where it matters: without `jq` or one of their libs, guard-rails and
prod-data refuse Bash calls and ci-babysitter refuses pushes, instead of letting unchecked commands
through. Advisory hooks tell you they are off and exit cleanly.

## Prerequisites

- `jq` for every hook plugin (macOS ships it since Sequoia; else `brew install jq`).
- `python3` 3.10 or later first on PATH (the Python hooks and `ro-mysql`; macOS's
  `/usr/bin/python3` can be older).
- `gh`, authenticated, for ci-babysitter and the workflow commands.
- prod-data: `brew install mysql-client` (ro-mysql runs only the Homebrew client, at its fixed
  path), ssh access to your database tunnels, and the Google Cloud SDK (`bq`, `gcloud auth login`).
- Bash 3.2 or later. The hooks are written for macOS's stock bash and also run on Linux.

A plugin's `bin/` is on the Bash tool's PATH while the plugin is enabled, so the session can run
`ro-mysql`, `bqro` and `claude-gc` directly. To use them in your own terminal, symlink them from
the plugin directory onto your PATH.

## Overlay config

Company and personal values never live in this repo. They live in `~/.claude/local/` (or
`$CLAUDE_CONFIG_DIR/local/`), which you or your team maintain, for example as a private git repo
cloned there. Every file is optional; a missing one turns off only what it drives.

### `kit.env`

`KEY=value` lines, the value literal and optionally quoted; parsed, never sourced. Copy
[`kit.env.example`](kit.env.example) to `~/.claude/local/kit.env`. `KIT_ENV=<file>` points the hooks
at another file, `KIT_ENV=/dev/null` runs them with none.

| Key | Used by | Effect when set |
|---|---|---|
| `GIT_AUTHOR` | your CLAUDE.md | the commit author your rules name |
| `RELEASE_BRANCH_RE` | guard-rails | extra base branches (ERE) for the two-dot diff check |
| `TEST_GATE_FILE` | ci-babysitter, session-context | a repo with this file at its root runs tests only through `TEST_RUNNER` |
| `TEST_RUNNER` | ci-babysitter, session-context | the one test wrapper allowed there; also allowlisted for the CI repair agent |
| `TEST_RUNNER_RE` | ci-babysitter, session-context | runner names that count as a test run (ERE; default `TEST_RUNNER`) |
| `TEST_REFUSED_CMDS` | ci-babysitter | `\|`-separated command prefixes refused in a gated repo |
| `TEST_NOTE` | ci-babysitter, session-context | one sentence appended to the gate's refusal and the TESTS line |
| `PROTOTYPE_DIRS` | auto-review, guard-rails | extra sandbox dir names (ERE): no review marker, no comment nudge |
| `CODE_DIRS` | session-context (`claude-gc`) | dirs whose children are git repos, for worktree housekeeping |
| `BQRO_PROJECT` | prod-data | the BigQuery jobs project; `bqro` refuses to run without it (or `--project_id=`) |

### `~/.claude/local/` layout

```
~/.claude/local/
├── kit.env                    # the keys above
├── <repo>-rules.md            # a repo's conventions, PR labels, ROLLOUT, "Branches and deploys"
├── <repo>-invariants.md       # its code rules; a "## TL;DR" section is injected at SessionStart
├── <repo>-testing.md          # how to run and write its tests on this machine
├── <repo>-ci.env              # ci-watch: CI_WATCH_WORKFLOW=<workflow>, CI_WATCH_LABEL=<label>
├── <repo>-ci-notes.md         # CI knowledge handed verbatim to the repair agent
├── ci-rules.md                # how you diagnose a red run (the workflow commands point here)
├── prod-data-repos            # repo names (one per line) that get the PROD DATA line
├── no-project-settings        # repo names whose own CLAUDE.md you do not load
├── jira-prefs.md              # written by /jira on first run: site, project, labels, field notes
├── debug/                     # the debug skill's company half: domain.md, mysql.md,
│                              #   bigquery.md, query-catalog.md, scripts/
├── qa-e2e/                    # qa-e2e: backend.md, frontend.md (repo paths, seeds, auth)
└── process-doc/               # process-doc: config.md, hosting.md, screenshot-catalog/
```

### Database tunnels (`~/.ssh/config`)

`ro-mysql` reaches only the local forwards of `Host db-tunnel-<name>` blocks. A tunnel or login
path whose name has a `staging`/`stg` segment is STAGING, every other one PROD, and every query
labels its target. Name the DB user with an annotation line inside the block:

```
Host db-tunnel-orders
  # ro-mysql: user=<db user>
  HostName <jump host>
  LocalForward 23306 <db host>:3306

Host db-tunnel-orders-staging
  # ro-mysql: user=<staging db user>
  HostName <jump host>
  LocalForward 23308 <staging db host>:3306
```

Give each tunnel its own local port. Then store each user's password in the macOS Keychain, in
your own terminal (it prompts, tests the password on every tunnel of that user, and stores it only
if one accepts it):

```bash
ro-mysql --rotate          # again whenever a password rotates
ro-mysql --tunnels         # every tunnel: kind, DB user, state; --refresh lists databases
ro-mysql --tunnel=orders -D <db> -e 'SELECT 1'
```

Passwords reach the client only through a 0600 option file removed when it exits; they never
appear in argv, the environment or the transcript. Without a Keychain entry, ro-mysql falls back to
a `mysql_config_editor` login path of the same kind on that port.

## Templates

Not plugins, because they change files you own. Read, diff, copy what you want.

- `templates/CLAUDE.md.example`: the global rules these plugins were built around. Take the
  structure and write your own lines; every line is loaded into every session.
- `templates/gitignore-claude-config`: an allowlist `.gitignore` for versioning your own
  `~/.claude` without ever committing a transcript, key or `settings.local.json`.
- `templates/statusline-command.sh`: model, dir, branch, context and rate-limit status line. Wire it
  with `"statusLine": {"type": "command", "command": "~/.claude/statusline-command.sh"}`.

## Developing

```
lib/hooks/        the ONE source of the shared hook libs
plugins/<name>/   one plugin: .claude-plugin/plugin.json, hooks/hooks.json, hooks/, bin/,
                  skills/, agents/, commands/
scripts/build.sh  copies lib/hooks into each plugin's hooks/lib and bash-guards into prod-data
scripts/scan.sh   the leak scan (CI and scripts/pre-push)
tests/            run-tests.sh and the hook suites it runs against the plugin copies
```

- Edit a shared lib in `lib/hooks/` (or `bash-guards` in guard-rails), then `scripts/build.sh`.
  `tests/run-tests.sh` fails when a copy drifts from its source.
- `bash tests/run-tests.sh` before every push. It runs the four hook suites against the plugin
  copies, plus the plugin-only cases (guard sets, namespaced agents, bqro's project).
- `scripts/scan.sh` runs gitleaks with [`.gitleaks.toml`](.gitleaks.toml) over the tree and every
  commit, checks an org denylist, commit identities and images. CI reads the denylist from the
  `LEAK_TERMS` secret (newline-separated EREs); locally, put it in
  `~/.config/claude-kit/leak-terms.txt` and install the hook:
  `ln -sf ../../scripts/pre-push .git/hooks/pre-push`.
- Images need an entry in `.scan-images-allow`.

## Licence

MIT (see [LICENSE](LICENSE)), except `sentry-fix-issues`, which is Apache-2.0
([LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt)). Third-party credits are in [NOTICE](NOTICE).
