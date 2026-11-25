"""The auto-graduation decision: pure logic, no network, no file, no clock.

`decide()` is the brain. Phase 8's worker feeds it health numbers (from
Prometheus) and evaluation counts every 30 seconds and acts on the answer.
Everything it needs to know arrives as arguments, which is what keeps it
unit-testable and honest.

Locked rules (Phase 7):
- Steps: 5, 25, 50, 75, 100.
- Advance only if evaluations >= 100, error rate < 1%, p95 no more than
  20% above baseline, no 5xx spike (the error rate input IS the 5xx rate
  from the Phase 4 Prometheus query, so a spike shows up there).
- Hold if evaluations < 100, or the step's dwell time has not passed.
- Rollback to the last healthy step — never below the first step, which is
  what "not to 0" means — when error rate >= 1% or p95 regresses; auto
  graduation pauses after a rollback (decision.pause = True) until a human
  restarts it.
"""

from __future__ import annotations

from dataclasses import dataclass

STEPS = [5, 25, 50, 75, 100]

ERROR_RATE_LIMIT = 0.01        # rollback threshold: 1% of requests failing
P95_TOLERANCE = 0.20           # p95 may sit at most 20% above baseline
ADVANCE_MIN_EVALUATIONS = 100
DEFAULT_DWELL_SECONDS = 1800.0 # a step must hold 30 minutes before advancing


@dataclass(frozen=True)
class GraduationDecision:
    decision: str      # "advance" | "hold" | "rollback"
    current_step: int
    target_step: int   # the step to move to (== current for hold/rollback at the first step)
    reason: str        # the exact, loggable reason
    pause: bool        # True after a rollback: auto graduation stops until restarted


def _previous_step(current_step: int) -> int:
    index = STEPS.index(current_step)
    return STEPS[max(0, index - 1)]


def _next_step(current_step: int) -> int | None:
    index = STEPS.index(current_step)
    return STEPS[index + 1] if index + 1 < len(STEPS) else None


def decide(
    current_step: int,
    error_rate: float,
    p95_ms: float,
    baseline_p95_ms: float,
    evaluations: int,
    *,
    dwell_seconds_elapsed: float | None = None,
    dwell_seconds_required: float = DEFAULT_DWELL_SECONDS,
) -> GraduationDecision:
    """One graduation decision, with the exact reason for the log.

    `dwell_seconds_elapsed = None` means the caller does not track dwell
    yet (Phase 8's worker will) — health is then the only gate.
    """
    if current_step not in STEPS:
        raise ValueError(f"current_step must be one of {STEPS}, got {current_step}")

    p95_ceiling = baseline_p95_ms * (1 + P95_TOLERANCE)
    error_spike = error_rate >= ERROR_RATE_LIMIT
    p95_regression = p95_ms > p95_ceiling

    # Safety first: a rollback decision, to the last healthy step.
    if error_spike or p95_regression:
        why: list[str] = []
        if error_spike:
            why.append(f"error rate {error_rate:.2%} is at or above the {ERROR_RATE_LIMIT:.0%} limit")
        if p95_regression:
            why.append(f"p95 {p95_ms:.0f}ms exceeds the {p95_ceiling:.0f}ms ceiling (baseline {baseline_p95_ms:.0f}ms + 20%)")
        return GraduationDecision(
            decision="rollback",
            current_step=current_step,
            target_step=_previous_step(current_step),
            reason="rollback: " + "; ".join(why),
            pause=True,
        )

    # Advance gate: every condition must hold, in the locked order.
    if evaluations < ADVANCE_MIN_EVALUATIONS:
        return GraduationDecision(
            decision="hold",
            current_step=current_step,
            target_step=current_step,
            reason=f"hold: {evaluations} evaluations is below the {ADVANCE_MIN_EVALUATIONS} minimum",
            pause=False,
        )

    if dwell_seconds_elapsed is not None and dwell_seconds_elapsed < dwell_seconds_required:
        return GraduationDecision(
            decision="hold",
            current_step=current_step,
            target_step=current_step,
            reason=(
                f"hold: dwell {dwell_seconds_elapsed:.0f}s at step {current_step} is shorter "
                f"than the required {dwell_seconds_required:.0f}s"
            ),
            pause=False,
        )

    next_step = _next_step(current_step)
    if next_step is None:
        return GraduationDecision(
            decision="hold",
            current_step=current_step,
            target_step=current_step,
            reason="hold: already at 100%, nothing left to advance to",
            pause=False,
        )

    return GraduationDecision(
        decision="advance",
        current_step=current_step,
        target_step=next_step,
        reason=(
            f"advance: health is clean over {evaluations} evaluations "
            f"(error rate {error_rate:.2%}, p95 {p95_ms:.0f}ms within 20% of baseline {baseline_p95_ms:.0f}ms)"
        ),
        pause=False,
    )


def instant_preflight(
    error_rate: float,
    p95_ms: float,
    baseline_p95_ms: float,
) -> GraduationDecision:
    """The gate before an instant 0 -> 100 jump.

    Same health bars as a step advance. "advance" means cleared for the
    jump (the caller still requires an explicit human confirm, per the
    spec); anything else is a refusal with the exact reason.
    """
    p95_ceiling = baseline_p95_ms * (1 + P95_TOLERANCE)
    if error_rate >= ERROR_RATE_LIMIT or p95_ms > p95_ceiling:
        return GraduationDecision(
            decision="hold",
            current_step=5,
            target_step=5,
            reason=(
                f"instant refused: error rate {error_rate:.2%}, p95 {p95_ms:.0f}ms "
                f"against ceiling {p95_ceiling:.0f}ms"
            ),
            pause=False,
        )
    return GraduationDecision(
        decision="advance",
        current_step=5,
        target_step=100,
        reason=(
            f"instant cleared: error rate {error_rate:.2%}, p95 {p95_ms:.0f}ms within tolerance "
            "(explicit confirm required; a 10-minute watch window follows the jump)"
        ),
        pause=False,
    )
