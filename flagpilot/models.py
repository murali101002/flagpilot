"""FlagPilot's flag model.

The one shape every part of FlagPilot (dashboard, CLI, webhook, seed
script) thinks in. Fields follow the Phase 5 spec: name, enabled, value,
last change, environment, status.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Flag:
    """One feature flag as FlagPilot sees it in one environment."""

    name: str
    enabled: bool
    # The environment's served value: "on"/"off" for booleans, a JSON string
    # for remote config, the variation key for multivariate serving.
    value: str | None
    # When the flag last changed in this environment (ISO string, or None
    # when the Admin API exposes no timestamp).
    last_change: str | None
    environment: str
    # Placeholder until Phase 6 classifies it as active / stale / dead.
    status: str = "unknown"

    def as_dict(self) -> dict:
        return asdict(self)
