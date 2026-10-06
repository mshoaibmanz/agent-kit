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


def hook_name(command: str) -> str:
    """The hook script a registry command runs: its first word's file name."""
    words = command.split()
    return words[0].rsplit("/", 1)[-1] if words else "?"
