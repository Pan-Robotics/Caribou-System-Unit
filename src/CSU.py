#!/usr/bin/env python3
"""CSU.py - Caribou System Unit main orchestrator.

Spawns and supervises the worker threads that make up the System Unit:
  - MAVLink.py:  ArduPilot telemetry ingestion via MAVSDK (UDP from FC)
  - TattuBMS.py: Per-arm BMS ingestion via DroneCAN (UAVCAN BatteryInfo
                  on `can1`)
  - HubLink.py:  WebSocket server (`caribou.stream.v1`) on :8765 that Hubs
                  dial into over Tailscale

Shared state lives in Data.py; Data.tlock protects reads/writes between
threads. Configuration is taken from environment variables (see each
worker for the full list); the process is intended to run under systemd.
SIGTERM/SIGINT trigger a clean shutdown.

Hobbywing.py (ESC over `can0`) will slot in here as a fourth worker
thread once that module lands.
"""

from __future__ import annotations

import logging
import os
import signal
import threading

import Data
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

    # Data.__init__ inherits its (TCP, modeselect) signature from FCPC.
    # The TCP positional is unused on CSU (HubLink replaces it); "FUI"
    # selects the airborne code paths in legacy modules that still read it.
    data = Data.Data(None, "FUI")

    mavlink = MAVLink.MAVLink(data)
    tattu_bms = TattuBMS.TattuBMS(data)
    hublink = HubLink.HubLink(data)

    stop = threading.Event()

    def _shutdown(signum, _frame):
        log.info("Signal %s received; shutting down", signum)
        stop.set()
        tattu_bms.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    workers = [
        threading.Thread(target=mavlink.run, name="MAVLink", daemon=True),
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
