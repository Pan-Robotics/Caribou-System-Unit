# Caribou System Unit

Onboard companion computer for **Project Caribou**. Runs on a Raspberry Pi CM5 in the airframe, ingests telemetry from the Pixhawk 6X flight controller (MAVLink/UDP), per-arm ESCs (DroneCAN on `can0`) and per-arm BMSes (DroneCAN on `can1`), and serves it to Caribou Hub operators via an inbound WebSocket server (HubLink) over Tailscale on 4G.

> **Status:** V1 deployed and HIL-validated on the bench drone. Telemetry plane is live end-to-end; per-arm CAN listeners are deployed and waiting for production ESC/BMS hardware on the buses. Camera plane is scaffolded and ready to install on drones that have a camera.
> Tracking issue: [Arrow-air/project-caribou#12](https://github.com/Arrow-air/project-caribou/issues/12)
> Architecture: [Docs/Architecture.md](Docs/Architecture.md)
> HubLink wire spec: [Docs/HubLink_Implementation_Spec.md](Docs/HubLink_Implementation_Spec.md)

Originated as a fork of the [Feather Companion Computer (FCPC)](https://github.com/Pan-Robotics/Feather-Companion-Computer); FCPC heritage modules have been removed (see git history).

## What it does

1. **Telemetry ingest** — MAVLink over UDP from the FC (eth0 to Pixhawk 6X); DroneCAN on `can0` for 6× Hobbywing XRotor X15 ESCs; DroneCAN on `can1` for 6× per-arm BMSes.
2. **Hub service** — WebSocket server `caribou.stream.v1` on `:8765`. Hubs dial in over Tailscale, present a per-drone bearer key, receive 10 Hz telemetry frames + capability manifest. Single-writer control lease arbitrates commands across multiple connected operators.
3. **Run-as-a-service** — runs under systemd (`csu.service`) with `Restart=always`; CAN interfaces brought up at boot by `caribou-can.service`. Survives reboots, transient outages, and `kill -9` on the main process.
4. **(Optional) Camera plane** — an opt-in, independent stack (`Installation/camera/`) bridges any RTSP camera to the Hub via go2rtc + Tailscale Funnel.

## Architecture at a glance

```
  [Pixhawk 6X]      ─MAVLink/UDP─▶  MAVLink.py    ─┐
  [6× XRotor X15]   ─DroneCAN can0▶ Hobbywing.py  ─┤
  [6× per-arm BMS]  ─DroneCAN can1▶ TattuBMS.py   ─┼─▶  Data.py
                                                   ┘   (tlock-guarded)
                                                          │
                                                          ▼
                                                     HubLink.py (WS :8765)
                                                          ▲
                                                          │  Hubs dial IN
                                                  Tailscale + 4G
                                                          │
                                                  Hub #1  …  Hub #N
```

See [Docs/Architecture.md](Docs/Architecture.md) for the full diagram, threading model, security layers, Hub-side compatibility notes, and open questions.

## Hardware

| Component | Choice |
|---|---|
| Compute | Raspberry Pi CM5 |
| Carrier | [Waveshare CM5-DUAL-ETH-4G-5G-BASE](https://www.waveshare.com/cm5-dual-eth-4g-5g-base.htm) |
| CAN | [Waveshare 2-CH CAN HAT](https://www.waveshare.com/2-ch-can-hat.htm) — MCP2515 × 2 on SPI0, 12 MHz crystals, IRQs GPIO 25 / GPIO 23 |
| Flight controller | Pixhawk 6X (Ethernet to CM5 `eth0` on `192.168.144.0/24`) |
| WAN | SIM7600X-H M.2 4G LTE (AT-driven; reference in [Docs/AT_Command_Reference.docx](Docs/AT_Command_Reference.docx)) |
| ESCs | 6 × [Hobbywing XRotor X15](https://www.hobbywing.com/en/products/xrotorx15) (set to DroneCAN mode via Hobbywing DataLink) |
| BMS | 6 × per-arm packs (DroneCAN BatteryInfo target; Tattu 18S confirmation pending) |
| Optional camera | any RTSP source (gimbal / IP camera / FPV cam) |
| Enclosure | 3D-printed ([Hardware/Enclosure/](Hardware/Enclosure/)) |

## Modules

| Module | Role |
|---|---|
| [src/CSU.py](src/CSU.py) | Process entry — spawns four daemon threads, signals + shutdown |
| [src/MAVLink.py](src/MAVLink.py) | MAVSDK UDP listener; 8 parallel collectors → `Data.MAVLinkPacket` |
| [src/Hobbywing.py](src/Hobbywing.py) | DroneCAN `esc.Status` on `can0`; per-arm → `Data.ESCArms` (default node IDs 20–25) |
| [src/TattuBMS.py](src/TattuBMS.py) | DroneCAN `BatteryInfo` on `can1`; per-arm → `Data.BMSArms` (default node IDs 10–15) |
| [src/HubLink.py](src/HubLink.py) | Inbound WebSocket server (`caribou.stream.v1`), 10 Hz telemetry, control lease, capability manifest, bearer auth |
| [src/Data.py](src/Data.py) | Shared state — `tlock` + the three structured dicts above |

All configuration is via environment variables. See each module's docstring (or `Docs/Architecture.md`) for the full list.

## Deploying a Fresh Drone

```bash
# 1. clone
git clone https://github.com/Pan-Robotics/Caribou-System-Unit.git ~/Caribou-System-Unit
cd ~/Caribou-System-Unit

# 2. one-time: enable SPI + load MCP2515 overlays, then reboot
sudo ./Installation/enable_uart_spi.sh   # idempotent, reboots at end

# 3. bootstrap everything else (interactive or env-var driven)
./Installation/bootstrap_drone.sh
```

`bootstrap_drone.sh` provisions:
1. System packages (`curl`, `python3-venv`, `ca-certificates`)
2. Tailscale install + tagged join (non-ephemeral, single-use auth key)
3. Python venv at `.venv/` with `mavsdk`, `websockets`, `dronecan`
4. `~/caribou-csu.env` (`API_KEY` + `DRONE_ID`, mode `0600`)
5. `/etc/systemd/system/caribou-can.service` (`can0` 500 kbit, `can1` 1 Mbit, brought up once kernel MCP2515 devices appear)
6. `/etc/systemd/system/csu.service` (enabled, started, `Restart=always`, ordered `After=caribou-can.service`)
7. Connection-summary card with the drone's MagicDNS name + tailnet IP + stream port to type into the Hub UI

Inputs can be interactive (defaults shown in prompts) or env-var supplied for unattended fleet provisioning:

```bash
DRONE_ID=caribou_007 \
API_KEY=<per-drone-bearer> \
TS_AUTHKEY=tskey-auth-... \
./Installation/bootstrap_drone.sh
```

The Tailscale auth key should be **non-ephemeral, single-use, and tagged** (`tag:drone`, `tag:fleet-caribou` by default). Non-ephemeral so drones persist on the tailnet across reboots and outages; single-use so each drone has its own minted-and-spent key.

## Optional: Camera Streaming

If the drone has an RTSP camera and you want browser-based WebRTC playback via the Hub, install the camera stack from [Installation/camera/](Installation/camera/) **after** `bootstrap_drone.sh` has joined the tailnet. This adds three systemd units that run parallel to and independent of `csu.service` — telemetry continues unaffected if the camera is removed or broken.

```bash
sudo ./Installation/camera/install_camera_services.sh
```

What gets installed:
- **`go2rtc.service`** — [go2rtc](https://github.com/AlexxIT/go2rtc) transcodes RTSP → WebRTC; serves the WHEP signaling API on `:1984`
- **`tailscale-funnel.service`** — exposes `:1984` on a public HTTPS URL via Tailscale Funnel (signaling only; WebRTC media flows peer-to-peer over UDP)
- **`camera-stream.service`** — Python wrapper that health-checks go2rtc and POSTs the WHEP URL to the Caribou Hub's `/api/rest/camera/stream-register` every 5 minutes

The installer inherits `DRONE_ID`/`API_KEY` from `~/caribou-csu.env` and writes camera-specific values (`RTSP_URL`, `HUB_URL`, `GO2RTC_*_PORT`) to `~/caribou-camera.env`. Two env files = two independently configurable service planes.

Data-plane separation:
```
  Telemetry plane:  FC + CAN  ─▶  CSU  ─▶  HubLink   (drone-as-server, Hub dials IN over Tailscale)
  Camera plane:     RTSP cam  ─▶  go2rtc ─▶ Tailscale Funnel (public HTTPS)  ─▶  Hub WHEP proxy
```

Same Tailscale identity, distinct ports + services. A broken camera doesn't affect telemetry; removing CSU doesn't affect the camera.

## Optional: Logs & OTA Service

For remote post-flight `.BIN` log download and over-the-air firmware updates from the Hub UI, install the Logs & OTA stack from [Installation/logs-ota/](Installation/logs-ota/). Like the camera plane, this is **independent of and parallel to `csu.service`** — telemetry continues if Logs & OTA is removed or broken.

```bash
sudo ./Installation/logs-ota/install_logs_ota.sh
```

What gets installed:
- **`logs-ota.service`** — Python service that polls the Hub job queue for `scan_fc_logs`, `download_fc_log`, and `flash_firmware` jobs. Reports system diagnostics (CPU, mem, disk, temp, services) every 10 s. Streams `journalctl` output over Socket.IO when the browser asks for live log tails.
- **`firmware_puller.lua`** + **`net_webserver_put.lua`** — Lua scripts that go on the **FC's SD card** under `APM/scripts/`. The puller polls the Pi on `:8070` for available firmware; the web server (optional Tier 2 fallback) supports HTTP PUT to `/APM/`.

OTA flash path (Tier 1):
```
  Hub UI ─▶ Pi downloads .abin ─▶ Pi serves on :8070 ─▶ FC (firmware_puller.lua) pulls
                                                       ─▶ FC writes /APM/ardupilot.abin
                                                       ─▶ Pi MAVLink-reboots the FC
                                                       ─▶ FC bootloader flashes new firmware
                                                       ─▶ Pi polls FC webserver to confirm reboot
```

**Two things to know before install:**

1. **MAVLink endpoint contention.** CSU already listens on UDP `:14540`. Logs & OTA needs its own MAVSDK link for MAVFTP / arm-state / reboot, defaulting to UDP `:14550`. You'll need to configure the Pixhawk's `NET_P2_*` parameters to push a second MAVLink stream to the Pi on `:14550` (alternative: run `mavlink-router` on the Pi). See [Installation/logs-ota/OTA_Setup_Guide.md](Installation/logs-ota/OTA_Setup_Guide.md).

2. **FC SD-card payload (remote, no SD removal).** [`install_fc_scripts.py`](Installation/logs-ota/install_fc_scripts.py) pushes `firmware_puller.lua` and `net_webserver_put.lua` to the FC's `/APM/scripts/` over MAVFTP — `install_logs_ota.sh` prompts to run it as its final step. Then set `SCR_ENABLE=1`, `FWPULL_ENABLE=1`, `FWPULL_PI_IPx`, and reboot the FC via Mission Planner. The only situation that still requires touching the SD card / USB-attaching the FC is the *very first* install on a fresh Pixhawk where no `NET_*` parameters have been set yet.

The installer inherits `DRONE_ID` + `API_KEY` from `~/caribou-csu.env` and writes service-specific values (`HUB_URL`, `FC_CONNECTION`, `FC_WEBSERVER_URL`) to `~/caribou-logs-ota.env`.

## Hub-side Compatibility

The Hub's Telemetry app (`Caribou Hub/Hub Interface/client/src/components/apps/TelemetryApp.tsx`) consumes our wire format via a small normalisation function `toArmData()` that maps HubLink's nested per-arm shape `{arm_id, bms:{}, esc:{}}` → the flat shape `HexStructuralView` renders. All fields HubLink emits today are wired up Hub-side, with two minor notes:

- **`heading_deg`** is emitted by HubLink but not currently passed to `<CockpitHUD heading={…}>` in `TelemetryApp.tsx` — compass falls back to `attitude.yaw_deg`. Hub-side fix.
- **`motor_temp_c`** (motor winding temperature, distinct from ESC board temp) is now passed through by HubLink as of June 2026.

See [Docs/Architecture.md](Docs/Architecture.md#hub-side-compatibility-notes) for the full field-by-field table.

## Repo Layout

```
Caribou System Unit/
├── src/                  6 .py files: CSU, Data, MAVLink, Hobbywing, TattuBMS, HubLink
├── Docs/                 Architecture, HubLink wire spec, modem AT-command reference
├── Hardware/             2-CH CAN HAT reference + 3D-printed enclosure files
├── Installation/         bootstrap + per-step scripts + systemd units + env template
│   ├── camera/           optional RTSP camera streaming stack (go2rtc + Tailscale Funnel)
│   └── logs-ota/         optional Logs + OTA stack (FC HTTP pull, diagnostics, journalctl streaming)
├── Logs/                 post-flight CSV log parsing tools
└── Test/                 HIL fixtures (Arduino BMS/ESC dummies, CAN HAT demo)
```

## Operations Cheatsheet

```bash
# Service status / logs
sudo systemctl status csu.service caribou-can.service
sudo journalctl -u csu.service -f
sudo journalctl -u csu.service --since "5 min ago" | grep -E "(Workers|ready|connected|Hub)"

# Restart core
sudo systemctl restart csu.service

# CAN sanity
ip -br link show type can       # both UP?
candump can0                    # ESC frames flowing?
candump can1                    # BMS frames flowing?

# Tailscale + Hub reach
tailscale status
tailscale ip                    # your tailnet IP
ss -tlnp | grep 8765            # HubLink listening?
```

## References

- [Architecture proposal](Docs/Architecture.md) — full design + current state
- [HubLink Implementation Spec](Docs/HubLink_Implementation_Spec.md) — wire protocol, auth, lease, manifest
- [Caribou tracking issue](https://github.com/Arrow-air/project-caribou/issues/12)
- [Caribou Hub (sibling repo)](https://github.com/Pan-Robotics/Caribou-Hub) — the production Hub the System Unit serves
- [Feather Companion Computer (heritage upstream)](https://github.com/Pan-Robotics/Feather-Companion-Computer)
