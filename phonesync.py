#!/usr/bin/env python3
"""
phonesync — Sync photos, downloads, and recordings from Android phones via ADB.

Supports two phones merging into one photo library, with:
  - One-way ingest from phone to computer (with mtime-based change detection)
  - Computer-side move/sort tracking with propagation back to phone
  - Phone-side move detection (updates tracking, no re-download)
  - Date-based photo organization (EXIF > filename > phone mtime)
  - Collision-safe file naming and phone moves (hash-verified)
  - Safe delete behavior (phone deletes don't remove from computer;
    computer deletes are reported but don't remove from phone)
  - Atomic state saves and file-based locking

TODO: USB mass-storage device support (non-ADB)

Usage:
  phonesync devices
  phonesync config --init [--config-dir DIR] [--data-dir DIR]
  phonesync sync [--device SERIAL] [--dry-run]
  phonesync status
  phonesync detect-paths [--device SERIAL]
  phonesync prune-state [--device SERIAL] [--clear-tombstones] [--rehash]
  phonesync reset-state [--device SERIAL]
"""

import argparse
import fcntl
import fnmatch
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants & Defaults
# ---------------------------------------------------------------------------

APP_NAME = "phonesync"

# Config lives in ~/.phonesync/
DEFAULT_CONFIG_DIR = Path.home() / ".phonesync"
# Data lives in ~/PhoneSync/
DEFAULT_DATA_DIR = Path.home() / "PhoneSync"

# Pointer file: if config dir has been moved, we leave a breadcrumb
# at the default location so we can still find it.
CONFIG_POINTER_FILE = DEFAULT_CONFIG_DIR / "location"

HASH_CHUNK_SIZE = 65536  # 64KB chunks for faster hashing

# Default phone source directories (internal storage paths)
DEFAULT_PHONE_SOURCES = {
    "photos": [
        "/sdcard/DCIM/Camera",
        "/sdcard/DCIM/Screenshots",
        "/sdcard/Pictures",
        "/sdcard/Movies",
    ],
    "downloads": [
        "/sdcard/Download",
    ],
    "recordings": [
        "/sdcard/Recordings",
        "/sdcard/DCIM/Recorder",
    ],
}

# Directories to always exclude when scanning recursively
DEFAULT_EXCLUDE_DIRS = [
    ".thumbnails",
    ".trash",
    ".Trash",
    "thumbnails",
    ".cache",
    ".nomedia_thumbnails",
]

# File patterns to exclude
DEFAULT_EXCLUDE_FILES = [
    ".nomedia",
    ".DS_Store",
    "Thumbs.db",
    ".pending-*",
]

# File extensions we care about per category
PHOTO_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".mp4", ".mov", ".avi", ".mkv", ".3gp",
    ".dng", ".raw", ".cr2", ".nef",
}
DOWNLOAD_EXTENSIONS = None  # accept everything
RECORDING_EXTENSIONS = {
    ".m4a", ".mp3", ".wav", ".ogg", ".aac", ".3gp", ".amr", ".flac",
}

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def default_config(config_dir: str = None, data_dir: str = None):
    return {
        "config_dir": str(config_dir or DEFAULT_CONFIG_DIR),
        "data_dir": str(data_dir or DEFAULT_DATA_DIR),
        "devices": {},
        "photo_date_folders": True,
        "keep_duplicates": True,  # Keep files with same hash but different names
        "recursive_scan": True,   # Scan subdirectories on phone
        "preserve_phone_subdirs": True,  # Maintain subdirectory structure from phone
        "exclude_dirs": DEFAULT_EXCLUDE_DIRS,
        "exclude_files": DEFAULT_EXCLUDE_FILES,
        "followlinks": "Hardcoded_True",
        "delete_from_phone_after_sync": False,
        "propagate_computer_deletes_to_phone": False,
        "conflict_resolution": "prefer_computer",
    }


def find_config_dir() -> Path:
    """Find the config directory, checking for a relocation pointer."""
    pointer = CONFIG_POINTER_FILE
    if pointer.exists():
        target = Path(pointer.read_text().strip())
        if target.exists() and (target / "config.json").exists():
            return target
    if (DEFAULT_CONFIG_DIR / "config.json").exists():
        return DEFAULT_CONFIG_DIR
    return DEFAULT_CONFIG_DIR


def load_config() -> dict:
    """Load config from the config directory."""
    cfg_dir = find_config_dir()
    cfg_path = cfg_dir / "config.json"
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = json.load(f)
        # Ensure new config keys exist (backwards compat)
        defaults = default_config()
        for key, val in defaults.items():
            if key not in cfg:
                cfg[key] = val
        return cfg
    return default_config()


def save_config(cfg: dict):
    cfg_dir = Path(cfg["config_dir"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "config.json"
    _atomic_json_write(cfg_path, cfg)

    # If config dir is non-default, leave a pointer at the default location
    if cfg_dir != DEFAULT_CONFIG_DIR:
        DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_POINTER_FILE.write_text(str(cfg_dir))

    logging.info(f"Config saved to {cfg_path}")


def _atomic_json_write(path: Path, data: dict):
    """Write JSON atomically: write to temp file, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class SyncLock:
    """File-based lock to prevent concurrent phonesync runs.

    Uses fcntl advisory locking. The lock file is never deleted — it's
    a stable inode that all processes open and lock against. Closing
    the fd (including on crash) automatically releases the lock.
    """

    def __init__(self, cfg_dir: Path):
        self.lock_path = cfg_dir / "sync.lock"
        self._fd = None

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Open without truncating — we want a stable inode
            self._fd = open(self.lock_path, "a+")
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Truncate and write our PID (informational only)
            self._fd.seek(0)
            self._fd.truncate()
            self._fd.write(f"{os.getpid()}\n{datetime.now().isoformat()}\n")
            self._fd.flush()
            return True
        except (IOError, OSError):
            if self._fd:
                self._fd.close()
                self._fd = None
            return False

    def release(self):
        if self._fd:
            try:
                self._fd.close()  # closing the fd releases the fcntl lock
            except (IOError, OSError):
                pass
            self._fd = None


def get_data_dir(cfg: dict) -> Path:
    return Path(cfg["data_dir"])


# ---------------------------------------------------------------------------
# State Management
# ---------------------------------------------------------------------------

class DeviceState:
    """
    Tracks the state of files synced from a specific device.

    State structure:
    {
        "device_serial": "...",
        "device_name": "...",
        "last_sync": "ISO timestamp",
        "files": {
            "<computer_relpath>": {
                "phone_path": "/sdcard/DCIM/Camera/IMG_001.jpg",
                "hash": "sha256:abcdef...",
                "size": 12345,
                "phone_mtime": "ISO timestamp",
                "synced_at": "ISO timestamp",
                "category": "photos",
                "device_name": "pixel-8",
            },
            ...
        }
    }

    Note: Multiple state entries CAN share the same hash (duplicate files
    from different paths or devices are kept separately).
    """

    def __init__(self, device_serial: str, device_name: str, cfg: dict):
        self.device_serial = device_serial
        self.device_name = device_name
        self.cfg_dir = Path(cfg["config_dir"])
        self.state_path = self.cfg_dir / f"state-{device_name}.json"
        self.files = {}
        self.last_sync = None
        self._load()

    def _load(self):
        if self.state_path.exists():
            with open(self.state_path) as f:
                data = json.load(f)
            self.files = data.get("files", {})
            self.last_sync = data.get("last_sync")
        # Else error and crash quickly? crash safely?

    def save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "device_serial": self.device_serial,
            "device_name": self.device_name,
            "last_sync": datetime.now().isoformat(),
            "files": self.files,
        }
        _atomic_json_write(self.state_path, data)

    def add_file(self, computer_relpath: str, phone_path: str,
                 file_hash: str, size: int, phone_mtime: str, category: str,
                 phone_source_dir: str = ""):
        self.files[computer_relpath] = {
            "phone_path": phone_path,
            "phone_source_dir": phone_source_dir,
            "hash": file_hash,
            "size": size,
            "phone_mtime": phone_mtime,
            "synced_at": datetime.now().isoformat(),
            "category": category,
            "device_name": self.device_name,
        }

    def find_by_hash_and_device(self, file_hash: str) -> list[str]:
        """Find all computer relpaths matching a hash for THIS device."""
        results = []
        for relpath, info in self.files.items():
            if info["hash"] == file_hash and info.get("device_name") == self.device_name:
                results.append(relpath)
        return results

    def find_by_phone_path(self, phone_path: str) -> Optional[str]:
        """Find a computer relpath by original phone path."""
        for relpath, info in self.files.items():
            if info["phone_path"] == phone_path:
                return relpath
        return None

    def find_by_hash_and_phone_path(self, file_hash: str, phone_path: str) -> Optional[str]:
        """Find by both hash and phone path (most precise match)."""
        for relpath, info in self.files.items():
            if info["hash"] == file_hash and info["phone_path"] == phone_path:
                return relpath
        return None


# ---------------------------------------------------------------------------
# ADB Helpers
# ---------------------------------------------------------------------------

class ADBError(Exception):
    pass


class ADB:
    def __init__(self, serial: str):
        self.serial = serial

    @staticmethod
    def _q(path: str) -> str:
        """Quote a path for use in adb shell commands."""
        return shlex.quote(path)

    def _run(self, args: list[str], check=True, capture=True,
             timeout=120) -> subprocess.CompletedProcess:
        cmd = ["adb", "-s", self.serial] + args
        logging.debug(f"Running: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd, capture_output=capture, text=True, timeout=timeout,
            )
            if check and result.returncode != 0:
                raise ADBError(
                    f"ADB command failed: {' '.join(cmd)}\n{result.stderr}")
            return result
        except subprocess.TimeoutExpired:
            raise ADBError(f"ADB command timed out: {' '.join(cmd)}")

    def shell(self, cmd: str, check=True, timeout=120) -> str:
        result = self._run(["shell", cmd], check=check, timeout=timeout)
        return result.stdout

    def list_files_recursive(self, remote_dir: str,
                             exclude_dirs: list[str] = None,
                             exclude_files: list[str] = None,
                             max_depth: int = 255) -> list[dict]:
        """
        Recursively list files in a directory on the phone.
        Returns list of {name, size, mtime_epoch, path, relpath}.
        relpath is relative to remote_dir.
        """
        if exclude_dirs is None:
            exclude_dirs = DEFAULT_EXCLUDE_DIRS
        if exclude_files is None:
            exclude_files = DEFAULT_EXCLUDE_FILES

        q = self._q
        # Check if directory exists
        check = self.shell(
            f'[ -d {q(remote_dir)} ] && echo EXISTS || echo MISSING',
            check=False)
        if "MISSING" in check:
            return []

        # Build find command with exclusions
        prune_clauses = []
        for ed in exclude_dirs:
            prune_clauses.append(f'-name {q(ed)} -prune')

        if prune_clauses:
            prune_expr = " -o ".join(prune_clauses)
            find_cmd = (
                f'find {q(remote_dir)} -maxdepth {max_depth} '
                f'\\( {prune_expr} \\) -o '
                f'-type f -exec stat -c "%s|%Y|%n" {{}} \\;'
            )
        else:
            find_cmd = (
                f'find {q(remote_dir)} -maxdepth {max_depth} '
                f'-type f -exec stat -c "%s|%Y|%n" {{}} \\;'
            )

        output = self.shell(find_cmd, check=False, timeout=300)
        files = []

        for line in output.strip().split("\n"):
            line = line.strip()
            if not line or "|" not in line:
                continue
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            try:
                size = int(parts[0])
                mtime = int(parts[1])
                filepath = parts[2]
                name = os.path.basename(filepath)

                # Check file exclusion patterns
                skip = False
                for pattern in exclude_files:
                    if fnmatch.fnmatch(name, pattern):
                        skip = True
                        break
                if skip:
                    continue

                # Compute relative path from the source directory
                if filepath.startswith(remote_dir):
                    relpath = filepath[len(remote_dir):].lstrip("/")
                else:
                    relpath = name

                files.append({
                    "name": name,
                    "size": size,
                    "mtime_epoch": mtime,
                    "path": filepath,
                    "relpath": relpath,
                })
            except (ValueError, IndexError):
                continue

        return files

    def list_files(self, remote_dir: str) -> list[dict]:
        """Non-recursive listing (backward compat wrapper)."""
        return self.list_files_recursive(remote_dir, max_depth=1)

    def pull(self, remote_path: str, local_path: str) -> bool:
        try:
            self._run(["pull", remote_path, local_path], timeout=600)
            return True
        except ADBError as e:
            logging.error(f"Failed to pull {remote_path}: {e}")
            return False

    def push(self, local_path: str, remote_path: str) -> bool:
        try:
            self._run(["push", local_path, remote_path], timeout=600)
            return True
        except ADBError as e:
            logging.error(f"Failed to push {remote_path}: {e}")
            return False

    def delete(self, remote_path: str) -> bool:
        try:
            self.shell(f'rm {self._q(remote_path)}')
            return True
        except ADBError as e:
            logging.error(f"Failed to delete {remote_path}: {e}")
            return False

    def mkdir(self, remote_path: str) -> bool:
        try:
            self.shell(f'mkdir -p {self._q(remote_path)}')
            return True
        except ADBError as e:
            logging.error(f"Failed to mkdir {remote_path}: {e}")
            return False

    def move(self, remote_src: str, remote_dst: str) -> bool:
        """Move a file on the phone. Does NOT check for collisions."""
        try:
            parent = os.path.dirname(remote_dst)
            self.shell(f'mkdir -p {self._q(parent)}')
            self.shell(f'mv {self._q(remote_src)} {self._q(remote_dst)}')
            return True
        except ADBError as e:
            logging.error(f"Failed to move {remote_src} -> {remote_dst}: {e}")
            return False

    def move_safe(self, remote_src: str, remote_dst: str,
                  expected_hash: str = None) -> dict:
        """Move a file on the phone with collision check and hash verification.

        Uses copy-verify-delete instead of mv:
          1. Check destination doesn't already exist (or has correct content)
          2. cp source to destination
          3. Verify hash at destination
          4. rm source

        Returns dict with:
          ok: bool - True if dest has correct content after this call
          action: str - "moved", "already_there", "collision", "copy_failed",
                        "hash_mismatch"
          source_deleted: bool - whether the source was removed
        """
        q = self._q
        result = {"ok": False, "action": "", "source_deleted": False}

        # Check if destination already exists
        check = self.shell(
            f'[ -e {q(remote_dst)} ] && echo EXISTS || echo FREE',
            check=False)
        if "EXISTS" in check:
            if expected_hash:
                existing_hash = self.file_hash(remote_dst)
                if existing_hash == expected_hash:
                    logging.info(
                        f"  Destination already has correct content: "
                        f"{remote_dst}")
                    # Delete source — we're mirroring a move, the old
                    # location should go away
                    try:
                        self.shell(f'rm {q(remote_src)}')
                        result["source_deleted"] = True
                    except ADBError as e:
                        logging.warning(
                            f"  Destination correct but source removal "
                            f"failed: {e}")
                    result["ok"] = True
                    result["action"] = "already_there"
                    return result
            logging.error(
                f"  Cannot move {remote_src} -> {remote_dst}: "
                f"destination exists with different content")
            result["action"] = "collision"
            return result

        # Copy instead of move
        parent = os.path.dirname(remote_dst)
        try:
            self.shell(f'mkdir -p {q(parent)}')
            self.shell(f'cp {q(remote_src)} {q(remote_dst)}')
        except ADBError as e:
            logging.error(f"  Failed to copy {remote_src} -> {remote_dst}: {e}")
            self.shell(f'rm -f {q(remote_dst)}', check=False)
            result["action"] = "copy_failed"
            return result

        # Verify hash at destination
        if expected_hash:
            actual_hash = self.file_hash(remote_dst)
            if actual_hash != expected_hash:
                logging.error(
                    f"  Hash mismatch after copy! "
                    f"Expected {expected_hash[:12]}..., "
                    f"got {actual_hash[:12] if actual_hash else 'None'}... "
                    f"Removing bad copy, source untouched.")
                self.shell(f'rm -f {q(remote_dst)}', check=False)
                result["action"] = "hash_mismatch"
                return result

        # Copy verified — now safe to remove source
        try:
            self.shell(f'rm {q(remote_src)}')
            result["source_deleted"] = True
        except ADBError as e:
            logging.warning(
                f"  Copy verified but source removal failed: {e}")

        result["ok"] = True
        result["action"] = "moved"
        return result

    def file_exists(self, remote_path: str) -> bool:
        """Check if a file exists on the phone."""
        check = self.shell(
            f'[ -e {self._q(remote_path)} ] && echo EXISTS || echo MISSING',
            check=False)
        return "EXISTS" in check

    def file_mtime(self, remote_path: str) -> Optional[int]:
        """Get the mtime (epoch seconds) of a file on the phone."""
        try:
            output = self.shell(
                f'stat -c "%Y" {self._q(remote_path)}', check=False)
            return int(output.strip())
        except (ADBError, ValueError):
            return None

    def file_hash(self, remote_path: str) -> Optional[str]:
        try:
            output = self.shell(
                f'sha256sum {self._q(remote_path)}', check=False)
            if output and " " in output:
                return output.strip().split()[0]
        except ADBError:
            pass
        return None

    def get_model(self) -> str:
        try:
            return self.shell("getprop ro.product.model", check=False).strip()
        except ADBError:
            return "unknown"

    def list_storage_volumes(self) -> list[dict]:
        """List all storage volumes (internal + external SD cards)."""
        volumes = []

        # Internal is always /sdcard -> /storage/emulated/0
        volumes.append({
            "type": "internal",
            "path": "/sdcard",
            "label": "Internal Storage",
        })

        # List /storage/ for external volumes
        try:
            output = self.shell("ls -1 /storage/", check=False)
            for entry in output.strip().split("\n"):
                entry = entry.strip()
                if not entry or entry == "emulated" or entry == "self":
                    continue
                vol_path = f"/storage/{entry}"
                check = self.shell(
                    f'[ -d {self._q(vol_path)} ] && echo EXISTS '
                    f'|| echo MISSING',
                    check=False)
                if "EXISTS" in check:
                    volumes.append({
                        "type": "external_sd",
                        "path": vol_path,
                        "label": f"SD Card ({entry})",
                        "id": entry,
                    })
        except ADBError:
            pass

        return volumes


def list_connected_devices() -> list[dict]:
    try:
        result = subprocess.run(
            ["adb", "devices", "-l"],
            capture_output=True, text=True, timeout=10
        )
    except FileNotFoundError:
        logging.error("adb not found. Install with: sudo apt install adb")
        return []
    except subprocess.TimeoutExpired:
        logging.error("adb devices timed out")
        return []

    devices = []
    for line in result.stdout.strip().split("\n")[1:]:
        line = line.strip()
        if not line or "offline" in line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serial = parts[0]
            model = "unknown"
            for part in parts[2:]:
                if part.startswith("model:"):
                    model = part.split(":", 1)[1]
                    break
            devices.append({"serial": serial, "model": model})
    return devices


# ---------------------------------------------------------------------------
# File Helpers
# ---------------------------------------------------------------------------

def file_sha256(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def get_photo_date(filepath: str,
                   phone_mtime_epoch: int = None) -> Optional[datetime]:
    """Extract date from EXIF, filename patterns, or phone mtime (in that order)."""
    # Try EXIF first
    try:
        from PIL import Image
        from PIL.ExifTags import Base as ExifBase
        img = Image.open(filepath)
        exif = img._getexif()
        if exif:
            for tag_id in (36867, 36868, 306):
                if tag_id in exif:
                    date_str = exif[tag_id]
                    return datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass

    # Try filename patterns
    basename = os.path.basename(filepath)
    patterns = [
        r'(\d{4})(\d{2})(\d{2})[_-]',
        r'(\d{4})-(\d{2})-(\d{2})',
        r'IMG[_-](\d{4})(\d{2})(\d{2})',
        r'VID[_-](\d{4})(\d{2})(\d{2})',
        r'PXL[_-](\d{4})(\d{2})(\d{2})',
    ]
    for pattern in patterns:
        m = re.search(pattern, basename)
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return datetime(y, mo, d)
            except (ValueError, IndexError):
                continue

    # Try unix epoch timestamps in filename (common in Kakao exports, etc.)
    # Seconds: 9-10 digits (2000-01-01 = 946684800, 2286 = 9999999999)
    # Milliseconds: 12-13 digits (2000-01-01 = 946684800000)
    # Use word boundaries to avoid grabbing substrings of longer numbers
    stem = Path(basename).stem
    for m in re.finditer(r'(?<!\d)(\d{12,13})(?!\d)', stem):
        try:
            epoch = int(m.group(1)) / 1000  # milliseconds
            if 946684800 <= epoch <= 4102444800:
                return datetime.fromtimestamp(epoch)
        except (ValueError, OverflowError, OSError):
            continue
    for m in re.finditer(r'(?<!\d)(\d{9,10})(?!\d)', stem):
        try:
            epoch = int(m.group(1))  # seconds
            if 946684800 <= epoch <= 4102444800:
                return datetime.fromtimestamp(epoch)
        except (ValueError, OverflowError, OSError):
            continue

    # Fall back to phone mtime
    if phone_mtime_epoch:
        try:
            return datetime.fromtimestamp(phone_mtime_epoch)
        except (OSError, ValueError, OverflowError):
            pass

    return None


def safe_filename(dest_dir: Path, name: str, device_name: str = "") -> Path:
    """Generate a collision-free filename in dest_dir."""
    dest = dest_dir / name
    if not dest.exists():
        return dest

    stem = Path(name).stem
    suffix = Path(name).suffix

    if device_name:
        candidate = dest_dir / f"{stem}_{device_name}{suffix}"
        if not candidate.exists():
            return candidate

    for i in range(1, 10000):
        tag = f"_{device_name}_{i}" if device_name else f"_{i}"
        candidate = dest_dir / f"{stem}{tag}{suffix}"
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Cannot find unique filename for {name} in {dest_dir}")


# ---------------------------------------------------------------------------
# Core Sync Engine
# ---------------------------------------------------------------------------

class SyncEngine:
    def __init__(self, cfg: dict, device_serial: str, dry_run: bool = False):
        self.cfg = cfg
        self.data_dir = get_data_dir(cfg)
        self.dry_run = dry_run
        self.device_serial = device_serial

        self.device_name = self._resolve_device_name()
        self.adb = ADB(device_serial)
        self.state = DeviceState(device_serial, self.device_name, cfg)

        self.stats = {
            "files_copied": 0,
            "files_skipped": 0,
            "files_updated": 0,     # re-pulled due to mtime change
            "duplicates_kept": 0,
            "duplicates_skipped": 0,  # keep_duplicates=false re-pulls
            "moves_synced": 0,
            "phone_moves_detected": 0,
            "local_deletions": 0,   # files missing from computer
            "errors": 0,
            "bytes_copied": 0,
        }
        self.discovered_subdirs = []
        self.deleted_files = []  # [(relpath, category)] for deletion report

    def _resolve_device_name(self) -> str:
        devices_cfg = self.cfg.get("devices", {})
        if self.device_serial in devices_cfg:
            return devices_cfg[self.device_serial]["name"]

        existing_names = {d["name"] for d in devices_cfg.values()}
        model = ADB(self.device_serial).get_model()
        name = re.sub(r'[^a-zA-Z0-9]', '-', model).lower().strip('-')
        if not name:
            name = "phone"

        base_name = name
        counter = 1
        while name in existing_names:
            counter += 1
            name = f"{base_name}-{counter}"

        devices_cfg[self.device_serial] = {
            "name": name,
            "model": model,
            "sources": DEFAULT_PHONE_SOURCES,
        }
        self.cfg["devices"] = devices_cfg
        save_config(self.cfg)

        logging.info(f"Registered new device: {name} ({model}) [{self.device_serial}]")
        return name


    ## TODO study this
    def _get_sources(self) -> dict:
        dev_cfg = self.cfg.get("devices", {}).get(self.device_serial, {})
        return dev_cfg.get("sources", DEFAULT_PHONE_SOURCES)

    def _dest_dir_for_category(self, category: str) -> Path:
        if category == "photos":
            return self.data_dir / "photos"
        elif category == "downloads":
            return self.data_dir / "downloads" / self.device_name
        elif category == "recordings":
            return self.data_dir / "recordings" / self.device_name
        else:
            return self.data_dir / category / self.device_name

    def _compute_photo_dest(self, local_tmp: str, filename: str,
                            phone_relpath: str,
                            phone_mtime_epoch: int = None) -> Path:
        """Compute destination path for a photo.

        Photos go into photos/YYYY/ based on EXIF or filename date.
        If the file came from a subdirectory (e.g. KakaoTalk/), we preserve
        that structure under the year folder.
        """
        base = self.data_dir / "photos"

        if self.cfg.get("photo_date_folders", True):
            date = get_photo_date(local_tmp, phone_mtime_epoch)
            if date:
                date_dir = base / str(date.year)
            else:
                date_dir = base / "unsorted"
        else:
            date_dir = base

        # Preserve subdirectory structure from phone if configured
        if self.cfg.get("preserve_phone_subdirs", True):
            subdir = os.path.dirname(phone_relpath)
            if subdir:
                return date_dir / subdir
        return date_dir

    def _is_relevant_file(self, filename: str, category: str) -> bool:
        ext = Path(filename).suffix.lower()
        if category == "photos":
            return ext in PHOTO_EXTENSIONS
        elif category == "recordings":
            return RECORDING_EXTENSIONS is None or ext in RECORDING_EXTENSIONS
        return True

    def _check_subdirectories(self, phone_dir: str, category: str,
                               exclude_dirs: list, recursive: bool):
        """Discover subdirectories in a phone source dir and warn about them."""
        q = self.adb._q
        output = self.adb.shell(
            f'find {q(phone_dir)} -maxdepth 1 -type d 2>/dev/null',
            check=False)

        all_configured_sources = set()
        for dirs_list in self._get_sources().values():
            for d in dirs_list:
                all_configured_sources.add(d)

        for line in output.strip().split("\n"):
            line = line.strip()
            if not line or line == phone_dir:
                continue
            subdir_name = os.path.basename(line)
            if not subdir_name:
                continue

            count_out = self.adb.shell(
                f'find {q(line)} -type f 2>/dev/null | wc -l',
                check=False)
            try:
                file_count = int(count_out.strip())
            except ValueError:
                file_count = 0

            if file_count == 0:
                continue

            is_excluded = subdir_name in exclude_dirs
            is_configured = line in all_configured_sources

            if is_excluded:
                self.discovered_subdirs.append(
                    (phone_dir, subdir_name, file_count, "excluded"))
            elif not recursive and not is_configured:
                self.discovered_subdirs.append(
                    (phone_dir, subdir_name, file_count, "not_scanned"))
                logging.warning(
                    f"  ⚠ Subdirectory not scanned (recursive_scan=false): "
                    f"{line} ({file_count} files)")
            elif not is_configured:
                self.discovered_subdirs.append(
                    (phone_dir, subdir_name, file_count, "scanned"))

    def run(self):
        logging.info(f"{'[DRY RUN] ' if self.dry_run else ''}Starting sync for "
                     f"{self.device_name} ({self.device_serial})")

        # Scan the phone ONCE — all phases share this snapshot
        self.phone_scan = self._scan_phone()

        # Phase 1: Detect phone-side moves (must happen before ingest
        # so moved files aren't re-ingested as new)
        self._phase_detect_phone_moves()

        # Phase 2: Ingest genuinely new files
        self._phase_ingest()

        # Phase 3: Detect computer-side moves/deletes, propagate to phone
        self._phase_sync_moves()

        if not self.dry_run:
            self.state.save()

        self._print_summary()

    def _scan_phone(self) -> dict:
        """Scan all configured phone source directories once.

        Returns dict keyed by category:
            {category: [(phone_dir, [file_info, ...]), ...]}
        where file_info has {name, size, mtime_epoch, path, relpath}.

        Also builds a flat index: phone_path -> file_info
        """
        logging.info("=== Scanning phone ===")
        sources = self._get_sources()
        exclude_dirs = self.cfg.get("exclude_dirs", DEFAULT_EXCLUDE_DIRS)
        exclude_files = self.cfg.get("exclude_files", DEFAULT_EXCLUDE_FILES)
        recursive = self.cfg.get("recursive_scan", True)

        scan = {}  # category -> [(phone_dir, [files])]
        self._phone_path_index = {}  # phone_path -> file_info

        for category, dirs in sources.items():
            scan[category] = []
            for phone_dir in dirs:
                logging.info(f"  Scanning {phone_dir} ({category})"
                             f"{' [recursive]' if recursive else ''}...")
                if recursive:
                    files = self.adb.list_files_recursive(
                        phone_dir, exclude_dirs=exclude_dirs,
                        exclude_files=exclude_files)
                else:
                    files = self.adb.list_files(phone_dir)

                logging.info(f"    Found {len(files)} files")
                scan[category].append((phone_dir, files))

                for finfo in files:
                    self._phone_path_index[finfo["path"]] = finfo

                # Subdirectory discovery
                self._check_subdirectories(
                    phone_dir, category, exclude_dirs, recursive)

        return scan

    def _phase_detect_phone_moves(self):
        """Phase 1: Detect files that moved on the phone.

        Uses the shared phone scan to find files whose tracked phone_path
        no longer exists but whose content (by size + hash) is at a new
        path. Updates state so ingest doesn't re-download them.

        Avoids false moves: if a candidate new path is already tracked
        by another state entry, it's not a move target — it's a separate
        file that happens to have the same content.
        """
        logging.info("=== Phase 1: Detecting phone-side moves ===")
        sources = self._get_sources()

        # Build set of all phone_paths currently tracked in state,
        # so we don't "move" to a path that's already someone else's
        tracked_phone_paths = set()
        for relpath, info in self.state.files.items():
            if info.get("device_name") == self.device_name:
                tracked_phone_paths.add(info["phone_path"])

        for relpath, info in list(self.state.files.items()):
            if info.get("device_name") != self.device_name:
                continue
            old_phone_path = info["phone_path"]

            # Is the file still at its expected phone location?
            if old_phone_path in self._phone_path_index:
                continue

            # File is gone from expected phone path.
            # Search for it at a new path by size + hash.
            file_hash = info["hash"]
            expected_size = info.get("size", -1)
            new_phone_path = None

            for ppath, pinfo in self._phone_path_index.items():
                # Skip paths already tracked by another state entry
                if ppath in tracked_phone_paths and ppath != old_phone_path:
                    continue
                if pinfo["size"] != expected_size:
                    continue
                phone_hash = self.adb.file_hash(ppath)
                if phone_hash == file_hash:
                    new_phone_path = ppath
                    break

            if new_phone_path:
                logging.info(
                    f"  Phone-side move detected: "
                    f"{old_phone_path} -> {new_phone_path}")
                self.state.files[relpath]["phone_path"] = new_phone_path
                # Update tracked set
                tracked_phone_paths.discard(old_phone_path)
                tracked_phone_paths.add(new_phone_path)
                # Update phone_source_dir if it changed
                for category, dirs in sources.items():
                    for phone_dir in dirs:
                        if new_phone_path.startswith(phone_dir + "/") or \
                           new_phone_path == phone_dir:
                            self.state.files[relpath]["phone_source_dir"] = \
                                phone_dir
                            break
                self.stats["phone_moves_detected"] += 1
            # else: file deleted from phone — that's fine, we keep it on
            # computer (safe delete behavior)

    def _phase_ingest(self):
        """Phase 2: Ingest genuinely new files from phone.

        Only processes phone paths NOT already tracked in state (which
        now includes paths reconciled by phone-move detection in phase 1).
        """
        logging.info("=== Phase 2: Ingesting new files ===")

        for category, dir_scans in self.phone_scan.items():
            for phone_dir, files in dir_scans:
                self._ingest_files(phone_dir, category, files)

    def _ingest_files(self, phone_dir: str, category: str,
                      files: list[dict]):
        """Ingest files from a single phone directory scan."""
        for finfo in files:
            filename = finfo["name"]
            phone_path = finfo["path"]
            phone_relpath = finfo.get("relpath", filename)
            size = finfo["size"]

            if not self._is_relevant_file(filename, category):
                continue

            # Check if we already track this phone path
            existing = self.state.find_by_phone_path(phone_path)
            if existing:
                # Check for tombstone — clear it if file reappeared
                # at the same phone_path with same content
                info = self.state.files[existing]
                if info.get("deleted_from_computer"):
                    # Tombstoned — skip (user intentionally deleted local)
                    self.stats["files_skipped"] += 1
                    continue

                # Compare mtime — re-pull if file changed on phone
                old_mtime = info.get("phone_mtime", "")
                new_mtime_iso = datetime.fromtimestamp(
                    finfo["mtime_epoch"]).isoformat()
                if old_mtime and old_mtime == new_mtime_iso:
                    self.stats["files_skipped"] += 1
                    continue
                elif old_mtime:
                    logging.info(
                        f"  File changed on phone (mtime): {phone_path}")
                    logging.info(
                        f"    old={old_mtime}  new={new_mtime_iso}")
                    # Fall through to re-pull
                else:
                    self.stats["files_skipped"] += 1
                    continue

            # Pull to temp location
            tmp_dir = Path(self.cfg["config_dir"]) / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = tmp_dir / filename

            if self.dry_run:
                if existing:
                    logging.info(f"  [DRY RUN] Would re-pull: {phone_path}")
                else:
                    logging.info(f"  [DRY RUN] Would copy: {phone_path}")
                self.stats["files_copied"] += 1
                continue

            logging.info(f"  {'Re-pulling' if existing else 'Copying'}: "
                         f"{phone_path} ({_human_size(size)})")
            if not self.adb.pull(phone_path, str(tmp_path)):
                self.stats["errors"] += 1
                continue

            local_hash = file_sha256(str(tmp_path))

            # If this is a re-pull of a changed file, update in place
            if existing:
                old_computer_path = self.data_dir / existing
                if old_computer_path.exists():
                    shutil.move(str(tmp_path), str(old_computer_path))
                    mtime_iso = datetime.fromtimestamp(
                        finfo["mtime_epoch"]).isoformat()
                    self.state.files[existing]["hash"] = local_hash
                    self.state.files[existing]["size"] = size
                    self.state.files[existing]["phone_mtime"] = mtime_iso
                    self.state.files[existing]["synced_at"] = \
                        datetime.now().isoformat()
                    self.stats["files_updated"] += 1
                    self.stats["bytes_copied"] += size
                    continue
                else:
                    # CONFLICT: file edited on phone AND moved on computer.
                    # Search for the moved file using the OLD hash.
                    old_hash = self.state.files[existing].get("hash")
                    moved_to = None
                    if old_hash:
                        moved_to = self._find_file_by_hash(
                            old_hash, previous_path=old_computer_path)
                    if moved_to:
                        logging.info(
                            f"  Conflict: file moved on computer AND "
                            f"edited on phone.")
                        new_relpath = str(
                            moved_to.relative_to(self.data_dir))
                        logging.info(
                            f"    Updating at moved location: "
                            f"{new_relpath}")
                        shutil.move(str(tmp_path), str(moved_to))
                        old_info = self.state.files.pop(existing)
                        mtime_iso = datetime.fromtimestamp(
                            finfo["mtime_epoch"]).isoformat()
                        old_info["hash"] = local_hash
                        old_info["size"] = size
                        old_info["phone_mtime"] = mtime_iso
                        old_info["synced_at"] = datetime.now().isoformat()
                        self.state.files[new_relpath] = old_info
                        self.stats["files_updated"] += 1
                        self.stats["bytes_copied"] += size
                        continue
                    else:
                        logging.warning(
                            f"  File edited on phone but local copy "
                            f"missing and not found elsewhere: "
                            f"{existing}. Treating as new file.")
                        del self.state.files[existing]

            # Check for duplicate by hash
            existing_by_hash = self.state.find_by_hash_and_device(local_hash)
            keep_duplicates = self.cfg.get("keep_duplicates", True)

            if existing_by_hash and not keep_duplicates:
                logging.info(f"  Skipping (duplicate by hash): {filename}")
                logging.debug(
                    f"    Note: keep_duplicates=false means this file "
                    f"will be re-pulled and re-hashed every sync. "
                    f"Consider keep_duplicates=true if you have many "
                    f"duplicates on the phone.")
                tmp_path.unlink(missing_ok=True)
                # Do NOT overwrite the existing entry's phone_path —
                # that would lose the original phone_path tracking.
                # The duplicate phone_path simply isn't tracked.
                self.stats["duplicates_skipped"] += 1
                self.stats["files_skipped"] += 1
                continue
            elif existing_by_hash:
                logging.info(f"  Keeping duplicate (same content, "
                             f"different path): {phone_path}")
                self.stats["duplicates_kept"] += 1

            # Determine destination
            if category == "photos":
                dest_dir = self._compute_photo_dest(
                    str(tmp_path), filename, phone_relpath,
                    phone_mtime_epoch=finfo["mtime_epoch"])
            else:
                dest_dir = self._dest_dir_for_category(category)
                if self.cfg.get("preserve_phone_subdirs", True):
                    subdir = os.path.dirname(phone_relpath)
                    if subdir:
                        dest_dir = dest_dir / subdir

            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = safe_filename(dest_dir, filename, self.device_name)

            shutil.move(str(tmp_path), str(dest_path))

            relpath = str(dest_path.relative_to(self.data_dir))
            mtime_iso = datetime.fromtimestamp(finfo["mtime_epoch"]).isoformat()
            self.state.add_file(
                relpath, phone_path, local_hash, size, mtime_iso, category,
                phone_source_dir=phone_dir)

            self.stats["files_copied"] += 1
            self.stats["bytes_copied"] += size

        # Cleanup tmp
        tmp_dir = Path(self.cfg["config_dir"]) / "tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _phase_sync_moves(self):
        """Phase 3: Detect moves on computer, propagate to phone.

        Also:
        - Reports files deleted from the computer
        - Clears tombstones for files that have reappeared
        - Recomputes desired phone paths for stale moves
        """
        logging.info("=== Phase 3: Detecting computer-side moves ===")

        moves_to_apply = []

        # First pass: clear tombstones for files that reappeared
        for relpath, info in list(self.state.files.items()):
            if info.get("deleted_from_computer"):
                computer_path = self.data_dir / relpath
                if computer_path.exists():
                    logging.info(f"  File reappeared: {relpath}")
                    del self.state.files[relpath]["deleted_from_computer"]

        # Second pass: detect moves and deletions
        for relpath, info in list(self.state.files.items()):
            if info.get("deleted_from_computer"):
                continue  # already tombstoned from a previous run

            computer_path = self.data_dir / relpath

            if computer_path.exists():
                continue

            # Only track moves for files from this device
            if info.get("device_name") != self.device_name:
                continue

            file_hash = info["hash"]
            new_path = self._find_file_by_hash(file_hash,
                                                previous_path=computer_path)

            if new_path:
                new_relpath = str(new_path.relative_to(self.data_dir))

                # Don't "move" if already tracked under a different entry
                if new_relpath in self.state.files and new_relpath != relpath:
                    logging.debug(
                        f"  File at {new_relpath} already tracked, "
                        f"removing stale entry {relpath}")
                    del self.state.files[relpath]
                    continue

                logging.info(
                    f"  Detected move on computer: {relpath} -> {new_relpath}")

                old_info = self.state.files.pop(relpath)
                old_info["synced_at"] = datetime.now().isoformat()
                self.state.files[new_relpath] = old_info

                # Compute desired phone path and queue the move
                if info["category"] == "photos":
                    desired = self._compute_desired_phone_path(
                        new_path, old_info)
                    if desired and desired != old_info["phone_path"]:
                        moves_to_apply.append(
                            (old_info["phone_path"], desired,
                             new_relpath, file_hash))

                self.stats["moves_synced"] += 1
            else:
                # File deleted from computer — tombstone it
                self.deleted_files.append(
                    (relpath, info.get("category", "unknown")))
                self.stats["local_deletions"] += 1
                self.state.files[relpath]["deleted_from_computer"] = True

        # Also recompute desired phone paths for files that haven't moved
        # on the computer but whose phone path might be stale (e.g. from
        # a previous failed move)
        for relpath, info in list(self.state.files.items()):
            if info.get("device_name") != self.device_name:
                continue
            if info.get("deleted_from_computer"):
                continue
            computer_path = self.data_dir / relpath
            if not computer_path.exists():
                continue
            if info["category"] != "photos":
                continue

            desired = self._compute_desired_phone_path(computer_path, info)
            if desired and desired != info["phone_path"]:
                already_queued = any(
                    m[2] == relpath for m in moves_to_apply)
                if not already_queued:
                    moves_to_apply.append(
                        (info["phone_path"], desired,
                         relpath, info["hash"]))

        # Execute phone-side moves with collision safety
        for old_phone, new_phone, relpath, file_hash in moves_to_apply:
            if self.dry_run:
                logging.info(
                    f"  [DRY RUN] Would move on phone: "
                    f"{old_phone} -> {new_phone}")
                continue

            # Check that source still exists on phone
            if not self.adb.file_exists(old_phone):
                logging.warning(
                    f"  Phone source gone, skipping move: {old_phone}")
                continue

            logging.info(f"  Moving on phone: {old_phone} -> {new_phone}")
            result = self.adb.move_safe(old_phone, new_phone, file_hash)
            if result["ok"]:
                self.state.files[relpath]["phone_path"] = new_phone
                if result["action"] == "already_there":
                    logging.info(f"    (destination already existed, "
                                 f"source {'removed' if result['source_deleted'] else 'kept'})")
            else:
                self.stats["errors"] += 1

    def _compute_desired_phone_path(self, computer_path: Path,
                                     info: dict) -> Optional[str]:
        """Compute where a file should be on the phone based on its
        computer location.

        Strips auto-generated structure (photos/, year folders) and
        mirrors meaningful subfolder names to the phone.
        """
        phone_source = info.get("phone_source_dir", "")
        if not phone_source:
            phone_source = os.path.dirname(info["phone_path"])
        phone_filename = os.path.basename(info["phone_path"])

        try:
            rel = computer_path.relative_to(self.data_dir)
        except ValueError:
            return None

        subfolder_parts = list(rel.parent.parts)

        # Strip auto-generated leading structure only
        meaningful = list(subfolder_parts)
        if meaningful and meaningful[0] == "photos":
            meaningful.pop(0)
            if meaningful and re.match(r'^\d{4}$', meaningful[0]):
                meaningful.pop(0)
        if meaningful and meaningful[0] == "unsorted":
            meaningful.pop(0)

        if meaningful:
            extra = "/".join(meaningful)
            return f"{phone_source}/{extra}/{phone_filename}"
        else:
            return f"{phone_source}/{phone_filename}"

    def _find_file_by_hash(self, target_hash: str,
                           previous_path: Path = None) -> Optional[Path]:
        """Search for a file by hash with priority ordering.

        Search order (to handle hash collisions gracefully):
          1. The directory the file was previously in
          2. Subdirectories of the previous directory
          3. Parent directories walking upward toward data_dir
          4. Everything else under data_dir

        Tracks resolved real paths to detect symlink cycles, with a
        configurable max_symlink_depth (default 2).

        Args:
            target_hash: SHA256 hash to search for.
            previous_path: The last known full path of the file (used to
                determine search priority). If None, searches all of
                data_dir with no priority.
        """
        if not self.data_dir.exists():
            return None

        max_symlink_depth = self.cfg.get("max_symlink_depth", 2)
        searched = set()       # track searched dirs to avoid re-walking
        resolved_seen = set()  # track resolved paths to detect cycles

        def _check_file(fpath: Path) -> bool:
            try:
                return file_sha256(str(fpath)) == target_hash
            except (OSError, IOError):
                return False

        def _symlink_depth(path: Path) -> int:
            """Count how many symlink hops are in a path's ancestry."""
            depth = 0
            for p in [path] + list(path.parents):
                if p.is_symlink():
                    depth += 1
            return depth

        def _scan_dir_only(directory: Path) -> Optional[Path]:
            """Check only immediate files in a directory (non-recursive)."""
            if not directory.is_dir() or str(directory) in searched:
                return None
            if _symlink_depth(directory) > max_symlink_depth:
                return None
            real = str(directory.resolve())
            if real in resolved_seen:
                return None  # cycle
            searched.add(str(directory))
            resolved_seen.add(real)
            try:
                for item in directory.iterdir():
                    if item.is_file() and _check_file(item):
                        return item
            except (OSError, PermissionError):
                pass
            return None

        def _scan_recursive(directory: Path) -> Optional[Path]:
            """Recursively search a directory, skipping already-searched dirs."""
            if not directory.is_dir():
                return None
            for root, dirs, files in os.walk(directory, followlinks=True):
                root_path = Path(root)
                real_root = str(root_path.resolve())
                if real_root in resolved_seen and str(root_path) not in searched:
                    dirs.clear()  # cycle — don't descend
                    continue
                if _symlink_depth(root_path) > max_symlink_depth:
                    dirs.clear()
                    continue
                resolved_seen.add(real_root)
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                if str(root_path) not in searched:
                    searched.add(str(root_path))
                    for fname in files:
                        fpath = root_path / fname
                        if _check_file(fpath):
                            return fpath
            return None

        if previous_path:
            prev_dir = previous_path.parent

            # Priority 1: Previous directory (flat scan)
            result = _scan_dir_only(prev_dir)
            if result:
                return result

            # Priority 2: Subdirectories of previous directory
            result = _scan_recursive(prev_dir)
            if result:
                return result

            # Priority 3: Walk up toward data_dir
            current = prev_dir.parent
            while current >= self.data_dir:
                result = _scan_dir_only(current)
                if result:
                    return result
                if current == self.data_dir:
                    break
                current = current.parent

        # Priority 4: Everything else under data_dir
        return _scan_recursive(self.data_dir)

    def _print_summary(self):
        s = self.stats
        prefix = "[DRY RUN] " if self.dry_run else ""
        print(f"\n{prefix}Sync complete for {self.device_name}:")
        print(f"  Files copied:     {s['files_copied']}")
        if s["files_updated"]:
            print(f"  Files updated:    {s['files_updated']} "
                  f"(re-pulled, mtime changed)")
        print(f"  Files skipped:    {s['files_skipped']} (already synced)")
        if s["duplicates_kept"]:
            print(f"  Duplicates kept:  {s['duplicates_kept']} "
                  f"(same content, different path)")
        if s["duplicates_skipped"]:
            print(f"  Duplicates skip:  {s['duplicates_skipped']} "
                  f"(re-pulled to verify, keep_duplicates=false)")
            if s["duplicates_skipped"] > 10:
                print(f"  ⚠ {s['duplicates_skipped']} duplicate files were "
                      f"pulled and discarded. With keep_duplicates=false, "
                      f"this happens every sync. Consider "
                      f"keep_duplicates=true to avoid repeated transfers.")
        if s["phone_moves_detected"]:
            print(f"  Phone moves:      {s['phone_moves_detected']}")
        print(f"  Moves synced:     {s['moves_synced']}")
        print(f"  Errors:           {s['errors']}")
        if s["bytes_copied"] > 0:
            print(f"  Data copied:      {_human_size(s['bytes_copied'])}")

        # === DELETION REPORT ===
        if self.deleted_files:
            print()
            print(f"  ⚠ WARNING: {len(self.deleted_files)} file(s) deleted "
                  f"from computer (kept in state, won't re-download):")

            # Group by directory for readability
            by_dir = defaultdict(list)
            for relpath, category in self.deleted_files:
                parent = str(Path(relpath).parent)
                by_dir[parent].append((relpath, category))

            for parent_dir, files_in_dir in sorted(by_dir.items()):
                if len(files_in_dir) <= 10:
                    for relpath, category in files_in_dir:
                        print(f"    DELETED: {relpath}")
                else:
                    print(f"    DELETED: {len(files_in_dir)} files in "
                          f"{parent_dir}/")
                    # Show first 3 as examples
                    for relpath, category in files_in_dir[:3]:
                        print(f"      e.g. {os.path.basename(relpath)}")
                    print(f"      ... and {len(files_in_dir) - 3} more")

            print(f"  → These files won't be re-downloaded from the phone.")
            print(f"    To re-download them, run: "
                  f"phonesync prune-state --clear-tombstones")

        # Subdirectory report
        if self.discovered_subdirs:
            not_scanned = [e for e in self.discovered_subdirs
                           if e[3] == "not_scanned"]
            excluded = [e for e in self.discovered_subdirs
                        if e[3] == "excluded"]
            scanned = [e for e in self.discovered_subdirs
                       if e[3] == "scanned"]

            if not_scanned:
                print(f"\n  ⚠ WARNING: Subdirectories with files NOT being synced "
                      f"(recursive_scan=false):")
                for phone_dir, name, count, _ in not_scanned:
                    print(f"    {phone_dir}/{name}/ ({count} files)")
                print(f"  → Add these to your sources in config, or set "
                      f"recursive_scan=true")

            if excluded:
                print(f"\n  Excluded subdirectories (in exclude_dirs):")
                for phone_dir, name, count, _ in excluded:
                    print(f"    {phone_dir}/{name}/ ({count} files)")

            if scanned:
                print(f"\n  Subdirectories scanned (via recursive scan):")
                for phone_dir, name, count, _ in scanned:
                    print(f"    {phone_dir}/{name}/ ({count} files)")
                print(f"  → These are included via recursive scan. To exclude "
                      f"any, add to exclude_dirs in config.")


def _human_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------

def cmd_sync(args):
    cfg = load_config()
    lock = SyncLock(Path(cfg["config_dir"]))

    if not lock.acquire():
        print("Another phonesync instance is already running. Exiting.")
        sys.exit(1)

    try:
        if args.device:
            serials = [args.device]
        else:
            devices = list_connected_devices()
            if not devices:
                print("No ADB devices connected.")
                print("Connect a phone with USB debugging enabled.")
                sys.exit(1)
            serials = [d["serial"] for d in devices]
            print(f"Found {len(devices)} device(s): "
                  f"{', '.join(d['model'] for d in devices)}")

        for serial in serials:
            engine = SyncEngine(cfg, serial, dry_run=args.dry_run)
            engine.run()
            print()
    finally:
        lock.release()


def cmd_status(args):
    cfg = load_config()
    cfg_dir = Path(cfg["config_dir"])
    data_dir = get_data_dir(cfg)

    print(f"Config directory: {cfg_dir}")
    print(f"Data directory:   {data_dir}")
    print()

    devices = cfg.get("devices", {})
    if not devices:
        print("No devices registered yet. "
              "Connect a phone and run 'phonesync sync'.")
        return

    for serial, dev_info in devices.items():
        name = dev_info["name"]
        model = dev_info.get("model", "unknown")
        state_path = cfg_dir / f"state-{name}.json"

        print(f"Device: {name} ({model})")
        print(f"  Serial: {serial}")

        sources = dev_info.get("sources", {})
        for cat, dirs in sources.items():
            print(f"  {cat}: {', '.join(dirs)}")

        if state_path.exists():
            with open(state_path) as f:
                state_data = json.load(f)
            file_count = len(state_data.get("files", {}))
            last_sync = state_data.get("last_sync", "never")
            total_size = sum(
                f.get("size", 0)
                for f in state_data.get("files", {}).values())
            print(f"  Files synced: {file_count}")
            print(f"  Total size:   {_human_size(total_size)}")
            print(f"  Last sync:    {last_sync}")
        else:
            print("  Not yet synced")
        print()

    connected = list_connected_devices()
    if connected:
        print(f"Currently connected: "
              f"{', '.join(d['model'] for d in connected)}")
    else:
        print("No devices currently connected.")


def cmd_devices(args):
    devices = list_connected_devices()
    if not devices:
        print("No ADB devices connected.")
        print("Make sure USB debugging is enabled and the phone is plugged in.")
        return

    for d in devices:
        serial = d["serial"]
        model = d["model"]
        print(f"\n  {serial}  {model}")

        adb = ADB(serial)
        q = adb._q
        volumes = adb.list_storage_volumes()
        for vol in volumes:
            vtype = vol["type"]
            vpath = vol["path"]
            label = vol["label"]
            print(f"    {label}: {vpath}")

            for subdir in ["DCIM", "Pictures", "Download", "Recordings"]:
                full = f"{vpath}/{subdir}"
                check = adb.shell(
                    f'[ -d {q(full)} ] && echo EXISTS || echo MISSING',
                    check=False)
                if "EXISTS" in check:
                    count_out = adb.shell(
                        f'find {q(full)} -type f 2>/dev/null | wc -l',
                        check=False)
                    count = count_out.strip()
                    subdirs_out = adb.shell(
                        f'ls -1d {q(full)}/*/ 2>/dev/null | head -20',
                        check=False)
                    subdirs = [
                        os.path.basename(s.rstrip("/"))
                        for s in subdirs_out.strip().split("\n")
                        if s.strip()
                    ]
                    subdir_str = ""
                    if subdirs:
                        subdir_str = f"  [{', '.join(subdirs)}]"
                    print(f"      {full} ({count} files){subdir_str}")


def cmd_detect_paths(args):
    """Auto-detect useful paths on connected phone(s)."""
    cfg = load_config()

    if args.device:
        serials = [args.device]
    else:
        devices = list_connected_devices()
        if not devices:
            print("No ADB devices connected.")
            sys.exit(1)
        serials = [d["serial"] for d in devices]

    for serial in serials:
        adb = ADB(serial)
        model = adb.get_model()
        print(f"\n=== {model} ({serial}) ===")

        volumes = adb.list_storage_volumes()
        suggested_sources = {"photos": [], "downloads": [], "recordings": []}

        for vol in volumes:
            vpath = vol["path"]
            label = vol["label"]
            print(f"\n  {label} ({vpath}):")

            media_dirs = [
                ("DCIM", "photos"), ("Pictures", "photos"),
                ("Download", "downloads"), ("Downloads", "downloads"),
                ("Recordings", "recordings"), ("Music", "recordings"),
            ]
            for dirname, category in media_dirs:
                full = f"{vpath}/{dirname}"
                q = adb._q
                check = adb.shell(
                    f'[ -d {q(full)} ] && echo EXISTS || echo MISSING',
                    check=False)
                if "EXISTS" in check:
                    count_out = adb.shell(
                        f'find {q(full)} -type f 2>/dev/null | wc -l',
                        check=False)
                    count = count_out.strip()
                    print(f"    found: {full} ({count} files) -> {category}")
                    if full not in suggested_sources[category]:
                        suggested_sources[category].append(full)

                    # Show subdirectories
                    subdirs_out = adb.shell(
                        f'find {q(full)} -maxdepth 1 -type d 2>/dev/null',
                        check=False)
                    for sd in subdirs_out.strip().split("\n"):
                        sd = sd.strip()
                        if sd and sd != full:
                            sdname = os.path.basename(sd)
                            sd_count = adb.shell(
                                f'find {q(sd)} -type f 2>/dev/null '
                                f'| wc -l',
                                check=False).strip()
                            excluded = DEFAULT_EXCLUDE_DIRS
                            marker = ("  [EXCLUDED]"
                                      if sdname in excluded else "")
                            print(f"      {sdname}/ "
                                  f"({sd_count} files){marker}")

        print(f"\n  Suggested config for this device:")
        print(f"  {json.dumps(suggested_sources, indent=4)}")

        if args.apply:
            devices_cfg = cfg.get("devices", {})
            if serial in devices_cfg:
                devices_cfg[serial]["sources"] = suggested_sources
                cfg["devices"] = devices_cfg
                save_config(cfg)
                print(f"\n  Applied to config!")
            else:
                print(f"\n  Device not yet registered. Run 'phonesync sync' "
                      f"first to register, then re-run with --apply.")


def cmd_config(args):
    if args.init:
        config_dir = args.config_dir or str(DEFAULT_CONFIG_DIR)
        data_dir = args.data_dir or str(DEFAULT_DATA_DIR)

        # Resolve paths
        config_dir = str(Path(config_dir).expanduser().resolve())
        data_dir = str(Path(data_dir).expanduser().resolve())

        cfg = default_config(config_dir=config_dir, data_dir=data_dir)
        save_config(cfg)

        dd = Path(data_dir)
        for d in ["photos", "downloads", "recordings"]:
            (dd / d).mkdir(parents=True, exist_ok=True)

        print(f"Initialized PhoneSync:")
        print(f"  Config: {config_dir}/config.json")
        print(f"  Data:   {data_dir}/")
    else:
        cfg = load_config()
        print(json.dumps(cfg, indent=2))


def cmd_prune_state(args):
    """Remove stale entries from state.

    By default, removes entries where the computer file is missing AND
    no tombstone is set. Tombstoned entries (deleted_from_computer=True)
    are kept so those files won't be re-downloaded.

    --clear-tombstones: Also remove tombstoned entries. This means
        the next sync WILL re-download those files from the phone.
        Use this if you accidentally deleted files and want them back.

    --rehash: Recompute hashes for files that still exist (slow).
    """
    cfg = load_config()
    cfg_dir = Path(cfg["config_dir"])
    data_dir = get_data_dir(cfg)

    devices = cfg.get("devices", {})
    if not devices:
        print("No devices registered. Nothing to prune.")
        return

    target_device = args.device
    clear_tombstones = args.clear_tombstones
    rehash = args.rehash

    for serial, dev_info in devices.items():
        name = dev_info["name"]
        if target_device and target_device not in (serial, name):
            continue

        state_path = cfg_dir / f"state-{name}.json"
        if not state_path.exists():
            print(f"No state file for {name}, skipping.")
            continue

        print(f"\nPruning state for {name}...")

        state = DeviceState(serial, name, cfg)
        original_count = len(state.files)

        removed_missing = 0
        removed_tombstones = 0
        updated = 0
        kept = 0
        tombstones_kept = 0

        for relpath in list(state.files.keys()):
            info = state.files[relpath]
            computer_path = data_dir / relpath
            is_tombstoned = info.get("deleted_from_computer", False)

            if not computer_path.exists():
                if is_tombstoned and not clear_tombstones:
                    tombstones_kept += 1
                elif is_tombstoned:
                    del state.files[relpath]
                    removed_tombstones += 1
                else:
                    del state.files[relpath]
                    removed_missing += 1
            elif rehash:
                new_hash = file_sha256(str(computer_path))
                if new_hash != info.get("hash"):
                    state.files[relpath]["hash"] = new_hash
                    state.files[relpath]["size"] = computer_path.stat().st_size
                    updated += 1
                else:
                    kept += 1
            else:
                kept += 1

        state.save()

        print(f"  Original entries:   {original_count}")
        print(f"  Kept:               {kept}")
        if tombstones_kept:
            print(f"  Tombstones kept:    {tombstones_kept} "
                  f"(use --clear-tombstones to remove)")
        if updated:
            print(f"  Re-hashed:          {updated}")
        if removed_missing:
            print(f"  Removed (missing):  {removed_missing} "
                  f"(will re-download on next sync)")
        if removed_tombstones:
            print(f"  Tombstones cleared: {removed_tombstones} "
                  f"(will re-download on next sync)")
        print(f"  Final entries:      {len(state.files)}")


def cmd_reset_state(args):
    cfg = load_config()
    cfg_dir = Path(cfg["config_dir"])

    if args.device:
        devices = cfg.get("devices", {})
        if args.device in devices:
            name = devices[args.device]["name"]
        else:
            name = args.device
        state_path = cfg_dir / f"state-{name}.json"
        if state_path.exists():
            state_path.unlink()
            print(f"Reset state for {name}")
        else:
            print(f"No state file found for {name}")
    else:
        for f in cfg_dir.glob("state-*.json"):
            f.unlink()
            print(f"Reset: {f.name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="phonesync",
        description="Sync photos, downloads, and recordings from "
                    "Android phones via ADB",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose output")
    subparsers = parser.add_subparsers(dest="command")

    # sync
    p_sync = subparsers.add_parser(
        "sync", help="Sync files from connected phone(s)")
    p_sync.add_argument(
        "-d", "--device", help="ADB device serial (default: all connected)")
    p_sync.add_argument(
        "-n", "--dry-run", action="store_true",
        help="Show what would be done")

    # status
    subparsers.add_parser("status", help="Show sync status")

    # devices
    subparsers.add_parser(
        "devices", help="List connected ADB devices and storage volumes")

    # detect-paths
    p_detect = subparsers.add_parser(
        "detect-paths",
        help="Auto-detect media directories on connected phone(s)")
    p_detect.add_argument(
        "-d", "--device", help="ADB device serial")
    p_detect.add_argument(
        "--apply", action="store_true",
        help="Apply detected paths to config")

    # config
    p_config = subparsers.add_parser(
        "config", help="Show or initialize config")
    p_config.add_argument(
        "--init", action="store_true",
        help="Initialize config and directories")
    p_config.add_argument(
        "--config-dir",
        help=f"Config directory (default: {DEFAULT_CONFIG_DIR})")
    p_config.add_argument(
        "--data-dir",
        help=f"Data directory (default: {DEFAULT_DATA_DIR})")

    # reset-state
    p_reset = subparsers.add_parser(
        "reset-state", help="Reset sync state for a device")
    p_reset.add_argument(
        "-d", "--device", help="Device serial or name (default: all)")

    # prune-state
    p_prune = subparsers.add_parser(
        "prune-state",
        help="Remove stale state entries (keeps tombstones by default)")
    p_prune.add_argument(
        "-d", "--device", help="Device serial or name (default: all)")
    p_prune.add_argument(
        "--clear-tombstones", action="store_true",
        help="Also remove tombstoned entries (will re-download on next sync)")
    p_prune.add_argument(
        "--rehash", action="store_true",
        help="Recompute file hashes (slow but thorough)")

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(format=LOG_FORMAT, level=level)

    if args.command == "sync":
        cmd_sync(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "devices":
        cmd_devices(args)
    elif args.command == "detect-paths":
        cmd_detect_paths(args)
    elif args.command == "config":
        cmd_config(args)
    elif args.command == "prune-state":
        cmd_prune_state(args)
    elif args.command == "reset-state":
        cmd_reset_state(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
