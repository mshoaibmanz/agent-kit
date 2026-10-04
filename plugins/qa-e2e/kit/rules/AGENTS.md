# Shared working rules

- Keep credentials in the provider's native store or a system secret store. Never print or commit them.
- Inspect repository instructions before editing its code.
- Preserve unrelated user edits. Diff explicit paths before staging.
- Ask before destructive operations and public publication.
- Do not write to production systems from an agent session.
- Choose representative local checks that verify changed behavior before a push.
- Work on a branch or isolated worktree. Never force-push shared history.
- Record handoffs, reusable knowledge and decisions in the bound task or project folder.
- Put disposable test and build directories there too; set TMPDIR to a directory inside that folder. Keep source worktrees with their repository.
- Reproduce review findings before fixing them. Reviewers must cite a mechanism and evidence.
- Keep repo-specific commands, ticket prefixes and domain facts in the per-user overlay.
