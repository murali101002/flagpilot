"""Flag usage evidence, read from the Flagsmith warehouse (local Postgres).

The warehouse is where SDK events land: one `$flag_exposure` row per
evaluation of one feature by one identity. The Flagsmith Admin API does
not expose evaluation history, so FlagPilot reads counts and last-seen
timestamps here — the same database Flagsmith itself uses as warehouse.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import psycopg

EXPOSURE_EVENT = "$flag_exposure"


def _dsn() -> str:
    dsn = os.environ.get("FLAGSMITH_DB_DSN", "")
    if not dsn:
        raise RuntimeError(
            "FLAGSMITH_DB_DSN is not set — the Flagsmith database connection string"
        )
    return dsn


def _environment_key() -> str:
    key = os.environ.get("FLAGSMITH_CLIENT_ENVIRONMENT_KEY", "")
    if not key:
        raise RuntimeError(
            "FLAGSMITH_CLIENT_ENVIRONMENT_KEY is not set — the environment's "
            "client-side SDK key (warehouse event rows carry that key)"
        )
    return key


def usage_for_feature(feature_name: str) -> dict:
    """Evaluation evidence for one feature.

    Returns {"last_evaluated": ISO str | None,
             "evaluations": {"7": n, "14": n, "30": n},
             "total_evaluations": n}.
    """
    now = datetime.now(timezone.utc)
    environment_key = _environment_key()
    with psycopg.connect(_dsn()) as connection:
        row = connection.execute(
            "SELECT max(timestamp) FROM experimentation_sdkexperimentationevent "
            "WHERE environment_key = %s AND feature_name = %s AND event = %s",
            (environment_key, feature_name, EXPOSURE_EVENT),
        ).fetchone()
        last = row[0] if row else None

        evaluations: dict[int, int] = {}
        for days in (7, 14, 30):
            evaluations[days] = connection.execute(
                "SELECT count(*) FROM experimentation_sdkexperimentationevent "
                "WHERE environment_key = %s AND feature_name = %s AND event = %s "
                "AND timestamp >= %s",
                (environment_key, feature_name, EXPOSURE_EVENT, now - timedelta(days=days)),
            ).fetchone()[0]

        total = connection.execute(
            "SELECT count(*) FROM experimentation_sdkexperimentationevent "
            "WHERE environment_key = %s AND feature_name = %s AND event = %s",
            (environment_key, feature_name, EXPOSURE_EVENT),
        ).fetchone()[0]

    return {
        "last_evaluated": last.isoformat() if last else None,
        "evaluations": {str(days): count for days, count in evaluations.items()},
        "total_evaluations": total,
    }
