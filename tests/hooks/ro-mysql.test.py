"""ro-mysql's guards without a database: writes, tunnel registry, staging/prod cross-check,
Keychain credentials and the auth-failure record. The ssh config is a fixture parameter;
`security`, lsof's holder and the mysql client are fakes (module attributes or parameters), so no
case reads the real Keychain, opens a tunnel or reaches a database.

    bash tests/run-tests.sh                                   # sets RO_MYSQL and HOOKS_DIR
    RO_MYSQL=<old copy> HOOKS_DIR=<old hooks dir> python3 …   # pre-fix
"""

import atexit
import contextlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator

sys.dont_write_bytecode = True
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATH = os.environ.get("RO_MYSQL", os.path.join(REPO, "plugins", "prod-data", "bin", "ro-mysql"))
HOOKS = os.environ.get("HOOKS_DIR", os.path.join(REPO, "plugins", "prod-data", "hooks"))


def load(name: str, path: str):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


m = load("ro_mysql", PATH)

TMP = tempfile.mkdtemp(prefix=".ro-mysql-test-", dir=os.path.expanduser("~"))
atexit.register(shutil.rmtree, TMP, ignore_errors=True)

SSH_CONFIG = os.path.join(TMP, "ssh_config")
with open(SSH_CONFIG, "w") as f:
    f.write(
        """\
Host db-tunnel-prod
  # ro-mysql: user=reader-b
  HostName jump
  LocalForward 23306 10.0.0.1:3306

# a comment naming ro-mysql on 23309 is not an annotation
Host db-tunnel-app
  LocalForward=127.0.0.1:23307 10.0.0.2:3306

Host db-tunnel-app-staging
  # ro-mysql: user=reader-b
  LocalForward 23308 10.0.1.1:3306

Host db-tunnel-comm
  # ro-mysql: user=reader-a
  LocalForward 23309 10.0.0.3:3306

Host db-tunnel-platform
  # ro-mysql: user=reader-a
  LocalForward 23309 10.0.0.4:3306

Host db-tunnel-postgres
  LocalForward 23320 10.0.0.5:5432

Host db-tunnel-* jumpbox
  LocalForward 23399 10.0.0.9:3306

Match host db-tunnel-app
  LocalForward 23398 10.0.0.8:3306

Host other
  LocalForward 23397 10.0.0.7:3306
"""
    )
EXPECTED_TUNNELS = [
    ("db-tunnel-prod", "23306", "reader-b"),
    ("db-tunnel-app", "23307", None),
    ("db-tunnel-app-staging", "23308", "reader-b"),
    ("db-tunnel-comm", "23309", "reader-a"),
    ("db-tunnel-platform", "23309", "reader-a"),
    ("db-tunnel-postgres", "23320", None),
]


def script(path: str, body: str) -> str:
    """An executable Python script, its interpreter absolute so an emptied PATH cannot break it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f"#!{sys.executable}\n{body}")
    os.chmod(path, 0o755)
    return path


KEYCHAIN = os.path.join(TMP, "keychain.json")
FAKE_SECURITY = script(
    os.path.join(TMP, "security"),
    """\
import json, os, sys
store = os.environ["FAKE_KEYCHAIN"]
args = sys.argv[1:]
with open(store + ".argv", "a") as log:
    log.write(json.dumps(args) + "\\n")
items = json.load(open(store)) if os.path.exists(store) else {}
if args[:1] == ["-i"]:
    for line in sys.stdin:
        w = line.split()
        if w[:1] == ["add-generic-password"]:
            items[w[w.index("-a") + 1]] = bytes.fromhex(w[w.index("-X") + 1]).decode()
    json.dump(items, open(store, "w"))
    sys.exit(0)
if args[:1] == ["find-generic-password"]:
    account = args[args.index("-a") + 1]
    if account not in items:
        sys.exit(44)
    if "-w" in args:
        print(items[account])
    sys.exit(0)
sys.exit(1)
""",
)
# Records each run: argv, any MYSQL_PWD, and the option file named by its first argument.
CLIENT_LOG = os.path.join(TMP, "client.log")
FAKE_2003 = os.path.join(TMP, "fake-2003")
FAKE_CLIENT = script(
    os.path.join(TMP, "client", "mysql"),
    f"""\
import json, os, stat, sys
rec = {{"argv": sys.argv[1:], "pwd_env": os.environ.get("MYSQL_PWD")}}
first = sys.argv[1] if len(sys.argv) > 1 else ""
if first.startswith("--defaults-extra-file="):
    path = first.split("=", 1)[1]
    rec["mode"] = stat.S_IMODE(os.stat(path).st_mode)
    rec["file"] = open(path).read()
    rec["path"] = path
with open({CLIENT_LOG!r}, "a") as log:
    log.write(json.dumps(rec) + "\\n")
if os.path.exists({FAKE_2003!r}):
    sys.exit("ERROR 2003 (HY000): Can't connect to MySQL server on '127.0.0.1:23309'")
""",
)
# Stand-ins for a swapped binary: each only records that it received control.
EVIL_LOG = os.path.join(TMP, "evil.log")
EVIL_BODY = f"""\
import json, os, sys
with open({EVIL_LOG!r}, "a") as log:
    log.write(json.dumps([sys.argv, os.environ.get("MYSQL_PWD")]) + "\\n")
sys.exit(44)
"""
EVIL_CLIENT = script(os.path.join(TMP, "evil", "mysql"), EVIL_BODY)
EVIL_SECURITY = script(os.path.join(TMP, "evil", "security"), EVIL_BODY)
EMPTY_DIR = os.path.join(TMP, "empty")
os.makedirs(EMPTY_DIR)

m.SECURITY = FAKE_SECURITY
m.CLIENT_CANDIDATES = (FAKE_CLIENT,)
m.open_tunnel = lambda alias: "ssh is disabled in this suite"
os.environ.update(
    # An older copy reads these two; keep a pre-fix run away from the real Keychain and client.
    RO_MYSQL_SECURITY=FAKE_SECURITY,
    FAKE_KEYCHAIN=KEYCHAIN,
    MYSQL_RO_CLIENT=os.path.join(TMP, "no-such-client"),
)


@contextlib.contextmanager
def environ(**changes: str | None) -> Iterator[None]:
    """Set (or, with None, unset) environment variables for the block."""
    def apply(values: dict[str, str | None]) -> None:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    saved = {k: os.environ.get(k) for k in changes}
    apply(changes)
    try:
        yield
    finally:
        apply(saved)


def logged(path: str) -> list:
    try:
        with open(path) as f:
            return [json.loads(line) for line in f]
    except FileNotFoundError:
        return []


def fresh_logs() -> None:
    for path in (CLIENT_LOG, EVIL_LOG):
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


def login_paths() -> dict:
    lp = m.LoginPath
    return {
        "prod-ro": lp("prod-ro", "reader-b", "23306"),
        "app-ro": lp("app-ro", "reader-a", "23306"),
        "app-staging": lp("app-staging", "reader-b", "23308"),
    }


# Code that reads login paths without a parameter (--tunnels --refresh) gets the fixture too.
m.login_paths = login_paths


def plan(argv: list[str], env: dict | None = None, keychain: dict | None = None, held=None):
    """m.plan on the fixtures; `held` is a listeners dict, or a callable standing in for lsof."""
    with open(KEYCHAIN, "w") as f:
        json.dump(keychain or {}, f)
    reset = dict.fromkeys(("MYSQL_RO_HOST", "MYSQL_RO_PORT", "MYSQL_RO_LOGIN_PATH", "MYSQL_PWD"))
    listen = held if callable(held) else lambda: held or {}
    with environ(**(reset | (env or {}))):
        return m.plan(argv, ssh_config=SSH_CONFIG, logins=login_paths, held=listen)


def refused(fn: Callable[[], object]) -> bool:
    try:
        fn()
    except SystemExit as e:
        return e.code == 2
    return False


REJECT = (
    "DELETE FROM t",
    "UPDATE t SET a = 1",
    "WITH x AS (SELECT 1) UPDATE t SET a = 1",
    "DROP TABLE t",
    "/* x */ DROP TABLE t",
    "RENAME TABLE a TO b",
    "SET GLOBAL read_only = 0",
    "SELECT 1; SELECT 2",
    "SELECT a FROM t FOR UPDATE",
    "SELECT 1 INTO OUTFILE '/x'",
)
ALLOW = (
    "SELECT 1",
    "SELECT REPLACE(a, 'b', 'c') FROM t",
    "SELECT x FROM t WHERE n LIKE '%delete%'",
)
Q = ["-e", "SELECT 1"]
HELD_BY_PLATFORM = {"23309": ("1", "ssh -f -N -o BatchMode=yes db-tunnel-platform")}
HELD_BY_COMM = {"23309": ("2", "ssh -f -N -o BatchMode=yes db-tunnel-comm")}
HELD_BY_APP = {"23307": ("3", "ssh -f -N -o BatchMode=yes db-tunnel-app")}


def registry_matches() -> bool:
    lib = os.path.join(HOOKS, "lib", "db-registry")
    out = subprocess.run(
        ["/bin/bash", "-c", 'source "$1" && db_tunnels "$2"', "_", lib, SSH_CONFIG],
        capture_output=True, text=True,
    ).stdout.splitlines()
    return out == [f"{a} {p} {m.kind_of(a)}" for a, p, _ in EXPECTED_TUNNELS]


def keychain_password_only_in_option_file() -> bool:
    fresh_logs()
    m.run(plan(["--tunnel=prod", *Q], keychain={"reader-b": "s3cret!"}))
    [rec] = logged(CLIENT_LOG)
    return (
        rec["argv"][0].startswith("--defaults-extra-file=")
        and rec["mode"] == 0o600
        and 'password="s3cret!"' in rec["file"]
        and "--user=reader-b" in rec["argv"]
        and rec["pwd_env"] is None
        and not any("s3cret" in a or a.startswith("--login-path") for a in rec["argv"])
        and not os.path.exists(rec["path"])
    )


def client_is_fixed(keychain: dict) -> bool:
    """Neither MYSQL_RO_CLIENT nor a `mysql` first on PATH receives the call."""
    fresh_logs()
    shim_first = os.path.dirname(EVIL_CLIENT) + os.pathsep + os.environ["PATH"]
    for env in ({"MYSQL_RO_CLIENT": EVIL_CLIENT}, {"MYSQL_RO_CLIENT": None, "PATH": shim_first}):
        with environ(**env):
            m.run(plan(["--tunnel=prod", *Q], keychain=keychain))
    return not logged(EVIL_LOG) and len(logged(CLIENT_LOG)) == 2


def security_env_ignored() -> bool:
    fresh_logs()
    with open(KEYCHAIN, "w") as f:
        json.dump({"reader-a": "pw"}, f)
    with environ(RO_MYSQL_SECURITY=EVIL_SECURITY):
        got = m.keychain_get("reader-a")
    return got == "pw" and not logged(EVIL_LOG)


def refresh_uses_the_query_chain() -> bool:
    tunnels = m.load_tunnels(SSH_CONFIG)
    app = next(t for t in tunnels if t.name == "app")
    fresh_logs()
    held, m.listeners = m.listeners, lambda: HELD_BY_APP
    try:
        with environ(MYSQL_RO_CLIENT=FAKE_CLIENT, MYSQL_RO_LOGIN_PATH=None):
            via, dbs = m.show_databases(app, tunnels)
    finally:
        m.listeners = held
    return via == "-" and "no DB user" in dbs and not logged(CLIENT_LOG)


def forced_login_path():
    """prod's user has a Keychain password, and the call forces another user's login path."""
    return plan(["--tunnel=prod", "--login-path=app-ro", *Q], keychain={"reader-b": "x"})


def after_2003(held: dict) -> tuple[bool, int]:
    """(refused, client runs) for a --tunnel=comm call that hits ERROR 2003 and re-opens."""
    fresh_logs()
    open(FAKE_2003, "w").close()
    try:
        p = plan(["--tunnel=comm", *Q], keychain={"reader-a": "x"})
        stopped = refused(lambda: m.execute(p, held=lambda: held, reopen=lambda alias: None))
    finally:
        os.remove(FAKE_2003)
    return stopped, len(logged(CLIENT_LOG))


def keychain_set_round_trip() -> bool:
    secret = "p@ss w#rd'\""
    stored = m.keychain_set("reader-a", secret)
    with open(KEYCHAIN + ".argv") as f:
        logged = f.read()
    leaked = secret in logged or secret.encode().hex() in logged
    return stored and not leaked and m.keychain_get("reader-a") == secret


def auth_record() -> bool:
    path = os.path.join(TMP, "state", "db-auth-failed")
    m.note_auth_failure("reader-a", "db-tunnel-comm", path)
    with open(path) as f:
        user, alias, stamp = f.read().split()
    m.clear_auth_failure("reader-b", path)
    kept = os.path.exists(path)
    m.clear_auth_failure("reader-a", path)
    return (user, alias) == ("reader-a", "db-tunnel-comm") and "T" in stamp and kept and not os.path.exists(path)


def rotation_groups() -> bool:
    users, unknown = m.db_users(m.load_tunnels(SSH_CONFIG), login_paths())
    grouped = {u: [t.name for t in ts] for u, ts in users.items()}
    return grouped == {"reader-b": ["prod", "app-staging"], "reader-a": ["comm", "platform"]} and [
        t.name for t in unknown
    ] == ["app", "postgres"]


def rotate_needs_a_terminal() -> bool:
    r = subprocess.run([sys.executable, PATH, "--rotate"], stdin=subprocess.DEVNULL, capture_output=True)
    return r.returncode == 2 and b"terminal" in r.stderr


checks: dict[str, Callable[[], bool]] = {
    "parse: db-tunnel hosts, ports, block users; wildcard/Match/other hosts skipped": lambda: [
        tuple(t) for t in m.load_tunnels(SSH_CONFIG)
    ] == EXPECTED_TUNNELS,
    "kind: staging/stg name segments are STAGING, postgres is not": lambda: [
        m.kind_of(n) for n in ("app-staging", "shop-stg", "db-tunnel-comm-staging", "postgres", "prod-ro")
    ] == ["STAGING", "STAGING", "STAGING", "PROD", "PROD"],
    "hooks/lib/db-registry derives the same alias/port/kind": registry_matches,
    "--tunnel picks by name and beats MYSQL_RO_PORT": lambda: plan(
        ["--tunnel=app-staging", *Q], env={"MYSQL_RO_PORT": "23306"}, keychain={"reader-b": "x"}
    ).tunnel.alias == "db-tunnel-app-staging",
    "MYSQL_RO_PORT alone picks its tunnel (the 2003 re-open target)": lambda: plan(
        Q, env={"MYSQL_RO_PORT": "23306"}, keychain={"reader-b": "x"}
    ).tunnel.alias == "db-tunnel-prod",
    "an unmapped port is refused, so it never starts ssh": lambda: refused(
        lambda: plan(Q, env={"MYSQL_RO_PORT": "23398"})
    ),
    "a remote host is refused": lambda: refused(
        lambda: plan(["--tunnel=prod", *Q], env={"MYSQL_RO_HOST": "10.0.0.1"})
    ),
    "no tunnel, port or login path is refused": lambda: refused(lambda: plan(Q)),
    "a shared port with no holder is refused without --tunnel": lambda: refused(
        lambda: plan(Q, env={"MYSQL_RO_PORT": "23309"}, keychain={"reader-a": "x"})
    ),
    "a shared port resolves to the tunnel holding it": lambda: plan(
        Q, env={"MYSQL_RO_PORT": "23309"}, keychain={"reader-a": "x"}, held=HELD_BY_PLATFORM
    ).tunnel.name == "platform",
    "--tunnel on a shared port its sibling holds is refused": lambda: refused(
        lambda: plan(["--tunnel=comm", *Q], keychain={"reader-a": "x"}, held=HELD_BY_PLATFORM)
    ),
    "--tunnel on a shared port is refused when lsof cannot say who holds it": lambda: refused(
        lambda: plan(["--tunnel=comm", *Q], env={"PATH": EMPTY_DIR}, keychain={"reader-a": "x"}, held=m.listeners)
    ),
    "...while an unshared port needs no lsof": lambda: plan(
        ["--tunnel=prod", *Q], env={"PATH": EMPTY_DIR}, keychain={"reader-b": "x"}, held=m.listeners
    ).tunnel.name == "prod",
    "after a 2003 re-open, a sibling holding the shared port stops the re-run": lambda: after_2003(
        HELD_BY_PLATFORM
    ) == (True, 1),
    "...and the tunnel's own forward gets its one re-run": lambda: after_2003(HELD_BY_COMM) == (False, 2),
    "a STAGING login path on a PROD port is refused": lambda: refused(
        lambda: plan(["--login-path=app-staging", *Q], env={"MYSQL_RO_PORT": "23306"})
    ),
    "a PROD login path on a STAGING tunnel is refused": lambda: refused(
        lambda: plan(["--tunnel=app-staging", "--login-path=prod-ro", *Q])
    ),
    "...even when the Keychain holds that tunnel's user": lambda: refused(
        lambda: plan(["--tunnel=app-staging", "--login-path=prod-ro", *Q], keychain={"reader-b": "x"})
    ),
    "a matching login path passes": lambda: "--login-path=app-ro"
    in plan(["--login-path=app-ro", *Q], env={"MYSQL_RO_PORT": "23307"}).cmd,
    "the label names kind, tunnel and port": lambda: plan(
        ["--tunnel=app-staging", *Q], keychain={"reader-b": "x"}
    ).label.startswith("STAGING app-staging @23308 "),
    "a Keychain password reaches the client only in a 0600 option file, deleted after":
        keychain_password_only_in_option_file,
    "a Keychain call ignores MYSQL_RO_CLIENT and a PATH shim": lambda: client_is_fixed(
        {"reader-b": "s3cret!"}
    ),
    "...and so does a login-path call, whose user can write": lambda: client_is_fixed({}),
    "RO_MYSQL_SECURITY no longer swaps the Keychain binary": security_env_ignored,
    "--login-path forces that login path over the tunnel user's Keychain password": lambda: (
        forced_login_path().cmd[1:2] == ["--login-path=app-ro"]
        and not any(a.startswith("--user=") for a in forced_login_path().cmd)
    ),
    "...and its user, not the tunnel's, is the one a 1045 records": lambda: forced_login_path().user
    == "reader-a",
    "no Keychain entry falls back to the same-port login path": lambda: "--login-path=prod-ro"
    in plan(["--tunnel=prod", *Q]).cmd,
    "an unannotated tunnel never borrows another port's login path": lambda: refused(
        lambda: plan(["--tunnel=app", *Q], env={"MYSQL_RO_LOGIN_PATH": "prod-ro"})
    ),
    "...nor does --tunnels --refresh, which shares the query's credential chain": refresh_uses_the_query_chain,
    "an inherited MYSQL_PWD never reaches a login-path call": lambda: "MYSQL_PWD"
    not in plan(["--tunnel=prod", *Q], env={"MYSQL_PWD": "stale"}).env,
    "keychain_set keeps the password out of argv and round-trips": keychain_set_round_trip,
    "keychain_set refuses a user name that could inject a command": lambda: m.keychain_set(
        "a -s x", "pw"
    ) is False,
    "a 1045 records '<user> <tunnel> <time>', cleared only by that user": auth_record,
    "rotation groups tunnels by DB user and lists the unknown": rotation_groups,
    "--rotate refuses without a terminal": rotate_needs_a_terminal,
}


def rejected(sql: str) -> bool:
    try:
        m.validate(sql)
    except SystemExit:
        return True
    return False


fails = 0
for sql in REJECT:
    if not rejected(sql):
        fails += 1
        print(f"FAIL allowed a write: {sql}")
for sql in ALLOW:
    if rejected(sql):
        fails += 1
        print(f"FAIL rejected a read: {sql}")
for name, check in checks.items():
    try:
        passed = check()
    except Exception as e:  # a missing function on an older copy is a failed case, not a crash
        passed = False
        name = f"{name}  ({type(e).__name__}: {e})"
    if not passed:
        fails += 1
        print(f"FAIL {name}")
total = len(REJECT) + len(ALLOW) + len(checks)
print(f"{total - fails}/{total} passed")
sys.exit(1 if fails else 0)
