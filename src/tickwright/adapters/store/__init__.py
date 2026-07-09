"""Store adapters. ``SQLiteStore`` is the zero-setup default (ADR-0019);
``PostgresStore`` is the production-parity backend paired with ``KafkaBus``.

``PostgresStore`` is deliberately *not* re-exported here: selecting the
hermetic default must not load the psycopg driver, so it loads only when the
composition root selects ``store=postgres`` — import it from
``tickwright.adapters.store.postgres``. Both configs are plain pydantic (no
driver import) and safe to export for the config surface.
"""

from .config import PostgresStoreConfig, SQLiteStoreConfig
from .sqlite import SQLiteStore

__all__ = ["PostgresStoreConfig", "SQLiteStore", "SQLiteStoreConfig"]
