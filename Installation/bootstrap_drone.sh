#!/bin/bash
# =============================================================================
# Caribou System Unit - Fresh Drone Bootstrap
# =============================================================================
# One-shot provisioning for a fresh Raspberry Pi CM5. Run this on the drone
# once; it sets up everything needed to come online and serve telemetry to
# the Caribou Hub:
#
#   1. System packages (curl, ca-certificates, python3-venv)
#   2. Tailscale install + tailnet join (drone-tagged, non-ephemeral)
#   3. Python venv with mavsdk + websockets
#   4. ~/caribou-csu.env (API_KEY + DRONE_ID, mode 0600)
#   5. /etc/systemd/system/csu.service installed + enabled + started
#   6. Connection summary card for the Hub operator
#
# Inputs can be supplied either interactively or via env vars (for unattended
# fleet provisioning):
#
#   DRONE_ID    = the drone identifier; also drives the tailnet hostname
#                 (underscores rewritten to hyphens). Default: caribou_001
#   API_KEY     = per-drone Hub API key. Required for production; empty
#                 falls back to "accept any bearer" benchtop mode.
#   TS_AUTHKEY  = Tailscale auth key for this drone. Should be:
#                   - non-ephemeral (drones persist across power cycles)
#                   - single-use (one drone per key)
#                   - tagged with tag:drone (and a fleet tag if you use them)
#                 If not set, the script falls back to interactive `tailscale
#                 up` (prints a login URL to click).
#   TS_TAGS     = comma-separated tag list to advertise. Only takes effect
#                 if TS_AUTHKEY is set with those tags pre-authorised in
#                 your tailnet ACL. Default: tag:drone,tag:fleet-caribou
#   TS_LOGIN_SERVER = override the coordination server (e.g. for Headscale).
#                     Default: https://controlplane.tailscale.com
#
# Idempotent: re-runs are safe; each step skips if already done.
#
# Usage:
#   chmod +x Installation/bootstrap_drone.sh
#   ./Installation/bootstrap_drone.sh
#
# Or unattended:
#   DRONE_ID=caribou_007 API_KEY=... TS_AUTHKEY=tskey-... \
#     ./Installation/bootstrap_drone.sh
# =============================================================================

set -e

# ── Pretty output ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

step() { echo -e "${GREEN}[${1}/6] ${2}${NC}"; }
info() { echo -e "${CYAN}  ${1}${NC}"; }
ok()   { echo -e "${GREEN}  ✓ ${1}${NC}"; }
warn() { echo -e "${YELLOW}  ! ${1}${NC}"; }
fail() { echo -e "${RED}  ✗ ${1}${NC}" >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="$(id -un)"
ENV_FILE="$HOME/caribou-csu.env"
VENV_DIR="$REPO_DIR/.venv"
UNIT_PATH="/etc/systemd/system/csu.service"

# ── Collect inputs ───────────────────────────────────────────────────────────
echo -e "${BOLD}========================================${NC}"
echo -e "${BOLD} Caribou System Unit - Drone Bootstrap${NC}"
echo -e "${BOLD}========================================${NC}"
echo "  Repo:         $REPO_DIR"
echo "  Service user: $SERVICE_USER"
echo

if [ -z "${DRONE_ID:-}" ]; then
    read -r -p "DRONE_ID [caribou_001]: " DRONE_ID
    DRONE_ID="${DRONE_ID:-caribou_001}"
fi

# Tailscale hostnames disallow underscores; map them to hyphens.
TS_HOSTNAME="$(echo "$DRONE_ID" | tr '_' '-')"

if [ -z "${API_KEY+x}" ]; then
    echo
    echo "API_KEY is the per-drone bearer issued by the Hub"
    echo "(Drone Configuration -> API Keys -> Generate). Leave empty for"
    echo "benchtop accept-any mode."
    read -r -p "API_KEY: " API_KEY
fi

if [ -z "${TS_AUTHKEY+x}" ]; then
    echo
    echo "TS_AUTHKEY is the Tailscale auth key (tskey-auth-...). It should"
    echo "be non-ephemeral, single-use, and tagged for this drone. Leave"
    echo "empty to use interactive browser auth instead."
    read -r -p "TS_AUTHKEY: " TS_AUTHKEY
fi

TS_TAGS="${TS_TAGS:-tag:drone,tag:fleet-caribou}"
TS_LOGIN_SERVER="${TS_LOGIN_SERVER:-https://controlplane.tailscale.com}"

echo
info "Hostname (tailnet): $TS_HOSTNAME"
info "Tailscale tags:     $TS_TAGS"
[ -n "$API_KEY" ] && info "API_KEY:            ${API_KEY:0:6}…(${#API_KEY} chars)" || warn "API_KEY: (empty - benchtop only)"
[ -n "$TS_AUTHKEY" ] && info "TS_AUTHKEY:         ${TS_AUTHKEY:0:18}…" || warn "TS_AUTHKEY: (empty - will prompt for browser login)"
echo

# ── Step 1: System packages ──────────────────────────────────────────────────
step 1 "System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq curl ca-certificates python3-venv >/dev/null
ok "curl, ca-certificates, python3-venv ready"
echo

# ── Step 2: Tailscale install + join ─────────────────────────────────────────
step 2 "Tailscale install + join"
if ! command -v tailscale >/dev/null 2>&1; then
    info "installing Tailscale via official script"
    curl -fsSL https://tailscale.com/install.sh | sudo sh >/dev/null
    ok "tailscale installed ($(tailscale version | head -1))"
else
    ok "tailscale already installed ($(tailscale version | head -1))"
fi

if tailscale status 2>&1 | grep -q "Logged out"; then
    info "joining tailnet"
    TS_UP_ARGS=(
        --hostname="$TS_HOSTNAME"
        --accept-routes=false
        --accept-dns=false
        --login-server="$TS_LOGIN_SERVER"
    )
    if [ -n "$TS_AUTHKEY" ]; then
        TS_UP_ARGS+=(--auth-key="$TS_AUTHKEY" --advertise-tags="$TS_TAGS")
    fi
    sudo tailscale up "${TS_UP_ARGS[@]}"
    ok "joined tailnet"
else
    ok "tailscale already logged in"
fi

# Wait a moment for state to settle and IPs to be assigned.
for i in $(seq 1 10); do
    TS_IPV4="$(tailscale ip -4 2>/dev/null || true)"
    if [ -n "$TS_IPV4" ]; then break; fi
    sleep 1
done
TS_DNS="$(tailscale status --self=true --json 2>/dev/null \
    | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("Self",{}).get("DNSName","").rstrip("."))' \
    || echo "(unknown)")"
info "Tailscale IPv4: $TS_IPV4"
info "Tailscale DNS:  $TS_DNS"
echo

# ── Step 3: Python venv + deps ───────────────────────────────────────────────
step 3 "Python venv + dependencies"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    ok "created $VENV_DIR"
else
    info "venv already present at $VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install -q "websockets>=12" mavsdk dronecan
ok "mavsdk + websockets + dronecan installed"
echo

# ── Step 4: Runtime env file ─────────────────────────────────────────────────
step 4 "Runtime environment file"
if [ -f "$ENV_FILE" ]; then
    warn "$ENV_FILE already exists; leaving as-is"
else
    install -m 0600 /dev/null "$ENV_FILE"
    cat > "$ENV_FILE" <<ENV
API_KEY=$API_KEY
DRONE_ID=$DRONE_ID
ENV
    ok "wrote $ENV_FILE (0600)"
fi
echo

# ── Step 5: systemd units (caribou-can + csu) ────────────────────────────────
step 5 "systemd units"

# 5a. caribou-can.service: bring up can0/can1 once kernel devices exist.
CAN_UNIT_PATH=/etc/systemd/system/caribou-can.service
sudo tee "$CAN_UNIT_PATH" >/dev/null <<'CAN_UNIT'
[Unit]
Description=Caribou System Unit - CAN0/CAN1 bring-up
Documentation=https://github.com/Pan-Robotics/Caribou-System-Unit
After=sys-subsystem-net-devices-can0.device sys-subsystem-net-devices-can1.device
Wants=sys-subsystem-net-devices-can0.device sys-subsystem-net-devices-can1.device
Before=csu.service

[Service]
Type=oneshot
RemainAfterExit=true
ExecStartPre=-/sbin/ip link set can0 down
ExecStartPre=-/sbin/ip link set can1 down
ExecStart=/sbin/ip link set can0 up type can bitrate 500000
ExecStart=/sbin/ip link set can0 txqueuelen 65536
ExecStart=/sbin/ip link set can1 up type can bitrate 1000000
ExecStart=/sbin/ip link set can1 txqueuelen 65536
ExecStop=/sbin/ip link set can0 down
ExecStop=/sbin/ip link set can1 down

[Install]
WantedBy=multi-user.target
CAN_UNIT
sudo chmod 0644 "$CAN_UNIT_PATH"

# 5b. csu.service: the main CSU process, ordered after caribou-can.
sudo tee "$UNIT_PATH" >/dev/null <<UNIT
[Unit]
Description=Caribou System Unit - MAVLink ingest + HubLink (caribou.stream.v1)
Documentation=https://github.com/Pan-Robotics/Caribou-System-Unit
After=network-online.target tailscaled.service caribou-can.service
Wants=network-online.target tailscaled.service caribou-can.service

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$REPO_DIR/src
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python3 -u CSU.py

Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5
LimitNOFILE=65536

StandardOutput=journal
StandardError=journal
SyslogIdentifier=caribou-csu

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$REPO_DIR

[Install]
WantedBy=multi-user.target
UNIT
sudo chmod 0644 "$UNIT_PATH"

sudo systemctl daemon-reload
sudo systemctl enable caribou-can.service csu.service >/dev/null 2>&1
sudo systemctl restart caribou-can.service || true
sudo systemctl restart csu.service
ok "caribou-can.service + csu.service installed, enabled, started"
echo

# ── Step 6: Settle + summary ─────────────────────────────────────────────────
step 6 "Connection summary"
sleep 3
CSU_STATE="$(systemctl is-active csu.service)"
CSU_PID="$(systemctl show -p MainPID --value csu.service)"
STREAM_PORT="$(grep -E '^STREAM_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)"
STREAM_PORT="${STREAM_PORT:-8765}"
echo

if [ "$CSU_STATE" = "active" ]; then
    SUMMARY_COLOR="$GREEN"
    STATE_LABEL="active (pid $CSU_PID)"
else
    SUMMARY_COLOR="$YELLOW"
    STATE_LABEL="$CSU_STATE  (check: sudo journalctl -u csu.service -n 30)"
fi

cat <<SUMMARY

${SUMMARY_COLOR}========================================${NC}
${BOLD} Drone provisioned${NC}
${SUMMARY_COLOR}========================================${NC}

  ${BOLD}On the Hub side${NC}, configure this drone with:

    Drone ID:        ${BOLD}$DRONE_ID${NC}
    Mode:            Pull
    Host:            ${BOLD}$TS_DNS${NC}
                     (or fallback IPv4: $TS_IPV4)
    Stream port:     ${BOLD}$STREAM_PORT${NC}
    API key:         ${BOLD}${API_KEY:-(none - benchtop)}${NC}

  ${BOLD}Local state${NC}:
    csu.service:     $STATE_LABEL
    Env file:        $ENV_FILE (mode 0600)
    Venv:            $VENV_DIR
    Tailnet IPv4:    $TS_IPV4
    MagicDNS:        $TS_DNS

  ${BOLD}Operations${NC}:
    sudo systemctl status csu.service
    sudo journalctl -u csu.service -f
    sudo systemctl restart csu.service

${SUMMARY_COLOR}========================================${NC}
SUMMARY
