#!/usr/bin/env python3
"""
Caribou Hub - Logs & OTA Service (Companion Computer)

Runs on the Raspberry Pi CM5 inside the airframe and provides:

  1. FC log sync       - background sync of .BIN files from FC via ArduPilot
                          net_webserver HTTP
  2. FC log serving    - serve locally-cached logs to Hub for user download
  3. OTA firmware flash - download .abin/.apj from Hub, transfer to FC via:
                          (a) Tier 1: FC pulls from companion HTTP server (~650 KB/s)
                          (b) Tier 2: HTTP PUT to FC web server (~650 KB/s)
                          (c) Tier 3: MAVFTP (~5 KB/s, last resort)
  4. System diagnostics - CPU, memory, disk, temp, services -> report to Hub
  5. Remote log stream  - journalctl -f -> Socket.IO -> browser

Architecture:
  Pi --HTTP-->     FC (ArduPilot net_webserver on port 8080 for log download)
  Pi --MAVSDK-->   FC (UDP/Ethernet for firmware flash + arm state)
  Pi --REST/WS--> Caribou Hub (REST API + Socket.IO for real-time progress)

This is OPTIONAL and PARALLEL to the core CSU telemetry stack. csu.service is
independent and unaffected if this service is removed.

MAVLink endpoint note for Caribou:
  csu.service (MAVLink.py) already listens on udpin://0.0.0.0:14540 for telemetry.
  This service needs its own MAVSDK connection (for MAVFTP + arm state + reboot).
  Default is udpin://0.0.0.0:14550. The Pixhawk's NET_P2_* parameters must be
  configured to push a second MAVLink stream to port 14550, OR a mavlink-router
  on the Pi can fan a single inbound stream out to multiple local endpoints.

Job types handled (polled from Hub droneJobs queue):
  - scan_fc_logs     -> list locally-cached logs from manifest
  - download_fc_log  -> serve a cached .BIN file from local store -> Hub
  - flash_firmware   -> upload .abin to FC and monitor flash stages

Usage:
    python3 logs_ota_service.py \\
        --hub-url https://your-hub.example.com \\
        --drone-id caribou_001 \\
        --api-key YOUR_API_KEY \\
        --fc-connection udpin://0.0.0.0:14550

Dependencies:
    pip install --break-system-packages mavsdk aiohttp \\
        python-socketio[asyncio_client] psutil requests
"""

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import platform
import re
import signal
import subprocess
import sys
import tempfile
import time
import zlib
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

# Optional imports with graceful fallback
try:
    import requests
except ImportError:
    requests = None

try:
    import psutil
except ImportError:
    psutil = None

try:
    import socketio
except ImportError:
    socketio = None

try:
    from mavsdk import System
    from mavsdk.ftp import FtpResult
except ImportError:
    System = None
    FtpResult = None

try:
    from aiohttp import web as aiohttp_web
except ImportError:
    aiohttp_web = None
    logging.getLogger("logs_ota").warning(
        "aiohttp not installed -> Tier 1 (fast HTTP pull) disabled. "
        "Install with: pip install --break-system-packages aiohttp"
    )

# --- Logging ----------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("logs_ota")


# --- Hub REST Client --------------------------------------------------------

class HubClient:
    """
    REST + tRPC client for communicating with Caribou Hub.
    """

    def __init__(self, hub_url: str, drone_id: str, api_key: str):
        self.hub_url = hub_url.rstrip("/")
        self.drone_id = drone_id
        self.api_key = api_key
        self.session = requests.Session() if requests else None

    # -- tRPC helpers --------------------------------------------------------

    def _trpc_query(self, procedure: str, input_data: dict) -> Optional[dict]:
        """Call a tRPC query endpoint (GET)."""
        if not self.session:
            logger.error("requests library not installed")
            return None
        try:
            url = f"{self.hub_url}/api/trpc/{procedure}"
            params = {"input": json.dumps({"json": input_data})}
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json().get("result", {}).get("data", {}).get("json")
        except Exception as e:
            logger.error(f"tRPC query {procedure} failed: {e}")
            return None

    def _trpc_mutation(self, procedure: str, input_data: dict) -> Optional[dict]:
        """Call a tRPC mutation endpoint (POST)."""
        if not self.session:
            logger.error("requests library not installed")
            return None
        try:
            url = f"{self.hub_url}/api/trpc/{procedure}"
            resp = self.session.post(url, json={"json": input_data}, timeout=15)
            resp.raise_for_status()
            return resp.json().get("result", {}).get("data", {}).get("json")
        except Exception as e:
            logger.error(f"tRPC mutation {procedure} failed: {e}")
            return None

    def _rest_post(self, path: str, data: dict, timeout: int = 30) -> Optional[dict]:
        """Call a REST POST endpoint."""
        if not self.session:
            logger.error("requests library not installed")
            return None
        try:
            url = f"{self.hub_url}/api/rest/{path}"
            resp = self.session.post(url, json=data, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"REST POST {path} failed: {e}")
            return None

    # -- Job polling ---------------------------------------------------------

    def get_pending_jobs(self) -> List[dict]:
        result = self._trpc_query("droneJobs.getPendingJobs", {
            "droneId": self.drone_id,
            "apiKey": self.api_key,
        })
        if result:
            return result.get("jobs", [])
        return []

    def acknowledge_job(self, job_id: int, locked_by: Optional[str] = None) -> bool:
        data: dict = {
            "jobId": job_id,
            "apiKey": self.api_key,
            "droneId": self.drone_id,
        }
        if locked_by:
            data["lockedBy"] = locked_by
        result = self._trpc_mutation("droneJobs.acknowledgeJob", data)
        if result is None:
            return False
        return result.get("success", False)

    def complete_job(self, job_id: int, success: bool, error_message: Optional[str] = None) -> bool:
        data: dict = {
            "jobId": job_id,
            "apiKey": self.api_key,
            "droneId": self.drone_id,
            "success": success,
        }
        if error_message is not None:
            data["errorMessage"] = error_message
        result = self._trpc_mutation("droneJobs.completeJob", data)
        return result is not None

    # -- FC log endpoints ----------------------------------------------------

    def report_fc_log_list(self, logs: List[dict]) -> bool:
        result = self._rest_post("logs/fc-list", {
            "api_key": self.api_key,
            "drone_id": self.drone_id,
            "logs": logs,
        })
        return result is not None and result.get("success", False)

    def report_fc_log_progress(self, log_id: int, status: str, progress: int,
                                error_message: Optional[str] = None) -> bool:
        result = self._rest_post("logs/fc-progress", {
            "api_key": self.api_key,
            "drone_id": self.drone_id,
            "log_id": log_id,
            "status": status,
            "progress": progress,
            "error_message": error_message,
        })
        return result is not None and result.get("success", False)

    def upload_fc_log(self, log_id: int, filename: str,
                       content: bytes, file_size: int) -> Optional[str]:
        """Upload a downloaded FC log file to the Hub.

        Multipart first (no base64 overhead, ~33% faster). Falls back to
        base64 JSON if multipart endpoint is unavailable.
        """
        url = f"{self.hub_url}/api/rest/logs/fc-upload-multipart"
        try:
            resp = requests.post(url, data={
                "api_key": self.api_key,
                "drone_id": self.drone_id,
                "log_id": str(log_id),
                "filename": filename,
                "file_size": str(file_size),
            }, files={
                "file": (filename, content, "application/octet-stream"),
            }, timeout=300)
            if resp.status_code == 200:
                result = resp.json()
                if result.get("success"):
                    return result.get("url")
            elif resp.status_code == 404:
                logger.warning("Multipart upload not available, falling back to base64")
            else:
                logger.error(f"Multipart upload failed: {resp.status_code} {resp.text[:200]}")
                return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"Multipart upload error, falling back to base64: {e}")

        # Fallback: base64 JSON (legacy)
        encoded = base64.b64encode(content).decode("ascii")
        result = self._rest_post("logs/fc-upload", {
            "api_key": self.api_key,
            "drone_id": self.drone_id,
            "log_id": log_id,
            "filename": filename,
            "content": encoded,
            "file_size": file_size,
        }, timeout=300)
        if result and result.get("success"):
            return result.get("url")
        return None

    # -- Firmware endpoints --------------------------------------------------

    def report_firmware_progress(self, update_id: int, status: str,
                                  progress: int, flash_stage: Optional[str] = None,
                                  error_message: Optional[str] = None,
                                  firmware_version: Optional[str] = None) -> bool:
        data: dict = {
            "api_key": self.api_key,
            "drone_id": self.drone_id,
            "update_id": update_id,
            "status": status,
            "progress": progress,
        }
        if flash_stage:
            data["flash_stage"] = flash_stage
        if error_message:
            data["error_message"] = error_message
        if firmware_version:
            data["firmware_version"] = firmware_version
        result = self._rest_post("firmware/progress", data)
        return result is not None and result.get("success", False)

    # -- Diagnostics ---------------------------------------------------------

    def report_diagnostics(self, diag: dict) -> bool:
        result = self._rest_post("diagnostics/report", {
            "api_key": self.api_key,
            "drone_id": self.drone_id,
            **diag,
        })
        return result is not None and result.get("success", False)


# --- FC Log Directory HTML Parser ------------------------------------------

class _FCLogDirParser(HTMLParser):
    """
    Parse the HTML directory listing returned by ArduPilot net_webserver.
    Expected row format:
      <tr><td><a href="00000042.BIN">00000042.BIN</a></td>
          <td>2026-04-16 12:34</td>
          <td>1234567</td></tr>
    """

    def __init__(self):
        super().__init__()
        self.entries: List[Dict[str, Any]] = []
        self._in_td = False
        self._in_a = False
        self._current_href: Optional[str] = None
        self._row_cells: List[str] = []
        self._current_text = ""

    def handle_starttag(self, tag: str, attrs: list):
        if tag == "tr":
            self._row_cells = []
        elif tag == "td":
            self._in_td = True
            self._current_text = ""
        elif tag == "a" and self._in_td:
            self._in_a = True
            for name, value in attrs:
                if name == "href":
                    self._current_href = value

    def handle_endtag(self, tag: str):
        if tag == "td" and self._in_td:
            self._in_td = False
            self._row_cells.append(self._current_text.strip())
            self._current_text = ""
        elif tag == "a":
            self._in_a = False
        elif tag == "tr" and len(self._row_cells) >= 3:
            name = self._row_cells[0]
            modtime = self._row_cells[1]
            size_str = self._row_cells[2]
            if name in ("Name", "..", ".", ""):
                self._current_href = None
                return
            size = 0
            if size_str and size_str != "0":
                try:
                    if size_str.upper().endswith("M"):
                        size = int(size_str[:-1]) * 1_000_000
                    else:
                        size = int(size_str)
                except ValueError:
                    pass
            is_dir = name.endswith("/")
            self.entries.append({
                "name": name.rstrip("/"),
                "size": size,
                "type": "directory" if is_dir else "file",
                "modtime": modtime,
                "href": self._current_href or name,
            })
            self._current_href = None

    def handle_data(self, data: str):
        if self._in_td:
            self._current_text += data


def parse_fc_log_directory(html: str) -> List[Dict[str, Any]]:
    parser = _FCLogDirParser()
    parser.feed(html)
    return parser.entries


# --- FC Log Syncer (HTTP-based) --------------------------------------------

class FCLogSyncer:
    """
    Background syncer that downloads FC log files from the ArduPilot
    net_webserver over HTTP and stores them locally on the companion computer.

    Avoids blocking the MAVLink TCP connection (which MAVFTP does) and
    provides fast local access for the dashboard.

    Safety: Only syncs when the drone is DISARMED.

    Local store layout:
      {log_store_dir}/
        manifest.json          - sync state for each file
        00000042.BIN           - cached log file
        00000043.BIN
        ...
    """

    DEFAULT_FC_WEBSERVER_URL = "http://192.168.144.10:8080"
    DEFAULT_LOG_STORE_DIR = "/var/lib/caribou/fc_logs"
    LOGS_PATH = "/mnt/APM/LOGS/"
    MANIFEST_FILE = "manifest.json"
    SYNC_INTERVAL = 60
    DOWNLOAD_CHUNK_SIZE = 65536
    MAX_DOWNLOAD_ATTEMPTS = 3

    def __init__(self, fc_webserver_url: str = None,
                 log_store_dir: str = None,
                 mavsdk_system=None):
        self.fc_url = (fc_webserver_url or self.DEFAULT_FC_WEBSERVER_URL).rstrip("/")
        self.log_store_dir = Path(log_store_dir or self.DEFAULT_LOG_STORE_DIR)
        self.mavsdk_system = mavsdk_system
        self._manifest: Dict[str, dict] = {}
        self._syncing = False
        self._last_sync_time: Optional[float] = None
        self._last_sync_error: Optional[str] = None
        self._armed = False
        self._arm_state_known = False

    def _ensure_store_dir(self):
        self.log_store_dir.mkdir(parents=True, exist_ok=True)

    def _manifest_path(self) -> Path:
        return self.log_store_dir / self.MANIFEST_FILE

    def _load_manifest(self):
        path = self._manifest_path()
        if path.exists():
            try:
                with open(path, "r") as f:
                    self._manifest = json.load(f)
                logger.debug(f"Loaded manifest: {len(self._manifest)} entries")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load manifest, starting fresh: {e}")
                self._manifest = {}
        else:
            self._manifest = {}

    def _save_manifest(self):
        try:
            with open(self._manifest_path(), "w") as f:
                json.dump(self._manifest, f, indent=2)
        except OSError as e:
            logger.error(f"Failed to save manifest: {e}")

    async def _check_arm_state(self) -> bool:
        if self.mavsdk_system is None:
            return True
        try:
            async for armed in self.mavsdk_system.telemetry.armed():
                self._armed = armed
                self._arm_state_known = True
                return not armed
        except Exception as e:
            logger.debug(f"Arm state check failed: {e}")
            if not self._arm_state_known:
                return True
            return not self._armed

    async def _fetch_directory_listing(self) -> Optional[List[Dict[str, Any]]]:
        url = f"{self.fc_url}{self.LOGS_PATH}"
        try:
            if not requests:
                logger.error("requests library not installed")
                return None
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                logger.warning(f"Unexpected content-type from FC: {content_type}")
                return None
            entries = parse_fc_log_directory(resp.text)
            log_entries = [
                e for e in entries
                if e["type"] == "file" and (
                    e["name"].upper().endswith(".BIN") or
                    e["name"].lower().endswith(".log")
                )
            ]
            logger.info(f"FC webserver: found {len(log_entries)} log file(s) "
                        f"(of {len(entries)} total entries)")
            return log_entries
        except requests.exceptions.ConnectionError:
            logger.debug(f"FC webserver not reachable at {url}")
            return None
        except requests.exceptions.Timeout:
            logger.debug(f"FC webserver timeout at {url}")
            return None
        except Exception as e:
            logger.error(f"Failed to fetch FC log directory: {e}")
            return None

    def _is_skipped(self, filename: str) -> bool:
        entry = self._manifest.get(filename, {})
        return entry.get("skipped", False)

    def _record_attempt(self, filename: str, success: bool, error: str = None):
        entry = self._manifest.setdefault(filename, {})
        attempts = entry.get("attempts", 0)
        if success:
            entry["attempts"] = 0
            entry["skipped"] = False
            entry["skip_reason"] = None
        else:
            attempts += 1
            entry["attempts"] = attempts
            entry["last_error"] = error or "unknown"
            if attempts >= self.MAX_DOWNLOAD_ATTEMPTS:
                entry["skipped"] = True
                entry["skip_reason"] = (f"Failed {attempts} times, last error: "
                                         f"{error or 'unknown'}")
                logger.warning(
                    f"Permanently skipping {filename} after {attempts} failed attempts "
                    f"(reason: {error or 'unknown'}). "
                    f"Delete the 'skipped' key in manifest.json to retry.")
            else:
                logger.info(f"Download attempt {attempts}/{self.MAX_DOWNLOAD_ATTEMPTS} "
                            f"failed for {filename}: {error or 'unknown'}")
        self._save_manifest()

    def reset_skipped(self, filename: str = None):
        if filename:
            entry = self._manifest.get(filename)
            if entry:
                entry["skipped"] = False
                entry["attempts"] = 0
                entry["skip_reason"] = None
                logger.info(f"Reset skip status for {filename}")
        else:
            for fn, entry in self._manifest.items():
                if entry.get("skipped"):
                    entry["skipped"] = False
                    entry["attempts"] = 0
                    entry["skip_reason"] = None
            logger.info("Reset skip status for all files")
        self._save_manifest()

    async def _download_log_file(self, entry: Dict[str, Any]) -> bool:
        filename = entry["name"]
        url = f"{self.fc_url}{self.LOGS_PATH}{entry['href']}"
        local_path = self.log_store_dir / filename
        tmp_path = self.log_store_dir / f".{filename}.tmp"

        try:
            logger.info(f"Downloading {filename} from FC webserver...")
            resp = requests.get(url, stream=True, timeout=600)
            resp.raise_for_status()

            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            sha256 = hashlib.sha256()

            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=self.DOWNLOAD_CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        sha256.update(chunk)
                        downloaded += len(chunk)

                        if downloaded % (512 * 1024) < self.DOWNLOAD_CHUNK_SIZE:
                            await asyncio.sleep(0)

                        if downloaded % (4 * 1024 * 1024) < self.DOWNLOAD_CHUNK_SIZE:
                            if not await self._check_arm_state():
                                logger.warning(
                                    f"Drone ARMED during download of {filename}, "
                                    f"aborting for safety ({downloaded}/{total_size} bytes)")
                                try:
                                    tmp_path.unlink()
                                except OSError:
                                    pass
                                self._record_attempt(filename, False, "Drone armed during download")
                                return False

            tmp_path.rename(local_path)
            file_size = local_path.stat().st_size
            file_hash = sha256.hexdigest()

            logger.info(f"Downloaded {filename}: {file_size} bytes, "
                        f"SHA-256: {file_hash[:16]}...")

            self._manifest[filename] = {
                "filename": filename,
                "remote_size": entry.get("size", 0) or file_size,
                "remote_modtime": entry.get("modtime", ""),
                "local_size": file_size,
                "synced": True,
                "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "sha256": file_hash,
                "attempts": 0,
                "skipped": False,
                "skip_reason": None,
            }
            self._save_manifest()
            self._record_attempt(filename, True)
            return True

        except Exception as e:
            logger.error(f"Failed to download {filename}: {e}")
            try:
                tmp_path.unlink()
            except OSError:
                pass
            self._record_attempt(filename, False, str(e))
            return False

    def _needs_sync(self, entry: Dict[str, Any]) -> bool:
        filename = entry["name"]
        existing = self._manifest.get(filename)
        if not existing:
            return True
        if not existing.get("synced"):
            return True
        local_path = self.log_store_dir / filename
        if not local_path.exists():
            return True
        remote_size = entry.get("size", 0)
        if remote_size > 0 and existing.get("local_size", 0) != remote_size:
            return True
        remote_modtime = entry.get("modtime", "")
        if remote_modtime and existing.get("remote_modtime") != remote_modtime:
            return True
        return False

    async def sync_once(self, download: bool = False) -> Dict[str, Any]:
        self._syncing = True
        summary = {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "fc_reachable": False,
            "armed": self._armed,
            "files_found": 0,
            "files_synced": 0,
            "files_skipped": 0,
            "files_failed": 0,
            "error": None,
        }

        try:
            if not await self._check_arm_state():
                summary["armed"] = True
                summary["error"] = "Drone is ARMED - skipping sync for safety"
                logger.info(summary["error"])
                return summary

            summary["armed"] = False

            entries = await self._fetch_directory_listing()
            if entries is None:
                summary["error"] = "FC webserver not reachable"
                return summary

            summary["fc_reachable"] = True
            summary["files_found"] = len(entries)

            for entry in entries:
                filename = entry["name"]
                if filename not in self._manifest:
                    self._manifest[filename] = {
                        "filename": filename,
                        "remote_size": entry.get("size", 0),
                        "remote_modtime": entry.get("modtime", ""),
                        "local_size": 0,
                        "synced": False,
                        "synced_at": None,
                        "sha256": None,
                    }

            if download:
                for entry in entries:
                    filename = entry["name"]

                    if self._is_skipped(filename):
                        summary["files_skipped"] += 1
                        continue

                    if not self._needs_sync(entry):
                        summary["files_skipped"] += 1
                        continue

                    if not await self._check_arm_state():
                        summary["armed"] = True
                        summary["error"] = "Drone ARMED during sync - stopping"
                        logger.warning(summary["error"])
                        break

                    if await self._download_log_file(entry):
                        summary["files_synced"] += 1
                    else:
                        summary["files_failed"] += 1
            else:
                for entry in entries:
                    filename = entry["name"]
                    if self._is_skipped(filename):
                        summary["files_skipped"] += 1
                    elif self._needs_sync(entry):
                        summary["files_skipped"] += 1
                    else:
                        summary["files_skipped"] += 1

            self._save_manifest()
            self._last_sync_time = time.time()
            self._last_sync_error = summary.get("error")

        except Exception as e:
            summary["error"] = str(e)
            logger.error(f"Sync cycle failed: {e}")
            self._last_sync_error = str(e)
        finally:
            self._syncing = False

        return summary

    async def run_sync_loop(self, running_flag):
        self._ensure_store_dir()
        self._load_manifest()

        logger.info(f"FCLogSyncer started (scan-only) - store: {self.log_store_dir}, "
                    f"FC: {self.fc_url}")

        while running_flag():
            try:
                summary = await self.sync_once(download=False)
                if summary.get("error"):
                    logger.debug(f"Scan summary: {summary}")
                elif summary["files_found"] > 0:
                    logger.debug(
                        f"Scan complete: {summary['files_found']} files on FC, "
                        f"{summary['files_skipped']} in manifest")
                else:
                    logger.debug(f"Scan: FC not reachable or no files found")
            except Exception as e:
                logger.error(f"Sync loop error: {e}")

            await asyncio.sleep(self.SYNC_INTERVAL)

    def get_cached_logs(self) -> List[Dict[str, Any]]:
        logs = []
        for filename, entry in sorted(self._manifest.items()):
            if filename.upper().endswith(".BIN") or filename.lower().endswith(".log"):
                local_path = self.log_store_dir / filename
                logs.append({
                    "remote_path": f"/APM/LOGS/{filename}",
                    "filename": filename,
                    "file_size": entry.get("local_size", 0) or entry.get("remote_size", 0),
                    "synced": entry.get("synced", False) and local_path.exists(),
                    "synced_at": entry.get("synced_at"),
                    "sha256": entry.get("sha256"),
                    "remote_modtime": entry.get("remote_modtime", ""),
                })
        return logs

    def get_local_file_path(self, filename: str) -> Optional[Path]:
        entry = self._manifest.get(filename)
        if not entry or not entry.get("synced"):
            return None
        path = self.log_store_dir / filename
        return path if path.exists() else None

    def get_status(self) -> Dict[str, Any]:
        total = len(self._manifest)
        synced = sum(1 for e in self._manifest.values() if e.get("synced"))
        return {
            "fc_webserver_url": self.fc_url,
            "log_store_dir": str(self.log_store_dir),
            "total_logs": total,
            "synced_logs": synced,
            "pending_logs": total - synced,
            "syncing": self._syncing,
            "last_sync_time": self._last_sync_time,
            "last_sync_error": self._last_sync_error,
            "armed": self._armed,
        }


# --- MAVFTP Operations ------------------------------------------------------

class MavFtpClient:
    """
    Wraps MAVSDK FTP plugin for file operations on the flight controller.
    """

    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self.system: Optional[Any] = None
        self._connected = False

    async def connect(self) -> bool:
        if System is None:
            logger.error("MAVSDK not installed. Run: pip install mavsdk")
            return False

        try:
            self.system = System()
            logger.info(f"Connecting to FC: {self.connection_string}")
            await self.system.connect(system_address=self.connection_string)

            logger.info("Waiting for FC heartbeat (15s timeout)...")
            try:
                async with asyncio.timeout(15):
                    async for state in self.system.core.connection_state():
                        if state.is_connected:
                            logger.info("Connected to flight controller")
                            self._connected = True
                            break
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning("No FC heartbeat within 15s")
                return False
            return True
        except Exception as e:
            logger.error(f"Failed to connect to FC: {e}")
            return False

    @property
    def connected(self) -> bool:
        return self._connected and self.system is not None

    async def list_directory(self, remote_path: str) -> List[dict]:
        if not self.connected:
            raise RuntimeError("Not connected to FC")

        try:
            result = await self.system.ftp.list_directory(remote_path)
            entries = []
            dirs_list = getattr(result, 'dirs', None) or []
            files_list = getattr(result, 'files', None) or []

            for dir_name in dirs_list:
                name = str(dir_name).strip()
                if name and name not in ('.', '..'):
                    entries.append({"name": name, "size": 0, "type": "directory"})

            for file_name in files_list:
                name = str(file_name).strip()
                if name:
                    entries.append({"name": name, "size": 0, "type": "file"})

            logger.info(f"Listed {remote_path}: {len(dirs_list)} dirs, {len(files_list)} files")
            return entries
        except Exception as e:
            logger.error(f"Failed to list directory {remote_path}: {e}")
            raise

    async def download_file(self, remote_path: str, local_path: str,
                             progress_callback=None) -> bool:
        if not self.connected:
            raise RuntimeError("Not connected to FC")

        try:
            logger.info(f"Downloading {remote_path} -> {local_path}")
            os.makedirs(os.path.dirname(local_path) or '.', exist_ok=True)
            local_dir = os.path.dirname(local_path) or '.'

            async for progress_data in self.system.ftp.download(
                remote_path, local_dir, use_burst=True
            ):
                if progress_callback and progress_data:
                    await progress_callback(
                        progress_data.bytes_transferred,
                        progress_data.total_bytes
                    )

            remote_filename = os.path.basename(remote_path)
            downloaded_path = os.path.join(local_dir, remote_filename)

            if downloaded_path != local_path and os.path.exists(downloaded_path):
                os.rename(downloaded_path, local_path)

            if os.path.exists(local_path):
                file_size = os.path.getsize(local_path)
                logger.info(f"Download complete: {file_size} bytes")
                if progress_callback:
                    await progress_callback(file_size, file_size)
                return True
            else:
                logger.error(f"Download completed but file not found at {local_path}")
                return False

        except Exception as e:
            logger.error(f"MAVFTP download failed for {remote_path}: {e}")
            raise

    async def upload_file(self, local_path: str, remote_dir: str,
                           progress_callback=None) -> bool:
        if not self.connected:
            raise RuntimeError("Not connected to FC")

        try:
            file_size = os.path.getsize(local_path)
            local_filename = os.path.basename(local_path)
            logger.info(f"Uploading {local_path} -> {remote_dir}{local_filename} ({file_size} bytes)")

            async for progress_data in self.system.ftp.upload(
                local_path, remote_dir
            ):
                if progress_callback and progress_data:
                    await progress_callback(
                        progress_data.bytes_transferred,
                        progress_data.total_bytes
                    )

            logger.info("Upload complete")
            if progress_callback:
                await progress_callback(file_size, file_size)
            return True

        except Exception as e:
            logger.error(f"MAVFTP upload failed: {e}")
            raise

    async def ensure_ready(self, retries: int = 3, delay: float = 3.0) -> bool:
        if self.connected:
            try:
                await self.system.ftp.list_directory("/APM/")
                return True
            except Exception as e:
                logger.warning(f"FTP health check failed: {e} -> will reconnect")
                self._connected = False

        for attempt in range(1, retries + 1):
            logger.info(f"FTP reconnect attempt {attempt}/{retries} "
                        f"(waiting {delay}s for MAVSDK to settle)...")
            await asyncio.sleep(delay)
            try:
                success = await self.connect()
                if success:
                    logger.info("FTP reconnected successfully")
                    return True
            except Exception as e:
                logger.warning(f"FTP reconnect attempt {attempt} failed: {e}")

        logger.error(f"FTP reconnection failed after {retries} attempts")
        return False

    async def file_exists(self, remote_path: str) -> bool:
        try:
            parent = "/".join(remote_path.rstrip("/").split("/")[:-1]) or "/"
            filename = remote_path.rstrip("/").split("/")[-1]
            entries = await self.list_directory(parent)
            return any(e["name"] == filename for e in entries)
        except Exception:
            return False

    async def remove_file(self, remote_path: str) -> bool:
        if not self.connected:
            raise RuntimeError("Not connected to FC")
        try:
            await self.system.ftp.remove_file(remote_path)
            return True
        except Exception as e:
            logger.error(f"Failed to remove {remote_path}: {e}")
            return False


# --- Job Handlers ----------------------------------------------------------

class LogsOtaJobHandler:
    """Handles scan_fc_logs, download_fc_log, and flash_firmware jobs."""

    FC_APM_PATH = "/mnt/APM/"
    HTTP_TIMEOUT = 5

    FIRMWARE_SERVER_PORT = 8070
    FIRMWARE_SERVER_ACK_TIMEOUT = 300

    def __init__(self, hub: HubClient, ftp: MavFtpClient,
                 log_syncer: Optional['FCLogSyncer'] = None,
                 log_path: str = "/APM/LOGS"):
        self.hub = hub
        self.ftp = ftp
        self.log_syncer = log_syncer
        self.log_path = log_path
        self.fc_url = log_syncer.fc_url if log_syncer else None
        self._fw_server_runner = None
        self._fw_serve_path = None
        self._fw_serve_size = 0
        self._fw_serve_downloaded = False
        self._fw_serve_bytes_sent = 0

    @staticmethod
    def _extract_abin_git_hash(abin_path: str) -> Optional[str]:
        try:
            with open(abin_path, 'rb') as f:
                header = f.read(512)
            try:
                header_text = header.decode('ascii', errors='replace')
            except Exception:
                return None
            for line in header_text.split('\n'):
                line = line.strip()
                if line.lower().startswith('git version:'):
                    git_hash = line.split(':', 1)[1].strip()
                    git_hash = git_hash.split()[0] if git_hash else None
                    if git_hash:
                        logger.info(f"Extracted git hash from .abin header: {git_hash}")
                        return git_hash
            logger.warning("No 'git version:' line found in .abin header")
            return None
        except Exception as e:
            logger.warning(f"Failed to read .abin header: {e}")
            return None

    async def _query_autopilot_version(self) -> Optional[dict]:
        if not self.ftp.connected or not self.ftp.system:
            logger.warning("Cannot query AUTOPILOT_VERSION - MAVSDK not connected")
            return None

        try:
            version = await asyncio.wait_for(
                self.ftp.system.info.get_version(),
                timeout=10
            )
            result = {
                "flight_sw_version": f"{version.flight_sw_major}.{version.flight_sw_minor}.{version.flight_sw_patch}",
                "flight_custom_version": getattr(version, 'flight_custom_version', ''),
                "os_custom_version": getattr(version, 'os_custom_version', ''),
            }
            logger.info(f"AUTOPILOT_VERSION: sw={result['flight_sw_version']}, "
                        f"git={result['flight_custom_version']}")
            return result
        except asyncio.TimeoutError:
            logger.warning("AUTOPILOT_VERSION query timed out (10s)")
            return None
        except Exception as e:
            logger.warning(f"Failed to query AUTOPILOT_VERSION: {e}")
            return None

    @staticmethod
    def _convert_apj_to_abin(apj_data: bytes, output_path: str) -> None:
        """Convert ArduPilot .apj firmware to .abin (text header + raw binary)."""
        try:
            apj = json.loads(apj_data)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"Invalid .apj file: not valid JSON - {e}")

        if apj.get("magic") != "APJFWv1":
            raise ValueError(
                f"Invalid .apj file: expected magic 'APJFWv1', "
                f"got '{apj.get('magic', '<missing>')}'")

        image_b64 = apj.get("image")
        if not image_b64:
            raise ValueError("Invalid .apj file: missing 'image' field")

        try:
            compressed = base64.b64decode(image_b64)
            raw_bin = zlib.decompress(compressed)
        except Exception as e:
            raise ValueError(f"Failed to decode/decompress .apj image: {e}")

        expected_size = apj.get("image_size")
        if expected_size is not None and len(raw_bin) != expected_size:
            raise ValueError(
                f".apj image_size mismatch: header says {expected_size}, "
                f"decompressed {len(raw_bin)} bytes")

        git_hash = apj.get("git_identity", "unknown")
        md5_hex = hashlib.md5(raw_bin).hexdigest()

        with open(output_path, "wb") as f:
            f.write(f"git version: {git_hash}\n".encode())
            f.write(f"MD5: {md5_hex}\n".encode())
            f.write(b"--\n")
            f.write(raw_bin)

        board = apj.get("summary", "unknown")
        logger.info(
            f"Converted .apj -> .abin: board={board}, git={git_hash}, "
            f"size={len(raw_bin)} bytes, md5={md5_hex[:16]}...")

    def _http_file_exists(self, filename: str) -> Optional[bool]:
        if not self.fc_url or not requests:
            return None
        try:
            url = f"{self.fc_url}{self.FC_APM_PATH}{filename}"
            resp = requests.get(url, headers={"Range": "bytes=0-0"},
                                timeout=self.HTTP_TIMEOUT, stream=True)
            resp.close()
            return resp.status_code in (200, 206)
        except Exception:
            return None

    def _http_fc_reachable(self) -> bool:
        if not self.fc_url or not requests:
            return False
        try:
            resp = requests.get(self.fc_url, timeout=self.HTTP_TIMEOUT, stream=True)
            resp.close()
            return resp.status_code == 200
        except Exception:
            return False

    # -- Tier 1: Firmware HTTP Server (FC pulls from Pi) ----------------

    async def _start_firmware_server(self, firmware_path: str) -> bool:
        if not aiohttp_web:
            logger.warning("aiohttp not available - cannot start firmware server")
            return False

        self._fw_serve_path = firmware_path
        self._fw_serve_size = os.path.getsize(firmware_path)
        self._fw_serve_downloaded = False
        self._fw_serve_bytes_sent = 0

        async def handle_status(request):
            if self._fw_serve_path and os.path.exists(self._fw_serve_path):
                return aiohttp_web.json_response({
                    "ready": True,
                    "filename": "ardupilot.abin",
                    "size": self._fw_serve_size,
                })
            return aiohttp_web.json_response({"ready": False})

        async def handle_download(request):
            if not self._fw_serve_path or not os.path.exists(self._fw_serve_path):
                return aiohttp_web.Response(status=404, text="No firmware available")

            try:
                with open(self._fw_serve_path, "rb") as f:
                    firmware_data = f.read()

                self._fw_serve_bytes_sent = len(firmware_data)
                logger.info(f"Serving firmware: {len(firmware_data)} bytes as single response")

                return aiohttp_web.Response(
                    body=firmware_data,
                    content_type="application/octet-stream",
                    headers={
                        "Content-Length": str(len(firmware_data)),
                        "Content-Disposition": 'attachment; filename="ardupilot.abin"',
                    },
                )
            except Exception as e:
                logger.error(f"Failed to serve firmware: {e}")
                return aiohttp_web.Response(status=500, text=str(e))

        async def handle_ack(request):
            self._fw_serve_downloaded = True
            logger.info("FC acknowledged firmware download complete")
            return aiohttp_web.json_response({"status": "ok"})

        app = aiohttp_web.Application()
        app.router.add_get("/firmware/status", handle_status)
        app.router.add_get("/firmware/download", handle_download)
        app.router.add_get("/firmware/ack", handle_ack)

        try:
            runner = aiohttp_web.AppRunner(app)
            await runner.setup()
            site = aiohttp_web.TCPSite(runner, "0.0.0.0", self.FIRMWARE_SERVER_PORT)
            await site.start()
            self._fw_server_runner = runner
            logger.info(f"Firmware HTTP server started on port {self.FIRMWARE_SERVER_PORT}")
            return True
        except Exception as e:
            logger.error(f"Failed to start firmware server: {e}")
            return False

    async def _stop_firmware_server(self):
        if self._fw_server_runner:
            try:
                await self._fw_server_runner.cleanup()
            except Exception as e:
                logger.warning(f"Error stopping firmware server: {e}")
            self._fw_server_runner = None
        self._fw_serve_path = None
        self._fw_serve_size = 0
        self._fw_serve_downloaded = False
        self._fw_serve_bytes_sent = 0

    async def _wait_for_fc_pull(self, update_id: int, timeout: int = None) -> bool:
        if timeout is None:
            timeout = self.FIRMWARE_SERVER_ACK_TIMEOUT

        elapsed = 0
        poll_interval = 2
        last_reported_pct = 0

        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            # Early exit: no FC pull activity after 30s = puller not installed / wrong config
            if elapsed >= 30 and self._fw_serve_bytes_sent == 0:
                logger.warning("No FC pull activity after 30s - "
                               "firmware_puller.lua may not be installed, "
                               "FWPULL_ENABLE=0, or FC can't reach Pi:8070")
                return False

            if self._fw_serve_size > 0 and self._fw_serve_bytes_sent > 0:
                pct = 20 + int(self._fw_serve_bytes_sent / self._fw_serve_size * 40)
                if pct > last_reported_pct:
                    last_reported_pct = pct
                    self.hub.report_firmware_progress(
                        update_id, "transferring", min(pct, 60),
                        flash_stage="uploading_http_pull"
                    )

            if self._fw_serve_downloaded:
                logger.info(f"FC pulled firmware in {elapsed}s "
                            f"({self._fw_serve_bytes_sent} bytes served)")
                return True

            if (self._fw_serve_size > 0 and
                    self._fw_serve_bytes_sent >= self._fw_serve_size):
                logger.info(f"All firmware bytes served ({self._fw_serve_bytes_sent}), "
                            f"treating as complete (ack may be delayed)")
                return True

        logger.warning(f"FC did not pull firmware within {timeout}s "
                       f"(served {self._fw_serve_bytes_sent} / {self._fw_serve_size} bytes)")
        return False

    def _http_upload_firmware(self, local_path: str, progress_callback=None) -> bool:
        """Tier 2: HTTP PUT to net_webserver_put.lua."""
        if not self.fc_url or not requests:
            return False

        url = f"{self.fc_url}/APM/ardupilot.abin"
        file_size = os.path.getsize(local_path)
        put_timeout = (10, 600)

        try:
            logger.info(f"Attempting HTTP PUT upload to {url} ({file_size} bytes)")
            import time as _time
            t0 = _time.monotonic()

            with open(local_path, "rb") as f:
                if progress_callback:
                    class ProgressReader:
                        def __init__(self, fobj, total, callback):
                            self._fobj = fobj
                            self._total = total
                            self._uploaded = 0
                            self._callback = callback

                        def read(self, size=-1):
                            data = self._fobj.read(size)
                            if data:
                                self._uploaded += len(data)
                                self._callback(self._uploaded, self._total)
                            return data

                        def __len__(self):
                            return self._total

                    reader = ProgressReader(f, file_size, progress_callback)
                    resp = requests.put(url, data=reader,
                        headers={"Content-Length": str(file_size),
                                 "Content-Type": "application/octet-stream"},
                        timeout=put_timeout)
                else:
                    data = f.read()
                    resp = requests.put(url, data=data,
                        headers={"Content-Length": str(file_size),
                                 "Content-Type": "application/octet-stream"},
                        timeout=put_timeout)

            elapsed = _time.monotonic() - t0
            speed_kbs = (file_size / 1024) / elapsed if elapsed > 0 else 0

            if resp.status_code == 201:
                logger.info(f"HTTP PUT upload succeeded: {file_size} bytes in "
                            f"{elapsed:.1f}s ({speed_kbs:.1f} KB/s)")
                return True
            elif resp.status_code == 405:
                logger.info("FC web server does not support PUT (stock net_webserver.lua)")
                return False
            elif resp.status_code == 403:
                logger.warning(f"HTTP PUT forbidden: {resp.text}")
                return False
            else:
                logger.warning(f"HTTP PUT returned {resp.status_code}: {resp.text}")
                return False

        except requests.exceptions.ConnectionError:
            logger.info("HTTP PUT connection failed - FC web server may not support PUT")
            return False
        except requests.exceptions.Timeout:
            logger.warning("HTTP PUT timed out (connect=%ss, read=%ss)" % put_timeout)
            return False
        except Exception as e:
            logger.warning(f"HTTP PUT failed: {e}")
            return False

    async def _check_file_exists(self, filename: str, http_only: bool = False) -> bool:
        http_result = await asyncio.to_thread(self._http_file_exists, filename)
        if http_result is not None:
            return http_result
        if http_only:
            return False
        return await self.ftp.file_exists(f"/APM/{filename}")

    async def _verify_fc_reboot(self, update_id: int, max_wait: int = 60):
        if not self.fc_url:
            logger.info("FC web server URL not configured - skipping reboot verification")
            return

        logger.info(f"Waiting up to {max_wait}s for FC to reboot and become reachable via HTTP...")
        self.hub.report_firmware_progress(
            update_id, "completed", 100,
            flash_stage="verifying_reboot"
        )

        elapsed = 0
        poll_interval = 5
        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            if await asyncio.to_thread(self._http_fc_reachable):
                logger.info(f"FC web server reachable after reboot ({elapsed}s)")
                self.hub.report_firmware_progress(
                    update_id, "completed", 100,
                    flash_stage="reboot_verified"
                )
                return

        logger.warning(f"FC web server not reachable after {max_wait}s - "
                       "FC may still be booting or web server not enabled")

    async def handle_scan_fc_logs(self, job: dict) -> Tuple[bool, Optional[str]]:
        """Report available FC log files to the Hub (HTTP first, MAVFTP fallback)."""
        payload = job.get("payload", {})

        try:
            if self.log_syncer:
                logger.info("Scanning FC logs via HTTP (on-demand)...")
                entries = await self.log_syncer._fetch_directory_listing()
                if entries is not None:
                    self.log_syncer._ensure_store_dir()
                    self.log_syncer._load_manifest()
                    for entry in entries:
                        fn = entry["name"]
                        if fn not in self.log_syncer._manifest:
                            self.log_syncer._manifest[fn] = {
                                "filename": fn,
                                "remote_size": entry.get("size", 0),
                                "remote_modtime": entry.get("modtime", ""),
                                "local_size": 0,
                                "synced": False,
                                "synced_at": None,
                                "sha256": None,
                            }
                    self.log_syncer._save_manifest()

                    log_files = []
                    for entry in entries:
                        fn = entry["name"]
                        local_path = self.log_syncer.log_store_dir / fn
                        cached = self.log_syncer._manifest.get(fn, {})
                        log_files.append({
                            "remote_path": f"/APM/LOGS/{fn}",
                            "filename": fn,
                            "file_size": entry.get("size", 0) or cached.get("local_size", 0),
                            "synced": cached.get("synced", False) and local_path.exists(),
                            "synced_at": cached.get("synced_at"),
                            "sha256": cached.get("sha256"),
                            "remote_modtime": entry.get("modtime", ""),
                        })

                    logger.info(f"Found {len(log_files)} log file(s) via HTTP")
                    if log_files:
                        self.hub.report_fc_log_list(log_files)
                    return True, None
                else:
                    logger.info("FC webserver unreachable, trying local cache...")

            if self.log_syncer:
                cached_logs = self.log_syncer.get_cached_logs()
                if cached_logs:
                    logger.info(f"Scan from local cache: {len(cached_logs)} log file(s)")
                    self.hub.report_fc_log_list(cached_logs)
                    return True, None

            scan_path = payload.get("logPath", self.log_path)
            logger.info(f"Scanning FC logs via MAVFTP at {scan_path} (slow fallback)")
            entries = await self.ftp.list_directory(scan_path)

            log_files = []
            for entry in entries:
                name = entry["name"]
                if entry["type"] == "file" and (
                    name.upper().endswith(".BIN") or name.lower().endswith(".log")
                ):
                    log_files.append({
                        "remote_path": f"{scan_path}/{name}",
                        "filename": name,
                        "file_size": entry.get("size", 0),
                    })

            logger.info(f"Found {len(log_files)} log file(s) via MAVFTP")
            if log_files:
                self.hub.report_fc_log_list(log_files)
            return True, None

        except Exception as e:
            error_msg = f"FC log scan failed: {e}"
            logger.error(error_msg)
            return False, error_msg

    async def handle_download_fc_log(self, job: dict) -> Tuple[bool, Optional[str]]:
        """Serve a specific FC log file to the Hub (local cache > HTTP > MAVFTP)."""
        payload = job.get("payload", {})
        log_id = payload.get("logId")
        remote_path = payload.get("remotePath")

        if not log_id or not remote_path:
            return False, "Missing logId or remotePath in job payload"

        filename = remote_path.split("/")[-1]

        try:
            if self.log_syncer:
                local_path = self.log_syncer.get_local_file_path(filename)
                if local_path:
                    logger.info(f"Serving {filename} from local cache: {local_path}")
                    return await self._upload_local_file_to_hub(
                        log_id, filename, local_path)

            if self.log_syncer and requests:
                logger.info(f"Downloading {filename} via HTTP from FC webserver...")
                self.hub.report_fc_log_progress(log_id, "downloading", 5)

                fc_url = f"{self.log_syncer.fc_url}{self.log_syncer.LOGS_PATH}{filename}"
                try:
                    resp = requests.get(fc_url, stream=True, timeout=300)
                    resp.raise_for_status()

                    total_size = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    sha256 = hashlib.sha256()

                    self.log_syncer._ensure_store_dir()
                    tmp_path = self.log_syncer.log_store_dir / f".{filename}.tmp"
                    final_path = self.log_syncer.log_store_dir / filename

                    with open(tmp_path, "wb") as f:
                        for chunk in resp.iter_content(
                                chunk_size=self.log_syncer.DOWNLOAD_CHUNK_SIZE):
                            if chunk:
                                f.write(chunk)
                                sha256.update(chunk)
                                downloaded += len(chunk)
                                if total_size > 0:
                                    pct = min(int(downloaded / total_size * 70), 70)
                                    self.hub.report_fc_log_progress(
                                        log_id, "downloading", pct)

                    tmp_path.rename(final_path)
                    file_size = final_path.stat().st_size
                    file_hash = sha256.hexdigest()

                    logger.info(f"Downloaded {filename} via HTTP: {file_size} bytes, "
                                f"SHA-256: {file_hash[:16]}...")

                    self.log_syncer._manifest[filename] = {
                        "filename": filename,
                        "remote_size": total_size or file_size,
                        "remote_modtime": resp.headers.get("Last-Modified", ""),
                        "local_size": file_size,
                        "synced": True,
                        "synced_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "sha256": file_hash,
                    }
                    self.log_syncer._save_manifest()

                    return await self._upload_local_file_to_hub(
                        log_id, filename, final_path)

                except requests.exceptions.ConnectionError:
                    logger.info(f"FC webserver unreachable for {filename}, "
                                f"falling back to MAVFTP")
                except requests.exceptions.Timeout:
                    logger.info(f"FC webserver timeout for {filename}, "
                                f"falling back to MAVFTP")
                except requests.exceptions.HTTPError as e:
                    logger.warning(f"HTTP error downloading {filename}: {e}, "
                                   f"falling back to MAVFTP")
                finally:
                    try:
                        tmp_p = self.log_syncer.log_store_dir / f".{filename}.tmp"
                        if tmp_p.exists():
                            tmp_p.unlink()
                    except OSError:
                        pass

            # Fallback: MAVFTP download (slow, last resort)
            logger.info(f"Downloading {filename} via MAVFTP (slow fallback)...")
            self.hub.report_fc_log_progress(log_id, "downloading", 0)

            with tempfile.NamedTemporaryFile(suffix=f"_{filename}", delete=False) as tmp:
                tmp_path = tmp.name

            try:
                async def progress_cb(downloaded: int, total: int):
                    if total > 0:
                        pct = min(int(downloaded / total * 80), 80)
                        self.hub.report_fc_log_progress(log_id, "downloading", pct)

                await self.ftp.download_file(remote_path, tmp_path, progress_cb)

                file_size = os.path.getsize(tmp_path)
                logger.info(f"Downloaded {filename} via MAVFTP: {file_size} bytes")

                self.hub.report_fc_log_progress(log_id, "uploading", 85)

                with open(tmp_path, "rb") as f:
                    content = f.read()

                url = self.hub.upload_fc_log(log_id, filename, content, file_size)

                if url:
                    logger.info(f"Uploaded {filename} to Hub: {url}")
                    return True, None
                else:
                    error_msg = "Failed to upload log to Hub"
                    self.hub.report_fc_log_progress(log_id, "failed", 0, error_msg)
                    return False, error_msg

            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        except Exception as e:
            error_msg = f"FC log download failed: {e}"
            logger.error(error_msg)
            self.hub.report_fc_log_progress(log_id, "failed", 0, error_msg)
            return False, error_msg

    async def _upload_local_file_to_hub(
            self, log_id: int, filename: str, local_path: Path
    ) -> Tuple[bool, Optional[str]]:
        self.hub.report_fc_log_progress(log_id, "uploading", 75)

        file_size = local_path.stat().st_size
        with open(local_path, "rb") as f:
            content = f.read()

        url = self.hub.upload_fc_log(log_id, filename, content, file_size)

        if url:
            logger.info(f"Uploaded {filename} to Hub: {url}")
            return True, None
        else:
            error_msg = f"Failed to upload {filename} to Hub"
            self.hub.report_fc_log_progress(log_id, "failed", 0, error_msg)
            return False, error_msg

    async def handle_flash_firmware(self, job: dict) -> Tuple[bool, Optional[str]]:
        """Flash firmware to FC via Tier 1 (FC HTTP pull). Auto-converts .apj -> .abin."""
        payload = job.get("payload", {})
        update_id = payload.get("updateId")
        firmware_url = payload.get("firmwareUrl")
        firmware_filename = payload.get("filename", "arducopter.abin")

        if not update_id or not firmware_url:
            return False, "Missing updateId or firmwareUrl in job payload"

        is_apj = firmware_filename.lower().endswith(".apj")

        try:
            self.hub.report_firmware_progress(update_id, "transferring", 5,
                                               flash_stage="downloading")
            logger.info(f"Downloading firmware from: {firmware_url}")

            tmp_dir = tempfile.mkdtemp(prefix="caribou_fw_")
            tmp_path = os.path.join(tmp_dir, "ardupilot.abin")

            try:
                resp = await asyncio.to_thread(
                    lambda: requests.get(firmware_url, timeout=120)
                )
                resp.raise_for_status()

                if is_apj:
                    logger.info("Detected .apj firmware - converting to .abin")
                    self.hub.report_firmware_progress(
                        update_id, "transferring", 7,
                        flash_stage="converting_apj")
                    try:
                        self._convert_apj_to_abin(resp.content, tmp_path)
                    except ValueError as conv_err:
                        error_msg = f".apj conversion failed: {conv_err}"
                        logger.error(error_msg)
                        self.hub.report_firmware_progress(
                            update_id, "failed", 0,
                            flash_stage="conversion_failed",
                            error_message=error_msg)
                        return False, error_msg
                else:
                    with open(tmp_path, "wb") as f:
                        f.write(resp.content)

                file_size = os.path.getsize(tmp_path)
                logger.info(f"{'Converted' if is_apj else 'Downloaded'} firmware: {file_size} bytes")

                expected_hash = payload.get("sha256Hash")
                if expected_hash:
                    actual_hash = hashlib.sha256(resp.content).hexdigest()
                    if actual_hash != expected_hash:
                        error_msg = (
                            f"SHA-256 mismatch! Expected: {expected_hash[:16]}... "
                            f"Got: {actual_hash[:16]}... "
                            f"Firmware may be corrupted - aborting flash."
                        )
                        logger.error(error_msg)
                        self.hub.report_firmware_progress(
                            update_id, "failed", 0,
                            flash_stage="hash_verification_failed",
                            error_message=error_msg
                        )
                        return False, error_msg
                    logger.info(f"SHA-256 verified: {actual_hash[:16]}...")
                    self.hub.report_firmware_progress(
                        update_id, "transferring", 8,
                        flash_stage="hash_verified"
                    )
                else:
                    logger.warning("No SHA-256 hash in job payload - skipping integrity check")

                expected_git_hash = self._extract_abin_git_hash(tmp_path)
                if expected_git_hash:
                    logger.info(f"Expected firmware git hash: {expected_git_hash}")
                else:
                    logger.warning("Could not extract git hash from .abin - "
                                   "post-flash verification will be skipped")

                self.hub.report_firmware_progress(update_id, "transferring", 10,
                                                   flash_stage="preparing")

                http_available = await asyncio.to_thread(self._http_fc_reachable)
                if http_available:
                    logger.info("FC web server reachable - checking for old firmware files")
                    for old_name in [
                        "ardupilot.abin",
                        "ardupilot-verify.abin",
                        "ardupilot-flash.abin",
                        "ardupilot-flashed.abin",
                    ]:
                        exists = await asyncio.to_thread(self._http_file_exists, old_name)
                        if exists:
                            logger.info(f"Old file {old_name} found on FC (will be overwritten)")
                else:
                    logger.info("FC web server not reachable - skipping pre-upload check")

                # Tier 1: FC HTTP pull (firmware_puller.lua)
                self.hub.report_firmware_progress(update_id, "transferring", 20,
                                                   flash_stage="uploading")

                if not aiohttp_web:
                    error_msg = ("aiohttp not installed - cannot serve firmware to FC. "
                                 "Install with: pip install aiohttp")
                    logger.error(error_msg)
                    self.hub.report_firmware_progress(
                        update_id, "failed", 0, error_message=error_msg)
                    return False, error_msg

                logger.info("Starting firmware HTTP server for FC pull (Tier 1)...")
                self.hub.report_firmware_progress(
                    update_id, "transferring", 20,
                    flash_stage="uploading_http_pull"
                )

                server_started = await self._start_firmware_server(tmp_path)
                if not server_started:
                    error_msg = "Failed to start firmware HTTP server on port 8070"
                    logger.error(error_msg)
                    self.hub.report_firmware_progress(
                        update_id, "failed", 0, error_message=error_msg)
                    return False, error_msg

                try:
                    fc_pulled = await self._wait_for_fc_pull(update_id,
                                                              self.FIRMWARE_SERVER_ACK_TIMEOUT)
                finally:
                    await self._stop_firmware_server()

                if not fc_pulled:
                    error_msg = ("FC did not pull firmware. Check: "
                                 "1) firmware_puller.lua in APM/scripts/ "
                                 "2) FWPULL_ENABLE=1 "
                                 "3) FWPULL_PI_IP matches Pi IP "
                                 "4) UFW allows FC IP on port 8070")
                    logger.error(error_msg)
                    self.hub.report_firmware_progress(
                        update_id, "failed", 0,
                        flash_stage="fc_pull_failed",
                        error_message=error_msg)
                    return False, error_msg

                upload_method = "HTTP pull (Tier 1)"
                logger.info(f"Firmware uploaded to FC as /APM/ardupilot.abin via {upload_method}")

                # Reboot FC to trigger bootloader flash
                self.hub.report_firmware_progress(update_id, "flashing", 70,
                                                   flash_stage="rebooting")

                reboot_sent = False
                if self.ftp.connected and self.ftp.system:
                    try:
                        logger.info("Sending reboot command to FC...")
                        await self.ftp.system.action.reboot()
                        reboot_sent = True
                        logger.info("Reboot command sent - FC will flash firmware on boot")
                    except Exception as reboot_err:
                        logger.warning(f"Reboot command failed: {reboot_err}")

                if not reboot_sent:
                    logger.warning("Could not send reboot command - please reboot FC manually")
                    self.hub.report_firmware_progress(
                        update_id, "flashing", 75,
                        flash_stage="awaiting_manual_reboot"
                    )

                # Wait for FC to come back online
                logger.info("Waiting for FC to reboot and come back online...")
                self.hub.report_firmware_progress(update_id, "flashing", 80,
                                                   flash_stage="waiting_for_reboot")

                await asyncio.sleep(5)

                max_wait = 120
                poll_interval = 5
                elapsed = 0
                fc_back = False

                while elapsed < max_wait:
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval

                    try:
                        if await asyncio.to_thread(self._http_fc_reachable):
                            fc_back = True
                            logger.info(f"FC back online after {elapsed}s - firmware flash complete!")
                            break
                    except Exception:
                        pass

                    pct = 80 + min(int(elapsed / max_wait * 20), 19)
                    self.hub.report_firmware_progress(
                        update_id, "flashing", pct,
                        flash_stage="waiting_for_reboot"
                    )

                # Firmware version verification
                firmware_version_str = None
                verified = False

                if fc_back and expected_git_hash:
                    logger.info("Querying AUTOPILOT_VERSION for firmware verification...")
                    self.hub.report_firmware_progress(
                        update_id, "verifying", 95,
                        flash_stage="reading_version"
                    )

                    try:
                        await self.ftp.ensure_ready(retries=3, delay=5)
                    except Exception as reconn_err:
                        logger.warning(f"MAVSDK reconnect for version check failed: {reconn_err}")

                    version_info = await self._query_autopilot_version()
                    if version_info:
                        actual_git = version_info.get("flight_custom_version", "")
                        sw_version = version_info.get("flight_sw_version", "")
                        firmware_version_str = f"{sw_version} ({actual_git[:8]})" if actual_git else sw_version

                        min_len = min(len(expected_git_hash), len(actual_git))
                        if min_len >= 6 and expected_git_hash[:min_len] == actual_git[:min_len]:
                            verified = True
                            logger.info(f"Firmware VERIFIED: git hash {actual_git[:8]} matches .abin header")
                        elif actual_git:
                            logger.warning(
                                f"Firmware git hash MISMATCH: expected {expected_git_hash[:8]}, "
                                f"got {actual_git[:8]} - flash may have failed or old firmware is running"
                            )
                        else:
                            logger.warning("AUTOPILOT_VERSION returned no git hash")
                    else:
                        logger.warning("Could not read AUTOPILOT_VERSION - version unverified")
                elif fc_back:
                    logger.info("FC back online but no expected git hash - skipping verification")

                if fc_back:
                    flash_stage = "verified" if verified else "reboot_verified"
                    self.hub.report_firmware_progress(
                        update_id, "completed", 100,
                        flash_stage=flash_stage,
                        firmware_version=firmware_version_str
                    )
                elif not fc_back:
                    if reboot_sent:
                        logger.warning(f"FC not back after {max_wait}s - may still be flashing.")
                        self.hub.report_firmware_progress(
                            update_id, "completed", 95,
                            flash_stage="reboot_timeout_check_manually"
                        )
                    else:
                        logger.info("Awaiting manual FC reboot to flash firmware")
                        self.hub.report_firmware_progress(
                            update_id, "completed", 90,
                            flash_stage="awaiting_manual_reboot"
                        )

                return True, None

            finally:
                try:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                    if os.path.exists(tmp_dir):
                        os.rmdir(tmp_dir)
                    logger.info(f"Cleaned up firmware temp dir: {tmp_dir}")
                except OSError as cleanup_err:
                    logger.warning(f"Failed to clean up temp dir {tmp_dir}: {cleanup_err}")

        except Exception as e:
            error_msg = f"Firmware flash failed: {e}"
            logger.error(error_msg)
            self.hub.report_firmware_progress(
                update_id, "failed", 0, error_message=error_msg
            )
            return False, error_msg


# --- System Diagnostics Collector ------------------------------------------

class DiagnosticsCollector:
    """Collects system health metrics from the Raspberry Pi.

    The set of systemd units monitored here is resolved at construction
    time. The Caribou Hub's Remote Logs tab populates its service-selector
    dropdown from whatever keys arrive in `services` — so adding a unit
    here makes it selectable in the UI on the next 10 s diagnostics tick.
    Two env vars let operators customise per fleet:

      MONITORED_SERVICES="csu.service,my-custom.service"
          Override the full list (replaces the defaults).
      MONITORED_SERVICES_EXTRA="winch.service,cargo.service"
          Add to the defaults — useful for per-payload extras.
    """

    # Defaults reflect the units shipped by this repo: csu.service and
    # caribou-can.service from bootstrap_drone.sh, logs-ota.service from
    # install_logs_ota.sh, and the optional camera plane (camera-stream,
    # go2rtc, tailscale-funnel). tailscaled is the overlay network itself.
    DEFAULT_MONITORED_SERVICES = [
        "csu.service",
        "caribou-can.service",
        "logs-ota.service",
        "camera-stream.service",
        "go2rtc.service",
        "tailscale-funnel.service",
        "tailscaled.service",
    ]

    def __init__(self, fc_webserver_url: str = None,
                 monitored_services: list = None):
        self.fc_webserver_url = fc_webserver_url

        env_full = os.environ.get("MONITORED_SERVICES", "").strip()
        env_extra = os.environ.get("MONITORED_SERVICES_EXTRA", "").strip()
        if monitored_services is not None:
            self.monitored_services = list(monitored_services)
        elif env_full:
            self.monitored_services = [s.strip() for s in env_full.split(",") if s.strip()]
        else:
            extras = [s.strip() for s in env_extra.split(",") if s.strip()]
            # Dedupe while preserving order.
            seen = set()
            self.monitored_services = []
            for svc in list(self.DEFAULT_MONITORED_SERVICES) + extras:
                if svc not in seen:
                    seen.add(svc)
                    self.monitored_services.append(svc)
        logger.info(f"DiagnosticsCollector monitoring {len(self.monitored_services)} services: "
                    f"{', '.join(s.replace('.service', '') for s in self.monitored_services)}")

    def collect(self) -> dict:
        diag: dict = {}

        if psutil:
            try:
                diag["cpu_percent"] = int(psutil.cpu_percent(interval=1))
            except Exception:
                pass

            try:
                mem = psutil.virtual_memory()
                diag["memory_percent"] = int(mem.percent)
            except Exception:
                pass

            try:
                disk = psutil.disk_usage("/")
                diag["disk_percent"] = int(disk.percent)
            except Exception:
                pass

            try:
                temps = psutil.sensors_temperatures()
                if "cpu_thermal" in temps:
                    diag["cpu_temp_c"] = int(temps["cpu_thermal"][0].current)
                elif "coretemp" in temps:
                    diag["cpu_temp_c"] = int(temps["coretemp"][0].current)
            except Exception:
                pass

            try:
                diag["uptime_seconds"] = int(time.time() - psutil.boot_time())
            except Exception:
                pass

            try:
                net = psutil.net_io_counters(pernic=True)
                addrs = psutil.net_if_addrs()
                network = {}
                for iface, counters in net.items():
                    if iface == "lo":
                        continue
                    ip = ""
                    if iface in addrs:
                        for addr in addrs[iface]:
                            if addr.family.name == "AF_INET":
                                ip = addr.address
                                break
                    network[iface] = {
                        "ip": ip,
                        "rx_bytes": counters.bytes_recv,
                        "tx_bytes": counters.bytes_sent,
                    }
                diag["network"] = network
            except Exception:
                pass
        else:
            try:
                load = os.getloadavg()
                cpu_count = os.cpu_count() or 1
                diag["cpu_percent"] = min(int(load[0] / cpu_count * 100), 100)
            except Exception:
                pass

            try:
                with open("/sys/class/thermal/thermal_zone0/temp") as f:
                    diag["cpu_temp_c"] = int(f.read().strip()) // 1000
            except Exception:
                pass

        services = {}
        for svc in self.monitored_services:
            try:
                result = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=5
                )
                status = result.stdout.strip()
                services[svc.replace(".service", "")] = status
            except Exception:
                services[svc.replace(".service", "")] = "unknown"
        diag["services"] = services

        if self.fc_webserver_url and requests:
            fc_ws: dict = {
                "url": self.fc_webserver_url,
                "reachable": False,
                "latency_ms": None,
                "last_checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            try:
                start = time.monotonic()
                resp = requests.head(
                    self.fc_webserver_url,
                    timeout=3,
                    allow_redirects=False,
                )
                elapsed_ms = int((time.monotonic() - start) * 1000)
                fc_ws["reachable"] = resp.status_code < 500
                fc_ws["latency_ms"] = elapsed_ms
                logger.debug(f"FC web server reachable: {resp.status_code} in {elapsed_ms}ms")
            except requests.exceptions.RequestException as e:
                logger.debug(f"FC web server unreachable: {e}")
            diag["fc_webserver"] = fc_ws

        return diag


# --- Remote Log Streamer ---------------------------------------------------

class RemoteLogStreamer:
    """Streams journalctl output to the Hub via Socket.IO."""

    def __init__(self, sio_client, drone_id: str):
        self.sio = sio_client
        self.drone_id = drone_id
        self._active_streams: Dict[str, asyncio.subprocess.Process] = {}

    async def start_stream(self, service: str, lines: int = 50):
        if service in self._active_streams:
            logger.info(f"Stream already active for {service}")
            return

        logger.info(f"Starting log stream for {service} (last {lines} lines)")

        try:
            proc = await asyncio.create_subprocess_exec(
                "journalctl", "-f", "-u", service, "-n", str(lines),
                "--no-pager", "-o", "short-iso",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._active_streams[service] = proc

            asyncio.create_task(self._read_stream(service, proc))

        except Exception as e:
            logger.error(f"Failed to start log stream for {service}: {e}")

    async def stop_stream(self, service: str):
        proc = self._active_streams.pop(service, None)
        if proc:
            logger.info(f"Stopping log stream for {service}")
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
            except Exception:
                pass

    async def stop_all(self):
        for service in list(self._active_streams.keys()):
            await self.stop_stream(service)

    async def _read_stream(self, service: str, proc: asyncio.subprocess.Process):
        try:
            buffer: List[str] = []
            flush_interval = 0.5

            async def flush():
                nonlocal buffer
                if buffer and self.sio and self.sio.connected:
                    await self.sio.emit("log_stream_line", {
                        "drone_id": self.drone_id,
                        "service": service,
                        "lines": buffer,
                    })
                    buffer = []

            last_flush = time.time()

            while service in self._active_streams:
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=flush_interval
                    )
                    if not line:
                        break
                    buffer.append(line.decode("utf-8", errors="replace").rstrip())

                    now = time.time()
                    if len(buffer) >= 20 or (now - last_flush) >= flush_interval:
                        await flush()
                        last_flush = now

                except asyncio.TimeoutError:
                    await flush()
                    last_flush = time.time()

            await flush()

        except Exception as e:
            logger.error(f"Log stream error for {service}: {e}")
        finally:
            self._active_streams.pop(service, None)


# --- Main Service ----------------------------------------------------------

class LogsOtaService:
    """Main service orchestrator."""

    def __init__(self, hub_url: str, drone_id: str, api_key: str,
                 fc_connection: str, poll_interval: int = 5,
                 diagnostics_interval: int = 10,
                 fc_webserver_url: str = None,
                 log_store_dir: str = None):
        self.hub = HubClient(hub_url, drone_id, api_key)
        self.ftp = MavFtpClient(fc_connection)
        self.log_syncer = FCLogSyncer(
            fc_webserver_url=fc_webserver_url,
            log_store_dir=log_store_dir,
        )
        self.job_handler = LogsOtaJobHandler(self.hub, self.ftp,
                                              log_syncer=self.log_syncer)
        self.diagnostics = DiagnosticsCollector(
            fc_webserver_url=self.log_syncer.fc_url
        )
        self.drone_id = drone_id
        self.hub_url = hub_url
        self.api_key = api_key
        self.poll_interval = poll_interval
        self.diagnostics_interval = diagnostics_interval
        self.running = False
        self.sio = None
        self.log_streamer = None
        self._fc_connected = False

    async def connect_fc(self) -> bool:
        try:
            success = await self.ftp.connect()
            self._fc_connected = success
            return success
        except Exception as e:
            logger.error(f"FC connection failed: {e}")
            self._fc_connected = False
            return False

    async def _setup_socketio(self):
        if socketio is None:
            logger.warning("python-socketio not installed, skipping real-time features")
            return

        self.sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=0,
            reconnection_delay=5,
            logger=False,
            engineio_logger=False,
        )

        self.log_streamer = RemoteLogStreamer(self.sio, self.drone_id)

        @self.sio.event
        async def connect():
            logger.info("Connected to Hub via Socket.IO")
            await self.sio.emit("register_companion", {
                "droneId": self.drone_id,
                "type": "logs_ota",
            })

        @self.sio.event
        async def disconnect():
            logger.warning("Disconnected from Hub Socket.IO")

        @self.sio.on("log_stream_request")
        async def on_log_stream_request(data):
            service = data.get("service", "")
            action = data.get("action", "")
            lines = data.get("lines", 50)

            logger.info(f"Log stream request: {action} {service}")

            if action == "start":
                await self.log_streamer.start_stream(service, lines)
            elif action == "stop":
                await self.log_streamer.stop_stream(service)

        hub_http = self.hub_url.rstrip("/")
        try:
            await self.sio.connect(hub_http, transports=["websocket"])
        except Exception as e:
            logger.error(f"Socket.IO connection failed: {e}")

    async def _job_poll_loop(self):
        while self.running:
            try:
                jobs = self.hub.get_pending_jobs()

                for job in jobs:
                    job_id = job.get("id")
                    job_type = job.get("type", "")

                    if job_type not in ("scan_fc_logs", "download_fc_log", "flash_firmware"):
                        continue

                    logger.info(f"Processing job {job_id}: {job_type}")

                    companion_id = f"logs_ota@{platform.node()}"
                    if not self.hub.acknowledge_job(job_id, locked_by=companion_id):
                        logger.warning(f"Job {job_id} already locked by another companion, skipping")
                        continue

                    if not self._fc_connected:
                        logger.warning("FC not connected, attempting reconnection...")
                        if not await self.connect_fc():
                            self.hub.complete_job(job_id, False, "FC not connected")
                            continue

                    success = False
                    error_msg = None

                    try:
                        if job_type == "scan_fc_logs":
                            success, error_msg = await self.job_handler.handle_scan_fc_logs(job)
                        elif job_type == "download_fc_log":
                            success, error_msg = await self.job_handler.handle_download_fc_log(job)
                        elif job_type == "flash_firmware":
                            success, error_msg = await self.job_handler.handle_flash_firmware(job)
                    except Exception as e:
                        error_msg = f"Job execution error: {e}"
                        logger.error(error_msg)

                    self.hub.complete_job(job_id, success, error_msg)

            except Exception as e:
                logger.error(f"Job poll error: {e}")

            await asyncio.sleep(self.poll_interval)

    async def _diagnostics_loop(self):
        while self.running:
            try:
                diag = self.diagnostics.collect()
                self.hub.report_diagnostics(diag)
                logger.debug(f"Diagnostics reported: CPU={diag.get('cpu_percent')}% "
                             f"MEM={diag.get('memory_percent')}% "
                             f"TEMP={diag.get('cpu_temp_c')}C")
            except Exception as e:
                logger.error(f"Diagnostics error: {e}")

            await asyncio.sleep(self.diagnostics_interval)

    async def run(self):
        self.running = True
        logger.info("=" * 60)
        logger.info("Caribou Hub - Logs & OTA Service starting")
        logger.info(f"  Drone ID:    {self.drone_id}")
        logger.info(f"  Hub URL:     {self.hub_url}")
        logger.info(f"  FC:          {self.ftp.connection_string}")
        logger.info(f"  FC Web:      {self.log_syncer.fc_url}")
        logger.info(f"  Log Store:   {self.log_syncer.log_store_dir}")
        logger.info(f"  Poll:        {self.poll_interval}s")
        logger.info(f"  Diagnostics: {self.diagnostics_interval}s")
        logger.info("=" * 60)

        asyncio.create_task(self._initial_fc_connect())

        if self.ftp.system:
            self.log_syncer.mavsdk_system = self.ftp.system

        await self._setup_socketio()

        tasks = [
            asyncio.create_task(self._job_poll_loop()),
            asyncio.create_task(self._diagnostics_loop()),
            asyncio.create_task(
                self.log_syncer.run_sync_loop(lambda: self.running)
            ),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            if self.log_streamer:
                await self.log_streamer.stop_all()
            if self.sio and self.sio.connected:
                await self.sio.disconnect()

    async def _initial_fc_connect(self):
        retry_delay = 5
        max_retries = 12
        for attempt in range(max_retries):
            if not self.running:
                return
            logger.info(f"FC connection attempt {attempt + 1}/{max_retries}...")
            if await self.connect_fc():
                if self.ftp.system:
                    self.log_syncer.mavsdk_system = self.ftp.system
                    logger.info("MAVSDK system passed to FCLogSyncer for arm-state guard")
                return
            await asyncio.sleep(retry_delay)
        logger.warning("Could not connect to FC after retries. "
                        "Jobs requiring MAVFTP will fail until FC is available.")

    def stop(self):
        logger.info("Shutdown requested")
        self.running = False


# --- CLI Entry Point -------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Caribou Hub - Logs & OTA Service (Companion Computer)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # UDP listener on a port the Pixhawk is configured to push to (NET_P2):
  python3 logs_ota_service.py \\
      --hub-url https://your-hub.example.com \\
      --drone-id caribou_001 \\
      --api-key YOUR_KEY \\
      --fc-connection udpin://0.0.0.0:14550

  # Serial connection (TELEM port at 921600 baud):
  python3 logs_ota_service.py \\
      --hub-url https://your-hub.example.com \\
      --drone-id caribou_001 \\
      --api-key YOUR_KEY \\
      --fc-connection serial:///dev/ttyAMA1:921600

  # Diagnostics only (no FC connection):
  python3 logs_ota_service.py \\
      --hub-url https://your-hub.example.com \\
      --drone-id caribou_001 \\
      --api-key YOUR_KEY \\
      --no-fc
        """,
    )

    parser.add_argument("--hub-url", required=True,
                        help="Caribou Hub URL")
    parser.add_argument("--drone-id", required=True,
                        help="Drone identifier (e.g., caribou_001)")
    parser.add_argument("--api-key", required=True,
                        help="API key for Hub authentication")
    parser.add_argument("--fc-connection", default="udpin://0.0.0.0:14550",
                        help="MAVSDK connection string (default: udpin://0.0.0.0:14550 — "
                             "distinct from CSU's :14540 so both services can run on the same FC)")
    parser.add_argument("--poll-interval", type=int, default=5,
                        help="Job polling interval in seconds (default: 5)")
    parser.add_argument("--diagnostics-interval", type=int, default=10,
                        help="Diagnostics reporting interval in seconds (default: 10)")
    parser.add_argument("--no-fc", action="store_true",
                        help="Run without FC connection (diagnostics + log streaming only)")
    parser.add_argument("--fc-webserver-url",
                        default="http://192.168.144.10:8080",
                        help="ArduPilot net_webserver URL for HTTP log download "
                             "(default: http://192.168.144.10:8080)")
    parser.add_argument("--log-store-dir",
                        default="/var/lib/caribou/fc_logs",
                        help="Local directory to cache FC log files "
                             "(default: /var/lib/caribou/fc_logs)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")

    parser.add_argument("--allow-non-root", action="store_true",
                        help="Allow running as non-root (some features may not work)")

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("mavsdk").setLevel(logging.DEBUG)

    if os.geteuid() != 0 and not args.allow_non_root:
        logger.warning(
            "Running as non-root user. Some features may not work:\n"
            "  - journalctl log streaming (requires root or systemd-journal group)\n"
            "  - systemctl service status (requires root or polkit)\n"
            "  - Serial port access (requires dialout group)\n"
            "Use --allow-non-root to suppress this warning, "
            "or run with: sudo python3 logs_ota_service.py ..."
        )

    if not requests:
        logger.error("requests library not installed. Run: pip install requests")
        sys.exit(1)

    service = LogsOtaService(
        hub_url=args.hub_url,
        drone_id=args.drone_id,
        api_key=args.api_key,
        fc_connection=args.fc_connection if not args.no_fc else "",
        poll_interval=args.poll_interval,
        diagnostics_interval=args.diagnostics_interval,
        fc_webserver_url=args.fc_webserver_url,
        log_store_dir=args.log_store_dir,
    )

    def signal_handler(sig, frame):
        service.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if args.no_fc:
        service._fc_connected = False
        logger.info("Running without FC connection (--no-fc)")

    try:
        asyncio.run(service.run())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        service.stop()


if __name__ == "__main__":
    main()
