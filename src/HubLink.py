"""HubLink.py - Caribou System Unit <-> Caribou Hub data plane.

WebSocket SERVER that runs on the drone. Hubs connect IN over Tailscale
(MagicDNS) and pull telemetry. One Hub at a time may hold the control
lease; only that Hub's commands are executed. See
Docs/HubLink_Implementation_Spec.md for the wire protocol.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

log = logging.getLogger("HubLink")

SUBPROTOCOL = "caribou.stream.v1"
PROTOCOL_VERSION = 1


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


class HubLink:
    """Async WebSocket server exposing telemetry + control to Caribou Hubs."""

    def __init__(
        self,
        data,
        *,
        host: str | None = None,
        port: int | None = None,
        api_key: str | None = None,
        update_rate_hz: float | None = None,
        lease_ttl_ms: int | None = None,
        drone_id: str | None = None,
        manifest: list | None = None,
    ) -> None:
        self.data = data
        self.host = host if host is not None else os.environ.get("STREAM_HOST", "0.0.0.0")
        self.port = int(port if port is not None else os.environ.get("STREAM_PORT", "8765"))
        self.api_key = api_key if api_key is not None else os.environ.get("API_KEY", "")
        self.update_rate_hz = float(
            update_rate_hz if update_rate_hz is not None else os.environ.get("UPDATE_RATE_HZ", "10")
        )
        self.lease_ttl_ms = int(
            lease_ttl_ms if lease_ttl_ms is not None else os.environ.get("LEASE_TTL_MS", "30000")
        )
        self.drone_id = drone_id if drone_id is not None else os.environ.get("DRONE_ID", "caribou_001")
        self.manifest: list = manifest if manifest is not None else []

        self._lease: dict | None = None
        self._command_handlers: dict = {}

    def register_command(self, action: str, handler) -> None:
        """Bind a payload-side handler for a manifest-declared `action`.

        Handler signature: `handler(params: dict) -> dict | None`, sync or async.
        Return value becomes the `result` of `command_result`.
        """
        self._command_handlers[action] = handler

    async def start(self) -> None:
        log.info(
            "HubLink listening on %s:%d (drone_id=%s, rate=%sHz)",
            self.host, self.port, self.drone_id, self.update_rate_hz,
        )
        if not self.api_key:
            log.warning("API_KEY not set; accepting any bearer (benchtop only).")
        async with serve(self._handler, self.host, self.port, subprotocols=[SUBPROTOCOL]):
            await self._expiry_loop()

    def run(self) -> None:
        """Blocking entry point for a thread that owns the asyncio loop."""
        asyncio.run(self.start())

    async def _handler(self, websocket) -> None:
        if not self._authenticate(websocket):
            log.warning("Auth rejected from %s", websocket.remote_address)
            await websocket.close(code=1008, reason="invalid bearer")
            return

        peer = websocket.remote_address
        log.info("Hub connected: %s", peer)
        try:
            await self._send(websocket, {
                "type": "hello",
                "protocol": PROTOCOL_VERSION,
                "services": ["telemetry", "control"],
            })
            await self._send(websocket, {"type": "manifest", "payloads": self.manifest})

            send_task = asyncio.create_task(self._send_loop(websocket))
            recv_task = asyncio.create_task(self._recv_loop(websocket))
            done, pending = await asyncio.wait(
                {send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, ConnectionClosed):
                    log.exception("Connection task crashed", exc_info=exc)
        except ConnectionClosed:
            pass
        finally:
            if self._lease is not None and self._lease.get("websocket") is websocket:
                log.info("Lease holder disconnected; clearing lease %s", self._lease.get("id"))
                self._lease = None
            log.info("Hub disconnected: %s", peer)

    def _authenticate(self, websocket) -> bool:
        raw = websocket.request.headers.get("Sec-WebSocket-Protocol", "")
        offered = [t.strip() for t in raw.split(",") if t.strip()]
        bearer = next((t[len("bearer."):] for t in offered if t.startswith("bearer.")), None)
        if not self.api_key:
            return True
        return bearer is not None and secrets.compare_digest(bearer, self.api_key)

    async def _send(self, websocket, frame: dict) -> None:
        await websocket.send(json.dumps(frame, default=str))

    async def _send_loop(self, websocket) -> None:
        interval = 1.0 / max(self.update_rate_hz, 0.1)
        while True:
            try:
                payload = self._build_telemetry()
            except Exception:
                log.exception("Failed to build telemetry frame; sending empty payload")
                payload = {}
            await self._send(websocket, {
                "type": "telemetry",
                "timestamp": _now_iso(),
                "telemetry": payload,
            })
            await asyncio.sleep(interval)

    async def _recv_loop(self, websocket) -> None:
        async for raw in websocket:
            if not isinstance(raw, str):
                continue
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(frame, dict):
                continue
            try:
                await self._dispatch(websocket, frame)
            except ConnectionClosed:
                raise
            except Exception:
                log.exception("Error handling frame: %r", frame.get("type"))

    async def _dispatch(self, websocket, frame: dict) -> None:
        ftype = frame.get("type")
        if ftype == "ping":
            await self._send(websocket, {"type": "pong", "ts": frame.get("ts", _now_ms())})
        elif ftype == "get_manifest":
            await self._send(websocket, {"type": "manifest", "payloads": self.manifest})
        elif ftype == "lease_acquire":
            await self._handle_lease_acquire(websocket, frame)
        elif ftype == "lease_heartbeat":
            self._handle_lease_heartbeat(frame)
        elif ftype == "lease_release":
            await self._handle_lease_release(websocket, frame)
        elif ftype == "command":
            await self._handle_command(websocket, frame)
        # Unknown frame types are ignored per spec §3.

    async def _handle_lease_acquire(self, websocket, frame: dict) -> None:
        hub_id = frame.get("hub_id", "")
        request_id = frame.get("request_id", "")
        now = _now_ms()
        active = self._lease
        renewing_same_holder = active is not None and active["hub_id"] == hub_id
        expired = active is not None and active["expires_at_ms"] <= now
        if active is None or expired or renewing_same_holder:
            lease_id = secrets.token_urlsafe(12)
            self._lease = {
                "id": lease_id,
                "hub_id": hub_id,
                "websocket": websocket,
                "expires_at_ms": now + self.lease_ttl_ms,
            }
            log.info("Lease granted to hub_id=%s (lease=%s)", hub_id, lease_id)
            await self._send(websocket, {
                "type": "lease_granted",
                "lease_id": lease_id,
                "ttl_ms": self.lease_ttl_ms,
                "request_id": request_id,
            })
        else:
            await self._send(websocket, {
                "type": "lease_denied",
                "held_by": active["hub_id"],
                "reason": "held",
                "request_id": request_id,
            })

    def _handle_lease_heartbeat(self, frame: dict) -> None:
        if self._lease is not None and self._lease["id"] == frame.get("lease_id"):
            self._lease["expires_at_ms"] = _now_ms() + self.lease_ttl_ms

    async def _handle_lease_release(self, websocket, frame: dict) -> None:
        lease_id = frame.get("lease_id")
        request_id = frame.get("request_id", "")
        if self._lease is not None and self._lease["id"] == lease_id:
            log.info("Lease released by hub_id=%s (lease=%s)", self._lease["hub_id"], lease_id)
            self._lease = None
            await self._send(websocket, {
                "type": "lease_released",
                "lease_id": lease_id,
                "request_id": request_id,
            })

    async def _handle_command(self, websocket, frame: dict) -> None:
        request_id = frame.get("request_id", "")
        lease_id = frame.get("lease_id")
        active = self._lease
        if (
            active is None
            or active["id"] != lease_id
            or active["websocket"] is not websocket
            or active["expires_at_ms"] <= _now_ms()
        ):
            await self._send(websocket, {
                "type": "command_result",
                "request_id": request_id,
                "ok": False,
                "error": "no_control",
            })
            return

        action = frame.get("action", "")
        params = frame.get("params") or {}
        handler = self._command_handlers.get(action)
        try:
            if handler is None:
                result = {"accepted": True, "note": "stub: no handler bound"}
            elif asyncio.iscoroutinefunction(handler):
                result = await handler(params)
            else:
                result = handler(params)
            if result is None:
                result = {"accepted": True}
            await self._send(websocket, {
                "type": "command_result",
                "request_id": request_id,
                "ok": True,
                "result": result,
            })
        except Exception as e:
            log.exception("Command %s failed", action)
            await self._send(websocket, {
                "type": "command_result",
                "request_id": request_id,
                "ok": False,
                "error": str(e),
            })

    async def _expiry_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            active = self._lease
            if active is None:
                continue
            if active["expires_at_ms"] > _now_ms():
                continue
            log.info("Lease %s (hub_id=%s) expired; revoking", active["id"], active["hub_id"])
            self._lease = None
            ws = active.get("websocket")
            if ws is None:
                continue
            try:
                await self._send(ws, {
                    "type": "lease_revoked",
                    "lease_id": active["id"],
                    "reason": "expired",
                })
            except ConnectionClosed:
                pass

    def _build_telemetry(self) -> dict:
        """Snapshot Data.py state under tlock and shape it per spec §4.

        MAVLink-sourced blocks (attitude/position/gps/battery_fc/in_air/...)
        are populated by MAVLink.py into Data.MAVLinkPacket in the exact
        shape the Hub expects, so they pass through as-is. Per-arm data is
        assembled from BMSLog/ESCLog (populated by Hobbywing.py and
        TattuBMS.py as those land).
        """
        with self.data.tlock:
            m = dict(self.data.MAVLinkPacket)
            bms = {k: dict(val) for k, val in self.data.BMSLog.items()}
            esc = {k: dict(val) for k, val in self.data.ESCLog.items()}

        arms = []
        for i in range(6):
            key = str(i)
            arm = {"arm_id": i + 1}
            if key in bms:
                b = bms[key]
                arm["bms"] = {
                    "voltage_v": float(b.get("packVoltage") or 0.0),
                    "current_a": float(b.get("packCurrent1") or 0.0),
                    "temperature_c": float(b.get("tBattHi") or 0.0),
                    "soc_pct": float(b.get("SOC") or 0.0),
                }
            if key in esc:
                e = esc[key]
                e60 = e.get("info_upload_6160") or {}
                e61 = e.get("info_upload_6161") or {}
                temps = e61.get("temperatures") or {}
                arm["esc"] = {
                    "rpm": float(e60.get("electrical_speed") or 0.0),
                    "voltage_v": float(e61.get("bus_voltage") or 0.0),
                    "current_a": float(e60.get("bus_current") or 0.0),
                    "temperature_c": float(temps.get("Motor") or temps.get("MOS") or 0.0),
                }
            arms.append(arm)

        return {
            "attitude": m.get("attitude"),
            "position": m.get("position"),
            "gps": m.get("gps"),
            "battery_fc": m.get("battery_fc"),
            "in_air": m.get("in_air"),
            "flight_mode": m.get("flight_mode"),
            "airspeed_ms": m.get("airspeed_ms"),
            "vertical_speed_ms": m.get("vertical_speed_ms"),
            "heading_deg": m.get("heading_deg"),
            "arms": arms,
        }


if __name__ == "__main__":
    # Standalone smoke test: serve a stub Data instance so a WS client can
    # exercise the protocol without the full CSU stack.
    import threading

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    class _StubData:
        def __init__(self):
            self.tlock = threading.Lock()
            ts = _now_iso()
            self.MAVLinkPacket = {
                "attitude": {"roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": 90.0,
                              "timestamp": ts},
                "position": {"latitude_deg": 40.4458, "longitude_deg": -79.9822,
                              "absolute_altitude_m": 40.0, "relative_altitude_m": 10.0,
                              "timestamp": ts},
                "gps": {"num_satellites": 14, "fix_type": 3, "timestamp": ts},
                "battery_fc": {"voltage_v": 48.2, "remaining_percent": 87.0,
                                "timestamp": ts},
                "in_air": False,
                "flight_mode": "LOITER",
                "airspeed_ms": 0.0,
                "vertical_speed_ms": 0.0,
                "heading_deg": 90.0,
            }
            self.BMSLog = {
                str(i): {"packVoltage": 22.2, "packCurrent1": 12.0,
                         "tBattHi": 30.0, "SOC": 87.0}
                for i in range(6)
            }
            self.ESCLog = {
                str(i): {
                    "info_upload_6160": {"electrical_speed": 4200, "bus_current": 11.0},
                    "info_upload_6161": {"bus_voltage": 22.2,
                                         "temperatures": {"Motor": 45, "MOS": 38}},
                }
                for i in range(6)
            }

    HubLink(_StubData()).run()
