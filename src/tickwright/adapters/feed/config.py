"""``ReplayFeed`` config — each adapter's config lives in its package
(ADR-0032); only the composition root reads them all."""

from pathlib import Path

from pydantic import BaseModel


class ReplayFeedConfig(BaseModel):
    """The JSONL tick file the deterministic replay drives from (ADR-0027)."""

    path: Path
