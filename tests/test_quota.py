"""Unit tests for the process-wide in-flight byte quota."""
from __future__ import annotations

import threading
import time

import pytest

from prober.app.quota import (
    DEFAULT_QUOTA_BYTES,
    MIN_QUOTA_BYTES,
    QUOTA_ENV_VAR,
    ByteQuota,
    env_quota_limit,
)

GROUP_LARGE = [(1472, "candidate"), (64, "control")]  # 1536B = minimum quota
GROUP_SMALL = [(200, "candidate"), (64, "control")]   # 264B


def make_quota(limit: int) -> ByteQuota:
    return ByteQuota(limit, log=lambda msg: None)


def test_env_quota_default_and_minimum(monkeypatch):
    monkeypatch.delenv(QUOTA_ENV_VAR, raising=False)
    assert env_quota_limit() == DEFAULT_QUOTA_BYTES
    monkeypatch.setenv(QUOTA_ENV_VAR, str(MIN_QUOTA_BYTES))
    assert env_quota_limit() == MIN_QUOTA_BYTES


def test_env_quota_rejects_bad_values(monkeypatch):
    monkeypatch.setenv(QUOTA_ENV_VAR, str(MIN_QUOTA_BYTES - 1))
    with pytest.raises(ValueError, match="minimum"):
        env_quota_limit()
    monkeypatch.setenv(QUOTA_ENV_VAR, "not-a-number")
    with pytest.raises(ValueError, match="not an integer"):
        env_quota_limit()


def test_quota_cannot_be_built_below_minimum():
    with pytest.raises(ValueError):
        make_quota(MIN_QUOTA_BYTES - 1)


def test_acquire_reserves_and_release_returns_bytes_once():
    quota = make_quota(2000)
    leases, waited = quota.acquire_group("run1", 1, GROUP_LARGE)
    assert waited >= 0.0 and waited < 0.05  # first reservation does not block
    snap = quota.snapshot()
    assert snap["inflight_bytes"] == 1536
    assert snap["available_bytes"] == 464
    assert snap["peak_inflight_bytes"] == 1536

    assert leases[0].release("receipt") is True
    assert leases[0].release("receipt") is False  # idempotent: returned once
    assert leases[1].release("group_timeout") is True
    assert leases[1].release("group_timeout") is False

    snap = quota.snapshot()
    assert snap["inflight_bytes"] == 0
    assert snap["available_bytes"] == 2000
    assert quota.released_bytes_by_reason["receipt"] == 1472
    assert quota.released_bytes_by_reason["group_timeout"] == 64


def test_group_blocks_until_bytes_are_freed():
    quota = make_quota(MIN_QUOTA_BYTES)
    leases, _ = quota.acquire_group("runA", 1, GROUP_LARGE)
    assert quota.snapshot()["inflight_bytes"] == 1536

    acquired = threading.Event()

    def waiter():
        got, _waited = quota.acquire_group("runB", 1, GROUP_SMALL)
        for lease in got:
            lease.release("receipt")
        acquired.set()

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.1)
    assert not acquired.is_set()  # 1536 + 264 > 1536: must be waiting
    for lease in leases:
        lease.release("receipt")
    t.join(timeout=2)
    assert acquired.is_set()
    assert quota.snapshot()["inflight_bytes"] == 0


def test_peak_inflight_never_exceeds_limit_under_contention():
    quota = make_quota(MIN_QUOTA_BYTES)
    barrier = threading.Barrier(4)
    errors = []

    def worker():
        try:
            barrier.wait()
            for _ in range(20):
                got, _ = quota.acquire_group("run", 1, GROUP_SMALL)
                assert quota.snapshot()["inflight_bytes"] <= MIN_QUOTA_BYTES
                time.sleep(0.002)
                for lease in got:
                    lease.release("receipt")
        except BaseException as exc:  # pragma: no cover - test failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors
    snap = quota.snapshot()
    assert snap["inflight_bytes"] == 0
    assert snap["peak_inflight_bytes"] <= MIN_QUOTA_BYTES
    assert snap["peak_inflight_bytes"] >= 264  # something actually flew
