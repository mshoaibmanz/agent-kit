#!/usr/bin/env python3
"""Payload tests for ~/.claude/hooks/bash-guards.

CLAUDE.md: "test every hook by piping it a real payload before trusting it" — a bash-3.2
regression in a hook fails silently and can sit undetected for weeks. Run this after ANY
edit to bash-guards:

    python3 ~/.claude/hooks/tests/bash-guards.test.py

HOOKS_DIR=<dir> tests another copy of the hooks, e.g. the pre-fix ones to prove a case fails.
Kept out of the scratchpad deliberately: /private/tmp/claude-* is reaped between days, and
a test that vanishes is a test that never runs.
"""
import ast
import atexit
import json
import os
import sys
import tempfile
import shutil
import subprocess
import time

H =os.path.join(os.environ.get("HOOKS_DIR") or os.path.expanduser("~/.claude/hooks"), "bash-guards")


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
    # --- hooks-bypass ----------------------------------------------------------
    # The agent git layer is injected by the settings env; each of these turns it off for a command.
    ("deny",  "git push --no-verify",                                  "push --no-verify"),
    ("deny",  "git -C \"$WT\" push -u origin HEAD --no-verify",        "...the worktree form, flag last"),
    ("deny",  "git commit -n -m 'x\n\nCo-authored-by: C <c@c>'",       "commit -n"),
    ("deny",  "git commit -anm 'x\n\nCo-authored-by: C <c@c>'",        "commit -n in a flag cluster"),
    ("deny",  "git commit --no-verify -m 'x\n\nCo-authored-by: C <c@c>'", "commit --no-verify"),
    ("deny",  "AGENT_GIT_HOOKS=off git push",                          "AGENT_GIT_HOOKS=off"),
    ("deny",  "export AGENT_GIT_HOOKS=off; git push",                  "...exported"),
    ("deny",  "CI_WATCH_ACTIVE=1 git push",                            "posing as the ci-watch agent"),
    ("deny",  "git -c core.hooksPath=/dev/null push",                  "-c core.hooksPath="),
    ("deny",  "git -c 'core.hooksPath=' push",                         "...quoted"),
    ("deny",  "git -c core.hookspath=x commit -m 'y\n\nCo-authored-by: C <c@c>'", "...any case"),
    ("deny",  "GIT_CONFIG_COUNT=0 git push",                           "GIT_CONFIG_COUNT on the command line"),
    ("deny",  "env GIT_CONFIG_VALUE_0=/dev/null git push",             "GIT_CONFIG_VALUE_0 via env"),
    ("deny",  "env -u GIT_CONFIG_COUNT git push",                      "env -u GIT_CONFIG_COUNT"),
    ("deny",  "unset GIT_CONFIG_COUNT && git push",                    "unset GIT_CONFIG_COUNT"),
    ("deny",  "env -u CLAUDECODE -u AI_AGENT git push",                "unsetting the agent markers"),
    ("deny",  "env -i PATH=/usr/bin git push",                         "env -i before git"),
    ("allow", "git push",                                              "a plain push"),
    ("allow", "git commit --amend --no-edit",                          "amend --no-edit is not --no-verify"),
    ("allow", "git log -n 5",                                          "log -n is not commit -n"),
    ("allow", "git push -n origin HEAD",                               "push -n is a dry run"),
    ("allow", "echo $GIT_CONFIG_COUNT",                                "reading the injected env"),
    ("allow", "git commit -m 'skip with --no-verify\n\nCo-authored-by: C <c@c>'", "--no-verify inside the message"),
    ("allow", "env | grep GIT_CONFIG",                                 "listing the env"),
    ("allow", "cat > n.md <<'EOF'\ndeny git push --no-verify and -c core.hooksPath=x\nEOF", "bypass forms in a data heredoc"),
    ("deny",  "bash <<'EOF'\ngit push --no-verify\nEOF",        "...but code in a heredoc fed to bash"),
    # An apostrophe in a "…" string or a comment no longer pairs with a quote lines later (B-1).
    ("deny",  "echo \"it's\" > a\ngit push --no-verify\necho 'x'",     "--no-verify after an apostrophe in a dq string"),
    # git accepts a unique prefix of a long option; --no-v/--no-ve/--no-ver are ambiguous with
    # --no-verbose and git refuses them, so only --no-veri and longer skip the hook (B-7).
    ("deny",  "git push --no-verif origin x",                          "push --no-verif (abbreviated)"),
    ("deny",  "git push --no-veri origin x",                           "push --no-veri"),
    ("deny",  "git commit --no-verif -m 'x\n\nCo-authored-by: C <c@c>'", "commit --no-verif"),
    ("deny",  "CLAUDECODE= AI_AGENT= git push origin x",               "emptying the agent markers"),
    ("deny",  "export CLAUDECODE= AI_AGENT= AGENT_HOST=; git push",     "...exported"),
    ("deny",  "env AI_AGENT= git push origin x",                       "...through env"),
    ("deny",  "/usr/bin/git push --no-verify origin x",                "git by absolute path (B-10)"),
    ("deny",  "\\git push --no-verify origin x",                       "backslash-escaped git"),
    ("deny",  "env - git push origin x",                               "env - is env -i"),
    ("deny",  "git -c 'core.hooksPath'=/dev/null push origin x",       "quoted -c key"),
    ("deny",  "git push '--no-verify'",                                "a quoted flag is still the flag"),
    ("deny",  "bash -c 'git push --no-verify'",                        "bash -c body is code"),
    # Mentions are not bypasses (B-9).
    ("allow", "git commit -m \"Deny git -c core.hooksPath= overrides\n\nCo-authored-by: C <c@c>\"", "-c core.hooksPath= named in a message"),
    ("allow", "gh pr create --body \"Deny git -c core.hooksPath= overrides\"", "...in a PR body"),
    ("allow", "grep -n GIT_CONFIG_COUNT= settings.base.json",          "GIT_CONFIG_COUNT= as a grep pattern"),
    ("allow", "git log -n 3 commit",                                   "log -n with a path named commit"),
    ("allow", "env -i HOME=$HOME python3 x.py && git status",          "env -i runs python, not git"),
    # Codex round 2 on 6d62a01. CX-1: a $( ) or backtick no longer splits the outer command, and a
    # shell -c body keeps the env -i it runs under.
    ("deny",  "git push \"$(printf origin)\" main --no-verify",       "$( ) before --no-verify (CX-1)"),
    ("deny",  "env -i bash -c 'git push'",                             "env -i bash -c git"),
    ("deny",  "env -i PATH=/usr/bin sh -c \"git status; git push origin x\"", "...the body's second command"),
    ("deny",  "env -i bash -c 'echo \"$(git push origin x)\"'",        "...a $( ) inside the body"),
    ("deny",  "/usr/bin/env -i /bin/bash -c 'git push'",               "...env and bash by path"),
    ("deny",  "exec -c git push origin x",                             "exec -c empties the env"),
    # CX-2: the key of a quoted -c/--config-env value with a space survives the masking.
    ("deny",  "git -c 'core.hooksPath=/tmp/no hooks' push origin main", "-c '...hooksPath=<value with a space>' (CX-2)"),
    ("deny",  "git -c \"core.hooksPath=/a b\" push",                   "...double-quoted"),
    ("deny",  "git -c user.name=x -c \"core.hooksPath=$HOME/a b\" push", "...the second -c"),
    ("deny",  "git --config-env 'core.hooksPath=MY VAR' push",         "--config-env with a space"),
    ("deny",  "git --config-env='core.hooksPath=MY VAR' push",         "--config-env=... with a space"),
    # CX-3: an option's operand is not a flag; the real flag still denies.
    ("allow", "git commit -m '--no-verify' -m 'Co-authored-by: C <c@c>'", "-m '--no-verify' is a message (CX-3)"),
    ("allow", "git commit -m '-n' -m 'Co-authored-by: C <c@c>'",       "-m '-n' is a message"),
    ("allow", "git commit -am -n -m 'Co-authored-by: C <c@c>'",        "-am -n: the cluster's m takes -n"),
    ("allow", "git commit --message=x --message '--no-verify' -m 'Co-authored-by: C <c@c>'", "--message takes an operand"),
    ("allow", "git commit --mess '-n' -m 'Co-authored-by: C <c@c>'",   "...abbreviated"),
    ("allow", "git push -o '--no-verify' origin x",                    "push -o takes an operand"),
    ("deny",  "git commit -pn -m 'x' -m 'Co-authored-by: C <c@c>'",    "-n in a -p cluster"),
    # CX-4: sh -c / eval words handed to echo or printf are data.
    ("allow", "echo sh -c 'git push --no-verify'",                     "sh -c as echo's argument (CX-4)"),
    # --- destructive-git -------------------------------------------------------
    ("allow", "cat > f.sh <<'EOF'\n# then git push\n[ -f x ] && echo y\nEOF",  "heredoc body: push in a comment, [ -f x ]: data"),
    ("allow", "python3 - <<'EOF'\nprint('git push origin b --force')\nEOF\ncat > g <<'EOF'\npush -f\nEOF", "two data heredocs"),
    ("ask",   "bash <<'EOF'\ngit push --force\nEOF",       "a body fed to bash is code"),
    ("ask",   "git -C ~/x push origin b --force",            "git -C <dir> push ... --force"),
    ("allow", "git push origin b && rm -f tmp",               "-f belongs to rm, not the push"),
    ("allow", "git commit -m 'subj\n\nnever git push --force\n\nCo-authored-by: C <c@c>'",  "force push named in a multi-line message"),
    ("ask",   "git push -uf origin b",                        "-f in a short-flag cluster"),
    ("ask",   "git push origin +main",                         "+refspec without a colon"),
    ("allow", "echo git push --force",                         "git as an argument to echo"),
    ("ask",   "cd x && git reset --hard HEAD~1",               "reset --hard after a cd"),
    ("allow", "git reset --soft HEAD~1 && grep -- --hard f",   "--hard belongs to grep"),
    ("ask",   "git push origin foo --force",      "force push, flag last"),
    ("ask",   "git reset --hard HEAD~1",          "reset --hard"),
    ("deny",  "git push origin HEAD:other-branch", "HEAD:<branch> without -u keeps the old upstream"),
    ("allow", "git push -u origin HEAD:other-branch", "...with -u"),
    ("allow", "git push --set-upstream origin HEAD:other-branch", "...with --set-upstream"),
    # B-1: an apostrophe in a "…" string or a # comment must not pair with a quote lines later.
    ("ask",   "git commit -m \"don't break\" -m 'Co-authored-by: C <c@c>'\ngit push --force origin foo\necho 'done'", "force push after an apostrophe in a dq string"),
    ("ask",   "echo \"it's\" > a\ngit reset --hard HEAD\necho 'x'",   "reset --hard after an apostrophe"),
    ("ask",   "# don't leave the old branch around\ngit branch -D old-feature\ngit log -1 --format='%h'", "branch -D after an apostrophe in a comment"),
    # B-2: git after a wrapper, keyword, xargs, $( or a shell -c body.
    ("ask",   "git branch --merged | grep -v main | xargs git branch -d", "xargs git branch -d"),
    ("ask",   "git for-each-ref --format='%(refname:short)' refs/heads/tmp- | while read b; do git branch -D \"$b\"; done", "while ... do git branch -D"),
    ("ask",   "for b in a b; do git push --force origin $b; done",    "for ... do git push --force"),
    ("ask",   "if true; then git reset --hard; fi",                   "then git reset --hard"),
    # sudo resets the env (env_reset), dropping the agent git layer: a hooks bypass like env -i
    # (phase 19 decision; this asked before). sudo -E keeps the env, so its force push still asks.
    ("deny",  "sudo git push --force",                                "sudo: hooks bypass"),
    ("deny",  "sudo git push origin b",                               "sudo: a plain push too"),
    ("deny",  "sudo -u me git commit -m 'x' -m 'Co-authored-by: C <c@c>'", "sudo -u: still a bypass"),
    ("ask",   "sudo -E git push --force",                             "sudo -E keeps the env: force push asks"),
    ("allow", "sudo -E git status",                                   "...and a plain git under it is fine"),
    # --preserve-env=LIST keeps only LIST (CX-1, B-7); -nE is -n plus -E; a $VAR with -c/-C is not git.
    ("deny",  "sudo --preserve-env=PATH git push origin b",           "sudo --preserve-env=LIST: still a bypass"),
    ("ask",   "sudo -nE git push --force",                            "sudo -nE keeps the env: force push asks"),
    ("allow", "sudo -nE git push origin b",                           "...and a plain push under -nE passes"),
    ("deny",  "sudo -nu root git push origin b",                      "sudo -nu root: a cluster's operand, still a bypass"),
    ("deny",  "sudo --user root git push origin b",                   "sudo --user root: the long operand form"),
    ("allow", "git status; sudo \"$PY\" -c 'x'",                      "sudo \"$PY\" -c: not git"),
    ("allow", "git status; sudo $MAKE -C d all",                      "sudo $MAKE -C d all: not git"),
    # git's global options before the subcommand of a dynamic git word (CX-2, B-7).
    ("ask",   "$GIT --no-pager push --force",                         "$GIT --no-pager push --force"),
    ("ask",   "\"$GIT\" --git-dir=x push --force",                    "\"$GIT\" --git-dir=x push --force"),
    ("ask",   "$(which git) --no-pager reset --hard",                 "$(which git) --no-pager reset --hard"),
    ("ask",   "\"$GIT\" -P -C ~/x reset --hard",                      "\"$GIT\" -P -C dir reset --hard"),
    # A substitution or a variable as git's command word.
    ("ask",   "$(which git) push --force",                            "$(which git) push --force"),
    ("ask",   "\"$GIT\" push --force origin b",                       "\"$GIT\" push --force"),
    ("ask",   "${GIT} reset --hard",                                  "${GIT} reset --hard"),
    ("ask",   "`command -v git` -C ~/x branch -D old",                "backtick git -C ... branch -D"),
    ("allow", "\"$EDITOR\" notes.md",                                 "a variable command word with no git subcommand"),
    ("ask", "$G push --force origin b", "R3-B-5: variable name need not contain git"),
    ("ask", "$G --no-pager reset --hard", "R3-B-5: dynamic git with global option"),
    ("deny", "$G push --no-verify", "R3-B-5: dynamic git hook bypass"),
    ("allow", "$EDITOR notes.md", "R3-B-5: unrelated variable program stays allowed"),
    # An unquoted heredoc body expands, so its $( ) runs.
    ("ask",   "cat <<EOF\n$(git push -f)\nEOF",                       "$( ) in an unquoted heredoc body"),
    ("ask",   "cat > f <<EOF\nx `git reset --hard` y\nEOF",          "backticks in an unquoted body"),
    ("allow", "cat <<'EOF'\n$(git push -f)\nEOF",                     "...a quoted body stays data"),
    ("allow", "cat <<EOF\nrun git push -f later \"quoted\"\nEOF",     "...an unquoted body with no $( ) stays data"),
    ("ask", "cat <<EOF\n$(echo x # )\ngit reset --hard\n)\nEOF", "R3-CX-2: comment ) does not close a heredoc substitution"),
    ("ask", "cat <<EOF\n$(# )\ngit reset --hard\n)\nEOF", "R3-CX-2: comment starts a substitution body"),
    # CX-3: a substitution spanning body lines, and quotes inside one.
    ("ask",   "cat <<EOF\n$(\ngit reset --hard\n)\nEOF",            "$( spanning heredoc lines"),
    ("ask",   "cat <<EOF\n$(git reset \"--hard\")\nEOF",            "quoted option inside a body $( )"),
    ("ask",   "cat <<EOF\nx \"$(echo \")\"; git reset --hard)\" y\nEOF", "a quoted ) does not end the $( )"),
    ("allow", "cat <<EOF\nsee \"$(date)\" and \"git reset --hard\"\nEOF", "...the body's own quoted text stays data"),
    ("ask",   "time git push --force",                                "time"),
    ("ask",   "nohup git push --force &",                             "nohup ... &"),
    ("ask",   "env X=y git push --force",                             "env X=y"),
    ("ask",   "command git push --force",                             "command"),
    ("ask",   "xargs -I {} git branch -D {} < list",                  "xargs -I {}"),
    ("ask",   "nice -n 5 git push -f",                                "nice -n 5"),
    ("ask",   "{ git push --force; }",                                "brace group"),
    ("ask",   "bash -c 'git push --force'",                           "bash -c '...'"),
    ("ask",   "sh -c \"git reset --hard\"",                           "sh -c \"...\""),
    ("ask",   "echo \"$(git push --force)\"",                         "$( ) inside a dq string"),
    ("ask",   "git push origin '--force'",                            "a quoted flag"),
    ("ask",   "git push \\\n  --force origin b",                      "flag after a line continuation"),
    ("allow", "git stash push -m clear",                              "stash push with a message named clear"),
    # Codex round 2 on 6d62a01. CX-1: a substitution leaves a placeholder in the outer command.
    ("ask",   "git push \"$(printf origin)\" main --force",           "dq $( ) before --force (CX-1)"),
    ("ask",   "git -C \"$(pwd)\" reset --hard",                       "-C \"$(pwd)\" reset --hard"),
    ("ask",   "git push $(git remote | head -1) main --force",        "bare $( ) before --force"),
    ("ask",   "git push `echo origin` main --force",                  "backticks before --force"),
    ("ask",   "git push $(echo $(echo origin)) --force",              "nested $( )"),
    ("ask",   "bash -c 'git push \"$(printf origin)\" --force'",      "$( ) inside a bash -c body"),
    ("ask",   "bash -c $'git push --force'",                          "$'...' -c body is code"),
    ("ask",   "eval $'git reset --hard'",                             "$'...' eval body is code"),
    # CX-4: only a shell or eval in command position makes its string code.
    ("allow", "echo eval 'git reset --hard'",                         "eval as echo's argument (CX-4)"),
    ("allow", "printf '%s\\n' bash -c 'git push --force'",            "bash -c as printf's argument"),
    ("allow", "time echo sh -c 'git push -f'",                        "...after a wrapper"),
    # --- bsd-cp-t --------------------------------------------------------------
    ("deny",  "cp a.py b.py -t src/pkg/",         "BSD cp copies onto the last operand"),
    ("allow", "cp a.py src/pkg/a.py",             "plain two-operand cp"),
    ("allow", "cp -r src dest",                   "other cp flags"),
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
    ("deny",  "command security -i < /tmp/cmds", "interactive security behind `command`"),
    ("deny",  "xcrun security -i < /tmp/cmds", "...behind xcrun"),
    ("deny",  "env LANG=C sudo -u me security -qp 'k> ' < /tmp/cmds", "...behind env and sudo"),
    ("allow", "mkdir -p /tmp/x && security list-keychains", "-p of another command, then a harmless subcommand"),
    ("allow", "security find-certificate -a -p -c Apple", "-p of a subcommand, not of security"),
    ("deny",  "security find-certificate -p -c Apple; security find-generic-password -w", "a secret subcommand beside one"),
    ("allow", "grep -n \"security -i\" ~/.claude/hooks/bash-guards", "grep for the interactive form"),
    # Each of these reached `security -i` or a secret subcommand and was allowed: a word before the
    # flag that is no plain subcommand, a flag with trailing text, or security reached by another
    # spelling than the literal word.
    ("deny",  "security $X -i < /tmp/cmds",              "an expansion before -i may itself be -i"),
    ("deny",  "security -i$X < /tmp/cmds",               "-i with trailing text"),
    ("deny",  "security -i{,} < /tmp/cmds",              "-i with a brace expansion"),
    ("deny",  "security -[i] < /tmp/cmds",               "a glob that a file named -i expands to -i"),
    ("deny",  "security < /tmp/cmds -i",                 "-i after a redirection"),
    ("deny",  "/usr/bin/secur?ty -i < /tmp/cmds",        "security reached through a glob"),
    ("deny",  "$(which security) -i < /tmp/cmds",        "...through a substitution"),
    ("deny",  "`command -v security` -i < /tmp/cmds",    "...through a backtick substitution"),
    ("deny",  "S=security; $S -i < /tmp/cmds",           "...through a variable"),
    ("deny",  "SECURITY -i < /tmp/cmds",                 "...in upper case: the disk is case-insensitive"),
    ("deny",  "{/usr/bin/security,-i} < /tmp/cmds",      "...through a brace expansion"),
    ("deny",  "/usr/bin/secur?ty find-generic-password -w", "a secret subcommand behind a glob"),
    ("deny",  "$(which security) dump-keychain",         "...behind a substitution"),
    ("deny",  "X=password; security find-generic-$X -w", "a subcommand name built from an expansion"),
    ("allow", "security -v list-keychains",              "a benign option, then a subcommand"),
    ("allow", "echo \"$(command -v security)\"",         "echoing its path"),
    ("allow", "find . -name security -type d",           "the word as another tool's argument"),
    ("allow", "mkdir -p a/security b",                   "...followed by a plain word"),
    ("allow", "grep -n \"CLIENT_CANDIDATES\\|MYSQL_RO_CLIENT\\|LSOF\" ~/.local/bin/ro-mysql",
     "an escaped pipe in a quoted BRE stays inside the grep span"),
    ("deny",  "grep -n x f | MYSQL_RO_CLIENT=/tmp/evil ro-mysql -e 'SELECT 1'", "a real pipe still ends the span"),
    ("allow", "rg -n 'x|MYSQL_RO_CLIENT' f",             "an unescaped pipe in a quoted pattern stays in the rg span"),
    ("allow", "grep -En \"x|MYSQL_RO_CLIENT|y\" f",      "...and in a double-quoted grep pattern"),
    ("deny",  "grep 'x|MYSQL_RO_CLIENT' f; bash -c 'x'", "a segment that is no read-only tool keeps the plain scan"),
    # Matched anywhere in the text, a word naming security (any case, glob, brace) before an -i/-p-like
    # word denied each of these; only a word in command position counts now.
    ("allow", "find . -name '*security*' -print",        "a glob naming security, as find's argument"),
    ("allow", "find . -path ./security -prune -o -name '*.py' -print", "a path named security, then -prune"),
    ("allow", "mkdir -p src/{security,models}",          "a brace naming security, as an argument"),
    ("allow", "git add src/{security,api}.py",           "...inside a path"),
    ("allow", "gh pr create --title 'Security [P1] fix' --body-file b.md", "a quoted PR title is prose"),
    ("allow", "gh pr create --body '**Security** `x` fix' --title t", "...with glob and backtick characters"),
    ("allow", "gh pr comment 12 --body 'never run security -i here'", "...even naming an interactive call"),
    # Command position is read as a shell reads it, so these still reach security.
    ("deny",  "'security' -i < /tmp/cmds",              "a quoted command word"),
    ("deny",  "$'\\x73ecurity' -i < /tmp/cmds",          "an ANSI-C string spelling it"),
    ("deny",  "secu{r,}ity -i < /tmp/cmds",              "a brace that expands to it"),
    ("deny",  "timeout 5 security -i < /tmp/cmds",       "behind timeout"),
    ("deny",  "kubectl exec pod -- security -i < /tmp/cmds", "the command after --"),
    ("deny",  "echo 'security -i' | sh",                 "echoed into a shell"),
    ("deny",  "echo x | xargs security",                 "xargs hands it a subcommand from stdin"),
    ("deny",  "cat <<'EOF' | bash\nsecurity find-generic-password -w\nEOF", "a heredoc piped into a shell"),
    ("deny",  "cat <<EOF\nit's\nEOF\nsecurity -i < /tmp/cmds", "an apostrophe in a heredoc body hides no later line"),
    ("deny",  "python3 -c \"import os; os.system('security find-generic-password -w')\"", "an interpreter one-liner"),
    ("deny",  "find . -maxdepth 0 -exec security find-generic-password -w \\;", "find -exec"),
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
    ("allow", f"grep -n 'ro-{_M}\\|{_M}' ~/.claude/hooks/bash-guards | head", "a client name after a quoted escaped pipe"),
    ("deny",  f"grep 'x\\|y' f | {_M} -e 'x'",           "...but not before a real pipe into it"),
    ("deny",  f"bash -c 'bash -c cat\\ x\\|{_M}'",       "a nested shell undoes the escape"),
    ("deny",  f"echo \"it's\" x; bash -c cat\\ y\\|{_M} 'z'", "an apostrophe cannot hide an unquoted escaped pipe"),
    ("allow", f"grep -n 'ro-{_M}|{_M}' f",               "a client name after a quoted unescaped pipe"),
    ("deny",  f"grep 'x|y' f | {_M} -e 'x'",             "...but not before a real pipe into it"),
    # An inspection span took the next word for the tool's one argument, so an option naming a
    # command the tool runs hid the client.
    ("deny",  f"rg --pre /opt/homebrew/bin/{_M} x src",  "rg --pre runs the command it names"),
    ("deny",  f"rg --pre={_M} x f",                      "...spelled --pre="),
    ("deny",  f"rg --hostname-bin={_M} x f",             "rg --hostname-bin runs one too"),
    ("allow", "rg --pre-glob '*.gz' -n x f",             "--pre-glob names no command"),
    ("allow", "rg --pre ./unzip.sh x f",                 "a --pre command that is no client"),
    ("deny",  f"vim -c '!{_M} -e x' f",                  "vim -c runs an ex command, a shell escape included"),
    ("deny",  f"open /opt/homebrew/bin/{_M}",            "open runs a binary in Terminal"),
    # A span's text reaches a shell that runs it, so no span is stripped there.
    ("deny",  f"echo '{_M} -e \"DROP TABLE t\"' | sh",   "echoed into a shell: the echo span hid the name"),
    ("deny",  f"sh -c \"$(true; echo {_M}) -e 'DROP TABLE t'\"", "...built inside sh -c"),
    ("deny",  f"eval \"$(true; echo {_M}) -e 'DROP TABLE t'\"", "...or for eval"),
    ("allow", f"grep -rn {_M} src | grep -v bash",       "a shell name as grep's pattern keeps the spans"),
    # A copy of the client runs under any name.
    ("deny",  f"cp /opt/homebrew/bin/{_M} ./m && ./m -e 'DROP TABLE t'", "the client copied under another name"),
    ("deny",  f"mv /opt/homebrew/bin/{_M} /tmp/m",       "...moved"),
    ("deny",  f"cat /opt/homebrew/bin/{_M} > ./m",       "...copied by cat"),
    ("deny",  f"ln -s /opt/homebrew/bin/{_M} ./m",       "...linked"),
    ("deny",  f"install -m 755 /opt/homebrew/bin/{_M} ./m", "...installed"),
    ("allow", f"cp references/{_M}.md /tmp/x.md",        "a doc named after it is no client"),
    ("allow", f"cat /opt/homebrew/var/{_M}/host.err 2>/dev/null", "stderr to a file is no copy"),
    # The disk is case-insensitive: these ran the client. Prose casing stays allowed.
    ("deny",  f"{_M.upper()} -e 'SELECT 1'",             "an upper-case client name"),
    ("deny",  "/opt/homebrew/bin/MySQL -e 'SELECT 1'",   "...a mixed-case client path"),
    ("allow", "gh pr create --title 'Fix MySQL pool' --body-file b.md", "MySQL as prose"),
    ("deny",  f"my\\\n{_M[2:]} -e 'SELECT 1'",           "a line continuation inside the name"),
    ("deny",  f"echo a\\\\\n{_M} -e 'SELECT 1'",         "an escaped backslash ends its line, so the next still runs"),
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
    # A group's own redirect covers every command inside it (retro 2026-W40:9).
    ("allow", f"{{ sed -n '5,$p' {BIG}; echo done; }} > {FIX}/grp.out", "{ …; } > file: the group's redirect"),
    ("allow", f"{{ sed -n '5,$p' {BIG}\necho done\n}} > {FIX}/grp.out", "...a multi-line group"),
    ("allow", f"( sed -n '5,$p' {BIG}; echo x ) > {FIX}/grp.out", "...a subshell group"),
    ("allow", f"{{ cat {BIG}; }} | head -50",            "...a group piped into a cap"),
    ("deny",  f"{{ sed -n '5,$p' {BIG}; echo done; }}",  "a group with no redirect still dumps"),
    ("deny",  f"{{ sed -n '5,$p' {BIG}; }} 2> {FIX}/grp.err", "...a group's 2> is not stdout"),
    ("deny",  f"{{ cat {BIG}; }} >&2",                   "...nor its >&2"),
    # CX-6: an inner redirect to stderr escapes the group's redirect.
    ("deny",  f"{{ cat {BIG} >&2; }} > {FIX}/grp.out",   "{ cat big >&2; } > file: stderr reaches context"),
    ("deny",  f"( cat {BIG} >&2; echo ) > {FIX}/grp.out", "...nor inside a subshell group"),
    ("deny",  f"{{ echo hi; }} > {FIX}/grp.out; sed -n '5,$p' {BIG}", "a dump after a redirected group"),
    # --- misc ------------------------------------------------------------------
    ("allow", "x=$(git rev-parse HEAD); echo $x",        "ordinary assignment from substitution"),
    ("allow", "grep -rn foo src/",                "ordinary grep"),
    ("allow", "gh pr view 123",                   "ordinary gh"),
    # --- inplace-edit ------------------------------------------------------------
    ("deny",  "sed -i '' 's/a/b/' src/x.py",      "BSD sed -i"),
    ("deny",  "sed -Ei 's/a/b/' src/x.py",        "-i inside a flag cluster"),
    ("deny",  "sed --in-place 's/a/b/' x.py",     "long form"),
    ("deny",  "cd repo && perl -pi -e 's/a/b/' x.py", "perl -pi after cd"),
    ("allow", "sed -i '' 's/a/b/' a.py b.py c.py # sweep", "declared multi-file sweep"),
    ("allow", "sed -n '1,20p' x.py",              "sed read, not in-place"),
    ("allow", "sed -e 's/i/j/' x.py > y.py",      "an i inside the script, not a flag"),
    ("allow", "grep -n 'sed -i' notes.md",        "sed -i named inside a quoted pattern"),
    # --- foreground-poll ---------------------------------------------------------
    ("deny",  "until gh run view 1 --json status | grep -q completed; do sleep 20; done", "until + sleep 20 in the foreground"),
    ("deny",  "while ! test -f done.txt; do sleep 5; done",  "while + sleep 5"),
    ("deny",  "gh pr checks 1 --watch",                      "gh pr checks --watch"),
    ("deny",  "gh run watch 123",                            "gh run watch"),
    ("allow", "until [ -f x ]; do sleep 0.1; done",          "a short readiness spin"),
    ("allow", "until [ -f x ]; do sleep 1; done",            "sleep under five seconds"),
    ("allow", "until gh run view 1 | grep -q done; do sleep 30; done # fg-wait", "the fg-wait marker"),
    ("allow", "gh pr checks 1",                              "a one-shot status read"),
    ("allow", "cat > w.sh <<'EOF2'\nuntil x; do sleep 30; done\nEOF2", "a loop written into a quoted heredoc"),
    ("deny",  "until gh run view 1 | grep -q done\ndo\n  sleep 30\ndone", "a loop written across lines"),
    ("allow", "rg -n 'gh run watch' bin/agent-run",         "a quoted search for the words is not a wait"),
    ("allow", 'grep -n "until .* sleep 30" notes.md',       "a quoted pattern naming a loop"),
    ("allow", "sleep 30",                                   "a bare sleep is not a polling loop"),
    ("deny",  'printf "%s\\n" "$(gh run watch 1)"',          "a watch run inside a double-quoted substitution"),
    ("deny",  'x="`gh pr checks 1 --watch`"',                "a watch in backticks inside double quotes"),
    ("allow", "echo '$(gh run watch 1)'",                   "single quotes keep a substitution literal"),
    ("deny",  "echo \"'$(gh run watch 1)'\"",                "an apostrophe inside double quotes does not quote"),
    ("allow", 'echo "\\$(gh run watch 1)"',                  "an escaped substitution is literal text"),
    ("deny",  'x="$(gh run watch $(printf 1))"',             "a nested substitution keeps the outer wait"),
    ("deny",  "while\ntrue; do sleep 20; done",              "a newline right after while"),
    ("deny",  "until\n! test -f x; do sleep 10; done",       "a newline right after until"),
    ("allow", "echo while\nsleep 10",                        "the word while as an argument, then a bare sleep"),
    ("deny",  "if true; then while\ntrue; do sleep 20; done; fi", "a newline after while, nested under then"),
    ("deny",  "if a; then :; else until\nb; do sleep 9; done; fi", "a newline after until, nested under else"),
    ("deny",  "for i in 1; do while\ntrue; do sleep 30; done; done", "a newline after while, nested under do"),
    ("deny",  "! while\ntrue; do sleep 15; done",                "a newline after while, after !"),
    ("allow", "echo do while\ndate\nsleep 10",                   "compound keywords as echo's arguments"),
    ("allow", "echo ! until\ndate\nsleep 10",                    "! and until as echo's arguments"),
    ("deny",  "if a; then ! while\nb; do sleep 12; done; fi",    "a chain of compound keywords before while"),
    # --- zsh-modifier ------------------------------------------------------------
    ("deny",  'git fetch origin "+refs/heads/$b:refs/remotes/origin/$b"', "refspec eaten by :r"),
    ("deny",  "echo $f:h",                         ":h on an unbraced var"),
    ("allow", 'git fetch origin "+refs/heads/${b}:refs/remotes/origin/${b}"', "braced refspec"),
    ("allow", 'echo "$host:$port"',                "colon followed by another expansion"),
    ("allow", "PATH=$PATH:/usr/local/bin",         "colon followed by a path"),
    ("allow", "echo '$b:refs'",                    "inside single quotes nothing expands"),
    ("allow", "cat > s.sh <<'EOF2'\necho $f:h\nEOF2", "a quoted heredoc body is literal"),
    ("deny",  "cat > s.sh <<EOF2\necho $f:h\nEOF2",   "an unquoted heredoc body expands"),
    ("deny",  "cat > s.sh <<'EOF2'\nx\nEOF2\necho $f:h", "after the quoted heredoc ends"),
    ("allow", "echo \\$b:r",                        "an escaped dollar"),
    ("deny",  'echo "x #$f:h"',                     "a # inside double quotes still expands"),
    ("allow", "cat <<<'hello'\necho ${f}:h",        "a here-string is not a heredoc"),
    ("deny",  "cat <<<'hello'\necho $f:h",          "...so the line after it is still checked"),
    # --- inplace-edit: switch clusters -------------------------------------------
    ("allow", "perl -Mstrict -e 'print 1'",        "perl -M is not -i"),
    ("allow", "perl -Ilib t/x.t",                  "perl -I is not -i"),
    ("deny",  "/usr/bin/sed -i '' 's/a/b/' x.py",  "sed by absolute path"),
    ("deny",  "gsed -i 's/a/b/' x.py",             "GNU sed"),
    ("deny",  "sed -e s/a/b/ -i x.py",             "-i after an -e operand"),
    ("deny",  "perl -pie 's/a/b/' x.py",           "perl -pie"),
    ("allow", "git commit -q -F - <<'EOF'\nfalse allows (/usr/bin/sed -i, gsed -i)\nEOF", "sed -i named in a commit-message heredoc"),
    ("allow", "cat > n.md <<EOF\nuse sed -i here\nEOF",  "sed -i in an unquoted heredoc body is still data"),
    ("deny",  "cat > n.md <<'EOF'\nx\nEOF\nsed -i '' 's/a/b/' x.py", "sed -i after the heredoc ends"),
    ("deny",  "sed -i'' 's/a/b/' x.py",            "-i'' glued"),
    ("deny",  'sed -i"" s/a/b/ x.py',              '-i"" glued'),
    ("deny",  "sed -ibak 's/a/b/' x.py",           "-ibak suffix"),
    ("deny",  "cat <<<'hello'\nsed -i '' 's/a/b/' x.py", "a here-string does not hide the next line"),
    ("deny",  "read x <<<foo\nsed -i '' s/a/b/ x.py",    "...unquoted here-string"),
    ("deny",  "bash <<'EOF'\nsed -i 's/a/b/' x.py\nEOF", "a heredoc fed to bash is code"),
    ("deny",  "bash <<EOF\nsed -i 's/a/b/' x.py\nEOF",   "...unquoted"),
    ("deny",  "find . -name '*.py' -exec sed -i 's/a/b/' {} +", "sed -i under find -exec"),
    ("allow", "rg sed -i src",                     "sed as a search word"),
    ("allow", "grep -rn sed src -i",               "sed as a grep pattern with -i"),
]


def _sections():
    """Map section name -> its slice of CASES, by source position rather than by label.

    Convention: a line whose stripped form starts with `# --- ` opens a section named by its
    third whitespace-delimited word. Do not write that prefix inside a case string.
    """
    src = open(__file__).read()
    node = next(n for n in ast.parse(src).body
                if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "CASES")
    bounds, end = {}, node.end_lineno or node.lineno
    for i, line in enumerate(src.split("\n"), 1):
        st = line.strip()
        if st.startswith("# --- ") and node.lineno < i < end:
            bounds[i] = st.split()[2]
    if not isinstance(node.value, ast.List):
        raise TypeError("CASES must be a list literal")
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
for _d in ("aw/tasks/t/scripts", "aw/tasks/t/worktrees/r", "aw/tasks/t/clone", "scratchpad"):
    os.makedirs(os.path.join(FIX, _d), exist_ok=True)
WR_SCRIPT = _fixture("aw/tasks/t/scripts/x.sh", "a\n")
WR_WT = _fixture("aw/tasks/t/worktrees/r/a.py", "a = 1\n")
# Test the legacy temporary-path exemption only when the selected test root is there.
# Otherwise use the configured work root, keeping every fixture in the test workspace.
SCR_ROOT = os.path.realpath(os.environ.get("TMPDIR") or FIX)
SCR_IS_SYSTEM_TEMP = SCR_ROOT in ("/tmp", "/private/tmp") or SCR_ROOT.startswith(("/tmp/", "/private/tmp/"))
if not SCR_IS_SYSTEM_TEMP:
    SCR_ROOT = os.path.join(FIX, "aw/tasks/t")
SCR_DIR = tempfile.mkdtemp(prefix=".bash-guards-scratch-", dir=SCR_ROOT)
atexit.register(shutil.rmtree, SCR_DIR, ignore_errors=True)
SCR_FILE = os.path.join(SCR_DIR, "x.txt")
open(SCR_FILE, "w").write("a\n")
NAMED_SCR = _fixture("scratchpad/x.txt", "a\n")
REAL_PY = _fixture("real.py", "a = 1\n")
# A git clone inside the work root is code (B-5).
subprocess.run(["git", "init", "-q", os.path.join(FIX, "aw/tasks/t/clone")], check=True)
WR_CLONE = _fixture("aw/tasks/t/clone/app.py", "a = 1\n")
KWR = _fixture("kit-wr.env", f"AGENT_WORK_ROOT={FIX}/aw\n")
# A ~/ path to the work root, for the literal-tilde form.
WR_TILDE = "~/" + os.path.relpath(WR_SCRIPT, os.path.expanduser("~"))
KIT_CASES = [
    (KIT,        "ask",   "git diff rel-7..HEAD",            "two-dot vs an overlay release branch"),
    (KIT,        "ask",   "git log trunk..HEAD",             "...another alternative of it"),
    (KIT,        "allow", "git diff rel-7...HEAD",           "three-dot vs an overlay release branch"),
    (os.devnull, "allow", "git diff rel-7..HEAD",            "no overlay: only origin/*, main, master are bases"),
    (os.devnull, "ask",   "git diff origin/master..HEAD",    "no overlay: a generic base still asks"),
    (os.devnull, "deny",  f"{_M} -e 'select 1'",             "no overlay: raw client still denied"),
    (os.devnull, "deny",  f"python3 -c 'import {_PYM}'",     "no overlay: driver import still denied"),
    (os.devnull, "deny",  "git commit -m 'test'",            "no overlay: trailers still required"),
    # inplace-edit: throwaway files under the scratchpad and the work root are exempt.
    (KWR,        "allow", f"sed -i '' 's/a/b/' {WR_SCRIPT}",  "in-place: a work-root script"),
    (KWR,        "deny",  f"sed -i '' 's/a/b/' \"{SCR_FILE}\"", "in-place: a quoted target is not pinned down (narrowed)"),
    (KWR,        "allow", f"sed -i '' 's/a/b/' {WR_TILDE}",   "in-place: a literal ~/ work-root path"),
    (KWR,        "allow", f"sed -E -i '' -e 's/a/b/' -e 's/c/d/' {SCR_FILE} 2>/dev/null", "in-place: -e twice, a /dev/null redirect"),
    (KWR,        "allow", f"perl -0777 -pi.bak -e 's/a/b/' {SCR_FILE}", "in-place: perl with -0777 and a suffix"),
    # B-4 / CX-4: one exempt file must not carry targets the parse cannot pin down.
    (KWR,        "deny",  f"sed -i '' 's/a/b/' {SCR_FILE} {FIX}/*.py", "in-place: a glob beside an exempt file"),
    (KWR,        "deny",  f"sed -i '' 's/a/b/' {SCR_FILE} {FIX}/{{real.py,big.md}}", "in-place: a brace expansion beside it"),
    (KWR,        "deny",  f"sed -i '' 's/a/b/' {SCR_FILE} 'real.py'", "in-place: a single-quoted target beside it"),
    (KWR,        "deny",  f"sed -i '' 's/a/b/' {SCR_FILE} real.py", "in-place: a relative target (a cd may precede it)"),
    (KWR,        "deny",  f"cd {FIX} && sed -i '' 's/a/b/' {SCR_FILE} real.py", "in-place: cd then a relative target"),
    (KWR,        "deny",  f"find {FIX} -name '*.py' -exec sed -i '' 's/a/b/' {SCR_FILE} {{}} +", "in-place: find -exec targets"),
    (KWR,        "deny",  f"git ls-files | xargs sed -i '' 's/a/b/' {SCR_FILE}", "in-place: xargs targets"),
    (KWR,        "deny",  f"perl -pi -e 's/a/b/' {SCR_FILE} \"$F\"", "in-place: perl with a $var target"),
    (KWR,        "deny",  f"gsed -i 's/a/b/' {REAL_PY} {SCR_FILE}", "in-place: GNU -i, the outside file read as the script refuses"),
    # B-5: a git checkout under /tmp or the work root, or a dir merely named scratchpad, is code.
    (KWR,        "deny",  f"sed -i '' 's/a/b/' {WR_CLONE}",   "in-place: a clone in the work root"),
    (KWR,        "deny",  f"sed -i '' 's/a/b/' {NAMED_SCR}",  "in-place: a dir named scratchpad outside /tmp"),
    (KWR,        "allow", f"perl -pi -e 's/a/b/' {SCR_FILE} {WR_SCRIPT}", "in-place: perl, every file exempt"),
    (KWR,        "allow", f"cat {SCR_FILE} && sed -i.bak -e 's/a/b/' {SCR_FILE}", "in-place: after another command"),
    (KWR,        "deny",  f"sed -i '' 's/a/b/' {SCR_FILE} {REAL_PY}", "in-place: one file outside refuses"),
    (KWR,        "deny",  f"sed -i '' 's/a/b/' {SCR_FILE}; sed -i '' 's/a/b/' {REAL_PY}", "in-place: a second command outside"),
    (KWR,        "deny",  f"sed -i '' 's/a/b/' {WR_WT}",      "in-place: a worktree in the work root is code"),
    (KWR,        "deny",  f"sed -i '' 's/a/b/' {SCR_DIR}/../real.py", "in-place: .. out of the scratchpad"),
    (KWR,        "deny",  "sed -i '' 's/a/b/' \"$F\"",        "in-place: an unknown $var target"),
    (KWR,        "deny",  f"sed -i '' 's/a/b/' {SCR_DIR}/missing.txt", "in-place: no existing file named"),
    (KWR,        "deny",  f"ls {SCR_DIR} | xargs sed -i '' 's/a/b/'", "in-place: targets from stdin"),
    (os.devnull, "allow" if SCR_IS_SYSTEM_TEMP else "deny", f"sed -i '' 's/a/b/' {SCR_FILE}", "in-place: only a legacy temporary path needs no overlay"),
    (os.devnull, "deny",  f"sed -i '' 's/a/b/' {WR_SCRIPT}",  "in-place: without the overlay that dir is not the work root"),
]


# (want, command, seconds, label): each must also finish within its bound. A glob that fails to match
# "security" took the Keychain check's matcher ~C(8+k, k) steps for k stars, and a hook past its 10s
# timeout fails open, so one slow word disabled every check after it.
TIMED = [
    ("allow", "tar czf a.tgz " + "*" * 16 + "z",           1.0, "16 stars (1.7s before)"),
    ("deny",  f"{_M} -e 'SELECT 1' " + "*" * 22 + "z",      1.0, "22 stars before the raw-client scan (15.5s before)"),
    ("allow", "echo x; " + "*" * 64 + "z",                 1.0, "64 stars as a command word"),
    ("deny",  "*" * 64 + "y -i < /tmp/cmds",               1.0, "...and 64 stars spelling security"),
    ("deny",  "/usr/bin/" + "?*" * 40 + "y -i < /tmp/cmds", 1.0, "a glob too long to be a name is refused"),
    ("allow", "echo security " + "a" * 60000,               1.0, "a 60KB word (its basename regex was quadratic: 9s)"),
    ("deny",  "echo security " + "a" * 70000,               1.0, "a command over 64KB is refused, not lexed"),
    ("allow", "security list-keychains; " + "a b|" * 16000 + "sh", 4.0, "64KB of the lexer's costliest shape"),
    ("deny",  "echo " + "! " * 20000 + "while\ndate\nwhile true; do sleep 20; done", 4.0,
     "40KB of keywords before a poll (the per-keyword rescan took 13s)"),
]


BG_CASES = [
    ("allow", "until gh run view 1 | grep -q done; do sleep 30; done", "foreground-poll: a background wait"),
    ("allow", "gh pr checks 1 --watch",                                "foreground-poll: --watch in the background"),
]


def decision(cmd, kit=None, timeout=None, bg=False):
    env = None if kit is None else {**os.environ, "KIT_ENV": kit}
    try:
        r = subprocess.run(
            [H], input=json.dumps({"cwd": FIX, "tool_input": {"command": cmd, "run_in_background": bg}}),
            capture_output=True, text=True, env=env, timeout=timeout, cwd=FIX,
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
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
    if want_section in ("overlay", "timing"):
        cases = []
    elif want_section:
        if want_section not in SECTIONS:
            raise SystemExit(f"no section {want_section!r}; try one of: overlay, timing, {', '.join(sorted(SECTIONS, key=str))}")
        cases = [(None, *c) for c in SECTIONS[want_section]]
        print(f"[{want_section}: {len(cases)} cases]\n")
    if want_section in (None, "overlay"):
        cases += KIT_CASES
    timed = TIMED if want_section in (None, "timing") else []

    fails = 0
    try:
        for kit, want, cmd, label in cases:
            got = decision(cmd, kit)
            if got != want:
                fails += 1
            print(f"{'ok  ' if got == want else 'FAIL'} want={want:<5} got={got:<5} {label}")
        bg_cases = BG_CASES if want_section in (None, "foreground-poll") else []
        for want, cmd, label in bg_cases:
            got = decision(cmd, bg=True)
            fails += got != want
            print(f"{'ok  ' if got == want else 'FAIL'} want={want:<5} got={got:<5} {label}")
        cases += [(None, *c) for c in bg_cases]
        for want, cmd, bound, label in timed:
            start = time.monotonic()
            got = decision(cmd, timeout=bound + 20)
            took = time.monotonic() - start
            ok = got == want and took <= bound
            fails += not ok
            print(f"{'ok  ' if ok else 'FAIL'} want={want:<5} got={got:<5} {took:5.2f}s (max {bound}s) {label}")
        cases += timed
        for _p in (os.path.join(FIX, "PWNED"), os.path.join(os.getcwd(), "PWNED")):
            assert not os.path.exists(_p), \
                f"context-dumps EXECUTED a crafted filename ({_p})"
    finally:
        pass   # fixtures are removed by the atexit hook registered on FIX
    print(f"\n{len(cases) - fails}/{len(cases)} passed")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
