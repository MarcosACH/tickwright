"""Structured logging setup: correlation on every line, secrets never on any (#18).

``configure_logging`` wires the real processor chain to a stream. Two guarantees
tested here against the *rendered* output (what an aggregator actually sees):
the run id rides every line, and a signing key placed in config is redacted from
every line — by its registered value and by any sensitive field name (ADR-0020).
"""

import io
import json

from tickwright.observability import NamedEvent, named_event
from tickwright.observability.correlation import bind_run_id, operation
from tickwright.observability.logging import configure_logging


def _lines(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def test_every_rendered_line_carries_the_run_id() -> None:
    buffer = io.StringIO()
    configure_logging(stream=buffer, json_output=True)
    bind_run_id("run-1")

    named_event(NamedEvent.ORDER_PLACED)

    (line,) = _lines(buffer)
    assert line["event"] == "order.placed"
    assert line["run_id"] == "run-1"


def test_saga_and_signal_ids_ride_their_respective_lines() -> None:
    buffer = io.StringIO()
    configure_logging(stream=buffer, json_output=True)

    with operation(signal_id="alpha:BTC:1"):
        named_event(NamedEvent.ORDER_PLACED)
    with operation(cloid="0xabc"):
        named_event(NamedEvent.ORDER_FILLED)

    placed, filled = _lines(buffer)
    assert placed["signal_id"] == "alpha:BTC:1"
    assert "cloid" not in placed
    assert filled["cloid"] == "0xabc"


def test_a_registered_secret_value_never_appears_in_a_rendered_line() -> None:
    buffer = io.StringIO()
    key = "0xdeadbeefcafe_fake_signing_key"
    configure_logging(stream=buffer, json_output=True, secrets=[key])

    named_event(NamedEvent.ORDER_PLACED, note=f"loaded key {key} from env")

    rendered = buffer.getvalue()
    assert key not in rendered
    assert "[REDACTED]" in rendered


def test_a_secret_nested_in_a_dict_or_list_field_is_scrubbed_recursively() -> None:
    buffer = io.StringIO()
    key = "0xfeedface_fake_signing_key"
    configure_logging(stream=buffer, json_output=True, secrets=[key])

    named_event(
        NamedEvent.ORDER_PLACED,
        context={"env": {"HYPERLIQUID_SIGNING_KEY": key}},
        notes=[f"key={key}"],
        attempt=1,  # a non-string scalar rides through untouched
    )

    line = json.loads(buffer.getvalue())
    rendered = buffer.getvalue()
    assert key not in rendered
    assert rendered.count("[REDACTED]") == 2
    assert line["attempt"] == 1


def test_a_sensitive_field_name_is_redacted_by_name() -> None:
    buffer = io.StringIO()
    configure_logging(stream=buffer, json_output=True)

    named_event(NamedEvent.ORDER_PLACED, signing_key="whatever-was-put-here")

    (line,) = _lines(buffer)
    assert line["signing_key"] == "[REDACTED]"
    assert "whatever-was-put-here" not in buffer.getvalue()
