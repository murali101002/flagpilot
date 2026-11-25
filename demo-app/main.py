"""Demo shop: a tiny checkout that runs on the checkout_v2 experiment.

The three variations simulate real checkout paths so the experiment has
something true to measure: old is slow, new_cached is fast, new_prefetch is
fast but fails sometimes. Outcomes are persistent per user — shoppers who
abandon a slow checkout or hit a broken one are gone for good, which is
what produces the conversion gap between variations. Restarting this app
resets that memory (a fresh population).
"""

import asyncio
import os
import random
import threading
import time
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from flagsmith import Flagsmith
from flagsmith.analytics import EventProcessorConfig
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter as PromCounter,
    Histogram as PromHistogram,
    generate_latest,
)

# Simulated checkout paths, per variation key. The keys must match the
# variation keys configured in Flagsmith for checkout_v2.
SLOW_PATH_SECONDS = 4.8
FAST_PATH_SECONDS = 0.24
PREFETCH_FAILURE_RATE = float(os.environ.get("DEMO_PREFETCH_FAILURE_RATE", "0.10"))
OLD_BOUNCE_RATE = 0.15 # shoppers who abandon the slow checkout

# The experiment flag this demo shop runs on.
FEATURE_NAME = "checkout_v2"

# Demo seed data: the rollout percentage each serving arm was allocated in
# the Flagsmith experiment. "control" (the flag's own value) and "old" (the
# default-value path for identities outside the rollout) both serve the
# legacy checkout experience.
VARIATION_ROLLOUT_PCT = {"control": 33, "old": 33, "new_cached": 33, "new_prefetch": 34}

# Fallback health path: if the OTEL collector is down, this in-memory window
# is what the app reports to FlagPilot directly.
_health_window: deque = deque(maxlen=2000)
_lock = threading.Lock()
REPORT_INTERVAL_SECONDS = 30
HEALTH_REPORT_URL = os.environ.get(
    "FLAGPILOT_HEALTH_REPORT_URL", "http://localhost:8003/health-report"
)

# Users who will never convert again (abandoned the slow path, or hit the
# broken one). In-memory on purpose: a restart is a fresh population.
_churned: set[str] = set()
_reporter_task: asyncio.Task | None = None

flagsmith = Flagsmith(
    environment_key=os.environ["FLAGSMITH_ENVIRONMENT_KEY"],
    api_url=os.environ["FLAGSMITH_API_URL"],
    enable_events=True,
    # The SDK's default events endpoint is Flagsmith Cloud; a self-hosted
    # instance receives events itself, at <API root>/v1/events.
    event_processor_config=EventProcessorConfig(
        events_api_url=os.environ["FLAGSMITH_EVENTS_API_URL"],
    ),
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Hold the reference: the event loop only weakly references tasks, so an
    # unsaved task can be garbage-collected mid-sleep.
    global _reporter_task
    _reporter_task = asyncio.create_task(_health_reporter())
    yield
    _reporter_task.cancel()
    # Buffered events (exposures, conversions) must leave the process,
    # or the experiment page never sees them.
    flagsmith.flush_events()


app = FastAPI(title="FlagPilot demo shop", lifespan=lifespan)

# OpenTelemetry: traces and metrics flow to the local collector on 4318,
# which re-exposes them for Prometheus. Endpoint comes from env, per the
# config-only rule.
_otel_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
_resource = Resource.create({"service.name": "demo-shop"})

_tracer_provider = TracerProvider(resource=_resource)
_tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{_otel_endpoint}/v1/traces"))
)
trace.set_tracer_provider(_tracer_provider)

metrics.set_meter_provider(
    MeterProvider(
        resource=_resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{_otel_endpoint}/v1/metrics"),
                export_interval_millis=15000, # snappy for the local practice loop
            )
        ],
    )
)
_meter = metrics.get_meter("demo-shop")
# Named so the Prometheus exporter emits http_server_requests_total —
# the counter FlagPilot's health queries read.
_requests_counter = _meter.create_counter("http_server_requests")
_duration_histogram = _meter.create_histogram("http_server_request_duration_seconds")

# prometheus_client mirrors for the fallback /metrics endpoint (used when
# the collector is down).
_fallback_requests = PromCounter(
    "checkout_requests", "Demo checkout requests", ["flag", "variation", "rollout_pct", "status"]
)
_fallback_duration = PromHistogram(
    "checkout_duration_ms", "Demo checkout latency", ["flag", "variation", "rollout_pct"]
)

FastAPIInstrumentor.instrument_app(app)


class CheckoutRequest(BaseModel):
    user_id: str
    amount: float


def run_variation(variation: str, identifier: str) -> tuple[bool, bool, int]:
    """Simulate one checkout path. Returns (request_ok, converted, latency_ms)."""
    started = time.perf_counter()

    def latency_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    if variation in ("old", "control"):
        # control (the flag's own value) serves the legacy experience.
        time.sleep(SLOW_PATH_SECONDS)
        # The request finishes, but the shopper left during the wait: no
        # conversion, and not a failure — the server did its job.
        if random.random() < OLD_BOUNCE_RATE:
            _churned.add(identifier)
            return True, False, latency_ms()
        return True, True, latency_ms()
    if variation == "new_cached":
        time.sleep(FAST_PATH_SECONDS)
        return True, True, latency_ms()
    if variation == "new_prefetch":
        time.sleep(FAST_PATH_SECONDS)
        if random.random() < PREFETCH_FAILURE_RATE:
            # A broken checkout loses the request AND the customer.
            _churned.add(identifier)
            return False, False, latency_ms()
        return True, True, latency_ms()
    # Unknown variation: treat like the safe slow path.
    time.sleep(SLOW_PATH_SECONDS)
    return True, True, latency_ms()


def _health_summary() -> dict[str, object]:
    """Snapshot of the recent window: error count, total count, p95 ms."""
    with _lock:
        window = list(_health_window)
    total = len(window)
    errors = sum(1 for item in window if not item["ok"])
    latencies = sorted(item["latency_ms"] for item in window)
    if latencies:
        index = max(0, -(-len(latencies) * 95 // 100) - 1)
        p95_ms = latencies[index]
    else:
        p95_ms = 0
    return {
        "flag": FEATURE_NAME,
        "total_count": total,
        "error_count": errors,
        "p95_ms": p95_ms,
    }


async def _health_reporter() -> None:
    """POST the health summary to FlagPilot every 30 seconds.

    The direct report is the fallback path: if the OTEL collector is down,
    FlagPilot can still decide from this. FlagPilot being down is fine —
    the next report is simply another attempt.
    """
    import httpx

    while True:
        await asyncio.sleep(REPORT_INTERVAL_SECONDS)
        summary = _health_summary()
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.post(HEALTH_REPORT_URL, json=summary)
            print(f"health report posted: {response.status_code} {summary}", flush=True)
        except Exception as error:
            print(f"health report failed (FlagPilot down?): {error}", flush=True)


@app.get("/metrics")
def metrics_endpoint() -> Response:
    """Native Prometheus exposition, the second half of the fallback path."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/checkout")
def checkout(request: CheckoutRequest) -> dict[str, object]:
    # 1. One identifier for the flag call AND every event, so exposures and
    #    conversions join on the same identity.
    identifier = request.user_id

    # 2. The experiment call: assigns the variation, records the exposure,
    #    returns it. Not the plain flag call.
    flag = flagsmith.get_experiment_flag("checkout_v2", identifier)
    variation = str(flag.value) if flag.value else "old"

    # 3. The user experiences the checkout here. A churned user is gone:
    #    the visit ends instantly, with no events and no conversion.
    if identifier in _churned:
        return {
            "variation": variation,
            "latency_ms": 0,
            "success": True,
            "converted": False,
        }

    span = trace.get_current_span()
    span.set_attribute("flag", FEATURE_NAME)
    span.set_attribute("variation", variation)
    span.set_attribute("rollout_pct", VARIATION_ROLLOUT_PCT.get(variation, 0))

    flagsmith.track_event("checkout_started", identifier=identifier)
    ok, converted, latency_ms = run_variation(variation, identifier)

    # 4. Conversion events. Names must match the metrics in Flagsmith exactly.
    #    A bounce on the slow path sends neither: no conversion, no failure.
    if not ok:
        flagsmith.track_event("checkout_failed", identifier=identifier)
    elif converted:
        flagsmith.track_event(
            "checkout_completed", identifier=identifier, value=request.amount
        )
    flagsmith.track_event(
        "checkout_duration_ms", identifier=identifier, value=latency_ms
    )

    # Health metrics, labelled by the flag state this request ran under.
    metric_attrs = {
        "flag": FEATURE_NAME,
        "variation": variation,
        "rollout_pct": VARIATION_ROLLOUT_PCT.get(variation, 0),
        "status": "200" if ok else "500",
    }
    _requests_counter.add(1, metric_attrs)
    _duration_histogram.record(latency_ms / 1000, metric_attrs)
    _fallback_requests.labels(
        FEATURE_NAME, variation, VARIATION_ROLLOUT_PCT.get(variation, 0), "200" if ok else "500"
    ).inc()
    _fallback_duration.labels(
        FEATURE_NAME, variation, VARIATION_ROLLOUT_PCT.get(variation, 0)
    ).observe(latency_ms)
    with _lock:
        _health_window.append({"ok": ok, "latency_ms": latency_ms})

    # 5/6. The response the dashboard and traffic generator read. A failed
    # checkout is a failed operation: HTTP 500, same JSON body.
    body = {
        "variation": variation,
        "latency_ms": latency_ms,
        "success": ok,
        "converted": converted,
    }
    return JSONResponse(status_code=200 if ok else 500, content=body)
