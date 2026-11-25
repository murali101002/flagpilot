"""All Prometheus queries FlagPilot makes live in this module.

The worker (Phase 8) calls health_snapshot(); nothing else in the codebase
talks to Prometheus. The queries are the locked Phase 4 ones, labelled by
flag and variation:

- error rate: sum(rate(http_server_requests_total{flag=…, status=~"5.."}[5m]))
              / sum(rate(http_server_requests_total{flag=…}[5m]))
- p95:        histogram_quantile(0.95, sum(rate(
              http_server_request_duration_seconds_bucket{flag=…}[5m])) by (le))
- traffic:    sum(rate(http_server_requests_total{flag=…}[5m]))
"""

from __future__ import annotations

import math
import os

import httpx

WINDOW_MINUTES = 5


def _window() -> str:
    return f"[{WINDOW_MINUTES}m]"


def _prometheus_url() -> str:
    url = os.environ.get("FLAGPILOT_PROMETHEUS_URL", "http://localhost:9090")
    return url.rstrip("/")


def _query(promql: str) -> list[dict]:
    response = httpx.get(
        f"{_prometheus_url()}/api/v1/query", params={"query": promql}, timeout=10.0
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Prometheus query failed: {response.status_code}: {response.text[:200]}")
    return response.json().get("data", {}).get("result", [])


def _scalar(promql: str) -> float | None:
    """The single value of an instant query, or None when there is no data."""
    series = _query(promql)
    if not series:
        return None
    value = float(series[0]["value"][1])
    return None if math.isnan(value) else value


def error_rate(flag: str) -> float | None:
    """Share of requests answered with a 5xx, over the window.

    Numerator and denominator are queried separately: a perfectly healthy
    window has NO 5xx series at all, and that must read as 0.0 — not as
    missing evidence. None means no traffic at all.
    """
    selector = f'flag="{flag}"'
    numerator = _scalar(
        f"sum(rate(http_server_requests_total{{{selector}, status=~\"5..\"}}{_window()}))"
    )
    denominator = _scalar(f"sum(rate(http_server_requests_total{{{selector}}}{_window()}))")
    if denominator is None or denominator == 0:
        return None
    return (numerator or 0.0) / denominator


def p95_ms(flag: str, variation: str | None = None) -> float | None:
    """p95 checkout latency in milliseconds, optionally for one variation."""
    selector = f'flag="{flag}"'
    if variation is not None:
        selector += f', variation="{variation}"'
    promql = (
        "histogram_quantile(0.95, "
        f"sum(rate(http_server_request_duration_seconds_bucket{{{selector}}}{_window()})) by (le))"
    )
    seconds = _scalar(promql)
    return None if seconds is None else seconds * 1000.0


def traffic_rate(flag: str) -> float | None:
    """Requests per second, over the window."""
    promql = f"sum(rate(http_server_requests_total{{flag=\"{flag}\"}}{_window()}))"
    return _scalar(promql)


def baseline_p95_ms(flag: str) -> float | None:
    """p95 of the legacy experience, across BOTH arms that serve it: the
    control bucket (enrolled identities) and the flag's default value
    ("old", served to identities outside the rollout). Regex match on
    purpose — that's what makes it cover both names."""
    selector = f'flag="{flag}", variation=~"control|old"'
    promql = (
        "histogram_quantile(0.95, "
        f"sum(rate(http_server_request_duration_seconds_bucket{{{selector}}}{_window()})) by (le))"
    )
    seconds = _scalar(promql)
    return None if seconds is None else seconds * 1000.0


def health_snapshot(flag: str) -> dict:
    """Everything a graduation decision needs for one flag, in one call.

    baseline_p95_ms is the legacy experience's p95. Some legacy traffic
    exists at every rollout step, so the baseline stays fresh as the
    rollout ramps.
    """
    return {
        "flag": flag,
        "window_minutes": WINDOW_MINUTES,
        "error_rate": error_rate(flag),
        "p95_ms": p95_ms(flag),
        "baseline_p95_ms": baseline_p95_ms(flag),
        "traffic_rate": traffic_rate(flag),
    }
