"""The graduation worker: polls health + experiment, decides, applies.

Every 30 seconds, for each env-configured graduating flag:
- read the current rollout step from the running experiment (Admin API)
- read error rate / p95 / baseline from Prometheus (health.py)
- read win probability from the experiment's stored results
- call the Phase 7 decide() and act:
  - advance / rollback → apply the new rollout via the Admin API and log
    the decision to the store (flag, old step, new step, reason, timestamp)
  - a rollback also pauses that flag in memory until the app restarts
- advance requires BOTH signals when an experiment is running: win
  probability above 90%, plus clean health (a missing or low win
  probability turns an advance into a hold)
- sample FlagPilot's native metrics into the store periodically

Everything env-configured: which flags graduate (FLAGPILOT_GRADUATE_FLAGS),
how long a step must dwell (FLAGPILOT_DWELL_SECONDS, default 30 minutes —
set it low locally to watch the ladder climb), and the Prometheus URL.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from datetime import datetime, timezone

from flagpilot import store
from flagpilot.evidence import flags_with_evidence
from flagpilot.flagsmith_client import (
    apply_rollout,
    experiment_results,
    find_feature,
    find_running_experiment,
    list_flags,
)
from flagpilot.graduation import STEPS, decide
from flagpilot.health import health_snapshot
from flagpilot.usage import usage_for_feature

WORKER_INTERVAL_SECONDS = 30
WIN_PROBABILITY_GATE = 0.90
NATIVE_METRICS_EVERY_TICKS = 10


def _graduating_flags() -> list[str]:
    raw = os.environ.get("FLAGPILOT_GRADUATE_FLAGS", "")
    return [name.strip() for name in raw.split(",") if name.strip()]


def _dwell_seconds() -> float:
    return float(os.environ.get("FLAGPILOT_DWELL_SECONDS", "1800"))


def _client_env_key() -> str:
    return os.environ.get("FLAGSMITH_CLIENT_ENVIRONMENT_KEY", "")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paused_flags() -> set[str]:
    """Flags paused by a rollback. In-memory: a restart re-arms them."""
    global _paused
    return _paused


_paused: set[str] = set()
_tick_count = 0
_last_graduation_at: dict[str, str] = {}


def _dwell_seconds_elapsed(flag: str, current_step: int) -> float | None:
    """Seconds since the flag was moved ONTO the current step, from history."""
    for row in store.graduation_history(flag, limit=50):
        if row["new_step"] == current_step:
            moved_at = datetime.fromisoformat(row["created_at"])
            return (datetime.now(timezone.utc) - moved_at).total_seconds()
    return None


def _win_probability(experiment_id: int) -> float | None:
    """The best treatment's chance_to_win vs control, or None if unknown."""
    try:
        payload = experiment_results(experiment_id).get("results", {}).get("payload", {})
        inference = payload.get("metrics", [{}])[0].get("inference") or {}
        chances = [
            entry.get("chance_to_win")
            for key, entry in inference.items()
            if key != "control" and entry and entry.get("chance_to_win") is not None
        ]
        return max(chances) if chances else None
    except Exception:
        return None


def _record_native_metrics() -> None:
    """Sample FlagPilot's own numbers into the store (best effort)."""
    try:
        flags = flags_with_evidence(list_flags())
        dead = sum(1 for f in flags if f["status"] == "dead")
        store.record_metric("dead_flag_count", float(dead))
        stale_last_seen = [
            f["last_evaluated"] for f in flags if f["status"] == "stale" and f.get("last_evaluated")
        ]
        if stale_last_seen:
            oldest = min(stale_last_seen)
            age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(oldest)).days
            store.record_metric("stale_flag_age_days", float(age_days))
        rollbacks = sum(
            1 for row in store.graduation_history(limit=200) if row["decision"] == "rollback"
        )
        store.record_metric("rollback_count", float(rollbacks))
        store.record_metric("purge_pr_merge_rate", 0.0, "no purges yet (Phase 9)")
    except Exception as error:
        print(f"native metrics sampling failed: {error}", flush=True)


def graduation_tick() -> list[dict]:
    """One worker pass. Returns the applied decisions (for the log/tests)."""
    global _tick_count
    _tick_count += 1

    # Dogfood gate (Phase 10): FlagPilot steers itself by its own flags.
    # The flag is re-read every tick, so toggling it in Flagsmith takes
    # effect within 30 seconds, with no redeploy.
    auto_grad = find_feature("flagpilot_auto_grad")
    if auto_grad is not None and not (auto_grad.get("environment_feature_state") or {}).get("enabled"):
        print("worker: paused — flagpilot_auto_grad is off in Flagsmith", flush=True)
        return []
    if auto_grad is None:
        print("worker: flagpilot_auto_grad does not exist yet; worker idle", flush=True)
        return []

    actions: list[dict] = []

    for flag_name in _graduating_flags():
        if flag_name in _paused:
            continue
        try:
            experiment = find_running_experiment(flag_name)
        except Exception as error:
            print(f"worker: experiment lookup failed for {flag_name}: {error}", flush=True)
            continue
        if experiment is None:
            continue

        rollout = experiment.get("experiment_rollout") or {}
        try:
            current_step = int(rollout.get("rollout_percentage", 0))
        except (TypeError, ValueError):
            continue
        if current_step not in STEPS:
            print(
                f"worker: {flag_name} rollout is {current_step}%, not a graduation step; skipping",
                flush=True,
            )
            continue

        health = health_snapshot(flag_name)
        if (
            health["error_rate"] is None
            or health["p95_ms"] is None
            or health["baseline_p95_ms"] is None
        ):
            print(f"worker: no health evidence for {flag_name}; holding silently", flush=True)
            continue

        evaluations = usage_for_feature(flag_name)["total_evaluations"]
        decision = decide(
            current_step,
            health["error_rate"],
            health["p95_ms"],
            health["baseline_p95_ms"],
            evaluations,
            dwell_seconds_elapsed=_dwell_seconds_elapsed(flag_name, current_step),
            dwell_seconds_required=_dwell_seconds(),
        )

        win_probability = _win_probability(experiment["id"])
        if decision.decision == "advance" and (win_probability is None or win_probability < WIN_PROBABILITY_GATE):
            shown = "unknown" if win_probability is None else f"{win_probability:.0%}"
            decision = dataclasses.replace(
                decision,
                decision="hold",
                reason=decision.reason + f"; win probability {shown} is below the 90% gate",
            )

        print(f"worker: {flag_name} at {current_step}% → {decision.decision}: {decision.reason}", flush=True)

        if decision.decision in ("advance", "rollback"):
            apply_rollout(experiment, decision.target_step)
            store.log_graduation(
                flag_name,
                current_step,
                decision.target_step,
                decision.decision,
                decision.reason,
                paused=decision.pause,
            )
            now_iso = _now_iso()
            if decision.decision == "advance":
                previous = _last_graduation_at.get(flag_name)
                if previous:
                    seconds = (
                        datetime.fromisoformat(now_iso) - datetime.fromisoformat(previous)
                    ).total_seconds()
                    store.record_metric("time_to_graduate", seconds, f"{current_step}% → {decision.target_step}%")
                _last_graduation_at[flag_name] = now_iso
            if decision.pause:
                _paused.add(flag_name)
            actions.append(dataclasses.asdict(decision) | {"flag": flag_name})
            _record_native_metrics()

    if _tick_count % NATIVE_METRICS_EVERY_TICKS == 0:
        _record_native_metrics()
    return actions


async def _worker_loop() -> None:
    while True:
        try:
            graduation_tick()
        except Exception as error:
            print(f"worker tick failed: {error}", flush=True)
        await asyncio.sleep(int(os.environ.get("FLAGPILOT_WORKER_INTERVAL_SECONDS", "30")))


def start_worker_loop() -> asyncio.Task | None:
    """Start the background loop. Returns None (and does nothing) when no
    flags are configured to graduate."""
    if not _graduating_flags():
        print("worker: FLAGPILOT_GRADUATE_FLAGS is empty; worker idle", flush=True)
        return None
    return asyncio.create_task(_worker_loop())
