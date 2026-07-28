"""The completeness gate for a seam whose members are claimed suite by suite.

``Store``'s gate (``tests/adapters/store/test_store_contract.py``) drives every
member from one map because its two backends promise *identical behaviour* and
the claim is universal — ``isinstance`` cannot express "either reaches durable
storage or raises", so the coverage has to be a list, and the list needs a
guard. ``Exchange`` promises no such thing: paper simulates fills, live posts to
a venue, and each adapter states the seam in its own idiom in its own suite.

What survives from ``Store``'s gate is the half that was never about durability:
a member added to the Protocol must not be able to arrive with **no claim
behind it**. ``isinstance(adapter, Exchange)`` catches the other direction — an
*adapter* left behind by a new member — but every adapter must implement a new
member for the engine to run at all, so it passes while nothing asserts what the
member does. So each adapter suite declares which of its tests claims each
member, and this asserts the declaration covers the Protocol exactly and names
only tests that exist.

Explicit assertion messages throughout: this module is not a test module, so
pytest does not rewrite its asserts and a bare comparison would fail blind.
"""

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import get_protocol_members


def assert_every_member_is_claimed(
    protocol: type, claims: Mapping[str, str], *, suite: Path
) -> None:
    """Assert ``claims`` names an existing test in ``suite`` for every ``protocol`` member.

    ``claims`` maps a Protocol member to the test that asserts what that member
    does for this adapter. Both directions fail loudly: an unclaimed member (the
    stale-list failure the guard exists for) and a claim naming a test that was
    renamed or deleted out from under it.
    """
    members = get_protocol_members(protocol)
    unclaimed = members - set(claims)
    assert not unclaimed, (
        f"{protocol.__name__} members with no claim in {suite}: {sorted(unclaimed)} — "
        f"add the test that asserts what each does here, then name it in the claims map"
    )
    stale = set(claims) - members
    assert not stale, (
        f"claims in {suite} for members {protocol.__name__} no longer has: {sorted(stale)}"
    )

    declared = _test_names(suite)
    dangling = {member: test for member, test in claims.items() if test not in declared}
    assert not dangling, (
        f"claims in {suite} naming tests that do not exist there: {sorted(dangling.items())}"
    )


def _test_names(suite: Path) -> set[str]:
    """Every module-level ``test_*`` function defined in ``suite``'s test modules.

    Read from the source rather than imported: these modules are pytest's to
    import, and importing them again under a second name would run their
    collection-time work twice. A whole directory, not one module — a claim may
    legitimately live in the sibling module that owns that member's subject
    (Hyperliquid's ``account_spec`` is asserted in ``test_account.py``).
    """
    names: set[str] = set()
    for module in sorted(suite.glob("test_*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        names.update(
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name.startswith("test_")
        )
    return names
