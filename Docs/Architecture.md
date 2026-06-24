# Caribou System Unit — Architecture

Repository: [Pan-Robotics/Caribou-System-Unit](https://github.com/Pan-Robotics/Caribou-System-Unit)
Tracks: [Arrow-air/project-caribou#12](https://github.com/Arrow-air/project-caribou/issues/12)

The Caribou System Unit (CSU) is the onboard telemetry handler for Project Caribou. It collects flight data from the flight controller, ESCs, and BMS and serves it to Caribou Hub operators ([Arrow-air/project-caribou#10](https://github.com/Arrow-air/project-caribou/issues/10)) over cellular: **HubLink is a WebSocket server running on the drone, and Hubs dial in over Tailscale.**

Originated as a fork of the [Feather Companion Computer (FCPC)](https://github.com/Pan-Robotics/Feather-Companion-Computer); all FCPC heritage modules have been removed (see git history).

## Hardware Platform

| Component | Choice |
|---|---|
| Compute | Raspberry Pi CM5 |
| Carrier | Waveshare CM5-DUAL-ETH-4G-5G-BASE (dual GbE + M.2 4G/5G slot) |
| CAN | Waveshare 2-CH CAN HAT (MCP2515 × 2) on the 40-pin header — `can0` (Hobbywing ESCs) + `can1` (BMS) |
| WAN | SIM7600X-H M.2 4G modem (AT-driven; reference in `Docs/AT_Command_Reference.docx`) |
| Network overlay | Tailscale tagged-ephemeral-key join; drone-as-service over MagicDNS |
| Enclosure | 3D-printed (`Hardware/Enclosure/`) |

## Data Sources

| Source | Quantity | Link | Module |
|---|---|---|---|
| ArduPilot flight controller | 1 | MAVLink over UDP / Ethernet (`udpin://0.0.0.0:14540`) | `MAVLink.py` |
| Hobbywing XRotor X15 ESC | 6 | DroneCAN `uavcan.equipment.esc.Status` on `can0` (X15 in DroneCAN mode) | `Hobbywing.py` |
| BMS (Tattu 18S target) | 6 | DroneCAN `uavcan.equipment.power.BatteryInfo` on `can1` | `TattuBMS.py` |

## Data Flow

```
                AIRCRAFT — Caribou System Unit (Pi CM5)
                ───────────────────────────────────────

  [ArduPilot FC]      ──MAVLink/UDP──▶ MAVLink.py    ─┐
  [6x Hobbywing X15]  ──CAN0(DroneCAN)─▶ Hobbywing.py ─┤    Data.py
  [6x BMS  (DroneCAN)]──CAN1(DroneCAN)─▶ TattuBMS.py  ─┼─▶  tlock-guarded
                                                       ┘    MAVLinkPacket /
                                                            BMSArms / ESCArms
                                                                  │
                                                                  ▼
                                                            HubLink.py (WS :8765)
                                                                  ▲
                                                                  │   Hubs dial IN
                                                          Tailscale (MagicDNS)
                                                                  │
                                                          ── Cellular WAN (4G) ──
                                                                  │
                                                          ┌───────┴───────┐
                                                          ▼               ▼
                                                       Hub #1   ...    Hub #N
```

## Module Layout

| Module | Responsibility |
|---|---|
| `src/CSU.py` | Process entry point. Constructs `Data`, spawns four daemon threads, traps SIGTERM/SIGINT. |
| `src/Data.py` | Shared in-process state: `tlock` + three structured dicts (`MAVLinkPacket`, `BMSArms`, `ESCArms`). Shape matches Caribou Hub telemetry §4 so HubLink passes through with no translation. |
| `src/MAVLink.py` | MAVSDK client. 8 parallel collectors (attitude, position, GPS, battery, in_air, flight_mode, velocity, heading) writing into `Data.MAVLinkPacket`. |
| `src/Hobbywing.py` | DroneCAN `esc.Status` listener on `can0`. Bridge node ID 113. Source-node-id → arm-id mapping (default 20→1 … 25→6). Writes `Data.ESCArms`. |
| `src/TattuBMS.py` | DroneCAN `BatteryInfo` listener on `can1`. Bridge node ID 112. Source-node-id → arm-id mapping (default 10→1 … 15→6). Writes `Data.BMSArms`. |
| `src/HubLink.py` | WebSocket server on `:8765` (`caribou.stream.v1`). Bearer-key auth, 10 Hz telemetry, single-writer control lease, capability manifest. |

## Network Architecture

The drone joins a Tailscale tailnet over 4G with a tagged, non-ephemeral auth key, giving it a stable MagicDNS name. Caribou Hub operators dial into that name on port `8765` — HubLink is the WebSocket server, Hubs are the clients. Drone-as-service lets multiple operators monitor the same drone concurrently and sidesteps carrier NAT/CGNAT without per-drone public IPs.

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

**Security layers.** Tailscale ACLs decide which Hubs can reach `:8765`; a per-drone bearer API key (sent in the WebSocket subprotocol list as `bearer.<key>`) decides which drone's data a Hub may pull. Mismatched keys close the connection with WebSocket code 1008.

**Control lease.** Monitoring is many-Hubs-read but control is single-writer: HubLink is the lease authority. At most one Hub holds the lease at a time, with a 30 s TTL refreshed by heartbeats. A holder that crashes or drops its connection automatically loses control on the next 1 Hz expiry sweep, so another operator can take over. See [HubLink_Implementation_Spec.md](HubLink_Implementation_Spec.md) §5.

**Capability manifest.** Each drone advertises its payloads and the typed commands they accept (e.g. camera zoom, winch deploy). The Hub renders command forms from the manifest and rejects unknown actions client-side. See [HubLink_Implementation_Spec.md](HubLink_Implementation_Spec.md) §6.

**Outage policy.** Cellular dropouts cause the Hub's WebSocket to break; the Hub reconnects and resumes pulling live telemetry. No local replay or backfill on the drone side.

## Threading Model

Single Python process; four daemon threads, one per source/sink:

```
main thread       parks on signal.signal(SIGTERM/SIGINT)
├── MAVLink       asyncio loop -> MAVSDK -> Data.MAVLinkPacket
├── Hobbywing     dronecan.node.spin(can0) -> Data.ESCArms
├── TattuBMS      dronecan.node.spin(can1) -> Data.BMSArms
└── HubLink       asyncio loop -> WS server on :8765 -> reads all three dicts under tlock
```

`Data.tlock` (a `threading.Lock`) is held only across the dict mutation / copy — every worker copies values out before doing any longer work.

## Deployment

systemd-managed on the CM5 via two units:

- `caribou-can.service` — oneshot, `Before=csu.service`. Brings up `can0` (500 kbit/s) and `can1` (1 Mbit/s) once the kernel MCP2515 devices appear. Idempotent (`ExecStartPre=-ip link set canN down` before the up).
- `csu.service` — `Type=simple`, runs `<venv>/bin/python3 -u CSU.py` as the service user, `Restart=always`, `EnvironmentFile=~/caribou-csu.env` (per-drone `API_KEY` + `DRONE_ID`). Hardened (`ProtectSystem=strict`, `ProtectHome=read-only`, `ReadWritePaths=<repo>`).

Fresh drones are provisioned by `Installation/bootstrap_drone.sh` (one shot: system pkgs → Tailscale → venv + deps → env file → both unit files → enable + start → prints a Hub-side connection card).

## Open Questions

1. **Tattu interface confirmation.** The architecture assumes Tattu 18S packs broadcast DroneCAN `BatteryInfo`. If they actually speak a proprietary protocol, swap the `TattuBMS._battery_callback` for whatever message type they use; `Data.BMSArms` shape stays identical.
2. **ESC bitrate.** `can0` is currently brought up at 500 kbit/s; the X15 wiki doesn't lock a default — confirm with a real ESC on the bench.
3. **Per-drone API key provisioning.** Currently a manual step (Hub UI generates key → operator pastes into `bootstrap_drone.sh`). Future Hub-side automation could mint via the Tailscale API (see memory note `hub-tailscale-key-management`).
4. **FC ethernet wiring.** Pixhawk 6X with Ethernet (current) → CM5 eth0 on the static 192.168.144.0/24 subnet; verify the same scheme works on production airframes.
5. **`bcm2835`/wiringPi removal.** Both third-party trees were FCPC heritage and have been removed. No current module needs them.

## References

- [HubLink Implementation Spec](HubLink_Implementation_Spec.md) — wire protocol, auth, lease, manifest, acceptance criteria
- [Pan-Robotics/Caribou-System-Unit](https://github.com/Pan-Robotics/Caribou-System-Unit)
- [Feather Companion Computer (heritage upstream)](https://github.com/Pan-Robotics/Feather-Companion-Computer)
- [FCPC Concept Doc](https://docs.google.com/document/d/15r7cTYvV1hOLt8er7vyQtWU0twEOfAIIOQ0pdE-wRtA/edit)
- [Data Network & Ground Equipment](https://docs.google.com/document/d/11VlSYsE245VFLZsYB7TvqWuuJ1UnPRjPKnnLWUzqcEM/edit)
