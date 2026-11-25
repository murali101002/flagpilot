"""Every Flagsmith Admin API call FlagPilot makes lives in this module.

Routers, the CLI (Phase 13) and the webhook never call Flagsmith
directly — they call these functions, so configuration and error
handling happen in exactly one place.
"""

from __future__ import annotations

import json
import os

import httpx

from flagpilot.models import Flag

_PAGE_SIZE = 100


def _base_url() -> str:
    """The Admin API root, derived from FLAGSMITH_API_URL.

    .env carries the SDK form (…/api/v1/); tolerate the bare host form too.
    """
    url = os.environ.get("FLAGSMITH_API_URL", "http://localhost:8000/api/v1/").rstrip("/")
    if not url.endswith("/api/v1"):
        url = f"{url}/api/v1"
    return url


def _api_root() -> str:
    """The /api root (for the __future__ flag-write endpoints)."""
    return _base_url().removesuffix("/api/v1")


def _client_environment_key() -> str:
    key = os.environ.get("FLAGSMITH_CLIENT_ENVIRONMENT_KEY", "")
    if not key:
        raise RuntimeError(
            "FLAGSMITH_CLIENT_ENVIRONMENT_KEY is not set — the environment's "
            "client-side SDK key (the __future__ flag-write endpoints key on it)"
        )
    return key


def _headers() -> dict[str, str]:
    token = os.environ.get("FLAGSMITH_API_TOKEN", "")
    if not token:
        raise RuntimeError("FLAGSMITH_API_TOKEN is not set — copy .env.example to .env and fill it in")
    return {"Authorization": f"Api-Key {token}"}


def _require_env(name: str, hint: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is not set — {hint}")
    return value


def _get(path: str, params: dict | None = None) -> dict:
    response = httpx.get(
        f"{_base_url()}{path}", headers=_headers(), params=params, timeout=10.0
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Flagsmith Admin API call failed: GET {path} → {response.status_code}: {response.text[:200]}"
        )
    return response.json()


def _paginate(path: str, params: dict | None = None):
    """Yield every item of a paginated Flagsmith list endpoint."""
    page = 1
    while True:
        payload = _get(path, {**(params or {}), "page": page, "page_size": _PAGE_SIZE})
        items = payload.get("results", []) if isinstance(payload, dict) else payload
        yield from items
        if isinstance(payload, dict) and payload.get("next"):
            page += 1
        else:
            return


def _environment_name(project_id: int, environment_id: int) -> str:
    environments = _paginate(f"/projects/{project_id}/environments/")
    for item in environments:
        if item.get("id") == environment_id:
            return item.get("name", f"environment-{environment_id}")
    return f"environment-{environment_id}"


def _served_value(raw: object) -> str | None:
    """feature_state_value arrives as a string (or a typed dict on some
    versions); number and boolean configs are stringified for display."""
    if raw is None or isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return raw.get("value")
    return json.dumps(raw)


def _last_change(feature: dict) -> str | None:
    """The freshest change timestamp the feature exposes, if any."""
    for key in ("last_modified_in_current_environment", "last_modified_in_any_environment"):
        if feature.get(key):
            return feature[key]
    return None


def list_flags() -> list[Flag]:
    """Every non-archived flag in the configured project + environment."""
    project_id = int(_require_env("FLAGSMITH_PROJECT_ID", "the numeric project id (1 locally)"))
    environment_id = int(
        _require_env("FLAGSMITH_ENVIRONMENT_ID", "the numeric environment id (2 for local Development)")
    )
    environment = _environment_name(project_id, environment_id)

    flags: list[Flag] = []
    for feature in _paginate(f"/projects/{project_id}/features/", {"environment": environment_id}):
        if feature.get("is_archived"):
            continue
        state = feature.get("environment_feature_state") or {}
        flags.append(
            Flag(
                name=feature["name"],
                enabled=bool(state.get("enabled")),
                value=_served_value(state.get("feature_state_value")),
                last_change=_last_change(feature),
                environment=environment,
                status="unknown",
            )
        )
    return flags


def find_feature(name: str) -> dict | None:
    """The project feature with this name (with its environment state), or None."""
    project_id = int(_require_env("FLAGSMITH_PROJECT_ID", "the numeric project id (1 locally)"))
    environment_id = int(
        _require_env("FLAGSMITH_ENVIRONMENT_ID", "the numeric environment id (2 for local Development)")
    )
    for feature in _paginate(f"/projects/{project_id}/features/", {"environment": environment_id}):
        if feature["name"] == name:
            return feature
    return None


def create_feature(name: str, *, description: str | None = None, initial_value: object = None) -> dict:
    """Create a project feature. initial_value of a non-string type makes it
    a remote config (number); a string makes it remote config; omitting it
    makes a plain boolean flag."""
    project_id = int(_require_env("FLAGSMITH_PROJECT_ID", "the numeric project id (1 locally)"))
    payload: dict = {"name": name}
    if description:
        payload["description"] = description
    if initial_value is not None:
        payload["initial_value"] = initial_value
    response = httpx.post(
        f"{_base_url()}/projects/{project_id}/features/",
        headers=_headers(), json=payload, timeout=10.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"create_feature({name!r}) → {response.status_code}: {response.text[:200]}")
    return response.json()


def create_feature_variant(feature_id: int, *, value: str, key: str, weight: float = 0) -> dict:
    """Add a keyed multivariate variant (weight 0: experiments set the split)."""
    project_id = int(_require_env("FLAGSMITH_PROJECT_ID", "the numeric project id (1 locally)"))
    payload = {
        "feature": feature_id,
        "string_value": value,
        "key": key,
        "default_percentage_allocation": weight,
    }
    response = httpx.post(
        f"{_base_url()}/projects/{project_id}/features/{feature_id}/mv-options/",
        headers=_headers(), json=payload, timeout=10.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"create_feature_variant({value!r}) → {response.status_code}: {response.text[:200]}"
        )
    return response.json()


def set_flag_enabled(feature_id: int, enabled: bool) -> dict:
    """Toggle the flag in the configured environment, via the __future__
    flag-write endpoint (keyed by the client-side environment key). The
    change nests under environment_default."""
    response = httpx.patch(
        f"{_api_root()}/api/__future__/environments/{_client_environment_key()}/features/{feature_id}/",
        headers=_headers(),
        json={"environment_default": {"enabled": enabled}},
        timeout=10.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"set_flag_enabled({feature_id}) → {response.status_code}: {response.text[:200]}")
    return response.json()


def find_running_experiment(flag_name: str) -> dict | None:
    """The running experiment for a flag (detail includes rollout config)."""
    experiments = _get(f"/environments/{_client_environment_key()}/experiments/")
    items = experiments.get("results", []) if isinstance(experiments, dict) else experiments
    for item in items:
        if item.get("status") != "running":
            continue
        detail = _get(f"/environments/{_client_environment_key()}/experiments/{item['id']}/")
        if detail.get("feature", {}).get("name") == flag_name:
            return detail
    return None


def experiment_results(experiment_id: int) -> dict:
    """The stored results payload (win probability lives in inference)."""
    return _get(f"/environments/{_client_environment_key()}/experiments/{experiment_id}/results/")


def apply_rollout(experiment: dict, rollout_percentage: int) -> dict:
    """Re-point a running experiment's rollout at a new percentage, keeping
    the configured value and variant shares (the endpoint requires every
    variant to be listed)."""
    rollout = experiment.get("experiment_rollout") or {}
    payload = {
        "enabled": rollout.get("enabled", True),
        "rollout_percentage": rollout_percentage,
        "feature_state_value": rollout.get("feature_state_value"),
        "multivariate_feature_state_values": rollout.get("multivariate_feature_state_values", []),
    }
    response = httpx.patch(
        f"{_base_url()}/environments/{_client_environment_key()}/experiments/{experiment['id']}/rollout/",
        headers=_headers(), json=payload, timeout=10.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"apply_rollout({rollout_percentage}) → {response.status_code}: {response.text[:200]}"
        )
    return response.json()
