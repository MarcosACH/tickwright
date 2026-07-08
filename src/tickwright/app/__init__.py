"""The application layer (ADR-0032): the composition root and CLI entry.

The one package at the top of the graph that may know every concrete —
``AppConfig`` reads the typed per-adapter configs, ``build_engine`` selects
impls with explicit ``match``es over the config discriminants.
"""

from .build import build_engine
from .config import AppConfig

__all__ = ["AppConfig", "build_engine"]
