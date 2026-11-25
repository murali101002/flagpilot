"""DEMO SEED: create the practice flag portfolio in local Flagsmith.

Run: uv run --env-file .env python scripts/seed_portfolio.py

Idempotent: a flag that already exists is skipped with a note. Every flag
is enabled in the configured Development environment. checkout_v2 is NOT
created here — it exists from Phase 1's manual setup and carries the
running hero experiment.

The two status-demo flags exist to put all three statuses on the board:
- old_search: created now, referenced in the demo app, zero traffic → stale
  (via the low-traffic arm).
- legacy_banner: zero evaluations and deliberately NOT referenced in the
  demo app's code → dead.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flagpilot import flagsmith_client as client

# name, initial_value (None = plain boolean), variants as (value, key).
PORTFOLIO = [
    {
        "name": "search_ranking_v2",
        "initial_value": "old_ranking",
        "variants": [
            ("old_ranking", "old_ranking"),
            ("new_ranking", "new_ranking"),
            ("semantic_ranking", "semantic_ranking"),
        ],
    },
    {
        "name": "new_onboarding",
        "initial_value": "old_flow",
        "variants": [("old_flow", "old_flow"), ("short_flow", "short_flow")],
    },
    {"name": "api_rate_limit", "initial_value": 100},          # remote config number
    {"name": "dark_mode"},                                      # boolean
    {"name": "pricing_tier", "initial_value": "enterprise"},   # remote config string
    {"name": "flagpilot_auto_grad"},                            # dogfood
    {"name": "flagpilot_purge_pr"},                             # dogfood
    {"name": "old_search"},                                     # status demo: stale
    {"name": "legacy_banner"},                                  # status demo: dead
]


def main() -> None:
    created: list[str] = []
    skipped: list[str] = []
    for spec in PORTFOLIO:
        name = spec["name"]
        existing = client.find_feature(name)
        if existing is None:
            feature = client.create_feature(name, initial_value=spec.get("initial_value"))
            for value, key in spec.get("variants", []):
                client.create_feature_variant(feature["id"], value=value, key=key)
            created.append(name)
        else:
            skipped.append(name)
        # Ensure-enabled runs every time, so the seed self-heals.
        client.set_flag_enabled(existing["id"] if existing else feature["id"], True)
    print("created:", created or "none")
    print("skipped (already exist):", skipped or "none")


if __name__ == "__main__":
    main()
