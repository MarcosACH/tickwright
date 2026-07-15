"""The ``.githooks`` delegation contract: a local-only guard that may not be there.

``.githooks/pre-commit`` and ``.githooks/commit-msg`` both end by handing off to a
private guard kept outside the repo, so it is never published. ``CONTRIBUTING.md``
tells every contributor to enable ``core.hooksPath .githooks`` and almost none of
them have that guard — "no guard on this clone" is the common case, not the exotic
one, and it must be a silent no-op.

Driven through a real ``git`` in a scratch repo: the hooks' whole subject is git's
exit-code contract, so nothing here is mocked (a process boundary). Global and
system git config are pinned to ``/dev/null`` — a developer's ``core.hooksPath`` or
``commit.gpgsign`` must not reach an outcome, the same hermeticity the rest of the
suite holds to.
"""

import os
import shutil
import subprocess
from pathlib import Path

_GITHOOKS = Path(__file__).resolve().parent.parent / ".githooks"

_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git in ``cwd``, raising unless the caller is the one asserting on the status."""
    return subprocess.run(
        ["git", *args], cwd=cwd, env=_ENV, capture_output=True, text=True, check=check
    )


def _repo(root: Path) -> Path:
    """A scratch repo with the real ``.githooks`` wired up the documented way."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    shutil.copytree(_GITHOOKS, root / ".githooks")
    _git(root, "config", "core.hooksPath", ".githooks")
    return root


def _commit(cwd: Path, name: str) -> subprocess.CompletedProcess[str]:
    """Stage and commit a non-Python file, so the hook's ruff block is not in play."""
    (cwd / name).write_text("scratch\n")
    _git(cwd, "add", name)
    # Unchecked: this status is the hook's verdict, which every test here asserts on.
    return _git(cwd, "commit", "-m", f"add {name}", check=False)


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
