#!/bin/bash
# =============================================================================
# Caribou System Unit Installation Script
# =============================================================================
# Installs the CSU as a persistent systemd service on the drone's
# Raspberry Pi CM5.
#
# What this script does:
#   1. Creates a Python venv in <REPO>/.venv and installs mavsdk + websockets
#   2. Ensures ~/caribou-csu.env exists (prompts for API_KEY + DRONE_ID if not)
#   3. Installs /etc/systemd/system/csu.service with the right WorkingDirectory
#      and ExecStart paths
#   4. Enables and starts csu.service
#
# Prerequisites:
#   - Raspberry Pi OS (Debian 13) on a CM5 or similar; Python 3.11+
#   - This repository cloned and the script run from inside it
#   - Tailscale already installed and joined to the tailnet (separate step;
#     see Docs/HubLink_Implementation_Spec.md section 9)
#
# Usage:
#   chmod +x Installation/install_csu.sh
#   ./Installation/install_csu.sh
# =============================================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="$(id -un)"
ENV_FILE="$HOME/caribou-csu.env"
VENV_DIR="$REPO_DIR/.venv"
UNIT_PATH="/etc/systemd/system/csu.service"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} Caribou System Unit installer${NC}"
echo -e "${GREEN}========================================${NC}"
echo "  Repo:        $REPO_DIR"
echo "  Service user: $SERVICE_USER"
echo "  Env file:    $ENV_FILE"
echo

# ── Step 1: Python venv + dependencies ───────────────────────────────────────
echo -e "${GREEN}[1/4] Python venv + dependencies${NC}"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    echo "  created $VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install "websockets>=12" mavsdk dronecan
echo -e "${GREEN}  ✓ venv ready${NC}"
echo

# ── Step 2: Env file ─────────────────────────────────────────────────────────
echo -e "${GREEN}[2/4] Runtime environment file${NC}"
if [ -f "$ENV_FILE" ]; then
    echo -e "${CYAN}  $ENV_FILE already exists; leaving as-is${NC}"
else
    echo -e "${YELLOW}  $ENV_FILE missing; collecting values${NC}"
    read -r -p "  DRONE_ID [caribou_001]: " DRONE_ID
    DRONE_ID="${DRONE_ID:-caribou_001}"
    read -r -p "  API_KEY (per-drone, from Hub; leave empty for benchtop): " API_KEY
    install -m 0600 /dev/null "$ENV_FILE"
    cat > "$ENV_FILE" <<ENV
API_KEY=$API_KEY
DRONE_ID=$DRONE_ID
ENV
    echo -e "${GREEN}  ✓ wrote $ENV_FILE (0600)${NC}"
fi
echo

# ── Step 3: systemd unit ─────────────────────────────────────────────────────
echo -e "${GREEN}[3/4] systemd unit${NC}"
sudo tee "$UNIT_PATH" >/dev/null <<UNIT
[Unit]
Description=Caribou System Unit - MAVLink ingest + HubLink (caribou.stream.v1)
Documentation=https://github.com/Pan-Robotics/Caribou-System-Unit
After=network-online.target tailscaled.service
Wants=network-online.target tailscaled.service

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
echo -e "${GREEN}  ✓ wrote $UNIT_PATH${NC}"
echo

# ── Step 4: Enable + start ───────────────────────────────────────────────────
echo -e "${GREEN}[4/4] Enable + start${NC}"
sudo systemctl daemon-reload
sudo systemctl enable csu.service
sudo systemctl restart csu.service
sleep 3
sudo systemctl status csu.service --no-pager -n 8 | head -14
echo
echo -e "${CYAN}Useful commands:${NC}"
echo "  sudo systemctl status csu.service"
echo "  sudo journalctl -u csu.service -f"
echo "  sudo systemctl restart csu.service"
