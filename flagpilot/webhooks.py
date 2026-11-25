"""Flagsmith webhook receiver.

POST /webhooks/flagsmith accepts Flagsmith's flag-change webhooks,
verifies the shared secret when FLAGSMITH_WEBHOOK_SECRET is set, logs
the change (flag, change type, actor, payload) to Postgres, and triggers
a background status recheck for that flag — never blocking the response.
"""

from __future__ import annotations

import asyncio
import hmac
import os

from fastapi import Request
from fastapi.responses import JSONResponse

from flagpilot import store
from flagpilot.evidence import flags_with_evidence
from flagpilot.flagsmith_client import list_flags

WEBHOOK_SECRET_HEADER = "X-Flagsmith-Signature"


def _secret() -> str | None:
    secret = os.environ.get("FLAGSMITH_WEBHOOK_SECRET", "")
    return secret or None


def _flag_name(payload: dict) -> str | None:
    data = payload.get("data") or {}
    feature = data.get("feature") or {}
    return feature.get("name") or payload.get("flag") or None


def _change_type(payload: dict) -> str | None:
    return payload.get("event_type") or payload.get("action") or None


def _actor(payload: dict) -> str | None:
    data = payload.get("data") or {}
    owner = data.get("owner") or payload.get("owner") or {}
    return owner.get("email") if isinstance(owner, dict) else None


def _recheck_status(flag_name: str | None) -> None:
    """Refresh the flag's status evidence, best effort, in the background."""
    if not flag_name:
        return

    def run() -> None:
        try:
            flags_with_evidence(list_flags())
        except Exception:
            pass  # the recheck is best effort; the next dashboard read retries anyway

    try:
        asyncio.get_running_loop().create_task(asyncio.to_thread(run))
    except RuntimeError:
        pass


def register(app) -> None:
    """Attach the webhook route to the app."""

    @app.post("/webhooks/flagsmith")
    async def flagsmith_webhook(request: Request) -> JSONResponse:
        secret = _secret()
        if secret:
            provided = request.headers.get(WEBHOOK_SECRET_HEADER, "")
            if not hmac.compare_digest(provided, secret):
                return JSONResponse({"detail": "invalid signature"}, status_code=401)

        try:
            payload = await request.json()
        except Exception:
            payload = {}

        flag_name = _flag_name(payload)
        store.log_webhook_event(
            flag_name or "unknown",
            _change_type(payload),
            _actor(payload),
            payload,
        )
        _recheck_status(flag_name)
        return {"received": True}
