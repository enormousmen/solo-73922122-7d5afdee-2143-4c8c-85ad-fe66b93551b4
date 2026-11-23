"""Probing engine: binary-search the largest UDP payload that passes the tunnel.

Rules implemented (per the operations spec):

* Every group sends one candidate-size packet plus one 64-byte control packet.
* A candidate size is recorded as *suspected oversize* only after timing out in
  two consecutive groups whose control packets both came back on time.
* Receipts are verified by probe number (seq) and received length.
* A late but valid receipt still confirms its size as long as it arrives
  before the run finishes; duplicate receipts are counted once.
* A suspected-oversize size must stay greater than every confirmed size.
* One run sends at most `budget` packets (hard cap 48) including controls.
* The run ends when the upper limit is confirmed reachable, or when
  min_suspected - max_confirmed <= 8 bytes, or when evidence runs out
  (budget exhausted / controls lost) — in the latter case the caller is
  told to retest.
"""
from __future__ import annotations

import select
import socket
import time
import uuid
from dataclasses import dataclass

from common.protocol import (
    CONTROL_SIZE,
    MAX_PACKET_SIZE,
    MIN_PACKET_SIZE,
    TYPE_RECEIPT,
    build_probe,
    parse_packet,
)

# Linux <linux/in.h>: PMTU discovery mode "do" — set DF on every IPv4 packet.
IP_MTU_DISCOVER = 10
IP_PMTUDISC_DO = 2

CONVERGENCE_GAP = 8          # stop when min_suspect - max_confirmed <= 8
MAX_INCONCLUSIVE_STREAK = 3  # abort when controls keep vanishing too
MAX_BUDGET = 48              # hard cap: packets per run, controls included


@dataclass
class SentPacket:
    group: int
    seq: int
    size: int
    kind: str  # "candidate" | "control"
    sent_at: float
    status: str = "pending"  # pending | confirmed | no_receipt
    rtt_ms: float | None = None


@dataclass
class GroupRecord:
    group: int
    candidate_size: int
    candidate_seq: int
    control_seq: int
    candidate_result: str = "pending"  # confirmed|timeout|suspected_oversize|inconclusive
    control_on_time: bool = False
    strikes_after: int = 0
    note: str = ""


class ProbeEngine:
    """One run probes a fixed receiver with DF-set IPv4 UDP packets."""

    def __init__(
        self,
        host: str,
        port: int,
        min_size: int = MIN_PACKET_SIZE,
        max_size: int = MAX_PACKET_SIZE,
        timeout_s: float = 0.5,
        budget: int = MAX_BUDGET,
        final_wait_s: float | None = None,
    ):
        if not MIN_PACKET_SIZE <= min_size < max_size <= MAX_PACKET_SIZE:
            raise ValueError(
                f"need {MIN_PACKET_SIZE} <= min_size < max_size <= {MAX_PACKET_SIZE}"
            )
        if not 2 <= budget <= MAX_BUDGET:
            raise ValueError(f"budget must be within 2..{MAX_BUDGET} packets")
        self.host = host
        self.port = port
        self.min_size = min_size
        self.max_size = max_size
        self.timeout_s = timeout_s
        self.budget = budget
        self.final_wait_s = timeout_s if final_wait_s is None else final_wait_s

        self.run_id = uuid.uuid4().bytes[:8]
        self.started_at = time.monotonic()
        self.sock: socket.socket | None = None
        self.df_set = False

        self.sent: dict[int, SentPacket] = {}
        self.groups: list[GroupRecord] = []
        self.confirmed: set[int] = set()
        self.suspects: set[int] = set()
        self.strikes: dict[int, int] = {}
        self.retry_size: int | None = None
        self.group_no = 0
        self.seq = 0
        self.inconclusive_streak = 0
        self.stop_reason = "running"
        self.valid_receipts = 0
        self.duplicate_receipts = 0
        self.invalid_receipts = 0

    # ------------------------------------------------------------------ loop
    def run(self) -> dict:
        self._open_socket()
        try:
            while not self._finished():
                candidate = self._next_candidate()
                if candidate is None:
                    self.stop_reason = "no_candidate"
                    break
                if len(self.sent) + 2 > self.budget:
                    self.stop_reason = "budget_exhausted"
                    break
                self._run_group(candidate)
            # Late receipts that arrive before the probe ends still count.
            self._collect_until(time.monotonic() + self.final_wait_s)
            self._finalize()
            return self._report()
        finally:
            self.sock.close()

    def _finished(self) -> bool:
        mc, ms = self._max_confirmed(), self._min_suspect()
        if ms is None and mc is not None and mc >= self.max_size:
            self.stop_reason = "upper_limit_confirmed"
            return True
        if mc is not None and ms is not None and ms - mc <= CONVERGENCE_GAP:
            self.stop_reason = "converged"
            return True
        if self.inconclusive_streak >= MAX_INCONCLUSIVE_STREAK:
            self.stop_reason = "controls_lost"
            return True
        return False

    def _next_candidate(self) -> int | None:
        if self.retry_size is not None:
            if self.retry_size not in self.confirmed:
                return self.retry_size
            self.retry_size = None  # confirmed by a late receipt meanwhile
        mc, ms = self._max_confirmed(), self._min_suspect()
        if ms is None:
            return self.max_size  # nothing suspected yet: try the top first
        low = max(mc if mc is not None else self.min_size, self.min_size)
        if ms - low <= CONVERGENCE_GAP:
            return None
        return low + (ms - low) // 2

    # ----------------------------------------------------------------- group
    def _run_group(self, candidate: int) -> None:
        self.group_no += 1
        record = GroupRecord(
            group=self.group_no,
            candidate_size=candidate,
            candidate_seq=self._send(candidate, "candidate"),
            control_seq=self._send(CONTROL_SIZE, "control"),
        )
        self._collect_until(time.monotonic() + self.timeout_s)

        cand = self.sent[record.candidate_seq]
        ctrl = self.sent[record.control_seq]
        record.control_on_time = ctrl.status == "confirmed"

        if candidate in self.confirmed:
            # confirmed by this packet's receipt or a late earlier one
            record.candidate_result = "confirmed"
            if cand.status != "confirmed":
                record.note = "confirmed by a late receipt for an earlier packet"
            self.retry_size = None
            self.inconclusive_streak = 0
        elif record.control_on_time:
            # candidate timed out while the control came back on time: a strike
            self.strikes[candidate] = self.strikes.get(candidate, 0) + 1
            record.strikes_after = self.strikes[candidate]
            self.inconclusive_streak = 0
            if self.strikes[candidate] >= 2:
                mc = self._max_confirmed()
                if mc is not None and candidate > mc:
                    self.suspects.add(candidate)
                    record.candidate_result = "suspected_oversize"
                    record.note = "timed out in 2 consecutive groups, controls on time"
                else:
                    record.candidate_result = "timeout"
                    record.note = "not recorded: not above confirmed sizes"
                self.retry_size = None
            else:
                record.candidate_result = "timeout"
                self.retry_size = candidate  # same candidate again next group
        else:
            # control missing too: whole-group loss, must not count
            record.candidate_result = "inconclusive"
            record.note = "control also missing; group not counted"
            self.strikes[candidate] = 0
            self.retry_size = candidate
            self.inconclusive_streak += 1
        self.groups.append(record)

    # ---------------------------------------------------------------- socket
    def _open_socket(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, IP_MTU_DISCOVER, IP_PMTUDISC_DO)
            self.df_set = True
        except OSError:
            self.df_set = False
        self.sock.bind(("0.0.0.0", 0))
        self.sock.setblocking(False)

    def _send(self, size: int, kind: str) -> int:
        self.seq += 1
        packet = build_probe(
            self.run_id, self.group_no, self.seq, size, control=(kind == "control")
        )
        self.sock.sendto(packet, (self.host, self.port))
        self.sent[self.seq] = SentPacket(
            group=self.group_no, seq=self.seq, size=size, kind=kind,
            sent_at=time.monotonic(),
        )
        return self.seq

    def _collect_until(self, deadline: float) -> None:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            ready, _, _ = select.select([self.sock], [], [], remaining)
            if not ready:
                return
            try:
                data, _addr = self.sock.recvfrom(65535)
            except BlockingIOError:
                continue
            self._handle_receipt(data, time.monotonic())

    def _handle_receipt(self, data: bytes, now: float) -> None:
        pkt = parse_packet(data)
        if pkt is None or pkt.ptype != TYPE_RECEIPT or pkt.run_id != self.run_id:
            self.invalid_receipts += 1
            return
        rec = self.sent.get(pkt.seq)
        if rec is None:
            self.invalid_receipts += 1  # unknown probe number
            return
        if pkt.value != rec.size:
            self.invalid_receipts += 1  # received length mismatch
            return
        if rec.status == "confirmed":
            self.duplicate_receipts += 1  # duplicates count once
            return
        rec.status = "confirmed"
        rec.rtt_ms = round((now - rec.sent_at) * 1000.0, 3)
        self.valid_receipts += 1
        self.confirmed.add(rec.size)
        self.strikes.pop(rec.size, None)  # a confirmed size carries no strikes
        # invariant: suspected oversize must stay above every confirmed size
        mc = max(self.confirmed)
        self.suspects = {s for s in self.suspects if s > mc}

    # ---------------------------------------------------------------- report
    def _max_confirmed(self) -> int | None:
        return max(self.confirmed) if self.confirmed else None

    def _min_suspect(self) -> int | None:
        return min(self.suspects) if self.suspects else None

    def _finalize(self) -> None:
        for rec in self.sent.values():
            if rec.status == "pending":
                rec.status = "no_receipt"
        mc = self._max_confirmed()
        if mc is not None:
            self.suspects = {s for s in self.suspects if s > mc}

    def _verdict(self, mc: int | None, ms: int | None, gap: int | None):
        if ms is None and mc is not None and mc >= self.max_size:
            return "ok", f"探测上限 {self.max_size} 字节已确认可达"
        if gap is not None and gap <= CONVERGENCE_GAP:
            return (
                "ok",
                f"边界已收敛：最大已确认 {mc} 字节，最小疑似超长 {ms} 字节，相差 {gap} 字节",
            )
        if mc is None:
            reason = "没有任何有效回执（含 64 字节对照），到接收端的路径可能不通"
        elif ms is None:
            reason = f"仅确认到 {mc} 字节，未定位疑似超长点"
        else:
            reason = f"边界未收敛（已确认 {mc}，疑似超长 {ms}，相差 {gap} 字节）"
        return "retest", f"证据不足，请重测：{reason}（停止原因：{self.stop_reason}）"

    def _report(self) -> dict:
        mc, ms = self._max_confirmed(), self._min_suspect()
        gap = ms - mc if (mc is not None and ms is not None) else None
        verdict, message = self._verdict(mc, ms, gap)
        return {
            "run_id": self.run_id.hex(),
            "target": f"{self.host}:{self.port}",
            "df_set": self.df_set,
            "search_range": [self.min_size, self.max_size],
            "verdict": verdict,
            "message": message,
            "max_confirmed_size": mc,
            "min_suspected_oversize": ms,
            "boundary_gap": gap,
            "stop_reason": self.stop_reason,
            "packets_sent": len(self.sent),
            "budget": self.budget,
            "receipts": {
                "valid": self.valid_receipts,
                "duplicates": self.duplicate_receipts,
                "invalid": self.invalid_receipts,
            },
            "groups": [
                {
                    "group": g.group,
                    "candidate_size": g.candidate_size,
                    "candidate_seq": g.candidate_seq,
                    "control_seq": g.control_seq,
                    "candidate_result": g.candidate_result,
                    "control_on_time": g.control_on_time,
                    "strikes_after": g.strikes_after,
                    "note": g.note,
                }
                for g in self.groups
            ],
            "packets": [
                {
                    "group": p.group,
                    "seq": p.seq,
                    "size": p.size,
                    "kind": p.kind,
                    "status": p.status,
                    "sent_at_ms": round((p.sent_at - self.started_at) * 1000.0, 3),
                    "rtt_ms": p.rtt_ms,
                }
                for p in self.sent.values()
            ],
        }
