"""FlagPilot dashboard app.

Minimal for now: health check + serves static/index.html.
Flags data, lifecycle logic, and Flagsmith API calls arrive in later files.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from flagpilot.evidence import flags_with_evidence
from flagpilot.flagsmith_client import list_flags
from flagpilot import purge
from flagpilot import promotion
from flagpilot import store
from flagpilot import worker
from flagpilot import webhooks

app = FastAPI(title="FlagPilot")

_worker_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _worker_task
    _worker_task = worker.start_worker_loop()
    yield
    if _worker_task is not None:
        _worker_task.cancel()


app = FastAPI(title="FlagPilot", lifespan=lifespan)
webhooks.register(app)

STATIC_DIR = Path(__file__).parent / "static"

_latest_health_report: dict[str, object] | None = None


class HealthReport(BaseModel):
    """The demo app's direct fallback report, sent every 30 seconds."""

    flag: str
    total_count: int
    error_count: int
    p95_ms: float


@app.post("/health-report")
def receive_health_report(report: HealthReport) -> dict[str, object]:
    global _latest_health_report
    _latest_health_report = report.model_dump()
    return {"received": True}


@app.get("/health-report")
def read_health_report() -> dict[str, object]:
    """Read back the latest report, for verification and the dashboard later."""
    return _latest_health_report or {}

PLACEHOLDER = """<!doctype html>
<html>
  <head><title>FlagPilot</title></head>
  <body>
    <h1>FlagPilot</h1>
    <p>Dashboard coming soon — flagpilot/static/index.html not created yet.</p>
  </body>
</html>"""


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/flags")
def flags() -> list[dict]:
    """Every flag in the environment, with usage evidence and status."""
    return flags_with_evidence(list_flags())


@app.get("/graduation-history")
def graduation_history() -> list[dict]:
    """The worker's applied decisions, newest first."""
    return store.graduation_history(limit=20)


@app.get("/graduation-metrics")
def graduation_metrics() -> dict:
    """FlagPilot's native metrics, latest sample per metric."""
    return store.latest_metrics()


@app.get("/experiment-summary")
def experiment_summary() -> dict:
    """The hero experiment at a glance: status, rollout step, win
    probability, conversion per variant, and latency per variant."""
    from flagpilot.flagsmith_client import find_running_experiment, experiment_results
    from flagpilot.health import health_snapshot, p95_ms

    experiment = find_running_experiment("checkout_v2")
    if experiment is None:
        return {"running": False}
    rollout = experiment.get("experiment_rollout") or {}
    results_payload = experiment_results(experiment["id"]).get("results", {}).get("payload", {})
    metrics = (results_payload.get("metrics") or [{}])[0]
    inference = metrics.get("inference") or {}
    variants = metrics.get("variants") or {}
    conversion = {
        name: (variant["sum"] / variant["n"]) if variant.get("n") else None
        for name, variant in variants.items()
    }
    win_probability = None
    for name, entry in inference.items():
        if name != "control" and entry and entry.get("chance_to_win") is not None:
            win_probability = max(win_probability or 0.0, entry["chance_to_win"])
    return {
        "running": experiment.get("status") == "running",
        "name": experiment.get("name"),
        "rollout_step": rollout.get("rollout_percentage"),
        "win_probability": win_probability,
        "conversion": conversion,
        "p95_by_variation": {
            variation: p95_ms("checkout_v2", variation)
            for variation in ("control", "new_cached", "new_prefetch")
        },
        "health": health_snapshot("checkout_v2"),
    }


@app.get("/purge/{flag}/preview")
def purge_preview(flag: str) -> dict:
    """What the purge would do: files, removed lines, diff, usage. Writes nothing."""
    return purge.preview(flag)


@app.post("/purge/{flag}")
def purge_execute(flag: str) -> dict:
    """Snapshot → branch → removal commits → PR → pending removal."""
    return purge.execute(flag)


@app.get("/pending-removals")
def pending_removals() -> list[dict]:
    """Flags awaiting their purge PR merge."""
    return store.pending_removals()


@app.get("/promotion/{flag}")
def promotion_state(flag: str) -> dict:
    """The flag's state across dev → staging → prod, with the chain."""
    return promotion.promotion_state(flag)


@app.post("/promotion/{flag}/promote")
def promote_flag(flag: str, request: dict) -> dict:
    """Apply dev → staging → staging → prod. Requires approved: true."""
    return promotion.promote(
        flag,
        request.get("from", "Development"),
        request.get("to", "Staging"),
        bool(request.get("approved")),
    )


@app.get("/promotion-log")
def promotion_log() -> list[dict]:
    """Recent applied promotions."""
    return store.promotion_history()


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    index = STATIC_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse(PLACEHOLDER)
