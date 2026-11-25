"""DEMO SEED: flag references for the practice portfolio.

These constants stand in for the way a real app references its flags in
code, so FlagPilot's code-reference search finds the seeded flags. One
status-demo flag is deliberately NOT referenced in this module at all:
a dead flag has no code reference, and that absence is its evidence.
"""

SEARCH_RANKING_V2 = "search_ranking_v2"
NEW_ONBOARDING = "new_onboarding"
API_RATE_LIMIT = "api_rate_limit"
DARK_MODE = "dark_mode"
PRICING_TIER = "pricing_tier"
FLAGPILOT_AUTO_GRAD = "flagpilot_auto_grad"
FLAGPILOT_PURGE_PR = "flagpilot_purge_pr"
OLD_SEARCH = "old_search"

# (The dead status-demo flag is intentionally absent from this module: zero
# evaluations AND no code reference is what makes it dead.)
