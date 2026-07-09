"""One bus per backend, for suites shared across both (ADR-0023/0028 parity).

The parity promise is that swapping the backend changes durability, never
behavior, so shared suites take a ``backend`` parameter and drive the same
scenario through whichever bus it names. Kafka runs over the fake broker
(ADR-0022); the in-memory bus is the hermetic real thing.
"""

from kafka_fakes import FakeKafkaBroker

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.bus.kafka import KafkaBus
from tickwright.domain import EventBus

BUS_BACKENDS = ["in_memory", "kafka"]


def make_bus(backend: str) -> EventBus:
    """A fresh bus of the named backend (kafka: over its own fresh fake broker)."""
    match backend:
        case "in_memory":
            return InMemoryBus()
        case "kafka":
            broker = FakeKafkaBroker()
            return KafkaBus(
                bootstrap_servers="kafka:9092",
                topic="tickwright.events",
                group_id="tickwright",
                producer_factory=broker.producer,
                consumer_factory=broker.consumer,
            )
        case unknown:
            raise ValueError(f"unknown bus backend {unknown!r}")
