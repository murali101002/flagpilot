"""Traffic generator for the demo shop.

Manufactures synthetic users and checkouts so the running experiment
accumulates a real sample size. Talks only to the demo app's HTTP API —
it knows nothing about Flagsmith or flag names, so it cannot bias the
experiment. Run it again later and the same users simply check out more.
"""

import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

CHECKOUT_URL = os.environ.get("DEMO_CHECKOUT_URL", "http://localhost:8002/checkout")
USER_COUNT = int(os.environ.get("TRAFFIC_USER_COUNT", "100"))
CHECKOUTS_PER_USER = (5, 10) # random per user, inclusive
AMOUNT_RANGE = (20.0, 100.0)
WORKERS = 8
DELAY_SECONDS = 0.05

_lock = threading.Lock()
_stats: dict[str, dict[str, list]] = {}
_done = 0


def _record(variation: str, converted: bool, failed: bool, latency_ms: int) -> None:
    global _done
    with _lock:
        bucket = _stats.setdefault(
            variation,
            {"checkouts": [], "converted": [], "failed": [], "latency": []},
        )
        bucket["checkouts"].append(latency_ms)
        if converted:
            bucket["converted"].append(latency_ms)
        if failed:
            bucket["failed"].append(latency_ms)
        if latency_ms > 0: # instant bounces carry no real latency
            bucket["latency"].append(latency_ms)
        _done += 1


def _one_checkout(client: httpx.Client, user_id: str) -> None:
    amount = round(random.uniform(*AMOUNT_RANGE), 2)
    response = client.post(
        CHECKOUT_URL, json={"user_id": user_id, "amount": amount}
    )
    # Failed checkouts come back HTTP 500 with the same JSON body — the
    # failure is the checkout's outcome, not a transport error.
    data = response.json()
    _record(data["variation"], data["converted"], not data["success"], data["latency_ms"])


def _user_job(user_id: str) -> None:
    client = httpx.Client(timeout=30.0)
    try:
        for _ in range(random.randint(*CHECKOUTS_PER_USER)):
            _one_checkout(client, user_id)
            time.sleep(DELAY_SECONDS)
    finally:
        client.close()


def _print_summary() -> None:
    print()
    print(
        f"{'variation':<15}{'checkouts':>10}{'converted':>11}"
        f"{'failed':>8}{'conv %':>8}{'avg ms':>9}"
    )
    total_checkouts = total_failures = total_converted = 0
    for variation in sorted(_stats):
        bucket = _stats[variation]
        checkouts = len(bucket["checkouts"])
        converted = len(bucket["converted"])
        failures = len(bucket["failed"])
        avg = sum(bucket["latency"]) / len(bucket["latency"]) if bucket["latency"] else 0
        total_checkouts += checkouts
        total_failures += failures
        total_converted += converted
        print(
            f"{variation:<15}{checkouts:>10}{converted:>11}{failures:>8}"
            f"{converted / checkouts * 100:>7.0f}%{avg:>9.0f}"
        )
    print(
        f"{'TOTAL':<15}{total_checkouts:>10}{total_converted:>11}"
        f"{total_failures:>8}{total_converted / total_checkouts * 100:>7.0f}%"
    )


def main() -> None:
    user_ids = [f"user_{i:04d}" for i in range(1, USER_COUNT + 1)]
    expected = len(user_ids) * sum(CHECKOUTS_PER_USER) // 2
    print(
        f"{len(user_ids)} users x {CHECKOUTS_PER_USER[0]}-{CHECKOUTS_PER_USER[1]} "
        f"checkouts (about {expected}) -> {CHECKOUT_URL}"
    )
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(_user_job, user_id) for user_id in user_ids]
        pending = {f for f in futures}
        while pending:
            done_now = {f for f in pending if f.done()}
            pending -= done_now
            with _lock:
                done = _done
            print(f"  {done} checkouts done...", flush=True)
            if not pending:
                break
            time.sleep(2.0)
        for f in futures:
            f.result() # re-raise any unexpected failure
    print(f"finished in {time.monotonic() - started:.0f}s")
    _print_summary()


if __name__ == "__main__":
    main()
