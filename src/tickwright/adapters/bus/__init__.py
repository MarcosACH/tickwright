"""EventBus adapters. ``InMemoryBus`` is the hermetic default (ADR-0023);
``KafkaBus`` is the durable backend (ADR-0028).

``KafkaBus`` is deliberately *not* re-exported here: the in-memory hot path
must import no serde or Kafka module (events pass by reference, ADR-0025), so
the wire stack loads only when the composition root selects ``bus=kafka`` —
import it from ``tickwright.adapters.bus.kafka``. ``KafkaBusConfig`` is plain
pydantic (no Kafka import) and safe to export for the config surface.
"""

from .config import KafkaBusConfig
from .inmemory import InMemoryBus

__all__ = ["InMemoryBus", "KafkaBusConfig"]
