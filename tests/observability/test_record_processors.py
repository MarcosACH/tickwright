"""The record-shaping segment is one source of truth (#18, ADR-0020).

``configure_logging`` (production) and ``capture_events`` (the test seam) must
apply the *same* content-shaping processors — correlation merge, then redaction —
or a test would observe a record production no longer emits. These pin that
shared segment so a new content rule cannot land in one path and drift from the
other: the drift's worst case is a secret that a green test never sees leaking
into a real line.
"""

from tickwright.observability import NamedEvent, named_event
from tickwright.observability.logging import RECORD_PROCESSORS, build_processors
from tickwright.observability.redaction import register_secrets
from tickwright.observability.testing import capture_events


def test_the_production_chain_begins_with_the_shared_record_processors() -> None:
    # capture_events runs exactly RECORD_PROCESSORS; proving production's chain
    # opens with the same segment is what makes them one source of truth.
    chain = build_processors(json_output=True)
    assert chain[: len(RECORD_PROCESSORS)] == list(RECORD_PROCESSORS)


def test_capture_events_redacts_a_registered_secret_like_production_does() -> None:
    # The test seam mirrors production redaction: a registered secret is scrubbed
    # from a captured record, so a redaction assertion made through capture_events
    # means the same thing it does against a rendered line.
    key = "0xc0ffee_fake_signing_key"
    register_secrets([key])
    with capture_events() as events:
        named_event(NamedEvent.ORDER_PLACED, note=f"loaded {key}")

    assert key not in events[0]["note"]
    assert "[REDACTED]" in events[0]["note"]
