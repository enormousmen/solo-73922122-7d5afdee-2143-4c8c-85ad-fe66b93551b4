#!/usr/bin/env python3
"""Fixed UDP receiver / test endpoint for the PMTU prober.

For every valid probe packet it sends back a short receipt carrying the
probe number (seq) and the length it actually received — unless a drop
rule matches. Drop rules are drill knobs set via environment:

    PORT              UDP listen port (default 5055)
    DROP_THRESHOLD    drop probe packets whose UDP payload is larger (0=off)
    DROP_GROUPS       comma/space separated group numbers dropped entirely
                      (simulates whole-group loss: candidate AND control)
    DUP_RECEIPTS      "1" = send every receipt twice (duplicate drill)
    RECEIPT_DELAY_MS  delay every receipt by N ms (late-receipt drill)

Every decision is logged to stdout (visible via `docker compose logs`).
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time

from common.protocol import TYPE_PROBE, build_receipt, parse_packet

PORT = int(os.environ.get("PORT", "5055"))
DROP_THRESHOLD = int(os.environ.get("DROP_THRESHOLD", "0") or "0")
DROP_GROUPS = {
    int(x)
    for x in os.environ.get("DROP_GROUPS", "").replace(",", " ").split()
    if x.strip()
}
DUP_RECEIPTS = os.environ.get("DUP_RECEIPTS", "0") == "1"
RECEIPT_DELAY_MS = int(os.environ.get("RECEIPT_DELAY_MS", "0") or "0")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stdout, flush=True)


def main() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PORT))
    log(
        f"receiver on 0.0.0.0:{PORT}/udp "
        f"drop_threshold={DROP_THRESHOLD or 'off'} "
        f"drop_groups={sorted(DROP_GROUPS) or 'off'} "
        f"dup_receipts={DUP_RECEIPTS} receipt_delay_ms={RECEIPT_DELAY_MS}"
    )
    while True:
        data, addr = sock.recvfrom(65535)
        pkt = parse_packet(data)
        if pkt is None or pkt.ptype != TYPE_PROBE:
            log(f"IGNORE invalid {len(data)}B from {addr[0]}:{addr[1]}")
            continue
        kind = "control" if pkt.is_control else "candidate"
        where = f"group={pkt.group} seq={pkt.seq} size={pkt.wire_len} ({kind})"
        if pkt.group in DROP_GROUPS:
            log(f"DROP {where} reason=whole-group-loss")
            continue
        if DROP_THRESHOLD and pkt.wire_len > DROP_THRESHOLD:
            log(f"DROP {where} reason=over-threshold>{DROP_THRESHOLD}")
            continue
        receipt = build_receipt(pkt.run_id, pkt.group, pkt.seq, pkt.wire_len)

        def send_receipt(packet=receipt, target=addr):
            copies = 2 if DUP_RECEIPTS else 1
            for _ in range(copies):
                try:
                    sock.sendto(packet, target)
                except OSError:
                    return

        if RECEIPT_DELAY_MS > 0:
            threading.Timer(RECEIPT_DELAY_MS / 1000.0, send_receipt).start()
        else:
            send_receipt()
        log(f"ACK  {where}")


if __name__ == "__main__":
    main()
