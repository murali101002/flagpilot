"""Evidence for the locked status definitions.

Two halves:
- code_references(): a simple search of the demo repo for the flag name
  (practice scope: the demo app's own code only; no generic code search).
- classify(): the locked Active / Stale / Dead rules, pure and clock-free —
  the caller passes counts and booleans, never a "now".
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flagpilot.models import Flag

# Locked thresholds (Phase 6 spec).
ACTIVE_MIN_EVALUATIONS_7D = 1     # "traffic above zero"
LOW_TRAFFIC_14D_MAX = 10          # "very low traffic in last 14 days"
NO_CHANGE_DAYS = 30               # "no change in 30 days"
FULL_ROLLOUT_DEAD_DAYS = 60       # "at 100 percent for 60 days"

# Demo seed data: flags that shipped long ago and sit at 100% with no
# variation left to test. Maps flag name → (full-rollout days, variations
# remaining). This is the dead rule's second arm — the classic purge case:
# the flag is still referenced in code, but it is dead weight.
DEMO_SHIPPED_FLAGS = {
    "legacy_banner": {"full_rollout_days": 62, "variations_remaining": 0},
}


@dataclass(frozen=True)
class Classification:
    status: str
    reasons: list[str]


def code_references(feature_name: str, demo_dir: str | None = None) -> list[str]:
    """Demo-repo files whose code mentions the flag name (repo-relative paths)."""
    root = (
        demo_dir
        or os.environ.get("FLAGPILOT_DEMO_DIR")
        or str(Path(__file__).resolve().parent.parent / "demo-app")
    )
    root_path = Path(root).resolve()
    found: list[str] = []
    for path in sorted(root_path.rglob("*.py")):
        if feature_name in path.read_text(encoding="utf-8", errors="ignore"):
            found.append(str(path.relative_to(root_path.parent)))
    return found


def changed_within_days(last_change: str | None, days: int) -> bool:
    """Whether the flag changed inside the window (I/O helper; classify stays pure)."""
    if not last_change:
        return False
    try:
        changed = datetime.fromisoformat(str(last_change).replace("Z", "+00:00"))
    except ValueError:
        return False
    return changed.tzinfo is not None and changed >= datetime.now(timezone.utc) - timedelta(days=days)


def classify(
    *,
    evaluations_7: int,
    evaluations_14: int,
    evaluations_30: int,
    changed_in_last_30_days: bool,
    code_references: list[str],
    full_rollout_days: int | None = None,
    variations_remaining: int | None = None,
) -> Classification:
    """The locked Phase 6 definitions. Dead wins over active; unknown is honest."""
    referenced = len(code_references) > 0
    references_line = "referenced in " + ", ".join(code_references)

    # Dead, arm 1: at 100% for 60 days with no variation left to test.
    # (full_rollout_days arrives from rollout history — wired in Phase 8.)
    if (
        full_rollout_days is not None
        and full_rollout_days >= FULL_ROLLOUT_DEAD_DAYS
        and (variations_remaining or 0) == 0
    ):
        return Classification(
            "dead",
            [f"at 100% rollout for {full_rollout_days} days with no variation left to test"],
        )

    # Dead, arm 2: zero evaluations in 30 days AND no code reference found.
    if evaluations_30 == 0 and not referenced:
        return Classification(
            "dead", ["zero evaluations in 30 days", "no code reference found"]
        )

    # Active: evaluated in last 7 days, traffic above zero, still in code.
    if evaluations_7 >= ACTIVE_MIN_EVALUATIONS_7D and referenced:
        return Classification(
            "active",
            [f"{evaluations_7} evaluations in the last 7 days", references_line],
        )

    # Stale: referenced, and no change in 30 days or very low traffic in 14 days.
    if referenced and (not changed_in_last_30_days or evaluations_14 <= LOW_TRAFFIC_14D_MAX):
        reasons: list[str] = []
        if not changed_in_last_30_days:
            reasons.append("no change in the last 30 days")
        if evaluations_14 <= LOW_TRAFFIC_14D_MAX:
            reasons.append(f"only {evaluations_14} evaluations in the last 14 days")
        reasons.append(references_line)
        return Classification("stale", reasons)

    return Classification("unknown", ["evidence does not match any locked definition"])


def flags_with_evidence(flags: list[Flag]) -> list[dict]:
    """Flags enriched with usage evidence and the locked classification.

    The warehouse being down must not take the dashboard down: a flag whose
    evidence can't be gathered reports honestly as unknown.
    """
    from flagpilot.usage import usage_for_feature

    enriched: list[dict] = []
    for flag in flags:
        try:
            usage = usage_for_feature(flag.name)
            refs = code_references(flag.name)
            shipped = DEMO_SHIPPED_FLAGS.get(flag.name, {})
            classification = classify(
                evaluations_7=usage["evaluations"]["7"],
                evaluations_14=usage["evaluations"]["14"],
                evaluations_30=usage["evaluations"]["30"],
                changed_in_last_30_days=changed_within_days(flag.last_change, NO_CHANGE_DAYS),
                code_references=refs,
                full_rollout_days=shipped.get("full_rollout_days"),
                variations_remaining=shipped.get("variations_remaining"),
            )
            enriched.append(
                {
                    **flag.as_dict(),
                    "last_evaluated": usage["last_evaluated"],
                    "total_evaluations": usage["total_evaluations"],
                    "code_references": refs,
                    "status": classification.status,
                    "status_reasons": classification.reasons,
                }
            )
        except Exception as error:
            enriched.append(
                {
                    **flag.as_dict(),
                    "code_references": [],
                    "status": "unknown",
                    "status_reasons": [f"evidence unavailable: {error}"],
                }
            )
    return enriched
