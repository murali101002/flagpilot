"""Environment promotion: dev → staging → prod, with diff and approval.

Practice scope: the local Flagsmith has Development and Production, and
the promotion module creates Staging on first use. Promoting copies the
source environment's flag state (enabled + value) onto the target
environment through the __future__ flag-write endpoint, and logs the
promotion to Postgres. Every promotion requires `approved: true` in the
request — the API refuses silent promotions.
"""

from __future__ import annotations

from flagpilot import store
from flagpilot.flagsmith_client import (
    _client_environment_key,
    _get,
    _base_url,
    _headers,
    _require_env,
    find_feature,
)
import httpx

PROMOTION_CHAIN = {"Development": "Staging", "Staging": "Production"}


def _environments() -> list[dict]:
    project_id = int(_require_env("FLAGSMITH_PROJECT_ID", "the numeric project id"))
    return list(_get(f"/projects/{project_id}/environments/"))


def ensure_staging() -> int:
    """Create the Staging environment if it does not exist (Phase 10)."""
    for environment in _environments():
        if environment["name"] == "Staging":
            return environment["id"]
    project_id = int(_require_env("FLAGSMITH_PROJECT_ID", "the numeric project id"))
    response = httpx.post(
        f"{_base_url()}/environments/",
        headers=_headers(),
        json={"name": "Staging", "project": project_id},
        timeout=10.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"create Staging → {response.status_code}: {response.text[:200]}")
    created = response.json()
    # A new environment needs a server-side key for local evaluation; the
    # Admin API mints one alongside. Nothing to do here — the state copy
    # below uses the environment's id through the states endpoint.
    return created["id"]


def _state_for_environment(flag_name: str, environment_id: int) -> dict:
    """A flag's enabled/value in one environment, via the admin featurestates
    read (keyed by environment id + feature id)."""
    feature = find_feature(flag_name)
    if feature is None:
        raise RuntimeError(f"flag {flag_name!r} not found in Flagsmith")
    states = _get(
        "/features/featurestates/",
        params={"environment": environment_id, "feature": feature["id"]},
    )
    items = states.get("results", []) if isinstance(states, dict) else states
    for row in items:
        if row.get("identity") is None and row.get("feature_segment") is None:
            raw_value = row.get("feature_state_value")
            if isinstance(raw_value, dict):
                raw_value = raw_value.get("value")
            return {"enabled": bool(row.get("enabled")), "value": raw_value}
    raise RuntimeError(f"no environment-level state found for {flag_name!r}")


def promotion_state(flag_name: str) -> dict:
    """The flag's state in dev → staging → prod, in chain order."""
    if "Staging" not in [env["name"] for env in _environments()]:
        ensure_staging()
    states = {}
    for environment in _environments():
        if environment["name"] in PROMOTION_CHAIN or environment["name"] == "Production":
            states[environment["name"]] = {
                "enabled": None,
                "value": None,
                "environment_id": environment["id"],
            }
    for name in states:
        try:
            detail = _state_for_environment(flag_name, states[name]["environment_id"])
            states[name]["enabled"] = detail["enabled"]
            states[name]["value"] = detail["value"]
            states[name]["environment_key"] = detail["environment"]
        except Exception as error:
            states[name]["error"] = str(error)
    return {"flag": flag_name, "chain": PROMOTION_CHAIN, "environments": states}


def promote(flag_name: str, from_environment: str, to_environment: str, approved: bool) -> dict:
    """Copy the source state onto the target environment. Requires approval."""
    if not approved:
        raise RuntimeError("promotion refused: approval is required (approved: true)")
    expected_from = {v: k for k, v in PROMOTION_CHAIN.items()}.get(to_environment)
    if expected_from != from_environment:
        raise RuntimeError(
            f"promotion chain violation: {to_environment} is promoted from {expected_from}, not {from_environment}"
        )
    source = _state_for_environment(flag_name, _environment_id_by_name(from_environment))

    value = source.get("value")
    body: dict = {"environment_default": {"enabled": bool(source.get("enabled"))}}
    if value is not None:
        body["environment_default"]["value"] = {"type": "string", "value": str(value)}

    target_key = _environment_key_by_name(to_environment)
    response = httpx.patch(
        f"{_base_url().removesuffix('/api/v1')}/api/__future__/environments/{target_key}/features/"
        + _feature_id(flag_name) + "/",
        headers=_headers(),
        json=body,
        timeout=10.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"promote → {response.status_code}: {response.text[:200]}")

    store.record_promotion(flag_name, from_environment, to_environment, bool(source.get("enabled")), str(value))
    return {
        "flag": flag_name,
        "from": from_environment,
        "to": to_environment,
        "applied": {"enabled": source.get("enabled"), "value": source.get("value")},
    }


def _environment_id_by_name(name: str) -> int:
    for environment in _environments():
        if environment["name"] == name:
            return environment["id"]
    raise RuntimeError(f"environment {name!r} not found")


def _environment_key_by_name(name: str) -> str:
    for environment in _environments():
        if environment["name"] == name:
            return environment["api_key"]
    raise RuntimeError(f"environment {name!r} not found")


def _feature_id(flag_name: str) -> str:
    feature = find_feature(flag_name)
    if feature is None:
        raise RuntimeError(f"flag {flag_name!r} not found")
    return str(feature["id"])
