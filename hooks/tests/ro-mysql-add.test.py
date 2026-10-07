"""ro-mysql add (a tunnel block written to the ssh config, checked with `ssh -G`, its password stored
out of argv) and the tunnel fields and helpers it reads, against a scratch HOME's ssh config. The
fixtures (the loaded ro-mysql, its fake Keychain and client) are ro-mysql.test.py's.

    python3 ~/.claude/hooks/tests/ro-mysql-add.test.py
    RO_MYSQL=<old copy> HOOKS_DIR=<old hooks dir> python3 …   # pre-fix
"""

import contextlib
import datetime
import importlib.machinery
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator

sys.dont_write_bytecode = True
# The base suite's fixtures; loaded under another name, its own checks do not run.
_loader = importlib.machinery.SourceFileLoader(
    "ro_mysql_test", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ro-mysql.test.py")
)
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
assert _spec is not None
base = importlib.util.module_from_spec(_spec)
_loader.exec_module(base)
KEYCHAIN = base.KEYCHAIN
PATH = base.PATH
TMP = base.TMP
m = base.m


# `add` runs against a scratch HOME's ssh config; `ssh -G -F <it>` is the real ssh, offline.
ADD_PASSWORD = "add-pw " + "Qx7'\"$z"
BASE_CONFIG = """\
Host jump
  HostName bastion.example.test
  User ops
  Port 2222

Host db-tunnel-orders
  # ro-mysql: user=reader
  HostName bastion.example.test
  LocalForward 15306 orders.db.example.test:3306
"""
ADD_ARGS = ["--name", "billing", "--user", "reader", "--via", "jump", "--remote", "billing.db.example.test:3306",
            "--local-port", "15310"]


def scratch_home(text: str = BASE_CONFIG, files: dict[str, str] | None = None) -> str:
    """A scratch HOME's ssh config; `{SSH}` in text and files is its .ssh folder, so an Include
    names an absolute path and ssh never reads the real ~/.ssh."""
    home = tempfile.mkdtemp(prefix="home-", dir=TMP)
    ssh = os.path.join(home, ".ssh")
    os.makedirs(ssh, mode=0o700)
    for name, body in (files or {}).items():
        os.makedirs(os.path.dirname(os.path.join(ssh, name)), exist_ok=True)
        with open(os.path.join(ssh, name), "w") as f:
            f.write(body.replace("{SSH}", ssh))
    config = os.path.join(ssh, "config")
    with open(config, "w") as f:
        f.write(text.replace("{SSH}", ssh))
    os.chmod(config, 0o644)
    return config


def run_add(config: str, argv: list[str], password: str = ADD_PASSWORD) -> tuple[bool, str, list[str]]:
    """(refused, stdout+stderr, prompts) of one add run; the Keychain is the fake with a fresh log."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(KEYCHAIN + ".argv")
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return password

    out, err = io.StringIO(), io.StringIO()
    refused = False
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            m.add(argv, config=config, ask=ask)
        except SystemExit:
            refused = True
    return refused, out.getvalue() + err.getvalue(), prompts


def add_writes_the_block_and_stores_the_password() -> bool:
    config = scratch_home()
    refused, shown, prompts = run_add(config, ADD_ARGS)
    with open(config) as f:
        text = f.read()
    backups = [n for n in os.listdir(os.path.dirname(config)) if n.startswith("config.bak-")]
    backup = os.path.join(os.path.dirname(config), backups[0]) if len(backups) == 1 else ""
    with open(KEYCHAIN + ".argv") as f:
        argv_log = f.read()
    block = (
        "Host db-tunnel-billing\n  # ro-mysql: user=reader\n  HostName bastion.example.test\n  User ops\n"
        "  Port 2222\n  ControlMaster no\n  ControlPath none\n  GatewayPorts no\n"
        "  LocalForward 15310 billing.db.example.test:3306\n  ExitOnForwardFailure yes\n"
        "  ServerAliveInterval 30\n  ServerAliveCountMax 3\n"
    )
    found = ("db-tunnel-billing", "15310", "reader") in [tuple(t)[:3] for t in m.load_tunnels(config)]
    return (
        not refused and len(prompts) == 1 and text == BASE_CONFIG + "\n" + block and found
        and bool(backup) and open(backup).read() == BASE_CONFIG and os.stat(backup).st_mode & 0o777 == 0o600
        and os.stat(config).st_mode & 0o777 == 0o644
        and m.keychain_get("reader@db-tunnel-billing") == ADD_PASSWORD
        and ADD_PASSWORD not in argv_log and ADD_PASSWORD.encode().hex() not in argv_log
        and ADD_PASSWORD not in shown and "ro-mysql --tunnels --refresh" in shown
    )


def add_refuses_a_known_alias_or_port() -> bool:
    config = scratch_home()
    cases = [
        ["--name", "orders", *ADD_ARGS[2:]],
        [*ADD_ARGS[:-1], "15306"],
        [*ADD_ARGS[:5], "nowhere", *ADD_ARGS[6:]],
    ]
    results = [run_add(config, argv) for argv in cases]
    with open(config) as f:
        unchanged = f.read() == BASE_CONFIG
    no_backup = not [n for n in os.listdir(os.path.dirname(config)) if n.startswith("config.bak-")]
    return all(refused and not prompts for refused, _, prompts in results) and unchanged and no_backup


def add_dry_run_prints_and_writes_nothing() -> bool:
    config = scratch_home()
    refused, shown, prompts = run_add(config, [*ADD_ARGS, "--dry-run"])
    with open(config) as f:
        unchanged = f.read() == BASE_CONFIG
    return not refused and not prompts and unchanged and shown.startswith("Host db-tunnel-billing\n") and (
        "LocalForward 15310 billing.db.example.test:3306" in shown
    ) and not os.path.exists(KEYCHAIN + ".argv")


def add_staging_names_the_alias_and_a_staging_name_needs_the_flag() -> bool:
    config = scratch_home()
    _, shown, _ = run_add(config, [*ADD_ARGS, "--staging", "--dry-run"])
    refused, _, _ = run_add(config, ["--name", "billing-stg", *ADD_ARGS[2:], "--dry-run"])
    return shown.startswith("Host db-tunnel-billing-staging\n") and refused


def add_never_takes_a_password_flag() -> bool:
    config = scratch_home()
    refused, shown, prompts = run_add(config, [*ADD_ARGS, "--password=" + ADD_PASSWORD])
    return refused and not prompts and ADD_PASSWORD not in shown


def add_needs_a_terminal() -> bool:
    r = subprocess.run([sys.executable, PATH, "add", *ADD_ARGS], stdin=subprocess.DEVNULL, capture_output=True)
    return r.returncode == 2 and b"terminal" in r.stderr


def ssh_g(config: str, host: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    r = subprocess.run(["ssh", "-G", "-F", config, host], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    for line in r.stdout.splitlines():
        key, _, value = line.partition(" ")
        out.setdefault(key, []).append(value)
    return out


def untouched(config: str, text: str) -> bool:
    """config still reads text (its {SSH} filled), with no backup and no temporary file beside it."""
    folder = os.path.dirname(config)
    with open(config) as f:
        same = f.read() == text.replace("{SSH}", folder)
    return same and not [n for n in os.listdir(folder) if n.startswith((".config.", "config.bak-"))]


INCLUDED = {"inc/team": "Host db-tunnel-orders\n  HostName other.example.test\n  LocalForward 15310 orders.db:3306\n"}
INCLUDING = "Include {SSH}/inc/*\n\n" + BASE_CONFIG.replace("db-tunnel-orders", "db-tunnel-legacy")


def add_sees_included_aliases_and_ports() -> bool:
    config = scratch_home(INCLUDING, INCLUDED)
    alias = run_add(config, ["--name", "orders", *ADD_ARGS[2:-1], "15399"])
    port = run_add(config, ADD_ARGS)
    listed = [t[:2] for t in m.load_tunnels(config)]
    return (
        alias[0] and not alias[2] and port[0] and not port[2] and untouched(config, INCLUDING)
        and ("db-tunnel-orders", "15310") in listed
    )


WILDCARD_LAST = BASE_CONFIG + "\nHost *\n  User fallback\n  Port 2200\n"


def add_goes_above_the_first_wildcard_host() -> bool:
    config = scratch_home(WILDCARD_LAST)
    refused, _, _ = run_add(config, ADD_ARGS)
    with open(config) as f:
        text = f.read()
    got = ssh_g(config, "db-tunnel-billing")
    return (
        not refused and text.index("Host db-tunnel-billing") < text.index("Host *")
        and got["user"] == ["ops"] and got["port"] == ["2222"]
    )


AUTH_BASTION = """\
Host jump
  HostName bastion.example.test
  User ops
  IdentityFile "{SSH}/jump key"
  IdentityFile {SSH}/second
  IdentitiesOnly yes
  ProxyCommand nc -X 5 -x proxy.example.test:1080 %h %p
  CertificateFile {SSH}/jump-cert.pub
"""


def add_copies_the_bastions_auth_and_transport() -> bool:
    config = scratch_home(AUTH_BASTION)
    refused, _, _ = run_add(config, ADD_ARGS)
    keys = ("hostname", "user", "port", "proxycommand", "identityfile", "identitiesonly", "certificatefile")
    new, jump = ssh_g(config, "db-tunnel-billing"), ssh_g(config, "jump")
    return not refused and all(new.get(k) == jump.get(k) for k in keys) and len(new["identityfile"]) == 2


SHADOWED = {"inc/defaults": "Host jump\n  HostName bastion.example.test\n  User ops\n\nHost *\n  User fallback\n"}


def add_refuses_a_config_where_the_alias_resolves_otherwise() -> bool:
    text = "Include {SSH}/inc/*\n"
    config = scratch_home(text, SHADOWED)
    refused, shown, prompts = run_add(config, ADD_ARGS)
    dry_refused, dry, _ = run_add(config, [*ADD_ARGS, "--dry-run"])
    return (
        refused and not prompts and "user: db-tunnel-billing would get fallback; jump has ops" in shown
        and not dry_refused and dry.startswith("Host db-tunnel-billing\n") and "warning: user:" in dry
        and untouched(config, text)
    )


def add_cancelled_at_the_prompt_writes_nothing() -> bool:
    config = scratch_home()
    results = []
    for error in (KeyboardInterrupt, EOFError):
        def ask(_prompt: str, error: type[BaseException] = error) -> str:
            raise error

        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                m.add(ADD_ARGS, config=config, ask=ask)
            results.append(False)
        except SystemExit:
            results.append(untouched(config, BASE_CONFIG))
        except (KeyboardInterrupt, EOFError):  # escaped add: not a clean abort
            results.append(False)
    return all(results)


def add_matches_the_bastion_alias_case_sensitively() -> bool:
    text = "Host Jump\n  User ops\n  Port 2222\n"
    config = scratch_home(text)
    other_case = run_add(config, [*ADD_ARGS, "--dry-run"])
    upper = run_add(config, [*ADD_ARGS[:5], "JUMP", *ADD_ARGS[6:], "--dry-run"])
    exact = run_add(config, [*ADD_ARGS[:5], "Jump", *ADD_ARGS[6:], "--dry-run"])
    return (
        other_case[0] and "case-sensitively" in other_case[1] and upper[0] and not exact[0]
        and "User ops" in exact[1] and untouched(config, text)
    )


FULL_BASTION = """\
Host jump
  HostName bastion.example.test
  User ops
  IdentityAgent {SSH}/agent.sock
  HostKeyAlias corp-bastion
  UserKnownHostsFile {SSH}/known_hosts.corp {SSH}/known_hosts.extra
  PubkeyAcceptedAlgorithms +ssh-rsa
  HostKeyAlgorithms +ssh-rsa
  PreferredAuthentications publickey
  ConnectTimeout 7
  Compression yes
"""


def comparable(settings: dict[str, list[str]]) -> dict[str, list[str]]:
    skip = {"host", "localforward", "exitonforwardfailure", "serveraliveinterval", "serveralivecountmax"}
    return {k: v for k, v in settings.items() if k not in skip}


def add_copies_and_verifies_the_bastions_full_config() -> bool:
    config = scratch_home(FULL_BASTION)
    refused, shown, _ = run_add(config, ADD_ARGS)
    new, jump = ssh_g(config, "db-tunnel-billing"), ssh_g(config, "jump")
    return not refused and comparable(new) == comparable(jump) and new["identityagent"] == jump["identityagent"] and (
        len(new["userknownhostsfile"][0].split()) == 2
    ), shown


WILDCARD_DEFAULTS = "Host jump\n  HostName bastion.example.test\n  User ops\n\nHost * !jump\n  ProxyJump jump\n"


def add_sets_back_what_a_wildcard_block_would_add() -> bool:
    config = scratch_home(WILDCARD_DEFAULTS)
    refused, shown, _ = run_add(config, ADD_ARGS)
    with open(config) as f:
        text = f.read()
    new = ssh_g(config, "db-tunnel-billing")
    return not refused and "  ProxyJump none\n" in text and "proxyjump" not in new


def add_explains_an_identity_list_it_cannot_match_and_dry_run_still_prints() -> bool:
    text = WILDCARD_DEFAULTS + "  IdentityFile ~/.ssh/inner_key\n"
    config = scratch_home(text)
    refused, shown, prompts = run_add(config, ADD_ARGS)
    dry_refused, dry, _ = run_add(config, [*ADD_ARGS, "--dry-run"])
    why = "adds to every host it matches: exclude db-tunnel-billing there"
    return (
        refused and not prompts and "identityfile:" in shown and why in shown and untouched(config, text)
        and not dry_refused and dry.startswith("Host db-tunnel-billing\n") and "  ProxyJump none\n" in dry
        and "warning: identityfile:" in dry
    )


QUOTED_INCLUDE = 'Include "{SSH}/team dir/*"\n\n' + BASE_CONFIG.replace("db-tunnel-orders", "db-tunnel-legacy")


def add_reads_a_quoted_include() -> bool:
    config = scratch_home(QUOTED_INCLUDE, {"team dir/x": INCLUDED["inc/team"]})
    port = run_add(config, ADD_ARGS)
    listed = [t[:2] for t in m.load_tunnels(config)]
    return (
        port[0] and "local port 15310 is already forwarded" in port[1] and untouched(config, QUOTED_INCLUDE)
        and ("db-tunnel-orders", "15310") in listed
        and m.config_args('"a b" c\\ d \'e\' # f') == ["a b", "c d", "e"] and m.config_args('"open') is None
    )


def add_keeps_an_edit_made_during_the_prompt() -> bool:
    config = scratch_home()
    edit = "\nHost db-tunnel-other\n  LocalForward 15399 other.db:3306\n"

    def ask(_prompt: str) -> str:
        with open(config, "a") as f:
            f.write(edit)
        return ADD_PASSWORD

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
        m.add(ADD_ARGS, config=config, ask=ask)
    with open(config) as f:
        text = f.read()
    folder = os.path.dirname(config)
    [backup] = [n for n in os.listdir(folder) if n.startswith("config.bak-")]
    with open(os.path.join(folder, backup)) as f:
        backed_up = f.read()
    return (
        text.startswith(BASE_CONFIG + edit) and "Host db-tunnel-billing" in text and backed_up == BASE_CONFIG + edit
        and "changed during the prompt" in err.getvalue() and not os.path.exists(config + ".ro-mysql-lock")
    )


def add_refuses_while_another_add_holds_the_lock() -> bool:
    config = scratch_home()
    os.mkdir(config + ".ro-mysql-lock")
    refused, shown, prompts = run_add(config, ADD_ARGS)
    os.rmdir(config + ".ro-mysql-lock")
    return refused and "another ro-mysql add holds" in shown and not prompts and untouched(config, BASE_CONFIG)


MULTIPLEXED = {
    "bastion": "Host jump\n  HostName bastion.example.test\n  User ops\n  ControlMaster auto\n"
    "  ControlPath {SSH}/cm-%r@%h:%p\n  ControlPersist 10m\n  GatewayPorts yes\n",
    "Host *": "Host jump\n  HostName bastion.example.test\n  User ops\n\n"
    "Host *\n  ControlMaster auto\n  ControlPath {SSH}/cm-%n\n  ControlPersist 10m\n  GatewayPorts yes\n",
}
MULTIPLEXING = {"controlmaster", "controlpath", "controlpersist", "gatewayports"}


def add_never_shares_the_bastions_multiplexing_socket() -> bool:
    """Set on the bastion or by `Host *`: the block resets them, and the rest still matches."""
    results = []
    for text in MULTIPLEXED.values():
        config = scratch_home(text)
        refused, shown, _ = run_add(config, ADD_ARGS)
        with open(config) as f:
            block = f.read().split("Host db-tunnel-billing\n", 1)[-1].split("\n\n", 1)[0]
        new, jump = ssh_g(config, "db-tunnel-billing"), ssh_g(config, "jump")
        rest = {k: v for k, v in comparable(new).items() if k not in MULTIPLEXING}
        results.append(
            not refused and "  ControlMaster no\n  ControlPath none\n  GatewayPorts no\n" in block
            and "ControlPersist" not in block and "cm-" not in block
            and new["controlmaster"] == ["false"] and "controlpath" not in new and new["gatewayports"] == ["no"]
            and rest == {k: v for k, v in comparable(jump).items() if k not in MULTIPLEXING}
        )
        if not results[-1]:
            print(shown, block, sep="\n")
    return all(results)


@contextlib.contextmanager
def editor_appends(path: str, edit: str, on_open: int) -> Iterator[None]:
    """Append edit to path just before its on_open-th open for reading in this process: another
    editor's write landing between two of add's reads. An audit hook (never removed) watches."""
    state = {"opens": 0, "armed": True}
    EDITORS.append((os.path.realpath(path), edit, on_open, state))
    try:
        yield
    finally:
        state["armed"] = False


EDITORS: list[tuple[str, str, int, dict]] = []


def _audit(event: str, args: tuple) -> None:
    if event != "open" or not EDITORS:
        return
    for path, edit, on_open, state in EDITORS:
        if not state["armed"] or not isinstance(args[0], str) or args[1] != "r":
            continue
        if os.path.realpath(args[0]) == path:
            state["opens"] += 1
            if state["opens"] == on_open:
                state["armed"] = False
                with open(path, "a") as f:
                    f.write(edit)


sys.addaudithook(_audit)


def add_keeps_an_edit_made_while_it_reads_the_config() -> bool:
    """The edit lands after add's first read of the config, before the next: the build input and
    what the re-check compares against are one snapshot, so the edit is seen and kept."""
    config = scratch_home()
    edit = "\nHost db-tunnel-other\n  LocalForward 15399 other.db:3306\n"
    with editor_appends(config, edit, on_open=2), contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()) as err:
        m.add(ADD_ARGS, config=config, ask=lambda _prompt: ADD_PASSWORD)
    with open(config) as f:
        text = f.read()
    return text.startswith(BASE_CONFIG + edit) and "Host db-tunnel-billing" in text and (
        "changed during the prompt" in err.getvalue()
    )


SIGNALLED_ADD = """\
import importlib.machinery, importlib.util, json, os, sys
loader = importlib.machinery.SourceFileLoader("rom", sys.argv[1])
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("rom", loader))
loader.exec_module(m)
m.add(json.loads(sys.argv[3]), config=sys.argv[2], ask=lambda _prompt: os.kill(os.getpid(), int(sys.argv[4])))
"""


def add_releases_the_lock_on_sigterm_or_sighup_at_the_prompt() -> bool:
    results = []
    for signum in (signal.SIGTERM, signal.SIGHUP):
        config = scratch_home()
        r = subprocess.run(
            [sys.executable, "-c", SIGNALLED_ADD, PATH, config, json.dumps(ADD_ARGS), str(int(signum))],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
        )
        results.append(
            r.returncode == 128 + signum and not os.path.exists(config + ".ro-mysql-lock")
            and untouched(config, BASE_CONFIG)
        )
    return all(results)


def cached_state_reads_each_row_kind() -> bool:
    return [m.cached_state(r) for r in (
        {},
        {"checked": "2026-01-02", "via": "via keychain", "databases": "a,b"},
        {"checked": "2026-01-02", "via": "-", "databases": "? access denied"},
    )] == [("", "", None, "not checked"), ("2026-01-02", "via keychain", ["a", "b"], ""),
           ("2026-01-02", "-", None, "access denied")]


def two_backups_in_one_second_get_two_names() -> bool:
    config = scratch_home()
    now = datetime.datetime(2026, 1, 2, 3, 4, 5)
    names = {m.backup_config(config, "a", now), m.backup_config(config, "b", now)}
    return len(names) == 2 and all(os.stat(n).st_mode & 0o777 == 0o600 for n in names)


ADD_CHECKS: dict[str, Callable[[], bool]] = {
    "add refuses an alias or local port an Included file has; load_tunnels reads Includes":
        add_sees_included_aliases_and_ports,
    "add puts the block above a trailing Host *, so its User and Port win": add_goes_above_the_first_wildcard_host,
    "add copies the bastion's IdentityFiles, IdentitiesOnly, ProxyCommand and CertificateFile":
        add_copies_the_bastions_auth_and_transport,
    "add refuses before the prompt, and --dry-run warns, where ssh -G resolves the alias otherwise":
        add_refuses_a_config_where_the_alias_resolves_otherwise,
    "add cancelled at the password prompt writes nothing": add_cancelled_at_the_prompt_writes_nothing,
    "add matches --via against Host case-sensitively, as ssh does": add_matches_the_bastion_alias_case_sensitively,
    "add copies and verifies the bastion's full ssh -G config (IdentityAgent, HostKeyAlias, known hosts...)":
        lambda: add_copies_and_verifies_the_bastions_full_config()[0],
    "add sets ProxyJump none where a wildcard block would give the alias one":
        add_sets_back_what_a_wildcard_block_would_add,
    "add explains an IdentityFile list it cannot match; --dry-run still prints the block":
        add_explains_an_identity_list_it_cannot_match_and_dry_run_still_prints,
    "add reads a quoted Include (port refusal and discovery)": add_reads_a_quoted_include,
    "add keeps an edit made during the prompt and backs up the current file": add_keeps_an_edit_made_during_the_prompt,
    "add refuses while another add holds the lock": add_refuses_while_another_add_holds_the_lock,
    "add never shares the bastion's ControlMaster/ControlPath/ControlPersist nor its GatewayPorts":
        add_never_shares_the_bastions_multiplexing_socket,
    "add keeps an edit landing between its reads of the config (one snapshot)":
        add_keeps_an_edit_made_while_it_reads_the_config,
    "add releases the lock on SIGTERM or SIGHUP at the prompt and writes nothing":
        add_releases_the_lock_on_sigterm_or_sighup_at_the_prompt,
    "cached_state reads a checked, a failed and a missing row": cached_state_reads_each_row_kind,
    "two backups within one second get two names": two_backups_in_one_second_get_two_names,
}


checks: dict[str, Callable[[], bool]] = {
    "add appends the block, backs the config up, stores the password out of argv and output":
        add_writes_the_block_and_stores_the_password,
    "add refuses a known alias, a forwarded port or an unknown bastion, before asking":
        add_refuses_a_known_alias_or_port,
    "add --dry-run prints the block and writes nothing": add_dry_run_prints_and_writes_nothing,
    "add --staging names the alias STAGING; a staging name without it is refused":
        add_staging_names_the_alias_and_a_staging_name_needs_the_flag,
    "add refuses a password on the command line": add_never_takes_a_password_flag,
    "add refuses without a terminal": add_needs_a_terminal,
    "a tunnel carries its forward's remote and its block's HostName": lambda: [
        (t.remote, t.hostname) for t in m.load_tunnels(scratch_home())
    ] == [("orders.db.example.test:3306", "bastion.example.test")],
    **ADD_CHECKS,
}

if __name__ == "__main__":
    sys.exit(base.run(checks))
