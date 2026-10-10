"""Small shared contracts; no scenario-dependent defaults."""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any


@dataclass
class RuntimeBudget:
    """A monotonic local cap also applies to the longer rehearsal/scouting runs."""

    limit_seconds: float = 1200
    reserve_seconds: float = 240
    expires_at_ms: float | None = None
    started: float = field(default_factory=time.monotonic)

    def remaining(self) -> float:
        local = self.limit_seconds - (time.monotonic() - self.started)
        if self.expires_at_ms is not None:
            local = min(local, self.expires_at_ms / 1000 - time.time())
        return max(0.0, local)

    def should_finalize(self) -> bool:
        return self.remaining() <= self.reserve_seconds

    def timeout(self, maximum: float = 90) -> float:
        return max(0.0, min(maximum, self.remaining()))


@dataclass
class RunOutcome:
    submitted: bool
    receipt: str | None = None
    reason: str = ""
    candidate: dict[str, Any] | None = None


SITE_ROLES = {
    "tender-portal": "tender",
    "customer-portal": "specification",
    "erp": "erp",
    "mail": "mail",
}
