"""Basic sanity tests: the pure logic must behave, offline."""

from __future__ import annotations

from flagpilot.graduation import decide, instant_preflight
from flagpilot.models import Flag
from flagpilot.purge import _remove_flag_check
from flagpilot.flagsmith_client import _served_value


def test_graduation_advances_when_healthy():
    decision = decide(25, 0.002, 300, 280, 500)
    assert decision.decision == "advance"
    assert decision.target_step == 50


def test_graduation_rolls_back_on_error_spike_and_pauses():
    decision = decide(50, 0.05, 300, 280, 500)
    assert decision.decision == "rollback"
    assert decision.target_step == 25
    assert decision.pause is True


def test_graduation_holds_on_low_sample():
    decision = decide(50, 0.002, 300, 280, 40)
    assert decision.decision == "hold"


def test_instant_preflight_refuses_unhealthy():
    assert instant_preflight(0.02, 300, 280).decision == "hold"


def test_models_round_trip():
    flag = Flag(
        name="checkout_v2",
        enabled=True,
        value="control",
        last_change=None,
        environment="Development",
    )
    assert flag.as_dict()["status"] == "unknown"


def test_purge_removes_check_keeps_enabled_code():
    source = (
        "def banner():\n"
        "    if flag_enabled(\"legacy_banner\"):\n"
        "        return \"banner-v2\"\n"
        "    return \"banner-v1\"\n"
    )
    new_content, removed = _remove_flag_check(source, "legacy_banner")
    assert 'return "banner-v2"' in new_content
    assert 'return "banner-v1"' not in new_content
    assert removed[0].strip().startswith("if flag_enabled")
    assert len(removed) == 2


def test_served_value_coerces_numbers():
    assert _served_value(100) == "100"
    assert _served_value("old") == "old"
    assert _served_value(None) is None
