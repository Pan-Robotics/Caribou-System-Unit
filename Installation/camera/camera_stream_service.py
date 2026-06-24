#!/usr/bin/env python3
"""
Caribou Camera Stream Service
------------------------------
Manages go2rtc for a single RTSP camera source, exposes it via Tailscale Funnel,
and registers the WHEP URL with the Caribou Hub for browser-based WebRTC playback.

Architecture:
  RTSP Camera -> go2rtc (local) -> Tailscale Funnel (public HTTPS) -> Hub WHEP proxy -> Browser

go2rtc handles:
  - RTSP ingest from any camera
  - WebRTC encoding and signaling (WHEP API)
  - ICE/STUN negotiation for peer-to-peer media

Tailscale Funnel handles:
  - Exposing the go2rtc API to the internet (HTTPS signaling only)
  - WebRTC media flows peer-to-peer via UDP, not through the funnel

Features:
  - Generic: works with ANY RTSP camera source (no camera-specific code)
  - Manages go2rtc process lifecycle with auto-restart
  - Auto-detects Tailscale funnel URL
  - Registers WHEP URL with Caribou Hub
  - Health monitoring with stream producer validation
  - 5-minute heartbeat for Hub re-registration

Usage:
  python3 camera_stream_service.py \
    --rtsp-url rtsp://192.168.1.100:8554/stream \
    --hub-url https://arrowhub-5j6w8bkt.manus.space \
    --drone-id caribou_001 \
    --api-key <your-api-key>

See Installation/camera/install_camera_services.sh for automated deployment.
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package required. Install with: pip3 install requests")
    sys.exit(1)

# --- Module-level constants -------------------------------------------------

GO2RTC_API_PORT = 1984
GO2RTC_WEBRTC_PORT = 8555
GO2RTC_BINARY = "go2rtc"  # must be in PATH or /usr/local/bin/
TAILSCALE_FUNNEL_PORT = 443

GO2RTC_CONFIG_DIR = Path("/tmp/go2rtc")
GO2RTC_CONFIG_PATH = GO2RTC_CONFIG_DIR / "go2rtc.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("caribou-camera-stream")


# --- Config generation ------------------------------------------------------

def generate_go2rtc_config(rtsp_url: str, api_port: int, webrtc_port: int) -> str:
    """Produce go2rtc YAML config for a single RTSP stream named 'camera'."""
    return textwrap.dedent(f"""\
        streams:
          camera: {rtsp_url}

        api:
          listen: ":{api_port}"

        webrtc:
          listen: ":{webrtc_port}"
          candidates:
            - stun:{webrtc_port}

        rtsp:
          listen: ""

        log:
          level: info
    """)


# --- Tailscale helpers ------------------------------------------------------

def get_tailscale_funnel_url(
    funnel_port: int = 443,
    max_retries: int = 15,
    retry_interval: float = 2.0,
) -> Optional[str]:
    """
    Detect the Tailscale MagicDNS hostname and build the public HTTPS URL.
    Returns None if Tailscale is not installed or not connected.
    """
    for attempt in range(max_retries):
        try:
            result = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(
                    f"tailscale status failed (attempt {attempt + 1}/{max_retries}): "
                    f"{result.stderr.strip()}"
                )
                time.sleep(retry_interval)
                continue

            data = json.loads(result.stdout)
            dns_name = data.get("Self", {}).get("DNSName", "")
            if not dns_name:
                logger.warning(f"No DNSName in tailscale status (attempt {attempt + 1}/{max_retries})")
                time.sleep(retry_interval)
                continue

            # Strip trailing dot
            dns_name = dns_name.rstrip(".")

            if funnel_port == 443:
                url = f"https://{dns_name}"
            else:
                url = f"https://{dns_name}:{funnel_port}"

            logger.info(f"Tailscale funnel URL detected: {url}")
            return url

        except FileNotFoundError:
            logger.warning("Tailscale not installed - cannot detect funnel URL")
            return None
        except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            logger.warning(f"Tailscale detection error (attempt {attempt + 1}/{max_retries}): {e}")
            time.sleep(retry_interval)
            continue

    logger.error(f"Failed to detect Tailscale URL after {max_retries} attempts")
    return None


def setup_tailscale_funnel(local_port: int = 1984, funnel_port: int = 443) -> bool:
    """
    Configure Tailscale to serve and funnel go2rtc's API port.
    Returns True on success. Failure is non-fatal (stream still works on LAN).
    """
    try:
        # Step 1: tailscale serve
        serve_cmd = [
            "tailscale", "serve", "--bg",
            f"--https={funnel_port}",
            f"http://localhost:{local_port}",
        ]
        logger.info(f"Setting up Tailscale serve: {' '.join(serve_cmd)}")
        serve_result = subprocess.run(serve_cmd, capture_output=True, text=True, timeout=30)

        if serve_result.returncode != 0:
            logger.warning(f"tailscale serve failed: {serve_result.stderr.strip()}")
            return False

        # Step 2: tailscale funnel
        funnel_cmd = ["tailscale", "funnel", str(funnel_port), "on"]
        logger.info(f"Enabling Tailscale funnel: {' '.join(funnel_cmd)}")
        funnel_result = subprocess.run(funnel_cmd, capture_output=True, text=True, timeout=30)

        if funnel_result.returncode != 0:
            logger.warning(f"tailscale funnel failed: {funnel_result.stderr.strip()}")
            return False

        logger.info(f"Tailscale funnel configured: port {funnel_port} -> localhost:{local_port}")
        return True

    except FileNotFoundError:
        logger.warning("Tailscale not installed - skipping funnel setup")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("Tailscale command timed out")
        return False


# --- WebRTC Streaming Service ----------------------------------------------

class WebRTCStreamingService:
    """
    Main service class. Manages go2rtc lifecycle, health monitoring,
    Tailscale URL detection, and Hub registration with heartbeat.
    """

    def __init__(
        self,
        rtsp_url: str,
        api_port: int = GO2RTC_API_PORT,
        webrtc_port: int = GO2RTC_WEBRTC_PORT,
        funnel_port: int = TAILSCALE_FUNNEL_PORT,
        hub_url: Optional[str] = None,
        drone_id: Optional[str] = None,
        api_key: Optional[str] = None,
        public_url: Optional[str] = None,
        skip_funnel_setup: bool = False,
    ):
        self.rtsp_url = rtsp_url
        self.api_port = api_port
        self.webrtc_port = webrtc_port
        self.funnel_port = funnel_port
        self.hub_url = hub_url
        self.drone_id = drone_id
        self.api_key = api_key
        self.public_url = public_url
        self.skip_funnel_setup = skip_funnel_setup

        # Instance state
        self._stream_registered = False
        self._last_registration_attempt = 0.0
        self._registration_retry_interval = 30.0       # seconds between failed registration retries
        self._registration_heartbeat_interval = 300.0  # force re-register every 5 min
        self._last_heartbeat = 0.0
        self._detected_webrtc_url: Optional[str] = None  # cached after first detection

        self.go2rtc_process: Optional[subprocess.Popen] = None
        self.running = False
        self.stream_healthy = False
        self.reconnect_count = 0
        self.max_reconnects = 10

    def _start_go2rtc(self) -> bool:
        """Write config and start go2rtc subprocess."""
        GO2RTC_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        config = generate_go2rtc_config(self.rtsp_url, self.api_port, self.webrtc_port)
        GO2RTC_CONFIG_PATH.write_text(config)
        logger.info(f"go2rtc config written to {GO2RTC_CONFIG_PATH}")

        try:
            self.go2rtc_process = subprocess.Popen(
                [GO2RTC_BINARY, "-config", str(GO2RTC_CONFIG_PATH)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid,
            )
            # Give go2rtc time to start and connect to RTSP source
            time.sleep(3)
            if self.go2rtc_process.poll() is not None:
                logger.error("go2rtc exited immediately after start")
                return False
            logger.info(f"go2rtc started (PID {self.go2rtc_process.pid})")
            return True
        except FileNotFoundError:
            logger.error(f"go2rtc binary not found. Ensure '{GO2RTC_BINARY}' is in PATH or /usr/local/bin/")
            return False
        except Exception as e:
            logger.error(f"Failed to start go2rtc: {e}")
            return False

    def _stop_go2rtc(self):
        """Stop go2rtc gracefully (SIGTERM -> wait 5s -> SIGKILL)."""
        if self.go2rtc_process and self.go2rtc_process.poll() is None:
            try:
                os.killpg(os.getpgid(self.go2rtc_process.pid), signal.SIGTERM)
                self.go2rtc_process.wait(timeout=5)
                logger.info("go2rtc stopped gracefully")
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self.go2rtc_process.pid), signal.SIGKILL)
                logger.warning("go2rtc killed (SIGKILL after 5s timeout)")
            except ProcessLookupError:
                pass
        self.go2rtc_process = None

    def _check_stream_health(self) -> bool:
        """
        Check if go2rtc has an active producer for the 'camera' stream.
        Returns True iff response["camera"]["producers"] is non-empty.
        """
        try:
            resp = requests.get(
                f"http://localhost:{self.api_port}/api/streams",
                timeout=5,
            )
            if resp.status_code != 200:
                return False
            data = resp.json()
            camera = data.get("camera", {})
            producers = camera.get("producers", [])
            return len(producers) > 0
        except Exception:
            # Fallback: check if process is alive
            if self.go2rtc_process and self.go2rtc_process.poll() is None:
                return True
            return False

    def _detect_webrtc_url(self) -> Optional[str]:
        """
        Detect the public WHEP URL.
        Priority: explicit --public-url > Tailscale auto-detect > localhost fallback.
        """
        if self.public_url:
            url = self.public_url.rstrip("/")
            if "/api/webrtc" not in url:
                url = f"{url}/api/webrtc?src=camera"
            logger.info(f"Using explicit public URL: {url}")
            return url

        base = get_tailscale_funnel_url(self.funnel_port)
        if base:
            url = f"{base}/api/webrtc?src=camera"
            logger.info(f"Using Tailscale funnel URL: {url}")
            return url

        # Fallback: LAN-only
        logger.warning("No public URL available - using localhost (LAN only)")
        return f"http://localhost:{self.api_port}/api/webrtc?src=camera"

    def _register_stream_with_hub(self, webrtc_url: str) -> bool:
        """Register the WHEP URL with the Caribou Hub."""
        if not self.hub_url or not self.drone_id or not self.api_key:
            logger.debug("Hub registration skipped - missing hub_url, drone_id, or api_key")
            return False

        self._last_registration_attempt = time.time()

        try:
            register_url = f"{self.hub_url.rstrip('/')}/api/rest/camera/stream-register"
            logger.info(f"Registering stream with Hub: POST {register_url}")

            resp = requests.post(
                register_url,
                json={
                    "api_key": self.api_key,
                    "drone_id": self.drone_id,
                    "webrtc_url": webrtc_url,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                self._stream_registered = True
                self._last_heartbeat = time.time()
                logger.info(f"Registered with Hub: {webrtc_url}")
                return True
            else:
                logger.warning(f"Hub registration failed ({resp.status_code}): {resp.text}")
                return False
        except Exception as e:
            logger.warning(f"Hub registration error: {e}")
            return False

    def _unregister_stream_from_hub(self):
        """Unregister from Hub on shutdown."""
        if not self.hub_url or not self.drone_id or not self.api_key:
            return
        if not self._stream_registered:
            return

        try:
            unregister_url = f"{self.hub_url.rstrip('/')}/api/rest/camera/stream-unregister"
            requests.post(
                unregister_url,
                json={
                    "api_key": self.api_key,
                    "drone_id": self.drone_id,
                },
                timeout=5,
            )
            logger.info("Unregistered from Hub")
        except Exception as e:
            logger.warning(f"Hub unregistration error: {e}")

    async def run(self):
        """Main service loop - 2-second cadence."""
        self.running = True
        logger.info(f"Starting Caribou camera stream service")
        logger.info(f"  RTSP URL: {self.rtsp_url}")
        logger.info(f"  go2rtc API port: {self.api_port}, WebRTC port: {self.webrtc_port}")
        if self.hub_url:
            logger.info(f"  Hub URL: {self.hub_url}")
            logger.info(f"  Drone ID: {self.drone_id}")

        # Set up Tailscale funnel if not skipped
        if not self.skip_funnel_setup:
            setup_tailscale_funnel(self.api_port, self.funnel_port)

        while self.running:
            # Check if go2rtc is running
            if self.go2rtc_process is None or self.go2rtc_process.poll() is not None:
                if self.reconnect_count >= self.max_reconnects:
                    logger.error(
                        f"Max reconnects ({self.max_reconnects}) reached. "
                        f"Cooling down 30s before reset..."
                    )
                    await asyncio.sleep(30)
                    self.reconnect_count = 0
                    continue

                logger.info(f"Starting go2rtc (attempt {self.reconnect_count + 1})...")
                if not self._start_go2rtc():
                    self.reconnect_count += 1
                    await asyncio.sleep(5)
                    continue

            # Health check
            was_healthy = self.stream_healthy
            self.stream_healthy = self._check_stream_health()

            # Became unhealthy - restart go2rtc
            if was_healthy and not self.stream_healthy:
                logger.warning("Stream became unhealthy - restarting go2rtc")
                self._stream_registered = False
                self._detected_webrtc_url = None
                self._stop_go2rtc()
                self.reconnect_count += 1
                await asyncio.sleep(2)
                continue

            # Healthy - handle URL detection and Hub registration
            if self.stream_healthy:
                # Detect URL if not cached
                if not self._detected_webrtc_url:
                    self._detected_webrtc_url = self._detect_webrtc_url()

                now = time.time()

                if self._detected_webrtc_url and not self._stream_registered:
                    # Retry registration with backoff
                    if (now - self._last_registration_attempt) >= self._registration_retry_interval:
                        self._register_stream_with_hub(self._detected_webrtc_url)

                elif self._stream_registered:
                    # Heartbeat re-registration every 5 minutes
                    if (now - self._last_heartbeat) >= self._registration_heartbeat_interval:
                        logger.info("Heartbeat - forcing re-registration with Hub")
                        self._stream_registered = False  # Will trigger re-registration next cycle

                # Reset reconnect counter on sustained health
                if self.reconnect_count > 0:
                    self.reconnect_count = 0

            await asyncio.sleep(2)

        # Cleanup on exit
        self._unregister_stream_from_hub()
        self._stop_go2rtc()
        logger.info("Caribou camera stream service stopped")


# --- Signal handling --------------------------------------------------------

def setup_signal_handlers(service: WebRTCStreamingService):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    def handler(signum, frame):
        sig_name = signal.Signals(signum).name
        logger.info(f"Received {sig_name} - shutting down gracefully")
        service.running = False

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


# --- CLI --------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Caribou Camera Stream Service - manages go2rtc and Hub registration for any RTSP source",
    )
    parser.add_argument(
        "--rtsp-url", required=True,
        help="Full RTSP URL for the camera (e.g. rtsp://192.168.1.100:8554/stream)",
    )
    parser.add_argument(
        "--api-port", type=int, default=GO2RTC_API_PORT,
        help=f"go2rtc HTTP API port (default: {GO2RTC_API_PORT})",
    )
    parser.add_argument(
        "--webrtc-port", type=int, default=GO2RTC_WEBRTC_PORT,
        help=f"go2rtc WebRTC UDP port (default: {GO2RTC_WEBRTC_PORT})",
    )
    parser.add_argument(
        "--funnel-port", type=int, default=TAILSCALE_FUNNEL_PORT,
        choices=[443, 8443, 10000],
        help=f"Tailscale funnel port: 443 | 8443 | 10000 (default: {TAILSCALE_FUNNEL_PORT})",
    )
    parser.add_argument(
        "--hub-url",
        help="Caribou Hub server URL (e.g. https://arrowhub-5j6w8bkt.manus.space)",
    )
    parser.add_argument(
        "--drone-id", default="caribou_001",
        help="Drone identifier for Hub registration (default: caribou_001)",
    )
    parser.add_argument(
        "--api-key",
        help="API key for Hub authentication",
    )
    parser.add_argument(
        "--public-url",
        help="Override public WHEP URL (skip Tailscale auto-detection)",
    )
    parser.add_argument(
        "--skip-funnel-setup", action="store_true",
        help="Skip tailscale serve/funnel commands (use if already configured externally)",
    )
    return parser.parse_args()


# --- Main -------------------------------------------------------------------

def main():
    args = parse_args()

    # Also check environment variables as fallback (for systemd EnvironmentFile)
    hub_url = args.hub_url or os.environ.get("HUB_URL")
    drone_id = args.drone_id or os.environ.get("DRONE_ID", "caribou_001")
    api_key = args.api_key or os.environ.get("API_KEY")

    service = WebRTCStreamingService(
        rtsp_url=args.rtsp_url,
        api_port=args.api_port,
        webrtc_port=args.webrtc_port,
        funnel_port=args.funnel_port,
        hub_url=hub_url,
        drone_id=drone_id,
        api_key=api_key,
        public_url=args.public_url,
        skip_funnel_setup=args.skip_funnel_setup,
    )

    setup_signal_handlers(service)
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
