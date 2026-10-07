"""The credential stores the kit's wrappers read: the macOS Keychain, libsecret's secret-tool on
Linux, else environment variables. Python 3.9 and the standard library only: bin/ro-mysql imports it
from the bin/lib installed beside it. Nothing here prints a secret."""

from __future__ import annotations

import os
import subprocess
from typing import Tuple

# Root-owned fixed paths, never one from PATH: a binary picked from the environment would receive
# every lookup.
SECURITY = "/usr/bin/security"
SECRET_TOOL_PATHS = ("/usr/bin/secret-tool", "/usr/local/bin/secret-tool")
# `security` exits 44 (errSecItemNotFound) when no item matches; any other failure is a denial or a
# locked Keychain.
ITEM_NOT_FOUND = 44


def store() -> str:
    """Where credentials live: "keychain", "secret-tool", else "env" (environment variables, also
    every wrapper's fallback when a store has no item)."""
    if os.path.exists(SECURITY):
        return "keychain"
    if any(os.path.exists(path) for path in SECRET_TOOL_PATHS):
        return "secret-tool"
    return "env"


def secret_tool() -> str:
    return next(path for path in SECRET_TOOL_PATHS if os.path.exists(path))


def lookup(service: str, account: str, reveal: bool, timeout: int = 10) -> Tuple[int, str]:
    """(exit code, the store's raw output when reveal, else ""); 0 means found. Without reveal the
    Keychain is asked for the item's attributes only, never the secret. secret-tool exits 1 for a
    missing item and a refusal alike, so either, or an empty secret, reads as ITEM_NOT_FOUND. A store
    that cannot run reads as 1; the env store has no items."""
    where = store()
    if where == "env":
        return ITEM_NOT_FOUND, ""
    if where == "keychain":
        command = [SECURITY, "find-generic-password", "-s", service, "-a", account, *(["-w"] if reveal else [])]
    else:
        command = [secret_tool(), "lookup", "service", service, "account", account]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    if where == "secret-tool" and (proc.returncode != 0 or not proc.stdout.strip()):
        return ITEM_NOT_FOUND, ""
    return proc.returncode, proc.stdout if reveal and proc.returncode == 0 else ""
