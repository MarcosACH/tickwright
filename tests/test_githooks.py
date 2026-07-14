"""The ``.githooks`` delegation contract: a local-only guard that may not be there.

``.githooks/pre-commit`` ends by handing off to a private guard kept outside the
repo (``<git-common-dir>/hooks-local/pre-commit``), so it is never published.
``CONTRIBUTING.md`` tells every contributor to enable ``core.hooksPath .githooks``
and almost none of them have that guard — "no guard on this clone" is the common
case, not the exotic one, and it must be a silent no-op. A guard that *is* present
and rejects must still block the commit, and a linked worktree must resolve to the
same guard as the main checkout.

Driven through a real ``git`` in a scratch repo: the hook's whole subject is git's
exit-code contract, so nothing here is mocked. Global/system git config is pinned
to ``/dev/null`` — a developer's ``core.hooksPath`` or ``commit.gpgsign`` must not
reach an outcome (the same hermeticity the Python suite holds to).
"""

import os
import shutil
import subprocess
from pathlib import Path

_GITHOOKS = Path(__file__).resolve().parent.parent / ".githooks"

# A guard is only consulted when executable; keep the bit explicit at both ends.
# Both announce themselves on stderr, which git forwards whether the hook passes or
# fails — so "did the guard actually run?" stays answerable even in the cases where
# it lets the commit through and the exit code alone would not say.
_GUARD_REJECTS = "#!/usr/bin/env bash\necho 'guard says no' >&2\nexit 1\n"
_GUARD_ACCEPTS = "#!/usr/bin/env bash\necho 'guard says yes' >&2\nexit 0\n"

_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git in ``cwd``, raising unless the caller is the one asserting on the status.

    Every call here but the commit under test is harness setup, and setup that fails
    quietly is precisely how this file's first draft passed vacuously — so the default
    is to raise rather than let a broken scratch repo read as a result.
    """
    return subprocess.run(
        ["git", *args], cwd=cwd, env=_ENV, capture_output=True, text=True, check=check
    )


def _repo(root: Path) -> Path:
    """A scratch repo with the real ``.githooks`` wired up the documented way.

    The hooks are *committed* before ``core.hooksPath`` is set: a linked worktree
    checks out tracked content only, so an untracked ``.githooks/`` would leave it
    pointing at a directory that does not exist — no hook would run and every
    assertion here would pass vacuously.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    shutil.copytree(_GITHOOKS, root / ".githooks")
    _git(root, "add", ".githooks")
    _git(root, "commit", "-q", "-m", "hooks")  # no hooksPath yet, so none run
    _git(root, "config", "core.hooksPath", ".githooks")
    return root


def _install_guard(git_common_dir: Path, hook: str, body: str = _GUARD_REJECTS) -> None:
    guard = git_common_dir / "hooks-local" / hook
    guard.parent.mkdir(parents=True, exist_ok=True)
    guard.write_text(body)
    guard.chmod(0o755)


def _commit(cwd: Path, name: str) -> subprocess.CompletedProcess[str]:
    """Stage and commit a non-Python file, so the ruff block is not in play."""
    (cwd / name).write_text("scratch\n")
    _git(cwd, "add", name)
    # Unchecked: this status is the hook's verdict, which every test here asserts on.
    return _git(cwd, "commit", "-m", f"add {name}", check=False)


def _subject(cwd: Path) -> str:
    """The subject of HEAD — what did (or did not) land."""
    return _git(cwd, "log", "-1", "--format=%s").stdout.strip()


def test_pre_commit_allows_the_commit_when_no_local_guard_is_installed(
    tmp_path: Path,
) -> None:
    """The documented setup on a clone without the unpublished guard.

    This is every contributor who follows ``CONTRIBUTING.md``. A missing optional
    guard means "skip it", and the hook must not turn that into a rejection.
    """
    repo = _repo(tmp_path / "repo")

    result = _commit(repo, "README.md")

    assert result.returncode == 0, (
        f"commit rejected with no guard installed:\n{result.stdout}\n{result.stderr}"
    )
    assert _subject(repo) == "add README.md"


def test_pre_commit_allows_the_commit_when_the_local_guard_accepts(
    tmp_path: Path,
) -> None:
    """A present guard that passes lets the commit land — and is proven to have run.

    The fourth quadrant of guard absent/present × accepts/rejects, and the only case
    that exercises the delegation propagating a guard's *success*. The stderr
    assertion is load-bearing: a guard that is never found also lets the commit land,
    so without it this would be indistinguishable from the no-guard test above —
    passing for the wrong reason, which is the failure this file has already been
    bitten by once.
    """
    repo = _repo(tmp_path / "repo")
    _install_guard(repo / ".git", "pre-commit", _GUARD_ACCEPTS)

    result = _commit(repo, "README.md")

    assert result.returncode == 0, (
        f"a passing guard blocked the commit:\n{result.stdout}\n{result.stderr}"
    )
    assert "guard says yes" in result.stderr, "the guard was never found or run"
    assert _subject(repo) == "add README.md"


def test_pre_commit_still_blocks_when_the_local_guard_rejects(tmp_path: Path) -> None:
    """A present guard keeps its veto — the no-guard fix must not disarm it.

    Only the *absent* case is being changed, so this pins the other half: a future
    rewrite of the delegation must not buy "an absent guard allows" by making a
    present guard's rejection unreachable too.
    """
    repo = _repo(tmp_path / "repo")
    _install_guard(repo / ".git", "pre-commit")

    result = _commit(repo, "README.md")

    assert result.returncode != 0, "a rejecting guard did not block the commit"
    assert "guard says no" in result.stderr
    assert _subject(repo) == "hooks", "the rejected commit landed anyway"


def test_pre_commit_finds_the_local_guard_from_a_linked_worktree(tmp_path: Path) -> None:
    """A worktree resolves the same guard as the main checkout.

    ``--git-dir`` points at ``.git/worktrees/<name>`` here, so a guard looked up
    that way is invisible and the scrub silently stops running where commits are
    just as real. Proven via the guard's own veto: only a guard that was actually
    found can reject.
    """
    repo = _repo(tmp_path / "repo")
    _install_guard(repo / ".git", "pre-commit")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", str(linked), "-b", "side")

    result = _commit(linked, "NOTES.md")

    assert result.returncode != 0, "the guard was not found from the worktree"
    assert "guard says no" in result.stderr


def test_commit_msg_finds_the_local_guard_from_a_linked_worktree(tmp_path: Path) -> None:
    """The message scrub resolves the same guard from a worktree as from ``.git``.

    Same ``--git-dir`` lookup as ``pre-commit``, and the same blind spot: this hook
    never had the exit-code bug (``exec`` plus a trailing ``exit 0`` already made an
    absent guard a no-op), so a worktree failed quietly here — the scrub simply
    stopped running rather than announcing itself.
    """
    repo = _repo(tmp_path / "repo")
    _install_guard(repo / ".git", "commit-msg")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", str(linked), "-b", "side")

    result = _commit(linked, "NOTES.md")

    assert result.returncode != 0, "the guard was not found from the worktree"
    assert "guard says no" in result.stderr
