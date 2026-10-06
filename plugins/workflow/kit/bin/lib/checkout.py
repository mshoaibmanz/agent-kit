"""The kit checkout under git: the one git runner (pack.git_head runs it too), a dev install's checks,
and the git steps of agent-setup update. They refuse with ValueError and never stash, reset or merge
the user's work."""

from __future__ import annotations

from collections.abc import Iterable
import io
from pathlib import Path
import subprocess
import tarfile
from typing import Any


def git(folder: Path, *arguments: str, timeout: int = 120, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-C', str(folder), *arguments], capture_output=True, text=text, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def short(commit: str | None) -> str:
    return commit[:12] if commit else 'none'


def dirty(folder: Path) -> int:
    """Tracked paths with uncommitted changes; 0 outside a git checkout. Untracked files are not kit
    files: no install links or copies them until they are committed and synced."""
    status = git(folder, 'status', '--porcelain', '--untracked-files=no', timeout=10)
    return len(status.stdout.splitlines()) if status.returncode == 0 else 0


def dev_problems(root: Path, source: Path, managed: Iterable[dict[str, Any]]) -> list[str]:
    """What breaks a dev install: its checkout gone, or a kit link whose file left it. A link changed to
    point elsewhere is drift, which doctor reports for every managed path."""
    if not (source / 'bin/agent-setup').is_file():
        return [f'dev source {source} is missing: every linked kit file is broken. Restore it, or rerun '
                'agent-setup --no-dev --source <checkout> --apply']
    dangling = [record['target'] for record in managed if record['kind'] == 'link'
                and root in Path(record['target']).parents and not Path(record['target']).exists()]
    if dangling:
        return [f'{len(dangling)} kit link(s) name a file gone from {source} (first {dangling[0]}): '
                'run agent-kit sync to retire them']
    return []


def refusal(checkout: Path, reason: str) -> ValueError:
    return ValueError(f'update refused: {checkout} {reason}; nothing changed')


def upstream_ahead(checkout: Path) -> tuple[str, str]:
    """(HEAD, its upstream's commit) of a checkout that can fast-forward to that upstream, fetched now.
    Refuses a checkout that is not git, has uncommitted changes to tracked files, a detached HEAD, no
    upstream branch, or commits its upstream lacks."""
    if git(checkout, 'rev-parse', '--is-inside-work-tree').returncode:
        raise refusal(checkout, 'is not a git checkout')
    if dirty(checkout):
        raise refusal(checkout, 'has uncommitted changes: commit or stash them yourself, then rerun')
    if git(checkout, 'symbolic-ref', '-q', 'HEAD').returncode:
        raise refusal(checkout, 'has a detached HEAD: check out its branch, then rerun')
    if git(checkout, 'rev-parse', '--abbrev-ref', '@{upstream}').returncode:
        raise refusal(checkout, 'has no upstream branch: git branch --set-upstream-to origin/main, then rerun')
    # fetch, then `merge --ff-only` (fast_forward): no pull.rebase or pull.ff setting is in play.
    fetched = git(checkout, 'fetch', '--quiet')
    if fetched.returncode:
        raise refusal(checkout, 'could not fetch its upstream: ' + one_line(fetched.stderr, 'git fetch failed'))
    if git(checkout, 'merge-base', '--is-ancestor', 'HEAD', '@{upstream}').returncode:
        raise refusal(checkout, 'has commits its upstream lacks, so it cannot fast-forward: push or rebase them yourself')
    return (git(checkout, 'rev-parse', 'HEAD').stdout.strip(), git(checkout, 'rev-parse', '@{upstream}').stdout.strip())


def export(checkout: Path, commit: str, folder: Path) -> None:
    """The tree of commit, written to folder (git archive): run before the checkout moves to it."""
    archive = git(checkout, 'archive', '--format=tar', commit, text=False)
    if archive.returncode:
        raise refusal(checkout, f'could not read {short(commit)}: ' + one_line(archive.stderr.decode(), 'git archive failed'))
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        if hasattr(tarfile, 'data_filter'):
            tar.extractall(folder, filter='data')
        else:
            tar.extractall(folder)  # noqa: S202 (git's own archive of the user's checkout)


def exported(path: str, checkout: Path, folder: Path) -> str:
    """path as a preview of the export in folder reads it: a path git tracks in checkout becomes its copy
    there (the upstream's version, or none when the upstream deleted it); any other path is unchanged."""
    try:
        relative = Path(path).expanduser().resolve().relative_to(checkout.resolve())
    except ValueError:
        return path
    if not relative.parts or git(checkout, 'ls-files', '--error-unmatch', '--', str(relative)).returncode:
        return path
    return str(folder / relative)


def fast_forward(checkout: Path, commit: str) -> None:
    """Moves the checkout's branch to commit. git refuses to overwrite an untracked file itself."""
    merged = git(checkout, 'merge', '--ff-only', '--quiet', commit)
    if merged.returncode:
        raise refusal(checkout, 'could not fast-forward: ' + one_line(merged.stderr, 'git merge failed'))


def one_line(text: str, default: str) -> str:
    """git's message on one line: an untracked file a merge would overwrite is named on its own line."""
    return ' '.join(line.strip() for line in text.strip().splitlines()) or default
