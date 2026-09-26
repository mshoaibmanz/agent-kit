#!/usr/bin/env python3
"""Payload tests for bash-guards, every guard set (no argument).

A bash-3.2 regression in a hook fails silently and can sit undetected for weeks. Run this after
ANY edit to bash-guards:

    bash tests/run-tests.sh

HOOKS_DIR=<dir> tests another copy of the hooks, e.g. the pre-fix ones to prove a case fails.
"""
import ast
import atexit
import json
import os
import sys
import tempfile
import shutil
import subprocess

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
H = os.path.join(
    os.environ.get("HOOKS_DIR") or os.path.join(_REPO, "plugins", "guard-rails", "hooks"), "bash-guards"
)


def _hook_const(name):
    """Read a constant out of the hook so the suite cannot drift from it."""
    for line in open(H):
        if line.startswith(f"{name}="):
            return int(line.split("=", 1)[1].split()[0])
    raise AssertionError(f"{name} not found in {H}")


BYTE_CAP = _hook_const("BYTE_CAP")
LINE_CAP = _hook_const("LINE_CAP")
# Hard-coded on purpose. Deriving the cases from the constant made them track it, so mutating
# LINE_CAP anywhere in 50..509 left the suite green — it pinned the off-by-one but never the
# value. Changing either constant should fail HERE, deliberately, not pass silently.
assert BYTE_CAP == 25000, f"BYTE_CAP changed to {BYTE_CAP}; update the byte cases below too"
assert LINE_CAP == 300, f"LINE_CAP changed to {LINE_CAP}; update the line cases below too"

# Every fixture is GENERATED. Pointing BIG at a real doc under ~/.claude/local gave
# it 843 bytes of margin over BYTE_CAP, so trimming one paragraph from a doc would have failed
# the whole suite before a single case ran. Under $HOME so the ~ / $HOME expansion cases stay
# meaningful; removed on exit.
FIX = tempfile.mkdtemp(prefix=".bash-guards-fixtures-", dir=os.path.expanduser("~"))
atexit.register(shutil.rmtree, FIX, ignore_errors=True)


def _fixture(name, content):
    q = os.path.join(FIX, name)
    with open(q, "w") as fh:
        fh.write(content)
    return q


BIG = _fixture("big.md", "".join(f"line {i} " + "x" * 60 + "\n" for i in range(500)))
SMALL = _fixture("small.txt", "a short file\n" * 20)
TINY = _fixture("tiny.txt", "a\nb\nc\n")
# The two shapes that broke the byte estimator this guard used to carry.
ONE_LINE = _fixture("oneline.log", "x" * 5_000_000)            # 5MB, ZERO newlines
UNEVEN = _fixture("uneven.log", "short\n" * 1000 + "y" * 3_000_000 + "\n")
# guard context-dumps once measured spans by eval'ing a command built around the filename.
# It no longer does, but the property is worth keeping pinned. Space-free on purpose: paths
# are whitespace-separated, so a filename CONTAINING a space is out of scope by design.
EVIL = _fixture("a`whoami`b.log", "z" * 40_000)
# Short lines first, fat lines later: a line count taken from the FRONT of the file says
# nothing about a span that starts at line 13001. This is the shape that made the sed branch
# looser than the revision it replaced.
NORMAL_LINES = _fixture("normal.log", ("line " + "x" * 60 + "\n") * 2000)
UNREADABLE = _fixture("noread.log", "q" * 40_000)
os.chmod(UNREADABLE, 0o000)
atexit.register(lambda: os.path.exists(UNREADABLE) and os.chmod(UNREADABLE, 0o600))
SHORTFAT = _fixture("shortthenfat.log", "a\n" * 13000 + ("z" * 99999 + "\n") * 500)
# First line alone exceeds BYTE_CAP: a cheap span AFTER it must still be allowed.
FATFIRST = _fixture("fatfirst.log", "F" * 60_000 + "\n" + "b\n" * 50)

assert os.path.getsize(BIG) > BYTE_CAP, "BIG fixture is not over BYTE_CAP"
assert os.path.getsize(SMALL) < BYTE_CAP, "SMALL fixture is not under BYTE_CAP"

# Split client/driver names so this file's own text cannot trip the guards when it is echoed
# into a Bash call. This became LOAD-BEARING when raw-client stopped parsing: that guard now
# denies on the name appearing anywhere outside an inspection span, and a heredoc carrying
# these cases is not an inspection span. (It was merely cosmetic before that change.)
_PYM = "py" + "mysql"
_M = "my" + "sql"

CASES = [
    # --- py-driver -------------------------------------------------------------
    # A driver imported from a script bypasses the wrapper entirely.
    ("allow", f"python3 -c \"p=r'{_PYM}|MySQLdb'\"",             "driver NAME in a regex literal"),
    ("deny",  f"python3 -c 'import {_PYM}'",                     "real driver import"),
    ("deny",  f"python3 -c \"create_engine('mysql+{_PYM}://x')\"", "sqlalchemy mysql url"),
    # --- commit-trailers -------------------------------------------------------
    ("deny",  "git commit -m 'test'",             "commit without trailers"),
    ("deny",  "git -C \"$WT\" commit -m 'test'",  "the quoted worktree form without trailers"),
    # --- destructive-git -------------------------------------------------------
    ("ask",   "git push origin foo --force",      "force push, flag last"),
    ("ask",   "git reset --hard HEAD~1",          "reset --hard"),
    # --- two-dot-diff ----------------------------------------------------------
    ("ask",   "git diff origin/master..HEAD",     "two-dot diff vs base"),
    ("allow", "git diff origin/master...HEAD",    "three-dot diff vs base"),
    # --- romysql-env -----------------------------------------------------------
    # ro-mysql refuses a remote host and a staging/prod login-path cross itself
    # (ro-mysql.test.py), so the hook lets those through; the client swap and the
    # Keychain reads are this hook's alone.
    ("allow", f"MYSQL_RO_PORT=23308 'ro-{_M}' --login-path=app-ro -e 'SELECT 1'", "staging/prod cross: ro-mysql refuses it"),
    ("allow", f"MYSQL_RO_HOST=10.0.0.1 </dev/null ro-{_M} -e 'SELECT 1'", "host redirect: ro-mysql refuses it"),
    ("allow", "python3 scripts/reconcile.py --login-path app-staging --dry-run", "a script's own --login-path app-staging"),
    ("allow", f"MYSQL_RO_HOST=127.0.0.1 ro-{_M} -e 'SELECT 1'\nMYSQL_RO_HOST=127.0.0.1 ro-{_M} -e 'SELECT 2'", "two local-host lines are not a doubled host"),
    ("deny",  f"MYSQL_RO_CLIENT=/tmp/evil ro-{_M} -e 'SELECT 1'", "binary swapped AFTER validation"),
    ("deny",  f"MYSQL_RO_CLIENT+=/tmp/evil ro-{_M} -e 'SELECT 1'", "+= onto an unset var is the same swap"),
    ("deny",  f"env MYSQL_RO_CLIENT=/tmp/evil ro-{_M} -e 'SELECT 1'", "...via env"),
    ("deny",  f"export MYSQL_RO_CLIENT=/tmp/evil; ro-{_M} -e 'SELECT 1'", "...via export"),
    ("deny",  f"read -r MYSQL_RO_CLIENT <<< /tmp/evil; export MYSQL_RO_CLIENT; ro-{_M} -e 'SELECT 1'", "...via read, no '='"),
    ("allow", f"grep -n MYSQL_RO_CLIENT ~/.local/bin/ro-{_M}", "grep for the variable"),
    ("allow", f"MYSQL_RO_PORT=23307 ro-{_M} --login-path=app-ro -e 'SELECT 1'", "wrapper with env prefix"),
    ("deny",  "security find-generic-password -s ro-mysql -a reader -w", "Keychain password printed"),
    ("deny",  "/usr/bin/security dump-keychain -d", "Keychain dumped, abs path"),
    ("deny",  "echo $(security 'find-generic-password' -w -a reader)", "quoted subcommand in a substitution"),
    ("deny",  "echo 'find-generic-password -s ro-mysql -a u -w' | security -i", "subcommand piped into security -i"),
    ("deny",  "security -i < /tmp/cmds", "interactive security, subcommand from a file"),
    ("deny",  "security -qp 'k> ' < /tmp/cmds", "-p implies -i, in a flag cluster"),
    ("deny",  "security find-internet-password -s host -w", "internet password printed"),
    ("deny",  "bash -c 'security find-generic-password -s ro-mysql -w'", "security named only inside bash -c"),
    ("allow", "grep -n find-generic-password ~/.local/bin/ro-mysql", "grep for the subcommand"),
    ("allow", "security list-keychains", "a harmless security subcommand"),
    ("allow", "mkdir -p docs/security", "-p beside the word security, not invoking it"),
    # --- prod-write ------------------------------------------------------------
    # The hook's SQL-verb text check was removed on 2026-09-13 (it false-fired on SELECTs):
    # ro-mysql and bqro validate the real SQL and reject writes. The hook lets these through.
    ("allow", f"true&&ro-{_M} -e 'DELETE FROM t'",       "write verb left to the wrapper"),
    ("allow", "true&&bqro 'SELECT 1'",                   "bqro without --project_id (wrapper defaults it)"),
    ("allow", f"ro-{_M} -e 'RENAME TABLE a TO b'",       "RENAME left to the wrapper (ro-mysql.test.py covers it)"),
    ("allow", f"cat ~/bin/ro-{_M}",                      "cat the wrapper"),
    ("allow", f"ro-{_M} -e \"SELECT x WHERE n LIKE '%delete%'\"", "SELECT mentioning delete"),
    ("allow", f"ro-{_M} -D appdb -e 'SELECT 1'",                 "the sanctioned wrapper"),
    # --- cmd-tokens ------------------------------------------------------------
    # lib/cmd-tokens feeds EVERY command-position guard. These pin that a glued
    # separator, a subshell, or eval cannot hide a command from it — and that
    # ordinary shell (brace expansion, find -exec, a hook eval) still passes.
    ("allow", "(echo hi)",                               "ordinary subshell still fine"),
    ("allow", "env -u VIRTUAL_ENV run-tests tests/x.py", "wrapper stepping still works"),
    ("allow", "cp {a,b}.txt /tmp/",                      "brace expansion"),
    ("allow", "find . -name '*.py' -exec grep -l x {} \\;", "find -exec {} \\;"),
    ("allow", 'eval "$(direnv hook zsh)"',               "eval a shell hook"),
    ("allow", f"echo $HOME/bin/{_M}",                    "a $VAR path naming a client"),
    ("allow", f"docker run --rm {_M}:8 --version",       "docker image named mysql"),
    ("allow", f"echo $HOME/bin/{_M}",                    "echo a path"),
    ("allow", f"docker run --rm {_M}:8 --version",       "docker image tag"),
    # --- raw-client ------------------------------------------------------------
    # Any mention of a raw client. A thermo-nuclear audit found 38 spellings that
    # defeated command-position parsing, from one apostrophe to an English
    # contraction — so this guard stopped parsing and now denies on the NAME.
    # Do not 'simplify' it back into a parser without re-running every case here.
    ("deny",  f"command {_M}check appdb",                "command mysqlcheck (was allowed)"),
    ("deny",  "command mariadb-dump appdb",              "command mariadb-dump (was allowed)"),
    ("deny",  "command mariadb-admin status",            "command mariadb-admin (was allowed)"),
    ("allow", f'echo "$(command -v {_M})"',              "echoing the path"),
    ("allow", f'[ -x "$(command -v {_M})" ] && echo ok', "testing the path"),
    ("deny",  f"$(command -v {_M}) -e 'SELECT 1'",       "but executing it still denies"),
    ("deny",  f"({_M} -e 'SELECT 1')",                   "subshell bypassed guard 1c"),
    ("deny",  f"eval {_M} -e 'SELECT 1'",                "eval bypassed guard 1c"),
    ("deny",  f"true&&{_M} -e 'DROP TABLE t'",           "separator glued between words"),
    ("deny",  f"echo x&&({_M} -e 'DROP TABLE t')",       "glued separator then subshell"),
    ("deny",  f"$({_M} -e 'DROP TABLE t')",              "command substitution"),
    ("deny",  f"x=$({_M} -e 'DROP TABLE t')",            "assignment from substitution"),
    ("deny",  f"eval $(command -v {_M}) -e 'DROP TABLE t'", "eval defeated the anchor"),
    ("allow", f"ls {{{_M},pgsql}}.cnf",                  "brace expansion naming a client"),
    ("deny",  f"'{_M}' -e 'DROP TABLE t'",              "quoted command name — ONE apostrophe"),
    ("deny",  f'"{_M}" -e \'DROP TABLE t\'',            "double-quoted name"),
    ("deny",  f"\\{_M} -e 'DROP TABLE t'",              "backslash-escaped name"),
    ("deny",  f"echo \"it's fine\"; {_M} -e 'DROP TABLE t'", "a contraction desyncs the quote sed"),
    ("deny",  f"if true;then {_M} -e 'DROP TABLE t';fi", ";then glued (spaced form denied)"),
    ("deny",  f"for i in 1;do {_M} -e 'x';done",         ";do glued"),
    ("deny",  f"</dev/null {_M} -e 'DROP TABLE t'",      "leading redirection"),
    ("deny",  f"{_M}<q.sql",                             "redirect glued to the name"),
    ("deny",  f"{_M}|less",                              "pipe glued to the name"),
    ("deny",  f"cat <({_M} -e 'DROP TABLE t')",          "process substitution"),
    ("deny",  f"timeout 10 {_M} -e 'DROP TABLE t'",      "wrapper not on the transparent list"),
    ("deny",  f"nice -n 5 {_M} -e 'x'",                  "...one needing argument arity"),
    ("deny",  f"bash -c '{_M} -e \"DROP TABLE t\"'",     "bash -c"),
    ("deny",  f"C={_M}; $C -e 'DROP TABLE t'",           "command name via a variable"),
    ("deny",  f"python3 -c \"import os; os.system('{_M} -e x')\"", "via a language runtime"),
    ("deny",  f"ssh host '{_M} -e \"x\"'",               "ssh"),
    ("deny",  f"find . -maxdepth 0 -exec {_M} -e 'x' \\;", "find -exec"),
    ("allow", f"ls -la /opt/homebrew/bin/{_M}",          "ls the binary"),
    ("deny",  f"{_M} -e 'SELECT 1'",                             "bare client"),
    ("deny",  f"/opt/homebrew/bin/{_M} -e 'SELECT 1'",           "abs path (the real PATH entry)"),
    ("deny",  f"MYSQL_PWD=x {_M} -e 'SELECT 1'",                 "env-var prefix defeats prefix match"),
    ("deny",  f"/opt/homebrew/bin/{_M}dump appdb > /tmp/d.sql",  "dump via abs path"),
    ("deny",  f"command {_M} -e 'SELECT 1'",                     "'command' builtin prefix"),
    ("deny",  f"$(command -v {_M}) -e 'SELECT 1'",               "path resolved at runtime"),
    ("deny",  "mariadb -e 'SELECT 1'",                          "mariadb client"),
    ("allow", f"command -v {_M}",                                "checking the client exists"),
    ("allow", f"which {_M}",                                     "which"),
    ("allow", f"ls -la /opt/homebrew/bin/{_M}",                  "listing the binary"),
    # --- context-dumps ---------------------------------------------------------
    # Half of this setup's context spend. ONE_LINE and UNEVEN (built in main) are
    # the two file shapes that broke the byte estimator this guard used to carry:
    # zero newlines divided by zero, one very long line false-denied.
    # These were stranded in main() because their fixtures were built there; the fixtures are
    # module-level now, so they belong with the rest of their guard.
    # A span's WIDTH says nothing about bytes when the lines are huge. Each of these was
    # allowed by the size-gate rewrite until the gate learned to bound as well as skip; the
    # emitted byte counts are measured, not estimated.
    # Round 6: every span form now measures bytes directly rather than inferring them from a
    # line count sampled at the front of the file. Each of these was allowed by the previous
    # revision; the byte figures in the labels are measured, not estimated.
    ("deny",  f"sed -n '13001,13500p' {SHORTFAT}",       "deep window of fat lines emits 50MB"),
    ("deny",  f"sed -n '13001,13301p' {SHORTFAT}",       "...narrower, same shape, 30MB"),
    ("deny",  f"sed -n '1001,1001p' {UNEVEN}",           "ONE line, but that line is 3MB"),
    ("allow", f"sed -n '2,10p' {FATFIRST}",              "cheap span after a 60KB first line"),
    ("deny",  f"head -n 5 {SMALL} {ONE_LINE}",           "$f is the LARGEST file, not the first"),
    ("deny",  f"tail -n 1 {SMALL} {UNEVEN}",             "...same via tail"),
    ("deny",  f"tail -n 14000000 {NORMAL_LINES}",        "an absurd count is capped before measuring"),
    ("deny",  f"head -n 5000 {UNREADABLE}",              "an unmeasurable file falls back to width"),
    ("deny",  f"tail -n 5000 {UNREADABLE}",              "...tail too, rather than reading 0 bytes"),
    ("deny",  f"head -n 5 {ONE_LINE}",                   "n=5 on a no-newline 5MB file emits 5MB"),
    ("deny",  f"sed -n '1,10p' {ONE_LINE}",              "...a 10-line span does too"),
    ("deny",  f"tail -n 5 {ONE_LINE}",                   "...and so does tail"),
    ("deny",  f"tail -n 1 {UNEVEN}",                     "one line, but that line is 3MB"),
    ("allow", f"sed -n '400,410p' {BIG}",                "a narrow window deep in a big file is cheap"),
    # LINE_CAP decides only when no readable file is named. Mutating it anywhere in 51..399
    # previously left the suite green, so it was a constant nothing tested.
    ("allow", f"head -n 300 /nonexistent/x.log",  "at LINE_CAP, unknown file"),
    ("deny",  f"head -n 301 /nonexistent/x.log", "one over LINE_CAP, unknown file"),
    ("deny",  f"cat {EVIL}",                             "a crafted filename is denied, not executed"),
    ("deny",  f"head -n 900 {EVIL}",                     "...same through the span branch"),
    ("allow", f"sed -n '1,900p' {TINY}",                 "a wide span cannot exceed a 3-line file"),
    ("allow", f"head -n 1000 {TINY}",                    "...nor can a big -n"),
    ("deny",  f"head -n 1000 {ONE_LINE}",                "5MB with zero newlines (wc -l == 0 divided by zero)"),
    ("deny",  f"tail -n 500 {ONE_LINE}",                 "...same via tail"),
    ("allow", f"head -n 200 {UNEVEN}",                   "200 short lines is ~4KB, not the prorated 600KB"),
    ("deny",  f"head -n 1002 {UNEVEN}",                  "...but reaching the 3MB line denies"),
    ("deny",  f"cat {BIG}",                       "big file, no cap"),
    ("allow", f"cat {BIG} | head -50",            "big file, capped"),
    ("allow", f"cat {BIG} | grep -n Serena",      "big file, grepped"),
    ("allow", f"cat {BIG} | cat",                 "deliberate | cat escape hatch"),
    ("allow", f"cat {SMALL}",                     "small file"),
    ("deny",  "sed -n '660,1169p' src/x.py",      "sed span > 400 lines"),
    ("allow", "sed -n '660,700p' src/x.py",       "sed span < 400 lines"),
    ("deny",  "head -n 2000 some.log",            "head -n 2000"),
    ("allow", "head -50 some.log",                "head -50"),
    ("deny",  f'cat "{BIG}"',                            "double-quoted path"),
    ("deny",  f"cat {BIG.replace(os.path.expanduser('~'), '~')}", "tilde path"),
    ("deny",  f'cat $HOME{BIG[len(os.path.expanduser("~")):]}', "$HOME path"),
    ("deny",  f"cat {BIG} 2>/dev/null",                  "2>/dev/null is not a cap"),
    ("deny",  f"for f in a b; do cat {BIG}; done",        "separator glued to the path"),
    ("deny",  f"cat {BIG} | catfish",                    "| catfish must not read as | cat"),
    ("allow", f'cat {BIG} >"/tmp/out.txt"',              "quoted stdout redirect target"),
    ("allow", f"cat {BIG} | tee /tmp/x",                 "| tee"),
    ("allow", f"cat {BIG} | less",                       "| less"),
    ("allow", f"cat {BIG} | pbcopy",                     "| pbcopy (never reaches context)"),
    ("allow", f"cat {BIG} | column -t",                  "| column"),
    ("allow", f"cat {BIG} > /tmp/out.txt",               "plain stdout redirect"),
    ("allow", "head -c 4000 some.log",                   "head -c is bytes, not lines"),
    ("allow", "grep -A 500 pat file",                    "grep -A 500 is not head -n"),
    ("allow", "git cat-file -p HEAD",                    "git cat-file is not cat"),
    ("deny",  f"cat {BIG} 2>>/dev/null",                 "2>> must not read as a stdout redirect"),
    ("deny",  f"cat {BIG}; awk '$1 > 5' /tmp/t",         "a quoted > must not disarm the guard"),
    ("deny",  f"cat {BIG}; echo done > /tmp/x",          "a redirect in an UNRELATED segment"),
    ("deny",  f"head -n 400 {BIG}",                      "line threshold let 25KB through"),
    ("deny",  f"tail -n 999 {BIG}",                      "same via tail"),
    ("allow", f"head -n 20 {BIG}",                       "a genuinely small slice"),
    ("allow", f"cat {BIG} 1> /tmp/out.txt",              "1> is a real stdout redirect"),
    ("allow", f"cat {BIG} &> /tmp/out.txt",              "&> likewise"),
    ("deny",  f"cat {SMALL}; cat {BIG}",                 "a later segment was never inspected"),
    ("deny",  f"cat {SMALL} | head -5 && cat {BIG}",     "a capped decoy segment disarmed it"),
    ("deny",  f"grep -n cat {SMALL} && cat {BIG}",       "tool name as an arg in segment 1"),
    ("deny",  f"head -n 900 {BIG}; tail -n 3 {SMALL}",   "trailing harmless tail disarmed it"),
    ("deny",  f"head -c 400000 {BIG}",                   "head -c was never thresholded"),
    ("deny",  f"tail -n +1 {BIG}",                       "tail -n +N prints to EOF"),
    ("deny",  f"sed -n '1,$p' {BIG}",                    "sed A,$p prints to EOF"),
    ("deny",  f"head -n 18446744073709551617 {BIG}",     "a count too large to be an integer is unbounded"),
    ("deny",  f"cat {BIG} >&2",                          ">&2 still lands in the tool result"),
    ("deny",  f"(cat {BIG})",                            "subshell bypassed guard 5"),
    ("allow", f"python3 - <<'PY'\ncat {BIG}\nPY",       "heredoc body is data, not a command"),
    ("deny",  f"bash <<EOF\ncat {BIG}\nEOF",            "heredoc fed to a shell still runs"),
    ("deny",  f"cat > /tmp/x <<'EOF'\nhi\nEOF\ncat {BIG}", "a dump after the heredoc ends"),
    ("allow", f"cat > /tmp/x <<'EOF'\n" + f"head -n 900 {BIG}\n" * 400 + "EOF",
     "400 dump-shaped heredoc lines are data"),
    # --- misc ------------------------------------------------------------------
    ("allow", "x=$(git rev-parse HEAD); echo $x",        "ordinary assignment from substitution"),
    ("allow", "grep -rn foo src/",                "ordinary grep"),
    ("allow", "gh pr view 123",                   "ordinary gh"),
]


def _sections():
    """Map section name -> its slice of CASES, by source position rather than by label.

    Convention: a line whose stripped form starts with `# --- ` opens a section named by its
    third whitespace-delimited word. Do not write that prefix inside a case string.
    """
    src = open(__file__).read()
    node = next(n for n in ast.parse(src).body
                if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "CASES")
    bounds = {}
    for i, line in enumerate(src.split("\n"), 1):
        st = line.strip()
        if st.startswith("# --- ") and node.lineno < i < node.end_lineno:
            bounds[i] = st.split()[2]
    out, current = {}, None
    for elt, case in zip(node.value.elts, CASES):
        for ln in sorted(bounds):
            if ln < elt.lineno:
                current = bounds[ln]
        out.setdefault(current, []).append(case)
    return out


SECTIONS = _sections()
# The sum is equal by construction, so it cannot detect a case that fell outside every marker
# — that case lands under key None. Assert on that directly.
assert None not in SECTIONS, \
    "a case appears above the first '# --- <section>' marker; move it under one"


# Keys from the per-user overlay (hooks/lib/hook-io kit_env): each case names the overlay it runs
# with. A missing overlay must leave every prod guard on and add no base branch.
KIT = _fixture("kit.env", "RELEASE_BRANCH_RE='rel-[0-9]+|trunk'\n")
KIT_CASES = [
    (KIT,        "ask",   "git diff rel-7..HEAD",            "two-dot vs an overlay release branch"),
    (KIT,        "ask",   "git log trunk..HEAD",             "...another alternative of it"),
    (KIT,        "allow", "git diff rel-7...HEAD",           "three-dot vs an overlay release branch"),
    (os.devnull, "allow", "git diff rel-7..HEAD",            "no overlay: only origin/*, main, master are bases"),
    (os.devnull, "ask",   "git diff origin/master..HEAD",    "no overlay: a generic base still asks"),
    (os.devnull, "deny",  f"{_M} -e 'select 1'",             "no overlay: raw client still denied"),
    (os.devnull, "deny",  f"python3 -c 'import {_PYM}'",     "no overlay: driver import still denied"),
    (os.devnull, "deny",  "git commit -m 'test'",            "no overlay: trailers still required"),
]


def decision(cmd, kit=None):
    env = None if kit is None else {**os.environ, "KIT_ENV": kit}
    r = subprocess.run(
        [H], input=json.dumps({"tool_input": {"command": cmd}}),
        capture_output=True, text=True, env=env,
    )
    # A crashed hook writes nothing to stdout, which used to read as "allow" — so a bash 3.2
    # syntax error made ~40 of these cases print `ok` while the guard was entirely dead. That
    # inverted the suite's whole purpose: the failure it exists to catch was the one it hid.
    # A hook that cannot run is a hook that is not guarding; surface it as its own verdict.
    if r.returncode != 0 or r.stderr.strip():
        return f"CRASH(rc={r.returncode}: {r.stderr.strip().splitlines()[0][:60] if r.stderr.strip() else 'no stderr'})"
    out = r.stdout.strip()
    if not out:
        return "allow"
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"]


def main():
    # Optional section filter: `bash-guards.test.py raw-client` runs one guard's cases.
    # Cheaper than starting yet another probe script, which is how a case gets dropped.
    want_section = sys.argv[1] if len(sys.argv) > 1 else None
    cases = [(None, *c) for c in CASES]
    if want_section == "overlay":
        cases = []
    elif want_section:
        if want_section not in SECTIONS:
            raise SystemExit(f"no section {want_section!r}; try one of: overlay, {', '.join(sorted(SECTIONS, key=str))}")
        cases = [(None, *c) for c in SECTIONS[want_section]]
        print(f"[{want_section}: {len(cases)} cases]\n")
    if want_section in (None, "overlay"):
        cases += KIT_CASES

    fails = 0
    try:
        for kit, want, cmd, label in cases:
            got = decision(cmd, kit)
            if got != want:
                fails += 1
            print(f"{'ok  ' if got == want else 'FAIL'} want={want:<5} got={got:<5} {label}")
        for _p in (os.path.join(FIX, "PWNED"), os.path.join(os.getcwd(), "PWNED")):
            assert not os.path.exists(_p), \
                f"context-dumps EXECUTED a crafted filename ({_p})"
    finally:
        pass   # fixtures are removed by the atexit hook registered on FIX
    print(f"\n{len(cases) - fails}/{len(cases)} passed")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
