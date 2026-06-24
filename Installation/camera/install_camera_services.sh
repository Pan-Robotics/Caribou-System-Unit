#!/bin/bash
# ===========================================================================
# Caribou Camera Stream Services - Installer
# ===========================================================================
#
# Installs and configures three systemd services that, together, expose an
# RTSP camera to the Caribou Hub as a public WebRTC stream:
#
#   1. go2rtc.service          - RTSP -> WebRTC transcoder (binary)
#   2. tailscale-funnel.service - publish go2rtc API on a public HTTPS URL
#                                  via Tailscale Funnel (signaling only)
#   3. camera-stream.service   - Python wrapper that health-checks go2rtc and
#                                  registers the WHEP URL with the Caribou Hub
#
# Works with ANY RTSP camera source (no camera-specific code).
#
# This is OPTIONAL and PARALLEL to the core CSU telemetry stack. Install only
# if this drone has a camera you want to stream via the Hub. The CSU service
# (csu.service) is independent and unaffected.
#
# Prerequisites:
#   - Caribou System Unit already provisioned (bootstrap_drone.sh ran cleanly)
#   - Tailscale already joined to the tailnet (the bootstrap covers this)
#   - An RTSP camera reachable from the drone
#
# Run as root from inside this directory:
#   sudo ./install_camera_services.sh
# ===========================================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  Caribou Camera Stream Services         ${NC}"
echo -e "${CYAN}========================================${NC}"

# --- Check root ------------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}ERROR: must be run as root (sudo)${NC}"; exit 1
fi

# --- Detect architecture for go2rtc download -------------------------------
ARCH=$(uname -m)
case "$ARCH" in
    aarch64) GO2RTC_ARCH="arm64" ;;
    armv7l)  GO2RTC_ARCH="arm" ;;
    x86_64)  GO2RTC_ARCH="amd64" ;;
    *) echo -e "${RED}Unsupported architecture: $ARCH${NC}"; exit 1 ;;
esac
echo "  Architecture: $ARCH ($GO2RTC_ARCH)"

# --- Locate the user running this (not the sudo'd root) --------------------
SUDO_INVOKER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
DEFAULT_INSTALL_DIR="$(eval echo ~$SUDO_INVOKER)/Caribou-System-Unit"
CSU_ENV_FILE="$(eval echo ~$SUDO_INVOKER)/caribou-csu.env"

# --- Reuse CSU env values if available -------------------------------------
PREFILL_DRONE_ID=""
PREFILL_API_KEY=""
if [ -f "$CSU_ENV_FILE" ]; then
    echo -e "${GREEN}Found $CSU_ENV_FILE - will inherit DRONE_ID/API_KEY${NC}"
    PREFILL_DRONE_ID=$(grep -E '^DRONE_ID=' "$CSU_ENV_FILE" | cut -d= -f2-)
    PREFILL_API_KEY=$(grep -E '^API_KEY=' "$CSU_ENV_FILE" | cut -d= -f2-)
fi

# --- Interactive prompts ---------------------------------------------------
echo
echo -e "${CYAN}--- Configuration ---${NC}"

read -p "RTSP URL (e.g. rtsp://192.168.1.100:8554/stream): " RTSP_URL
if [ -z "$RTSP_URL" ]; then
    echo -e "${RED}RTSP URL is required${NC}"; exit 1
fi

read -p "Caribou Hub URL [http://caribou-hub-dev-laptop.tail8c21ec.ts.net:3005]: " HUB_URL
HUB_URL=${HUB_URL:-"http://caribou-hub-dev-laptop.tail8c21ec.ts.net:3005"}

read -p "Drone ID [${PREFILL_DRONE_ID:-caribou_001}]: " DRONE_ID
DRONE_ID=${DRONE_ID:-${PREFILL_DRONE_ID:-caribou_001}}

if [ -n "$PREFILL_API_KEY" ]; then
    read -p "API key [inherited from caribou-csu.env]: " API_KEY
    API_KEY=${API_KEY:-$PREFILL_API_KEY}
else
    read -p "API key for Hub authentication: " API_KEY
fi
if [ -z "$API_KEY" ]; then
    echo -e "${YELLOW}WARNING: no API key - Hub registration will be disabled${NC}"
fi

read -p "Install directory [$DEFAULT_INSTALL_DIR]: " INSTALL_DIR
INSTALL_DIR=${INSTALL_DIR:-"$DEFAULT_INSTALL_DIR"}

read -p "Service user [$SUDO_INVOKER]: " SERVICE_USER
SERVICE_USER=${SERVICE_USER:-"$SUDO_INVOKER"}

echo
echo -e "${CYAN}--- Summary ---${NC}"
echo "  RTSP URL:     $RTSP_URL"
echo "  Hub URL:      $HUB_URL"
echo "  Drone ID:     $DRONE_ID"
echo "  API key:      ${API_KEY:+<set>}${API_KEY:-<not set>}"
echo "  Install dir:  $INSTALL_DIR"
echo "  Service user: $SERVICE_USER"
echo "  go2rtc arch:  $GO2RTC_ARCH"
echo
read -p "Proceed? [Y/n]: " CONFIRM
[[ "${CONFIRM:-Y}" =~ ^[Yy] ]] || { echo "Aborted."; exit 0; }

# --- Step 1: System packages ----------------------------------------------
echo -e "${GREEN}[1/8] System packages${NC}"
apt-get update -qq
apt-get install -y -qq python3 python3-pip curl jq >/dev/null

# --- Step 2: Python deps ---------------------------------------------------
echo -e "${GREEN}[2/8] Python deps (requests)${NC}"
pip3 install --break-system-packages -q requests >/dev/null

# --- Step 3: go2rtc binary -------------------------------------------------
echo -e "${GREEN}[3/8] go2rtc binary${NC}"
GO2RTC_URL=$(curl -s https://api.github.com/repos/AlexxIT/go2rtc/releases/latest \
    | jq -r ".assets[] | select(.name == \"go2rtc_linux_${GO2RTC_ARCH}\") | .browser_download_url")
if [ -z "$GO2RTC_URL" ] || [ "$GO2RTC_URL" = "null" ]; then
    echo -e "${RED}Could not find go2rtc release for $GO2RTC_ARCH${NC}"; exit 1
fi
curl -sL "$GO2RTC_URL" -o /usr/local/bin/go2rtc
chmod +x /usr/local/bin/go2rtc
echo "  go2rtc version: $(/usr/local/bin/go2rtc --version 2>/dev/null || echo 'unknown')"

# --- Step 4: Tailscale sanity check (do not re-install) -------------------
echo -e "${GREEN}[4/8] Tailscale${NC}"
if ! command -v tailscale >/dev/null 2>&1; then
    echo -e "${RED}Tailscale not installed${NC}"
    echo "Run ../bootstrap_drone.sh first (it joins the tailnet)."
    exit 1
fi
TS_STATE=$(tailscale status --json 2>/dev/null | jq -r '.BackendState' 2>/dev/null || echo "unknown")
if [ "$TS_STATE" != "Running" ]; then
    echo -e "${YELLOW}Tailscale not connected (state=$TS_STATE) - funnel won't work until you run 'tailscale up'${NC}"
fi

# --- Step 5: Copy files into INSTALL_DIR ----------------------------------
echo -e "${GREEN}[5/8] Stage files in $INSTALL_DIR${NC}"
mkdir -p "$INSTALL_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" \
    "$SCRIPT_DIR/camera_stream_service.py" "$INSTALL_DIR/camera_stream_service.py"

# go2rtc.yaml
cat > "$INSTALL_DIR/go2rtc.yaml" <<EOF
streams:
  camera: ${RTSP_URL}

api:
  listen: ":1984"

webrtc:
  listen: ":8555"
  candidates:
    - stun:8555

rtsp:
  listen: ""

log:
  level: info
EOF
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/go2rtc.yaml"

# camera env file (separate from caribou-csu.env so the two services stay
# independently configurable)
CAM_ENV_FILE="$(eval echo ~$SERVICE_USER)/caribou-camera.env"
install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" /dev/null "$CAM_ENV_FILE"
cat > "$CAM_ENV_FILE" <<EOF
HUB_URL=${HUB_URL}
DRONE_ID=${DRONE_ID}
API_KEY=${API_KEY}
RTSP_URL=${RTSP_URL}
GO2RTC_API_PORT=1984
GO2RTC_WEBRTC_PORT=8555
EOF
echo "  wrote $CAM_ENV_FILE (0600)"

# --- Step 6: systemd units -------------------------------------------------
echo -e "${GREEN}[6/8] systemd units${NC}"

cat > /etc/systemd/system/go2rtc.service <<EOF
[Unit]
Description=go2rtc - RTSP to WebRTC streaming
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
ExecStart=/usr/local/bin/go2rtc -config ${INSTALL_DIR}/go2rtc.yaml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/tailscale-funnel.service <<EOF
[Unit]
Description=Tailscale Funnel - expose go2rtc API publicly
After=tailscaled.service go2rtc.service
Wants=tailscaled.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/tailscale funnel --bg --https=443 http://localhost:1984
ExecStop=/usr/bin/tailscale funnel --https=443 off

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/camera-stream.service <<EOF
[Unit]
Description=Caribou Camera Stream Service - health monitor + Hub registration
After=go2rtc.service tailscale-funnel.service
Requires=go2rtc.service

[Service]
Type=simple
User=${SERVICE_USER}
EnvironmentFile=${CAM_ENV_FILE}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/camera_stream_service.py \\
    --rtsp-url \${RTSP_URL} \\
    --hub-url \${HUB_URL} \\
    --drone-id \${DRONE_ID} \\
    --api-key \${API_KEY} \\
    --api-port \${GO2RTC_API_PORT} \\
    --webrtc-port \${GO2RTC_WEBRTC_PORT} \\
    --skip-funnel-setup
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
echo "  wrote /etc/systemd/system/{go2rtc,tailscale-funnel,camera-stream}.service"

# --- Step 7: enable + start ------------------------------------------------
echo -e "${GREEN}[7/8] Enable + start${NC}"
systemctl daemon-reload
systemctl enable go2rtc.service tailscale-funnel.service camera-stream.service
systemctl restart go2rtc.service
sleep 2
systemctl restart tailscale-funnel.service || true
sleep 1
systemctl restart camera-stream.service

# --- Step 8: Status ---------------------------------------------------------
echo -e "${GREEN}[8/8] Status${NC}"
sleep 2
for unit in go2rtc tailscale-funnel camera-stream; do
    printf "  %-22s %s\n" "${unit}.service" "$(systemctl is-active ${unit}.service)"
done

# Detect funnel URL (may take a few seconds)
sleep 3
TS_DNS=$(tailscale status --json 2>/dev/null | jq -r '.Self.DNSName' 2>/dev/null | sed 's/\.$//')
if [ -n "$TS_DNS" ] && [ "$TS_DNS" != "null" ]; then
    WHEP_URL="https://${TS_DNS}/api/webrtc?src=camera"
else
    WHEP_URL="(funnel URL pending - check 'tailscale status')"
fi

echo
echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN} Camera services installed${NC}"
echo -e "${CYAN}========================================${NC}"
echo
echo "  WHEP URL (give this to the Hub):"
echo "    ${WHEP_URL}"
echo
echo "  Useful commands:"
echo "    sudo systemctl status go2rtc tailscale-funnel camera-stream"
echo "    sudo journalctl -u camera-stream -f"
echo "    sudo journalctl -u go2rtc -f"
echo "    curl http://localhost:1984/api/streams"
echo
echo "  Config files:"
echo "    ${INSTALL_DIR}/go2rtc.yaml"
echo "    ${CAM_ENV_FILE}"
echo
