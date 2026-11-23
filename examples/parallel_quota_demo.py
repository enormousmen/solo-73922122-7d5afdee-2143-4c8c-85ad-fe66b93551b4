#!/usr/bin/env python3
"""Demo: one /probe round is still awaiting receipts while a parallel round
sends using the bytes the first one just released.

Run against the docker-compose stack (or any local API instance):

    python examples/parallel_quota_demo.py http://localhost:8000

It fires two /probe requests in parallel against a process whose in-flight
quota is exactly one largest group (1536 bytes = 1472 candidate + 64 control),
and samples GET /quota while both are in flight:

    t0        run A reserves 1536B, sends group 1, then waits for receipts
    t0+0.15s  snapshot: inflight=1536 (A's round is waiting for receipts)
    t0+0.15s  run B tries to reserve -> blocked, snapshot shows a waiter
    t0+~0.4s  A's receipts arrive -> A releases 1536B -> B acquires and sends
    t0+end    both runs finish, inflight back to 0

Tip: point the stack at a slow receiver so the overlap is easy to see:

    RECEIPT_DELAY_MS=350 PROBE_INFLIGHT_QUOTA_BYTES=1536 \
        docker compose up --build
"""
from __future__ import annotations

import sys
import threading
import time

import httpx


def snapshot(client: httpx.Client) -> dict:
    return client.get("/quota").json()


def main(base_url: str) -> int:
    results: dict[str, dict] = {}

    def call(name: str, start_delay: float) -> None:
        time.sleep(start_delay)
        resp = httpx.post(
            f"{base_url}/probe",
            json={"timeout_ms": 1000, "final_wait_ms": 300},
            timeout=30,
        )
        resp.raise_for_status()
        results[name] = resp.json()

    with httpx.Client(base_url=base_url) as client:
        print(f"[init] {client.get('/healthz').json()}")
        print(f"[init] quota {snapshot(client)}")

        t0 = time.monotonic()
        ta = threading.Thread(target=call, args=("A", 0.0))
        tb = threading.Thread(target=call, args=("B", 0.15))
        ta.start()
        tb.start()

        time.sleep(0.25)  # A holds the full quota; B is queued behind it
        mid1 = snapshot(client)
        print(f"[t+{time.monotonic() - t0:.2f}s] A waiting for receipts: {mid1}")

        time.sleep(0.25)  # A's receipts arrive, bytes freed, B sends
        mid2 = snapshot(client)
        print(f"[t+{time.monotonic() - t0:.2f}s] B sends on released bytes: {mid2}")

        ta.join(timeout=30)
        tb.join(timeout=30)
        end = snapshot(client)
        print(f"[t+{time.monotonic() - t0:.2f}s] both done: {end}")

    a, b = results["A"], results["B"]
    print("\n--- run A (held the quota first) ---")
    print(
        f"verdict={a['verdict']} groups={len(a['groups'])} "
        f"packets={a['packets_sent']} max_confirmed={a['max_confirmed_size']}"
    )
    print("--- run B (queued, then used A's released bytes) ---")
    g0 = b["groups"][0]
    print(
        f"verdict={b['verdict']} groups={len(b['groups'])} "
        f"packets={b['packets_sent']} max_confirmed={b['max_confirmed_size']}"
    )
    print(
        f"B group1: candidate_result={g0['candidate_result']} "
        f"control_on_time={g0['control_on_time']} "
        f"quota_wait_ms={g0['quota_wait_ms']} "
        f"(blocked for quota, but that time was NOT charged to its receipt timeout)"
    )

    ok = (
        a["verdict"] == "ok"
        and b["verdict"] == "ok"
        and g0["quota_wait_ms"] > 0
        and g0["candidate_result"] == "confirmed"
        and end["inflight_bytes"] == 0
    )
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    raise SystemExit(main(url.rstrip("/")))
