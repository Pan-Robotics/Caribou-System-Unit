#!/bin/bash
# Runtime Python dependencies for the Caribou System Unit.
# bootstrap_drone.sh / install_csu.sh do this inside a venv at <repo>/.venv;
# this script is the fallback for environments where you'd rather pip into
# the system Python (PEP 668-managed on Debian 13 — hence --break-system-packages).

set -e

sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv python3-can python3-serial

# MAVLink.py
pip install mavsdk --break-system-packages

# HubLink.py
pip install "websockets>=12" --break-system-packages

# Hobbywing.py + TattuBMS.py
pip install dronecan --break-system-packages
