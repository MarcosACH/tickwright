"""The ``.githooks`` delegation contract: a local hook guard that may not be there.

``.githooks/pre-commit`` and ``.githooks/commit-msg`` both end by handing off to a
private guard kept outside the repo, so it is never published. The contract these
tests fence — location, arming, authority — is stated in ``CONTRIBUTING.md``
("Local hook guards"); this file pins it, it does not redefine it.

``CONTRIBUTING.md`` tells every contributor to enable ``core.hooksPath .githooks``
and almost none of them have that guard — "no guard on this clone" is the common
case, not the exotic one, and it must be a silent no-op.

Driven through a real ``git`` in a scratch repo: the hooks' whole subject is git's
exit-code contract, so nothing here is mocked (a process boundary). Global and
system git config are pinned to ``/dev/null`` — a developer's ``core.hooksPath`` or
``commit.gpgsign`` must not reach an outcome, the same hermeticity the rest of the
suite holds to.
"""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

_GITHOOKS = Path(__file__).resolve().parent.parent / ".githooks"

# A guard is only consulted when executable; keep the bit explicit at both ends.
# It announces itself on stderr, which git forwards whether the hook passes or
# fails — so "did the guard actually run?" stays answerable in the cases where the
# exit code alone would not say.
_GUARD_REJECTS = "#!/usr/bin/env bash\necho 'guard says no' >&2\nexit 1\n"
_GUARD_ACCEPTS = "#!/usr/bin/env bash\necho 'guard says yes' >&2\nexit 0\n"

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
    """A scratch repo with the real ``.githooks`` wired up the documented way.

    The hooks are *committed* before ``core.hooksPath`` is set, so no hook runs
    during setup and HEAD gives the tests a baseline subject to compare against.
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
    """Plant a stand-in for the private guard where the real one lives."""
    guard = git_common_dir / "hooks-local" / hook
    guard.parent.mkdir(parents=True, exist_ok=True)
    guard.write_text(body)
    guard.chmod(0o755)


def _subject(cwd: Path) -> str:
    """The subject of HEAD — what did (or did not) land."""
    return _git(cwd, "log", "-1", "--format=%s").stdout.strip()


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
    assert _subject(repo) == "add README.md"


def test_pre_commit_allows_the_commit_when_the_local_guard_accepts(
    tmp_path: Path,
) -> None:
    """A present guard that passes lets the commit land — and is proven to have run.

    The stderr assertion is load-bearing: a guard that is never found *also* lets the
    commit land, so without it this would be indistinguishable from the no-guard test
    above, and the whole file could pass with the hook never wired up at all.
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
    """A present guard keeps its veto — the absent-guard fix must not disarm it.

    Only the *absent* case is being changed, so this pins the other half. It is the
    fence that rejects ``[ -x "$guard" ] && "$guard" || true``, which buys "an absent
    guard allows" by making a present guard's rejection unreachable too — silently
    disabling the private reference scrub.
    """
    repo = _repo(tmp_path / "repo")
    _install_guard(repo / ".git", "pre-commit")

    result = _commit(repo, "README.md")

    assert result.returncode != 0, "a rejecting guard did not block the commit"
    assert "guard says no" in result.stderr
    assert _subject(repo) == "hooks", "the rejected commit landed anyway"


def test_pre_commit_finds_the_local_guard_from_a_linked_worktree(tmp_path: Path) -> None:
    """A worktree resolves the same guard as the main checkout.

    ``--git-dir`` is ``.git/worktrees/<name>`` here, not ``.git``, so a guard looked
    up that way is invisible exactly in a worktree — and the scrub stops running on
    commits that are every bit as real. Proven via the guard's own veto: only a guard
    that was actually found can reject.
    """
    repo = _repo(tmp_path / "repo")
    _install_guard(repo / ".git", "pre-commit")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", str(linked), "-b", "side")

    result = _commit(linked, "NOTES.md")

    assert result.returncode != 0, "the guard was not found from the worktree"
    assert "guard says no" in result.stderr


def test_pre_commit_keeps_unstaged_work_when_the_local_guard_rejects(
    tmp_path: Path,
) -> None:
    """A rejected commit leaves the developer's unstaged hunks where they left them.

    ``pre-commit`` peels unstaged changes off the working tree so it formats only
    staged content, and restores them from an EXIT trap. The guard runs *inside* that
    window, so how it is invoked decides whether the trap survives a rejection.

    This is the fence against unifying the delegation on ``exec`` the way ``commit-msg``
    does it — which reads as tidier and silently eats uncommitted work: ``exec``
    replaces the process, the trap is discarded, and the peeled hunks survive only in
    a ``.git`` patch file the developer never knew about. ``commit-msg`` can ``exec``
    safely only because it has no trap to lose.
    """
    repo = _repo(tmp_path / "repo")
    _install_guard(repo / ".git", "pre-commit")
    tracked = repo / "notes.md"
    tracked.write_text("line1\nline2\n")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-q", "-m", "notes", "--no-verify")

    tracked.write_text("line1\nstaged edit\n")
    _git(repo, "add", "notes.md")
    tracked.write_text("line1\nstaged edit\nprecious unstaged work\n")

    result = _git(repo, "commit", "-m", "try", check=False)

    assert result.returncode != 0, "a rejecting guard did not block the commit"
    assert "precious unstaged work" in tracked.read_text(), (
        "the guard's rejection swallowed the developer's unstaged hunks"
    )


def test_commit_msg_finds_the_local_guard_from_a_linked_worktree(tmp_path: Path) -> None:
    """The message scrub resolves the same guard from a worktree as from ``.git``.

    ``commit-msg`` shares ``pre-commit``'s ``--git-dir`` lookup and the same blind
    spot, but never had the exit-status bug — ``exec`` plus a trailing ``exit 0``
    already made an absent guard a clean no-op. So a worktree failed *quietly* here:
    the scrub simply stopped running rather than announcing itself, which is why this
    half went unnoticed while its sibling was loudly aborting.
    """
    repo = _repo(tmp_path / "repo")
    _install_guard(repo / ".git", "commit-msg")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", str(linked), "-b", "side")

    result = _commit(linked, "NOTES.md")

    assert result.returncode != 0, "the guard was not found from the worktree"
    assert "guard says no" in result.stderr


# --- pre-push autostash across the shared stash stack (issue #83) ----------------
#
# ``pre-push`` autostashes uncommitted work, runs ``uv run mypy .``, and pops in an
# EXIT trap. ``refs/stash`` lives in the common git dir and is shared by every linked
# worktree, so a pop of the unqualified ``stash@{0}`` can restore an entry another
# worktree pushed while ``mypy`` ran — applying that worktree's work into this one and
# dropping its stash entry, silently when the patches don't conflict.
#
# The race needs no concurrency to pin: the defect is that the pop is unqualified, so
# a *sequential* interleaving reproduces it. The stub ``uv`` below is that interleaving
# — it stands in for ``mypy`` (the tests can't run the real thing) and, in the same
# breath, pushes a foreign stash onto the shared stack *between* the hook's push and
# its pop. A stub on ``PATH`` is a process boundary, so it keeps the no-mocking-our-code
# rule the rest of this file holds to.


def _stub_uv_stashing(worktree: Path) -> str:
    """A stand-in ``uv`` that, mid-window, stashes ``worktree`` onto the shared stack.

    This is the foreign push the hook must not restore: it runs while the hook holds
    its own autostash open, so the entry it creates becomes ``stash@{0}`` — exactly
    the slot an unqualified ``git stash pop`` would take.
    """
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"cd {shlex.quote(str(worktree))}\n"
        'git stash push --quiet --message "foreign worktree autostash"\n'
        "exit 0\n"
    )


def _stub_uv_noop() -> str:
    """A stand-in ``uv`` that only stands in for ``mypy`` — no foreign stash."""
    return "#!/usr/bin/env bash\nexit 0\n"


def _install_stub_uv(bin_dir: Path, body: str) -> dict[str, str]:
    """Plant an executable ``uv`` in ``bin_dir`` and return an env that finds it first.

    The hooks invoke ``uv run <tool>`` (``mypy`` in pre-push, ``ruff`` in pre-commit);
    prepending ``bin_dir`` to ``PATH`` is what puts this stub in that seat instead of the
    real toolchain.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    uv = bin_dir / "uv"
    uv.write_text(body)
    uv.chmod(0o755)
    return {**_ENV, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def _pre_push_repo(root: Path) -> Path:
    """A scratch repo wired for ``pre-push``: a bare ``origin`` and a Python baseline.

    No ``origin/main`` is fetched, so the hook's ``merge-base origin/main`` finds no
    base and takes its "check everything" branch — the autostash path runs on every
    push here, without the test having to arrange a Python diff in the pushed range.
    """
    origin = root.parent / "origin.git"
    _git(root.parent, "init", "--bare", "-q", str(origin))
    repo = _repo(root)
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "own.py").write_text("x = 1\n")
    (repo / "foreign.py").write_text("y = 2\n")
    _git(repo, "add", "own.py", "foreign.py")
    _git(repo, "commit", "-q", "--no-verify", "-m", "baseline")
    return repo


def _push(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Push a fresh branch, running ``pre-push`` under ``env`` (which finds the stub)."""
    return subprocess.run(
        ["git", "push", "-q", "origin", "HEAD:refs/heads/probe"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _arrange_foreign_stash_race(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """Wire the deterministic interleaving both worktree tests below share.

    The main worktree carries its own uncommitted hunk into a push; the linked
    worktree carries a distinct one, left dirty for the stub ``uv`` to stash onto the
    shared stack while ``mypy`` "runs". Returns ``(repo, linked, env)`` — ``env`` puts
    the stub in front of the real ``uv`` on ``PATH``.
    """
    repo = _pre_push_repo(tmp_path / "repo")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", str(linked), "-b", "side")

    (repo / "own.py").write_text("x = 1\n# main own hunk\n")
    (linked / "foreign.py").write_text("y = 2\n# foreign hunk\n")

    env = _install_stub_uv(tmp_path / "bin", _stub_uv_stashing(linked))
    return repo, linked, env


def test_pre_push_restores_its_own_autostash_not_a_foreign_one(tmp_path: Path) -> None:
    """The hook restores the work *it* stashed, not whatever reached ``stash@{0}`` since.

    The main worktree pushes with its own uncommitted hunk; while ``mypy`` runs, a
    linked worktree stashes onto the shared stack. An unqualified pop restores the
    linked worktree's entry into the main tree and leaves the main hunk buried one
    slot down — so the developer who pushed loses exactly the work the autostash was
    meant to protect. The fix must restore the entry the hook created and no other.
    """
    repo, _linked, env = _arrange_foreign_stash_race(tmp_path)

    result = _push(repo, env)

    assert result.returncode == 0, f"push failed:\n{result.stdout}\n{result.stderr}"
    assert (repo / "own.py").read_text() == "x = 1\n# main own hunk\n", (
        "pre-push restored a foreign worktree's stash instead of its own autostash"
    )


def test_pre_push_leaves_a_foreign_stash_entry_on_the_stack(tmp_path: Path) -> None:
    """The hook drops only what it created — a foreign entry survives the push intact.

    The mirror of the sibling test: even when the hook correctly ignores the foreign
    ``stash@{0}`` for its own restore, it must not *drop* it either. An unqualified
    ``git stash pop`` removes the entry it applied, so the buggy hook silently deletes
    the linked worktree's stashed work; the fix, staying off the shared stack, leaves
    that entry exactly where the linked worktree left it.
    """
    repo, _linked, env = _arrange_foreign_stash_race(tmp_path)

    result = _push(repo, env)

    assert result.returncode == 0, f"push failed:\n{result.stdout}\n{result.stderr}"
    stash_list = _git(repo, "stash", "list").stdout
    assert "foreign worktree autostash" in stash_list, (
        f"pre-push dropped a stash entry it did not create:\n{stash_list!r}"
    )


def test_pre_push_autostashes_and_restores_cleanly_in_the_single_worktree_case(
    tmp_path: Path,
) -> None:
    """The ordinary case still works: dirty push, own work back, nothing left behind.

    The fence for the fix itself — going off the shared stash stack must not break the
    plain single-worktree push it was always for. With no foreign entry in play, the
    hook stashes the uncommitted hunk for ``mypy``, restores it verbatim, and leaves no
    stray stash entry or parked ref behind.
    """
    repo = _pre_push_repo(tmp_path / "repo")
    (repo / "own.py").write_text("x = 1\n# uncommitted hunk\n")

    env = _install_stub_uv(tmp_path / "bin", _stub_uv_noop())
    result = _push(repo, env)

    assert result.returncode == 0, f"push failed:\n{result.stdout}\n{result.stderr}"
    assert (repo / "own.py").read_text() == "x = 1\n# uncommitted hunk\n", (
        "pre-push did not restore the single worktree's own uncommitted work"
    )
    assert _git(repo, "stash", "list").stdout == "", "pre-push left a stray stash entry"
    parked = _git(
        repo, "rev-parse", "--verify", "-q", "refs/worktree/pre-push-autostash", check=False
    )
    assert parked.returncode != 0, "pre-push left its parked autostash ref behind"


# --- pre-commit rollback when a reformat collides with an unstaged hunk (issue #84) --
#
# ``pre-commit`` formats only *staged* content: it peels unstaged hunks off the working
# tree, runs ``ruff format`` on what's left, and reapplies the hunks from an EXIT trap.
# The one case that peel-and-reapply cannot survive is when ``ruff format`` rewrites a
# line an unstaged hunk also touches — the reversed patch no longer applies. The trap's
# recovery branch (`.githooks/pre-commit` ``restore_unstaged``) then rolls the index and
# working tree all the way back to the pre-hook state and aborts, so neither the staged
# edit nor the unstaged hunk is lost and no half-merged file is left behind.
#
# That branch is unreachable through the guard-delegation tests above: those stage a
# non-Python file on purpose, so the ruff block never runs and nothing is ever
# reformatted. Reaching it means running the ruff block, which shells out to ``uv run
# ruff`` in a scratch repo that has no ``pyproject.toml`` or venv. The stub ``uv`` below
# stands in for that toolchain and makes the reformat deterministic — a process boundary,
# so it keeps the no-mocking-our-code rule the rest of this file holds to.


def _stub_uv_reformatting() -> str:
    """A stand-in ``uv`` whose ``ruff format`` rewrites ``a=2`` to ``a = 2``.

    That single whitespace change is exactly what real ``ruff format`` would make, and it
    lands on the one line the unstaged hunk this scenario arranges also edits — so the
    reversed unstaged patch can no longer reapply, forcing the rollback branch. ``ruff
    check --fix`` is a no-op here, as it is on this input; only ``format`` mutates.
    """
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "fmt=0\n"
        "files=()\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        "    format) fmt=1 ;;\n"
        '    *.py) files+=("$arg") ;;\n'
        "  esac\n"
        "done\n"
        '[ "$fmt" = 1 ] || exit 0\n'
        'for f in "${files[@]}"; do\n'
        '  out="$(sed -E \'s/([[:alnum:]_]+)=([[:alnum:]_]+)/\\1 = \\2/g\' "$f")"\n'
        '  printf \'%s\\n\' "$out" >"$f"\n'
        "done\n"
    )


def _arrange_reformat_collision(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A partially-staged ``.py`` whose staged line ``ruff format`` will rewrite, with an
    unstaged hunk on that very line.

    The baseline commits ``a=1``. The staged edit (``a=2``) is what ``ruff format`` turns
    into ``a = 2``; the unstaged hunk (``a=3``) sits on the same line, so once the staged
    content is reformatted the unstaged patch no longer applies. Returns ``(repo, env)`` —
    ``env`` puts the stub ``uv`` in front of the real toolchain on ``PATH``.
    """
    repo = _repo(tmp_path / "repo")
    mod = repo / "mod.py"
    mod.write_text("a=1\n")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "baseline", "--no-verify")

    mod.write_text("a=2\n")  # staged edit; ruff format will rewrite it to `a = 2`
    _git(repo, "add", "mod.py")
    mod.write_text("a=3\n")  # unstaged hunk on the same line the reformat touches

    env = _install_stub_uv(tmp_path / "bin", _stub_uv_reformatting())
    return repo, env


def _commit_under(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run ``git commit`` with ``env`` so the hook finds the stub ``uv`` on ``PATH``."""
    return subprocess.run(
        ["git", "commit", "-m", "try"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_pre_commit_aborts_when_a_reformat_collides_with_an_unstaged_hunk(
    tmp_path: Path,
) -> None:
    """The rollback branch is reached and refuses the commit with its overlap message.

    This is the tracer: it proves the scenario actually drives execution into
    ``restore_unstaged``'s recovery path — reformatting staged content across a line an
    unstaged hunk also touches — rather than the clean-reapply return just above it. A
    passing commit here would mean the collision never happened and the sibling
    assertions below are vacuous.
    """
    repo, env = _arrange_reformat_collision(tmp_path)

    result = _commit_under(repo, env)

    assert result.returncode != 0, (
        f"the colliding commit was not aborted:\n{result.stdout}\n{result.stderr}"
    )
    assert "staged and unstaged edits overlap" in result.stderr, (
        f"the rollback branch's message did not appear:\n{result.stderr}"
    )
    assert _subject(repo) == "baseline", "the aborted commit landed anyway"


def test_pre_commit_rollback_restores_the_staged_edit_to_the_index(
    tmp_path: Path,
) -> None:
    """The index is rolled back to its pre-hook state — the staged edit, unreformatted.

    ``restore_unstaged`` ``git add``ed the reformatted ``a = 2`` before it discovered the
    collision, so a half-done rollback would strand that reformatted blob in the index.
    The recovery ``git read-tree "$orig_tree"`` must put the *original* staged content
    back, so a retry sees exactly what the developer staged. The staged blob is therefore
    ``a=2`` (the staged edit), not ``a = 2`` (the abandoned reformat) nor ``a=1`` (base).
    """
    repo, env = _arrange_reformat_collision(tmp_path)

    _commit_under(repo, env)

    staged_blob = _git(repo, "show", ":mod.py").stdout
    assert staged_blob == "a=2\n", (
        f"the index was not rolled back to the staged edit: {staged_blob!r}"
    )


def test_pre_commit_rollback_restores_the_working_tree_byte_for_byte(
    tmp_path: Path,
) -> None:
    """The working file is exactly what it was before the hook ran — the unstaged hunk.

    Before the hook, ``mod.py`` on disk was ``a=3`` (the staged ``a=2`` with the unstaged
    hunk on top). The rollback ``checkout-index``es the original staged content back and
    reapplies the unstaged patch, so the file returns to ``a=3`` — not the reformatted
    ``a = 2`` the hook left mid-run, and not a conflict-marked half-merge. This is the
    "never lose work" promise the recovery comment makes, observed on disk.
    """
    repo, env = _arrange_reformat_collision(tmp_path)

    _commit_under(repo, env)

    assert (repo / "mod.py").read_text() == "a=3\n", (
        f"the working tree was not restored to its pre-hook bytes: "
        f"{(repo / 'mod.py').read_text()!r}"
    )


def test_pre_commit_rollback_leaves_no_stranded_patch_file(tmp_path: Path) -> None:
    """The peeled-off unstaged patch is cleaned up even when the reapply fails.

    ``pre-commit`` writes the unstaged hunks to ``$GIT_DIR/pre-commit-unstaged.patch``
    while it reformats. On the clean path it removes that file after reapplying; the
    rollback branch must do the same after restoring by hand. A stranded patch is the
    "half-merged file or lost work" the recovery comment promises never happens — a stale
    hunk the next commit could silently pick up.
    """
    repo, env = _arrange_reformat_collision(tmp_path)

    _commit_under(repo, env)

    patch = repo / ".git" / "pre-commit-unstaged.patch"
    assert not patch.exists(), f"the rollback left a stranded patch file: {patch}"


# --- pre-commit vs a `uv run` that re-locks a dirty uv.lock mid-hook (issue #98) -----
#
# ``pre-commit`` peels unstaged changes off the worktree so it reformats only staged
# content, reverting each unstaged file (``uv.lock`` included) to its committed state,
# then restores them from an EXIT trap. Its ruff block shells out to ``uv run`` in the
# middle of that window — and ``uv run`` re-locks ``uv.lock`` the instant ``pyproject.toml``
# disagrees with it, which the revert momentarily arranges. That mutation moves ``uv.lock``
# out from under the saved patch, so the restore can no longer reapply it and the hook
# aborts an otherwise-valid commit. Same peel/restore machinery as #84, but the mutation
# comes from ``uv run`` *inside* the hook, not a reformat colliding with an unstaged hunk.
#
# The stub ``uv`` below stands in for that toolchain (the scratch repo has no venv) and
# models the one behaviour that matters here: ``uv run`` re-locks ``uv.lock`` unless it is
# told to stay frozen. A stub on ``PATH`` is a process boundary, so it keeps the
# no-mocking-our-code rule the rest of this file holds to.


def _stub_uv_relocking() -> str:
    """A stand-in ``uv`` that re-locks ``uv.lock`` on every ``run`` — unless told not to.

    Real ``uv run``'s mid-hook mutation distilled to its cause: seeing the lockfile as
    stale, it rewrites it. It stands down only when pinned frozen — via ``UV_FROZEN=1`` in
    the environment or ``--frozen`` on the command line, both of which real ``uv`` honours —
    so the tests key on the *behaviour* the fix must produce, not the flag it happens to
    pick. The ``ruff check``/``format`` arguments are irrelevant to the lockfile, so ignored.
    """
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "frozen=0\n"
        '[ "${UV_FROZEN:-0}" = "1" ] && frozen=1\n'
        'for arg in "$@"; do\n'
        '  [ "$arg" = "--frozen" ] && frozen=1\n'
        "done\n"
        "[ \"$frozen\" = 1 ] || printf 'lock = new\\n' >uv.lock\n"
    )


def _arrange_relock_scenario(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A staged ``.py`` commit with ``uv.lock`` left unstaged-dirty, and a stub ``uv``
    poised to re-lock it the moment the ruff block runs.

    The baseline commits ``uv.lock`` (``old``) and an already-formatted ``mod.py``. The
    pending commit stages a ``mod.py`` edit — giving the ruff block a ``.py`` file to run
    ``uv run`` against — and leaves ``uv.lock`` at ``new`` in the worktree, unstaged: the
    exact shape a version bump leaves behind once an earlier ``uv run`` re-locked it and
    only the source change got staged. Returns ``(repo, env)`` with the stub ``uv`` ahead of
    the real toolchain on ``PATH``.
    """
    repo = _repo(tmp_path / "repo")
    lock = repo / "uv.lock"
    lock.write_text("lock = old\n")
    mod = repo / "mod.py"
    mod.write_text("x = 1\n")
    _git(repo, "add", "uv.lock", "mod.py")
    _git(repo, "commit", "-q", "-m", "baseline", "--no-verify")

    mod.write_text("y = 2\n")  # staged .py edit: gives the ruff block a `uv run` to make
    _git(repo, "add", "mod.py")
    lock.write_text("lock = new\n")  # uv.lock dirty in the worktree, pointedly not staged

    env = _install_stub_uv(tmp_path / "bin", _stub_uv_relocking())
    return repo, env


def test_pre_commit_lands_the_commit_when_uv_relocks_a_dirty_lockfile(
    tmp_path: Path,
) -> None:
    """A staged commit lands even though a dirty ``uv.lock`` is re-locked mid-hook.

    The tracer for #98: with ``uv.lock`` unstaged-dirty, the peel reverts it, ``uv run``
    re-locks it behind the peel's back, and the unfixed hook's restore can no longer
    reapply its patch — so it falls into the rollback branch and aborts a commit that has
    nothing to do with the lockfile. A dirty ``uv.lock`` must not be able to veto an
    unrelated staged commit; the commit lands.
    """
    repo, env = _arrange_relock_scenario(tmp_path)

    result = _commit_under(repo, env)

    assert result.returncode == 0, (
        f"a dirty uv.lock aborted an unrelated staged commit:\n{result.stdout}\n{result.stderr}"
    )
    assert _subject(repo) == "try", "the commit did not land"


def test_pre_commit_leaves_the_dirty_lockfile_unstaged_and_uncommitted(
    tmp_path: Path,
) -> None:
    """The dirty ``uv.lock`` stays exactly where the developer left it: unstaged on disk,
    and out of the commit it was never staged into.

    The hook reformats only *staged* content, so the lockfile the developer pointedly did
    not ``git add`` must not ride along — the committed copy stays ``old`` — and the
    unstaged edit it carried must survive untouched — the worktree keeps ``new``. This is
    the "leaves the tree as it found it" half of #98: pinning ``uv`` frozen fixes the abort
    without the peel/restore quietly swallowing or committing the lockfile in the process.
    """
    repo, env = _arrange_relock_scenario(tmp_path)

    _commit_under(repo, env)

    committed_lock = _git(repo, "show", "HEAD:uv.lock").stdout
    assert committed_lock == "lock = old\n", (
        f"the unstaged uv.lock was swept into the commit: {committed_lock!r}"
    )
    assert (repo / "uv.lock").read_text() == "lock = new\n", (
        f"the developer's unstaged uv.lock edit was lost: {(repo / 'uv.lock').read_text()!r}"
    )
    assert _git(repo, "diff", "--cached", "--name-only").stdout == "", (
        "the hook left changes staged after the commit"
    )
