"""The hooks that can refuse a tool call or stop a turn: agent-setup installs them only with
--blocking-hooks, and the dashboard labels them."""

BLOCKING = frozenset(
    {
        "bash-guards",
        "test-exec-gate",
        "edit-guard",
        "commit-cohesion",
        "context-guard",
        "review-trigger",
        "unfinished-work",
        "context-watch",
        "read-streak",
        "pre-compact",
        "project-bind",
        "ci-watch-on-push",
    }
)
