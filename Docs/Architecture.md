# Caribou System Unit — Architecture Proposal

Repository: [Pan-Robotics/Caribou-System-Unit](https://github.com/Pan-Robotics/Caribou-System-Unit)

Deliverable for [Arrow-air/project-caribou#12](https://github.com/Arrow-air/project-caribou/issues/12).

The Caribou System Unit (CSU) is the onboard telemetry handler for Project Caribou. It collects flight data from the flight controller, ESCs, and BMS; logs it locally; and streams it to the Caribou Hub ([Arrow-air/project-caribou#10](https://github.com/Arrow-air/project-caribou/issues/10)) over cellular.

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
                          [CSV log on SSD]            HubLink.py
                          (full rate, always)         (TCP, live-only)
                                                          │
                                                          ▼
                                                   [4G modem (AT)]
                                                          │
                                                ──[ Cellular WAN ]──
                                                          │
                                                          ▼
                                              [ WireGuard / Tailscale ]
                                                          │
                                                          ▼
                                                ╔══════════════════╗
                                                ║   Caribou Hub    ║
                                                ║   (issue #10)    ║
                                                ╚══════════════════╝
```

## Module Map — FCPC → CSU

| FCPC module | CSU module | Change |
|---|---|---|
| `FCPC.py` | `CSU.py` | Rename main orchestrator (cosmetic) |
| `Veronte.py` | `MAVLink.py` | Embention UART → pymavlink UDP listener |
| `ESC.py` + `CyphalCAN3.py` | `Hobbywing.py` | MAD Motors / Cyphal-CAN → Hobbywing CAN protocol |
| `BMS.py` + `VESCCAN.py` | `TattuBMS.py` | Ennoid / VESC-CAN → Tattu 18S (pluggable interface) |
| `server.py` + `TCP.py` | `HubLink.py` | Two-display server → single outbound stream to Hub |
| `LoRa.py` | **removed** | 4G replaces long-range link |
| `display1.py`, `display2.py`, `protocols_functions.py` | **removed** | Caribou Hub owns display |
| `Data.py` | unchanged | Log basename already renamed to `CaribouSystemLog` |
| `IO.py`, `Joystick.py` | unchanged | |

## Network Architecture

The System Unit reaches Caribou Hub through a single 4G uplink. Carrier NAT prevents inbound connections, so the CSU **dials out** through a VPN tunnel (WireGuard or Tailscale) terminated at a known endpoint. Caribou Hub connects to the same endpoint; both sides appear on a shared private network.

```
  [CSU on aircraft] ──4G──▶ [Carrier NAT] ──▶ [VPN endpoint] ◀── [Caribou Hub]
                                                   │
                                          private overlay net
                                          (all parties addressable)
```

**Outage policy:** `HubLink.py` drops frames during cellular dropouts and resumes live on reconnect. No local buffer or replay. Full-rate CSV on SSD captures continuity for post-flight analysis. Rationale: keeps Hub-side view always-current; flight data is recoverable from the onboard log.

## Threading Model

Inherited from FCPC: one Python process, one thread per data source + one for `HubLink`. `Data.py` holds the shared state behind a lock and exposes a snapshot to the logger and uplink. The main loop in `CSU.py` spawns and supervises threads.

## V1 Scope (issue #12 deliverable)

In scope:
- `MAVLink.py` — pymavlink UDP listener, attitude / GPS / battery / status streams
- `Hobbywing.py` — `can0` reader, 6-motor telemetry parse (voltage, current, temperature, RPM)
- `TattuBMS.py` — stub with a pluggable adapter, populated once Tattu comms are confirmed
- `HubLink.py` — outbound TCP stream over VPN tunnel, live-only
- `Data.py` — central store + CSV logger (already migrated)
- `CSU.py` — main loop, thread orchestration

Out of scope for V1:
- Outage buffering / replay
- Multi-FC redundancy
- Hub-side consumer (tracked in issue #10)
- LoRa fallback

## Open Questions

1. **Tattu 18S BMS comms interface** — CAN / UART / SMBus / proprietary. Drives whether `can1` is used or a different bus is needed. Blocks `TattuBMS.py` implementation past the stub.
2. **CM5 carrier compatibility** — the existing FCPC Breakout PCB was designed against CM4 / Pi 4. Confirm CM5 footprint and pin compatibility before manufacturing the next revision.
3. **VPN provider** — WireGuard (self-hosted) vs. Tailscale (managed). Tailscale is simpler operationally; WireGuard is fully under our control. Decision deferred until Caribou Hub side is in place.
4. **FC Ethernet hardware** — which ArduPilot board provides MAVLink-over-UDP natively (CubePilot Cube Orange+ on Ethernet carrier, Holybro Pixhawk 6X, etc.) vs. requiring a serial-to-Ethernet bridge.
5. **`bcm2835-1.60` dependency** — audit whether any retained module still needs it, or whether `RPi.GPIO` / `gpiozero` covers everything. Cleanup candidate.

## References

- [Caribou System Unit repo](https://github.com/Pan-Robotics/Caribou-System-Unit)
- [Feather Companion Computer (upstream)](https://github.com/Pan-Robotics/Feather-Companion-Computer)
- [FCPC Concept Doc](https://docs.google.com/document/d/15r7cTYvV1hOLt8er7vyQtWU0twEOfAIIOQ0pdE-wRtA/edit)
- [Data Network & Ground Equipment](https://docs.google.com/document/d/11VlSYsE245VFLZsYB7TvqWuuJ1UnPRjPKnnLWUzqcEM/edit)
- [Feather PT3 Ready FCPC Bounty](https://docs.google.com/document/d/1tQf7TGQPeBz3J82_hX4NeGAglC0C-ZWMout1vr6JEZM/edit)
- [Feather PT4 Ready FCPC Bounty](https://docs.google.com/document/d/1vPeeErJhhwsIkiBVpW47scY42erG127w0NTgtlc_Bck/edit)
