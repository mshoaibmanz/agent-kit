"""The one credential classifier: preset.validate_catalog refuses what it calls a credential, and the
dashboard masks it (and, deny by default, anything else it cannot vouch for)."""

from __future__ import annotations

import re
import shlex
from typing import Any, NamedTuple
from urllib.parse import parse_qsl, urlsplit

from pack import PRIVATE_KEY, TOKEN, TOKEN_PLACEHOLDER

MASK = "•••• hidden"
CREDENTIAL_NAME = re.compile(
    r"token|secret|passw|pwd|api[-_]?key|access[-_]?key|private[-_]?key|client[-_]?key|auth(?!ors?(?:$|[-_]))"
    r"|cookie|credential|bearer|session|signature|^key$|^pat$|^pass$",
    re.I,
)
# A flag or variable with one of these endings names where a credential is, not the credential.
REFERENCE_NAME = re.compile(
    r"[-_](?:file|path|dir|url|uri|type|mode|method|port|env|var|id|name)$", re.I
)
HEADER_FLAGS = ("--header", "-H")
HEADER = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*):\s*(\S.*)$")
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
# Shapes masked wherever they appear on a page, beside pack.TOKEN's formats and private keys.
SECRET_SHAPES = {
    "url userinfo": re.compile(r"(?<=://)[^/\s@\"'<>]+@"),
    "bearer": re.compile(r"\bbearer\s+[^\s\"'<>]+", re.I),
    "jwt": re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}(?:\.[\w-]*)?"),
    "sentry token": re.compile(r"\bsntry[su]_[\w=+/-]{10,}"),
    "atlassian token": re.compile(r"\bATATT[\w=+/-]{10,}"),
    "google token": re.compile(r"\bya29\.[\w-]{10,}"),
}
QUERY_CREDENTIAL = re.compile(
    r"[?&](?:access[-_]?token|auth[-_]?token|token|api[-_]?key|key|password|secret)=", re.I
)


class Shown(NamedTuple):
    """text: what a page may show. credentials: why a masked part is a credential (validate_catalog
    refuses those); a part masked only because it is opaque adds nothing here."""

    text: str
    credentials: tuple[str, ...]


def credential_name(name: str) -> bool:
    name = name.lstrip("-")
    return bool(CREDENTIAL_NAME.search(name)) and not REFERENCE_NAME.search(name)


def secret_segment(segment: str) -> bool:
    """A URL path segment shaped like a key: long with letters and digits, or shorter in every case."""
    letters, digits = re.search(r"[A-Za-z]", segment), re.search(r"\d", segment)
    mixed_case = re.search(r"[a-z]", segment) and re.search(r"[A-Z]", segment)
    return bool(letters and digits and (len(segment) >= 16 or (len(segment) >= 10 and mixed_case)))


def holds_secret(text: str) -> bool:
    """A token format (not a placeholder), a private key, a credential shape or a credential query."""
    if any(not TOKEN_PLACEHOLDER.search(m.group()) for m in TOKEN.finditer(text)):
        return True
    shapes = (PRIVATE_KEY, QUERY_CREDENTIAL, *SECRET_SHAPES.values())
    return any(shape.search(text) for shape in shapes)


def show_url(url: str) -> Shown:
    """scheme://host[:port]/path: userinfo and fragment dropped, every query value masked, and a
    key-shaped path segment masked."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return Shown(MASK, ("unparseable URL",))
    found = []
    if "@" in parts.netloc:
        found.append("userinfo in the URL")
    segments = []
    for segment in parts.path.split("/"):
        if secret_segment(segment):
            found.append("key-shaped URL path segment")
            segments.append(MASK)
        else:
            segments.append(segment)
    host = (parts.hostname or "") + (f":{port}" if port else "")
    query = parse_qsl(parts.query, keep_blank_values=True)
    for key, _ in query:
        if credential_name(key) or re.sub(r"[-_]", "", key).casefold() in ("key", "apikey"):
            found.append(f"URL query {key}")
    shown = f"{parts.scheme}://{host}{'/'.join(segments)}"
    if query:
        shown += "?" + "&".join(f"{key}={MASK}" for key, _ in query)
    return Shown(shown, tuple(found))


def show_args(args: list[Any]) -> Shown:
    """Each argument as a page may show it, and the credentials among them."""
    out: list[str] = []
    found: list[str] = []
    after: str | None = None
    for raw in args:
        arg = str(raw) if not isinstance(raw, dict) else "<credential reference>"
        if after is not None and not arg.startswith("-"):
            header = HEADER.match(arg) if after in HEADER_FLAGS else None
            out.append(f"{header.group(1)}: {MASK}" if header else MASK)
            found.append(f"value after {after}")
            after = None
            continue
        after = None
        flag, sep, value = arg.partition("=")
        assignment = ASSIGNMENT.match(arg)
        header = HEADER.match(arg)
        if holds_secret(arg):
            out.append(MASK)
            found.append("a token-shaped argument")
        elif arg.startswith("-") and sep and (flag in HEADER_FLAGS or credential_name(flag)):
            out.append(f"{flag}={MASK}")
            found.append(f"value of {flag}")
        elif arg.startswith("-") and not sep and (arg in HEADER_FLAGS or credential_name(arg)):
            out.append(arg)
            after = arg
        elif assignment and credential_name(assignment.group(1)):
            out.append(f"{assignment.group(1)}={MASK}")
            found.append(f"value of {assignment.group(1)}")
        elif header and "://" not in arg:
            out.append(f"{header.group(1)}: {MASK}")
            if credential_name(header.group(1)) or header.group(1).casefold() == "authorization":
                found.append(f"header {header.group(1)}")
        elif "://" in arg:
            shown = show_url(arg)
            out.append(shown.text)
            found.extend(shown.credentials)
        else:
            out.append(arg)
    return Shown(shlex.join(out), tuple(found))


def mask_tokens(text: str) -> tuple[str, int]:
    """Last line of defence over a whole page: every credential shape is masked."""
    count = 0
    text, n = TOKEN.subn("[masked token]", text)
    count += n
    text, n = PRIVATE_KEY.subn("[masked key]", text)
    count += n
    for name, shape in SECRET_SHAPES.items():
        replacement = "[masked]@" if name == "url userinfo" else "[masked token]"
        text, n = shape.subn(replacement, text)
        count += n
    return text, count


def declared_credentials(spec: Any) -> list[tuple[str, str]]:
    """The Keychain items an MCP catalog entry declares (`"credentials": [{"keychain", "account"}]`),
    then those its values reference as {"$keychain": {"service", "account"}}."""
    items: list[tuple[str, str]] = []
    if isinstance(spec, dict) and isinstance(spec.get("credentials"), list):
        items += [
            (str(c["keychain"]), str(c["account"]))
            for c in spec["credentials"]
            if isinstance(c, dict) and "keychain" in c and "account" in c
        ]

    def refs(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$keychain")
            if isinstance(ref, dict) and len(value) == 1:
                items.append((str(ref.get("service", "")), str(ref.get("account", ""))))
                return
            for key, inner in value.items():
                if key != "credentials":
                    refs(inner)
        elif isinstance(value, list):
            for inner in value:
                refs(inner)

    refs(spec)
    return list(dict.fromkeys(items))


def valid_declarations(value: Any) -> bool:
    """`credentials`: a list of {"keychain": <service>, "account": <account>}, names only."""
    name = re.compile(r"[\w./@:-]{1,200}")
    return isinstance(value, list) and all(
        isinstance(item, dict)
        and set(item) == {"keychain", "account"}
        and all(isinstance(v, str) and name.fullmatch(v) for v in item.values())
        for item in value
    )
