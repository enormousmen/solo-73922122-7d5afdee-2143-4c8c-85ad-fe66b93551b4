"""Process-wide in-flight byte budget for probes running over low-bandwidth tunnels.

When several ``POST /probe`` calls run in parallel, the service must cap the
total payload bytes of probe packets whose receipts it is still waiting for.
The cap is one process-wide :class:`ByteQuota`, configured with the
``PROBE_INFLIGHT_QUOTA_BYTES`` environment variable (minimum
:data:`MIN_QUOTA_BYTES` = 1536 bytes — exactly the largest possible group,
1472-byte candidate + 64-byte control, so a group always fits once the budget
drains).

Reservation / release rules (see the operations spec):

* A group reserves the candidate and the control packet **together**, before
  either byte hits the wire. Time blocked waiting for quota is *not* part of
  the receipt timeout: the engine anchors its timeout window after this call
  returns.
* Bytes are released per packet, at most once: on the packet's first valid
  receipt, or when its own group times out. A late but valid receipt stays
  valid evidence, but its quota has already been returned and is never
  returned a second time.
* If a packet fails to leave the socket, its unsent bytes are returned
  immediately; packets that did go out keep their reservation until a receipt
  or the group timeout, exactly as normal.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass

MIN_QUOTA_BYTES = 1536
DEFAULT_QUOTA_BYTES = 4096
QUOTA_ENV_VAR = "PROBE_INFLIGHT_QUOTA_BYTES"


def env_quota_limit() -> int:
    """Read the process quota from the environment.

    Defaults to :data:`DEFAULT_QUOTA_BYTES` when unset/empty. Raises
    ``ValueError`` on a non-integer or a value below the hard minimum so a
    bad configuration fails loudly at process startup.
    """
    raw = os.environ.get(QUOTA_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_QUOTA_BYTES
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{QUOTA_ENV_VAR}={raw!r} is not an integer byte budget"
        ) from exc
    if limit < MIN_QUOTA_BYTES:
        raise ValueError(
            f"{QUOTA_ENV_VAR}={limit} is below the minimum "
            f"{MIN_QUOTA_BYTES} bytes (largest group = 1472 + 64)"
        )
    return limit


def _stdout_log(msg: str) -> None:
    print(msg, file=sys.stdout, flush=True)


@dataclass
class QuotaLease:
    """Reservation of ``size`` bytes for one not-yet-receipted probe packet."""

    quota: "ByteQuota"
    size: int
    run_hex: str
    group: int
    kind: str  # "candidate" | "control"
    seq: int | None = None
    released: bool = False

    def release(self, reason: str) -> bool:
        """Return the bytes once. Returns False on every later call."""
        return self.quota._release(self, reason)


class ByteQuota:
    """Process-wide cap on bytes of probe packets awaiting receipts."""

    def __init__(self, limit: int, clock=time.monotonic, log=_stdout_log):
        if limit < MIN_QUOTA_BYTES:
            raise ValueError(
                f"quota limit {limit} is below the minimum {MIN_QUOTA_BYTES} bytes"
            )
        self.limit = limit
        self._clock = clock
        self._log = log or (lambda msg: None)
        self._cond = threading.Condition()
        self._inflight = 0
        self._peak = 0
        self._waiters = 0
        self._active_runs = 0
        self._total_wait_s = 0.0
        self.released_bytes_by_reason: dict[str, int] = {}
        self.release_calls = 0

    # ------------------------------------------------------------- reservation
    def acquire_group(
        self,
        run_hex: str,
        group: int,
        sizes: list[tuple[int, str]],
    ) -> tuple[list[QuotaLease], float]:
        """Reserve every packet of a group together, blocking until it fits.

        Returns the leases (same order as ``sizes``) and the seconds spent
        blocked waiting for quota — the caller excludes that time from the
        receipt-timeout window.
        """
        need = sum(size for size, _kind in sizes)
        started = self._clock()
        with self._cond:
            self._waiters += 1
            try:
                while self._inflight + need > self.limit:
                    self._cond.wait()
            except BaseException:
                self._waiters -= 1
                raise
            self._waiters -= 1
            waited = self._clock() - started
            self._total_wait_s += waited
            leases: list[QuotaLease] = []
            for size, kind in sizes:
                self._inflight += size
                leases.append(
                    QuotaLease(
                        quota=self, size=size, run_hex=run_hex,
                        group=group, kind=kind,
                    )
                )
            self._peak = max(self._peak, self._inflight)
        self._log(
            f"[quota] run={run_hex} group={group} reserve {need}B "
            f"(inflight {self._inflight}/{self.limit}, waited {waited * 1000:.1f}ms)"
        )
        return leases, waited

    def _release(self, lease: QuotaLease, reason: str) -> bool:
        with self._cond:
            if lease.released:
                return False  # each packet's bytes are returned only once
            lease.released = True
            self._inflight -= lease.size
            self.release_calls += 1
            self.released_bytes_by_reason[reason] = (
                self.released_bytes_by_reason.get(reason, 0) + lease.size
            )
            inflight = self._inflight
            self._cond.notify_all()  # wake groups waiting for freed bytes
        self._log(
            f"[quota] run={lease.run_hex} group={lease.group} "
            f"seq={lease.seq} release {lease.size}B reason={reason} "
            f"(inflight {inflight}/{self.limit})"
        )
        return True

    # -------------------------------------------------------------------- runs
    def run_enter(self) -> None:
        with self._cond:
            self._active_runs += 1

    def run_exit(self) -> None:
        with self._cond:
            self._active_runs = max(0, self._active_runs - 1)

    # ----------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        with self._cond:
            return {
                "limit_bytes": self.limit,
                "inflight_bytes": self._inflight,
                "available_bytes": self.limit - self._inflight,
                "peak_inflight_bytes": self._peak,
                "active_runs": self._active_runs,
                "waiters": self._waiters,
                "total_wait_for_quota_ms": round(self._total_wait_s * 1000.0, 3),
                "released_bytes_by_reason": dict(self.released_bytes_by_reason),
            }
