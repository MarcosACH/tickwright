"""``KafkaBus`` config — each adapter's config lives in its package
(ADR-0032); only the composition root reads them all."""

from pydantic import BaseModel


class KafkaBusConfig(BaseModel):
    """Where the one keyed topic lives (ADR-0028).

    ``events_topic``, ``group_id``, and the store location are **per-process**
    configuration (ADR-0028/0031 instance isolation): two engine processes
    must never share any of them — unqualified symbols (BTC on two venues)
    would collide on a shared topic.
    """

    bootstrap_servers: str = "localhost:9092"
    events_topic: str = "tickwright.events"
    group_id: str = "tickwright"
