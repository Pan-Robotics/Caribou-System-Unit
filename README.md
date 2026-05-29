# Caribou System Unit

Onboard telemetry handler for **Project Caribou**. Collects flight data from the flight controller, ESCs, and BMS; logs it locally; and streams it to the Caribou Hub over cellular.

> **Status:** Phase 1 — architecture locked, V1 implementation in progress.
> Tracking issue: [Arrow-air/project-caribou#12](https://github.com/Arrow-air/project-caribou/issues/12)
> Full architecture: [Docs/Architecture.md](Docs/Architecture.md)

This codebase is adapted from the [Feather Companion Computer (FCPC)](https://github.com/Pan-Robotics/Feather-Companion-Computer). The threading model and `Data.py` logging layer carry over; protocol handlers and the uplink swap out for Caribou hardware.

## Functions

1. **Data ingestion** — MAVLink (FC), CAN (ESCs + BMS), GPIO/joystick
2. **Onboard logging** — full-rate CSV to SSD for post-flight analysis
3. **Hub uplink** — live TCP stream to Caribou Hub over 4G + WireGuard/Tailscale

## Architecture (overview)

```
  [ArduPilot FC]    ──MAVLink/UDP──▶  MAVLink.py    ─┐
  [6x Hobbywing ESC]──CAN0────────▶  Hobbywing.py  ─┤
  [Tattu 18S BMS]   ──CAN1────────▶  TattuBMS.py   ─┼─▶ Data.py ─┬─▶ CSV log
  [GPIO / joystick] ───────────────▶  IO / Joystick ─┘            │
                                                                  └─▶ HubLink.py
                                                                       │
                                                                  4G + VPN
                                                                       │
                                                                       ▼
                                                              Caribou Hub
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
| `CSU.py` | Main loop, thread orchestration | planned (renames `FCPC.py`) |
| `MAVLink.py` | ArduPilot ingestion over UDP via pymavlink | planned (replaces `Veronte.py`) |
| `Hobbywing.py` | 6x XRotor X15 ESC telemetry on `can0` | planned (replaces `ESC.py`, `CyphalCAN3.py`) |
| `TattuBMS.py` | 18S Tattu smart battery, pluggable adapter | planned, interface TBD (replaces `BMS.py`, `VESCCAN.py`) |
| `HubLink.py` | Outbound TCP stream to Caribou Hub | planned (replaces `server.py` + `TCP.py`) |
| `Data.py` | Central state + CSV logger | kept |
| `IO.py`, `Joystick.py` | GPIO + joystick input | kept |

Removed for Caribou: `LoRa.py`, `display1.py`, `display2.py`, `protocols_functions.py` (Caribou Hub owns display; 4G replaces LoRa).

Current `src/` still contains the FCPC modules — they will be replaced as V1 modules land.

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
- [Caribou tracking issue](https://github.com/Arrow-air/project-caribou/issues/12)
- [Feather Companion Computer (upstream)](https://github.com/Pan-Robotics/Feather-Companion-Computer)
- [FCPC Concept Document](https://docs.google.com/document/d/15r7cTYvV1hOLt8er7vyQtWU0twEOfAIIOQ0pdE-wRtA/edit)
- [Data Network & Ground Equipment](https://docs.google.com/document/d/11VlSYsE245VFLZsYB7TvqWuuJ1UnPRjPKnnLWUzqcEM/edit)
