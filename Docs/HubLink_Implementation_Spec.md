# HubLink.py — Implementation Spec (Caribou System Unit ↔ Caribou Hub)

**Audience:** the developer/Claude session building `HubLink.py` in this (System
Unit) repo. **This document is self-contained** — you do not need the Hub repo to
implement against it. A working reference implementation already exists in the Hub
repo at `companion_scripts/hublink_service.py`; if you can get it, copy its
structure and swap the telemetry source. If you can't, this spec is enough.

**Status of the Hub side:** fully built, tested, and on the Hub's `dev` branch. The
Hub already speaks this protocol (outbound subscriber + control lease + capability
manifest). HubLink just has to implement the drone side described here.

---

## 0. The one thing to get right first: HubLink is a SERVER, not a client

The current System Unit README/Architecture describe HubLink as an **outbound TCP
stream** ("live TCP stream to the Hub"). **That is superseded by this spec.** The
data plane was inverted:

> **HubLink is a WebSocket _server_ that runs on the drone. The Hub connects _in_
> and pulls.**

Why: drones and Hubs are never on the same network (4G/CGNAT), and *multiple* Hubs
(operators) may watch the *same* drone. Making the drone a service that any
authorised Hub pulls from is what makes multi-operator monitoring fall out for
free, and it matches how the camera (go2rtc) already works. Reachability is
provided by Tailscale (the drone has a stable MagicDNS name); the Hub dials it.

```
        ┌─────────── Caribou System Unit (this repo) ───────────┐
  FC ───▶ MAVLink.py  ─┐                                        │
 ESCs ──▶ Hobbywing.py ─┼─▶ Data.py (central state) ─▶ HubLink.py  ◀─── WS ──── Hub #1
 BMS ───▶ TattuBMS.py  ─┘                              (WS SERVER, :8765)  ◀── WS ── Hub #2
        └───────────────────────────────────────────────────────┘     over Tailscale (4G)
```

HubLink **replaces** `server.py` + `TCP.py`. It reads the already-aggregated state
out of `Data.py` and serves it; it does not open outbound connections (except
Tailscale itself, which is a separate system service).

---

## 1. Where it sits in the CSU process

- Runs as one thread/async task inside the main CSU process (alongside the MAVLink,
  Hobbywing, TattuBMS, and `Data.py` workers), **or** as its own systemd service
  that imports the same `Data` instance — your call based on the threading model.
- It **only reads** `Data.py` state (`Data.VerontePacket`, `Data.BMSLog`,
  `Data.ESCLog`, etc.) under `Data.tlock`. It never blocks the data threads.
- It owns the WebSocket server, one connection handler per connected Hub, the
  single control-lease state, and the capability manifest.

Recommended library: **`websockets`** (`pip install "websockets>=12"`), asyncio.
The whole service is ~250 lines (see the reference).

---

## 2. Transport, endpoint, and auth

| Item | Value |
|---|---|
| Protocol | WebSocket, **JSON text frames**, UTF-8, one connection per Hub |
| Bind | `0.0.0.0:8765` (reachable on the drone's Tailscale interface). Port configurable via `STREAM_PORT`. |
| Path | the server accepts any path; the Hub dials `ws://<drone>.<tailnet>.ts.net:8765/stream?drone_id=<id>` |
| App subprotocol | **`caribou.stream.v1`** — the server must offer and negotiate this |
| Auth | the Hub presents the drone's **per-drone API key** in the WS subprotocol list as **`bearer.<API_KEY>`** |

**Auth handling (important):**
- The client offers `Sec-WebSocket-Protocol: caribou.stream.v1, bearer.<key>`.
- Configure the WS server with `subprotocols=["caribou.stream.v1"]` so it
  auto-negotiates the app subprotocol.
- In the handler, read the offered subprotocols, extract the `bearer.<...>` value,
  and compare it to the expected API key (`API_KEY` env). On mismatch, **close with
  code 1008** (policy violation) and drop the connection.
- The API key is the **same secret** the drone would have used to authenticate to
  the Hub in the old push mode. It is provisioned per drone by the Hub operator.
- If `API_KEY` is unset, accept any bearer (benchtop only) and log a loud warning.

> Two layers of security: Tailscale ACLs decide *which Hubs can even reach
> :8765*; the API key decides *which drone's data a Hub may pull*. Don't put the
> key in the URL — it rides in the subprotocol so it never lands in logs/paths.

---

## 3. Frames — drone → Hub (what HubLink SENDS)

Every frame is a JSON object with a string `type`. **Never put `drone_id` in a
frame** — the Hub injects it from the authenticated connection (so a buggy drone
can't spoof another's data).

### 3.1 `hello` — send once, immediately on connect
```json
{ "type": "hello", "protocol": 1, "services": ["telemetry", "control"] }
```

### 3.2 `manifest` — send right after `hello`, and on `get_manifest` (see §6)
```json
{ "type": "manifest", "payloads": [ /* see §6 */ ] }
```

### 3.3 `telemetry` — send at the telemetry rate (10 Hz typical)
```json
{ "type": "telemetry", "timestamp": "2026-06-16T12:00:00.000Z", "telemetry": { /* §4 */ } }
```
- `timestamp` is ISO-8601 UTC and **optional** (the Hub stamps server time if you
  omit it). Including it is preferred.
- `telemetry` is the object specified in **§4** — same shape the Hub's REST ingest
  and the legacy `telemetry_forwarder.py` used. The Hub persists it, updates the
  drone's "last seen", and broadcasts it to browsers.

### 3.4 `pong` — reply to the Hub's `ping`
```json
{ "type": "pong", "ts": 1718539200000 }
```

### 3.5 (optional) `camera_status`, `pointcloud`
Only if this unit produces them. Camera is normally served separately by go2rtc, so
most units won't send these. Shapes:
```json
{ "type": "camera_status", "timestamp": 1718539200000, "connected": true,
  "recording": false, "zoom_level": 1 }          // timestamp = epoch ms, optional
{ "type": "pointcloud", "timestamp": "<ISO>", "points": [ ... ], "stats": { ... } }
```

### 3.6 Control replies — see §5
`lease_granted`, `lease_denied`, `lease_revoked`, `lease_released`, `command_result`.

> **Forward-compatibility:** the Hub ignores unknown frame types and drops
> malformed frames without dropping the connection. You can add fields freely.

---

## 4. Telemetry payload shape (the `telemetry` object)

This is the contract the Hub UI renders. Emit exactly these keys; use `null` for a
block you don't have yet. All fields are plain numbers unless noted.

```jsonc
{
  "attitude": {                      // null if unknown
    "roll_deg":  0.0,
    "pitch_deg": 0.0,
    "yaw_deg":   0.0,                // 0..360
    "timestamp": "<ISO>"
  },
  "position": {                      // null if no GPS fix
    "latitude_deg":  51.5074,        // DECIMAL degrees
    "longitude_deg": -0.1278,        // DECIMAL degrees
    "absolute_altitude_m": 100.0,    // AMSL
    "relative_altitude_m": 50.0,     // AGL / above home
    "timestamp": "<ISO>"
  },
  "gps": { "num_satellites": 14, "fix_type": 3, "timestamp": "<ISO>" },
  "battery_fc": {                    // the FC's own battery estimate
    "voltage_v": 22.2,
    "remaining_percent": 87.0,
    "timestamp": "<ISO>"
  },
  "battery_uavcan": {                // aggregate pack view (optional)
    "battery_id": 0, "voltage_v": 22.2, "current_a": 12.0,
    "temperature_k": 305.0, "state_of_charge_pct": 87.0, "timestamp": "<ISO>"
  },
  "in_air": true,

  // Optional extras the Hub HUD/cards use if present:
  "flight_mode": "AUTO",
  "airspeed_ms": 0.0,
  "vertical_speed_ms": 0.0,
  "heading_deg": 0.0,

  // Optional per-arm structural view (Caribou is a hexarotor: 6 arms).
  // Each entry = one arm's BMS + ESC. Include if you have per-arm data.
  "arms": [
    { "arm_id": 1,
      "bms": { "voltage_v": 22.2, "current_a": 12.0, "temperature_c": 30.0, "soc_pct": 87.0 },
      "esc": { "rpm": 4200, "voltage_v": 22.2, "current_a": 11.0, "temperature_c": 45.0 } }
    // … arms 2..6
  ]
}
```

### 4.1 Mapping from this repo's `Data.py`

`Data.py` already aggregates the hardware. Build the `telemetry` object from it
under `Data.tlock`. Field names differ — map them:

| Hub telemetry field | Source in `Data.py` (current FCPC field → CSU V1) |
|---|---|
| `attitude.roll_deg` / `pitch_deg` | `VerontePacket['attitude_roll']` / `['attitude_pitch']` (→ MAVLink ATTITUDE in CSU V1) |
| `attitude.yaw_deg` | `VerontePacket['heading']` or `['compass']` |
| `position.latitude_deg` / `longitude_deg` | `VerontePacket['latitude']` / `['longitude']` — **convert to decimal degrees** (the current FCPC strings like `'40d26a46q'` are not the wire format; emit floats) |
| `position.absolute_altitude_m` | `VerontePacket['altitude_ABS']` |
| `position.relative_altitude_m` | `VerontePacket['altitude_AGL']` |
| `gps.*` | MAVLink `GPS_RAW_INT` (sats, fix type) in CSU V1 |
| `battery_fc.*` | FC `SYS_STATUS`/`BATTERY_STATUS`, or derived from packs |
| `arms[i].bms.*` | `Data.BMSLog['<i>']` → `SOC`, `packVoltage`, `packCurrent*`, `tBattHi` (note FCPC values are raw/scaled — apply real scaling in CSU V1) |
| `arms[i].esc.*` | `Data.ESCLog['<i>']` → `info_upload_6160.electrical_speed` (rpm), `info_upload_6161.bus_voltage`, `bus_current`, `temperatures.Motor/MOS` |
| `in_air` | derive from FC armed + altitude/airspeed, or MAVLink `EXTENDED_SYS_STATE` |

> Start with `attitude` + `position` + `gps` + `battery_fc` + `in_air` (drives the
> cockpit HUD and map). Add `arms[]` once the per-arm BMS/ESC scaling is finalised.
> Send `null` for blocks not wired yet — the UI handles partial telemetry.

---

## 5. Control lease — drone is the single-writer arbiter

Monitoring is many-Hubs-read; **control is single-writer**. HubLink is the
authority because it is the only node every Hub shares. At most **one** Hub holds
the lease at a time; only that Hub's `command`s are executed.

### 5.1 State to keep (server-wide, shared across all connection handlers)
```
lease = None  or  { id, hub_id, websocket, expires_at_ms }
LEASE_TTL_MS = 30000   # env-configurable
```

### 5.2 Inbound control frames (Hub → drone) and required behaviour

| Inbound frame | Behaviour |
|---|---|
| `{"type":"lease_acquire","hub_id":"<id>","request_id":"<r>"}` | If no active lease, or it is expired, or it is already held by the **same** `hub_id` (renew): mint a new `lease_id`, set `expires_at = now + TTL`, remember the holder's websocket, and reply **`lease_granted`**. Otherwise reply **`lease_denied`** with `held_by = <current holder hub_id>`. |
| `{"type":"lease_heartbeat","lease_id":"<id>"}` | If it matches the active lease, extend `expires_at = now + TTL`. No reply needed. |
| `{"type":"lease_release","lease_id":"<id>","request_id":"<r>"}` | If it matches, clear the lease and reply **`lease_released`**. |
| `{"type":"command","lease_id":"<id>","request_id":"<r>","action":"...","params":{...}}` | Execute **only if** `lease_id` matches the active lease **and** the frame arrived on the holder's connection. Then dispatch to the payload and reply **`command_result {ok:true,result:{...}}`**. Otherwise reply **`command_result {ok:false,error:"no_control"}`**. |
| `{"type":"ping","ts":<n>}` | reply `pong` (§3.4) |
| `{"type":"get_manifest"}` | reply `manifest` (§6) |

### 5.3 Outbound control replies (drone → Hub)
```json
{ "type": "lease_granted",  "lease_id": "<id>", "ttl_ms": 30000, "request_id": "<r>" }
{ "type": "lease_denied",   "held_by": "<hub_id>", "reason": "held", "request_id": "<r>" }
{ "type": "lease_revoked",  "lease_id": "<id>", "reason": "expired" }
{ "type": "lease_released", "lease_id": "<id>", "request_id": "<r>" }
{ "type": "command_result", "request_id": "<r>", "ok": true, "result": { /* any */ } }
```

### 5.4 Expiry & disconnect (critical for safety)
- Run a **1 Hz background task**: if `lease and lease.expires_at_ms <= now`, clear
  the lease and send **`lease_revoked`** to the (former) holder if still connected.
  This means a Hub that crashes or loses 4G **automatically** loses control, so
  another operator can take over.
- On a connection close, **if that websocket held the lease, clear it.**
- `hub_id` is just an opaque string the Hub sends (e.g. `hub-<hostname>`); store and
  echo it, don't interpret it.

### 5.5 Commands
- A command is `{action: string, params: object}`. Dispatch `action` to the payload
  subsystem (camera/winch/etc.). The set of valid actions is what you advertise in
  the manifest (§6).
- Return a small `result` object (`{accepted:true}` is fine) or an `error` string.
- The Hub also rejects actions not in the manifest **before** sending, so you mainly
  need to guard against stale lease ids and unknown actions defensively.

---

## 6. Capability manifest — typed per-payload commands

Because loadouts are heterogeneous, the drone advertises *what it can do*. The Hub
renders typed command forms from this and rejects unknown actions client-side.

Send a `manifest` frame after `hello`, and whenever the Hub sends `get_manifest`
(and ideally again if a payload is hot-swapped):

```json
{
  "type": "manifest",
  "payloads": [
    {
      "id": "camera",
      "name": "Gimbal Camera",
      "commands": [
        { "action": "set_zoom", "label": "Set Zoom",
          "params": [ { "name": "level", "type": "number", "min": 1, "max": 10, "step": 1, "required": true } ] },
        { "action": "set_mode", "label": "Set Mode",
          "params": [ { "name": "mode", "type": "enum", "options": ["photo","video","night"], "required": true } ] },
        { "action": "start_recording", "label": "Start Recording", "params": [] }
      ]
    },
    { "id": "winch", "name": "Payload Winch",
      "commands": [
        { "action": "deploy",  "label": "Deploy", "params": [ { "name": "length_m", "type": "number", "min": 0, "max": 50, "step": 0.5, "required": true } ] },
        { "action": "retract", "label": "Retract", "params": [] }
      ] }
  ]
}
```

**Param types:** `number` (`min`/`max`/`step`), `string`, `boolean`, `enum`
(`options[]`). Each param may carry `label`, `required`, `default`. Build this list
from the actually-detected loadout. Keep `action` strings stable — they are the
contract used by `command` frames.

---

## 7. Configuration

Environment variables (match the reference so configs are portable):

| Var | Default | Meaning |
|---|---|---|
| `DRONE_ID` | `caribou_001` | informational/logging |
| `API_KEY` | (empty) | expected bearer key; empty = accept-any (benchtop only) |
| `STREAM_HOST` | `0.0.0.0` | bind address |
| `STREAM_PORT` | `8765` | listen port |
| `UPDATE_RATE_HZ` | `10` | telemetry emit rate |
| `LEASE_TTL_MS` | `30000` | lease lifetime without a heartbeat |

The `API_KEY` is created in the Hub's **Drone Configuration → API Keys** for this
drone and copied to the unit (env file / secret).

---

## 8. Concurrency model (asyncio)

- One `websockets.serve(handler, host, port, subprotocols=["caribou.stream.v1"])`.
- Per connection: authenticate → send `hello` + `manifest` → run two tasks
  concurrently: a **send loop** (telemetry at `UPDATE_RATE_HZ`) and a **receive
  loop** (handle ping/get_manifest/lease/command). End the connection when either
  finishes.
- One background **expiry loop** (1 Hz) for lease revocation.
- Lease state is shared instance state; asyncio is single-threaded so no locks are
  needed for the lease itself. Reading `Data.py` from the async context must take
  `Data.tlock` briefly (it's a `threading.Lock`; hold it only to copy values out).

---

## 9. Tailscale & systemd (deployment)

- The unit joins the tailnet with a **tagged ephemeral auth key**:
  `tailscale up --authkey=<key> --advertise-tags=tag:drone,tag:fleet-<name>`.
  After that it has a stable MagicDNS name the Hub dials. (The Hub repo's
  `infra/tailscale/setup-drone-tailscale.sh` does exactly this if you have it.)
- Run HubLink under systemd: `Restart=always`, `After=network-online.target
  tailscaled.service`, env from a file. Mirror the existing CSU service units.
- **Do not** Funnel :8765 publicly — it stays tailnet-private. (Camera/go2rtc is the
  only thing that ever optionally used Funnel, and only for benchtop.)

---

## 10. Acceptance criteria (how to know it's done)

Test against a real Hub (someone runs the Hub locally or on its tailnet):

1. **Register the drone + make an API key** in the Hub's Drone Configuration; put
   that key in `API_KEY`.
2. In the Hub, set the drone to **pull mode**: Drone Configuration → *Data Plane &
   Control* → mode = **Pull**, host = the drone's `<name>.<tailnet>.ts.net` (or IP
   on the benchtop), port = 8765, Save. (Equivalent tRPC: `drones.setConnection`.)
3. ✅ **Telemetry**: the Hub's telemetry views/HUD/map populate live. Wrong key →
   the Hub's connection is refused (you close 1008).
4. ✅ **Manifest**: the *Data Plane & Control* card lists your payloads and their
   typed command forms.
5. ✅ **Control**: click **Acquire** → you grant the lease. A *second* Hub/operator
   acquiring is **denied** (held_by you). Sending a command without the lease →
   `no_control`; with it → executed + `command_result ok`.
6. ✅ **Failover**: kill HubLink's network/heartbeat → after `LEASE_TTL_MS` the
   lease is revoked and another Hub can acquire. Reconnect → telemetry resumes.

A self-contained smoke test without the full Hub: connect a WS client that offers
`["caribou.stream.v1","bearer.<key>"]`, and assert you receive `hello` + `manifest`
+ `telemetry`, that `lease_acquire` → `lease_granted`, and that a `command` with the
granted `lease_id` → `command_result ok`.

---

## 11. Implementation checklist

- [ ] Add `websockets` to `Installation/` deps.
- [ ] `HubLink.py`: WS server on `STREAM_HOST:STREAM_PORT`, subprotocol
      `caribou.stream.v1`, bearer-key auth (close 1008 on mismatch).
- [ ] On connect: send `hello` then `manifest`.
- [ ] Send loop: build the §4 telemetry object from `Data.py` (under `tlock`) and
      emit at `UPDATE_RATE_HZ`.
- [ ] Receive loop: handle `ping`→`pong`, `get_manifest`→`manifest`, and the four
      lease/command frames per §5.2.
- [ ] Shared lease state + 1 Hz expiry/revoke task + clear-on-disconnect.
- [ ] Build the manifest (§6) from the real loadout; dispatch `command.action` to
      the payload subsystems.
- [ ] systemd unit + Tailscale tagged-ephemeral join.
- [ ] Update this repo's README/Architecture: HubLink is an **inbound WS server**,
      not an outbound TCP client (supersedes the current "live TCP stream" wording).
- [ ] Verify against the Hub per §10.

---

## 12. Reference implementation

The Hub repo ships a complete, runnable drone-side reference:
`companion_scripts/hublink_service.py` (with `--demo` it emits synthetic telemetry
and a demo camera+winch manifest). It implements **every frame and rule in this
spec**. The wire protocol is identical between it and production HubLink — the only
difference is the telemetry source (demo generator vs. this repo's `Data.py`) and
the real payload command dispatch. The authoritative wire contract lives in the Hub
repo at `docs/architecture/Caribou_Drone_Stream_Protocol.md`
(§9 = control lease, §10 = capability manifest) and
`docs/architecture/Tailscale_Network_Architecture.md` (topology, ACLs, addressing).
If you have access to that repo, start by copying `hublink_service.py` and
replacing `DemoTelemetrySource` with a `Data.py`-backed source.
