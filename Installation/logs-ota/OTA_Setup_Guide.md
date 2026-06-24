# Caribou OTA Firmware Update — Setup & Implementation Guide

Remote Over-The-Air (OTA) firmware updates for the Pixhawk 6X flight controller via the onboard Raspberry Pi CM5. Firmware is pushed from the Caribou Hub dashboard, downloaded by the Pi, and pulled by the FC at Ethernet speed (~650 KB/s). Total flash time: ~33 seconds from job trigger to verified reboot.

This is **optional and parallel** to the core CSU telemetry stack. If you don't need OTA, you don't have to install it. `csu.service` is independent.

## Architecture

```
Hub Dashboard ─(HTTPS via Tailscale)─▶ Pi (logs_ota_service.py) downloads .abin
                                       Pi serves on HTTP :8070
FC (firmware_puller.lua) ─(HTTP GET)─▶ pulls from Pi:8070
                                       writes /APM/ardupilot.abin
Pi sends MAVLink reboot ─────────────▶ FC reboots
                                       Bootloader flashes firmware
Pi polls FC webserver ────────────────▶ FC back online ─▶ "completed"
```

Three planes coexist on the same drone:

| Plane | Service | Listens / talks |
|---|---|---|
| Telemetry (mandatory) | `csu.service` | MAVLink/UDP 14540 (in), DroneCAN on `can0`/`can1`, WebSocket `:8765` for Hubs |
| Logs & OTA (optional) | `logs-ota.service` | MAVLink/UDP 14550 (in), HTTP :8070 (out to FC), HTTPS to Hub |
| Camera (optional) | `go2rtc.service` + `camera-stream.service` + `tailscale-funnel.service` | RTSP (in), HTTP :1984 (out), Tailscale Funnel public TLS |

## FC Parameters Required

### Scripting (one-time)
| Parameter | Value | Notes |
|---|---|---|
| `SCR_ENABLE` | `1` | Lua scripting on. **Requires FC reboot to take effect.** |
| `SCR_HEAP_SIZE` | `204800` | 200 KB Lua VM heap (defaults are too small for multiple scripts) |

### Firmware Puller (`firmware_puller.lua`, Tier 1 OTA)
| Parameter | Value | Notes |
|---|---|---|
| `FWPULL_ENABLE` | `1` | Enable polling of the Pi for available firmware |
| `FWPULL_PI_IP0` | `192` | Pi's IPv4 octet 1 (matches the Pi's address on the FC subnet) |
| `FWPULL_PI_IP1` | `168` | Pi IP octet 2 |
| `FWPULL_PI_IP2` | `144` | Pi IP octet 3 (default Caribou subnet) |
| `FWPULL_PI_IP3` | `15` | Pi IP octet 4 (CM5's eth0 IP, per the Caribou architecture) |
| `FWPULL_PORT` | `8070` | Pi firmware server port |

> Verify with `hostname -I` on the Pi that you've got the right IP for the FC-facing interface (eth0, `192.168.144.15`).

### Web Server (`net_webserver_put.lua`, optional)
| Parameter | Value | Notes |
|---|---|---|
| `WEB_ENABLE` | `1` | Lua web server on |
| `WEB_BIND_PORT` | `8080` | Web server port |
| `WEB_PUT_ENABLE` | `1` | HTTP PUT uploads (Tier 2 fallback; not used in normal Tier 1 flow) |
| `WEB_MAX_UPLOAD` | `16777216` | 16 MB max |

### Networking (Caribou already configures these)
| Parameter | Value | Notes |
|---|---|---|
| `NET_ENABLE` | `1` | Ethernet enabled |
| `NET_P1_*` | matches `csu.service` (UDP 14540) | Already set during initial FC bring-up |
| `NET_P2_*` | UDP push to Pi:14550 | **New for logs-ota** — distinct from CSU's :14540 |

#### MAVLink endpoint contention

CSU's `MAVLink.py` already binds `udpin://0.0.0.0:14540` (listens for the Pixhawk's MAVLink push). `logs_ota_service.py` needs **its own** MAVSDK connection for MAVFTP / arm-state checks / reboot. By default it binds `udpin://0.0.0.0:14550`. For both to work simultaneously you must configure the Pixhawk to publish a second MAVLink output stream to UDP `:14550` via the `NET_P2_*` parameters (port, destination IP, type=2 UDP client). Alternative: run `mavlink-router` on the Pi and fan a single inbound stream out to multiple local UDP endpoints.

## Pi Configuration

### UFW firewall (if active)
The FC must be able to reach the Pi on TCP `:8070` (firmware HTTP pull). If UFW is enabled with a default DROP policy:
```bash
sudo ufw allow from 192.168.144.10 to any port 8070
```

### Python dependencies
Handled by `install_logs_ota.sh`. Manually:
```bash
pip install --break-system-packages mavsdk aiohttp python-socketio[asyncio_client] psutil requests
```

## FC SD-card payload

The two Lua scripts (`firmware_puller.lua` and `net_webserver_put.lua`) need to land in `APM/scripts/` on the FC's SD card. **You don't have to pull the SD card** — the Pi can push them over MAVFTP using [`install_fc_scripts.py`](install_fc_scripts.py):

```bash
python3 Installation/logs-ota/install_fc_scripts.py \
    --fc-connection udpin://0.0.0.0:14550 \
    --reboot
```

`install_logs_ota.sh` calls this as an optional final step (it'll prompt you "Push Lua scripts to FC now? [y/N]"). You can also invoke it standalone any time you need to update or re-push the scripts.

What it does, in order:
1. Opens an MAVSDK connection to the FC on the supplied `--fc-connection`.
2. Ensures `/APM/scripts/` exists (idempotent — silently skips if already there).
3. Lists what's already in `/APM/scripts/` so you see the before/after.
4. MAVFTP-uploads each `.lua` file, reporting per-file progress.
5. With `--reboot`, fires a MAVLink reboot so the scripts register their `FWPULL_*` / `WEB_*` parameter tables on the next boot.

**Chicken-and-egg note for fresh FCs:** if you've never set `NET_*` parameters on the Pixhawk (so it has no MAVLink-over-Ethernet at all), MAVFTP can't reach it from the Pi. For that very first install, plug the FC into Mission Planner over USB and set `SCR_ENABLE=1` + `NET_ENABLE=1` + `NET_P1_*` (and ideally `NET_P2_*` too) over the USB MAVLink. Once Ethernet MAVLink is working, every subsequent script update is remote via `install_fc_scripts.py`.

**Endpoint contention if `csu.service` is running:** `csu.service` owns `udpin://0.0.0.0:14540`. To push scripts at the same time, either:
- Configure the Pixhawk's `NET_P2_*` to push a second MAVLink stream to `udpin://0.0.0.0:14550` (recommended; both services then coexist).
- Or `sudo systemctl stop csu.service`, run `install_fc_scripts.py --fc-connection udpin://0.0.0.0:14540`, then `sudo systemctl start csu.service`.

The end state on the FC:
```
APM/
├── scripts/
│   ├── firmware_puller.lua          (Tier 1 OTA pull, REQUIRED for OTA)
│   ├── net_webserver_put.lua        (Tier 2 PUT support, optional)
│   └── (any other Lua scripts you already have)
├── ardupilot.abin                    (written by puller, consumed by bootloader)
```

## Pi installation

After `bootstrap_drone.sh` has joined the tailnet and `csu.service` is running:

```bash
sudo ./Installation/logs-ota/install_logs_ota.sh
```

The installer:
1. pip-installs `mavsdk`, `aiohttp`, `python-socketio`, `psutil`, `requests`
2. Inherits `DRONE_ID` + `API_KEY` from `~/caribou-csu.env`
3. Prompts for `HUB_URL` and FC connection type (default UDP listener on `:14550`)
4. Writes `~/caribou-logs-ota.env` (`HUB_URL`, `FC_CONNECTION`, `FC_WEBSERVER_URL`)
5. Creates `/var/lib/caribou/fc_logs/` for the local log cache
6. Installs `/etc/systemd/system/logs-ota.service` (`After=csu.service`)
7. Enables + starts the service

## Monitored services

`DiagnosticsCollector` periodically reports the status of these systemd units to the Hub:

- `csu.service`
- `caribou-can.service`
- `logs-ota.service`
- `camera-stream.service`
- `go2rtc.service`
- `tailscale-funnel.service`
- `tailscaled.service`

Each is reported as `active` / `inactive` / `failed` / `unknown` in the diagnostics payload.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `FWPull: connect failed` GCS messages | UFW blocking, wrong `FWPULL_PI_IPx`, Pi not on Pixhawk subnet | `sudo ufw status \| grep 8070`; verify Pi IP via `hostname -I`; ping Pi from a Mission Planner shell |
| `firmware_puller.lua` not loading | `SCR_ENABLE=0` or file not in `APM/scripts/` | Set `SCR_ENABLE=1`, reboot, copy the script |
| Service starts but no FC connection | Pixhawk not pushing to `:14550` | Set `NET_P2_TYPE=2` (UDP client), `NET_P2_IP*` to Pi, `NET_P2_PORT=14550`, reboot FC |
| `MAVFTP sequence corruption` (`Ignore: last: X, req: Y`) | Mixed Tier-1/Tier-3 in one MAVSDK session | Restart `logs-ota.service` for a fresh session; the Caribou flow is Tier 1 only by default |
| Flash stuck at ~65% / no stage transitions | Old code path that polled bootloader stage files | Already fixed: the service now reboots the FC and polls the webserver for the FC to come back online |
| `logs-ota.service` running but no Hub logs | `HUB_URL` unreachable from the Pi | Check the URL inherits cleanly: `cat ~/caribou-logs-ota.env`; curl it from the Pi |
| Pi can't reach Hub on Tailscale | Tailnet ACL hasn't been updated | Add `tag:drone -> tag:hub-operator:443` (or whatever your Hub port is) |
