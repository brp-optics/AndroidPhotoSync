#!/usr/bin/env python3
"""
phonesync — Sync photos, downloads, and recordings from Android phones via ADB.

Supports two phones merging into one photo library, with:
  - Date-based photo organization (from EXIF or file timestamp)
  - Collision-safe file naming
  - Move/sort tracking (bidirectional)
  - Safe delete behavior (phone deletes don't remove from computer)

Usage:
  phonesync sync [--device SERIAL] [--dry-run]
  phonesync status
  phonesync devices
  phonesync config [--init]
  phonesync reset-state [--device SERIAL]
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants & Defaults
# ---------------------------------------------------------------------------

APP_NAME = "phonesync"
DEFAULT_BASE_DIR = Path.home() / "PhoneSync"
CONFIG_DIR_NAME = ".phonesync"
CONFIG_FILE = "config.yaml"
STATE_FILE_TEMPLATE = "state-{device}.json"
HASH_CHUNK_SIZE = 8192

# Default phone source directories (internal storage paths)
DEFAULT_PHONE_SOURCES = {
    "photos": [
        "/sdcard/DCIM/Camera",
        "/sdcard/DCIM/Screenshots",
        "/sdcard/Pictures",
        "/sdcard/DCIM/Camera",
        "/sdcard/DCIM/Screenshots",
        "/internalstorage/Pictures",
        "/internalstorage/DCIM/Messages",
        "/internalstorage/DCIM/Camera",
        "/internalstorage/DCIM/Screenshots",
        "/internalstorage/Movies/Messages",
    ],
    "downloads": [
        "/sdcard/Download",
        "/internalstorage/Download",
        "/internalstorage/Pictures/Messages",
        "/sdcard/Movies/Messages",
    ],
    "recordings": [
        "/sdcard/Recordings",
        "/sdcard/DCIM/Recorder",  # Some phones put voice recordings here
        "/internalstorage/Recordings",
        "/internalstorage/Music/KakaoTalk",
        "/internalstorage/Music/Recordings",
    ],
    "music": [
        "/internalstorage/Ringtones",
    ],
}

MY_US_PHONE_SOURCES={
    "photos": [
        "/internalstorage/DCIM/Camera",
        "/internalstorage/DCIM/Screenshots",
        "/internalstorage/DCIM/Video Editor",
        "/internalstorage/DCIM/Wardmap",
        "/internalstorage/DCIM/메시지",
        "/internalstorage/DCIM/야",
        "/internalstorage/DCIM/혜영",
        "/internalstorage/Movies/KakaoTalk",
        "/internalstorage/Movies/Messages",
        "/internalstorage/Movies/메시지",
        "/internalstorage/Pictures",
        "/internalstorage/Pictures/KakaoTalk",
        "/internalstorage/Pictures/Skype",
        "/internalstorage/Pictures/hellotalk",
        "/sdcard/BlackPhone/DCIM/Camera",
        "/sdcard/BlackPhone/DCIM/Screenshots",
        "/sdcard/BlackPhone/DCIM/서류",
        "/sdcard/BlackPhone/Pictures/KakaoTalk"
        "/sdcard/BlackPhone/Pictures/hellotalk",
        "/sdcard/BlackPhone/Pictures/메시지",
        "/sdcard/Phone2/DCIM/Camera",
        "/sdcard/Phone2/Screenshots",
        "/sdcard/Pictures/KakaoTalk",
        "/sdcard/DCIM/Camera",
        "/sdcard/DCIM/Hyeyoung_for_홈배경화면",
        "/sdcard/DCIM/Screenshots",
        "/sdcard/KakaoTalk",
        "/sdcard/Pictures",
    ],
    "downloads": [
        "/sdcard/Download",
        "/sdcard/Download/KakaoTalk",
        "/sdcard/BlackPhone/Download",
        "/internalstorage/Download",
        "/internalstorage/Pictures/메시지",
        "/internalstorage/Pictures/Messages",
        "/sdcard/Movies/메시지",
        "/sdcard/Movies/Messages",
        "/sdcard/Phone2/Podcasts",
        "/sdcard/Pictures/KakaoTalk",
        
    ],
    "recordings": [
        "/sdcard/Recordings",
        "/sdcard/DCIM/Recorder",  # Some phones put voice recordings here
        "/sdcard/BlackPhone/Music/QuickVoiceRecorder",
        "/sdcard/BlackPhone/Music/Recordings",
        "/internalstorage/Recordings",
        "/internalstorage/Music/KakaoTalk",
        "/internalstorage/Music/Recordings",
        "/sdcard/Phone2/Call",
        "/sdcard/Recordings/Voice Recorder",
    ],
    "music": [
        "/internalstorage/Ringtones",
        "/sdcard/Music",
    ],
    
}

# File extensions we care about per category
PHOTO_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".mp4", ".mov", ".avi", ".mkv", ".3gp",  # videos from camera too
    ".dng", ".raw", ".cr2", ".nef",  # raw photos
}
DOWNLOAD_EXTENSIONS = None  # accept everything
RECORDING_EXTENSIONS = {
    ".m4a", ".mp3", ".wav", ".ogg", ".aac", ".3gp", ".amr", ".flac",
}

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def default_config():
    return {
        "base_dir": str(DEFAULT_BASE_DIR),
        "devices": {},  # serial -> {name: "phone-a", sources: {...}}
        "photo_date_folders": True,  # organize photos into YYYY/MM
        "delete_from_phone_after_sync": False,
        "propagate_computer_deletes_to_phone": False,
        "conflict_resolution": "prefer_computer",  # for move conflicts
    }


def config_dir(base_dir: Path) -> Path:
    return Path.home() / CONFIG_DIR_NAME

### There might be an issue here with where I want to store the binary file.
### I don't want the config dir in the base dir - I want that in my home directory.
### I don't want the 
def load_config(base_dir: Optional[Path] = None) -> dict:
    """Load config, trying base_dir argument, then default location."""
    if base_dir is None:
        base_dir = DEFAULT_BASE_DIR
    cfg_path = config_dir(base_dir) / "config.json"
    if cfg_path.exists():
        with open(cfg_path) as f:
            return json.load(f)
    return default_config()

def save_config(cfg: dict):
    base_dir = Path(cfg["base_dir"])
    cd = config_dir(base_dir)
    cd.mkdir(parents=True, exist_ok=True)
    cfg_path = cd / "config.json"
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
    logging.info(f"Config saved to {cfg_path}")


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
            },
            ...
        }
    }
    """

    def __init__(self, device_serial: str, device_name: str, base_dir: Path):
        self.device_serial = device_serial
        self.device_name = device_name
        self.base_dir = base_dir
        self.state_path = config_dir(base_dir) / f"state-{device_name}.json"
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
        with open(self.state_path, "w") as f:
            json.dump(data, f, indent=2)

    def add_file(self, computer_relpath: str, phone_path: str,
                 file_hash: str, size: int, phone_mtime: str, category: str):
        self.files[computer_relpath] = {
            "phone_path": phone_path,
            "hash": file_hash,
            "size": size,
            "phone_mtime": phone_mtime,
            "synced_at": datetime.now().isoformat(),
            "category": category,
        }

    def find_by_hash(self, file_hash: str) -> Optional[str]:
        """Find a computer relpath by hash."""
        for relpath, info in self.files.items():
            if info["hash"] == file_hash:
                return relpath
        return None

    def find_by_phone_path(self, phone_path: str) -> Optional[str]:
        """Find a computer relpath by original phone path."""
        for relpath, info in self.files.items():
            if info["phone_path"] == phone_path:
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

    def _run(self, args: list[str], check=True, capture=True) -> subprocess.CompletedProcess:
        cmd = ["adb", "-s", self.serial] + args
        logging.debug(f"Running: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                capture_output=capture,
                text=True,
                timeout=120,
            )
            if check and result.returncode != 0:
                raise ADBError(f"ADB command failed: {' '.join(cmd)}\n{result.stderr}")
            return result
        except subprocess.TimeoutExpired:
            raise ADBError(f"ADB command timed out: {' '.join(cmd)}")

    def shell(self, cmd: str, check=True) -> str:
        """Run a shell command on the device."""
        result = self._run(["shell", cmd], check=check)
        return result.stdout

    def list_files(self, remote_dir: str) -> list[dict]:
        """
        List files in a directory on the phone.
        Returns list of {name, size, mtime_epoch, path}.
        """
        # Use stat-based listing for reliable parsing
        # First check if directory exists
        check = self.shell(f'[ -d "{remote_dir}" ] && echo EXISTS || echo MISSING', check=False)
        if "MISSING" in check:
            return []

        # Use find + stat for reliable output
        # Format: size|mtime_epoch|filepath
        output = self.shell(
            f'find "{remote_dir}" -maxdepth 1 -type f '
            f'-exec stat -c "%s|%Y|%n" {{}} \\;',
            check=False
        )
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
                files.append({
                    "name": name,
                    "size": size,
                    "mtime_epoch": mtime,
                    "path": filepath,
                })
            except (ValueError, IndexError):
                continue
        return files

    def pull(self, remote_path: str, local_path: str) -> bool:
        """Pull a file from the phone to the computer."""
        try:
            self._run(["pull", remote_path, local_path])
            return True
        except ADBError as e:
            logging.error(f"Failed to pull {remote_path}: {e}")
            return False

    def push(self, local_path: str, remote_path: str) -> bool:
        """Push a file from the computer to the phone."""
        try:
            self._run(["push", local_path, remote_path])
            return True
        except ADBError as e:
            logging.error(f"Failed to push {remote_path}: {e}")
            return False

    def delete(self, remote_path: str) -> bool:
        """Delete a file on the phone."""
        try:
            self.shell(f'rm "{remote_path}"')
            return True
        except ADBError as e:
            logging.error(f"Failed to delete {remote_path}: {e}")
            return False

    def mkdir(self, remote_path: str) -> bool:
        """Create a directory on the phone."""
        try:
            self.shell(f'mkdir -p "{remote_path}"')
            return True
        except ADBError as e:
            logging.error(f"Failed to mkdir {remote_path}: {e}")
            return False

    def move(self, remote_src: str, remote_dst: str) -> bool:
        """Move/rename a file on the phone."""
        try:
            parent = os.path.dirname(remote_dst)
            self.shell(f'mkdir -p "{parent}"')
            self.shell(f'mv "{remote_src}" "{remote_dst}"')
            return True
        except ADBError as e:
            logging.error(f"Failed to move {remote_src} -> {remote_dst}: {e}")
            return False

    def file_hash(self, remote_path: str) -> Optional[str]:
        """Get SHA256 hash of a file on the phone."""
        try:
            output = self.shell(f'sha256sum "{remote_path}"', check=False)
            if output and " " in output:
                return output.strip().split()[0]
        except ADBError:
            pass
        return None

    def get_model(self) -> str:
        """Get the phone model name."""
        try:
            return self.shell("getprop ro.product.model", check=False).strip()
        except ADBError:
            return "unknown"


def list_connected_devices() -> list[dict]:
    """List all connected ADB devices."""
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
            # Extract model from the extended info
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
    """Compute SHA256 hash of a local file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def get_photo_date(filepath: str) -> Optional[datetime]:
    """Try to extract date from EXIF data, falling back to filename patterns."""
    # Try EXIF first
    try:
        from PIL import Image
        from PIL.ExifTags import Base as ExifBase
        img = Image.open(filepath)
        exif = img._getexif()
        if exif:
            # DateTimeOriginal (36867) or DateTime (306)
            for tag_id in (36867, 36868, 306):
                if tag_id in exif:
                    date_str = exif[tag_id]
                    return datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass

    # Try common filename patterns
    basename = os.path.basename(filepath)
    patterns = [
        r'(\d{4})(\d{2})(\d{2})[_-]',      # 20250115_...
        r'(\d{4})-(\d{2})-(\d{2})',          # 2025-01-15
        r'IMG[_-](\d{4})(\d{2})(\d{2})',     # IMG_20250115
        r'VID[_-](\d{4})(\d{2})(\d{2})',     # VID_20250115
        r'PXL[_-](\d{4})(\d{2})(\d{2})',     # PXL_20250115 (Pixel phones)
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

    return None


def safe_filename(dest_dir: Path, name: str, device_name: str = "") -> Path:
    """Generate a collision-free filename in dest_dir."""
    dest = dest_dir / name
    if not dest.exists():
        return dest

    stem = Path(name).stem
    suffix = Path(name).suffix

    # Try adding device name
    if device_name:
        candidate = dest_dir / f"{stem}_{device_name}{suffix}"
        if not candidate.exists():
            return candidate

    # Try numeric suffix
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
        self.base_dir = Path(cfg["base_dir"])
        self.dry_run = dry_run
        self.device_serial = device_serial

        # Resolve device name
        self.device_name = self._resolve_device_name()

        self.adb = ADB(device_serial)
        self.state = DeviceState(device_serial, self.device_name, self.base_dir)

        # Stats
        self.stats = {
            "files_copied": 0,
            "files_skipped": 0,
            "moves_synced": 0,
            "errors": 0,
            "bytes_copied": 0,
        }

    def _resolve_device_name(self) -> str:
        """Get or create a friendly name for this device."""
        devices_cfg = self.cfg.get("devices", {})
        if self.device_serial in devices_cfg:
            return devices_cfg[self.device_serial]["name"]

        # Auto-assign a name
        existing_names = {d["name"] for d in devices_cfg.values()}
        model = ADB(self.device_serial).get_model()

        # Use model name, sanitized
        name = re.sub(r'[^a-zA-Z0-9]', '-', model).lower().strip('-')
        if not name:
            name = "phone"

        # Deduplicate
        base_name = name
        counter = 1
        while name in existing_names:
            counter += 1
            name = f"{base_name}-{counter}"

        # Save to config
        devices_cfg[self.device_serial] = {
            "name": name,
            "model": model,
            "sources": DEFAULT_PHONE_SOURCES,
        }
        self.cfg["devices"] = devices_cfg
        save_config(self.cfg)

        logging.info(f"Registered new device: {name} ({model}) [{self.device_serial}]")
        return name

    def _get_sources(self) -> dict:
        """Get source directories for this device."""
        dev_cfg = self.cfg.get("devices", {}).get(self.device_serial, {})
        return dev_cfg.get("sources", DEFAULT_PHONE_SOURCES)

    def _dest_dir_for_category(self, category: str) -> Path:
        """Get the destination directory for a category."""
        if category == "photos":
            return self.base_dir / "photos"
        elif category == "downloads":
            return self.base_dir / "downloads" / self.device_name
        elif category == "recordings":
            return self.base_dir / "recordings" / self.device_name
        else:
            return self.base_dir / category / self.device_name

    def _compute_photo_dest(self, local_tmp: str, filename: str) -> Path:
        """Compute destination path for a photo, using date-based folders."""
        base = self.base_dir / "photos"
        if self.cfg.get("photo_date_folders", True):
            date = get_photo_date(local_tmp)
            if date:
                return base / str(date.year) / f"{date.month:02d}"
            else:
                return base / "unsorted"
        return base

    def _is_relevant_file(self, filename: str, category: str) -> bool:
        """Check if a file is relevant for a given category."""
        ext = Path(filename).suffix.lower()
        if category == "photos":
            return ext in PHOTO_EXTENSIONS
        elif category == "recordings":
            return RECORDING_EXTENSIONS is None or ext in RECORDING_EXTENSIONS
        return True  # downloads: accept everything

    def run(self):
        """Run the full sync cycle."""
        logging.info(f"{'[DRY RUN] ' if self.dry_run else ''}Starting sync for "
                     f"{self.device_name} ({self.device_serial})")

        # Phase 1: Ingest new files from phone
        self._phase_ingest()

        # Phase 2 & 3: Detect and propagate moves
        self._phase_sync_moves()

        # Save state
        if not self.dry_run:
            self.state.save()

        # Summary
        self._print_summary()

    def _phase_ingest(self):
        """Phase 1: Copy new files from phone to computer."""
        logging.info("=== Phase 1: Ingesting new files ===")
        sources = self._get_sources()

        for category, dirs in sources.items():
            for phone_dir in dirs:
                self._ingest_directory(phone_dir, category)

    def _ingest_directory(self, phone_dir: str, category: str):
        """Ingest files from a single phone directory."""
        logging.info(f"Scanning {phone_dir} ({category})...")
        files = self.adb.list_files(phone_dir)
        logging.info(f"  Found {len(files)} files")

        for finfo in files:
            filename = finfo["name"]
            phone_path = finfo["path"]
            size = finfo["size"]

            # Skip irrelevant files
            if not self._is_relevant_file(filename, category):
                continue

            # Skip if already synced (by phone path)
            existing = self.state.find_by_phone_path(phone_path)
            if existing:
                self.stats["files_skipped"] += 1
                continue

            # Pull to temp location first
            tmp_dir = self.base_dir / CONFIG_DIR_NAME / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = tmp_dir / filename

            if self.dry_run:
                logging.info(f"  [DRY RUN] Would copy: {phone_path}")
                self.stats["files_copied"] += 1
                continue

            logging.info(f"  Copying: {phone_path} ({_human_size(size)})")
            if not self.adb.pull(phone_path, str(tmp_path)):
                self.stats["errors"] += 1
                continue

            # Compute hash
            local_hash = file_sha256(str(tmp_path))

            # Check if we already have this file (by hash — maybe from other phone)
            existing_by_hash = self.state.find_by_hash(local_hash)
            if existing_by_hash:
                logging.info(f"  Skipping (duplicate by hash): {filename}")
                tmp_path.unlink(missing_ok=True)
                # Still record the phone path mapping
                info = self.state.files[existing_by_hash]
                self.state.add_file(
                    existing_by_hash, phone_path, local_hash,
                    size, datetime.fromtimestamp(finfo["mtime_epoch"]).isoformat(),
                    category
                )
                self.stats["files_skipped"] += 1
                continue

            # Determine destination
            if category == "photos":
                dest_dir = self._compute_photo_dest(str(tmp_path), filename)
            else:
                dest_dir = self._dest_dir_for_category(category)

            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = safe_filename(dest_dir, filename, self.device_name)

            # Move from tmp to final destination
            shutil.move(str(tmp_path), str(dest_path))

            # Record in state
            relpath = str(dest_path.relative_to(self.base_dir))
            mtime_iso = datetime.fromtimestamp(finfo["mtime_epoch"]).isoformat()
            self.state.add_file(relpath, phone_path, local_hash, size, mtime_iso, category)

            self.stats["files_copied"] += 1
            self.stats["bytes_copied"] += size

        # Cleanup tmp
        tmp_dir = self.base_dir / CONFIG_DIR_NAME / "tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _phase_sync_moves(self):
        """Phase 2 & 3: Detect moves on computer, propagate to phone."""
        logging.info("=== Phase 2-3: Detecting and syncing moves ===")

        moves_to_apply = []

        for relpath, info in list(self.state.files.items()):
            computer_path = self.base_dir / relpath

            if computer_path.exists():
                continue

            # File is missing from expected location — was it moved?
            file_hash = info["hash"]

            # Search for the file by hash in the base directory
            new_path = self._find_file_by_hash(file_hash, info["category"])

            if new_path:
                new_relpath = str(new_path.relative_to(self.base_dir))
                logging.info(f"  Detected move on computer: {relpath} -> {new_relpath}")

                # Update state
                old_info = self.state.files.pop(relpath)
                old_info["synced_at"] = datetime.now().isoformat()
                self.state.files[new_relpath] = old_info

                # Should we propagate this move to the phone?
                if info["category"] == "photos":
                    # For photos, we could move on phone too, but phone DCIM
                    # is flat typically — only do this if the new location
                    # maps to a subfolder we can represent on the phone
                    phone_path = info["phone_path"]
                    phone_dir = os.path.dirname(phone_path)
                    phone_filename = os.path.basename(phone_path)

                    # Check if it was moved into a subfolder on computer
                    old_parts = Path(relpath).parts
                    new_parts = Path(new_relpath).parts

                    if len(new_parts) > len(old_parts):
                        # Moved deeper — create subfolder on phone
                        extra = "/".join(new_parts[len(old_parts)-1:-1])
                        new_phone_path = f"{phone_dir}/{extra}/{phone_filename}"
                        moves_to_apply.append((phone_path, new_phone_path, new_relpath))

                self.stats["moves_synced"] += 1
            else:
                # File gone from computer — this is fine, we just note it
                # (user may have deleted from computer, which is their choice)
                logging.debug(f"  File removed from computer: {relpath}")

        # Apply phone-side moves
        for old_phone, new_phone, relpath in moves_to_apply:
            if self.dry_run:
                logging.info(f"  [DRY RUN] Would move on phone: {old_phone} -> {new_phone}")
                continue
            logging.info(f"  Moving on phone: {old_phone} -> {new_phone}")
            if self.adb.move(old_phone, new_phone):
                # Update the phone_path in state
                self.state.files[relpath]["phone_path"] = new_phone
            else:
                self.stats["errors"] += 1

    def _find_file_by_hash(self, target_hash: str, category: str) -> Optional[Path]:
        """Search for a file by hash within the appropriate category directory."""
        if category == "photos":
            search_dir = self.base_dir / "photos"
        elif category == "downloads":
            search_dir = self.base_dir / "downloads"
        elif category == "recordings":
            search_dir = self.base_dir / "recordings"
        else:
            search_dir = self.base_dir

        if not search_dir.exists():
            return None

        for root, dirs, files in os.walk(search_dir):
            # Skip the config directory
            dirs[:] = [d for d in dirs if d != CONFIG_DIR_NAME]
            for fname in files:
                fpath = Path(root) / fname
                try:
                    if file_sha256(str(fpath)) == target_hash:
                        return fpath
                except (OSError, IOError):
                    continue
        return None

    def _print_summary(self):
        """Print sync summary."""
        s = self.stats
        prefix = "[DRY RUN] " if self.dry_run else ""
        print(f"\n{prefix}Sync complete for {self.device_name}:")
        print(f"  Files copied:  {s['files_copied']}")
        print(f"  Files skipped: {s['files_skipped']} (already synced)")
        print(f"  Moves synced:  {s['moves_synced']}")
        print(f"  Errors:        {s['errors']}")
        if s["bytes_copied"] > 0:
            print(f"  Data copied:   {_human_size(s['bytes_copied'])}")


def _human_size(size_bytes: int) -> str:
    """Convert bytes to human-readable size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------

def cmd_sync(args):
    """Run sync for one or all connected devices."""
    cfg = load_config()

    if args.device:
        serials = [args.device]
    else:
        devices = list_connected_devices()
        if not devices:
            print("No ADB devices connected. Connect a phone with USB debugging enabled.")
            sys.exit(1)
        serials = [d["serial"] for d in devices]
        print(f"Found {len(devices)} device(s): {', '.join(d['model'] for d in devices)}")

    for serial in serials:
        engine = SyncEngine(cfg, serial, dry_run=args.dry_run)
        engine.run()
        print()


def cmd_status(args):
    """Show sync status."""
    cfg = load_config()
    base_dir = Path(cfg["base_dir"])

    print(f"PhoneSync Base Directory: {base_dir}")
    print(f"Config: {config_dir(base_dir) / 'config.json'}")
    print()

    # Show registered devices
    devices = cfg.get("devices", {})
    if not devices:
        print("No devices registered yet. Connect a phone and run 'phonesync sync'.")
        return

    for serial, dev_info in devices.items():
        name = dev_info["name"]
        model = dev_info.get("model", "unknown")
        state_path = config_dir(base_dir) / f"state-{name}.json"

        print(f"Device: {name} ({model})")
        print(f"  Serial: {serial}")

        if state_path.exists():
            with open(state_path) as f:
                state_data = json.load(f)
            file_count = len(state_data.get("files", {}))
            last_sync = state_data.get("last_sync", "never")
            total_size = sum(f.get("size", 0) for f in state_data.get("files", {}).values())
            print(f"  Files synced: {file_count}")
            print(f"  Total size:   {_human_size(total_size)}")
            print(f"  Last sync:    {last_sync}")
        else:
            print("  Not yet synced")
        print()

    # Show connected devices
    connected = list_connected_devices()
    if connected:
        print(f"Currently connected: {', '.join(d['model'] for d in connected)}")
    else:
        print("No devices currently connected.")


def cmd_devices(args):
    """List connected ADB devices."""
    devices = list_connected_devices()
    if not devices:
        print("No ADB devices connected.")
        print("Make sure USB debugging is enabled and the phone is plugged in.")
        return

    for d in devices:
        print(f"  {d['serial']}  {d['model']}")


def cmd_config(args):
    """Show or initialize config."""
    if args.init:
        cfg = default_config()
        if args.base_dir:
            cfg["base_dir"] = str(Path(args.base_dir).expanduser().resolve())
        save_config(cfg)
        base_dir = Path(cfg["base_dir"])
        # Create directory structure
        for d in ["photos", "downloads", "recordings"]:
            (base_dir / d).mkdir(parents=True, exist_ok=True)
        print(f"Initialized PhoneSync at {base_dir}")
        print(f"Config: {config_dir(base_dir) / 'config.json'}")
    else:
        cfg = load_config()
        print(json.dumps(cfg, indent=2))


def cmd_reset_state(args):
    """Reset state for a device (forces re-scan on next sync)."""
    cfg = load_config()
    base_dir = Path(cfg["base_dir"])

    if args.device:
        # Find device name
        devices = cfg.get("devices", {})
        if args.device in devices:
            name = devices[args.device]["name"]
        else:
            # Maybe they passed the name directly
            name = args.device
        state_path = config_dir(base_dir) / f"state-{name}.json"
        if state_path.exists():
            state_path.unlink()
            print(f"Reset state for {name}")
        else:
            print(f"No state file found for {name}")
    else:
        # Reset all
        cd = config_dir(base_dir)
        for f in cd.glob("state-*.json"):
            f.unlink()
            print(f"Reset: {f.name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="phonesync",
        description="Sync photos, downloads, and recordings from Android phones via ADB",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    subparsers = parser.add_subparsers(dest="command")

    # sync
    p_sync = subparsers.add_parser("sync", help="Sync files from connected phone(s)")
    p_sync.add_argument("-d", "--device", help="ADB device serial (default: all connected)")
    p_sync.add_argument("-n", "--dry-run", action="store_true", help="Show what would be done")

    # status
    subparsers.add_parser("status", help="Show sync status")

    # devices
    subparsers.add_parser("devices", help="List connected ADB devices")

    # config
    p_config = subparsers.add_parser("config", help="Show or initialize config")
    p_config.add_argument("--init", action="store_true", help="Initialize config and directories")
    p_config.add_argument("--base-dir", help="Base directory (default: ~/PhoneSync)")

    # reset-state
    p_reset = subparsers.add_parser("reset-state", help="Reset sync state for a device")
    p_reset.add_argument("-d", "--device", help="Device serial or name (default: all)")

    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(format=LOG_FORMAT, level=level)

    if args.command == "sync":
        cmd_sync(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "devices":
        cmd_devices(args)
    elif args.command == "config":
        cmd_config(args)
    elif args.command == "reset-state":
        cmd_reset_state(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()


