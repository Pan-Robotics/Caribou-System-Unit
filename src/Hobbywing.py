"""Hobbywing.py - Per-arm ESC telemetry ingestion for Caribou System Unit.

Listens on the ESC CAN bus (default `can0`) for UAVCAN/DroneCAN
`uavcan.equipment.esc.Status` broadcasts from the 6x Hobbywing XRotor
X15 ESCs and routes them by source node ID to arms 1..6. Each arm's
latest snapshot lands in `Data.ESCArms`; HubLink reads from there and
serves the `esc` blocks in spec §4 `arms[]`.

Modelled directly on `TattuBMS.py` — same dronecan library, same
SocketCAN driver, same routing pattern. The X15 supports both HWCAN
(proprietary) and DroneCAN; this module assumes the ESCs are configured
in DroneCAN mode via the Hobbywing DataLink tool. If a fleet ends up
using HWCAN instead, swap the handler for whatever messages HWCAN
broadcasts and keep `Data.ESCArms` identical.

The X15 product page states: "HWCAN and DroneCAN dual protocols,
ensuring communication compatibility with multiple mainstream flight
controllers on the market."
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone

import dronecan
from dronecan.driver.socketcan import SocketCAN

log = logging.getLogger("Hobbywing")


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# Default UAVCAN node-id-to-arm mapping. Mirrors the Hub forwarder's
# ARM_ESC_NODE_IDS so a drone provisioned for one stack works with the
# other. Override per-deployment via the ESC_NODE_IDS env var, e.g.
#   ESC_NODE_IDS="20:1,21:2,22:3,23:4,24:5,25:6"
DEFAULT_ESC_NODE_IDS = {
    20: 1,   # node 20 -> arm 1 (top-left)
    21: 2,   # node 21 -> arm 2 (top-right)
    22: 3,   # node 22 -> arm 3 (right)
    23: 4,   # node 23 -> arm 4 (bottom-right)
    24: 5,   # node 24 -> arm 5 (bottom-left)
    25: 6,   # node 25 -> arm 6 (left)
}


def _parse_node_id_map(spec: str) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        node_s, _, arm_s = chunk.partition(":")
        mapping[int(node_s)] = int(arm_s)
    return mapping


class Hobbywing:
    """DroneCAN ESC Status listener that populates `Data.ESCArms`."""

    def __init__(
        self,
        data,
        *,
        can_interface: str | None = None,
        bridge_node_id: int | None = None,
        esc_node_ids: dict[int, int] | None = None,
    ) -> None:
        self.data = data
        self.can_interface = (
            can_interface if can_interface is not None
            else os.environ.get("ESC_CAN_INTERFACE", "can0")
        )
        # Distinct from the BMS bridge (112) so both UAVCAN bridges can coexist.
        self.bridge_node_id = int(
            bridge_node_id if bridge_node_id is not None
            else os.environ.get("ESC_BRIDGE_NODE_ID", "113")
        )

        env_map = os.environ.get("ESC_NODE_IDS")
        if esc_node_ids is not None:
            self.esc_node_ids = dict(esc_node_ids)
        elif env_map:
            try:
                self.esc_node_ids = _parse_node_id_map(env_map)
            except ValueError:
                log.exception("Failed to parse ESC_NODE_IDS=%r; using defaults", env_map)
                self.esc_node_ids = dict(DEFAULT_ESC_NODE_IDS)
        else:
            self.esc_node_ids = dict(DEFAULT_ESC_NODE_IDS)

        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        """Blocking entry point - spins the DroneCAN node until `stop()`."""
        log.info(
            "Hobbywing opening %s as bridge node %d (mapping: %s)",
            self.can_interface, self.bridge_node_id, self.esc_node_ids,
        )

        try:
            driver = SocketCAN(self.can_interface)
        except Exception:
            log.exception("Failed to open SocketCAN(%s) - ESC worker exiting", self.can_interface)
            return

        node = dronecan.node.Node(driver, node_id=self.bridge_node_id)
        info = dronecan.uavcan.protocol.GetNodeInfo.Response()
        info.name = "caribou_system_unit_esc"
        info.software_version.major = 1
        info.software_version.minor = 0
        node.node_info = info

        node.add_handler(
            dronecan.uavcan.equipment.esc.Status,
            self._esc_callback,
        )

        log.info("Hobbywing ready on %s, waiting for ESC Status frames", self.can_interface)

        while not self._stop.is_set():
            try:
                node.spin(timeout=0.1)
            except Exception:
                log.exception("UAVCAN spin error")

    def _esc_callback(self, event) -> None:
        msg = event.message
        source_node_id = event.transfer.source_node_id
        arm_id = self.esc_node_ids.get(source_node_id)
        if arm_id is None:
            log.debug("Ignoring ESC Status from unmapped node %d", source_node_id)
            return

        # UAVCAN esc.Status reports temperatures in Kelvin. Convert to Celsius;
        # `temperature` is the ESC board, `motor_temperature` is the winding
        # (separate thermistor channel on the X15). Either may be 0/uninit.
        esc_temp_k = float(getattr(msg, "temperature", 0.0))
        motor_temp_k = float(getattr(msg, "motor_temperature", 0.0))
        esc_temp_c = (esc_temp_k - 273.15) if esc_temp_k > 0 else 0.0
        motor_temp_c = (motor_temp_k - 273.15) if motor_temp_k > 0 else 0.0

        entry = {
            "rpm": int(getattr(msg, "rpm", 0)),
            "voltage_v": float(getattr(msg, "voltage", 0.0)),
            "current_a": float(getattr(msg, "current", 0.0)),
            "temperature_c": round(esc_temp_c, 1),
            "motor_temperature_c": round(motor_temp_c, 1),
            "power_rating_pct": int(getattr(msg, "power_rating_pct", 0)),
            "error_count": int(getattr(msg, "error_count", 0)),
            "timestamp": _now_iso(),
        }

        with self.data.tlock:
            self.data.ESCArms[arm_id] = entry


if __name__ == "__main__":
    # Standalone smoke test: opens the CAN bus and dumps every recognised
    # ESC Status frame to the log.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    class _StubData:
        def __init__(self):
            self.tlock = threading.Lock()
            self.ESCArms = {}

    data = _StubData()

    def _printer():
        import time
        while True:
            time.sleep(1.0)
            with data.tlock:
                snap = dict(data.ESCArms)
            log.info("ESCArms: %s", snap)

    threading.Thread(target=_printer, daemon=True).start()
    Hobbywing(data).run()
