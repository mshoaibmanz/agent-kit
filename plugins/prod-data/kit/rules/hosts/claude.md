# Claude host notes

Use the installed named agents for review. Reuse existing Claude authentication.

Pick the named agent by task: `researcher` for read-only discovery and research, `web-browser` to
drive a web page, `engineer` for feature code and fixes, `second-opinion` to challenge a plan or diff.
Resume a finished subagent only for a short follow-up below {{SUBAGENT_RESUME_MAX_K}}K context; above it,
start a fresh one from its notes (`SUBAGENT_RESUME_MAX` in kit.env sets the limit).
