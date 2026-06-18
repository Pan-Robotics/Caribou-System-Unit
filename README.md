# Caribou System Unit

Onboard telemetry handler for **Project Caribou**. Collects flight data from the flight controller, ESCs, and BMS; logs it locally; and serves it to Caribou Hub operators via an inbound WebSocket server (HubLink) over Tailscale on 4G.

> **Status:** Phase 1 — architecture locked, V1 implementation in progress.
> Tracking issue: [Arrow-air/project-caribou#12](https://github.com/Arrow-air/project-caribou/issues/12)
> Full architecture: [Docs/Architecture.md](Docs/Architecture.md)

This codebase is adapted from the [Feather Companion Computer (FCPC)](https://github.com/Pan-Robotics/Feather-Companion-Computer). The threading model and `Data.py` logging layer carry over; protocol handlers and the uplink swap out for Caribou hardware.

## Functions

1. **Data ingestion** — MAVLink (FC), CAN (ESCs + BMS), GPIO/joystick
2. **Onboard logging** — full-rate CSV to SSD for post-flight analysis
3. **Hub service** — WebSocket server (`caribou.stream.v1`) on `:8765` that Hubs dial into over Tailscale; single-writer control lease + capability manifest

## Architecture (overview)

```
  [ArduPilot FC]    ──MAVLink/UDP──▶  MAVLink.py    ─┐
  [6x Hobbywing ESC]──CAN0────────▶  Hobbywing.py  ─┤
  [Tattu 18S BMS]   ──CAN1────────▶  TattuBMS.py   ─┼─▶ Data.py ─┬─▶ CSV log
  [GPIO / joystick] ───────────────▶  IO / Joystick ─┘            │
                                                                  └─▶ HubLink.py
                                                                      (WS :8765)
                                                                          ▲
                                                                          │  Hubs dial IN
                                                                  Tailscale + 4G
                                                                          │
                                                                  Hub #1 ... Hub #N
```

See [Docs/Architecture.md](Docs/Architecture.md) for the full diagram, module map, network topology, and open questions.

## Hardware

| Component | Choice |
|---|---|
| Compute | Raspberry Pi CM5 |
| Carrier | FCPC Breakout PCB ([Hardware/PCBs/FCPC Breakout PCB/](Hardware/PCBs/FCPC%20Breakout%20PCB/)) |
| CAN | 2-channel CAN HAT ([Hardware/PCBs/2-CH CAN HAT drawing/](Hardware/PCBs/2-CH%20CAN%20HAT%20drawing/)) |
| WAN | 4G modem (AT-driven, ref [Docs/AT_Command_Reference.docx](Docs/AT_Command_Reference.docx)) |
| Storage | onboard SSD |
| Enclosure | 3D-printed ([Hardware/Enclosure/](Hardware/Enclosure/)) |

## Module Plan (V1)

| Module | Role | Status |
|---|---|---|
| `CSU.py` | Main entry point: spawns MAVLink + HubLink threads, handles SIGTERM/SIGINT | implemented (supersedes `FCPC.py`) |
| `MAVLink.py` | ArduPilot ingestion over UDP via MAVSDK, populates `Data.MAVLinkPacket` | implemented (replaces `Veronte.py`) |
| `Hobbywing.py` | 6x XRotor X15 ESC telemetry on `can0` | planned (replaces `ESC.py`, `CyphalCAN3.py`) |
| `TattuBMS.py` | 18S Tattu smart battery, pluggable adapter | planned, interface TBD (replaces `BMS.py`, `VESCCAN.py`) |
| `HubLink.py` | Inbound WS server (`caribou.stream.v1`), lease + manifest | implemented (replaces `server.py` + `TCP.py`); see [Docs/HubLink_Implementation_Spec.md](Docs/HubLink_Implementation_Spec.md) |
| `Data.py` | Central state + CSV logger | kept |
| `IO.py`, `Joystick.py` | GPIO + joystick input | kept |

Removed for Caribou: `LoRa.py`, `display1.py`, `display2.py`, `protocols_functions.py` (Caribou Hub owns display; 4G replaces LoRa).

Current `src/` still contains the FCPC modules — they will be replaced as V1 modules land.

## Deploying to a Fresh Drone

A new CM5 is brought online with one script:

```bash
git clone https://github.com/Pan-Robotics/Caribou-System-Unit.git ~/Caribou-System-Unit
cd ~/Caribou-System-Unit
./Installation/bootstrap_drone.sh
```

The script provisions:
1. System packages (`curl`, `python3-venv`, `ca-certificates`)
2. Tailscale (`tailscale up` with the supplied auth key + drone tags)
3. A Python venv at `.venv/` with `mavsdk` + `websockets`
4. `~/caribou-csu.env` (`API_KEY` + `DRONE_ID`, mode `0600`)
5. `/etc/systemd/system/csu.service` (enabled + started, `Restart=always`)
6. A connection-summary card with the drone's MagicDNS name + tailnet IP + stream port to enter into the Hub UI

Inputs can be interactive (defaults shown in prompts) or env-var supplied for unattended fleet provisioning:

```bash
DRONE_ID=caribou_007 \
API_KEY=<per-drone-bearer> \
TS_AUTHKEY=tskey-auth-... \
./Installation/bootstrap_drone.sh
```

The Tailscale auth key should be **non-ephemeral, single-use, and tagged** (`tag:drone`, `tag:fleet-caribou` by default). Drones persist on the tailnet across reboots and outages.

## Repo Layout

```
Caribou System Unit/
├── src/                 application code (CSU modules)
├── Docs/                architecture, BOM, protocol references
├── Hardware/            PCB designs, enclosure files
├── Installation/        Raspberry Pi setup scripts and dependencies
├── Logs/                sample logs, log parser
├── Test/                hardware-in-the-loop test fixtures and Arduino dummies
└── images/              UI assets
```

## References

- [Architecture proposal](Docs/Architecture.md) — full design doc for issue #12
- [HubLink Implementation Spec](Docs/HubLink_Implementation_Spec.md) — wire protocol, auth, lease, manifest
- [Caribou tracking issue](https://github.com/Arrow-air/project-caribou/issues/12)
- [Feather Companion Computer (upstream)](https://github.com/Pan-Robotics/Feather-Companion-Computer)
- [FCPC Concept Document](https://docs.google.com/document/d/15r7cTYvV1hOLt8er7vyQtWU0twEOfAIIOQ0pdE-wRtA/edit)
- [Data Network & Ground Equipment](https://docs.google.com/document/d/11VlSYsE245VFLZsYB7TvqWuuJ1UnPRjPKnnLWUzqcEM/edit)
