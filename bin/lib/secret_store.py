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
    """(exit code, the secret when reveal, else ""); 0 means found. The secret comes without the
    newline the store ends its output with. Without reveal the Keychain is asked for the item's
    attributes only, never the secret. secret-tool exits 1 for a missing item and a refusal alike, so
    either, or an empty secret, reads as ITEM_NOT_FOUND. A store that cannot run reads as 1; the env
    store has no items."""
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
    if not reveal or proc.returncode != 0:
        return proc.returncode, ""
    secret = proc.stdout.rstrip("\n") if where == "secret-tool" else proc.stdout
    return 0, secret[:-1] if secret.endswith("\n") else secret


def save(service: str, account: str, secret: str, label: str) -> bool:
    """Store secret under (service, account), then read it back: True when the store holds it. The
    secret never enters argv: `security -i` reads it hex-encoded on stdin, secret-tool on stdin. A
    service or account a `security -i` line would split is refused; the env store saves nothing."""
    where = store()
    if where == "env" or any(c.isspace() or c in "\"'\\" for c in service + account):
        return False
    if where == "keychain":
        command = [SECURITY, "-i"]
        stdin = f"add-generic-password -U -s {service} -a {account} -X {secret.encode().hex()}\n"
    else:
        command = [secret_tool(), "store", f"--label={label}", "service", service, "account", account]
        stdin = secret
    try:
        proc = subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and lookup(service, account, reveal=True) == (0, secret)


def env_suffix(text: str) -> str:
    """text as a variable name part: ASCII letters and digits kept, every other byte as _XX (hex),
    so prod-a and prod_a never share a variable."""
    return "".join(chr(byte) if chr(byte).isascii() and chr(byte).isalnum() else f"_{byte:02X}"
                   for byte in text.encode())
