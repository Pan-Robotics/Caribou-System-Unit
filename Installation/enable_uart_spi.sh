#!/bin/bash
# Enable the kernel device tree overlays needed for the Caribou System Unit:
#   - SPI (driven by the MCP2515 overlays themselves; explicit dtparam=spi=on
#     is omitted because it would also expose spidev on the same CS lines the
#     mcp2515-canX overlays need)
#   - UART (useful for headless console + future serial wiring)
#   - Two MCP2515 CAN controllers from the Waveshare 2-CH CAN HAT+:
#         CAN0 on SPI0/CS0, IRQ GPIO 25, 12 MHz crystal
#         CAN1 on SPI0/CS1, IRQ GPIO 24, 12 MHz crystal
#     Per https://www.waveshare.com/wiki/2-CH_CAN_HAT+
#
# Idempotent — re-runs are safe. Triggers a reboot at the end because device
# tree changes take effect at next boot.

set -e

CONFIG=/boot/firmware/config.txt

ensure_line() {
    grep -q "$1" "$CONFIG" || echo "$2" | sudo tee -a "$CONFIG" >/dev/null
}

# UART (harmless if unused)
ensure_line "^enable_uart=1" "enable_uart=1"

# Drop any legacy entries from earlier revisions of this script before adding
# the corrected ones below.
sudo sed -i '/^dtparam=spi=on$/d' "$CONFIG"
sudo sed -i '/^dtoverlay=mcp2515-can[01],/d' "$CONFIG"
sudo sed -i '/^dtoverlay=spi-bcm2835-overlay$/d' "$CONFIG"

# Waveshare 2-CH CAN HAT (MCP2515, NON-isolated variant).
# Verified working against this HAT model on CM5 + Waveshare DUAL-ETH-4G-5G-BASE.
# If you swap to the "+" / isolated HAT, change can1 interrupt to GPIO 24.
ensure_line "^dtoverlay=mcp2515-can0," "dtoverlay=mcp2515-can0,oscillator=12000000,interrupt=25"
ensure_line "^dtoverlay=mcp2515-can1," "dtoverlay=mcp2515-can1,oscillator=12000000,interrupt=23"

echo "config.txt updated. Rebooting in 2s..."
sleep 2
sudo systemctl reboot
