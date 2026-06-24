# Caribou System Unit

Onboard telemetry handler for **Project Caribou**. Collects flight data from the flight controller, ESCs, and BMS; serves it to Caribou Hub operators via an inbound WebSocket server (HubLink) over Tailscale on 4G.

> **Status:** Phase 1 — implementation complete, awaiting full HIL validation against the airframe-mounted hardware.
> Tracking issue: [Arrow-air/project-caribou#12](https://github.com/Arrow-air/project-caribou/issues/12)
> Full architecture: [Docs/Architecture.md](Docs/Architecture.md)
> HubLink wire spec: [Docs/HubLink_Implementation_Spec.md](Docs/HubLink_Implementation_Spec.md)

Originated as a fork of the [Feather Companion Computer (FCPC)](https://github.com/Pan-Robotics/Feather-Companion-Computer); the protocol handlers, uplink, and data plane have been swapped out for Caribou hardware. The FCPC heritage modules have been removed — see git history if you need to consult the original code.

## Functions

1. **Data ingestion** — MAVLink (FC over UDP/Ethernet), DroneCAN (`can0` ESCs, `can1` BMS)
2. **Hub service** — WebSocket server (`caribou.stream.v1`) on `:8765` that Hubs dial into over Tailscale; single-writer control lease + capability manifest
3. **Run-as-a-service** — runs under systemd (`csu.service`) with `Restart=always`; CAN interfaces brought up at boot by `caribou-can.service`

## Architecture (overview)

```
  [ArduPilot FC]      ──MAVLink/UDP──▶  MAVLink.py    ─┐
  [6x Hobbywing X15]  ──CAN0────────▶  Hobbywing.py  ─┤
  [6x BMS (Tattu)]    ──CAN1────────▶  TattuBMS.py   ─┼─▶  Data.py
                                                      ┘
                                                          │
                                                          ▼
                                                     HubLink.py (WS :8765)
                                                          ▲
                                                          │  Hubs dial IN
                                                  Tailscale + 4G
                                                          │
                                                  Hub #1  …  Hub #N
```

See [Docs/Architecture.md](Docs/Architecture.md) for the full diagram, network topology, and open questions.

## Hardware

| Component | Choice |
|---|---|
| Compute | Raspberry Pi CM5 |
| Carrier | Waveshare CM5-DUAL-ETH-4G-5G-BASE (dual GbE, M.2 4G slot) |
| CAN | Waveshare 2-CH CAN HAT (MCP2515 ×2; [Hardware/PCBs/2-CH CAN HAT drawing/](Hardware/PCBs/2-CH%20CAN%20HAT%20drawing/)) |
| WAN | SIM7600X-H M.2 (AT-driven, ref [Docs/AT_Command_Reference.docx](Docs/AT_Command_Reference.docx)) |
| Enclosure | 3D-printed ([Hardware/Enclosure/](Hardware/Enclosure/)) |

## Modules

| Module | Role |
|---|---|
| [src/CSU.py](src/CSU.py) | Main entry point — spawns four daemon threads, handles SIGTERM/SIGINT |
| [src/MAVLink.py](src/MAVLink.py) | ArduPilot ingestion over UDP via MAVSDK; populates `Data.MAVLinkPacket` |
| [src/Hobbywing.py](src/Hobbywing.py) | 6× XRotor X15 ESC telemetry via DroneCAN `esc.Status` on `can0`; populates `Data.ESCArms`. X15 must be in DroneCAN mode (it supports HWCAN+DroneCAN dual) via the Hobbywing DataLink tool. |
| [src/TattuBMS.py](src/TattuBMS.py) | Per-arm BMS via DroneCAN `BatteryInfo` on `can1`; populates `Data.BMSArms` |
| [src/HubLink.py](src/HubLink.py) | Inbound WebSocket server (`caribou.stream.v1`), single-writer control lease + capability manifest |
| [src/Data.py](src/Data.py) | Shared in-process state; `tlock` + the three structured dicts above |

## Deploying to a Fresh Drone

A new CM5 is brought online with one script:

```bash
git clone https://github.com/Pan-Robotics/Caribou-System-Unit.git ~/Caribou-System-Unit
cd ~/Caribou-System-Unit
./Installation/bootstrap_drone.sh
```

The script provisions:
1. System packages
2. Tailscale (`tailscale up` with the supplied auth key + drone tags)
3. A Python venv at `.venv/` with `mavsdk`, `websockets`, `dronecan`
4. `~/caribou-csu.env` (`API_KEY` + `DRONE_ID`, mode `0600`)
5. `/etc/systemd/system/caribou-can.service` (boots `can0`/`can1` once the kernel devices appear)
6. `/etc/systemd/system/csu.service` (enabled + started, `Restart=always`, ordered `After=caribou-can.service`)
7. A connection-summary card with the drone's MagicDNS name + tailnet IP + stream port to type into the Hub UI

Inputs can be interactive (defaults shown in prompts) or env-var supplied for unattended fleet provisioning:

```bash
DRONE_ID=caribou_007 \
API_KEY=<per-drone-bearer> \
TS_AUTHKEY=tskey-auth-... \
./Installation/bootstrap_drone.sh
```

The Tailscale auth key should be **non-ephemeral, single-use, and tagged** (`tag:drone`, `tag:fleet-caribou` by default). Drones persist on the tailnet across reboots and outages.

**Pre-flight on the CM5 only:** the MCP2515 CAN HAT overlays need a one-time write to `/boot/firmware/config.txt` + reboot. Run `./Installation/enable_uart_spi.sh` (idempotent; reboots at the end) before the first `bootstrap_drone.sh`.

## Optional: Camera Streaming

If the drone has an RTSP camera and you want browser-based WebRTC playback via the Hub, install the camera stack from [Installation/camera/](Installation/camera/) **after** `bootstrap_drone.sh` has joined the tailnet. This adds three systemd units that run parallel to and independent of `csu.service` — telemetry continues unaffected if the camera is removed or broken.

```bash
sudo ./Installation/camera/install_camera_services.sh
```

What gets installed:
- **`go2rtc.service`** — [go2rtc](https://github.com/AlexxIT/go2rtc) binary transcodes RTSP → WebRTC; serves the WHEP signaling API on `:1984`
- **`tailscale-funnel.service`** — exposes `:1984` on a public HTTPS URL via Tailscale Funnel (signaling only; WebRTC media flows peer-to-peer over UDP)
- **`camera-stream.service`** — Python wrapper that health-checks go2rtc and POSTs the WHEP URL to the Caribou Hub's `/api/rest/camera/stream-register` every 5 minutes

The installer inherits `DRONE_ID`/`API_KEY` from `~/caribou-csu.env` (set by `bootstrap_drone.sh`) and writes camera-specific values (`RTSP_URL`, `HUB_URL`, `GO2RTC_*_PORT`) to `~/caribou-camera.env`. The two env files keep the two service planes independently configurable.

Data plane separation:
```
  Telemetry plane:   FC + CAN  -> CSU  -> HubLink (drone-as-server, Hub dials in over Tailscale)
  Camera plane:      RTSP cam  -> go2rtc -> Tailscale Funnel (public HTTPS) -> Hub WHEP proxy
```

## Repo Layout

```
Caribou System Unit/
├── src/          application code (CSU, MAVLink, Hobbywing, TattuBMS, HubLink, Data)
├── Docs/         architecture, HubLink wire spec, modem AT-command reference
├── Hardware/     PCB references (2-CH CAN HAT) and 3D-printed enclosure files
├── Installation/ bootstrap + per-step shell scripts + systemd unit files + env template
│   └── camera/  optional RTSP-camera streaming stack (go2rtc + Tailscale Funnel + Hub registration)
├── Logs/         post-flight CSV log parsing tools
└── Test/         HIL fixtures (Arduino BMS/ESC dummies, CAN HAT demo)
```

## References

- [Architecture proposal](Docs/Architecture.md) — full design doc for issue #12
- [HubLink Implementation Spec](Docs/HubLink_Implementation_Spec.md) — wire protocol, auth, lease, manifest
- [Caribou tracking issue](https://github.com/Arrow-air/project-caribou/issues/12)
- [Feather Companion Computer (heritage upstream)](https://github.com/Pan-Robotics/Feather-Companion-Computer)
