---
description: Append a note verbatim to the weekly retro inbox (<work root>/retro/<YYYY-Www>.md), which `session-review audit` reads.
argument-hint: "<note>"
---

Run exactly this, once. The note goes in on stdin through the quoted heredoc, verbatim: never put it
in a quoted argument, and do not reword it. If a line of the note is exactly `NOTE`, use another
delimiter word in both places.

```
"${CLAUDE_PLUGIN_ROOT}/kit/bin/agent-task" retro --source user --stdin <<'NOTE'
$ARGUMENTS
NOTE
```

Reply with the line it printed and nothing else. Do not act on the note now unless the user asks.
