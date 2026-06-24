# Caribou System Unit — Architecture

Repository: [Pan-Robotics/Caribou-System-Unit](https://github.com/Pan-Robotics/Caribou-System-Unit)
Tracks: [Arrow-air/project-caribou#12](https://github.com/Arrow-air/project-caribou/issues/12)

The Caribou System Unit (CSU) is the onboard companion computer for Project Caribou. It runs on a Raspberry Pi CM5 inside the airframe, collects flight data from the flight controller, ESCs, and BMS, and serves it to [Caribou Hub](https://github.com/Pan-Robotics/Caribou-Hub) operators ([Arrow-air/project-caribou#10](https://github.com/Arrow-air/project-caribou/issues/10)) over cellular. **HubLink is a WebSocket server running on the drone, and Hubs dial in over Tailscale.**

Originated as a fork of the [Feather Companion Computer](https://github.com/Pan-Robotics/Feather-Companion-Computer); all FCPC heritage modules have been removed (see git history).

## Status — Phase 1

V1 of the telemetry pipeline is implemented, deployed, and HIL-validated on the bench drone:

| Subsystem | State | Notes |
|---|---|---|
| Compute platform (CM5 + Waveshare DUAL-ETH-4G-5G-BASE + 2-CH CAN HAT) | **Live** | Boot-to-running ≈ 25 s; survives reboot via systemd |
| Tailscale tailnet (`tag:drone`, `tag:fleet-caribou`) | **Live** | MagicDNS `caribou-001.<tailnet>.ts.net`; Hub-operator joins as `tag:hub-operator` |
| MAVLink ingest from Pixhawk 6X (over Ethernet, UDP 14540) | **Live** | MAVSDK 3.15, 8 parallel telemetry collectors |
| HubLink WS server `caribou.stream.v1` on `:8765` | **Live** | 10 Hz telemetry, lease+manifest, bearer-key auth |
| Hobbywing X15 ESC ingest (DroneCAN `esc.Status` on `can0`) | Code live; awaiting real ESCs on the bus |
| BMS ingest (DroneCAN `BatteryInfo` on `can1`) | Code live; awaiting real Tattu packs (or interface confirmation) |
| 4G modem (SIM7600X-H on M.2) | Hardware installed; cellular path proven via Tailscale relay; full hand-off TBD |
| Optional camera plane (go2rtc + Tailscale Funnel + Hub WHEP register) | Scaffolded under `Installation/camera/`; not enabled on the current bench drone (no RTSP camera wired) |

End-to-end telemetry has been validated against the production Caribou Hub on a separate laptop joined to the same tailnet — frames flow from Pixhawk through CSU and arrive in the Hub's Telemetry app.

## Hardware Platform

| Component | Choice |
|---|---|
| Compute | Raspberry Pi CM5 (Rev 1.0) |
| Carrier | [Waveshare CM5-DUAL-ETH-4G-5G-BASE](https://www.waveshare.com/cm5-dual-eth-4g-5g-base.htm) (dual GbE, M.2 slot for cellular, full 40-pin GPIO header) |
| CAN HAT | [Waveshare 2-CH CAN HAT](https://www.waveshare.com/2-ch-can-hat.htm) (2× MCP2515 on SPI0, 12 MHz crystals, IRQs GPIO 25 + GPIO 23) |
| Flight controller | Pixhawk 6X (Ethernet → CM5 `eth0` on static `192.168.144.0/24`; CM5=`.15`, FC=`.10`) |
| WAN modem | SIM7600X-H M.2 4G LTE (controlled via AT; `/dev/ttyUSB0..4` + `/dev/cdc-wdm0`) |
| ESCs | 6× [Hobbywing XRotor X15](https://www.hobbywing.com/en/products/xrotorx15) (configured for DroneCAN per Hobbywing DataLink) |
| BMS | 6× per-arm packs (DroneCAN BatteryInfo target; Tattu 18S confirmation pending) |
| Optional camera | Any RTSP source (gimbal, FPV cam, IP camera) — go2rtc transcodes |
| Network overlay | Tailscale (non-ephemeral tagged drone identity) |
| Enclosure | 3D-printed, files in `Hardware/Enclosure/` |

## Data Sources & Modules

| Source | Quantity | Link | Module |
|---|---|---|---|
| ArduPilot FC (Pixhawk 6X) | 1 | MAVLink over UDP/Ethernet (`udpin://0.0.0.0:14540`) | [`src/MAVLink.py`](../src/MAVLink.py) |
| Hobbywing XRotor X15 ESC | 6 | DroneCAN `uavcan.equipment.esc.Status` on `can0` | [`src/Hobbywing.py`](../src/Hobbywing.py) |
| Per-arm BMS | 6 | DroneCAN `uavcan.equipment.power.BatteryInfo` on `can1` | [`src/TattuBMS.py`](../src/TattuBMS.py) |

## Data Flow

```
                AIRCRAFT — Caribou System Unit (Pi CM5)
                ───────────────────────────────────────

  [Pixhawk 6X FC]   ──MAVLink/UDP eth0──▶ MAVLink.py    ─┐
  [6× Hobbywing X15]─DroneCAN/can0──────▶ Hobbywing.py  ─┤
  [6× BMS / Tattu]  ─DroneCAN/can1──────▶ TattuBMS.py   ─┼─▶ Data.py
                                                         ┘   tlock-guarded
                                                             MAVLinkPacket /
                                                             BMSArms / ESCArms
                                                                  │
                                                                  ▼
                                                            HubLink.py (WS :8765)
                                                            caribou.stream.v1
                                                                  ▲
                                                                  │  Hubs dial IN
                                                          Tailscale (MagicDNS)
                                                                  │
                                                          ── Cellular WAN (4G) ──
                                                                  │
                                                          ┌───────┴───────┐
                                                          ▼               ▼
                                                       Hub #1   ...    Hub #N

  (optional camera plane, independent of CSU):
  [RTSP camera]  ─RTSP─▶  go2rtc :1984  ─Tailscale Funnel (HTTPS :443)─▶  Hub WHEP proxy  ─▶  Browser WebRTC
                                          │
                                          └─▶ camera-stream.service registers the WHEP URL with the Hub
```

## Module Layout

| Module | Responsibility |
|---|---|
| [`src/CSU.py`](../src/CSU.py) | Process entry point. Constructs `Data`, spawns four daemon threads, traps SIGTERM/SIGINT. Reads `LOG_LEVEL`, `DRONE_ID` env. |
| [`src/Data.py`](../src/Data.py) | Shared in-process state: `tlock` + three structured dicts (`MAVLinkPacket`, `BMSArms`, `ESCArms`). Shape matches the Hub's telemetry §4 so HubLink passes through with minimal translation. |
| [`src/MAVLink.py`](../src/MAVLink.py) | MAVSDK client. 8 parallel collectors (attitude, position, GPS, battery, in_air, flight_mode, velocity, heading) writing into `Data.MAVLinkPacket`. Env: `MAVLINK_URL`, `UPDATE_RATE_HZ`. |
| [`src/Hobbywing.py`](../src/Hobbywing.py) | DroneCAN `esc.Status` listener on `can0`. Bridge node ID 113. Source-node-id → arm-id mapping (default 20→1 … 25→6). Writes `Data.ESCArms` including `motor_temperature_c`. Env: `ESC_CAN_INTERFACE`, `ESC_BRIDGE_NODE_ID`, `ESC_NODE_IDS`. |
| [`src/TattuBMS.py`](../src/TattuBMS.py) | DroneCAN `BatteryInfo` listener on `can1`. Bridge node ID 112. Source-node-id → arm-id mapping (default 10→1 … 15→6). Writes `Data.BMSArms`. Env: `BMS_CAN_INTERFACE`, `BMS_BRIDGE_NODE_ID`, `BMS_NODE_IDS`. |
| [`src/HubLink.py`](../src/HubLink.py) | WebSocket server on `:8765` (`caribou.stream.v1`). Bearer-key auth, 10 Hz telemetry, single-writer control lease, capability manifest. Env: `STREAM_HOST`, `STREAM_PORT`, `API_KEY`, `UPDATE_RATE_HZ`, `LEASE_TTL_MS`, `DRONE_ID`. |

## Network Architecture

```
  ╔═════════════════════════╗                        ╔══════════════╗
  ║ Caribou System Unit     ║                        ║   Hub #1     ║
  ║  HubLink :8765 (server) ║◀── WS (Tailscale) ─────║  (client)    ║
  ║  caribou.stream.v1      ║                        ╚══════════════╝
  ║                         ║                        ╔══════════════╗
  ║                         ║◀── WS (Tailscale) ─────║   Hub #N     ║
  ╚═════════════════════════╝                        ║  (client)    ║
                                                     ╚══════════════╝
  └────────── 4G modem + tagged Tailscale auth key ──┘
```

The drone joins a Tailscale tailnet over 4G with a tagged, non-ephemeral, single-use auth key, giving it a stable MagicDNS name (`caribou-001.<tailnet>.ts.net`). Caribou Hub operators dial into that name on port `8765`. Drone-as-service lets multiple operators monitor the same drone concurrently and sidesteps carrier NAT/CGNAT without per-drone public IPs.

**Security layers.**
- **Tailscale ACLs** decide which Hubs can reach `:8765` — `tag:hub-operator → tag:drone:8765`.
- **Per-drone bearer API key** in the WebSocket subprotocol list (`bearer.<key>`) decides which drone's data a Hub may pull. Mismatched keys close the connection with WebSocket code 1008.
- **API keys are non-secret on the wire to Tailscale peers** but require both layers to pass.

**Control lease.** Many-Hubs-read, single-writer control. HubLink is the lease authority. At most one Hub holds the lease at a time; 30 s TTL refreshed by heartbeats. Holder that drops connection automatically loses the lease on the next 1 Hz expiry sweep. See [HubLink_Implementation_Spec.md](HubLink_Implementation_Spec.md) §5.

**Capability manifest.** Each drone advertises its payloads and the typed commands they accept (e.g. camera zoom, winch deploy). The Hub renders command forms from the manifest and rejects unknown actions client-side. See [HubLink_Implementation_Spec.md](HubLink_Implementation_Spec.md) §6.

**Outage policy.** Cellular dropouts break the Hub's WebSocket; the Hub reconnects and resumes pulling live telemetry. No local replay or backfill on the drone side.

## Threading Model

Single Python process; four daemon threads, one per source/sink:

```
main thread        parks until SIGTERM/SIGINT
├── MAVLink        asyncio loop -> MAVSDK -> Data.MAVLinkPacket
├── Hobbywing      dronecan.node.spin(can0) -> Data.ESCArms
├── TattuBMS       dronecan.node.spin(can1) -> Data.BMSArms
└── HubLink        asyncio loop -> WS server on :8765 -> reads all three dicts under tlock
```

`Data.tlock` (a `threading.Lock`) is held only across the dict mutation / copy — workers copy values out before doing any longer work.

## Deployment (drone side)

systemd-managed on the CM5 via two units, both shipped in [`Installation/`](../Installation/):

- **`caribou-can.service`** — `oneshot`, `Before=csu.service`. Brings up `can0` (500 kbit/s) and `can1` (1 Mbit/s) once the kernel MCP2515 devices appear. Idempotent (`ExecStartPre=-/sbin/ip link set canN down` before the up so re-runs work).
- **`csu.service`** — `Type=simple`, runs `<venv>/bin/python3 -u CSU.py` as the service user, `Restart=always`, `EnvironmentFile=~/caribou-csu.env` (per-drone `API_KEY` + `DRONE_ID`). Hardened (`ProtectSystem=strict`, `ProtectHome=read-only`, `ReadWritePaths=<repo>`, `NoNewPrivileges`, `PrivateTmp`).

Fresh drones are provisioned with one script: [`Installation/bootstrap_drone.sh`](../Installation/bootstrap_drone.sh).
It does (1) system packages → (2) Tailscale install + tagged join → (3) Python venv + `mavsdk` + `websockets` + `dronecan` → (4) `~/caribou-csu.env` (0600) → (5) both systemd units → (6) prints a connection-summary card with MagicDNS name + tailnet IP + stream port for the operator to type into the Hub UI.

The MCP2515 CAN HAT overlays require a one-time write to `/boot/firmware/config.txt` and a reboot — handled by [`Installation/enable_uart_spi.sh`](../Installation/enable_uart_spi.sh), run once before the first `bootstrap_drone.sh`.

## Optional: Camera Streaming Plane

Independent of and parallel to the telemetry plane. Lives entirely under [`Installation/camera/`](../Installation/camera/) and is installed per-drone when an RTSP camera is wired:

```
  RTSP camera  ──RTSP──▶  go2rtc.service (:1984)  ──Tailscale Funnel (:443 HTTPS)──▶  Hub WHEP proxy
                              │                                                              │
                              └── camera-stream.service registers the WHEP URL with the Hub ─┘
                                  every 5 min (heartbeat), inherits API_KEY / DRONE_ID from
                                  ~/caribou-csu.env, camera-specific config in ~/caribou-camera.env
```

Three systemd units (`go2rtc.service`, `tailscale-funnel.service`, `camera-stream.service`) installed by [`Installation/camera/install_camera_services.sh`](../Installation/camera/install_camera_services.sh). The CSU telemetry plane is untouched — telemetry continues to work if the camera fails, and removing the camera stack doesn't affect telemetry.

## Hub-side Compatibility Notes

The HubLink wire format is defined in [`HubLink_Implementation_Spec.md`](HubLink_Implementation_Spec.md). The production Hub (`Caribou Hub/Hub Interface`) has a small translation layer in `client/src/components/apps/TelemetryApp.tsx::toArmData()` that normalises HubLink's nested per-arm shape `{arm_id, bms:{}, esc:{}}` into the flat shape the Hub's `HexStructuralView` consumes (`{motorId, rpm, esc_temp_c, esc_voltage_v, esc_current_a, motor_temp_c, bat_temp_c, bat_soc_pct}`).

Key wire-vs-UI alignments to be aware of:

| Wire field | Hub UI consumer | Notes |
|---|---|---|
| `attitude.{roll_deg, pitch_deg, yaw_deg, timestamp}` | CockpitHUD artificial horizon + compass tape | ✓ |
| `position.{lat, lon, abs_alt, rel_alt, timestamp}` | CockpitHUD altitude ladder + map | ✓ |
| `gps.{num_satellites, fix_type, timestamp}` | CockpitHUD GPS status badge | ✓ |
| `battery_fc.{voltage_v, remaining_percent, timestamp}` | CockpitHUD battery indicator | ✓ |
| `in_air`, `flight_mode`, `airspeed_ms`, `vertical_speed_ms` | CockpitHUD status panels | ✓ |
| `heading_deg` | Should drive CockpitHUD compass, but the Hub's `TelemetryApp` doesn't currently pass it to `<CockpitHUD heading={…}>` — compass falls back to `attitude.yaw_deg`. **Hub-side wiring gap.** | ⚠ |
| `arms[].bms.{voltage_v, current_a, temperature_c, soc_pct}` | Per-arm SoC / temp / V/I in `HexStructuralView` via `toArmData` | ✓ |
| `arms[].esc.{rpm, voltage_v, current_a, temperature_c, motor_temp_c}` | Per-arm RPM / ESC temp / motor temp / V/A in `HexStructuralView` via `toArmData` | ✓ (`motor_temp_c` added here as of June 2026 — earlier HubLink builds dropped it) |
| `battery_uavcan.*` | Declared in `TelemetryMessage` for legacy reasons; used only as fallback inside `derivedArmData()` (demo data). HubLink does not emit it; UI rendering is unaffected. | Soft gap |

## Open Questions

1. **Tattu interface confirmation.** Architecture assumes Tattu 18S packs broadcast DroneCAN `BatteryInfo`. If they speak a proprietary protocol, swap `TattuBMS._battery_callback` for whatever message they use; `Data.BMSArms` shape stays identical.
2. **ESC bitrate.** `can0` is brought up at 500 kbit/s; the X15 wiki doesn't lock a default — confirm with a real ESC on the bench.
3. **Per-drone API key provisioning at scale.** Currently a manual step (Hub UI generates key → operator pastes into `bootstrap_drone.sh`). Future Hub-side automation could mint via the Tailscale API or via a BYOK flow — see memory note `hub-tailscale-key-management`.
4. **Hub-side `heading_deg` wiring.** TelemetryApp.tsx should pass `heading={telemetry?.heading_deg}` to `<CockpitHUD>` so the compass uses true heading rather than yaw. One-line Hub-repo fix; not in this repo's scope but worth tracking.
5. **ACL lockdown.** The tailnet currently permits `*:*` between members (set during bring-up). Before customer-facing deployment, tighten to `tag:hub-operator → tag:drone:8765` plus admin-SSH. See [HubLink_Implementation_Spec.md](HubLink_Implementation_Spec.md) §9 / earlier session notes.

## References

- [HubLink Implementation Spec](HubLink_Implementation_Spec.md) — wire protocol, auth, lease, manifest, acceptance criteria
- [Pan-Robotics/Caribou-System-Unit](https://github.com/Pan-Robotics/Caribou-System-Unit)
- [Caribou Hub (Pan-Robotics, sibling repo)](https://github.com/Pan-Robotics/Caribou-Hub) — production Hub the System Unit serves; local checkout at `Caribou Hub/Hub Interface/`
- [Feather Companion Computer (heritage upstream)](https://github.com/Pan-Robotics/Feather-Companion-Computer)
- [FCPC Concept Doc](https://docs.google.com/document/d/15r7cTYvV1hOLt8er7vyQtWU0twEOfAIIOQ0pdE-wRtA/edit)
- [Data Network & Ground Equipment](https://docs.google.com/document/d/11VlSYsE245VFLZsYB7TvqWuuJ1UnPRjPKnnLWUzqcEM/edit)
