"""TattuBMS.py - Per-arm BMS ingestion for Caribou System Unit.

Listens on the BMS CAN bus (default `can1`) for UAVCAN/DroneCAN
`uavcan.equipment.power.BatteryInfo` broadcasts and routes them by source
node ID to the corresponding arm (1..6). Each arm's latest snapshot lands
in `Data.BMSArms`; HubLink reads from there and serves the bms blocks
in spec §4 `arms[]`.

Modelled on the Hub-side `telemetry_forwarder.py` UAVCAN worker so the
two implementations stay aligned: same `dronecan` library, same callback
shape, same node-ID-to-arm mapping pattern.

The module name is `TattuBMS` (matches the architecture doc commitment),
but the implementation is generic UAVCAN BatteryInfo — it works with any
DroneCAN battery that broadcasts the standard message, including Tattu
Plus packs once their interface is confirmed.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone

import dronecan
from dronecan.driver.socketcan import SocketCAN

log = logging.getLogger("TattuBMS")


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# Default UAVCAN node-id-to-arm mapping. Mirrors the Hub forwarder's
# ARM_BMS_NODE_IDS so a drone provisioned for one stack works with the
# other. Override per-deployment via the BMS_NODE_IDS env var, e.g.
#   BMS_NODE_IDS="10:1,11:2,12:3,13:4,14:5,15:6"
DEFAULT_BMS_NODE_IDS = {
    10: 1,   # node 10 -> arm 1 (top-left)
    11: 2,   # node 11 -> arm 2 (top-right)
    12: 3,   # node 12 -> arm 3 (right)
    13: 4,   # node 13 -> arm 4 (bottom-right)
    14: 5,   # node 14 -> arm 5 (bottom-left)
    15: 6,   # node 15 -> arm 6 (left)
}


def _parse_node_id_map(spec: str) -> dict[int, int]:
    """Parse 'node:arm,node:arm,...' env strings into a mapping dict."""
    mapping: dict[int, int] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        node_s, _, arm_s = chunk.partition(":")
        mapping[int(node_s)] = int(arm_s)
    return mapping


class TattuBMS:
    """DroneCAN BatteryInfo listener that populates `Data.BMSArms`."""

    def __init__(
        self,
        data,
        *,
        can_interface: str | None = None,
        bridge_node_id: int | None = None,
        bms_node_ids: dict[int, int] | None = None,
    ) -> None:
        self.data = data
        self.can_interface = (
            can_interface if can_interface is not None
            else os.environ.get("BMS_CAN_INTERFACE", "can1")
        )
        self.bridge_node_id = int(
            bridge_node_id if bridge_node_id is not None
            else os.environ.get("BMS_BRIDGE_NODE_ID", "112")
        )

        # Resolve mapping: explicit kwarg > env override > built-in default.
        env_map = os.environ.get("BMS_NODE_IDS")
        if bms_node_ids is not None:
            self.bms_node_ids = dict(bms_node_ids)
        elif env_map:
            try:
                self.bms_node_ids = _parse_node_id_map(env_map)
            except ValueError:
                log.exception("Failed to parse BMS_NODE_IDS=%r; using defaults", env_map)
                self.bms_node_ids = dict(DEFAULT_BMS_NODE_IDS)
        else:
            self.bms_node_ids = dict(DEFAULT_BMS_NODE_IDS)

        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        """Blocking entry point — spins the DroneCAN node until `stop()`."""
        log.info(
            "TattuBMS opening %s as bridge node %d (mapping: %s)",
            self.can_interface, self.bridge_node_id, self.bms_node_ids,
        )

        try:
            driver = SocketCAN(self.can_interface)
        except Exception:
            log.exception("Failed to open SocketCAN(%s) — BMS worker exiting", self.can_interface)
            return

        node = dronecan.node.Node(driver, node_id=self.bridge_node_id)
        info = dronecan.uavcan.protocol.GetNodeInfo.Response()
        info.name = "caribou_system_unit_bms"
        info.software_version.major = 1
        info.software_version.minor = 0
        node.node_info = info

        node.add_handler(
            dronecan.uavcan.equipment.power.BatteryInfo,
            self._battery_callback,
        )

        log.info("TattuBMS ready on %s, waiting for BatteryInfo frames", self.can_interface)

        while not self._stop.is_set():
            try:
                node.spin(timeout=0.1)
            except Exception:
                # Don't kill the worker on transient parse/spin errors —
                # log and keep spinning. systemd Restart=always covers fatal cases.
                log.exception("UAVCAN spin error")

    def _battery_callback(self, event) -> None:
        msg = event.message
        source_node_id = event.transfer.source_node_id
        arm_id = self.bms_node_ids.get(source_node_id)
        if arm_id is None:
            log.debug("Ignoring BatteryInfo from unmapped node %d", source_node_id)
            return

        # UAVCAN BatteryInfo reports temperature in Kelvin. Convert to Celsius
        # for the spec; pass through 0 if the field is unpopulated/zero.
        temp_k = float(msg.temperature)
        temp_c = (temp_k - 273.15) if temp_k > 0 else 0.0

        entry = {
            "voltage_v": float(msg.voltage),
            "current_a": float(msg.current),
            "temperature_k": temp_k,
            "temperature_c": round(temp_c, 1),
            "soc_pct": float(msg.state_of_charge_pct),
            "soh_pct": float(msg.state_of_health_pct),
            "battery_id": int(getattr(msg, "battery_id", arm_id)),
            "timestamp": _now_iso(),
        }

        with self.data.tlock:
            self.data.BMSArms[arm_id] = entry


if __name__ == "__main__":
    # Standalone smoke test: opens the CAN bus and dumps every recognised
    # BatteryInfo frame to the log. Useful for verifying wiring before
    # bringing up the full CSU process.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    class _StubData:
        def __init__(self):
            self.tlock = threading.Lock()
            self.BMSArms = {}

    data = _StubData()

    def _printer():
        import time
        while True:
            time.sleep(1.0)
            with data.tlock:
                snap = {k: {**v, "timestamp": v.get("timestamp")[:23] if v.get("timestamp") else None}
                        for k, v in data.BMSArms.items()}
            log.info("BMSArms: %s", snap)

    threading.Thread(target=_printer, daemon=True).start()
    TattuBMS(data).run()
