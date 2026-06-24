#!/usr/bin/env python3
"""CSU.py - Caribou System Unit main orchestrator.

Spawns and supervises the worker threads that make up the System Unit:
  - MAVLink.py:   ArduPilot telemetry ingestion via MAVSDK (UDP from FC)
  - Hobbywing.py: Per-arm ESC ingestion via DroneCAN (UAVCAN esc.Status
                   on `can0`)
  - TattuBMS.py:  Per-arm BMS ingestion via DroneCAN (UAVCAN BatteryInfo
                   on `can1`)
  - HubLink.py:   WebSocket server (`caribou.stream.v1`) on :8765 that Hubs
                   dial into over Tailscale

Shared state lives in Data.py; Data.tlock protects reads/writes between
threads. Configuration is taken from environment variables (see each
worker for the full list); the process is intended to run under systemd.
SIGTERM/SIGINT trigger a clean shutdown.
"""

from __future__ import annotations

import logging
import os
import signal
import threading

import Data
import Hobbywing
import HubLink
import MAVLink
import TattuBMS

log = logging.getLogger("CSU")


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    drone_id = os.environ.get("DRONE_ID", "caribou_001")
    log.info("Caribou System Unit starting (drone_id=%s)", drone_id)

    data = Data.Data()

    mavlink = MAVLink.MAVLink(data)
    hobbywing = Hobbywing.Hobbywing(data)
    tattu_bms = TattuBMS.TattuBMS(data)
    hublink = HubLink.HubLink(data)

    stop = threading.Event()

    def _shutdown(signum, _frame):
        log.info("Signal %s received; shutting down", signum)
        stop.set()
        hobbywing.stop()
        tattu_bms.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    workers = [
        threading.Thread(target=mavlink.run, name="MAVLink", daemon=True),
        threading.Thread(target=hobbywing.run, name="Hobbywing", daemon=True),
        threading.Thread(target=tattu_bms.run, name="TattuBMS", daemon=True),
        threading.Thread(target=hublink.run, name="HubLink", daemon=True),
    ]
    for t in workers:
        t.start()
    log.info("Workers started: %s", [t.name for t in workers])

    # Park the main thread until a signal arrives. Workers run their own
    # asyncio loops; daemon=True means they exit with the process.
    while not stop.is_set():
        stop.wait(1.0)

    log.info("Caribou System Unit shutdown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
