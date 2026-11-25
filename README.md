# FlagPilot

A lifecycle autopilot for self-hosted Flagsmith. One dashboard shows every
flag's status — active, stale, or dead — with the evidence to prove it.
Winning experiments are auto-graduated 5 → 25 → 50 → 75 → 100% behind a
health gate, rolled back automatically the moment errors spike, and dead
flags are purged through a real GitHub PR.

## Try it

```sh
docker compose up -d
```

Then open the dashboard at http://localhost:8003 and Flagsmith at
http://localhost:8000.

## Run it

- Python 3.14 and [uv](https://docs.astral.sh/uv/)
- clone this repo
- `cp .env.example .env` and fill in the Flagsmith and GitHub values
- `uv sync`
- `docker compose up -d`
- `uv run python scripts/seed_flagsmith.py` (recreates the full Flagsmith setup)
- `uv run --env-file .env uvicorn flagpilot.main:app --reload --port 8003`

## How it works

- **Experiment**: the demo shop records exposures and conversions for
  `checkout_v2`; win probability appears on the experiment.
- **Auto graduation**: a worker polls win probability (Flagsmith) and
  error rate / p95 (Prometheus) every 30 seconds, and moves the rollout up
  5 → 25 → 50 → 75 → 100% only when both signals are healthy.
- **Rollback**: error rate at 1% or a p95 regression sends the flag back
  to the last healthy step and pauses the autopilot, with the exact
  reason logged.
- **Purge**: dead flags get a preview, a config snapshot, and a GitHub PR
  that removes the flag check and keeps the enabled code; the flag goes
  pending until the PR merges.

## CLI

```sh
uv run flagpilot status [--flag NAME]
uv run flagpilot graduate --flag NAME --mode incremental|instant [--confirm]
uv run flagpilot purge --flag NAME [--preview]
```

## Reuse

```sh
uv sync
docker compose up -d
uv run python scripts/seed_flagsmith.py
```

Then point any app's Flagsmith SDK at your instance and watch the
portfolio on the dashboard.

## Notes

Demo seed data and simulated checkout numbers. Self-hosted Flagsmith only.
