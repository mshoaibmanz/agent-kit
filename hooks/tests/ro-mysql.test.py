"""ro-mysql's guards without a database: writes, tunnel registry, staging/prod cross-check,
Keychain credentials and auth-failure records. The ssh config is a fixture parameter;
`security`, lsof's holder and the mysql
client are fakes (module attributes or parameters), so no case reads the real Keychain, opens a
tunnel or reaches a database.

    python3 ~/.claude/hooks/tests/ro-mysql.test.py
    RO_MYSQL=<old copy> HOOKS_DIR=<old hooks dir> python3 …   # pre-fix
"""

import atexit
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator

sys.dont_write_bytecode = True
PATH = os.environ.get("RO_MYSQL", os.path.expanduser("~/.local/bin/ro-mysql"))
HOOKS = os.environ.get("HOOKS_DIR", os.path.expanduser("~/.claude/hooks"))


def load(name: str, path: str):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


m = load("ro_mysql", PATH)

TMP = tempfile.mkdtemp(prefix=".ro-mysql-test-", dir=os.environ.get("AGENT_TEST_FIXTURE_ROOT", os.path.expanduser("~")))
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
FAKE_3024 = os.path.join(TMP, "fake-3024")
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
if os.path.exists({FAKE_3024!r}):
    if sys.argv[-1].startswith("EXPLAIN "):
        print("id\\tselect_type\\ttable\\ttype\\tpossible_keys\\tkey\\trows\\tExtra")
        print("1\\tSIMPLE\\tevents\\tALL\\tNULL\\tNULL\\t48211934\\tUsing where")
        sys.exit(0)
    sys.exit("ERROR 3024 (HY000) at line 1: Query execution was interrupted, maximum statement execution time exceeded")
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

m.secret_store.SECURITY = FAKE_SECURITY
m.AUTH_FAILED = os.path.join(TMP, "db-auth-failed")
# Their defaults were bound to the real state file at import; a successful fake run clears through them.
m.note_auth_failure.__defaults__ = (m.AUTH_FAILED,)
m.clear_auth_failure.__defaults__ = (m.AUTH_FAILED,)
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


# Callers that take no logins parameter (show_databases under --tunnels --refresh, show_tunnels,
# rotate) look m.login_paths up when called, so they get the fixture too.
m.login_paths = login_paths


def plan(
    argv: list[str],
    env: dict | None = None,
    keychain: dict | None = None,
    held=None,
    logins=login_paths,
    ssh_config: str = SSH_CONFIG,
):
    """m.plan on the fixtures; `held` is a listeners dict, or a callable standing in for lsof."""
    with open(KEYCHAIN, "w") as f:
        json.dump(keychain or {}, f)
    reset = dict.fromkeys(("MYSQL_RO_HOST", "MYSQL_RO_PORT", "MYSQL_RO_LOGIN_PATH", "MYSQL_PWD"))
    listen = held if callable(held) else lambda: held or {}
    with environ(**(reset | (env or {}))):
        return m.plan(argv, ssh_config=ssh_config, logins=logins, held=listen)


def fixed_logins(*rows: tuple[str, str, str]) -> Callable[[], dict]:
    """A login-path fixture of (name, user, port) rows."""
    return lambda: {name: m.LoginPath(name, user, port) for name, user, port in rows}


# The unannotated app tunnel's port (23307) carries no login path in login_paths(); these give it one.
APP_READER = ("app-reader", "reader-c", "23307")
APP_RO = ("app-ro", "reader-a", "23306")
# A second user on app's port, sorting first: the default chain used to take it unchecked.
APP_OTHER = ("app-other", "reader-d", "23307")


def refused(fn: Callable[[], object]) -> bool:
    try:
        fn()
    except SystemExit as e:
        return e.code == 2
    return False


def refusal(fn: Callable[[], object]) -> str:
    """What a refused call printed on stderr; '' when it was not refused."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        stopped = refused(fn)
    return buf.getvalue() if stopped else ""


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


def keychain_password_only_in_option_file() -> bool:
    fresh_logs()
    m.run(plan(["--tunnel=prod", *Q], keychain={"reader-b@db-tunnel-prod": "s3cret!"}))
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


def store_miss_falls_back_to_env() -> bool:
    fresh_logs()
    with open(KEYCHAIN, "w") as f:
        json.dump({}, f)
    with environ(**{m.env_name("reader-a"): "from-env"}):
        return m.keychain_get("reader-a") == "from-env"


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
    """prod's user has a Keychain password, and the call forces a login path of that same user."""
    return plan(["--tunnel=prod", "--login-path=prod-ro", *Q], keychain={"reader-b@db-tunnel-prod": "x"})


def refresh_reads_login_paths_at_call_time() -> bool:
    """--tunnels --refresh on an unannotated tunnel tries the login paths m.login_paths returns now."""
    tunnels = m.load_tunnels(SSH_CONFIG)
    app = next(t for t in tunnels if t.name == "app")
    lp = m.LoginPath
    fresh_logs()
    saved = m.login_paths, m.listeners
    m.login_paths = lambda: {"app-reader": lp("app-reader", "reader-c", "23307")}
    m.listeners = lambda: HELD_BY_APP
    try:
        with environ(MYSQL_RO_LOGIN_PATH=None):
            m.show_databases(app, tunnels)
    finally:
        m.login_paths, m.listeners = saved
    return any("--login-path=app-reader" in rec["argv"] for rec in logged(CLIENT_LOG))


def after_2003(held: dict) -> tuple[bool, int]:
    """(refused, client runs) for a --tunnel=comm call that hits ERROR 2003 and re-opens."""
    fresh_logs()
    open(FAKE_2003, "w").close()
    try:
        p = plan(["--tunnel=comm", *Q], keychain={"reader-a@db-tunnel-comm": "x"})
        stopped = refused(lambda: m.execute(p, held=lambda: held, reopen=lambda alias: None))
    finally:
        os.remove(FAKE_2003)
    return stopped, len(logged(CLIENT_LOG))


def after_3024(argv: list[str]) -> tuple[str, list]:
    """(stderr, client runs) for a call whose statement hits the max_execution_time cap."""
    fresh_logs()
    open(FAKE_3024, "w").close()
    buf = io.StringIO()
    try:
        p = plan(["--tunnel=comm", *argv], keychain={"reader-a@db-tunnel-comm": "x"})
        with contextlib.redirect_stderr(buf):
            m.run_plan(p)
    finally:
        os.remove(FAKE_3024)
    return buf.getvalue(), logged(CLIENT_LOG)


def timeout_explains_in_the_same_session() -> bool:
    err, runs = after_3024(["-D", "appdb", "--vertical", "-e", "SELECT * FROM events WHERE j->>'$.a' = 1"])
    extra = err.split("maximum statement execution time exceeded\n", 1)[-1].splitlines()
    query, explain = runs[0]["argv"], runs[1]["argv"] if len(runs) > 1 else []
    init = [a for a in query if a.startswith("--init-command=")]
    return (
        len(runs) == 2
        and explain[-1] == "EXPLAIN FORMAT=TRADITIONAL SELECT * FROM events WHERE j->>'$.a' = 1"
        and init and init[0] in explain and "read_only = ON" in init[0]
        and "-D" in explain and "appdb" in explain and "--vertical" not in explain
        and len(extra) == 2
        and "events type=ALL key=NULL rows~48211934" in extra[0]
        and "id BETWEEN a AND b" in extra[1] and "bqro" in extra[1]
    )


def timeout_of_a_show_skips_explain() -> bool:
    err, runs = after_3024(["-e", "SHOW TABLES"])
    extra = err.split("maximum statement execution time exceeded\n", 1)[-1].splitlines()
    return len(runs) == 1 and len(extra) == 1 and "bqro" in extra[0]


def file_statement() -> bool:
    path = os.path.join(TMP, "q.sql")
    with open(path, "w") as f:
        f.write("SELECT id\nFROM t\nWHERE a = 1\n")
    argv = m.with_sql_file(["--tunnel=comm"], path)
    p = plan(argv, keychain={"reader-a@db-tunnel-comm": "x"})
    with open(path, "w") as f:
        f.write("SELECT 1; DELETE FROM t")
    bad = m.with_sql_file(["--tunnel=comm"], path)
    return (
        p.cmd[-1] == "SELECT id\nFROM t\nWHERE a = 1\n"
        and refused(lambda: plan(bad, keychain={"reader-a@db-tunnel-comm": "x"}))
        and refused(lambda: m.with_sql_file(["--tunnel=comm", *Q], path))
    )


def each_runs_one_call_per_value() -> bool:
    fresh_logs()
    rest, sql_file, values = m.split_extras(["--tunnel=comm", "--each=AE, S'A", "-e", "SELECT {} v FROM t WHERE c = {}"])
    p = plan(rest, keychain={"reader-a@db-tunnel-comm": "x"})
    with contextlib.redirect_stderr(io.StringIO()):
        rc = m.run_each(p, values)
    sqls = [rec["argv"][-1] for rec in logged(CLIENT_LOG)]
    return (
        rc == 0 and sql_file is None
        and sqls == ["SELECT 'AE' v FROM t WHERE c = 'AE'", "SELECT 'S''A' v FROM t WHERE c = 'S''A'"]
        and refused(lambda: m.run_each(plan(["--tunnel=comm", *Q], keychain={"reader-a@db-tunnel-comm": "x"}), ["x"]))
    )


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
    m.clear_auth_failure("reader-b", "db-tunnel-comm", path)
    m.clear_auth_failure("reader-a", "db-tunnel-platform", path)
    kept = os.path.exists(path)
    m.clear_auth_failure("reader-a", "db-tunnel-comm", path)
    return (user, alias) == ("reader-a", "db-tunnel-comm") and "T" in stamp and kept and not os.path.exists(path)


def keychain_is_per_tunnel() -> bool:
    """One user, two instances, two passwords: each tunnel gets its own; one without falls back."""
    both = {"reader-b@db-tunnel-prod": "p", "reader-b@db-tunnel-app-staging": "s"}
    only_staging = {"reader-b@db-tunnel-app-staging": "s"}
    fallback = plan(["--tunnel=prod", *Q], keychain=only_staging)
    return (
        plan(["--tunnel=prod", *Q], keychain=both).password == "p"
        and plan(["--tunnel=app-staging", *Q], keychain=both).password == "s"
        and fallback.password is None
        and "--login-path=prod-ro" in fallback.cmd
    )


def rotate_per_tunnel() -> bool:
    """--rotate asks once per tunnel and stores only what that tunnel accepted, under its account."""
    answers = {"prod": "p", "app-staging": "s", "comm": "", "platform": "wrong"}
    accepts = {"db-tunnel-prod": "p", "db-tunnel-app-staging": "s", "db-tunnel-platform": "right"}
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt.split()[0])
        return answers[prompt.split()[0]]

    def attempt(t, creds, sql):
        ok = accepts.get(t.alias) == creds.password
        return subprocess.CompletedProcess([], 0 if ok else 1, "", "" if ok else "ERROR 1045 (28000): denied")

    @contextlib.contextmanager
    def tunnel_up(t, tunnels):
        yield None

    with open(KEYCHAIN, "w") as f:
        json.dump({}, f)
    saved = m.tunnel_up, m.attempt
    m.tunnel_up, m.attempt = tunnel_up, attempt
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            m.rotate(m.load_tunnels(SSH_CONFIG), ask=ask, auth_path=os.path.join(TMP, "rotate-auth-failed"))
    finally:
        m.tunnel_up, m.attempt = saved
    with open(KEYCHAIN) as f:
        stored = json.load(f)
    return sorted(asked) == sorted(answers) and stored == {
        "reader-b@db-tunnel-prod": "p", "reader-b@db-tunnel-app-staging": "s"
    }


def multi_port_block() -> bool:
    """Two local ports under one alias would share one Keychain account: --rotate never asks for it,
    says so once even without a user, and --tunnels points at the fix instead of at --rotate. Two binds
    of one local port are one forward, asked for once."""
    path = os.path.join(TMP, "ssh_config_multi")
    with open(path, "w") as f:
        f.write(
            "Host db-tunnel-x\n  # ro-mysql: user=reader-x\n  LocalForward 23330 a:3306\n  LocalForward 23331 b:3306\n"
            "Host db-tunnel-y\n  LocalForward 23332 c:3306\n  LocalForward 23333 d:3306\n"
            "Host db-tunnel-z\n  # ro-mysql: user=reader-z\n"
            "  LocalForward 127.0.0.1:23334 e:3306\n  LocalForward [::1]:23334 e:3306\n"
        )
    with open(KEYCHAIN, "w") as f:
        json.dump({}, f)
    asked: list[str] = []
    rotated, listed = io.StringIO(), io.StringIO()
    saved = m.listeners, m.read_cache
    m.listeners, m.read_cache = (lambda: {}), (lambda: {})
    try:
        with contextlib.redirect_stdout(rotated):
            m.rotate(m.load_tunnels(path), ask=lambda prompt: asked.append(prompt) or "")
        with contextlib.redirect_stdout(listed):
            m.show_tunnels(m.load_tunnels(path), do_refresh=False)
    finally:
        m.listeners, m.read_cache = saved
    skips = rotated.getvalue().splitlines()
    listing = listed.getvalue()
    return (
        asked == ["z (reader-z) password, blank skips: "]
        and skips == [
            "skip db-tunnel-x: it has several LocalForwards; give each forward its own alias",
            "skip db-tunnel-y: it has several LocalForwards; give each forward its own alias",
        ]
        and "db-tunnel-x has several LocalForwards" in listing
        and "db-tunnel-y has several LocalForwards" in listing
        and "db-tunnel-z has several" not in listing
        and [line for line in listing.splitlines() if line.startswith("no Keychain")]
        == ["no Keychain password or login path for z: run `ro-mysql --rotate` in a terminal"]
    )


def tunnels_counts_the_env_login_path() -> bool:
    """With no login path readable, --tunnels counts MYSQL_RO_LOGIN_PATH as the query does, and never
    asks the Keychain for a password (`-w`)."""
    with open(KEYCHAIN, "w") as f:
        json.dump({}, f)
    with contextlib.suppress(FileNotFoundError):
        os.remove(KEYCHAIN + ".argv")
    saved = m.login_paths, m.listeners, m.read_cache
    m.login_paths = fixed_logins()
    m.listeners, m.read_cache = (lambda: {}), (lambda: {})
    out = io.StringIO()
    try:
        with environ(MYSQL_RO_LOGIN_PATH="prod-ro"), contextlib.redirect_stdout(out):
            m.show_tunnels(m.load_tunnels(SSH_CONFIG), do_refresh=False)
    finally:
        m.login_paths, m.listeners, m.read_cache = saved
    lines = out.getvalue().splitlines()
    calls = [json.loads(line) for line in open(KEYCHAIN + ".argv")]
    return (
        "no Keychain password for prod, comm, platform (login path used): run `ro-mysql --rotate` in a terminal"
        in lines
        and "no Keychain password or login path for app-staging: run `ro-mysql --rotate` in a terminal" in lines
        and not any("-w" in c for c in calls)
    )


def tunnels_lists_missing_passwords_per_tunnel() -> bool:
    """--tunnels names each tunnel with no Keychain entry, split by whether a login path covers it."""
    with open(KEYCHAIN, "w") as f:
        json.dump({"reader-b@db-tunnel-prod": "x"}, f)
    saved = m.login_paths, m.listeners, m.read_cache
    m.login_paths = fixed_logins(("app-staging", "reader-b", "23308"))
    m.listeners, m.read_cache = (lambda: {}), (lambda: {})
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            m.show_tunnels(m.load_tunnels(SSH_CONFIG), do_refresh=False)
    finally:
        m.login_paths, m.listeners, m.read_cache = saved
    lines = out.getvalue().splitlines()
    return (
        "no Keychain password for app-staging (login path used): run `ro-mysql --rotate` in a terminal" in lines
        and "no Keychain password or login path for comm, platform: run `ro-mysql --rotate` in a terminal" in lines
    )


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
        tuple(t)[:3] for t in m.load_tunnels(SSH_CONFIG)
    ] == EXPECTED_TUNNELS,
    "kind: staging/stg name segments are STAGING, postgres is not": lambda: [
        m.kind_of(n) for n in ("app-staging", "shop-stg", "db-tunnel-comm-staging", "postgres", "prod-ro")
    ] == ["STAGING", "STAGING", "STAGING", "PROD", "PROD"],
    "--tunnel picks by name and beats MYSQL_RO_PORT": lambda: plan(
        ["--tunnel=app-staging", *Q], env={"MYSQL_RO_PORT": "23306"}, keychain={"reader-b@db-tunnel-app-staging": "x"}
    ).tunnel.alias == "db-tunnel-app-staging",
    "MYSQL_RO_PORT alone picks its tunnel (the 2003 re-open target)": lambda: plan(
        Q, env={"MYSQL_RO_PORT": "23306"}, keychain={"reader-b@db-tunnel-prod": "x"}
    ).tunnel.alias == "db-tunnel-prod",
    "an unmapped port is refused, so it never starts ssh": lambda: refused(
        lambda: plan(Q, env={"MYSQL_RO_PORT": "23398"})
    ),
    "a remote host is refused": lambda: refused(
        lambda: plan(["--tunnel=prod", *Q], env={"MYSQL_RO_HOST": "10.0.0.1"})
    ),
    "no tunnel, port or login path is refused": lambda: refused(lambda: plan(Q)),
    "a shared port with no holder is refused without --tunnel": lambda: refused(
        lambda: plan(Q, env={"MYSQL_RO_PORT": "23309"}, keychain={"reader-a@db-tunnel-comm": "x"})
    ),
    "a shared port resolves to the tunnel holding it": lambda: plan(
        Q, env={"MYSQL_RO_PORT": "23309"}, keychain={"reader-a@db-tunnel-platform": "x"}, held=HELD_BY_PLATFORM
    ).tunnel.name == "platform",
    "--tunnel on a shared port its sibling holds is refused": lambda: refused(
        lambda: plan(["--tunnel=comm", *Q], keychain={"reader-a@db-tunnel-comm": "x"}, held=HELD_BY_PLATFORM)
    ),
    "--tunnel on a shared port is refused when lsof cannot say who holds it": lambda: refused(
        lambda: plan(["--tunnel=comm", *Q], env={"PATH": EMPTY_DIR}, keychain={"reader-a@db-tunnel-comm": "x"}, held=m.listeners)
    ),
    "...while an unshared port needs no lsof": lambda: plan(
        ["--tunnel=prod", *Q], env={"PATH": EMPTY_DIR}, keychain={"reader-b@db-tunnel-prod": "x"}, held=m.listeners
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
        lambda: plan(["--tunnel=app-staging", "--login-path=prod-ro", *Q], keychain={"reader-b@db-tunnel-app-staging": "x"})
    ),
    "a login path of its port's one user passes on an unannotated tunnel": lambda: "--login-path=app-reader"
    in plan(["--login-path=app-reader", *Q], env={"MYSQL_RO_PORT": "23307"}, logins=fixed_logins(APP_READER)).cmd,
    "--login-path on an unannotated tunnel with no user to check it against is refused": lambda: refused(
        lambda: plan(["--tunnel=app", "--login-path=app-ro", *Q])
    ),
    "...and the refusal names the annotation to add": lambda: "# ro-mysql: user=<db user>" in refusal(
        lambda: plan(["--tunnel=app", "--login-path=app-ro", *Q])
    ),
    "...and so is one whose port's login paths name another user": lambda: refused(
        lambda: plan(["--tunnel=app", "--login-path=app-ro", *Q], logins=fixed_logins(APP_READER, APP_RO))
    ),
    "...or two users": lambda: refused(
        lambda: plan(["--tunnel=app", "--login-path=app-reader", *Q], logins=fixed_logins(APP_READER, APP_OTHER))
    ),
    "an unannotated tunnel whose port's login paths name two users is refused without --login-path":
        lambda: refused(lambda: plan(["--tunnel=app", *Q], logins=fixed_logins(APP_READER, APP_OTHER))),
    "...and the refusal names the annotation, counting the users without naming them": lambda: (
        lambda msg: "# ro-mysql: user=<db user>" in msg and "2 login-path users" in msg and "reader-" not in msg
    )(refusal(lambda: plan(["--tunnel=app", *Q], logins=fixed_logins(APP_READER, APP_OTHER)))),
    "...and so is one whose port's only login path names no user": lambda: refused(
        lambda: plan(["--tunnel=app", *Q], logins=fixed_logins(("app-anon", None, "23307")))
    ),
    "...while its port's one user still needs no annotation": lambda: "--login-path=app-reader"
    in plan(["--tunnel=app", *Q], logins=fixed_logins(APP_READER)).cmd,
    "the label names kind, tunnel and port": lambda: plan(
        ["--tunnel=app-staging", *Q], keychain={"reader-b@db-tunnel-app-staging": "x"}
    ).label.startswith("STAGING app-staging @23308 "),
    "a Keychain password reaches the client only in a 0600 option file, deleted after":
        keychain_password_only_in_option_file,
    "a Keychain call ignores MYSQL_RO_CLIENT and a PATH shim": lambda: client_is_fixed(
        {"reader-b@db-tunnel-prod": "s3cret!"}
    ),
    "...and so does a login-path call, whose user can write": lambda: client_is_fixed({}),
    "RO_MYSQL_SECURITY no longer swaps the Keychain binary": security_env_ignored,
    "a store without the item falls back to the account's environment variable": store_miss_falls_back_to_env,
    "two tunnel aliases that differ only in punctuation get two environment names": lambda: len(
        {m.env_name("reader@db-tunnel-prod-a"), m.env_name("reader@db-tunnel-prod_a"), m.env_name("Reader@db-tunnel-prod-a")}
    ) == 3,
    "--login-path of another user than the tunnel's annotated one is refused": lambda: refused(
        lambda: plan(["--tunnel=prod", "--login-path=app-ro", *Q], keychain={"reader-b@db-tunnel-prod": "x"})
    ),
    "...and so is one naming no known login path": lambda: refused(
        lambda: plan(["--tunnel=prod", "--login-path=nope", *Q], keychain={"reader-b@db-tunnel-prod": "x"})
    ),
    "--login-path of the tunnel's own user forces it over the Keychain password": lambda: (
        forced_login_path().cmd[1:2] == ["--login-path=prod-ro"]
        and not any(a.startswith("--user=") for a in forced_login_path().cmd)
        and forced_login_path().user == "reader-b"
    ),
    "no Keychain entry falls back to the same-port login path": lambda: "--login-path=prod-ro"
    in plan(["--tunnel=prod", *Q]).cmd,
    "an unannotated tunnel never borrows another port's login path": lambda: refused(
        lambda: plan(["--tunnel=app", *Q], env={"MYSQL_RO_LOGIN_PATH": "prod-ro"})
    ),
    "...nor does --tunnels --refresh, which shares the query's credential chain": refresh_uses_the_query_chain,
    "--tunnels --refresh reads the login paths when it runs, not when ro-mysql loaded":
        refresh_reads_login_paths_at_call_time,
    "an inherited MYSQL_PWD never reaches a login-path call": lambda: "MYSQL_PWD"
    not in plan(["--tunnel=prod", *Q], env={"MYSQL_PWD": "stale"}).env,
    "keychain_set keeps the password out of argv and round-trips": keychain_set_round_trip,
    "keychain_set refuses a user name that could inject a command": lambda: m.keychain_set(
        "a -s x", "pw"
    ) is False,
    "a 1045 records '<user> <tunnel> <time>', cleared only by that user on that tunnel": auth_record,
    "a Keychain password belongs to one tunnel, not to its DB user": keychain_is_per_tunnel,
    "--rotate asks per tunnel and stores only what that tunnel accepted": rotate_per_tunnel,
    "a Host block with several forwards is skipped by --rotate and flagged by --tunnels": multi_port_block,
    "--tunnels counts MYSQL_RO_LOGIN_PATH like the query and reads no password": tunnels_counts_the_env_login_path,
    "unavailable credentials name the account and require access checks before rotation": lambda: all(
        fragment in refusal(lambda: plan(["--tunnel=comm", *Q], logins=fixed_logins()))
        for fragment in (
            "reader-a@db-tunnel-comm", "missing or Keychain access denied", "trusted terminal",
            "only after confirming",
        )
    ),
    "--tunnels lists each tunnel missing a Keychain password, and whether a login path covers it":
        tunnels_lists_missing_passwords_per_tunnel,
    "rotation groups tunnels by DB user and lists the unknown": rotation_groups,
    "--rotate refuses without a terminal": rotate_needs_a_terminal,
    "a bare tunnel name is refused, not read as a database on the default tunnel": lambda: "pass --tunnel=comm"
    in refusal(lambda: plan(["comm", *Q], logins=fixed_logins())),
    "-D <db> whose name matches a tunnel is still a database": lambda: "is a tunnel"
    not in refusal(lambda: plan(["--tunnel=app", "-D", "comm", *Q], logins=fixed_logins(APP_READER))),
    "the session cap defaults to 30s": lambda: any(
        a.endswith("max_execution_time = 30000")
        for a in plan(["--tunnel=comm", *Q], env={"MYSQL_RO_MAX_MS": None}, keychain={"reader-a@db-tunnel-comm": "x"}).cmd
    ),
    "a 3024 EXPLAINs the SELECT in the same read-only session and prints two lines": timeout_explains_in_the_same_session,
    "...and a 3024 on a non-SELECT prints only the next step": timeout_of_a_show_skips_explain,
    "a multi-statement refusal names the one-call-per-statement form": lambda: "own `ro-mysql -e" in refusal(
        lambda: m.validate("SELECT 1; SELECT 2")
    ),
    "--file reads the statement and validates it like -e; --file with -e is refused": file_statement,
    "--each runs one call per value with {} as a quoted literal, and needs a {}": each_runs_one_call_per_value,
}


def rejected(sql: str) -> bool:
    try:
        m.validate(sql)
    except SystemExit:
        return True
    return False


def run(checks: dict[str, Callable[[], bool]], reject: tuple[str, ...] = (), allow: tuple[str, ...] = ()) -> int:
    """Run each check (and the write/read classifier cases); print every failure and the tally."""
    fails = 0
    for sql in reject:
        if not rejected(sql):
            fails += 1
            print(f"FAIL allowed a write: {sql}")
    for sql in allow:
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
    total = len(reject) + len(allow) + len(checks)
    print(f"{total - fails}/{total} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run(checks, REJECT, ALLOW))
