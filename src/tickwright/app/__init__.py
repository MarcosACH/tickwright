"""The application layer (ADR-0032): the composition root and CLI entry.

The one package at the top of the graph that may know every concrete —
``AppConfig`` composes the typed per-adapter configs, ``build_engine`` selects
impls with explicit ``match``es over the config discriminants.

``AppConfig`` is exported because it is the pure type ``build_engine`` takes.
Its env-reading sibling ``AppSettings`` is deliberately absent from ``__all__``:
exporting it would advertise a second place ambient config may be read, and
``__main__`` is the only legitimate one (issue #71).
"""

from .build import build_engine
from .config import AppConfig

__all__ = ["AppConfig", "build_engine"]
