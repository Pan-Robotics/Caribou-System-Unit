#!/bin/bash
# =============================================================================
# Caribou Logs & OTA Service - Installer
# =============================================================================
#
# Installs the Logs & OTA companion service on the Caribou CM5.
#
# Provides:
#   - FC log sync via ArduPilot net_webserver HTTP (fast, no MAVLink blocking)
#   - FC log serving to the Hub (S3-bound)
#   - OTA firmware flash via Tier 1 (FC HTTP pull) - requires firmware_puller.lua
#     on the FC; falls back through Tier 2 (HTTP PUT) and Tier 3 (MAVFTP) where
#     available
#   - System diagnostics reporting (CPU, mem, disk, temp, csu/can/camera/etc.)
#   - Remote journalctl log streaming over Socket.IO
#
# This is OPTIONAL and PARALLEL to csu.service. Telemetry is unaffected if this
# service is removed.
#
# Prerequisites:
#   - bootstrap_drone.sh has been run (Tailscale joined, csu.service running)
#   - The CM5 is on the same Pixhawk Ethernet subnet (default 192.168.144.0/24)
#   - The Pixhawk has been configured to push a second MAVLink stream to the Pi
#     on UDP 14550 (NET_P2_* params) so this service has its own FC link
#     distinct from csu.service's :14540. See OTA_Setup_Guide.md.
#
# Usage:
#   sudo ./install_logs_ota.sh
#   sudo ./install_logs_ota.sh --uninstall
# =============================================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

SERVICE_NAME="logs-ota"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# -- Uninstall mode ----------------------------------------------------------
if [ "$1" = "--uninstall" ]; then
    echo -e "${RED}========================================${NC}"
    echo -e "${RED} Uninstalling Logs & OTA Service${NC}"
    echo -e "${RED}========================================${NC}"

    if [ "$EUID" -ne 0 ]; then
        echo -e "${RED}Please run as root (sudo)${NC}"
        exit 1
    fi

    echo
    read -p "This will stop and remove logs-ota.service. Continue? [y/N]: " CONFIRM
    if [[ ! "$CONFIRM" =~ ^[Yy] ]]; then
        echo "Aborted."
        exit 0
    fi

    systemctl stop ${SERVICE_NAME} 2>/dev/null || true
    systemctl disable ${SERVICE_NAME} 2>/dev/null || true
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload

    echo -e "${GREEN}  Service stopped, disabled, and removed${NC}"
    echo -e "${DIM}  Note: Python packages and log store directory were not removed.${NC}"
    echo -e "${DIM}  To remove cached logs: sudo rm -rf /var/lib/caribou/fc_logs${NC}"
    exit 0
fi

# -- Main install ------------------------------------------------------------
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} Caribou Hub - Logs & OTA Installer${NC}"
echo -e "${GREEN}========================================${NC}"
echo

if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Please run as root (sudo)${NC}"
    exit 1
fi

IS_UPGRADE=false
if [ -f "$SERVICE_FILE" ]; then
    IS_UPGRADE=true
    echo -e "${YELLOW}Existing installation detected. This will upgrade in place.${NC}"
    echo
fi

# -- Detect SUDO_USER + CSU env file ----------------------------------------
SUDO_INVOKER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
CSU_ENV_FILE="$(eval echo ~$SUDO_INVOKER)/caribou-csu.env"
DEFAULT_INSTALL_DIR="$(eval echo ~$SUDO_INVOKER)/Caribou-System-Unit/Installation/logs-ota"

PREFILL_DRONE_ID=""
PREFILL_API_KEY=""
if [ -f "$CSU_ENV_FILE" ]; then
    echo -e "${GREEN}Found $CSU_ENV_FILE — inheriting DRONE_ID/API_KEY${NC}"
    PREFILL_DRONE_ID=$(grep -E '^DRONE_ID=' "$CSU_ENV_FILE" | cut -d= -f2-)
    PREFILL_API_KEY=$(grep -E '^API_KEY=' "$CSU_ENV_FILE" | cut -d= -f2-)
    echo
fi

# -- Section 1: Basic Configuration -----------------------------------------
echo -e "${BOLD}${CYAN}--- Basic Configuration ---${NC}"
echo

read -p "  Service user [${SUDO_INVOKER}]: " SERVICE_USER
SERVICE_USER=${SERVICE_USER:-$SUDO_INVOKER}

if ! id "$SERVICE_USER" &>/dev/null; then
    echo -e "${RED}  Error: User '$SERVICE_USER' does not exist${NC}"
    exit 1
fi

read -p "  Install directory [${DEFAULT_INSTALL_DIR}]: " INSTALL_DIR
INSTALL_DIR=${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}

echo

# -- Section 2: Hub Connection ----------------------------------------------
echo -e "${BOLD}${CYAN}--- Hub Connection ---${NC}"
echo

read -p "  Caribou Hub URL (e.g. http://caribou-hub-dev-laptop.tail8c21ec.ts.net:3005): " HUB_URL
HUB_URL=${HUB_URL%/}
if [ -z "$HUB_URL" ]; then
    echo -e "${RED}  Hub URL is required${NC}"
    exit 1
fi

read -p "  Drone ID [${PREFILL_DRONE_ID:-caribou_001}]: " DRONE_ID
DRONE_ID=${DRONE_ID:-${PREFILL_DRONE_ID:-caribou_001}}

if [ -n "$PREFILL_API_KEY" ]; then
    read -p "  API Key [inherited from caribou-csu.env]: " API_KEY
    API_KEY=${API_KEY:-$PREFILL_API_KEY}
else
    read -sp "  API Key (hidden): " API_KEY
    echo
fi
if [ -z "$API_KEY" ]; then
    echo -e "${RED}  Error: API key is required${NC}"
    exit 1
fi

echo

# -- Section 3: FC Connection -----------------------------------------------
echo -e "${BOLD}${CYAN}--- Flight Controller Connection ---${NC}"
echo
echo -e "  ${DIM}This service needs its own MAVSDK link to the FC (for MAVFTP +${NC}"
echo -e "  ${DIM}reboot + arm state). csu.service already uses udpin://0.0.0.0:14540,${NC}"
echo -e "  ${DIM}so this service defaults to udpin://0.0.0.0:14550. Configure the${NC}"
echo -e "  ${DIM}Pixhawk's NET_P2_* parameters to push a second MAVLink stream there.${NC}"
echo
echo "    1) UDP listener on :14550 (recommended for Caribou)"
echo "    2) UDP listener on a custom port"
echo "    3) Serial (TELEM port, e.g. /dev/ttyAMA1:921600)"
echo "    4) No FC (diagnostics + log streaming only)"
echo
read -p "  Choose [1]: " FC_CHOICE
FC_CHOICE=${FC_CHOICE:-1}

FC_CONNECTION=""
NO_FC_FLAG=""

case "$FC_CHOICE" in
    1) FC_CONNECTION="udpin://0.0.0.0:14550" ;;
    2)
        read -p "  Port [14550]: " PORT
        PORT=${PORT:-14550}
        FC_CONNECTION="udpin://0.0.0.0:${PORT}"
        ;;
    3)
        read -p "  Serial device [/dev/ttyAMA1]: " SERIAL_DEV
        SERIAL_DEV=${SERIAL_DEV:-/dev/ttyAMA1}
        read -p "  Baud rate [921600]: " BAUD_RATE
        BAUD_RATE=${BAUD_RATE:-921600}
        FC_CONNECTION="serial://${SERIAL_DEV}:${BAUD_RATE}"
        ;;
    4)
        NO_FC_FLAG="--no-fc"
        ;;
    *) echo -e "${RED}  Invalid choice${NC}"; exit 1 ;;
esac

echo

# -- Section 4: FC web server (log download) --------------------------------
echo -e "${BOLD}${CYAN}--- FC Log Download (ArduPilot net_webserver) ---${NC}"
echo
echo -e "  ${DIM}If the FC runs net_webserver_put.lua (or stock net_webserver.lua),${NC}"
echo -e "  ${DIM}.BIN logs can be pulled via HTTP without blocking MAVLink.${NC}"

if [ "$FC_CHOICE" = "4" ]; then
    echo -e "  ${YELLOW}Skipped (no FC connection)${NC}"
    FC_WEBSERVER_URL=""
else
    read -p "  FC web server URL [http://192.168.144.10:8080]: " FC_WEBSERVER_URL
    FC_WEBSERVER_URL=${FC_WEBSERVER_URL:-"http://192.168.144.10:8080"}
    FC_WEBSERVER_URL=${FC_WEBSERVER_URL%/}
fi

echo

# -- Summary ----------------------------------------------------------------
echo -e "${BOLD}${CYAN}--- Summary ---${NC}"
echo "  Install dir:    $INSTALL_DIR"
echo "  Service user:   $SERVICE_USER"
echo "  Hub URL:        $HUB_URL"
echo "  Drone ID:       $DRONE_ID"
echo "  API Key:        ${API_KEY:0:8}..."
echo "  FC connection:  ${FC_CONNECTION:-<none>}"
echo "  FC web server:  ${FC_WEBSERVER_URL:-<none>}"
echo "  Log cache:      /var/lib/caribou/fc_logs"
echo
read -p "  Proceed? [Y/n]: " CONFIRM
CONFIRM=${CONFIRM:-Y}
[[ "$CONFIRM" =~ ^[Yy] ]] || { echo "Aborted."; exit 0; }

echo

# -- Step 1: Python deps ----------------------------------------------------
echo -e "${GREEN}[1/5] Installing Python dependencies${NC}"

# Prefer the CSU venv if it exists (avoids re-downloading mavsdk ~18 MB to
# system Python and keeps one Python environment per drone). Falls back to
# system Python with --break-system-packages if no venv is found.
CSU_VENV="$(eval echo ~$SERVICE_USER)/Caribou-System-Unit/.venv"
if [ -x "$CSU_VENV/bin/python3" ]; then
    PYTHON="$CSU_VENV/bin/python3"
    PIP_CMD="$CSU_VENV/bin/pip"
    echo "  using CSU venv: $CSU_VENV"
    sudo -u "$SERVICE_USER" "$PIP_CMD" install -q \
        mavsdk requests psutil "python-socketio[asyncio_client]" aiohttp
else
    PYTHON="/usr/bin/python3"
    PIP_CMD="pip3"
    echo "  ${YELLOW}CSU venv not found at $CSU_VENV - using system Python${NC}"
    $PIP_CMD install --break-system-packages -q \
        mavsdk requests psutil "python-socketio[asyncio_client]" aiohttp
fi
echo "  done (python: $PYTHON)"
echo

# -- Step 2: Stage logs_ota_service.py (already in place if running from repo) ---
echo -e "${GREEN}[2/5] Staging service script${NC}"
mkdir -p "$INSTALL_DIR"
if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
    cp "$SCRIPT_DIR/logs_ota_service.py" "$INSTALL_DIR/logs_ota_service.py"
fi
chmod +x "$INSTALL_DIR/logs_ota_service.py"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/logs_ota_service.py"
echo "  at $INSTALL_DIR/logs_ota_service.py"
echo

# -- Step 3: Log cache directory --------------------------------------------
echo -e "${GREEN}[3/5] Log cache directory${NC}"
LOG_STORE_DIR="/var/lib/caribou/fc_logs"
mkdir -p "$LOG_STORE_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$LOG_STORE_DIR"
echo "  $LOG_STORE_DIR"
echo

# -- Step 4: caribou-logs-ota.env (logs-ota specific values) -----------------
echo -e "${GREEN}[4/5] Writing ~${SERVICE_USER}/caribou-logs-ota.env (0600)${NC}"
LOGS_OTA_ENV="$(eval echo ~$SERVICE_USER)/caribou-logs-ota.env"
install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" /dev/null "$LOGS_OTA_ENV"
cat > "$LOGS_OTA_ENV" <<EOF
HUB_URL=${HUB_URL}
FC_CONNECTION=${FC_CONNECTION:-udpin://0.0.0.0:14550}
FC_WEBSERVER_URL=${FC_WEBSERVER_URL:-http://192.168.144.10:8080}
EOF
echo "  wrote $LOGS_OTA_ENV"
echo

# -- Step 5: systemd unit ---------------------------------------------------
echo -e "${GREEN}[5/5] systemd unit${NC}"
sudo tee "$SERVICE_FILE" >/dev/null <<UNIT
[Unit]
Description=Caribou Hub - Logs & OTA Service
Documentation=https://github.com/Pan-Robotics/Caribou-System-Unit
After=network-online.target csu.service
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=/home/${SERVICE_USER}
EnvironmentFile=${CSU_ENV_FILE}
EnvironmentFile=-${LOGS_OTA_ENV}
ExecStart=${PYTHON} ${INSTALL_DIR}/logs_ota_service.py \\
    --hub-url \${HUB_URL} \\
    --drone-id \${DRONE_ID} \\
    --api-key \${API_KEY} \\
    --fc-connection \${FC_CONNECTION} \\
    --fc-webserver-url \${FC_WEBSERVER_URL} \\
    --log-store-dir ${LOG_STORE_DIR} \\
    --poll-interval 5 \\
    --diagnostics-interval 10
Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=logs-ota

Environment=PYTHONUNBUFFERED=1

ProtectSystem=strict
ReadWritePaths=/tmp /home/${SERVICE_USER} ${LOG_STORE_DIR}
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT

if [ "$IS_UPGRADE" = "true" ]; then
    systemctl stop ${SERVICE_NAME} 2>/dev/null || true
fi

systemctl daemon-reload
systemctl enable ${SERVICE_NAME}.service >/dev/null 2>&1
systemctl restart ${SERVICE_NAME}.service
sleep 2

if systemctl is-active --quiet ${SERVICE_NAME}; then
    echo -e "${GREEN}  ${SERVICE_NAME}.service: active${NC}"
else
    echo -e "${YELLOW}  ${SERVICE_NAME}.service did not become active. Check logs:${NC}"
    echo -e "${DIM}    sudo journalctl -u ${SERVICE_NAME} -n 30 --no-pager${NC}"
fi

echo
echo -e "${GREEN}========================================${NC}"
if [ "$IS_UPGRADE" = "true" ]; then
    echo -e "${GREEN} Upgrade complete${NC}"
else
    echo -e "${GREEN} Installation complete${NC}"
fi
echo -e "${GREEN}========================================${NC}"
echo

# -- Optional: push Lua scripts to the FC over MAVFTP -----------------------
if [ "$FC_CHOICE" != "4" ]; then
    echo -e "${BOLD}${CYAN}--- FC SD-card payload (Lua scripts) ---${NC}"
    echo
    echo -e "  ${DIM}install_fc_scripts.py can push firmware_puller.lua and${NC}"
    echo -e "  ${DIM}net_webserver_put.lua to the FC's /APM/scripts/ over MAVFTP —${NC}"
    echo -e "  ${DIM}no SD-card removal needed.${NC}"
    echo
    echo -e "  ${YELLOW}Heads up:${NC} csu.service is using udpin://0.0.0.0:14540."
    echo -e "  If you have NOT yet set up NET_P2_* on the Pixhawk to push a second"
    echo -e "  MAVLink stream to ${FC_CONNECTION}, this will fail. In that case:"
    echo -e "    - skip this now and set NET_P2_* via Mission Planner first, OR"
    echo -e "    - temporarily \`sudo systemctl stop csu.service\` and re-invoke"
    echo -e "      install_fc_scripts.py with --fc-connection udpin://0.0.0.0:14540"
    echo
    read -p "  Push Lua scripts to FC now? [y/N]: " PUSH_LUA
    PUSH_LUA=${PUSH_LUA:-N}
    if [[ "$PUSH_LUA" =~ ^[Yy] ]]; then
        # Drop privs so MAVSDK runs as the service user (matches systemd unit env)
        if command -v sudo >/dev/null 2>&1 && [ -n "${SUDO_USER:-}" ]; then
            sudo -u "${SUDO_USER}" "${PYTHON}" \
                "${INSTALL_DIR}/install_fc_scripts.py" \
                --fc-connection "${FC_CONNECTION}" \
                --scripts-dir "${INSTALL_DIR}" || true
        else
            "${PYTHON}" "${INSTALL_DIR}/install_fc_scripts.py" \
                --fc-connection "${FC_CONNECTION}" \
                --scripts-dir "${INSTALL_DIR}" || true
        fi
        echo
    else
        echo -e "${DIM}  Skipped. Run later:${NC}"
        echo -e "${DIM}    ${PYTHON} ${INSTALL_DIR}/install_fc_scripts.py \\${NC}"
        echo -e "${DIM}        --fc-connection ${FC_CONNECTION}${NC}"
        echo
    fi
fi

echo -e "${BOLD}Remaining FC parameter setup (one-time, via Mission Planner / MAVProxy):${NC}"
echo "  SCR_ENABLE=1                    (Lua scripting on; needs reboot)"
echo "  FWPULL_ENABLE=1                 (firmware_puller active)"
echo "  FWPULL_PI_IP0..3 = <this Pi's IP>"
echo "  WEB_ENABLE=1                    (FC web server for log download)"
echo "  WEB_PUT_ENABLE=1                (Tier 2 PUT fallback; optional)"
echo "  NET_P2_TYPE=2 NET_P2_PORT=14550 NET_P2_IP*=<this Pi's IP>"
echo "  (NET_P2 is what gives logs-ota.service its own MAVLink endpoint distinct"
echo "   from csu.service's :14540.)"
echo "  See OTA_Setup_Guide.md for the full table."
echo
echo -e "${BOLD}Useful commands:${NC}"
echo "  sudo systemctl status ${SERVICE_NAME}"
echo "  sudo journalctl -u ${SERVICE_NAME} -f"
echo "  sudo systemctl restart ${SERVICE_NAME}"
echo "  sudo ./install_logs_ota.sh --uninstall"
echo
