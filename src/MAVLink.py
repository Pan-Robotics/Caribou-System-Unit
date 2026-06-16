"""MAVLink.py - ArduPilot telemetry ingestion for Caribou System Unit.

Connects to the flight controller over UDP via MAVSDK and populates
`Data.MAVLinkPacket` with attitude, position, GPS, battery, velocity,
heading, flight mode, and in-air status. HubLink reads those blocks
and serves them to Caribou Hub operators.

Modelled on the Hub-side `telemetry_forwarder.py` collectors so the
two implementations stay aligned (same MAVSDK calls, same rates, same
field shapes). The drone side does not POST to the Hub - HubLink owns
the data plane on this end.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from mavsdk import System

log = logging.getLogger("MAVLink")


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class MAVLink:
    """Async MAVSDK client that populates `Data.MAVLinkPacket` from the FC."""

    def __init__(
        self,
        data,
        *,
        mavlink_url: str | None = None,
        update_rate_hz: float | None = None,
    ) -> None:
        self.data = data
        self.mavlink_url = (
            mavlink_url if mavlink_url is not None
            else os.environ.get("MAVLINK_URL", "udpin://0.0.0.0:14540")
        )
        self.update_rate_hz = float(
            update_rate_hz if update_rate_hz is not None
            else os.environ.get("UPDATE_RATE_HZ", "10")
        )

    async def start(self) -> None:
        log.info("MAVLink connecting to %s", self.mavlink_url)
        drone = System()
        await drone.connect(system_address=self.mavlink_url)

        async for state in drone.core.connection_state():
            if state.is_connected:
                log.info("MAVLink connected: %s", self.mavlink_url)
                break

        try:
            await drone.telemetry.set_rate_attitude_euler(self.update_rate_hz)
            await drone.telemetry.set_rate_position(self.update_rate_hz)
            await drone.telemetry.set_rate_velocity_ned(self.update_rate_hz)
            await drone.telemetry.set_rate_gps_info(2.0)
            await drone.telemetry.set_rate_battery(2.0)
        except Exception:
            log.exception("Failed to set telemetry rates; continuing with FC defaults")

        await asyncio.gather(
            self._collect("attitude", self._collect_attitude(drone)),
            self._collect("position", self._collect_position(drone)),
            self._collect("gps", self._collect_gps(drone)),
            self._collect("battery_fc", self._collect_battery(drone)),
            self._collect("in_air", self._collect_in_air(drone)),
            self._collect("flight_mode", self._collect_flight_mode(drone)),
            self._collect("velocity", self._collect_velocity(drone)),
            self._collect("heading", self._collect_heading(drone)),
        )

    def run(self) -> None:
        """Blocking entry point for a thread that owns the asyncio loop."""
        asyncio.run(self.start())

    def _write(self, key: str, value) -> None:
        with self.data.tlock:
            self.data.MAVLinkPacket[key] = value

    async def _collect(self, name: str, coro) -> None:
        """Run a collector; log and exit cleanly if its stream raises.

        Letting one stream die independently means a failure in (say) the
        GPS feed does not blank attitude/position. Systemd restarts the
        whole process if everything goes silent.
        """
        try:
            await coro
        except Exception:
            log.exception("MAVLink %s stream ended", name)

    async def _collect_attitude(self, drone) -> None:
        async for a in drone.telemetry.attitude_euler():
            self._write("attitude", {
                "roll_deg": a.roll_deg,
                "pitch_deg": a.pitch_deg,
                "yaw_deg": a.yaw_deg,
                "timestamp": _now_iso(),
            })

    async def _collect_position(self, drone) -> None:
        async for p in drone.telemetry.position():
            self._write("position", {
                "latitude_deg": p.latitude_deg,
                "longitude_deg": p.longitude_deg,
                "absolute_altitude_m": p.absolute_altitude_m,
                "relative_altitude_m": p.relative_altitude_m,
                "timestamp": _now_iso(),
            })

    async def _collect_gps(self, drone) -> None:
        async for g in drone.telemetry.gps_info():
            fix_type = g.fix_type
            # MAVSDK may surface fix_type as an enum or a raw int depending on version.
            fix_value = fix_type.value if hasattr(fix_type, "value") else int(fix_type)
            self._write("gps", {
                "num_satellites": g.num_satellites,
                "fix_type": fix_value,
                "timestamp": _now_iso(),
            })

    async def _collect_battery(self, drone) -> None:
        async for b in drone.telemetry.battery():
            remaining = b.remaining_percent
            # Older MAVSDK returns 0..1; newer returns 0..100.
            if remaining is not None and remaining <= 1.0:
                remaining = remaining * 100.0
            self._write("battery_fc", {
                "voltage_v": b.voltage_v,
                "remaining_percent": remaining,
                "timestamp": _now_iso(),
            })

    async def _collect_in_air(self, drone) -> None:
        async for in_air in drone.telemetry.in_air():
            self._write("in_air", bool(in_air))

    async def _collect_flight_mode(self, drone) -> None:
        async for mode in drone.telemetry.flight_mode():
            self._write("flight_mode", str(mode).replace("FlightMode.", ""))

    async def _collect_velocity(self, drone) -> None:
        async for v in drone.telemetry.velocity_ned():
            # NED down is negative when climbing; flip so positive = up.
            vz_climb = -v.down_m_s
            ground_speed = (v.north_m_s ** 2 + v.east_m_s ** 2) ** 0.5
            with self.data.tlock:
                self.data.MAVLinkPacket["vertical_speed_ms"] = round(vz_climb, 2)
                self.data.MAVLinkPacket["airspeed_ms"] = round(ground_speed, 2)

    async def _collect_heading(self, drone) -> None:
        async for h in drone.telemetry.heading():
            self._write("heading_deg", h.heading_deg)


if __name__ == "__main__":
    # Standalone smoke test: connects to whatever FC / SITL is on MAVLINK_URL
    # and dumps Data.MAVLinkPacket every second to the log.
    import threading

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    class _StubData:
        def __init__(self):
            self.tlock = threading.Lock()
            self.MAVLinkPacket = {
                "attitude": None, "position": None, "gps": None,
                "battery_fc": None, "in_air": None, "flight_mode": None,
                "airspeed_ms": None, "vertical_speed_ms": None, "heading_deg": None,
            }

    data = _StubData()

    async def _dump():
        while True:
            await asyncio.sleep(1.0)
            with data.tlock:
                snap = dict(data.MAVLinkPacket)
            log.info("MAVLinkPacket: %s", snap)

    async def _main():
        await asyncio.gather(MAVLink(data).start(), _dump())

    asyncio.run(_main())
