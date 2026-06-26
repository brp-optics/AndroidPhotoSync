"""
Test fixtures for phonesync.

Provides FakeADB (local-filesystem ADB substitute) and TestHarness
(full sync test environment with two phones and one computer).
"""
import fnmatch
import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import pytest
    HAS_PYTEST = True
except ImportError:
    HAS_PYTEST = False

sys.path.insert(0, str(Path(__file__).parent.parent))
import phonesync


# ---------------------------------------------------------------------------
# FakeADB — local filesystem substitute for ADB
# ---------------------------------------------------------------------------

class FakeADB:
    """Drop-in replacement for phonesync.ADB that operates on local dirs.

    Design principles:
      - Raises NotImplementedError for unsupported shell commands
        (fail loudly, not silently)
      - Parses shlex-quoted paths to match real ADB quoting behavior
      - Models external storage for detect-paths tests
    """

    _registry: dict[str, dict] = {}

    @classmethod
    def register(cls, serial: str, root: Path, model: str = "FakePhone",
                 external_sd: Path = None):
        cls._registry[serial] = {
            "root": root,
            "model": model,
            "external_sd": external_sd,
        }

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
        self.external_sd = info.get("external_sd")

    @staticmethod
    def _q(path: str) -> str:
        return shlex.quote(path)

    def _local(self, remote_path: str) -> Path:
        """Map a phone-absolute path to a local path under self.root."""
        rel = remote_path
        for prefix in ("/sdcard/", "/storage/emulated/0/"):
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
                break
        # Handle external SD
        if self.external_sd and rel.startswith("/storage/"):
            # e.g. /storage/ABCD-1234/DCIM/...
            parts = rel.split("/", 3)  # ['', 'storage', 'ABCD-1234', rest]
            if len(parts) >= 4:
                return self.external_sd / parts[3]
            return self.external_sd
        if rel.startswith("/"):
            rel = rel[1:]
        return self.root / rel

    def _unquote(self, token: str) -> str:
        """Unquote a shlex-quoted path."""
        # shlex.split handles all quoting styles
        try:
            parts = shlex.split(token)
            return parts[0] if parts else token
        except ValueError:
            return token.strip("'\"")

    @staticmethod
    def _has_pipe(cmd: str) -> bool:
        """Check if cmd contains a real pipe (|) not a logical OR (||)."""
        import re
        return bool(re.search(r'(?<!\|)\|(?!\|)', cmd))

    def _run(self, args, check=True, capture=True, timeout=120):
        """Not used by FakeADB — shell() handles everything."""
        raise NotImplementedError("FakeADB._run() not implemented")

    def shell(self, cmd: str, check=True, timeout=120) -> str:
        """Execute a shell command against the local fake filesystem.

        Supports the specific command patterns used by phonesync.
        Raises NotImplementedError for unrecognized commands.
        """
        # Strip stderr redirects (common in phonesync commands)
        cmd = cmd.replace("2>/dev/null", "").strip()

        # --- Piped commands: handle single | (not || or &&) ---
        if self._has_pipe(cmd):
            return self._shell_piped(cmd, check, timeout)

        # --- [ -d path ] && echo EXISTS || echo MISSING ---
        if cmd.startswith("[ -d ") or cmd.startswith("[ -e "):
            return self._shell_test(cmd)

        # --- stat -c ---
        if cmd.startswith("stat -c "):
            return self._shell_stat(cmd)

        # --- sha256sum ---
        if cmd.startswith("sha256sum "):
            return self._shell_sha256(cmd)

        # --- mkdir -p ---
        if cmd.startswith("mkdir -p "):
            return self._shell_mkdir(cmd)

        # --- rm / rm -f ---
        if cmd.startswith("rm "):
            return self._shell_rm(cmd, check)

        # --- cp ---
        if cmd.startswith("cp "):
            return self._shell_cp(cmd)

        # --- mv ---
        if cmd.startswith("mv "):
            return self._shell_mv(cmd)

        # --- find ---
        if cmd.startswith("find "):
            return self._shell_find(cmd)

        # --- getprop ---
        if cmd.startswith("getprop "):
            if "ro.product.model" in cmd:
                return self.model
            return ""

        # --- ls ---
        if cmd.startswith("ls "):
            return self._shell_ls(cmd)

        raise NotImplementedError(
            f"FakeADB.shell() does not support command: {cmd!r}")

    def _shell_piped(self, cmd: str, check: bool, timeout: int) -> str:
        """Handle piped commands like 'find ... | wc -l'."""
        parts = cmd.split("|", 1)
        left = parts[0].strip()
        right = parts[1].strip()

        left_output = self.shell(left, check=check, timeout=timeout)

        if right == "wc -l":
            lines = [l for l in left_output.strip().split("\n") if l.strip()]
            return str(len(lines))
        if right.startswith("head"):
            import re
            m = re.search(r'-(\d+)', right)
            n = int(m.group(1)) if m else 10
            lines = left_output.strip().split("\n")
            return "\n".join(lines[:n])

        raise NotImplementedError(
            f"FakeADB.shell() does not support pipe RHS: {right!r}")

    def _shell_test(self, cmd: str) -> str:
        """Handle [ -d path ] / [ -e path ] tests."""
        # Parse: [ -d '/path' ] && echo EXISTS || echo MISSING
        try:
            tokens = shlex.split(cmd.split("]")[0].replace("[", "").strip())
        except ValueError:
            return "MISSING"
        # tokens like ['-d', '/path']
        if len(tokens) >= 2:
            flag = tokens[0]
            path = tokens[1]
            local = self._local(path)
            if flag == "-d":
                return "EXISTS" if local.is_dir() else "MISSING"
            elif flag == "-e":
                return "EXISTS" if local.exists() else "MISSING"
        return "MISSING"

    def _shell_stat(self, cmd: str) -> str:
        """Handle stat -c <format> <path>."""
        # stat -c %s '/path' or stat -c '%s' '/path'
        rest = cmd[len("stat -c "):].strip()
        try:
            tokens = shlex.split(rest)
        except ValueError:
            return ""
        if len(tokens) < 2:
            return ""
        fmt = tokens[0]
        path = tokens[1]
        local = self._local(path)
        if not local.exists():
            return ""
        st = local.stat()
        if fmt == "%s":
            return str(st.st_size)
        elif fmt == "%Y":
            return str(int(st.st_mtime))
        elif fmt == "%s %Y":
            return f"{st.st_size} {int(st.st_mtime)}"
        return ""

    def _shell_sha256(self, cmd: str) -> str:
        """Handle sha256sum <path>."""
        rest = cmd[len("sha256sum "):].strip()
        path = self._unquote(rest)
        local = self._local(path)
        if local.exists():
            h = hashlib.sha256(local.read_bytes()).hexdigest()
            return f"{h}  {path}"
        return ""

    def _shell_mkdir(self, cmd: str) -> str:
        """Handle mkdir -p <path>."""
        rest = cmd[len("mkdir -p "):].strip()
        path = self._unquote(rest)
        self._local(path).mkdir(parents=True, exist_ok=True)
        return ""

    def _shell_rm(self, cmd: str, check: bool) -> str:
        """Handle rm [-f] <path>."""
        force = "-f" in cmd
        # Extract path (last token)
        rest = cmd[len("rm "):].strip()
        if rest.startswith("-f "):
            rest = rest[3:].strip()
        path = self._unquote(rest)
        local = self._local(path)
        if local.exists():
            local.unlink()
        elif not force and check:
            raise phonesync.ADBError(f"rm: {path}: No such file")
        return ""

    def _shell_cp(self, cmd: str) -> str:
        """Handle cp <src> <dst>."""
        rest = cmd[len("cp "):].strip()
        try:
            tokens = shlex.split(rest)
        except ValueError:
            raise phonesync.ADBError(f"cp: parse error: {rest}")
        if len(tokens) < 2:
            raise phonesync.ADBError(f"cp: missing operand")
        src_path = tokens[-2]
        dst_path = tokens[-1]
        local_src = self._local(src_path)
        local_dst = self._local(dst_path)
        if not local_src.exists():
            raise phonesync.ADBError(f"cp: {src_path}: No such file")
        local_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(local_src), str(local_dst))
        return ""

    def _shell_mv(self, cmd: str) -> str:
        """Handle mv <src> <dst>."""
        rest = cmd[len("mv "):].strip()
        try:
            tokens = shlex.split(rest)
        except ValueError:
            raise phonesync.ADBError(f"mv: parse error: {rest}")
        if len(tokens) < 2:
            raise phonesync.ADBError(f"mv: missing operand")
        src_path = tokens[-2]
        dst_path = tokens[-1]
        local_src = self._local(src_path)
        local_dst = self._local(dst_path)
        if not local_src.exists():
            raise phonesync.ADBError(f"mv: {src_path}: No such file")
        local_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(local_src), str(local_dst))
        return ""

    def _shell_find(self, cmd: str) -> str:
        """Handle find commands."""
        import re

        # Extract directory (first shlex token after 'find')
        # This is tricky because find commands have complex syntax.
        # We parse known flags rather than trying to shlex the whole thing.
        rest = cmd[len("find "):].strip()

        # Extract the directory (first non-option token)
        directory = None
        try:
            tokens = shlex.split(rest)
        except ValueError:
            tokens = rest.split()

        for t in tokens:
            if not t.startswith("-") and not t.startswith("\\"):
                directory = t
                break
        if not directory:
            return ""
        local_dir = self._local(directory)
        if not local_dir.is_dir():
            return ""

        # Parse options
        max_depth = 999
        m = re.search(r'-maxdepth\s+(\d+)', cmd)
        if m:
            max_depth = int(m.group(1))

        type_filter = None
        if "-type d" in cmd:
            type_filter = "d"
        elif "-type f" in cmd:
            type_filter = "f"

        # Check for our printf+stat pattern (null-separated format)
        if "printf" in cmd and "stat" in cmd:
            return self._find_with_stat(local_dir, directory,
                                         max_depth, type_filter)

        # Simple find: return paths
        return self._find_simple(local_dir, directory,
                                  max_depth, type_filter)

    def _find_with_stat(self, local_dir: Path, phone_base: str,
                        max_depth: int, type_filter: str) -> str:
        """Handle find -exec sh -c 'printf ... stat ...' pattern."""
        lines = []
        for root, dirs, files in os.walk(str(local_dir)):
            depth = str(root).count(os.sep) - str(local_dir).count(os.sep)
            if depth >= max_depth:
                dirs.clear()
                continue
            if type_filter != "f":
                continue
            for fname in files:
                fpath = Path(root) / fname
                rel_to_root = fpath.relative_to(self.root)
                phone_path = f"/sdcard/{rel_to_root}"
                st = fpath.stat()
                lines.append(
                    f"{st.st_size}\0{int(st.st_mtime)}\0{phone_path}")
        return "\n".join(lines)

    def _find_simple(self, local_dir: Path, phone_base: str,
                     max_depth: int, type_filter: str) -> str:
        """Handle simple find commands returning path lists."""
        results = []
        for root, dirs, files in os.walk(str(local_dir)):
            depth = str(root).count(os.sep) - str(local_dir).count(os.sep)
            if depth >= max_depth:
                dirs.clear()
                continue
            rel = Path(root).relative_to(self.root)
            phone_path = f"/sdcard/{rel}"
            if type_filter == "d":
                if depth == 0:
                    results.append(phone_path)
                for d in dirs:
                    d_rel = (Path(root) / d).relative_to(self.root)
                    results.append(f"/sdcard/{d_rel}")
            elif type_filter == "f":
                for fname in files:
                    f_rel = (Path(root) / fname).relative_to(self.root)
                    results.append(f"/sdcard/{f_rel}")
            else:
                results.append(phone_path)
        return "\n".join(results)

    def _shell_ls(self, cmd: str) -> str:
        """Handle ls commands."""
        # Support ls -1 /path/ and ls -1d /path/*/
        rest = cmd[len("ls "):].strip()
        tokens = rest.split()
        # Find the path (last non-flag token)
        paths = [t for t in tokens if not t.startswith("-")]
        if not paths:
            return ""
        pattern = paths[0]
        # Handle glob patterns like /storage/*/
        if "*" in pattern:
            import glob
            phone_dir = os.path.dirname(pattern.replace("*", ""))
            local_dir = self._local(phone_dir)
            if not local_dir.is_dir():
                return ""
            results = []
            for item in local_dir.iterdir():
                if item.is_dir():
                    rel = item.relative_to(self.root)
                    results.append(f"/sdcard/{rel}/")
            return "\n".join(results)
        return ""

    # --- High-level methods (match ADB interface exactly) ---

    def list_files_recursive(self, remote_dir: str,
                             exclude_dirs: list[str] = None, exclude_files: list[str] = None,
                             max_depth: int = 255) -> list[dict]:
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
            dirs[:] = [d for d in dirs if d not in exclude_dirs]

            for fname in filenames:
                skip = False
                for pattern in exclude_files:
                    if fnmatch.fnmatch(fname, pattern):
                        skip = True
                        break
                if skip:
                    continue

                fpath = Path(root) / fname
                st = fpath.stat()
                rel_to_root = fpath.relative_to(self.root)
                phone_path = f"/sdcard/{rel_to_root}"
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
        """Copy-verify-delete move, matching real ADB.move_safe."""
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

    def file_mtime(self, remote_path: str) -> Optional[int]:
        local = self._local(remote_path)
        if local.exists():
            return int(local.stat().st_mtime)
        return None

    def file_hash(self, remote_path: str) -> Optional[str]:
        local = self._local(remote_path)
        if local.exists():
            return hashlib.sha256(local.read_bytes()).hexdigest()
        return None

    def get_model(self) -> str:
        return self.model

    def list_storage_volumes(self) -> list[dict]:
        volumes = [{
            "type": "internal",
            "path": "/sdcard",
            "label": "Internal Storage",
        }]
        if self.external_sd and self.external_sd.is_dir():
            volumes.append({
                "type": "external_sd",
                "path": "/storage/EXT_SD",
                "label": "SD Card (EXT_SD)",
                "id": "EXT_SD",
            })
        return volumes


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

    Uses dependency injection (adb_cls parameter) for SyncEngine
    instead of monkey-patching globals. Only patches
    list_connected_devices for CLI tests.
    """

    def __init__(self):
        self.tmpdir = None
        self._original_list = None

    def __enter__(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="phonesync_test_"))

        # Create phone directories
        self.phone_a_dir = self.tmpdir / "phone_a"
        self.phone_b_dir = self.tmpdir / "phone_b"
        self.phone_a_dir.mkdir()
        self.phone_b_dir.mkdir()

        for phone_dir in (self.phone_a_dir, self.phone_b_dir):
            (phone_dir / "DCIM" / "Camera").mkdir(parents=True)
            (phone_dir / "DCIM" / "Screenshots").mkdir(parents=True)
            (phone_dir / "Pictures").mkdir(parents=True)
            (phone_dir / "Download").mkdir(parents=True)
            (phone_dir / "Recordings").mkdir(parents=True)

        self.data_dir = self.tmpdir / "PhoneSync"
        self.cfg_dir = self.tmpdir / "config"
        self.data_dir.mkdir()
        self.cfg_dir.mkdir()

        # Register fake devices
        FakeADB.reset_registry()
        FakeADB.register("SERIAL_A", self.phone_a_dir, "PhoneA")
        FakeADB.register("SERIAL_B", self.phone_b_dir, "PhoneB")

        # Patch list_connected_devices (still needed for CLI commands)
        global _fake_connected_devices
        _fake_connected_devices = [
            {"serial": "SERIAL_A", "model": "PhoneA"},
            {"serial": "SERIAL_B", "model": "PhoneB"},
        ]
        self._original_list = phonesync.list_connected_devices
        phonesync.list_connected_devices = fake_list_connected_devices

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
        return self

    def __exit__(self, *args):
        phonesync.list_connected_devices = self._original_list
        FakeADB.reset_registry()
        global _fake_connected_devices
        _fake_connected_devices = []
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
        base = self.data_dir / relpath
        if not base.exists():
            return []
        result = []
        for root, dirs, files in os.walk(str(base)):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                rel = str((Path(root) / f).relative_to(self.data_dir))
                result.append(rel)
        return sorted(result)

    # --- State helpers ---

    def get_state(self, phone: str) -> dict:
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
        return self.get_state(phone)["files"][relpath]["phone_path"]

    def state_is_tombstoned(self, phone: str, relpath: str) -> bool:
        files = self.get_state(phone).get("files", {})
        return files.get(relpath, {}).get("deleted_from_computer", False)

    # --- Sync (uses dependency injection, NOT monkey-patching) ---

    def sync(self, phone: str, dry_run: bool = False) -> phonesync.SyncEngine:
        """Run a full sync using DI (adb_cls=FakeADB)."""
        self.cfg = phonesync.load_config()
        serial = self._phone_serial(phone)
        engine = phonesync.SyncEngine(
            self.cfg, serial, dry_run=dry_run, adb_cls=FakeADB)
        engine.run()
        return engine

    def sync_all(self, dry_run: bool = False) -> list[phonesync.SyncEngine]:
        return [self.sync("a", dry_run), self.sync("b", dry_run)]


# ---------------------------------------------------------------------------
# Pytest fixtures (only if pytest is available)
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
