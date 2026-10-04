---
description: Jira module — find/create a ticket, link a PR, transition, sprint, assign — driven by per-user preferences recorded on first run. The full ticket→PR→merge-ready flow is /kickoff; if the arguments describe work to BUILD rather than a ticket operation, follow "${AGENT_KIT_DIR}/commands/kickoff.md" instead.
---

Jira operations only. All identity and project specifics come from the prefs file — nothing
personal is hardcoded here. If `$ARGUMENTS` describes a bug or feature to implement (not
"create a ticket for…", "move ABC-123 to review", "link this PR"), stop and run
`"${AGENT_KIT_DIR}/commands/kickoff.md"` with the same arguments — this file is its phase 1.

## Preferences (read FIRST, record if missing)

Read `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/local/jira-prefs.md`. If it exists and has every key you need this run, use
it. Otherwise, fill the gaps ONCE and write the file:

1. **Self-discover — never ask for what you can look up:**
   - `site` + `cloud_id` → `getAccessibleAtlassianResources`
   - `me_email` + `me_account_id` → `atlassianUserInfo`
2. **Ask the user once** (AskUserQuestion, all in one call) for what can't be discovered:
   - `project_key` — offer choices from `getVisibleJiraProjects`
   - `base_branch_rule` — how to pick the branch/PR base. Offer: "repo default branch" /
     "latest release branch matching <PATTERN>" (verify against
     `gh pr list --state merged --json baseRefName` — where PRs actually land)
   - `pr_labels` — labels to apply to every PR (may be empty)
   - `board_id` — optional; skip unless sprint operations need it
3. **Write** `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/local/jira-prefs.md` as plain `key: value` lines with a one-line
   header saying the file is consumed by /jira and /kickoff and can be deleted to re-run
   setup; keep any `## <KEY> notes` sections below them. Confirm the recorded values to the
   user in one line.

## Find or create

1. FIRST search `project_key` (JQL on summary/text keywords from the input) for an existing
   ticket covering this work; if a plausible match exists, use it (prefer one assigned to
   `me_account_id`) and say so, creating nothing.
2. Otherwise create — type `Story` or `Bug` (infer; never `Task`/other types), summary and
   description from the input. Concise title.
3. Report the key `<KEY>-N` and title.

## Update

- **Link a PR**: one short comment with the PR URL.
- **Transition**: In Progress when work starts; review only at /kickoff phase 5's finish line
  (CI green, review threads handled). Use the project's transition ids from its notes section.
- **Sprint + assignee**: active sprint (JQL `sprint in openSprints()` with `fields:["*all"]`
  on one issue to find its id), assign to `me_account_id`.
- **Closing note** when the PR is green: PR URL, one line on the change, how it was verified.

## Project-specific API notes

Discovered facts per project (field ids, transition ids, create-screen quirks) live in the
prefs file, one `## <KEY> notes` section per project; read the section for `project_key`
before creating or transitioning. When a project has no section, discover what you need
(`getJiraProjectIssueTypesMetadata`, `getJiraIssueTypeMetaWithFields`, one probe issue) and
OFFER to append one so the next run doesn't rediscover it.

Request: $ARGUMENTS
