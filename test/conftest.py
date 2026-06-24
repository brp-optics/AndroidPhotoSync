"""
Test fixtures for phonesync.

Provides FakeADB (local-filesystem ADB substitute) and TestHarness
(full sync test environment with two phones and one computer).
"""
import fnmatch
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

try:
    import pytest
    HAS_PYTEST = True
except ImportError:
    HAS_PYTEST = False

# Add parent directory to path so we can import phonesync
sys.path.insert(0, str(Path(__file__).parent.parent))

import phonesync


# ---------------------------------------------------------------------------
# FakeADB — local filesystem substitute for ADB
# ---------------------------------------------------------------------------

class FakeADB:
    """Drop-in replacement for phonesync.ADB that operates on local dirs.

    Each instance is bound to a root directory that represents /sdcard
    on the phone. All paths passed to methods are treated as absolute
    paths on the "phone" and mapped to local paths under self.root.
    """

    # Class-level registry: serial -> root directory
    _registry: dict[str, Path] = {}

    @classmethod
    def register(cls, serial: str, root: Path, model: str = "FakePhone"):
        cls._registry[serial] = {"root": root, "model": model}

    @classmethod
    def reset_registry(cls):
        cls._registry.clear()

    def __init__(self, serial: str):
        self.serial = serial
        if serial not in self._registry:
            raise ValueError(f"FakeADB: unknown serial {serial}")
        info = self._registry[serial]
        self.root = info["root"]
        self.model = info["model"]

    @staticmethod
    def _q(path: str) -> str:
        return path  # no quoting needed for local ops

    def _local(self, remote_path: str) -> Path:
        """Map a phone-absolute path to a local path under self.root."""
        # Strip leading /sdcard/ or /storage/emulated/0/
        rel = remote_path
        for prefix in ("/sdcard/", "/storage/emulated/0/"):
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
                break
        if rel.startswith("/"):
            rel = rel[1:]
        return self.root / rel

    def shell(self, cmd: str, check=True, timeout=120) -> str:
        """Not fully implemented — only supports specific patterns."""
        # Support [ -d ... ] && echo EXISTS || echo MISSING
        if "EXISTS" in cmd and "MISSING" in cmd:
            import re
            m = re.search(r'\[ -[de] (.+?) \]', cmd)
            if m:
                path = m.group(1).strip("'\"")
                local = self._local(path)
                if "-d" in cmd[:20]:
                    return "EXISTS" if local.is_dir() else "MISSING"
                return "EXISTS" if local.exists() else "MISSING"
        # Support sha256sum
        if cmd.startswith("sha256sum "):
            path = cmd.split(None, 1)[1].strip("'\"")
            local = self._local(path)
            if local.exists():
                h = hashlib.sha256(local.read_bytes()).hexdigest()
                return f"{h}  {path}"
            return ""
        # Support stat -c %s / %Y
        if "stat -c" in cmd:
            import re
            m = re.search(r'stat -c (%\w+) (.+)', cmd)
            if m:
                fmt, path = m.group(1), m.group(2).strip("'\"")
                local = self._local(path)
                if local.exists():
                    st = local.stat()
                    if fmt == "%s":
                        return str(st.st_size)
                    elif fmt == "%Y":
                        return str(int(st.st_mtime))
            return ""
        # Support mkdir -p
        if cmd.startswith("mkdir -p "):
            path = cmd.split(None, 2)[2].strip("'\"")
            self._local(path).mkdir(parents=True, exist_ok=True)
            return ""
        # Support rm
        if cmd.startswith("rm ") and "-f" not in cmd:
            path = cmd.split(None, 1)[1].strip("'\"")
            local = self._local(path)
            if local.exists():
                local.unlink()
            elif check:
                raise phonesync.ADBError(f"rm: {path}: No such file")
            return ""
        if cmd.startswith("rm -f "):
            path = cmd.split(None, 2)[2].strip("'\"")
            local = self._local(path)
            local.unlink(missing_ok=True)
            return ""
        # Support cp
        if cmd.startswith("cp "):
            parts = cmd.split()
            src = parts[1].strip("'\"")
            dst = parts[2].strip("'\"")
            local_src = self._local(src)
            local_dst = self._local(dst)
            local_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(local_src), str(local_dst))
            return ""
        # Support mv
        if cmd.startswith("mv "):
            parts = cmd.split()
            src = parts[1].strip("'\"")
            dst = parts[2].strip("'\"")
            local_src = self._local(src)
            local_dst = self._local(dst)
            local_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(local_src), str(local_dst))
            return ""
        # Support getprop
        if "getprop ro.product.model" in cmd:
            return self.model
        # Support find
        if cmd.startswith("find "):
            return self._handle_find(cmd)
        # Support ls
        if cmd.startswith("ls "):
            return ""
        # Support wc -l piped from find
        if "wc -l" in cmd:
            # Extract find part before the pipe
            find_part = cmd.split("|")[0].strip()
            if find_part.startswith("find "):
                result = self._handle_find(find_part)
                count = len([l for l in result.strip().split("\n")
                             if l.strip()])
                return str(count)
            return "0"
        return ""

    def _handle_find(self, cmd: str) -> str:
        """Handle find commands on local filesystem."""
        import re
        # Extract directory
        parts = cmd.split()
        if len(parts) < 2:
            return ""
        # Find the directory (first non-option argument after 'find')
        directory = None
        for p in parts[1:]:
            if not p.startswith("-") and not p.startswith("\\"):
                directory = p.strip("'\"")
                break
        if not directory:
            return ""
        local_dir = self._local(directory)
        if not local_dir.is_dir():
            return ""

        # Check for -maxdepth
        max_depth = 999
        if "-maxdepth" in cmd:
            m = re.search(r'-maxdepth\s+(\d+)', cmd)
            if m:
                max_depth = int(m.group(1))

        # Check for -type d or -type f
        type_filter = None
        if "-type d" in cmd:
            type_filter = "d"
        elif "-type f" in cmd:
            type_filter = "f"

        # Check for -exec with stat/printf (our null-separated format)
        if "printf" in cmd and "stat" in cmd:
            # Our null-byte separated format
            lines = []
            for root, dirs, files in os.walk(str(local_dir)):
                depth = str(root).count(os.sep) - str(local_dir).count(os.sep)
                if depth >= max_depth:
                    dirs.clear()
                    continue
                if type_filter == "f":
                    for fname in files:
                        fpath = Path(root) / fname
                        # Map back to phone path
                        rel = fpath.relative_to(self.root)
                        phone_path = f"/sdcard/{rel}"
                        st = fpath.stat()
                        lines.append(
                            f"{st.st_size}\0{int(st.st_mtime)}\0{phone_path}")
            return "\n".join(lines)

        # Simple find: return paths
        results = []
        for root, dirs, files in os.walk(str(local_dir)):
            depth = str(root).count(os.sep) - str(local_dir).count(os.sep)
            if depth >= max_depth:
                dirs.clear()
                continue
            rel = Path(root).relative_to(self.root)
            phone_path = f"/sdcard/{rel}"
            if type_filter == "d":
                results.append(phone_path)
                for d in dirs:
                    if depth + 1 < max_depth:
                        d_rel = (Path(root) / d).relative_to(self.root)
                        results.append(f"/sdcard/{d_rel}")
            elif type_filter == "f":
                for fname in files:
                    f_rel = (Path(root) / fname).relative_to(self.root)
                    results.append(f"/sdcard/{f_rel}")
            else:
                results.append(phone_path)
        return "\n".join(results)

    def list_files_recursive(self, remote_dir: str,
                             exclude_dirs=None, exclude_files=None,
                             max_depth: int = 10) -> list[dict]:
        """List files recursively, matching real ADB output format."""
        if exclude_dirs is None:
            exclude_dirs = phonesync.DEFAULT_EXCLUDE_DIRS
        if exclude_files is None:
            exclude_files = phonesync.DEFAULT_EXCLUDE_FILES

        local_dir = self._local(remote_dir)
        if not local_dir.is_dir():
            return []

        files = []
        for root, dirs, filenames in os.walk(str(local_dir)):
            depth = str(root).count(os.sep) - str(local_dir).count(os.sep)
            if depth >= max_depth:
                dirs.clear()
                continue
            # Prune excluded dirs
            dirs[:] = [d for d in dirs if d not in exclude_dirs]

            for fname in filenames:
                # Check exclusion patterns
                skip = False
                for pattern in exclude_files:
                    if fnmatch.fnmatch(fname, pattern):
                        skip = True
                        break
                if skip:
                    continue

                fpath = Path(root) / fname
                st = fpath.stat()
                # Map to phone path
                rel_to_root = fpath.relative_to(self.root)
                phone_path = f"/sdcard/{rel_to_root}"
                # Compute relpath from remote_dir
                rel_to_dir = fpath.relative_to(local_dir)
                relpath = str(rel_to_dir)

                files.append({
                    "name": fname,
                    "size": st.st_size,
                    "mtime_epoch": int(st.st_mtime),
                    "path": phone_path,
                    "relpath": relpath,
                })
        return files

    def list_files(self, remote_dir: str) -> list[dict]:
        return self.list_files_recursive(remote_dir, max_depth=1)

    def pull(self, remote_path: str, local_path: str) -> bool:
        src = self._local(remote_path)
        if not src.exists():
            return False
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), local_path)
        return True

    def push(self, local_path: str, remote_path: str) -> bool:
        dst = self._local(remote_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, str(dst))
        return True

    def delete(self, remote_path: str) -> bool:
        local = self._local(remote_path)
        if local.exists():
            local.unlink()
            return True
        return False

    def mkdir(self, remote_path: str) -> bool:
        self._local(remote_path).mkdir(parents=True, exist_ok=True)
        return True

    def move(self, remote_src: str, remote_dst: str) -> bool:
        src = self._local(remote_src)
        dst = self._local(remote_dst)
        if not src.exists():
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return True

    def move_safe(self, remote_src: str, remote_dst: str,
                  expected_hash: str = None) -> dict:
        """Copy-verify-delete move, matching real ADB.move_safe interface."""
        result = {"ok": False, "action": "", "source_deleted": False}
        src = self._local(remote_src)
        dst = self._local(remote_dst)

        if dst.exists():
            if expected_hash:
                existing_hash = hashlib.sha256(
                    dst.read_bytes()).hexdigest()
                if existing_hash == expected_hash:
                    if src.exists():
                        src.unlink()
                        result["source_deleted"] = True
                    result["ok"] = True
                    result["action"] = "already_there"
                    return result
            result["action"] = "collision"
            return result

        if not src.exists():
            result["action"] = "copy_failed"
            return result

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))

        if expected_hash:
            actual = hashlib.sha256(dst.read_bytes()).hexdigest()
            if actual != expected_hash:
                dst.unlink()
                result["action"] = "hash_mismatch"
                return result

        src.unlink()
        result["ok"] = True
        result["action"] = "moved"
        result["source_deleted"] = True
        return result

    def file_exists(self, remote_path: str) -> bool:
        return self._local(remote_path).exists()

    def file_mtime(self, remote_path: str):
        local = self._local(remote_path)
        if local.exists():
            return int(local.stat().st_mtime)
        return None

    def file_hash(self, remote_path: str):
        local = self._local(remote_path)
        if local.exists():
            return hashlib.sha256(local.read_bytes()).hexdigest()
        return None

    def get_model(self) -> str:
        return self.model

    def list_storage_volumes(self) -> list[dict]:
        return [{"type": "internal", "path": "/sdcard",
                 "label": "Internal Storage"}]


# ---------------------------------------------------------------------------
# Fake list_connected_devices
# ---------------------------------------------------------------------------

_fake_connected_devices: list[dict] = []


def fake_list_connected_devices() -> list[dict]:
    return list(_fake_connected_devices)


# ---------------------------------------------------------------------------
# TestHarness
# ---------------------------------------------------------------------------

class TestHarness:
    """Full test environment with two phones and one computer.

    Sets up temp directories, config, and patches phonesync to use
    FakeADB. Provides helper methods for writing/reading/moving files.

    Usage:
        with TestHarness() as h:
            h.phone_write("a", "DCIM/Camera/IMG.jpg", b"photo data")
            h.sync("a")
            assert h.computer_exists("photos/2025/IMG.jpg")
    """

    def __init__(self):
        self.tmpdir = None
        self._original_adb = None
        self._original_list = None

    def __enter__(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="phonesync_test_"))

        # Create phone directories
        self.phone_a_dir = self.tmpdir / "phone_a"
        self.phone_b_dir = self.tmpdir / "phone_b"
        self.phone_a_dir.mkdir()
        self.phone_b_dir.mkdir()

        # Create default phone directory structures
        for phone_dir in (self.phone_a_dir, self.phone_b_dir):
            (phone_dir / "DCIM" / "Camera").mkdir(parents=True)
            (phone_dir / "DCIM" / "Screenshots").mkdir(parents=True)
            (phone_dir / "Pictures").mkdir(parents=True)
            (phone_dir / "Download").mkdir(parents=True)
            (phone_dir / "Recordings").mkdir(parents=True)

        # Create data and config directories
        self.data_dir = self.tmpdir / "PhoneSync"
        self.cfg_dir = self.tmpdir / "config"
        self.data_dir.mkdir()
        self.cfg_dir.mkdir()

        # Register fake devices
        FakeADB.reset_registry()
        FakeADB.register("SERIAL_A", self.phone_a_dir, "PhoneA")
        FakeADB.register("SERIAL_B", self.phone_b_dir, "PhoneB")

        # Set up fake connected devices
        global _fake_connected_devices
        _fake_connected_devices = [
            {"serial": "SERIAL_A", "model": "PhoneA"},
            {"serial": "SERIAL_B", "model": "PhoneB"},
        ]

        # Create config
        self.cfg = {
            "config_dir": str(self.cfg_dir),
            "data_dir": str(self.data_dir),
            "photo_date_folders": True,
            "recursive_scan": True,
            "keep_duplicates": True,
            "preserve_phone_subdirs": True,
            "delete_from_phone_after_sync": False,
            "propagate_computer_deletes_to_phone": False,
            "max_symlink_depth": 2,
            "devices": {
                "SERIAL_A": {
                    "name": "phone-a",
                    "model": "PhoneA",
                    "sources": {
                        "photos": [
                            "/sdcard/DCIM/Camera",
                            "/sdcard/DCIM/Screenshots",
                            "/sdcard/Pictures",
                        ],
                        "downloads": ["/sdcard/Download"],
                        "recordings": ["/sdcard/Recordings"],
                    },
                },
                "SERIAL_B": {
                    "name": "phone-b",
                    "model": "PhoneB",
                    "sources": {
                        "photos": [
                            "/sdcard/DCIM/Camera",
                            "/sdcard/DCIM/Screenshots",
                            "/sdcard/Pictures",
                        ],
                        "downloads": ["/sdcard/Download"],
                        "recordings": ["/sdcard/Recordings"],
                    },
                },
            },
        }
        phonesync.save_config(self.cfg)

        # Monkey-patch phonesync to use FakeADB
        self._original_adb = phonesync.ADB
        self._original_list = phonesync.list_connected_devices
        phonesync.ADB = FakeADB
        phonesync.list_connected_devices = fake_list_connected_devices

        return self

    def __exit__(self, *args):
        # Restore originals
        phonesync.ADB = self._original_adb
        phonesync.list_connected_devices = self._original_list
        FakeADB.reset_registry()

        global _fake_connected_devices
        _fake_connected_devices = []

        # Cleanup
        if self.tmpdir and self.tmpdir.exists():
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- Phone helpers ---

    def _phone_dir(self, phone: str) -> Path:
        if phone == "a":
            return self.phone_a_dir
        elif phone == "b":
            return self.phone_b_dir
        raise ValueError(f"Unknown phone: {phone}")

    def _phone_serial(self, phone: str) -> str:
        return "SERIAL_A" if phone == "a" else "SERIAL_B"

    def phone_write(self, phone: str, relpath: str, content: bytes,
                    mtime: float = None):
        """Write a file to a phone's filesystem."""
        p = self._phone_dir(phone) / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        if mtime is not None:
            os.utime(str(p), (mtime, mtime))

    def phone_read(self, phone: str, relpath: str) -> bytes:
        return (self._phone_dir(phone) / relpath).read_bytes()

    def phone_exists(self, phone: str, relpath: str) -> bool:
        return (self._phone_dir(phone) / relpath).exists()

    def phone_move(self, phone: str, src: str, dst: str):
        s = self._phone_dir(phone) / src
        d = self._phone_dir(phone) / dst
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))

    def phone_delete(self, phone: str, relpath: str):
        (self._phone_dir(phone) / relpath).unlink()

    def phone_list(self, phone: str, relpath: str = "") -> list[str]:
        """List all files under a phone path, relative to phone root."""
        base = self._phone_dir(phone) / relpath
        if not base.exists():
            return []
        result = []
        for root, dirs, files in os.walk(str(base)):
            for f in files:
                rel = str((Path(root) / f).relative_to(
                    self._phone_dir(phone)))
                result.append(rel)
        return sorted(result)

    # --- Computer helpers ---

    def computer_write(self, relpath: str, content: bytes,
                       mtime: float = None):
        """Write a file to the computer's data directory."""
        p = self.data_dir / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        if mtime is not None:
            os.utime(str(p), (mtime, mtime))

    def computer_read(self, relpath: str) -> bytes:
        return (self.data_dir / relpath).read_bytes()

    def computer_exists(self, relpath: str) -> bool:
        return (self.data_dir / relpath).exists()

    def computer_move(self, src: str, dst: str):
        s = self.data_dir / src
        d = self.data_dir / dst
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))

    def computer_delete(self, relpath: str):
        (self.data_dir / relpath).unlink()

    def computer_list(self, relpath: str = "") -> list[str]:
        """List all files under a data dir path, relative to data_dir."""
        base = self.data_dir / relpath
        if not base.exists():
            return []
        result = []
        for root, dirs, files in os.walk(str(base)):
            # Skip config dirs
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                rel = str((Path(root) / f).relative_to(self.data_dir))
                result.append(rel)
        return sorted(result)

    # --- State helpers ---

    def get_state(self, phone: str) -> dict:
        """Get the current state dict for a phone."""
        name = "phone-a" if phone == "a" else "phone-b"
        state_path = self.cfg_dir / f"state-{name}.json"
        if state_path.exists():
            with open(state_path) as f:
                return json.load(f)
        return {"files": {}}

    def state_file_count(self, phone: str) -> int:
        return len(self.get_state(phone).get("files", {}))

    def state_has_relpath(self, phone: str, relpath: str) -> bool:
        return relpath in self.get_state(phone).get("files", {})

    def state_phone_path(self, phone: str, relpath: str) -> str:
        """Get the phone_path for a given computer relpath in state."""
        return self.get_state(phone)["files"][relpath]["phone_path"]

    def state_is_tombstoned(self, phone: str, relpath: str) -> bool:
        files = self.get_state(phone).get("files", {})
        return files.get(relpath, {}).get("deleted_from_computer", False)

    # --- Sync ---

    def sync(self, phone: str, dry_run: bool = False) -> phonesync.SyncEngine:
        """Run a full sync for one phone. Returns the engine for inspection."""
        self.cfg = phonesync.load_config()
        serial = self._phone_serial(phone)
        engine = phonesync.SyncEngine(self.cfg, serial, dry_run=dry_run)
        engine.run()
        return engine

    def sync_all(self, dry_run: bool = False) -> list[phonesync.SyncEngine]:
        """Sync both phones."""
        return [self.sync("a", dry_run), self.sync("b", dry_run)]


# Pytest fixtures (only defined when pytest is available)
# ---------------------------------------------------------------------------

if HAS_PYTEST:
    @pytest.fixture
    def harness():
        with TestHarness() as h:
            yield h

    @pytest.fixture
    def img_data():
        counter = [0]
        def _make(name="photo", year=2025, month=1, day=15):
            counter[0] += 1
            content = (
                f"FAKE_IMAGE_{name}_{counter[0]}_{year}{month:02d}{day:02d}"
            ).encode()
            dt = datetime(year, month, day, 12, 0, 0)
            mtime = dt.timestamp()
            return content, mtime
        return _make
