"""The FlagPilot CLI: the same functions the dashboard uses, in a terminal.

uv run flagpilot status [--flag NAME]
uv run flagpilot graduate --flag NAME --mode incremental|instant [--confirm]
uv run flagpilot purge --flag NAME [--preview]
"""

from __future__ import annotations

import argparse
import json
import sys

from flagpilot.evidence import flags_with_evidence
from flagpilot.flagsmith_client import list_flags
from flagpilot.graduation import instant_preflight
from flagpilot import purge
from flagpilot import worker


def _status(flag_name: str | None) -> None:
    flags = flags_with_evidence(list_flags())
    if flag_name:
        flags = [f for f in flags if f["name"] == flag_name]
        if not flags:
            print(f"flag {flag_name!r} not found", file=sys.stderr)
            sys.exit(1)
    for f in flags:
        print(f"{f['name']:22} {f['status']:8} last evaluated: {f.get('last_evaluated') or 'never'}")
        for reason in f.get("status_reasons", []):
            print(f"  - {reason}")


def _graduate(flag_name: str, mode: str, confirm: bool) -> None:
    from flagpilot.flagsmith_client import apply_rollout, find_running_experiment
    from flagpilot.health import health_snapshot
    from flagpilot.usage import usage_for_feature
    from flagpilot.store import log_graduation, record_metric

    experiment = find_running_experiment(flag_name)
    if experiment is None:
        print(f"no running experiment for {flag_name!r}", file=sys.stderr)
        sys.exit(1)
    rollout = experiment.get("experiment_rollout") or {}
    current = int(rollout.get("rollout_percentage", 0))

    if mode == "instant":
        health = health_snapshot(flag_name)
        if health["error_rate"] is None or health["p95_ms"] is None or health["baseline_p95_ms"] is None:
            print("no health evidence — refusing the instant jump", file=sys.stderr)
            sys.exit(1)
        gate = instant_preflight(health["error_rate"], health["p95_ms"], health["baseline_p95_ms"])
        if gate.decision != "advance":
            print(f"instant refused: {gate.reason}", file=sys.stderr)
            sys.exit(1)
        if not confirm:
            print("instant mode requires an explicit confirm: pass --confirm", file=sys.stderr)
            sys.exit(1)
        apply_rollout(experiment, 100)
        log_graduation(flag_name, current, 100, "advance", "instant jump after clean preflight and explicit confirm", paused=False)
        print(f"instant: {current}% → 100%. Watch window runs for 10 minutes; any health failure rolls back to 75%.")
        print("Run `uv run flagpilot status --flag " + flag_name + "` after the watch window to verify.")
        return

    actions = worker.graduation_tick()
    mine = [a for a in actions if a["flag"] == flag_name]
    if not mine:
        print(f"no applied change for {flag_name!r} this tick (held or not configured)", file=sys.stderr)
        for action in actions:
            print(f"applied: {action['flag']} {action['current_step']}% → {action['target_step']}% ({action['decision']})")
        return
    for action in mine:
        print(f"{action['decision']}: {action['current_step']}% → {action['target_step']}% | {action['reason']}")


def _purge(flag_name: str, preview_only: bool) -> None:
    if preview_only:
        print(json.dumps(purge.preview(flag_name), indent=1))
        return
    result = purge.execute(flag_name)
    print("PR:", result.get("pr_url") or "(none created)")
    if result.get("existing"):
        print("(existing branch/PR returned — nothing duplicated)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="flagpilot", description="Flag lifecycle autopilot CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="list flags with status and evidence")
    status.add_argument("--flag", help="one flag, with full evidence")

    graduate = sub.add_parser("graduate", help="move a flag up the graduation ladder")
    graduate.add_argument("--flag", required=True)
    graduate.add_argument("--mode", choices=["incremental", "instant"], default="incremental")
    graduate.add_argument("--confirm", action="store_true", help="explicit confirm for instant mode")

    purge_parser = sub.add_parser("purge", help="purge a dead flag through a GitHub PR")
    purge_parser.add_argument("--flag", required=True)
    purge_parser.add_argument("--preview", action="store_true", help="preview only, no PR")

    args = parser.parse_args(argv)
    if args.command == "status":
        _status(args.flag)
    elif args.command == "graduate":
        _graduate(args.flag, args.mode, args.confirm)
    elif args.command == "purge":
        _purge(args.flag, args.preview)


if __name__ == "__main__":
    main()
