"""DEMO SEED: recreate the complete Flagsmith setup in one command.

Run: uv run --env-file .env python scripts/seed_flagsmith.py

Creates (idempotently — everything that already exists is skipped):
- the project "FlagPilot Demo" (new projects get Development + Production)
- checkout_v2 (multivariate: old / new_cached / new_prefetch, keyed)
- the 8-flag portfolio (search_ranking_v2, new_onboarding, api_rate_limit,
  dark_mode, pricing_tier, flagpilot_auto_grad, flagpilot_purge_pr)
- the two status-demo flags (old_search, legacy_banner)
- segments internal_team and beta_users
- remote config checkout_config
- 5 experiment metrics
- the checkout_v2 experiment (control 33% serving the legacy experience,
  new_cached 33%, new_prefetch 34%), started

Run it twice: the second run creates nothing and reports everything as
existing. Uses FLAGSMITH_API_TOKEN + FLAGSMITH_API_URL from env.
"""

from __future__ import annotations

import os

import httpx


def _base() -> str:
    url = os.environ.get("FLAGSMITH_API_URL", "http://localhost:8000/api/v1/").rstrip("/")
    return url if url.endswith("/api/v1") else f"{url}/api/v1"


def _headers() -> dict[str, str]:
    token = os.environ.get("FLAGSMITH_API_TOKEN", "")
    if not token:
        raise RuntimeError("FLAGSMITH_API_TOKEN is not set")
    return {"Authorization": f"Api-Key {token}"}


def _get(path: str, params: dict | None = None):
    response = httpx.get(f"{_base()}{path}", headers=_headers(), params=params, timeout=10.0)
    if response.status_code >= 400:
        raise RuntimeError(f"GET {path} → {response.status_code}: {response.text[:200]}")
    return response.json()


def _post(path: str, payload: dict, params: dict | None = None):
    response = httpx.post(f"{_base()}{path}", headers=_headers(), json=payload, params=params, timeout=10.0)
    if response.status_code >= 400:
        raise RuntimeError(f"POST {path} → {response.status_code}: {response.text[:200]}")
    return response.json()


def _patch(path: str, payload: dict):
    response = httpx.patch(f"{_base()}{path}", headers=_headers(), json=payload, timeout=10.0)
    if response.status_code >= 400:
        raise RuntimeError(f"PATCH {path} → {response.status_code}: {response.text[:200]}")
    return response.json()


def _seed_project_name() -> str:
    return os.environ.get("FLAGSMITH_SEED_PROJECT_NAME", "FlagPilot Demo")


def _ensure_project() -> dict:
    org = int(os.environ.get("FLAGSMITH_ORGANISATION_ID", "1"))
    for project in _get("/projects/"):
        if project["name"] == _seed_project_name():
            print(f"project: exists ({project['id']})")
            return project
    project = _post("/projects/", {"name": _seed_project_name(), "organisation": org})
    print(f"project: created ({project['id']})")
    return project


def _ensure_environment(project: dict, name: str) -> dict:
    for environment in _get(f"/projects/{project['id']}/environments/"):
        if environment["name"] == name:
            print(f"environment {name}: exists")
            return environment
    environment = _post(f"/projects/{project['id']}/environments/", {"name": name})
    print(f"environment {name}: created")
    return environment


def _ensure_feature(project_id: int, name: str, initial_value=None) -> dict:
    payload = _get(f"/projects/{project_id}/features/")
    existing = payload.get("results", []) if isinstance(payload, dict) else payload
    for feature in existing:
        if feature["name"] == name:
            print(f"feature {name}: exists")
            return feature
    payload = {"name": name}
    if initial_value is not None:
        payload["initial_value"] = initial_value
    feature = _post(f"/projects/{project_id}/features/", payload)
    print(f"feature {name}: created")
    return feature


def _ensure_variant(project_id: int, feature_id: int, value: str, key: str) -> None:
    for option in _get(f"/projects/{project_id}/features/{feature_id}/").get("multivariate_options", []):
        if option.get("key") == key:
            return
    _post(
        f"/projects/{project_id}/features/{feature_id}/mv-options/",
        {"feature": feature_id, "string_value": value, "key": key, "default_percentage_allocation": 0},
    )
    print(f"variant {key}: created")


def _enable_in_environment(project_id: int, environment_id: int, name: str) -> None:
    payload = _get(
        f"/projects/{project_id}/features/", params={"environment": environment_id}
    )
    existing = payload.get("results", []) if isinstance(payload, dict) else payload
    for feature in existing:
        if feature["name"] == name:
            state = feature.get("environment_feature_state") or {}
            state_id = state.get("id")
            if state_id and state.get("enabled"):
                return
            _patch(f"/environments/{environment_id}/featurestates/{state_id}/", {"enabled": True})
            return
    raise RuntimeError(f"feature {name!r} not found when enabling")


def _ensure_segment(project_id: int, name: str, trait: str, value: str) -> None:
    payload = _get(f"/projects/{project_id}/segments/")
    existing = payload.get("results", []) if isinstance(payload, dict) else payload
    for segment in existing:
        if segment["name"] == name:
            print(f"segment {name}: exists")
            return
    _post(
        f"/projects/{project_id}/segments/",
        {
            "name": name,
            "project": project_id,
            "rules": [
                {
                    "type": "ALL",
                    "rules": [
                        {
                            "type": "ANY",
                            "rules": [
                                {
                                    "type": "ALL",
                                    "rules": [
                                        {
                                            "type": "CONDITION",
                                            "property": trait,
                                            "operator": "EQUAL",
                                            "value": value,
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    )
    print(f"segment {name}: created")


def _ensure_metric(environment_key: str, name: str, event: str, aggregation: str, direction: str) -> int:
    payload = _get(f"/environments/{environment_key}/experiment-metrics/")
    existing = payload.get("results", []) if isinstance(payload, dict) else payload
    for metric in existing:
        if metric["name"] == name:
            print(f"metric {name}: exists")
            return metric["id"]
    metric = _post(
        f"/environments/{environment_key}/experiment-metrics/",
        {
            "name": name,
            "aggregation": aggregation,
            "direction": direction,
            "definition": {"version": 1, "event": event},
        },
    )
    print(f"metric {name}: created")
    return metric["id"]


def _ensure_experiment(environment_key: str, feature: dict, metric_id: int) -> None:
    payload = _get(f"/environments/{environment_key}/experiments/")
    existing = payload.get("results", []) if isinstance(payload, dict) else payload
    for experiment in existing:
        if experiment.get("feature", {}).get("id") == feature["id"]:
            print("experiment: exists")
            return
    experiment = _post(
        f"/environments/{environment_key}/experiments/",
        {
            "feature": feature["id"],
            "name": "Checkout v2 latency win",
            "hypothesis": "new_cached cuts checkout latency from ~4.8s to ~240ms without hurting conversion",
            "metrics": [{"metric": metric_id, "expected_direction": "increase"}],
            "experiment_rollout": {
                "enabled": True,
                "rollout_percentage": 100,
                "feature_state_value": {"type": "string", "value": "control"},
                "multivariate_feature_state_values": [
                    {"multivariate_feature_option": 1, "percentage_allocation": 0}
                ],
            },
        },
    )
    _patch(
        f"/environments/{environment_key}/experiments/{experiment['id']}/rollout/",
        {
            "enabled": True,
            "rollout_percentage": 100,
            "feature_state_value": {"type": "string", "value": "control"},
            "multivariate_feature_state_values": [
                {"multivariate_feature_option": option["id"], "percentage_allocation": 33 if option["key"] == "new_cached" else 34}
                for option in feature.get("multivariate_options", [])
                if option["key"] in ("new_cached", "new_prefetch")
            ],
        },
    )
    _post(f"/environments/{environment_key}/experiments/{experiment['id']}/start/", {})
    print("experiment: created and started")


def main() -> None:
    project = _ensure_project()
    development = _ensure_environment(project, "Development")
    _ensure_environment(project, "Production")

    checkout_v2 = _ensure_feature(project["id"], "checkout_v2", initial_value="control")
    for value, key in (("old", "old"), ("new_cached", "new_cached"), ("new_prefetch", "new_prefetch")):
        _ensure_variant(project["id"], checkout_v2["id"], value, key)

    portfolio = [
        ("search_ranking_v2", "old_ranking", [("old_ranking", "old_ranking"), ("new_ranking", "new_ranking"), ("semantic_ranking", "semantic_ranking")]),
        ("new_onboarding", "old_flow", [("old_flow", "old_flow"), ("short_flow", "short_flow")]),
        ("api_rate_limit", 100, []),
        ("dark_mode", None, []),
        ("pricing_tier", "enterprise", []),
        ("flagpilot_auto_grad", None, []),
        ("flagpilot_purge_pr", None, []),
        ("old_search", None, []),
        ("legacy_banner", None, []),
    ]
    for name, initial_value, variants in portfolio:
        feature = _ensure_feature(project["id"], name, initial_value=initial_value)
        for value, key in variants:
            _ensure_variant(project["id"], feature["id"], value, key)
        _enable_in_environment(project["id"], development["id"], name)
    _enable_in_environment(project["id"], development["id"], "checkout_v2")

    _ensure_segment(project["id"], "internal_team", "team", "internal")
    _ensure_segment(project["id"], "beta_users", "user_type", "beta")

    _ensure_feature(project["id"], "checkout_config", initial_value='{"cache_ttl": 60, "batch_size": 20, "retry_count": 2}')
    _enable_in_environment(project["id"], development["id"], "checkout_config")

    client_key = next(
        (
            environment["api_key"]
            for environment in _get(f"/projects/{project['id']}/environments/")
            if environment["id"] == development["id"]
        ),
        None,
    )
    if not client_key:
        raise RuntimeError("could not resolve the Development environment's client-side key")

    completion = _ensure_metric(client_key, "Checkout Completion Rate", "checkout_completed", "occurrence", "up")
    _ensure_metric(client_key, "Checkout Failure Rate", "checkout_failed", "occurrence", "down")
    _ensure_metric(client_key, "Average Order Value", "checkout_completed", "mean", "up")
    _ensure_metric(client_key, "Average Checkout Latency", "checkout_duration_ms", "mean", "down")
    _ensure_metric(client_key, "Total Revenue", "checkout_completed", "sum", "up")

    checkout_v2 = next(
        (
            feature
            for feature in _get(f"/projects/{project['id']}/features/").get("results", [])
            if feature["name"] == "checkout_v2"
        ),
        checkout_v2,
    )
    _ensure_experiment(client_key, checkout_v2, completion)

    print("seed complete.")


if __name__ == "__main__":
    main()
