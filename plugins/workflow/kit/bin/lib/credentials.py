"""The one credential classifier, with the two rules the README states: the install refuses
high-confidence shapes only (`Shown.credentials`, preset.validate_catalog), and the page shows a value
only when it can vouch for it (`Shown.text`, deny by default)."""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from typing import NamedTuple
from urllib.parse import parse_qsl, urlsplit

from pack import PRIVATE_KEY, TOKEN, TOKEN_PLACEHOLDER

MASK = "•••• hidden"
CREDENTIAL_NAME = re.compile(
    r"token|secret|passw|passphrase|pwd|api[-_]?key|access[-_]?key|private[-_]?key|client[-_]?key"
    r"|license[-_]?key|auth(?!or(?:s|ity|ities)?(?:$|[-_]))|cookie|credential|bearer|session|signature"
    r"|(?:^|[-_])key$|^pat$|^pass$",
    re.I,
)
# A flag or variable with one of these endings names where a credential is, not the credential.
REFERENCE_NAME = re.compile(
    r"[-_](?:file|path|dir|url|uri|type|mode|method|port|env|var|id|name)$", re.I
)
# Flags whose next argument is a secret whatever it looks like (the install refuses a literal one).
SECRET_FLAGS = re.compile(
    r"^--(?:key|api[-_]?key|token|access[-_]token|auth[-_]token|pat|password|passphrase|secret)$", re.I
)
SHORT_SECRET_FLAGS = ("-p",)
HEADER_FLAGS = ("--header", "-H")
HEADER = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):\s*(\S.*)$")
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
ENV_REF = re.compile(r"^\$(?:\{[A-Za-z_]\w*\}|[A-Za-z_]\w*)$")
HEADER_REF = re.compile(r"^(?:[A-Za-z]+\s+)?\$(?:\{[A-Za-z_]\w*\}|[A-Za-z_]\w*)$")
NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
PATH = re.compile(r"^(?:[/~.]|\{\{[A-Z_]+\}\})")
PACKAGE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(?:@[\w.^~<>=*+-]+)?$")
HOST_PORT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*:\d{1,5}$")
WORD = re.compile(r"^[a-z0-9._-]{1,24}$")
# Shapes masked wherever they appear on a page, beside pack.TOKEN's formats and private keys.
SECRET_SHAPES = {
    "url userinfo": re.compile(r"(?<=://)[^/\s@\"'<>]+@"),
    "bearer": re.compile(r"\bbearer\s+(?!\$)[^\s\"'<>]+", re.I),
    "jwt": re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}(?:\.[\w-]*)?"),
    "sentry token": re.compile(r"\bsntry[su]_[\w=+/-]{10,}"),
    "atlassian token": re.compile(r"\bATATT[\w=+/-]{10,}"),
    "google token": re.compile(r"\bya29\.[\w-]{10,}"),
}
QUERY_CREDENTIAL = re.compile(
    r"[?&](?:access[-_]?token|auth[-_]?token|token|api[-_]?key|key|password|secret)=", re.I
)


class Shown(NamedTuple):
    """text: what a page may show. credentials: why the install refuses the value; a part masked
    only because the page cannot vouch for it adds nothing here."""

    text: str
    credentials: tuple[str, ...]


class Credential(NamedTuple):
    """A Keychain item a catalog entry names: a `credentials` declaration or a $keychain reference."""

    service: str
    account: str


def credential_name(name: str) -> bool:
    name = name.lstrip("-")
    return bool(CREDENTIAL_NAME.search(name)) and not REFERENCE_NAME.search(name)


def key_shaped(text: str) -> bool:
    """A run (split at - _ . ~ @ /) shaped like a key: long with letters and digits, or shorter in
    every case. A UUID or a slug has no such run."""
    for run in re.split(r"[-_.~@/]", text):
        letters, digits = re.search(r"[A-Za-z]", run), re.search(r"\d", run)
        mixed_case = re.search(r"[a-z]", run) and re.search(r"[A-Z]", run)
        if letters and digits and (len(run) >= 16 or (len(run) >= 10 and mixed_case)):
            return True
    return False


def holds_secret(text: str) -> bool:
    """A token format (not a placeholder), a private key, a credential shape or a credential query."""
    if any(not TOKEN_PLACEHOLDER.search(m.group()) for m in TOKEN.finditer(text)):
        return True
    shapes = (PRIVATE_KEY, QUERY_CREDENTIAL, *SECRET_SHAPES.values())
    return any(shape.search(text) for shape in shapes)


def reference(value: str) -> bool:
    """A value that names where something is rather than holding it: an env reference, a number or
    a path. The install never refuses one."""
    return bool(ENV_REF.match(value) or NUMBER.match(value) or PATH.match(value))


def show_value(value: str) -> str:
    """value as the page may show it: deny by default (module docstring)."""
    if holds_secret(value):
        return MASK
    if "://" in value:
        return show_url(value).text
    if reference(value) or HOST_PORT.match(value):
        return value
    if (PACKAGE.match(value) or WORD.match(value)) and not key_shaped(value):
        return value
    return MASK


def show_secret_value(value: str) -> str:
    """A value after a credential-named flag: shown only when it is a reference or a URL."""
    if not holds_secret(value) and "://" in value:
        return show_url(value).text
    return value if reference(value) and not holds_secret(value) else MASK


def show_url(url: str) -> Shown:
    """scheme://host[:port]/path: userinfo and fragment dropped, every query value masked, and a
    key-shaped path segment masked. Userinfo and a credential query key are credentials."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return Shown(MASK, ("unparseable URL",))
    found = ["userinfo in the URL"] if "@" in parts.netloc else []
    segments = [MASK if key_shaped(s) or holds_secret(s) else s for s in parts.path.split("/")]
    host = (parts.hostname or "") + (f":{port}" if port else "")
    query = parse_qsl(parts.query, keep_blank_values=True)
    for key, _ in query:
        if credential_name(key) or re.sub(r"[-_]", "", key).casefold() in ("key", "apikey"):
            found.append(f"URL query {key}")
    shown = f"{parts.scheme}://{host}{'/'.join(segments)}"
    if query:
        shown += "?" + "&".join(f"{key}={MASK}" for key, _ in query)
    return Shown(shown, tuple(found))


def show_header(text: str, flag: str, found: list[str]) -> str:
    """A header argument: its name, and its value only when that is a ${VAR} reference. A literal
    after a header flag, or under a credential name, is a credential."""
    header = HEADER.match(text)
    if header is None:
        if flag and not ENV_REF.match(text):
            found.append(f"value after {flag}")
        return MASK
    name, value = header.groups()
    if HEADER_REF.match(value):
        return f"{name}: {value}"
    if flag or credential_name(name) or name.casefold() == "authorization":
        found.append(f"header {name}" + (f" after {flag}" if flag else ""))
    return f"{name}: {MASK}"


def show_args(args: Sequence[object]) -> Shown:
    """Each argument as a page may show it, and the credentials the install refuses."""
    out: list[str] = []
    found: list[str] = []
    after = ""
    for raw in args:
        if not isinstance(raw, str):
            out.append("<credential reference>" if isinstance(raw, dict) else MASK)
            after = ""
            continue
        arg, flag, after = raw, after, ""
        is_flag = arg.startswith("-") and len(arg) > 1 and not NUMBER.match(arg)
        if flag and not is_flag:
            if flag in HEADER_FLAGS:
                out.append(show_header(arg, flag, found))
                continue
            if SECRET_FLAGS.match(flag) and not reference(arg) and "://" not in arg:
                found.append(f"value after {flag}")
            out.append(show_secret_value(arg))
            continue
        if holds_secret(arg):
            out.append(MASK)
            found.append("a token-shaped argument")
        elif is_flag:
            name, sep, value = arg.partition("=")
            if not sep:
                out.append(arg)
                if arg in HEADER_FLAGS or arg in SHORT_SECRET_FLAGS or credential_name(arg):
                    after = arg
            elif name in HEADER_FLAGS:
                out.append(f"{name}={show_header(value, name, found)}")
            elif credential_name(name):
                if value and not reference(value) and "://" not in value:
                    found.append(f"value of {name}")
                out.append(f"{name}={show_secret_value(value)}")
            else:
                out.append(f"{name}={show_value(value)}")
        elif (assignment := ASSIGNMENT.match(arg)) is not None:
            name, value = assignment.groups()
            if credential_name(name):
                if value and not reference(value):
                    found.append(f"value of {name}")
                out.append(f"{name}={show_secret_value(value)}")
            else:
                out.append(f"{name}={show_value(value)}")
        elif HEADER.match(arg) and "://" not in arg and not HOST_PORT.match(arg):
            out.append(show_header(arg, "", found))
        elif "://" in arg:
            shown = show_url(arg)
            out.append(shown.text)
            found.extend(shown.credentials)
        else:
            out.append(show_value(arg))
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


def declared_credentials(spec: object) -> list[Credential]:
    """The Keychain items an MCP catalog entry declares (`"credentials": [{"service", "account"}]`),
    then those its values reference as {"$keychain": {"service", "account"}}."""
    items: list[Credential] = []
    if isinstance(spec, dict) and isinstance(spec.get("credentials"), list):
        items += [
            Credential(str(c["service"]), str(c["account"]))
            for c in spec["credentials"]
            if isinstance(c, dict) and "service" in c and "account" in c
        ]

    def refs(value: object) -> None:
        if isinstance(value, dict):
            ref = value.get("$keychain")
            if isinstance(ref, dict) and len(value) == 1:
                items.append(Credential(str(ref.get("service", "")), str(ref.get("account", ""))))
                return
            for key, inner in value.items():
                if key != "credentials":
                    refs(inner)
        elif isinstance(value, list):
            for inner in value:
                refs(inner)

    refs(spec)
    return list(dict.fromkeys(items))


def valid_declarations(value: object) -> bool:
    """`credentials`: a list of {"service": <service>, "account": <account>}, names only."""
    name = re.compile(r"[\w./@:-]{1,200}")
    return isinstance(value, list) and all(
        isinstance(item, dict)
        and set(item) == {"service", "account"}
        and all(isinstance(v, str) and name.fullmatch(v) for v in item.values())
        for item in value
    )
