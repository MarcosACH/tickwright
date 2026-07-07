"""Secret redaction — no key material in any structured record (ADR-0020).

A structlog processor that scrubs two ways before rendering:

* **by registered value** — the exact secret strings loaded from config (the
  Hyperliquid signing key, notably) are replaced wherever they appear, so a
  secret accidentally interpolated into a message or field never surfaces; and
* **by field name** — any value under a sensitive key (``signing_key``,
  ``password``, …) is replaced outright, regardless of content.

Both walk nested dicts and lists. Latency is an explicit non-goal (ADR-0020), so
the processor always walks the record rather than guarding with a fast path.
"""

from collections.abc import Iterable

from structlog.typing import EventDict, WrappedLogger

_REDACTED = "[REDACTED]"

# Field names whose value is a secret no matter what it contains.
_SENSITIVE_KEYS = frozenset(
    {
        "signing_key",
        "private_key",
        "secret",
        "secret_key",
        "api_secret",
        "password",
        "token",
        "authorization",
    }
)

_secrets: frozenset[str] = frozenset()


def register_secrets(values: Iterable[str]) -> None:
    """Register the exact secret strings to scrub from every record.

    Replaces the prior registration wholesale. Empty strings are ignored — one
    would match every value and redact the whole log.
    """
    global _secrets
    _secrets = frozenset(value for value in values if value)


def redact(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor: scrub sensitive-named fields and registered secret
    substrings from ``event_dict``, recursively."""
    return {key: _scrub_field(key, value) for key, value in event_dict.items()}


def _scrub_field(key: str, value: object) -> object:
    if key in _SENSITIVE_KEYS:
        return _REDACTED
    return _scrub_value(value)


def _scrub_value(value: object) -> object:
    if isinstance(value, str):
        for secret in _secrets:
            value = value.replace(secret, _REDACTED)
        return value
    if isinstance(value, dict):
        return {key: _scrub_field(str(key), val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub_value(item) for item in value)
    return value
