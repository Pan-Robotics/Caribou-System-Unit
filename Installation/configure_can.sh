#!/bin/bash
# Bring up the two CAN interfaces after enable_uart_spi.sh + reboot.
#   can0 -> Hobbywing XRotor X15 ESCs (bitrate TBD; 500 kbit is the common default)
#   can1 -> DroneCAN BMS (1 Mbit/s per the DroneCAN standard)

set -e

sudo ip link set can0 up type can bitrate 500000
sudo ip link set can0 txqueuelen 65536

sudo ip link set can1 up type can bitrate 1000000
sudo ip link set can1 txqueuelen 65536

echo "CAN interfaces up:"
ip -br link show type can
