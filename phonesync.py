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
import builtins
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
        "recursive_scan": True,   # Scan subdirectories on phone
        "preserve_phone_subdirs": True,  # Maintain subdirectory structure from phone
        "verify_pulls": True,     # Hash-verify each pull against the phone
        "use_library_index": True,  # Cache library hashes; complete partial moves
        "read_only": True,        # Safe default: no phone writes. Move
                                  # propagation requires --apply-phone-moves
                                  # (alias --allow-phone-writes).
        "check_free_space": True,  # Abort before pulling if the disk would fill
        "free_space_margin_bytes": 104857600,  # 100 MB headroom over estimate
        # What to do when a phone edit would overwrite a computer file that
        # was ALSO edited locally since the last sync: "ask" (prompt when
        # interactive, otherwise keep local), "never" (always keep local),
        # or "always" (always take the phone version). Per-file overrides are
        # stored in each file's state entry as "overwrite_policy".
        "overwrite_policy": "ask",
        "exclude_dirs": DEFAULT_EXCLUDE_DIRS,
        "exclude_files": DEFAULT_EXCLUDE_FILES,
        "followlinks": "Hardcoded_True",
        # Reserved for future use — NOT read by the current code. The tool
        # never deletes from the phone, never propagates computer deletes,
        # and always resolves move conflicts in favor of the computer. See
        # the "Reserved keys" section of the README.
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


def known_devices_path(cfg: dict) -> Path:
    return Path(cfg["config_dir"]) / "known-devices.json"


def load_known_devices(cfg: dict) -> dict:
    """Load the registry of devices the user has approved for syncing.

    Returns {serial: {"name": str, "approved_at": iso}}. A device must be
    approved before its first (non-dry-run) sync — this both protects
    against accidentally ingesting a huge library unprompted and scopes any
    future auto-sync (#17) to devices the user has explicitly OK'd.
    """
    path = known_devices_path(cfg)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and "devices" in data:
            return data["devices"]
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_known_devices(cfg: dict, devices: dict):
    _atomic_json_write(known_devices_path(cfg),
                       {"version": 1, "devices": devices})


def is_device_known(cfg: dict, serial: str) -> bool:
    return serial in load_known_devices(cfg)


def approve_device(cfg: dict, serial: str, name: str = "") -> None:
    """Add a device to the approved registry (idempotent)."""
    devices = load_known_devices(cfg)
    if serial not in devices:
        devices[serial] = {
            "name": name,
            "approved_at": datetime.now().isoformat(),
        }
        save_known_devices(cfg, devices)


def forget_device(cfg: dict, serial: str) -> bool:
    """Remove a device from the approved registry. Returns True if removed."""
    devices = load_known_devices(cfg)
    if serial in devices:
        del devices[serial]
        save_known_devices(cfg, devices)
        return True
    return False


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
        # Unresolved issues from the most recent run, persisted so `recover`
        # can list them after the run ends (they're otherwise only summary
        # counters). Cleared at the start of move detection each run.
        self.conflicts = []        # [{relpath?, old_phone_path, candidates,
                                   #   reason, detected_at}]
        self.partial_moves = []    # [{relpath, old_phone, new_phone, reason,
                                   #   detected_at}]
        self._load()

    def _load(self):
        if self.state_path.exists():
            with open(self.state_path) as f:
                data = json.load(f)
            self.files = data.get("files", {})
            self.last_sync = data.get("last_sync")
            self.conflicts = data.get("conflicts", [])
            self.partial_moves = data.get("partial_moves", [])

    def clear_run_issues(self):
        """Reset conflict/partial-move records at the start of a run so they
        reflect only the latest run, not accumulate across runs."""
        self.conflicts = []
        self.partial_moves = []

    def record_conflict(self, old_phone_path: str, candidates: list,
                        reason: str = "ambiguous_move_target",
                        relpath: str = ""):
        self.conflicts.append({
            "relpath": relpath,
            "old_phone_path": old_phone_path,
            "candidates": list(candidates),
            "reason": reason,
            "detected_at": datetime.now().isoformat(),
        })

    def record_partial_move(self, relpath: str, old_phone: str,
                            new_phone: str,
                            reason: str = "source_not_removed"):
        self.partial_moves.append({
            "relpath": relpath,
            "old_phone": old_phone,
            "new_phone": new_phone,
            "reason": reason,
            "detected_at": datetime.now().isoformat(),
        })

    def save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        # Back up the PREVIOUS state file before overwriting it, so a bad
        # write or logic bug can be rolled back. Never let a backup failure
        # block the actual save.
        try:
            self._backup_state_file()
        except Exception as e:
            logging.warning(f"State backup failed (continuing): {e}")
        data = {
            "device_serial": self.device_serial,
            "device_name": self.device_name,
            "last_sync": datetime.now().isoformat(),
            "files": self.files,
            "conflicts": self.conflicts,
            "partial_moves": self.partial_moves,
        }
        _atomic_json_write(self.state_path, data)

    def _backup_state_file(self, keep: int = 10):
        """Copy the current on-disk state file into a timestamped backup.

        Keeps the most recent `keep` backups for this device, pruning
        older ones. No-op if there's no existing state file yet.
        """
        if not self.state_path.exists():
            return
        backup_dir = self.state_path.parent / "state-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        stem = self.state_path.stem  # e.g. "state-phone-a"
        backup_path = backup_dir / f"{stem}.{stamp}.json"
        shutil.copy2(str(self.state_path), str(backup_path))

        # Prune old backups for THIS device only
        backups = sorted(
            backup_dir.glob(f"{stem}.*.json"),
            key=lambda p: p.name)
        excess = len(backups) - keep
        for old in backups[:max(0, excess)]:
            try:
                old.unlink()
            except OSError:
                pass

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
        # Use printf with \0 field AND record separators for fully safe
        # filename handling. Output is a flat NUL-separated stream of
        # fields: size\0 mtime\0 filepath\0 size\0 mtime\0 filepath\0 ...
        # \0 is the only byte illegal in filenames on both Linux and
        # Android, so neither a field nor a record separator can ever
        # appear inside a value. (A newline record terminator would break
        # on filenames that legally contain '\n'.)
        prune_clauses = []
        for ed in exclude_dirs:
            prune_clauses.append(f'-name {q(ed)} -prune')

        # Use sh -c with {} + for batching (faster than \;)
        stat_script = (
            'for f; do '
            's=$(stat -c %s "$f") && '
            'm=$(stat -c %Y "$f") && '
            'printf "%s\\0%s\\0%s\\0" "$s" "$m" "$f"; '
            'done'
        )

        if prune_clauses:
            prune_expr = " -o ".join(prune_clauses)
            find_cmd = (
                f'find {q(remote_dir)} -maxdepth {max_depth} '
                f'\\( {prune_expr} \\) -o '
                f'-type f -exec sh -c {q(stat_script)} _ {{}} +'
            )
        else:
            find_cmd = (
                f'find {q(remote_dir)} -maxdepth {max_depth} '
                f'-type f -exec sh -c {q(stat_script)} _ {{}} +'
            )

        output = self.shell(find_cmd, check=False, timeout=300)
        files = []

        # Parse the flat NUL-separated field stream in groups of three.
        # A trailing empty token (after the final \0) is ignored.
        tokens = output.split("\0")
        # Drop a trailing empty element from the final record separator.
        if tokens and tokens[-1] == "":
            tokens.pop()

        for i in range(0, len(tokens) - 2, 3):
            size_s = tokens[i]
            mtime_s = tokens[i + 1]
            filepath = tokens[i + 2]
            try:
                size = int(size_s)
                mtime = int(mtime_s)
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

        # Distinguish a genuinely empty directory from a failed scan. The
        # directory existed (the [ -d ] check above said EXISTS), so if we
        # parsed zero files we probe the device. An unreachable device means
        # the empty result is a transport failure, not an empty folder —
        # raise so the caller can abort rather than silently treating the
        # directory as having no files.
        if not files:
            if not self.is_reachable():
                raise ADBError(
                    f"Scan of {remote_dir} returned no files and the device "
                    f"is no longer reachable — aborting to avoid acting on a "
                    f"partial/empty snapshot.")

        return files

    def list_files(self, remote_dir: str) -> list[dict]:
        """Non-recursive listing (backward compat wrapper)."""
        return self.list_files_recursive(remote_dir, max_depth=1)

    def is_reachable(self) -> bool:
        """Return True if the device responds to a trivial shell command.

        Used to distinguish a genuinely empty/missing directory from a
        dropped connection (USB unplugged, daemon dead, device offline).
        """
        try:
            out = self.shell("echo __PS_OK__", check=True, timeout=15)
            return "__PS_OK__" in out
        except ADBError:
            return False

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
# Library content index
# ---------------------------------------------------------------------------

class LibraryIndex:
    """An on-disk content index of the data directory (the photo library).

    Maps content hashes to the relpaths that hold that content, so the sync
    engine can answer "is this content already in my library?" without
    re-hashing the whole tree every run.

    The library is SHARED across all devices, so the index is shared (one
    cache file, not per-device).

    A persistent cache keyed by (relpath -> {size, mtime, hash}) lets us
    re-hash only files that are new or whose size/mtime changed. The cache
    is advisory: a wrong cached hash can never cause data loss because the
    engine still verifies content on the phone side before skipping a pull.
    """

    def __init__(self, data_dir: Path, cfg: dict):
        self.data_dir = Path(data_dir)
        self.cfg = cfg
        self.cache_path = Path(cfg["config_dir"]) / "library-index.json"
        # relpath -> {"size", "mtime", "hash"}
        self._cache: dict = {}
        # hash -> set(relpath)
        self._by_hash: dict = {}
        # sizes present on disk before this run (cheap pre-filter)
        self._prerun_sizes: set = set()
        # hashes present on disk before this run's ingest (frozen in build())
        self._prerun_hashes: set = set()
        self._loaded = False

    def _load_cache(self):
        if self.cache_path.exists():
            try:
                with open(self.cache_path) as f:
                    data = json.load(f)
                if isinstance(data, dict) and "files" in data:
                    self._cache = data["files"]
            except (json.JSONDecodeError, OSError):
                # Corrupt cache is non-fatal: rebuild from scratch.
                self._cache = {}

    def _save_cache(self):
        data = {"version": 1, "files": self._cache}
        try:
            _atomic_json_write(self.cache_path, data)
        except OSError as e:
            logging.warning(f"Could not save library index cache: {e}")

    def build(self):
        """Scan the library, hashing only new/changed files (per cache).

        Populates the in-memory hash->relpaths map. Safe to call once per
        run before the phases.
        """
        self._load_cache()
        new_cache = {}
        self._by_hash = {}

        if self.data_dir.exists():
            for root, dirs, files in os.walk(self.data_dir):
                # Skip hidden dirs (e.g. .stversions, .git) for speed/safety
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fname in files:
                    fpath = Path(root) / fname
                    try:
                        st = fpath.stat()
                    except OSError:
                        continue
                    relpath = str(fpath.relative_to(self.data_dir))
                    size = st.st_size
                    mtime = int(st.st_mtime)

                    cached = self._cache.get(relpath)
                    if (cached and cached.get("size") == size
                            and cached.get("mtime") == mtime
                            and cached.get("hash")):
                        h = cached["hash"]
                    else:
                        try:
                            h = file_sha256(str(fpath))
                        except OSError:
                            continue
                    new_cache[relpath] = {
                        "size": size, "mtime": mtime, "hash": h}
                    self._by_hash.setdefault(h, set()).add(relpath)

        self._cache = new_cache
        self._loaded = True
        # Freeze the set of hashes present BEFORE this run's ingest begins.
        # The move-completion check uses this so a file pulled earlier in the
        # SAME run can't be mistaken for a pre-existing library copy.
        self._prerun_hashes = set(self._by_hash.keys())
        self._prerun_sizes = {
            v["size"] for v in self._cache.values() if "size" in v}
        self._save_cache()

    def contains_hash(self, file_hash: str) -> bool:
        return file_hash in self._by_hash and bool(self._by_hash[file_hash])

    def contains_hash_prerun(self, file_hash: str) -> bool:
        """True if this hash existed on disk before the run's ingest began."""
        return file_hash in self._prerun_hashes

    def maybe_prerun_size(self, size: int) -> bool:
        """Cheap pre-filter: could any pre-run file have this size? If not,
        the content definitely isn't in the library and we can skip hashing
        the phone file."""
        return size in self._prerun_sizes

    def paths_for_hash(self, file_hash: str) -> list:
        return sorted(self._by_hash.get(file_hash, set()))

    def add(self, relpath: str, file_hash: str, size: int, mtime: int):
        """Register a newly-written file in the in-memory index + cache."""
        self._by_hash.setdefault(file_hash, set()).add(relpath)
        self._cache[relpath] = {
            "size": size, "mtime": mtime, "hash": file_hash}

    def remove_relpath(self, relpath: str):
        """Drop a relpath from the index (e.g. after it's moved/deleted)."""
        info = self._cache.pop(relpath, None)
        if info and info.get("hash") in self._by_hash:
            self._by_hash[info["hash"]].discard(relpath)


# ---------------------------------------------------------------------------
# Core Sync Engine
# ---------------------------------------------------------------------------

class PlanRecorder:
    """Collects structured plan records describing what a run did or (in
    dry-run) would do, so the log isn't the only review surface for
    automation. Records are appended by the phases and can be rendered as a
    human-readable plan or emitted as JSON (TODO item D).

    Record kinds: "ingest", "phone_move", "deletion", "conflict",
    "partial_move". Each carries a "kind" plus kind-specific fields.
    """

    def __init__(self, device_name: str, dry_run: bool):
        self.device_name = device_name
        self.dry_run = dry_run
        self.records = []

    def add(self, kind: str, **fields):
        rec = {"kind": kind}
        rec.update(fields)
        self.records.append(rec)

    def of_kind(self, kind: str):
        return [r for r in self.records if r["kind"] == kind]

    def to_dict(self):
        counts = {}
        for r in self.records:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        return {
            "device": self.device_name,
            "dry_run": self.dry_run,
            "counts": counts,
            "records": self.records,
        }

    def render_text(self, details=None):
        """Human-readable plan. `details` optionally filters to certain kinds
        (e.g. {"phone_move"}) for `--details moves`."""
        lines = []
        prefix = "[DRY RUN] " if self.dry_run else ""
        lines.append(f"{prefix}Plan for {self.device_name}:")
        order = ["ingest", "phone_move", "partial_move", "deletion",
                 "conflict"]
        labels = {
            "ingest": "Ingest (phone -> computer)",
            "phone_move": "Phone-side moves",
            "partial_move": "Partial moves (file left at both paths)",
            "deletion": "Tombstones (deleted on computer)",
            "conflict": "Move conflicts (not guessed)",
        }
        for kind in order:
            if details and kind not in details:
                continue
            recs = self.of_kind(kind)
            if not recs:
                continue
            lines.append(f"\n  {labels[kind]}: {len(recs)}")
            for r in recs:
                lines.append("    " + self._render_record(kind, r))
        if len(lines) == 1:
            lines.append("  (nothing to do)")
        return "\n".join(lines)

    @staticmethod
    def _render_record(kind, r):
        if kind == "ingest":
            return f"{r.get('phone_path')} -> {r.get('computer_relpath')}"
        if kind == "phone_move":
            flags = []
            if not r.get("source_exists", True):
                flags.append("source MISSING")
            if r.get("dest_exists"):
                flags.append("dest EXISTS")
            if r.get("would_remove_old_phone_path"):
                flags.append("would remove old path")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            return (f"{r.get('phone_old')} -> {r.get('phone_new')} "
                    f"({r.get('reason', '')}){suffix}")
        if kind == "partial_move":
            return (f"{r.get('phone_old')} -> {r.get('phone_new')} "
                    f"(relpath: {r.get('computer_relpath')})")
        if kind == "deletion":
            return f"{r.get('computer_relpath')} ({r.get('category', '')})"
        if kind == "conflict":
            cands = ", ".join(c.get("path", "?")
                              for c in r.get("candidates", []))
            return (f"{r.get('phone_old')} ({r.get('reason', '')}); "
                    f"candidates: {cands}")
        return str(r)


class SyncEngine:
    def __init__(self, cfg: dict, device_serial: str, dry_run: bool = False,
                 adb_cls=None):
        self.cfg = cfg
        self.data_dir = get_data_dir(cfg)
        self.dry_run = dry_run
        self.device_serial = device_serial
        self.adb_cls = adb_cls or ADB

        self.device_name = self._resolve_device_name()
        self.adb = self.adb_cls(device_serial)
        self.state = DeviceState(device_serial, self.device_name, cfg)
        self.plan = PlanRecorder(self.device_name, dry_run)

        self.stats = {
            "files_copied": 0,
            "files_skipped": 0,
            "files_updated": 0,     # re-pulled due to mtime change
            "moves_synced": 0,
            "phone_moves_detected": 0,
            "move_conflicts": 0,    # ambiguous move targets, not guessed
            "local_deletions": 0,   # files missing from computer
            "errors": 0,
            "pull_verify_failures": 0,  # pulls that failed hash verification
            "partial_moves": 0,     # dest written but source not removed
            "move_completions": 0,  # partial-move dest recognized, not re-pulled
            "phone_writes_suppressed": 0,  # phone moves skipped in read-only mode
            "overwrites_applied": 0,   # local file overwritten by phone edit
            "overwrites_kept_local": 0,  # phone edit declined, local file kept
            "adopted": 0,            # adopt-existing: mapped to on-disk copy
            "bytes_copied": 0,
        }
        self.discovered_subdirs = []
        self.deleted_files = []  # [(relpath, category)] for deletion report
        self._scan_failed = False
        self._aborted_no_space = False
        self._aborted_unconfirmed = False
        # Shared on-disk content index of the library (built in run()).
        self.library = LibraryIndex(self.data_dir, cfg)
        # Relpaths whose phone_path was updated by phone-side move detection
        # in the current run. Phase 3 must not immediately "repair" those
        # back to the computer-derived desired phone path.
        self._phone_moved_relpaths = set()

    def _resolve_device_name(self) -> str:
        """Return this device's name. For an already-known device that's just
        a lookup. For a NEW device, compute a unique name; persist the device
        into config ONLY when not a dry run — a dry run must change nothing
        on disk (TODO item K). Name resolution itself has no write side
        effect; registration is an explicit, guarded step.
        """
        devices_cfg = self.cfg.get("devices", {})
        if self.device_serial in devices_cfg:
            return devices_cfg[self.device_serial]["name"]

        existing_names = {d["name"] for d in devices_cfg.values()}
        model = self.adb_cls(self.device_serial).get_model()
        name = re.sub(r'[^a-zA-Z0-9]', '-', model).lower().strip('-')
        if not name:
            name = "phone"

        base_name = name
        counter = 1
        while name in existing_names:
            counter += 1
            name = f"{base_name}-{counter}"

        if self.dry_run:
            # Don't touch config.json on a dry run. Report what WOULD happen.
            logging.info(
                f"[DRY RUN] Would register new device: {name} ({model}) "
                f"[{self.device_serial}]")
            return name

        self._register_device(name, model)
        return name

    def _register_device(self, name: str, model: str):
        """Persist a new device into config.json. Real-run only."""
        devices_cfg = self.cfg.get("devices", {})
        devices_cfg[self.device_serial] = {
            "name": name,
            "model": model,
            "sources": DEFAULT_PHONE_SOURCES,
        }
        self.cfg["devices"] = devices_cfg
        save_config(self.cfg)
        logging.info(
            f"Registered new device: {name} ({model}) "
            f"[{self.device_serial}]")


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

        # Scan the phone ONCE — all phases share this snapshot. If the scan
        # C: For automation, reject an unapproved first-time device BEFORE
        # scanning it. Scanning enumerates the whole phone (privacy /
        # performance), and an unattended run must not even do that for a
        # device it will refuse anyway. The interactive prompt path stays
        # AFTER the scan (below), because the confirmation shows a pull
        # estimate that needs the scan. Dry-run skips the gate entirely.
        if (not self.dry_run and self._is_first_time_device()
                and not self._is_interactive()):
            self._aborted_unconfirmed = True
            logging.error(
                f"Device '{self.device_name}' ({self.device_serial}) is not "
                f"approved for syncing, and there is no terminal to confirm.")
            logging.error(
                f"  Refusing before scan. Approve it first with:  "
                f"phonesync devices --approve {self.device_serial}")
            return False

        # fails (device unreachable, transport error), abort the ENTIRE run
        # before any phase mutates state. Acting on a partial/empty snapshot
        # could mis-tombstone or mis-move files we simply couldn't see.
        try:
            self.phone_scan = self._scan_phone()
        except ADBError as e:
            self.stats["errors"] += 1
            logging.error(
                f"ABORTING sync for {self.device_name}: phone scan failed.")
            logging.error(f"  {e}")
            logging.error(
                "  No files were changed and state was not saved. "
                "Reconnect the device and try again.")
            self._scan_failed = True
            return False

        # First-time device confirmation (#5): a brand-new device must be
        # explicitly approved before we pull anything. Interactive runs
        # prompt; unattended runs refuse an unapproved device (which also
        # scopes the future auto-sync service to known devices, #17).
        # Skipped in dry-run, which writes nothing.
        if not self.dry_run and not self._check_first_time_device():
            self._aborted_unconfirmed = True
            return False

        # Build the shared library content index once, before the phases.
        # Lets ingest skip pulling content that's already in the library
        # (first run against an existing library, and partial-move dups).
        if self.cfg.get("use_library_index", True):
            logging.info("=== Indexing library ===")
            self.library.build()
            logging.info(
                f"    Indexed {len(self.library._cache)} library files")

        # Reset last run's conflict/partial-move records BEFORE detection so
        # what we persist reflects only THIS run. Must precede phase 1, which
        # is where conflicts are recorded. (Real runs only — dry-run never
        # saves, so leaving prior records in memory is harmless there.)
        if not self.dry_run:
            self.state.clear_run_issues()

        # Phase 1: Detect phone-side moves (must happen before ingest
        # so moved files aren't re-ingested as new)
        self._phase_detect_phone_moves()

        # Free-space pre-flight (#4): estimate the bytes phase 2 may pull and
        # refuse to start a large copy that won't fit, rather than failing
        # partway with files scattered. Skipped in dry-run.
        if not self.dry_run and self.cfg.get("check_free_space", True):
            if not self._check_free_space():
                # Abort before pulling: no state saved, nothing copied.
                self._aborted_no_space = True
                return False

        # Phase 2: Ingest genuinely new files
        self._phase_ingest()

        # Phase 3: Detect computer-side moves/deletes, propagate to phone
        self._phase_sync_moves()

        if not self.dry_run:
            self.state.save()
            # Persist the library cache including files this run pulled, so
            # the next run doesn't have to re-hash them. (build() saved the
            # pre-ingest snapshot; this captures additions from ingest.)
            if self.cfg.get("use_library_index", True):
                self.library._save_cache()

        self._print_summary()
        return True

    def _estimate_pull_bytes(self) -> int:
        """Upper-bound estimate of bytes phase 2 may pull this run.

        Sums the sizes of every scanned, relevant file whose phone_path is
        NOT already tracked for this device and is NOT tombstoned. This
        over-counts slightly (e.g. content the library would recognize as a
        move completion), which is the safe direction for a space check.
        """
        tracked_paths = {
            info["phone_path"]
            for info in self.state.files.values()
            if info.get("device_name") == self.device_name
            and not info.get("deleted_from_computer")
        }
        total = 0
        for category, dir_scans in self.phone_scan.items():
            for phone_dir, files in dir_scans:
                for finfo in files:
                    if not self._is_relevant_file(finfo["name"], category):
                        continue
                    if finfo["path"] in tracked_paths:
                        continue
                    total += finfo.get("size", 0)
        return total

    def _check_free_space(self) -> bool:
        """Return True if it's safe to proceed; False to abort the run.

        Compares the estimated pull size (plus a safety margin) against free
        space on the data filesystem. Aborts loudly if it won't fit.
        """
        try:
            estimate = self._estimate_pull_bytes()
        except Exception as e:
            # Never let estimation failure block a sync; just skip the check.
            logging.debug(f"Free-space estimate failed, skipping check: {e}")
            return True

        if estimate <= 0:
            return True

        margin = int(self.cfg.get("free_space_margin_bytes", 100 * 1024 * 1024))
        needed = estimate + margin
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(str(self.data_dir)).free
        except OSError as e:
            logging.debug(f"disk_usage failed, skipping check: {e}")
            return True

        if free < needed:
            logging.error(
                f"NOT ENOUGH FREE SPACE to sync {self.device_name}.")
            logging.error(
                f"  Estimated to copy: {_human_size(estimate)} "
                f"(+ {_human_size(margin)} margin = {_human_size(needed)})")
            logging.error(
                f"  Free on {self.data_dir}: {_human_size(free)}")
            logging.error(
                "  Aborting before copying anything. Free up space or move "
                "the data directory, then sync again.")
            self.stats["errors"] += 1
            return False

        logging.info(
            f"  Free-space check OK: need ~{_human_size(needed)}, "
            f"have {_human_size(free)}")
        return True

    def _estimate_pull_count(self) -> int:
        """Number of files phase 2 may pull (same filter as the byte
        estimate). Used in the first-sync confirmation message."""
        tracked_paths = {
            info["phone_path"]
            for info in self.state.files.values()
            if info.get("device_name") == self.device_name
            and not info.get("deleted_from_computer")
        }
        count = 0
        for category, dir_scans in self.phone_scan.items():
            for phone_dir, files in dir_scans:
                for finfo in files:
                    if not self._is_relevant_file(finfo["name"], category):
                        continue
                    if finfo["path"] in tracked_paths:
                        continue
                    count += 1
        return count

    def _device_has_state(self) -> bool:
        """True if this device already has tracked files (it has synced
        before, even if it predates the approved-devices registry)."""
        return any(
            info.get("device_name") == self.device_name
            for info in self.state.files.values()
        )

    def _is_first_time_device(self) -> bool:
        """True if this device has never been approved AND has no prior sync
        state — i.e. a brand-new device that needs explicit confirmation."""
        if is_device_known(self.cfg, self.device_serial):
            return False
        if self._device_has_state():
            # Synced before this feature existed: treat as known and backfill
            # the registry so future auto-sync scoping (#17) is accurate.
            approve_device(self.cfg, self.device_serial, self.device_name)
            return False
        return True

    def _confirm_first_sync(self) -> bool:
        """Prompt to approve a brand-new device. Overridable in tests.
        Only called when interactive."""
        try:
            est_bytes = self._estimate_pull_bytes()
            est_count = self._estimate_pull_count()
        except Exception:
            est_bytes, est_count = 0, 0
        print(f"\n  ⚠ First-time sync for device '{self.device_name}' "
              f"(serial {self.device_serial}).")
        print(f"    About to pull up to {est_count} file(s) "
              f"({_human_size(est_bytes)}) into {self.data_dir}.")
        print("    This device has not been synced before.")
        try:
            answer = input("    Proceed and remember this device? "
                           "[y/N]: ").strip().lower()
        except EOFError:
            return False
        return answer in ("y", "yes")

    def _check_first_time_device(self) -> bool:
        """Gate a first-time device behind explicit approval.

        Returns True to proceed, False to abort. Interactive: prompt, and on
        yes record the approval. Non-interactive: refuse (do NOT sync an
        unapproved device unattended) and explain how to approve.
        """
        if not self._is_first_time_device():
            return True

        if self._is_interactive():
            if self._confirm_first_sync():
                approve_device(self.cfg, self.device_serial, self.device_name)
                logging.info(
                    f"  ✓ Device '{self.device_name}' approved for syncing.")
                return True
            logging.warning(
                f"  Sync of '{self.device_name}' declined by user. "
                f"Nothing was copied.")
            return False

        # Non-interactive (cron/udev/automation): never sync an unapproved
        # device. This is also the scoping guard for the auto-sync service.
        logging.error(
            f"Device '{self.device_name}' ({self.device_serial}) is not "
            f"approved for syncing, and there is no terminal to confirm.")
        logging.error(
            f"  Skipping. Approve it first with:  "
            f"phonesync devices --approve {self.device_serial}")
        return False

    def adopt_existing(self) -> bool:
        """Adopt a pre-existing on-disk library: for each phone file whose
        CONTENT already exists somewhere in the data dir, record a state
        mapping to that existing relpath WITHOUT pulling a second copy.
        Files not already present are left untouched for a normal sync.

        Opt-in (the `adopt-existing` command), because it changes the meaning
        of duplicates (#16 keeps every copy; this deliberately does not pull a
        copy that's already on disk). Honors dry_run. Returns False on a scan
        failure (so the CLI can exit non-zero), True otherwise.
        """
        logging.info(f"=== Adopt-existing for {self.device_name} ===")
        try:
            self.phone_scan = self._scan_phone()
        except ADBError as e:
            self.stats["errors"] += 1
            logging.error(
                f"ABORTING adopt for {self.device_name}: phone scan failed.")
            logging.error(f"  {e}")
            self._scan_failed = True
            return False

        # Approval gate applies here too (this writes state for a device).
        if not self.dry_run and not self._check_first_time_device():
            self._aborted_unconfirmed = True
            return False

        logging.info("=== Indexing library ===")
        self.library.build()
        logging.info(f"    Indexed {len(self.library._cache)} library files")

        adopted = 0
        already_tracked = 0
        not_present = 0

        for category, dir_scans in self.phone_scan.items():
            for phone_dir, files in dir_scans:
                for finfo in files:
                    filename = finfo["name"]
                    phone_path = finfo["path"]
                    size = finfo["size"]
                    if not self._is_relevant_file(filename, category):
                        continue
                    # Already tracked for this device? nothing to do.
                    if self.state.find_by_phone_path(phone_path):
                        already_tracked += 1
                        continue
                    # Cheap size pre-filter before asking the phone to hash.
                    if not self.library.maybe_prerun_size(size):
                        not_present += 1
                        continue
                    phone_hash = self.adb.file_hash(phone_path)
                    if not phone_hash or not \
                            self.library.contains_hash_prerun(phone_hash):
                        not_present += 1
                        continue
                    relpath = self.library.paths_for_hash(phone_hash)[0]
                    if self.dry_run:
                        logging.info(
                            f"  [DRY RUN] Would adopt: {phone_path} -> "
                            f"{relpath}")
                        adopted += 1
                        continue
                    mtime_iso = datetime.fromtimestamp(
                        finfo["mtime_epoch"]).isoformat()
                    self.state.add_file(
                        relpath, phone_path, phone_hash, size, mtime_iso,
                        category, phone_source_dir=phone_dir)
                    logging.info(f"  Adopted: {phone_path} -> {relpath}")
                    adopted += 1

        self.stats["adopted"] = adopted
        if not self.dry_run and adopted:
            self.state.save()

        prefix = "[DRY RUN] " if self.dry_run else ""
        logging.info(
            f"{prefix}Adopt complete for {self.device_name}: "
            f"{adopted} adopted, {already_tracked} already tracked, "
            f"{not_present} not in library (left for normal sync).")
        return True

    def _is_interactive(self) -> bool:
        """True if we can prompt the user. False under cron/udev/automation
        or when stdin isn't a terminal. Overridable in tests."""
        try:
            return sys.stdin is not None and sys.stdin.isatty()
        except (AttributeError, ValueError):
            return False

    def _prompt_overwrite(self, relpath: str) -> str:
        """Ask the user what to do about overwriting a locally-edited file.

        Returns one of: "overwrite", "keep", "always", "never".
        Overridable in tests. Only called when interactive.
        """
        print(f"\n  ⚠ '{relpath}' was edited BOTH on the phone and locally "
              f"since the last sync.")
        print("    Overwriting will replace your local edits with the phone "
              "version.")
        prompt = ("    [o]verwrite once / [k]eep local once / "
                  "[a]lways overwrite this file / [n]ever overwrite "
                  "this file: ")
        mapping = {"o": "overwrite", "k": "keep",
                   "a": "always", "n": "never"}
        while True:
            try:
                choice = input(prompt).strip().lower()[:1]
            except EOFError:
                return "keep"
            if choice in mapping:
                return mapping[choice]

    def _resolve_overwrite(self, relpath: str, info: dict) -> str:
        """Decide whether a phone edit may overwrite a locally-edited file.

        Returns "overwrite" (take the phone version) or "keep" (preserve the
        local file). Honors a per-file policy stored in state, then the
        global config policy. In "ask" mode prompts only when interactive;
        non-interactively it keeps local (the safe choice — automation must
        never silently destroy local edits). "always"/"never" answers from a
        prompt are persisted to the file's state entry.
        """
        policy = info.get("overwrite_policy") \
            or self.cfg.get("overwrite_policy", "ask")

        if policy == "always":
            return "overwrite"
        if policy == "never":
            return "keep"

        # policy == "ask"
        if not self._is_interactive():
            return "keep"
        answer = self._prompt_overwrite(relpath)
        if answer == "always":
            info["overwrite_policy"] = "always"
            return "overwrite"
        if answer == "never":
            info["overwrite_policy"] = "never"
            return "keep"
        return answer  # "overwrite" or "keep" (one-time)

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

        # Fail fast if the device isn't reachable before we begin: a partial
        # or empty scan can cause later phases to mis-handle files they
        # simply couldn't see (e.g. treat them as deleted).
        if not self.adb.is_reachable():
            raise ADBError(
                f"Device {self.device_name} ({self.device_serial}) is not "
                f"reachable — aborting before any phase runs.")

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
        # Track moves detected in this run so Phase 3 does not immediately
        # undo a user-initiated phone-side move by recomputing the desired
        # phone path from the unchanged computer path.
        self._phone_moved_relpaths = set()
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
            file_hash = info["hash"]
            expected_size = info.get("size", -1)

            # Is the file still at its expected phone location?
            # It counts as "still there" only if the CONTENT at that path
            # still matches what we tracked. If a different file now
            # occupies the old path (same path, different content), our
            # tracked file has moved away and must be searched for by hash.
            if old_phone_path in self._phone_path_index:
                occupant = self._phone_path_index[old_phone_path]
                if occupant["size"] != expected_size:
                    # Size differs — a different file occupies the old
                    # path; fall through to search for ours by hash.
                    pass
                else:
                    # Size matches. Use mtime as a cheap proxy to avoid
                    # hashing unchanged files: if size AND mtime match the
                    # tracked values, the content is almost certainly the
                    # same, so it's not a move.
                    tracked_mtime = info.get("phone_mtime", "")
                    occupant_mtime = datetime.fromtimestamp(
                        occupant["mtime_epoch"]).isoformat()
                    if tracked_mtime and tracked_mtime == occupant_mtime:
                        continue
                    # mtime differs (or unknown): hash to disambiguate
                    # "same file touched" from "different file at old path".
                    occupant_hash = self.adb.file_hash(old_phone_path)
                    if occupant_hash == file_hash:
                        # Same content still at old path — not a move.
                        continue
                    # else: different content at old path — fall through
                    # and search for our file elsewhere by hash.

            # File is gone from (or displaced at) its expected phone path.
            # Search for it at a new path by size + hash. There may be more
            # than one untracked copy with identical content; in that case we
            # must NOT pick arbitrarily. Collect all candidates, then use the
            # free signals we already scanned (basename + mtime) to break the
            # tie. mv preserves both name and mtime, whereas a copy/re-export
            # usually changes at least one. If the best tier still has more
            # than one candidate, we refuse to guess and flag a conflict.
            old_basename = os.path.basename(old_phone_path)
            tracked_mtime = info.get("phone_mtime", "")

            candidates = []
            for ppath, pinfo in self._phone_path_index.items():
                if ppath == old_phone_path:
                    continue
                if ppath in tracked_phone_paths and ppath != old_phone_path:
                    continue
                if pinfo["size"] != expected_size:
                    continue
                phone_hash = self.adb.file_hash(ppath)
                if phone_hash == file_hash:
                    cand_mtime = datetime.fromtimestamp(
                        pinfo["mtime_epoch"]).isoformat()
                    candidates.append({
                        "path": ppath,
                        "name_match": os.path.basename(ppath) == old_basename,
                        "mtime_match": bool(tracked_mtime)
                        and cand_mtime == tracked_mtime,
                    })

            new_phone_path = self._choose_move_target(
                old_phone_path, candidates, relpath=relpath)

            if new_phone_path:
                logging.info(
                    f"  Phone-side move detected: "
                    f"{old_phone_path} -> {new_phone_path}")
                self.state.files[relpath]["phone_path"] = new_phone_path
                self._phone_moved_relpaths.add(relpath)
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

    def _choose_move_target(self, old_phone_path: str,
                            candidates: list[dict],
                            relpath: str = "") -> Optional[str]:
        """Pick the single best move target from same-content candidates.

        candidates: [{"path", "name_match", "mtime_match"}, ...]

        Ranking tiers (a real `mv` preserves both basename and mtime; a
        copy/re-export usually changes at least one):
            tier 0: name_match AND mtime_match   (strongest)
            tier 1: name_match XOR mtime_match
            tier 2: neither                       (weakest)

        We take the best non-empty tier. If exactly one candidate sits in
        it, that's the move. If more than one candidate is tied in the best
        tier, the evidence is genuinely ambiguous — we REFUSE to guess,
        log a conflict, and return None (leaving state pointing at the old
        path). The user can resolve it. Returning None here is the safe
        outcome: at worst the extra copies are ingested as new files
        (no data loss), rather than silently attaching the original's
        history to an arbitrary copy.
        """
        if not candidates:
            return None

        def tier(c):
            if c["name_match"] and c["mtime_match"]:
                return 0
            if c["name_match"] or c["mtime_match"]:
                return 1
            return 2

        best_tier = min(tier(c) for c in candidates)
        best = [c for c in candidates if tier(c) == best_tier]

        if len(best) == 1:
            return best[0]["path"]

        # Ambiguous: multiple indistinguishable candidates in the best tier.
        logging.warning(
            f"  Move CONFLICT for {old_phone_path}: {len(best)} untracked "
            f"copies have identical content and equally-strong evidence "
            f"(name/mtime). Not guessing which is the move; leaving state "
            f"unchanged. Candidates:")
        for c in best:
            logging.warning(
                f"    - {c['path']} "
                f"(name_match={c['name_match']}, "
                f"mtime_match={c['mtime_match']})")
        self.stats["move_conflicts"] += 1
        cand_list = [{"path": c["path"], "name_match": c["name_match"],
                      "mtime_match": c["mtime_match"]} for c in best]
        self.state.record_conflict(
            old_phone_path, cand_list,
            reason="ambiguous_move_target", relpath=relpath)
        self.plan.add(
            "conflict", computer_relpath=relpath, phone_old=old_phone_path,
            reason="ambiguous_move_target", candidates=cand_list)
        return None

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

            # Pull to a temp location ON THE DATA FILESYSTEM. The free-space
            # pre-flight checks data_dir; if temp lived under config_dir (a
            # possibly-small home/root FS) a big pull could fill the wrong
            # filesystem after the check passed. Keeping temp under data_dir
            # also makes the final move into place a same-FS rename.
            tmp_dir = self.data_dir / ".phonesync-tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = tmp_dir / filename

            if self.dry_run:
                # Compute and report the destination so the user can verify
                # where files would land. For photos we need the bytes to
                # read EXIF, so pull to a temp file, compute, then discard.
                # For downloads/recordings the destination is computable
                # without the bytes.
                action = "re-pull" if existing else "copy"
                if existing:
                    # Re-pull lands at the existing tracked location.
                    dest_display = existing
                elif category == "photos":
                    pulled = self.adb.pull(phone_path, str(tmp_path))
                    if pulled:
                        dest_dir = self._compute_photo_dest(
                            str(tmp_path), filename, phone_relpath,
                            phone_mtime_epoch=finfo["mtime_epoch"])
                        dest_path = safe_filename(
                            dest_dir, filename, self.device_name)
                        dest_display = str(
                            dest_path.relative_to(self.data_dir))
                        tmp_path.unlink(missing_ok=True)
                    else:
                        dest_display = "(could not pull to determine dest)"
                else:
                    dest_dir = self._dest_dir_for_category(category)
                    if self.cfg.get("preserve_phone_subdirs", True):
                        subdir = os.path.dirname(phone_relpath)
                        if subdir:
                            dest_dir = dest_dir / subdir
                    dest_path = safe_filename(
                        dest_dir, filename, self.device_name)
                    dest_display = str(dest_path.relative_to(self.data_dir))

                logging.info(
                    f"  [DRY RUN] Would {action}: {phone_path}")
                logging.info(
                    f"              -> {dest_display}")
                self.plan.add(
                    "ingest",
                    phone_path=phone_path,
                    computer_relpath=dest_display,
                    category=category,
                    action=action,
                    size=finfo.get("size", 0))
                self.stats["files_copied"] += 1
                continue

            # Move completion (#14): a partial phone move can leave content
            # at a NEW phone path that we don't yet track by phone_path,
            # while a tracked entry for THIS device still points at the old
            # path. On the next sync that new path looks like a brand-new
            # file and would be re-pulled, creating a duplicate. We detect
            # the one specific case where that's wrong: this untracked phone
            # file's content is already in the library AND it sits exactly
            # where a tracked same-device file is trying to move to (its
            # computed desired phone path). That's a move finishing, not a
            # new file — adopt it without re-pulling.
            #
            # This is the ONLY case where we suppress a pull. Every other
            # duplicate (same photo on two phones, the same photo in two
            # albums on one phone, etc.) is intentional and gets kept. Junk
            # is excluded by folder via exclude_dirs, not by content.
            #
            # The phone hash is only fetched when the cheap size pre-filter
            # says a same-size file exists in the library; on uncertainty we
            # fall through to a normal pull (never skip).
            if (not existing and not self.dry_run
                    and self.cfg.get("use_library_index", True)
                    and self.library.maybe_prerun_size(size)):
                phone_hash = self.adb.file_hash(phone_path)
                if phone_hash and self.library.contains_hash_prerun(
                        phone_hash):
                    completion_relpath = None
                    for lp in self.library.paths_for_hash(phone_hash):
                        e = self.state.files.get(lp)
                        if not e or e.get("device_name") != self.device_name:
                            continue
                        desired = self._compute_desired_phone_path(
                            self.data_dir / lp, e)
                        if desired == phone_path:
                            completion_relpath = lp
                            break

                    if completion_relpath is not None:
                        mtime_iso = datetime.fromtimestamp(
                            finfo["mtime_epoch"]).isoformat()
                        self.state.add_file(
                            completion_relpath, phone_path, phone_hash,
                            size, mtime_iso, category,
                            phone_source_dir=phone_dir)
                        logging.info(
                            f"  Move completed (already in library, not "
                            f"pulling): {phone_path} -> {completion_relpath}")
                        self.stats["move_completions"] += 1
                        continue

            logging.info(f"  {'Re-pulling' if existing else 'Copying'}: "
                         f"{phone_path} ({_human_size(size)})")
            if not self.adb.pull(phone_path, str(tmp_path)):
                self.stats["errors"] += 1
                continue

            local_hash = file_sha256(str(tmp_path))

            # Pull integrity check: compare the freshly-pulled bytes against
            # the phone's own hash of the same file. Catches truncated or
            # corrupted transfers (disconnect mid-pull, ADB flake) BEFORE we
            # commit anything to the library or state. On by default; can be
            # disabled for speed on large trusted batches.
            if self.cfg.get("verify_pulls", True):
                phone_hash = self.adb.file_hash(phone_path)
                if phone_hash is None:
                    logging.warning(
                        f"  Could not verify pull (no phone hash) for "
                        f"{phone_path}; skipping to be safe.")
                    tmp_path.unlink(missing_ok=True)
                    self.stats["errors"] += 1
                    self.stats["pull_verify_failures"] += 1
                    continue
                if phone_hash != local_hash:
                    logging.error(
                        f"  PULL INTEGRITY FAILURE: {phone_path}")
                    logging.error(
                        f"    phone={phone_hash[:12]}... "
                        f"local={local_hash[:12]}... — discarding, "
                        f"will retry next sync.")
                    tmp_path.unlink(missing_ok=True)
                    self.stats["errors"] += 1
                    self.stats["pull_verify_failures"] += 1
                    continue

            # If this is a re-pull of a changed file, update in place
            if existing:
                old_computer_path = self.data_dir / existing
                if old_computer_path.exists():
                    # Overwrite protection (#7): the phone copy changed, but
                    # did the LOCAL copy also change since we last synced? If
                    # the on-disk hash no longer matches the hash we recorded,
                    # the user edited the computer file, and overwriting would
                    # destroy that edit. Resolve via per-file/global policy.
                    info = self.state.files[existing]
                    stored_hash = info.get("hash")
                    local_changed = False
                    if stored_hash:
                        try:
                            current_local_hash = file_sha256(
                                str(old_computer_path))
                            local_changed = (
                                current_local_hash != stored_hash
                                and current_local_hash != local_hash)
                        except OSError:
                            local_changed = False

                    if local_changed:
                        decision = self._resolve_overwrite(existing, info)
                        if decision == "keep":
                            logging.warning(
                                f"  Keeping local edits, NOT overwriting: "
                                f"{existing}")
                            tmp_path.unlink(missing_ok=True)
                            # Acknowledge we've seen this phone version so we
                            # don't re-evaluate the SAME version every sync,
                            # but leave the local file and its (locally
                            # edited) hash untouched.
                            info["phone_mtime"] = datetime.fromtimestamp(
                                finfo["mtime_epoch"]).isoformat()
                            self.stats["overwrites_kept_local"] += 1
                            continue
                        else:
                            self.stats["overwrites_applied"] += 1

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

            # Duplicates are always kept. A file whose content matches
            # another file (same photo in two albums, the same photo on two
            # phones, an app-state backup, etc.) is a deliberate copy from
            # the user's point of view — this is a one-way ingest tool, so
            # every duplicate already existed on a phone on purpose. Junk is
            # excluded by folder (exclude_dirs), never by content identity.

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
            # Keep the live index current (the pre-run snapshot is frozen,
            # so a file pulled this run won't be mistaken for a pre-existing
            # library copy by a later move-completion check).
            try:
                self.library.add(
                    relpath, local_hash, size,
                    int(dest_path.stat().st_mtime))
            except OSError:
                pass

            self.stats["files_copied"] += 1
            self.stats["bytes_copied"] += size

        # Cleanup tmp
        tmp_dir = self.data_dir / ".phonesync-tmp"
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
                self.plan.add(
                    "deletion", computer_relpath=relpath,
                    category=info.get("category", "unknown"))

        # Also recompute desired phone paths for files that haven't moved
        # on the computer but whose phone path might be stale (e.g. from
        # a previous failed move)
        for relpath, info in list(self.state.files.items()):
            if relpath in self._phone_moved_relpaths:
                continue
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

        # Execute phone-side moves with collision safety.
        # In read-only mode (or dry-run) we never write to the phone: we
        # report what would move but leave the phone and state untouched.
        read_only = self.cfg.get("read_only", False)
        for old_phone, new_phone, relpath, file_hash in moves_to_apply:
            if self.dry_run or read_only:
                tag = "[DRY RUN]" if self.dry_run else "[READ-ONLY]"
                logging.info(
                    f"  {tag} Would move on phone: "
                    f"{old_phone} -> {new_phone}")
                # Structured plan record: query the phone (read-only) so the
                # plan shows whether the move would actually be safe.
                src_exists = self.adb.file_exists(old_phone)
                dst_exists = self.adb.file_exists(new_phone)
                self.plan.add(
                    "phone_move",
                    computer_relpath=relpath,
                    phone_old=old_phone,
                    phone_new=new_phone,
                    reason="local_folder_organization_changed",
                    source_exists=src_exists,
                    dest_exists=dst_exists,
                    would_remove_old_phone_path=(src_exists and not
                                                 dst_exists),
                    suppressed_read_only=(read_only and not self.dry_run))
                if read_only and not self.dry_run:
                    self.stats["phone_writes_suppressed"] += 1
                continue

            # Check that source still exists on phone
            if not self.adb.file_exists(old_phone):
                logging.warning(
                    f"  Phone source gone, skipping move: {old_phone}")
                continue

            logging.info(f"  Moving on phone: {old_phone} -> {new_phone}")
            result = self.adb.move_safe(old_phone, new_phone, file_hash)
            if result["ok"] and result["source_deleted"]:
                # Move fully completed: destination has the content AND the
                # old source is gone. Safe to advance state.
                self.state.files[relpath]["phone_path"] = new_phone
                if result["action"] == "already_there":
                    logging.info(
                        f"    (destination already existed, source removed)")
            elif result["ok"] and not result["source_deleted"]:
                # PARTIAL move: the destination now has the content, but the
                # old source could not be removed from the phone. The file
                # physically exists at BOTH paths. Do NOT advance phone_path
                # to the new location — that would orphan the source and it
                # would likely be re-ingested as a duplicate next sync. Leave
                # state pointing at the still-existing old path and surface
                # the problem.
                logging.error(
                    f"  PARTIAL MOVE: destination written but source "
                    f"could not be removed: {old_phone}. State left "
                    f"pointing at the old path; resolve on the phone "
                    f"(the file now exists at both {old_phone} and "
                    f"{new_phone}).")
                self.stats["errors"] += 1
                self.stats["partial_moves"] += 1
                self.state.record_partial_move(
                    relpath, old_phone, new_phone,
                    reason="source_not_removed")
                self.plan.add(
                    "partial_move", computer_relpath=relpath,
                    phone_old=old_phone, phone_new=new_phone,
                    reason="source_not_removed")
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
        # The summary is human-facing diagnostic output, so it goes to
        # stderr alongside progress (stdout is reserved for any future
        # machine-readable output). Shadow print() locally to avoid changing
        # every line below.
        def print(*a, **k):
            k.setdefault("file", sys.stderr)
            k.setdefault("flush", True)
            builtins.print(*a, **k)

        s = self.stats
        prefix = "[DRY RUN] " if self.dry_run else ""
        print(f"\n{prefix}Sync complete for {self.device_name}:")
        print(f"  Files copied:     {s['files_copied']}")
        if s["move_completions"]:
            print(f"  Moves completed:  {s['move_completions']} "
                  f"(already in library, not re-pulled)")
        if s["files_updated"]:
            print(f"  Files updated:    {s['files_updated']} "
                  f"(re-pulled, mtime changed)")
        if s["overwrites_kept_local"]:
            print(f"  ⚠ Local kept:     {s['overwrites_kept_local']} "
                  f"file(s) edited on BOTH sides; local edits preserved, "
                  f"phone version not applied")
        if s["overwrites_applied"]:
            print(f"  Local overwritten: {s['overwrites_applied']} "
                  f"file(s) had local edits replaced by the phone version")
        print(f"  Files skipped:    {s['files_skipped']} (already synced)")
        if s["phone_moves_detected"]:
            print(f"  Phone moves:      {s['phone_moves_detected']}")
        if s["move_conflicts"]:
            print(f"  ⚠ Move conflicts: {s['move_conflicts']} "
                  f"(ambiguous identical copies; not guessed — see log)")
        print(f"  Moves synced:     {s['moves_synced']}")
        if s["phone_writes_suppressed"]:
            print(f"  Read-only:        {s['phone_writes_suppressed']} "
                  f"phone move(s) suppressed (no phone writes)")
        print(f"  Errors:           {s['errors']}")
        if s["pull_verify_failures"]:
            print(f"  ⚠ Pull verify fails: {s['pull_verify_failures']} "
                  f"(corrupt/truncated transfers, will retry next sync)")
        if s["partial_moves"]:
            print(f"  ⚠ Partial moves:  {s['partial_moves']} "
                  f"(file at both old and new phone path; resolve manually)")
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

def resolve_read_only(config_read_only: bool, apply_phone_moves: bool,
                      read_only_flag: bool) -> bool:
    """Resolve the effective read_only for a run from the config value and
    the two CLI flags. --apply-phone-moves enables writes; --read-only forces
    them off and wins if both are passed (the safe direction)."""
    effective = config_read_only
    if apply_phone_moves:
        effective = False
    if read_only_flag:
        effective = True
    return effective


def cmd_adopt_existing(args):
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

        any_failed = False
        for serial in serials:
            engine = SyncEngine(cfg, serial, dry_run=args.dry_run)
            ok = engine.adopt_existing()
            if ok is False:
                any_failed = True
    finally:
        lock.release()

    if any_failed:
        sys.exit(2)


def cmd_sync(args):
    cfg = load_config()
    # Phone writes (move propagation) are OFF by default. --apply-phone-moves
    # (alias --allow-phone-writes) enables them for this run; --read-only
    # forces them off and takes precedence if both are somehow passed.
    cfg["read_only"] = resolve_read_only(
        cfg.get("read_only", True),
        getattr(args, "apply_phone_moves", False),
        getattr(args, "read_only", False))
    # --overwrite-policy overrides the config policy for this run.
    if getattr(args, "overwrite_policy", None):
        cfg["overwrite_policy"] = args.overwrite_policy
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

        any_failed = False
        engines = []
        for serial in serials:
            engine = SyncEngine(cfg, serial, dry_run=args.dry_run)
            ok = engine.run()
            if ok is False:
                any_failed = True
            engines.append(engine)
            print()
    finally:
        lock.release()

    _emit_plan(engines, args)

    if any_failed:
        sys.exit(2)


def _emit_plan(engines, args):
    """Render the structured plan (--plan / --plan-json / --details) from the
    engines' PlanRecorders. Human plan -> stderr (diagnostic); JSON -> the
    requested file, or stdout for '-' (machine-readable output belongs on
    stdout)."""
    want_text = getattr(args, "plan", False)
    json_target = getattr(args, "plan_json", None)
    if not want_text and not json_target:
        return

    details = None
    d = getattr(args, "details", None)
    if d and d != "all":
        details = {
            "moves": "phone_move", "ingest": "ingest",
            "deletions": "deletion", "conflicts": "conflict",
        }.get(d)
        details = {details} if details else None

    if want_text:
        for engine in engines:
            print(engine.plan.render_text(details=details), file=sys.stderr,
                  flush=True)

    if json_target:
        payload = {
            "dry_run": bool(getattr(args, "dry_run", False)),
            "devices": [engine.plan.to_dict() for engine in engines],
        }
        text = json.dumps(payload, indent=2)
        if json_target == "-":
            builtins.print(text, flush=True)   # machine output -> stdout
        else:
            try:
                with open(json_target, "w") as f:
                    f.write(text)
                print(f"Wrote plan JSON to {json_target}", file=sys.stderr)
            except OSError as e:
                print(f"Could not write plan JSON to {json_target}: {e}",
                      file=sys.stderr)


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
    cfg = load_config()

    # Manage the approved-devices registry (#5/#17) without syncing.
    if getattr(args, "approve", None):
        approve_device(cfg, args.approve)
        print(f"✓ Approved device for syncing: {args.approve}")
        return
    if getattr(args, "forget", None):
        if forget_device(cfg, args.forget):
            print(f"✓ Removed device from approved list: {args.forget}")
        else:
            print(f"Device not in approved list: {args.forget}")
        return

    known = load_known_devices(cfg)
    devices = list_connected_devices()
    if not devices:
        print("No ADB devices connected.")
        print("Make sure USB debugging is enabled and the phone is plugged in.")
        if known:
            print("\nApproved devices (not currently connected):")
            for serial, info in known.items():
                print(f"  {serial}  {info.get('name', '')}")
        return

    for d in devices:
        serial = d["serial"]
        model = d["model"]
        status = "approved" if serial in known else "NOT approved"
        print(f"\n  {serial}  {model}  [{status}]")
        if serial not in known:
            print(f"    First sync will ask for confirmation. Pre-approve "
                  f"with: phonesync devices --approve {serial}")

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


def run_doctor(adb, cfg, want_phone_writes=False, device_name=""):
    """Probe the on-device shell capabilities phonesync relies on, using the
    SAME commands the sync uses, against a temp dir on the phone. Returns a
    list of result dicts: {name, ok, critical, detail}.

    Android userland is toybox/busybox, which varies across builds; a missing
    or differently-behaving find/stat/printf/sha256sum can cause silent
    empty scans, skipped pulls, or failed move propagation. This catches that
    before trusting a real library. (TODO item H, absorbs old #15.)

    `adb` is an ADB-like instance (real or a test double). Probes that would
    write to the phone (mkdir/cp/mv/rm) are marked critical only when
    want_phone_writes is True (move propagation enabled); read/scan probes are
    always critical.
    """
    results = []

    def record(name, ok, critical, detail=""):
        results.append({"name": name, "ok": bool(ok), "critical": critical,
                        "detail": detail})

    q = adb._q
    # A temp working dir on the phone. Under /sdcard so it's writable and on
    # the same volume the tool reads from.
    probe_dir = "/sdcard/.phonesync-doctor"
    # A filename containing a NEWLINE, to exercise the NUL-separated scanner
    # format (#11): if find/printf mangle this, the scan is unsafe.
    tricky_name = "doctor probe\nline2.txt"
    tricky_path = f"{probe_dir}/{tricky_name}"

    # 1. adb reachable / shell works at all.
    try:
        echoed = adb.shell("echo PHONESYNC_OK", check=False).strip()
        reachable = "PHONESYNC_OK" in echoed
        record("adb shell reachable", reachable, True,
               "" if reachable else f"got: {echoed!r}")
    except Exception as e:
        record("adb shell reachable", False, True, str(e))
        reachable = False
    if not reachable:
        # A shell that can't even echo is unusable; the rest would be noise.
        return results

    # 2. mkdir -p (needed to create dirs for pulls/moves; also our probe dir).
    made = False
    try:
        adb.shell(f"mkdir -p {q(probe_dir)}", check=False)
        made = "EXISTS" in adb.shell(
            f"[ -d {q(probe_dir)} ] && echo EXISTS || echo MISSING",
            check=False)
        record("mkdir -p", made, want_phone_writes,
               "" if made else "could not create probe dir")
    except Exception as e:
        record("mkdir -p", False, want_phone_writes, str(e))

    if not made:
        # Can't run file-level probes without the probe dir.
        return results

    # Create the tricky-named test file (write via the phone shell, using a
    # redirect; if this fails we still try the rest).
    try:
        adb.shell(f"printf 'hello' > {q(tricky_path)}", check=False)
    except Exception:
        pass
    file_there = "EXISTS" in adb.shell(
        f"[ -e {q(tricky_path)} ] && echo EXISTS || echo MISSING",
        check=False)

    # 3. stat -c %s / %Y (size + mtime; the scan depends on both).
    try:
        size = adb.shell(f'stat -c %s {q(tricky_path)}', check=False).strip()
        mtime = adb.shell(f'stat -c %Y {q(tricky_path)}', check=False).strip()
        ok = size.isdigit() and mtime.isdigit()
        record("stat -c %s / %Y", ok, True,
               "" if ok else f"size={size!r} mtime={mtime!r}")
    except Exception as e:
        record("stat -c %s / %Y", False, True, str(e))

    # 4. The NUL-separated recursive scanner on a newline-containing filename.
    #    This is the real list_files_recursive path (#11) — the strongest
    #    single check that find + printf + stat cooperate safely.
    try:
        listed = adb.list_files_recursive(probe_dir)
        names = [e["name"] for e in listed]
        ok = tricky_name in names
        record("recursive scan (NUL-safe, newline filename)", ok, True,
               "" if ok else f"scanned names={names!r}")
    except Exception as e:
        record("recursive scan (NUL-safe, newline filename)", False, True,
               str(e))

    # 5. sha256sum (pull verification + move/library hashing depend on it).
    try:
        h = adb.file_hash(tricky_path)
        ok = bool(h) and len(h) == 64
        record("sha256sum", ok, True,
               "" if ok else f"got hash={h!r}")
    except Exception as e:
        record("sha256sum", False, True, str(e))

    # 6. cp (only used when phone writes are enabled, for move propagation).
    try:
        cp_dst = f"{probe_dir}/doctor-cp-copy.txt"
        adb.shell(f"cp {q(tricky_path)} {q(cp_dst)}", check=False)
        ok = "EXISTS" in adb.shell(
            f"[ -e {q(cp_dst)} ] && echo EXISTS || echo MISSING",
            check=False)
        record("cp (phone writes)", ok, want_phone_writes,
               "" if ok else "copy did not appear")
    except Exception as e:
        record("cp (phone writes)", False, want_phone_writes, str(e))

    # Cleanup: best-effort remove the probe dir.
    try:
        adb.shell(f"rm -rf {q(probe_dir)}", check=False)
    except Exception:
        pass

    return results


def cmd_doctor(args):
    cfg = load_config()
    want_writes = not cfg.get("read_only", True)

    if args.device:
        serials = [args.device]
    else:
        devices = list_connected_devices()
        if not devices:
            print("No ADB devices connected.")
            print("Connect a phone with USB debugging enabled.")
            sys.exit(1)
        serials = [d["serial"] for d in devices]

    overall_ok = True
    for serial in serials:
        engine_adb = ADB(serial)
        try:
            name = engine_adb.get_model()
        except Exception:
            name = serial
        print(f"\nphonesync doctor — {name} ({serial})")
        if want_writes:
            print("  (phone writes ENABLED — checking write capabilities "
                  "as critical)")
        else:
            print("  (phone writes off — write checks are informational)")

        results = run_doctor(engine_adb, cfg, want_phone_writes=want_writes,
                             device_name=name)
        for r in results:
            mark = "✓" if r["ok"] else ("✗" if r["critical"] else "•")
            line = f"  {mark} {r['name']}"
            if r["detail"]:
                line += f"  — {r['detail']}"
            print(line)
            if not r["ok"] and r["critical"]:
                overall_ok = False

        crit_fail = [r for r in results
                     if not r["ok"] and r["critical"]]
        if crit_fail:
            print(f"  RESULT: {len(crit_fail)} critical check(s) failed — "
                  f"this device may not sync reliably.")
        else:
            print("  RESULT: all critical checks passed.")

    if not overall_ok:
        sys.exit(2)


def _state_paths_for(cfg, target_device):
    """Yield (name, state_path) for the target device or all devices."""
    cfg_dir = Path(cfg["config_dir"])
    devices = cfg.get("devices", {})
    for serial, info in devices.items():
        name = info.get("name", serial)
        if target_device and target_device not in (serial, name):
            continue
        yield name, cfg_dir / f"state-{name}.json"


def cmd_recover(args):
    """Recovery / introspection for unattended operation.

    Read-only by default (listing). --restore-backup and --rebuild-index are
    the only state-changing actions, and each requires an explicit flag.
    """
    cfg = load_config()
    cfg_dir = Path(cfg["config_dir"])

    did_something = False

    # --- list tombstones ----------------------------------------------------
    if args.list_tombstones:
        did_something = True
        any_found = False
        for name, sp in _state_paths_for(cfg, args.device):
            if not sp.exists():
                continue
            try:
                with open(sp) as f:
                    files = json.load(f).get("files", {})
            except (OSError, json.JSONDecodeError):
                print(f"  {name}: could not read state file")
                continue
            tombstoned = [rp for rp, i in files.items()
                          if i.get("deleted_from_computer")]
            if tombstoned:
                any_found = True
                print(f"\n{name}: {len(tombstoned)} tombstoned "
                      f"(deleted on computer, won't re-download):")
                for rp in sorted(tombstoned):
                    print(f"  {rp}")
        if not any_found:
            print("No tombstoned files.")
        print("\nTo clear tombstones (allow re-download): "
              "phonesync prune-state --clear-tombstones")

    # --- list backups -------------------------------------------------------
    if args.list_backups:
        did_something = True
        backup_dir = cfg_dir / "state-backups"
        backups = sorted(backup_dir.glob("state-*.json")) \
            if backup_dir.exists() else []
        if args.device:
            backups = [b for b in backups
                       if b.name.startswith(f"state-{args.device}.")
                       or f"-{args.device}." in b.name]
        if not backups:
            print("No state backups found.")
        else:
            print(f"State backups in {backup_dir}:")
            for b in backups:
                print(f"  {b.name}")
            print("\nRestore with: phonesync recover --restore-backup "
                  "<filename>")

    # --- restore a backup ---------------------------------------------------
    if args.restore_backup:
        did_something = True
        backup_dir = cfg_dir / "state-backups"
        src = backup_dir / args.restore_backup
        if not src.exists():
            # Allow passing just a timestamp/partial — match uniquely.
            matches = sorted(backup_dir.glob(f"*{args.restore_backup}*")) \
                if backup_dir.exists() else []
            if len(matches) == 1:
                src = matches[0]
            elif len(matches) > 1:
                print(f"Ambiguous: {args.restore_backup} matches "
                      f"{len(matches)} backups. Be more specific:")
                for m in matches:
                    print(f"  {m.name}")
                sys.exit(1)
            else:
                print(f"Backup not found: {args.restore_backup}")
                print("List available backups: phonesync recover "
                      "--list-backups")
                sys.exit(1)
        # The backup file is state-<name>.<stamp>.json -> restore to
        # state-<name>.json. Strip the trailing .<stamp>.json (two suffixes).
        stem = src.name
        # remove ".json"
        stem = stem[:-len(".json")] if stem.endswith(".json") else stem
        # remove ".<stamp>"
        base = stem.rsplit(".", 1)[0]
        dest = cfg_dir / f"{base}.json"
        # Back up the current state before overwriting it, so restore is
        # itself reversible.
        if dest.exists():
            safety = cfg_dir / "state-backups" / \
                f"{base}.pre-restore-" \
                f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
            safety.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(dest), str(safety))
            print(f"Saved current state to {safety.name} before restoring.")
        shutil.copy2(str(src), str(dest))
        print(f"Restored {dest.name} from {src.name}")

    # --- list conflicts -----------------------------------------------------
    if getattr(args, "list_conflicts", False):
        did_something = True
        any_found = False
        for name, sp in _state_paths_for(cfg, args.device):
            if not sp.exists():
                continue
            try:
                with open(sp) as f:
                    conflicts = json.load(f).get("conflicts", [])
            except (OSError, json.JSONDecodeError):
                continue
            if conflicts:
                any_found = True
                print(f"\n{name}: {len(conflicts)} unresolved move "
                      f"conflict(s) from the last run:")
                for c in conflicts:
                    print(f"  {c.get('old_phone_path', '?')} "
                          f"({c.get('reason', '')})")
                    for cand in c.get("candidates", []):
                        print(f"      candidate: {cand.get('path', '?')} "
                              f"(name_match={cand.get('name_match')}, "
                              f"mtime_match={cand.get('mtime_match')})")
        if not any_found:
            print("No unresolved move conflicts.")

    # --- list partial moves -------------------------------------------------
    if getattr(args, "list_partial_moves", False):
        did_something = True
        any_found = False
        for name, sp in _state_paths_for(cfg, args.device):
            if not sp.exists():
                continue
            try:
                with open(sp) as f:
                    partials = json.load(f).get("partial_moves", [])
            except (OSError, json.JSONDecodeError):
                continue
            if partials:
                any_found = True
                print(f"\n{name}: {len(partials)} partial move(s) from the "
                      f"last run (file exists at BOTH paths on the phone):")
                for p in partials:
                    print(f"  {p.get('old_phone', '?')} -> "
                          f"{p.get('new_phone', '?')}  "
                          f"(relpath: {p.get('relpath', '?')})")
        if not any_found:
            print("No partial moves.")

    # --- rebuild library index ---------------------------------------------
    if args.rebuild_index:
        did_something = True
        cache = cfg_dir / "library-index.json"
        if cache.exists():
            try:
                cache.unlink()
            except OSError as e:
                print(f"Could not remove old index: {e}")
        idx = LibraryIndex(get_data_dir(cfg), cfg)
        idx.build()
        print(f"Rebuilt library index: {len(idx._cache)} files indexed "
              f"({cache}).")

    if not did_something:
        print("Nothing to do. Available actions:")
        print("  --list-tombstones      files deleted on computer "
              "(won't re-download)")
        print("  --list-conflicts       unresolved move conflicts (last run)")
        print("  --list-partial-moves   files left at both phone paths "
              "(last run)")
        print("  --list-backups         available state backups")
        print("  --restore-backup NAME  restore a state backup")
        print("  --rebuild-index        rebuild the library content index")


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

class _FlushingStreamHandler(logging.StreamHandler):
    """StreamHandler that flushes after every record, so progress shows up
    live even when stderr is block-buffered (piped/redirected)."""
    def emit(self, record):
        super().emit(record)
        try:
            self.flush()
        except Exception:
            pass


def setup_output(verbose: bool = False):
    """Configure live, line-buffered output following Unix conventions:
    diagnostics and progress go to STDERR (so stdout stays clean for any
    machine-readable output), and both streams are line-buffered so a long
    sync streams progress instead of dumping everything at exit.

    Without this, when stdout/stderr are piped or redirected, Python
    block-buffers them and nothing appears until the process ends — which
    makes the tool useless for watching progress (TODO item L).
    """
    # Force line buffering so each print()/log line is flushed immediately,
    # even when not attached to a TTY. reconfigure() exists on the standard
    # text streams in Python 3.7+; guard in case stdout/stderr were replaced.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass

    level = logging.DEBUG if verbose else logging.INFO
    handler = _FlushingStreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    root.handlers.clear()        # replace any prior basicConfig handler
    root.addHandler(handler)
    root.setLevel(level)


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
    p_sync.add_argument(
        "--read-only", action="store_true",
        help="Never write to the phone (the default). Skips move "
             "propagation.")
    p_sync.add_argument(
        "--apply-phone-moves", "--allow-phone-writes",
        dest="apply_phone_moves", action="store_true",
        help="Enable phone writes for this run: propagate computer-side "
             "moves back to the phone (copy-verify-delete of the old path). "
             "Off by default.")
    p_sync.add_argument(
        "--overwrite-policy", choices=["ask", "never", "always"],
        help="When a file was edited on BOTH phone and computer: ask "
             "(default; keeps local if non-interactive), never (always keep "
             "local edits), or always (always take the phone version)")
    p_sync.add_argument(
        "--plan", action="store_true",
        help="Print a structured, reviewable plan of what the run did/would "
             "do (most useful with --dry-run)")
    p_sync.add_argument(
        "--plan-json", metavar="FILE",
        help="Write the structured plan as JSON to FILE (use '-' for stdout)")
    p_sync.add_argument(
        "--details", choices=["moves", "ingest", "deletions", "conflicts",
                              "all"],
        help="With --plan, show only records of this kind (default: all)")

    # status
    subparsers.add_parser("status", help="Show sync status")

    # doctor
    p_doctor = subparsers.add_parser(
        "doctor",
        help="Probe on-device shell capabilities phonesync relies on "
             "(find/stat/printf/sha256sum/cp) before trusting a real sync")
    p_doctor.add_argument(
        "-d", "--device", help="ADB device serial (default: all connected)")

    # adopt-existing
    p_adopt = subparsers.add_parser(
        "adopt-existing",
        help="Map phone files whose content is ALREADY in the library to the "
             "existing copy, without pulling duplicates (for a pre-existing "
             "main library)")
    p_adopt.add_argument(
        "-d", "--device", help="ADB device serial (default: all connected)")
    p_adopt.add_argument(
        "-n", "--dry-run", action="store_true",
        help="Show what would be adopted without writing state")

    # devices
    p_devices = subparsers.add_parser(
        "devices", help="List connected ADB devices and storage volumes")
    p_devices.add_argument(
        "--approve", metavar="SERIAL",
        help="Approve a device for syncing (skips the first-sync prompt; "
             "required for unattended/auto-sync)")
    p_devices.add_argument(
        "--forget", metavar="SERIAL",
        help="Remove a device from the approved list")

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

    # recover
    p_recover = subparsers.add_parser(
        "recover",
        help="Recovery/introspection: list tombstones/backups, restore a "
             "state backup, rebuild the library index")
    p_recover.add_argument(
        "-d", "--device", help="Device serial or name (default: all)")
    p_recover.add_argument(
        "--list-tombstones", action="store_true",
        help="List files deleted on the computer (won't be re-downloaded)")
    p_recover.add_argument(
        "--list-conflicts", action="store_true",
        help="List unresolved move conflicts from the last run")
    p_recover.add_argument(
        "--list-partial-moves", action="store_true",
        help="List partial moves from the last run (file at both phone paths)")
    p_recover.add_argument(
        "--list-backups", action="store_true",
        help="List available state backups")
    p_recover.add_argument(
        "--restore-backup", metavar="NAME",
        help="Restore a state backup (full filename, or a unique timestamp "
             "substring); the current state is backed up first")
    p_recover.add_argument(
        "--rebuild-index", action="store_true",
        help="Rebuild the library content index from scratch")

    args = parser.parse_args()

    setup_output(verbose=args.verbose)

    if args.command == "sync":
        cmd_sync(args)
    elif args.command == "adopt-existing":
        cmd_adopt_existing(args)
    elif args.command == "doctor":
        cmd_doctor(args)
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
    elif args.command == "recover":
        cmd_recover(args)
    elif args.command == "reset-state":
        cmd_reset_state(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
