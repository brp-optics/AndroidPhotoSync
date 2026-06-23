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
  phonesync reindex [--device SERIAL] [--rehash]
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
    """File-based lock to prevent concurrent phonesync runs."""

    def __init__(self, cfg_dir: Path):
        self.lock_path = cfg_dir / "sync.lock"

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = open(self.lock_path, "w")
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd.write(f"{os.getpid()}\n{datetime.now().isoformat()}\n")
            self._fd.flush()
            return True
        except (IOError, OSError):
            return False

    def release(self):
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()
            self.lock_path.unlink(missing_ok=True)
        except (IOError, OSError, AttributeError):
            pass


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
                             max_depth: int = 10) -> list[dict]:
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
                  expected_hash: str = None) -> bool:
        """Move a file on the phone with collision check and hash verification.

        Returns True only if the move succeeded and (if expected_hash is
        provided) the file at the destination matches the expected hash.
        """
        q = self._q
        # Check if destination already exists
        check = self.shell(
            f'[ -e {q(remote_dst)} ] && echo EXISTS || echo FREE',
            check=False)
        if "EXISTS" in check:
            logging.warning(
                f"  Phone move collision: {remote_dst} already exists")
            # Check if it's the same file
            if expected_hash:
                existing_hash = self.file_hash(remote_dst)
                if existing_hash == expected_hash:
                    logging.info(
                        f"  Collision is same file, removing source")
                    self.delete(remote_src)
                    return True
            logging.error(
                f"  Cannot move {remote_src} -> {remote_dst}: "
                f"destination exists with different content")
            return False

        if not self.move(remote_src, remote_dst):
            return False

        # Verify hash after move
        if expected_hash:
            actual_hash = self.file_hash(remote_dst)
            if actual_hash != expected_hash:
                logging.error(
                    f"  Hash mismatch after phone move! "
                    f"Expected {expected_hash[:12]}..., "
                    f"got {actual_hash[:12] if actual_hash else 'None'}...")
                # Move it back
                self.move(remote_dst, remote_src)
                return False

        return True

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

    def run(self):
        logging.info(f"{'[DRY RUN] ' if self.dry_run else ''}Starting sync for "
                     f"{self.device_name} ({self.device_serial})")

        self._phase_ingest()
        self._phase_detect_phone_moves()
        self._phase_sync_moves()

        if not self.dry_run:
            self.state.save()

        self._print_summary()

    def _phase_ingest(self):
        logging.info("=== Phase 1: Ingesting new files ===")
        sources = self._get_sources()
        exclude_dirs = self.cfg.get("exclude_dirs", DEFAULT_EXCLUDE_DIRS)
        exclude_files = self.cfg.get("exclude_files", DEFAULT_EXCLUDE_FILES)
        recursive = self.cfg.get("recursive_scan", True)

        for category, dirs in sources.items():
            for phone_dir in dirs:
                self._ingest_directory(
                    phone_dir, category, exclude_dirs, exclude_files,
                    recursive)

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
            # Check if this subdir is itself a configured source
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
                # Recursive is on, so files ARE being picked up, but still
                # worth noting for awareness
                self.discovered_subdirs.append(
                    (phone_dir, subdir_name, file_count, "scanned"))

    def _ingest_directory(self, phone_dir: str, category: str,
                          exclude_dirs: list, exclude_files: list,
                          recursive: bool):
        logging.info(f"Scanning {phone_dir} ({category})"
                     f"{' [recursive]' if recursive else ''}...")

        if recursive:
            files = self.adb.list_files_recursive(
                phone_dir, exclude_dirs=exclude_dirs,
                exclude_files=exclude_files)
        else:
            files = self.adb.list_files(phone_dir)

        logging.info(f"  Found {len(files)} files")

        # Discover subdirectories and warn about them
        self._check_subdirectories(phone_dir, category, exclude_dirs, recursive)

        for finfo in files:
            filename = finfo["name"]
            phone_path = finfo["path"]
            phone_relpath = finfo.get("relpath", filename)
            size = finfo["size"]

            if not self._is_relevant_file(filename, category):
                continue

            # Check if we already track this exact phone path
            existing = self.state.find_by_phone_path(phone_path)
            if existing:
                # Compare mtime — re-pull if file changed on phone
                old_info = self.state.files[existing]
                old_mtime = old_info.get("phone_mtime", "")
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
                    # No stored mtime (old state format), skip
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
                    # Old file missing from computer — treat as new copy
                    del self.state.files[existing]

            # Check for duplicate by hash
            existing_by_hash = self.state.find_by_hash_and_device(local_hash)
            keep_duplicates = self.cfg.get("keep_duplicates", True)

            if existing_by_hash and not keep_duplicates:
                logging.info(f"  Skipping (duplicate by hash): {filename}")
                tmp_path.unlink(missing_ok=True)
                # Still record the phone_path -> existing mapping
                self.state.add_file(
                    existing_by_hash[0], phone_path, local_hash,
                    size, datetime.fromtimestamp(finfo["mtime_epoch"]).isoformat(),
                    category, phone_source_dir=phone_dir)
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

    def _phase_detect_phone_moves(self):
        """Detect files that moved on the phone and update state.

        If a tracked phone_path no longer exists but a file with the same
        hash is found at a new phone path (from the current scan), update
        the state's phone_path so we don't re-ingest it as a duplicate.
        """
        logging.info("=== Phase 2: Detecting phone-side moves ===")
        sources = self._get_sources()
        exclude_dirs = self.cfg.get("exclude_dirs", DEFAULT_EXCLUDE_DIRS)
        exclude_files = self.cfg.get("exclude_files", DEFAULT_EXCLUDE_FILES)

        # Build a map of all files currently on the phone
        # phone_path -> {hash, mtime_epoch, size}
        phone_files = {}
        for category, dirs in sources.items():
            for phone_dir in dirs:
                files = self.adb.list_files_recursive(
                    phone_dir, exclude_dirs=exclude_dirs,
                    exclude_files=exclude_files)
                for finfo in files:
                    phone_files[finfo["path"]] = finfo

        # Check each tracked file
        for relpath, info in list(self.state.files.items()):
            if info.get("device_name") != self.device_name:
                continue
            old_phone_path = info["phone_path"]

            # Is the file still at its expected phone location?
            if old_phone_path in phone_files:
                continue

            # File is gone from expected phone path.
            # Check if it re-appeared at a different path (by hash).
            file_hash = info["hash"]
            new_phone_path = None
            for ppath, pinfo in phone_files.items():
                # Quick size check first (avoid expensive hash)
                if pinfo["size"] != info.get("size", -1):
                    continue
                # Check hash on phone
                phone_hash = self.adb.file_hash(ppath)
                if phone_hash == file_hash:
                    new_phone_path = ppath
                    break

            if new_phone_path:
                logging.info(
                    f"  Phone-side move detected: "
                    f"{old_phone_path} -> {new_phone_path}")
                self.state.files[relpath]["phone_path"] = new_phone_path
                # Update phone_source_dir if it changed
                for category, dirs in sources.items():
                    for phone_dir in dirs:
                        if new_phone_path.startswith(phone_dir):
                            self.state.files[relpath]["phone_source_dir"] = \
                                phone_dir
                            break
                self.stats["phone_moves_detected"] += 1
            # else: file deleted from phone — that's fine, we keep it on
            # computer (safe delete behavior)

    def _phase_sync_moves(self):
        """Phase 3: Detect moves on computer, propagate to phone.

        Also reports files deleted from the computer.
        """
        logging.info("=== Phase 3: Detecting computer-side moves ===")

        moves_to_apply = []

        for relpath, info in list(self.state.files.items()):
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
                # File deleted from computer — report it
                self.deleted_files.append(
                    (relpath, info.get("category", "unknown")))
                self.stats["local_deletions"] += 1
                # Keep the state entry so we don't re-download from phone;
                # the file is intentionally gone from computer.
                # Mark it so we know.
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
                # Check this isn't already queued
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
            if self.adb.move_safe(old_phone, new_phone, file_hash):
                self.state.files[relpath]["phone_path"] = new_phone
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

        Args:
            target_hash: SHA256 hash to search for.
            previous_path: The last known full path of the file (used to
                determine search priority). If None, searches all of
                data_dir with no priority.
        """
        if not self.data_dir.exists():
            return None

        searched = set()  # track searched dirs to avoid re-walking

        def _check_file(fpath: Path) -> bool:
            try:
                return file_sha256(str(fpath)) == target_hash
            except (OSError, IOError):
                return False

        def _scan_dir_only(directory: Path) -> Optional[Path]:
            """Check only immediate files in a directory (non-recursive)."""
            if not directory.is_dir() or str(directory) in searched:
                return None
            searched.add(str(directory))
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
                dirs[:] = [d for d in dirs if not d.startswith(".")
                           and str(Path(root) / d) not in searched]
                if str(root) not in searched:
                    searched.add(str(root))
                    for fname in files:
                        fpath = Path(root) / fname
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

            print(f"  → These files are preserved on the phone. To remove "
                  f"from state, run 'phonesync reindex'.")

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


def cmd_reindex(args):
    """Rebuild state from files on disk.

    Scans the data directory and reconciles the state file:
    - Removes state entries for files that no longer exist on disk
    - Optionally re-hashes files to update stale hashes
    - Reports what changed

    Unlike reset-state, this preserves phone_path mappings for files
    that still exist, so the next sync won't re-download everything.
    """
    cfg = load_config()
    cfg_dir = Path(cfg["config_dir"])
    data_dir = get_data_dir(cfg)

    devices = cfg.get("devices", {})
    if not devices:
        print("No devices registered. Nothing to reindex.")
        return

    target_device = args.device
    rehash = args.rehash

    for serial, dev_info in devices.items():
        name = dev_info["name"]
        if target_device and target_device not in (serial, name):
            continue

        state_path = cfg_dir / f"state-{name}.json"
        if not state_path.exists():
            print(f"No state file for {name}, skipping.")
            continue

        print(f"\nReindexing {name}...")

        state = DeviceState(serial, name, cfg)
        original_count = len(state.files)

        removed = 0
        updated = 0
        kept = 0

        for relpath in list(state.files.keys()):
            computer_path = data_dir / relpath
            if not computer_path.exists():
                del state.files[relpath]
                removed += 1
            elif rehash:
                new_hash = file_sha256(str(computer_path))
                if new_hash != state.files[relpath].get("hash"):
                    state.files[relpath]["hash"] = new_hash
                    state.files[relpath]["size"] = computer_path.stat().st_size
                    updated += 1
                else:
                    kept += 1
            else:
                kept += 1

        state.save()

        print(f"  Original entries: {original_count}")
        print(f"  Kept:             {kept}")
        if updated:
            print(f"  Re-hashed:        {updated}")
        if removed:
            print(f"  Removed (gone):   {removed}")
        print(f"  Final entries:    {len(state.files)}")


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

    # reindex
    p_reindex = subparsers.add_parser(
        "reindex",
        help="Rebuild state from files on disk (removes stale entries)")
    p_reindex.add_argument(
        "-d", "--device", help="Device serial or name (default: all)")
    p_reindex.add_argument(
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
    elif args.command == "reindex":
        cmd_reindex(args)
    elif args.command == "reset-state":
        cmd_reset_state(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
