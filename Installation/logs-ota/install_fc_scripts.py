#!/usr/bin/env python3
"""
Caribou - FC Lua Script Installer
----------------------------------
Pushes Lua scripts (firmware_puller.lua, net_webserver_put.lua) to the
flight controller's /APM/scripts/ directory over MAVFTP.

No more physically pulling the FC's SD card to copy scripts — once the
FC is reachable from the Pi over MAVLink, the entire FC-side install
is remote.

Workflow:
  1. Connect to the FC via MAVSDK.
  2. Verify or create /APM/scripts/ directory on the FC SD card.
  3. Upload each .lua script via MAVFTP. Progress is logged per file.
  4. Optionally send a MAVLink reboot command so the new scripts load.

Connection notes:
  - On a Caribou drone where csu.service is already running, csu owns
    udpin://0.0.0.0:14540. To talk to the FC without a port conflict,
    either:
      (a) configure the Pixhawk's NET_P2_* parameters to push a second
          MAVLink stream to udp://<pi>:14550 and pass --fc-connection
          udpin://0.0.0.0:14550 here (the default), OR
      (b) temporarily stop csu.service and pass
          --fc-connection udpin://0.0.0.0:14540 to reuse the existing
          endpoint just for the install.
  - For the very first install on a fresh FC (no NET_* params set yet)
    a USB-attached Mission Planner is still the easier path; once
    SCR_ENABLE=1 and Ethernet are configured, this script handles
    every subsequent script update remotely.

Usage:
    python3 install_fc_scripts.py \\
        --fc-connection udpin://0.0.0.0:14550 \\
        --reboot

Dependencies:
    pip install --break-system-packages mavsdk
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

try:
    from mavsdk import System
except ImportError:
    print("ERROR: mavsdk not installed. Run: pip install --break-system-packages mavsdk")
    sys.exit(1)

# Default scripts to push, in upload order. The puller is mandatory for OTA;
# net_webserver_put.lua adds optional Tier 2 HTTP PUT support.
DEFAULT_SCRIPTS = ["firmware_puller.lua", "net_webserver_put.lua"]

FC_SCRIPTS_DIR = "/APM/scripts"

# ANSI colors
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
NC = "\033[0m"


def banner():
    print(f"{GREEN}========================================{NC}")
    print(f"{GREEN} Caribou FC Lua Script Installer{NC}")
    print(f"{GREEN}========================================{NC}")


def err(msg: str) -> None:
    print(f"{RED}  {msg}{NC}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"{YELLOW}  {msg}{NC}")


def ok(msg: str) -> None:
    print(f"{GREEN}  {msg}{NC}")


def info(msg: str) -> None:
    print(f"{CYAN}  {msg}{NC}")


async def connect_fc(connection: str, timeout: float = 15.0) -> System:
    """Open MAVSDK system and wait for the first heartbeat."""
    info(f"Connecting to FC: {connection}")
    drone = System()
    await drone.connect(system_address=connection)

    try:
        async with asyncio.timeout(timeout):
            async for state in drone.core.connection_state():
                if state.is_connected:
                    ok(f"FC heartbeat received")
                    return drone
    except (asyncio.TimeoutError, TimeoutError):
        raise SystemExit(
            f"FC did not respond within {timeout:.0f}s on {connection}.\n"
            "Hints:\n"
            "  - if csu.service is using udpin://0.0.0.0:14540, either set\n"
            "    NET_P2_* on the Pixhawk to push to a second UDP port and\n"
            "    use that here, or temporarily `sudo systemctl stop csu.service`.\n"
            "  - confirm the FC Ethernet link is up and the Pi can ping it.\n"
        )

    raise SystemExit("MAVSDK connect_state loop exited without is_connected.")


async def ensure_scripts_dir(drone: System) -> None:
    """Make sure /APM/scripts exists. MAVFTP create_directory is idempotent
    on ArduPilot — a second create against an existing dir returns success."""
    try:
        await drone.ftp.create_directory(FC_SCRIPTS_DIR)
        info(f"Ensured {FC_SCRIPTS_DIR}/ exists on FC SD card")
    except Exception as e:
        # Many ArduPilot builds report a benign error if the dir already
        # exists; don't fail on that. We'll find out for real when the
        # first upload runs.
        warn(f"create_directory({FC_SCRIPTS_DIR}) returned: {e} "
             "(continuing — may already exist)")


async def list_scripts_dir(drone: System) -> list[str]:
    try:
        result = await drone.ftp.list_directory(FC_SCRIPTS_DIR)
        files = list(getattr(result, "files", []) or [])
        return [str(f).strip() for f in files if str(f).strip()]
    except Exception as e:
        warn(f"list_directory({FC_SCRIPTS_DIR}) failed: {e}")
        return []


async def upload_one(drone: System, local_path: Path, remote_dir: str) -> bool:
    """Upload a single file via MAVFTP. Returns True on success."""
    if not local_path.is_file():
        err(f"Local file missing: {local_path}")
        return False

    size = local_path.stat().st_size
    info(f"Uploading {local_path.name} ({size} bytes) -> {remote_dir}/")

    try:
        last_pct = -1
        async for progress in drone.ftp.upload(str(local_path), remote_dir):
            if progress and progress.total_bytes:
                pct = int(progress.bytes_transferred * 100 / progress.total_bytes)
                if pct != last_pct and pct % 10 == 0:
                    print(f"    {pct}% ({progress.bytes_transferred}/{progress.total_bytes})")
                    last_pct = pct
        ok(f"{local_path.name} uploaded")
        return True
    except Exception as e:
        err(f"Upload of {local_path.name} failed: {e}")
        return False


async def reboot_fc(drone: System) -> None:
    warn("Sending MAVLink reboot to FC — connection will drop")
    try:
        await drone.action.reboot()
        ok("Reboot command sent. FC will come back up with the new scripts loaded "
           "(assuming SCR_ENABLE=1).")
    except Exception as e:
        err(f"Reboot command failed: {e}")
        warn("Reboot the FC manually so the new Lua scripts load.")


async def main_async(args) -> int:
    scripts_dir = Path(args.scripts_dir).resolve()
    if not scripts_dir.is_dir():
        err(f"Scripts dir not found: {scripts_dir}")
        return 1

    selected: list[Path] = []
    for name in args.script:
        path = scripts_dir / name
        if not path.exists():
            err(f"Selected script missing: {path}")
            return 1
        selected.append(path)

    print()
    print(f"{BOLD}  FC connection: {NC}{args.fc_connection}")
    print(f"{BOLD}  Local scripts dir:{NC} {scripts_dir}")
    print(f"{BOLD}  Will upload to:{NC} {FC_SCRIPTS_DIR}/")
    for p in selected:
        print(f"    - {p.name}")
    print(f"{BOLD}  Reboot after: {NC}{'yes' if args.reboot else 'no'}")
    print()

    drone = await connect_fc(args.fc_connection, timeout=args.timeout)
    await ensure_scripts_dir(drone)

    existing = await list_scripts_dir(drone)
    if existing:
        info(f"Existing files in {FC_SCRIPTS_DIR}: {', '.join(sorted(existing))}")

    failures = 0
    for local in selected:
        if not await upload_one(drone, local, FC_SCRIPTS_DIR):
            failures += 1

    print()
    if failures:
        err(f"{failures} upload(s) failed.")
        return 2

    ok(f"All {len(selected)} script(s) uploaded.")
    print()
    print(f"{BOLD}Next steps on the FC (via Mission Planner or QGroundControl):{NC}")
    print("  1. Set SCR_ENABLE=1 (if not already).")
    print("  2. Set FWPULL_ENABLE=1 and FWPULL_PI_IP0..3 to this Pi's IP.")
    print("  3. Set WEB_ENABLE=1 (and WEB_PUT_ENABLE=1 if you want Tier 2).")
    print("  4. Reboot the FC so the scripts register their param tables.")
    print()

    if args.reboot:
        await reboot_fc(drone)

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Push Caribou Lua scripts to the FC's /APM/scripts/ via MAVFTP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--fc-connection", default="udpin://0.0.0.0:14550",
        help="MAVSDK connection string for the FC "
             "(default: udpin://0.0.0.0:14550 — distinct from csu.service's :14540)",
    )
    parser.add_argument(
        "--scripts-dir", default=str(Path(__file__).resolve().parent),
        help="Local directory containing the .lua scripts to push "
             "(default: this script's own directory)",
    )
    parser.add_argument(
        "--script", action="append", default=None,
        help=f"Specific script filename to upload (can be repeated). "
             f"Default: {' '.join(DEFAULT_SCRIPTS)}",
    )
    parser.add_argument(
        "--reboot", action="store_true",
        help="Send a MAVLink reboot command after all uploads succeed",
    )
    parser.add_argument(
        "--timeout", type=float, default=15.0,
        help="Seconds to wait for the FC heartbeat (default: 15)",
    )
    args = parser.parse_args()

    if args.script is None:
        args.script = DEFAULT_SCRIPTS

    banner()
    try:
        rc = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        warn("Interrupted")
        rc = 130
    except SystemExit as e:
        if isinstance(e.code, str):
            err(e.code)
            rc = 1
        else:
            rc = e.code or 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
