# Caribou System Unit — Architecture Proposal

Repository: [Pan-Robotics/Caribou-System-Unit](https://github.com/Pan-Robotics/Caribou-System-Unit)

Deliverable for [Arrow-air/project-caribou#12](https://github.com/Arrow-air/project-caribou/issues/12).

The Caribou System Unit (CSU) is the onboard telemetry handler for Project Caribou. It collects flight data from the flight controller, ESCs, and BMS; logs it locally; and serves it to Caribou Hub operators ([Arrow-air/project-caribou#10](https://github.com/Arrow-air/project-caribou/issues/10)) over cellular: HubLink is a WebSocket **server** running on the drone, and Hubs dial in over Tailscale.

This codebase is adapted from the [Feather Companion Computer (FCPC)](https://github.com/Pan-Robotics/Feather-Companion-Computer). The module structure, threading model, and `Data.py` logging layer carry over; the protocol handlers and uplink swap out for Caribou hardware.

## Hardware Platform

| Component | Choice |
|---|---|
| Compute | Raspberry Pi CM5 |
| Carrier | FCPC Breakout PCB (existing, in `Hardware/PCBs/FCPC Breakout PCB/`) |
| CAN interface | 2-channel CAN HAT (existing, in `Hardware/PCBs/2-CH CAN HAT drawing/`) |
| WAN | 4G modem (AT-command driven, reference in `Docs/AT_Command_Reference.docx`) |
| Storage | onboard SSD for full-rate CSV logs |
| Enclosure | 3D-printed, files in `Hardware/Enclosure/` |

## Data Sources

| Source | Quantity | Link | Module |
|---|---|---|---|
| ArduPilot flight controller | 1 | MAVLink over UDP / Ethernet | `MAVLink.py` |
| Hobbywing XRotor X15 ESC | 6 | CAN bus (`can0`) | `Hobbywing.py` |
| Tattu 18S smart battery | 1+ | TBD — CAN / UART / SMBus | `TattuBMS.py` |
| GPIO inputs, joystick | — | RPi GPIO + USB / CAN | `IO.py`, `Joystick.py` |

## Data Flow

```
                    AIRCRAFT — Caribou System Unit (Pi CM5)
                    ───────────────────────────────────────

  [ArduPilot FC] ──MAVLink/UDP──▶ MAVLink.py ──┐
                  (Ethernet)                   │
                                               │
  [6x Hobbywing XRotor X15] ──CAN0──▶ Hobbywing.py ──┤
                                               │
  [Tattu 18S BMS] ──?(TBD)──▶ TattuBMS.py ─────┤
                                               │
  [GPIO, joystick]   ──────▶ IO.py / Joystick.py ─┤
                                               ▼
                                          ┌──────────┐
                                          │  Data.py │
                                          │  + queue │
                                          └────┬─────┘
                                               │
                                  ┌────────────┴────────────┐
                                  ▼                         ▼
                          [CSV log on SSD]      HubLink.py (WS SERVER :8765)
                          (full rate, always)   caribou.stream.v1
                                                          ▲
                                                          │  (Hubs dial IN)
                                                          │
                                                  [ Tailscale tailnet ]
                                                          │
                                                ──[ Cellular WAN (4G) ]──
                                                          │
                                                  ┌───────┴────────┐
                                                  ▼                ▼
                                            ╔══════════╗     ╔══════════╗
                                            ║  Hub #1  ║ ... ║  Hub #N  ║
                                            ║ (issue   ║     ║          ║
                                            ║   #10)   ║     ║          ║
                                            ╚══════════╝     ╚══════════╝
```

## Module Map — FCPC → CSU

| FCPC module | CSU module | Change |
|---|---|---|
| `FCPC.py` | `CSU.py` | New thread orchestrator — spawns `MAVLink.run` and `HubLink.run` in daemon threads, parks main on SIGTERM/SIGINT; FCPC.py kept around until the legacy modules it imports are deleted |
| `Veronte.py` | `MAVLink.py` | Embention UART → MAVSDK UDP listener (`udpin://0.0.0.0:14540`), 8 parallel collectors writing to `Data.MAVLinkPacket` |
| `ESC.py` + `CyphalCAN3.py` | `Hobbywing.py` | MAD Motors / Cyphal-CAN → DroneCAN `esc.Status` listener on `can0`; 6 UAVCAN node IDs (default 20–25) routed to arms 1–6; populates `Data.ESCArms`. XRotor X15 supports HWCAN+DroneCAN dual protocols — must be set to DroneCAN via Hobbywing DataLink. |
| `BMS.py` + `VESCCAN.py` | `TattuBMS.py` | Ennoid / VESC-CAN → DroneCAN `BatteryInfo` listener on `can1`; 6 UAVCAN node IDs (default 10–15) routed to arms 1–6; populates `Data.BMSArms` |
| `server.py` + `TCP.py` | `HubLink.py` | Dual-display TCP server → inbound WebSocket server (`caribou.stream.v1`) that one or more Hubs dial into |
| `LoRa.py` | **removed** | 4G replaces long-range link |
| `display1.py`, `display2.py`, `protocols_functions.py` | **removed** | Caribou Hub owns display |
| `Data.py` | unchanged | Log basename already renamed to `CaribouSystemLog` |
| `IO.py`, `Joystick.py` | unchanged | |

## Network Architecture

The drone joins a Tailscale tailnet over 4G with a tagged ephemeral auth key, giving it a stable MagicDNS name. Caribou Hub operators dial into that name on port `8765` — HubLink is the WebSocket server, Hubs are the clients. This inverts the original "outbound TCP stream" design (see [HubLink_Implementation_Spec.md](HubLink_Implementation_Spec.md) §0): drone-as-service lets multiple operators monitor the same drone concurrently and sidesteps carrier NAT/CGNAT without per-drone public IPs.

```
  ╔═════════════════════════╗                        ╔══════════════╗
  ║ Caribou System Unit     ║                        ║   Hub #1     ║
  ║  HubLink :8765 (server) ║◀── WS (Tailscale) ─────║  (client)    ║
  ║  caribou.stream.v1      ║                        ╚══════════════╝
  ║                         ║                        ╔══════════════╗
  ║                         ║◀── WS (Tailscale) ─────║   Hub #N     ║
  ╚═════════════════════════╝                        ║  (client)    ║
                                                     ╚══════════════╝
  └─── 4G modem + tagged ephemeral Tailscale auth ───┘
```

**Security layers.** Tailscale ACLs decide which Hubs can reach `:8765`; a per-drone bearer API key (sent in the WebSocket subprotocol list as `bearer.<key>`) decides which drone's data a Hub may pull. Mismatched keys close the connection with WebSocket code 1008.

**Control lease.** Monitoring is many-Hubs-read but control is single-writer: HubLink is the lease authority. At most one Hub holds the lease at a time, with a 30 s TTL refreshed by heartbeats. A holder that crashes or drops its connection automatically loses control on the next 1 Hz expiry sweep, so another operator can take over. See [HubLink_Implementation_Spec.md](HubLink_Implementation_Spec.md) §5.

**Capability manifest.** Each drone advertises its payloads and the typed commands they accept (e.g. camera zoom, winch deploy). The Hub renders command forms from the manifest and rejects unknown actions client-side. See [HubLink_Implementation_Spec.md](HubLink_Implementation_Spec.md) §6.

**Outage policy.** Cellular dropouts cause the Hub's WebSocket to break; the Hub reconnects and resumes pulling live telemetry. No local replay or backfill — full-rate CSV on the SSD covers post-flight analysis. Keeps Hub-side views always-current.

## Threading Model

Inherited from FCPC: one Python process, one thread per data source + one for `HubLink`. `Data.py` holds the shared state behind a lock and exposes a snapshot to the logger and uplink. The main loop in `CSU.py` spawns and supervises threads.

## V1 Scope (issue #12 deliverable)

In scope:
- `MAVLink.py` — MAVSDK UDP listener; collects attitude, position, GPS, FC battery, velocity, heading, flight mode, in-air into `Data.MAVLinkPacket`
- `Hobbywing.py` — DroneCAN `esc.Status` listener on `can0`, source-node-id-to-arm mapping (default 20–25), populates `Data.ESCArms`
- `TattuBMS.py` — DroneCAN `BatteryInfo` listener on `can1`, source-node-id to arm mapping, populates `Data.BMSArms`
- `HubLink.py` — inbound WebSocket server (`caribou.stream.v1`) for telemetry + control lease + capability manifest
- `Data.py` — central store + CSV logger (already migrated)
- `CSU.py` — spawns MAVLink + HubLink in daemon threads, parks main thread on SIGTERM/SIGINT; reads `LOG_LEVEL`/`DRONE_ID` env

Out of scope for V1:
- Outage buffering / replay
- Multi-FC redundancy
- Hub-side consumer (tracked in issue #10)
- LoRa fallback

## Open Questions

1. **Tattu 18S BMS comms interface** — CAN / UART / SMBus / proprietary. Drives whether `can1` is used or a different bus is needed. Blocks `TattuBMS.py` implementation past the stub.
2. **CM5 carrier compatibility** — the existing FCPC Breakout PCB was designed against CM4 / Pi 4. Confirm CM5 footprint and pin compatibility before manufacturing the next revision.
3. **Per-drone API keys** — the Hub provisions an `API_KEY` per drone during its "Drone Configuration → API Keys" flow. Need a deployment procedure (env file / systemd `EnvironmentFile`) so the key lands on the unit without being committed to the repo.
4. **FC Ethernet hardware** — which ArduPilot board provides MAVLink-over-UDP natively (CubePilot Cube Orange+ on Ethernet carrier, Holybro Pixhawk 6X, etc.) vs. requiring a serial-to-Ethernet bridge.
5. **`bcm2835-1.60` dependency** — audit whether any retained module still needs it, or whether `RPi.GPIO` / `gpiozero` covers everything. Cleanup candidate.

## References

- [HubLink Implementation Spec](HubLink_Implementation_Spec.md) — wire protocol, auth, lease, manifest, acceptance criteria
- [Caribou System Unit repo](https://github.com/Pan-Robotics/Caribou-System-Unit)
- [Feather Companion Computer (upstream)](https://github.com/Pan-Robotics/Feather-Companion-Computer)
- [FCPC Concept Doc](https://docs.google.com/document/d/15r7cTYvV1hOLt8er7vyQtWU0twEOfAIIOQ0pdE-wRtA/edit)
- [Data Network & Ground Equipment](https://docs.google.com/document/d/11VlSYsE245VFLZsYB7TvqWuuJ1UnPRjPKnnLWUzqcEM/edit)
- [Feather PT3 Ready FCPC Bounty](https://docs.google.com/document/d/1tQf7TGQPeBz3J82_hX4NeGAglC0C-ZWMout1vr6JEZM/edit)
- [Feather PT4 Ready FCPC Bounty](https://docs.google.com/document/d/1vPeeErJhhwsIkiBVpW47scY42erG127w0NTgtlc_Bck/edit)
