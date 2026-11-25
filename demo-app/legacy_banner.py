"""DEMO SEED: the shipped legacy_banner feature.

This file stands in for a flag check a real app forgets to remove after a
flag ships at 100%: the disabled branch is dead weight, the enabled branch
is the product. FlagPilot's purge removes the check and keeps the enabled
code.
"""


def flag_enabled(name: str) -> bool:
    """DEMO SEED stand-in for the demo app's flag check. The flag shipped:
    everyone gets the new banner."""
    return True


def render_homepage_banner() -> str:
    # FlagPilot purge target: the check below is the only Flagsmith
    # dependency left in this file after the flag shipped.
    if flag_enabled("legacy_banner"):
        return "banner-v2"
    # disabled branch: dead weight once the flag has shipped
    return "banner-v1"
