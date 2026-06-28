"""
Test fixtures for phonesync.

Provides FakeADB (local-filesystem ADB substitute) and TestHarness
(full sync test environment with two phones and one computer).
"""
import fnmatch
import hashlib
import json
import os
import re
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


if HAS_PYTEST:
    def pytest_configure(config):
        """Refuse to run under pytest as root.

        Root bypasses filesystem permission bits, making permission-
        dependent tests give false passes.
        """
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.exit(
                "Refusing to run tests as root: root bypasses permission "
                "bits and produces false passes. Run as a non-root user.",
                returncode=2)


class FakeADB:
    """Drop-in replacement for phonesync.ADB that operates on local dirs."""

    _registry: dict[str, dict] = {}

    @classmethod
    def register(cls, serial: str, root: Path, model: str = "FakePhone",
                 external_sd: Path = None):
        cls._registry[serial] = {
            "root": root, "model": model, "external_sd": external_sd,
        }

    @classmethod
    def reset_registry(cls):
        cls._registry.clear()
        cls._unreachable_serials = set()

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
        rel = remote_path
        for prefix in ("/sdcard/", "/storage/emulated/0/"):
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
                break
        if self.external_sd and rel.startswith("/storage/"):
            parts = rel.split("/", 3)
            if len(parts) >= 4:
                return self.external_sd / parts[3]
            return self.external_sd
        if rel.startswith("/"):
            rel = rel[1:]
        return self.root / rel

    def _unquote(self, token: str) -> str:
        try:
            parts = shlex.split(token)
            return parts[0] if parts else token
        except ValueError:
            return token.strip("'\"")

    def _phone_path(self, local_path: Path) -> str:
        local_path = Path(local_path)
        try:
            rel = local_path.relative_to(self.root)
            return f"/sdcard/{rel}"
        except ValueError:
            pass
        if self.external_sd:
            try:
                rel = local_path.relative_to(self.external_sd)
                return f"/storage/EXT_SD/{rel}"
            except ValueError:
                pass
        raise ValueError(f"{local_path!s} is not under fake storage")

    @staticmethod
    def _has_pipe(cmd: str) -> bool:
        in_single = False
        in_double = False
        i = 0
        while i < len(cmd):
            c = cmd[i]
            if c == "'" and not in_double:
                in_single = not in_single
            elif c == '"' and not in_single:
                in_double = not in_double
            elif c == '\\' and not in_single:
                i += 1
            elif c == '|' and not in_single and not in_double:
                if i + 1 < len(cmd) and cmd[i + 1] == '|':
                    i += 1
                else:
                    return True
            i += 1
        return False

    @staticmethod
    def _strip_unquoted_stderr_redirect(cmd: str) -> str:
        """Remove unquoted stderr redirects without touching filenames.

        phonesync commonly appends ``2>/dev/null`` to shell commands. A
        filename may also literally contain that text, so we only strip it
        when it appears outside single or double quotes.
        """
        out = []
        in_single = False
        in_double = False
        i = 0

        while i < len(cmd):
            c = cmd[i]

            if c == "'" and not in_double:
                in_single = not in_single
                out.append(c)
                i += 1
                continue

            if c == '"' and not in_single:
                in_double = not in_double
                out.append(c)
                i += 1
                continue

            if c == '\\' and not in_single:
                if i + 1 < len(cmd):
                    out.append(cmd[i])
                    out.append(cmd[i + 1])
                    i += 2
                else:
                    out.append(c)
                    i += 1
                continue

            if not in_single and not in_double:
                if cmd.startswith("2>/dev/null", i):
                    i += len("2>/dev/null")
                    continue
                if cmd.startswith("2> /dev/null", i):
                    i += len("2> /dev/null")
                    continue

            out.append(c)
            i += 1

        return "".join(out).strip()

    def _run(self, args, check=True, capture=True, timeout=120):
        raise NotImplementedError("FakeADB._run() not implemented")

    def shell(self, cmd: str, check=True, timeout=120) -> str:
        cmd = self._strip_unquoted_stderr_redirect(cmd)
        if self._has_pipe(cmd):
            return self._shell_piped(cmd, check, timeout)
        if cmd.startswith("[ -d ") or cmd.startswith("[ -e "):
            return self._shell_test(cmd)
        if cmd.startswith("stat -c "):
            return self._shell_stat(cmd)
        if cmd.startswith("sha256sum "):
            return self._shell_sha256(cmd)
        if cmd.startswith("mkdir -p "):
            return self._shell_mkdir(cmd)
        if cmd.startswith("rm "):
            return self._shell_rm(cmd, check)
        if cmd.startswith("cp "):
            return self._shell_cp(cmd)
        if cmd.startswith("mv "):
            return self._shell_mv(cmd)
        if cmd.startswith("find "):
            return self._shell_find(cmd)
        if cmd.startswith("getprop "):
            if "ro.product.model" in cmd:
                return self.model
            return ""
        if cmd.startswith("echo "):
            # Bare `echo TEXT` (no redirect) — used by the doctor probe.
            return cmd[len("echo "):].strip()
        if cmd.startswith("printf ") and ">" in cmd:
            # `printf 'CONTENT' > /path` — write a file (doctor probe).
            return self._shell_printf_write(cmd)
        if cmd.startswith("ls "):
            return self._shell_ls(cmd)
        raise NotImplementedError(
            f"FakeADB.shell() does not support command: {cmd!r}")

    def _shell_printf_write(self, cmd):
        # Parse: printf 'CONTENT' > /remote/path
        body = cmd[len("printf "):]
        gt = body.rfind(">")
        content_part = body[:gt].strip()
        path_part = body[gt + 1:].strip()
        # Strip surrounding quotes from content and path.
        content = content_part.strip().strip("'\"")
        remote = self._unquote(path_part)
        local = self._local(remote)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(content)
        return ""

    def _shell_piped(self, cmd, check, timeout):
        parts = cmd.split("|", 1)
        left = parts[0].strip()
        right = parts[1].strip()
        left_output = self.shell(left, check=check, timeout=timeout)
        if right == "wc -l":
            lines = [l for l in left_output.strip().split("\n") if l.strip()]
            return str(len(lines))
        if right.startswith("head"):
            m = re.search(r'-(\d+)', right)
            n = int(m.group(1)) if m else 10
            lines = left_output.strip().split("\n")
            return "\n".join(lines[:n])
        raise NotImplementedError(
            f"FakeADB.shell() does not support pipe RHS: {right!r}")

    def _shell_test(self, cmd):
        bracket_end = cmd.find("] &&")
        if bracket_end == -1:
            bracket_end = cmd.find("] ||")
        if bracket_end == -1:
            return "MISSING"
        inner = cmd[1:bracket_end].strip()
        try:
            tokens = shlex.split(inner)
        except ValueError:
            return "MISSING"
        if len(tokens) >= 2:
            flag, path = tokens[0], tokens[1]
            local = self._local(path)
            if flag == "-d":
                return "EXISTS" if local.is_dir() else "MISSING"
            elif flag == "-e":
                return "EXISTS" if local.exists() else "MISSING"
        return "MISSING"

    def _shell_stat(self, cmd):
        rest = cmd[len("stat -c "):].strip()
        try:
            tokens = shlex.split(rest)
        except ValueError:
            return ""
        if len(tokens) < 2:
            return ""
        fmt, path = tokens[0], tokens[1]
        local = self._local(path)
        if not local.exists():
            return ""
        st = local.stat()
        if fmt == "%s":
            return str(st.st_size)
        elif fmt == "%Y":
            return str(int(st.st_mtime))
        return ""

    def _shell_sha256(self, cmd):
        rest = cmd[len("sha256sum "):].strip()
        path = self._unquote(rest)
        local = self._local(path)
        try:
            if local.exists() and local.is_file():
                h = hashlib.sha256(local.read_bytes()).hexdigest()
                return f"{h}  {path}"
        except OSError:
            pass
        return ""

    def _shell_mkdir(self, cmd):
        rest = cmd[len("mkdir -p "):].strip()
        path = self._unquote(rest)
        try:
            self._local(path).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise phonesync.ADBError(f"mkdir: {path}: {e}")
        return ""

    def _shell_rm(self, cmd, check):
        force = "-f" in cmd
        rest = cmd[len("rm "):].strip()
        if rest.startswith("-f "):
            rest = rest[3:].strip()
        path = self._unquote(rest)
        local = self._local(path)
        try:
            if local.exists():
                if local.is_dir():
                    if check and not force:
                        raise phonesync.ADBError(f"rm: {path}: Is a directory")
                    return ""
                local.unlink()
            elif not force and check:
                raise phonesync.ADBError(f"rm: {path}: No such file")
        except OSError as e:
            if check and not force:
                raise phonesync.ADBError(f"rm: {path}: {e}")
        return ""

    def _shell_cp(self, cmd):
        rest = cmd[len("cp "):].strip()
        try:
            tokens = shlex.split(rest)
        except ValueError:
            raise phonesync.ADBError(f"cp: parse error: {rest}")
        if len(tokens) < 2:
            raise phonesync.ADBError("cp: missing operand")
        src_path, dst_path = tokens[-2], tokens[-1]
        local_src = self._local(src_path)
        local_dst = self._local(dst_path)
        if not local_src.exists():
            raise phonesync.ADBError(f"cp: {src_path}: No such file")
        local_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(local_src), str(local_dst))
        return ""

    def _shell_mv(self, cmd):
        rest = cmd[len("mv "):].strip()
        try:
            tokens = shlex.split(rest)
        except ValueError:
            raise phonesync.ADBError(f"mv: parse error: {rest}")
        if len(tokens) < 2:
            raise phonesync.ADBError("mv: missing operand")
        src_path, dst_path = tokens[-2], tokens[-1]
        local_src = self._local(src_path)
        local_dst = self._local(dst_path)
        if not local_src.exists():
            raise phonesync.ADBError(f"mv: {src_path}: No such file")
        local_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(local_src), str(local_dst))
        return ""

    def _shell_find(self, cmd):
        rest = cmd[len("find "):].strip()
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
        max_depth = 999
        m = re.search(r'-maxdepth\s+(\d+)', cmd)
        if m:
            max_depth = int(m.group(1))
        type_filter = None
        if "-type d" in cmd:
            type_filter = "d"
        elif "-type f" in cmd:
            type_filter = "f"
        prune_names = re.findall(r"-name\s+'([^']+)'\s+-prune", cmd)
        if not prune_names:
            prune_names = re.findall(r'-name\s+"([^"]+)"\s+-prune', cmd)
        if "printf" in cmd and "stat" in cmd:
            return self._find_with_stat(local_dir, directory,
                                        max_depth, type_filter, prune_names)
        return self._find_simple(local_dir, directory,
                                 max_depth, type_filter, prune_names)

    def _find_with_stat(self, local_dir, phone_base, max_depth, type_filter,
                        prune_names=None):
        if prune_names is None:
            prune_names = []
        # Emit a flat NUL-separated field stream with a trailing NUL after
        # each record, matching the real ADB printf "%s\0%s\0%s\0" format.
        records = []
        for root, dirs, files in os.walk(str(local_dir)):
            depth = str(root).count(os.sep) - str(local_dir).count(os.sep)
            if depth >= max_depth:
                dirs.clear()
                continue
            dirs[:] = [d for d in dirs if d not in prune_names]
            if type_filter != "f":
                continue
            for fname in files:
                fpath = Path(root) / fname
                phone_path = self._phone_path(fpath)
                st = fpath.stat()
                records.append(
                    f"{st.st_size}\0{int(st.st_mtime)}\0{phone_path}\0")
        return "".join(records)

    def _find_simple(self, local_dir, phone_base, max_depth, type_filter,
                     prune_names=None):
        if prune_names is None:
            prune_names = []
        results = []
        for root, dirs, files in os.walk(str(local_dir)):
            depth = str(root).count(os.sep) - str(local_dir).count(os.sep)
            if depth >= max_depth:
                dirs.clear()
                continue
            dirs[:] = [d for d in dirs if d not in prune_names]
            phone_path = self._phone_path(Path(root))
            if type_filter == "d":
                if depth == 0:
                    results.append(phone_path)
                for d in dirs:
                    results.append(self._phone_path(Path(root) / d))
            elif type_filter == "f":
                for fname in files:
                    results.append(self._phone_path(Path(root) / fname))
            else:
                results.append(phone_path)
        return "\n".join(results)

    def _shell_ls(self, cmd):
        rest = cmd[len("ls "):].strip()
        tokens = rest.split()
        paths = [t for t in tokens if not t.startswith("-")]
        if not paths:
            return ""
        pattern = paths[0]
        if "*" in pattern:
            phone_dir = os.path.dirname(pattern.replace("*", ""))
            local_dir = self._local(phone_dir)
            if not local_dir.is_dir():
                return ""
            results = []
            for item in local_dir.iterdir():
                if item.is_dir():
                    results.append(self._phone_path(item) + "/")
            return "\n".join(results)
        return ""

    # --- High-level methods ---

    def list_files_recursive(self, remote_dir: str,
                             exclude_dirs: list[str] = None,
                             exclude_files: list[str] = None,
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
                for pat in exclude_files:
                    if fnmatch.fnmatch(fname, pat):
                        skip = True
                        break
                if skip:
                    continue
                fpath = Path(root) / fname
                st = fpath.stat()
                phone_path = self._phone_path(fpath)
                rel_to_dir = fpath.relative_to(local_dir)
                files.append({
                    "name": fname,
                    "size": st.st_size,
                    "mtime_epoch": int(st.st_mtime),
                    "path": phone_path,
                    "relpath": str(rel_to_dir),
                })
        return files

    def list_files(self, remote_dir: str) -> list[dict]:
        return self.list_files_recursive(remote_dir, max_depth=1)

    # Test-controllable reachability. Tests can set
    # FakeADB._unreachable_serials.add(serial) to simulate a dropped device.
    _unreachable_serials: set = set()

    def is_reachable(self) -> bool:
        return self.serial not in self._unreachable_serials

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
        try:
            if local.exists():
                if local.is_dir():
                    return False
                local.unlink()
                return True
        except OSError:
            return False
        return False

    def mkdir(self, remote_path: str) -> bool:
        try:
            self._local(remote_path).mkdir(parents=True, exist_ok=True)
            return True
        except OSError:
            return False

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
        result = {"ok": False, "action": "", "source_deleted": False}
        src = self._local(remote_src)
        dst = self._local(remote_dst)
        if dst.exists():
            if expected_hash:
                existing_hash = hashlib.sha256(dst.read_bytes()).hexdigest()
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
        try:
            if local.exists() and local.is_file():
                return hashlib.sha256(local.read_bytes()).hexdigest()
        except OSError:
            pass
        return None

    def get_model(self) -> str:
        return self.model

    def list_storage_volumes(self) -> list[dict]:
        volumes = [{"type": "internal", "path": "/sdcard",
                     "label": "Internal Storage"}]
        if self.external_sd and self.external_sd.is_dir():
            volumes.append({"type": "external_sd", "path": "/storage/EXT_SD",
                            "label": "SD Card (EXT_SD)", "id": "EXT_SD"})
        return volumes


# ---------------------------------------------------------------------------
_fake_connected_devices: list[dict] = []

def fake_list_connected_devices() -> list[dict]:
    return list(_fake_connected_devices)


# ---------------------------------------------------------------------------
class TestHarness:
    def __init__(self):
        self.tmpdir = None
        self._original_list = None

    def __enter__(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="phonesync_test_"))
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
        FakeADB.reset_registry()
        FakeADB.register("SERIAL_A", self.phone_a_dir, "PhoneA")
        FakeADB.register("SERIAL_B", self.phone_b_dir, "PhoneB")
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
            "photo_date_folders": True, "recursive_scan": True,
            "preserve_phone_subdirs": True,
            # Production defaults read_only=True (no phone writes). The
            # existing move-propagation tests assume writes happen, so the
            # harness opts in, mirroring a user who passes --apply-phone-moves.
            # The default-true behavior is covered by TestReadOnlyDefault.
            "read_only": False,
            "delete_from_phone_after_sync": False,
            "propagate_computer_deletes_to_phone": False,
            "max_symlink_depth": 2,
            "devices": {
                "SERIAL_A": {"name": "phone-a", "model": "PhoneA",
                    "sources": {"photos": ["/sdcard/DCIM/Camera",
                        "/sdcard/DCIM/Screenshots", "/sdcard/Pictures"],
                        "downloads": ["/sdcard/Download"],
                        "recordings": ["/sdcard/Recordings"]}},
                "SERIAL_B": {"name": "phone-b", "model": "PhoneB",
                    "sources": {"photos": ["/sdcard/DCIM/Camera",
                        "/sdcard/DCIM/Screenshots", "/sdcard/Pictures"],
                        "downloads": ["/sdcard/Download"],
                        "recordings": ["/sdcard/Recordings"]}},
            },
        }
        phonesync.save_config(self.cfg)
        # Pre-approve both test devices so the first-time-device gate (#5)
        # doesn't block the many tests that sync a fresh device. Tests that
        # exercise the gate itself clear this via forget_device().
        phonesync.approve_device(self.cfg, "SERIAL_A", "phone-a")
        phonesync.approve_device(self.cfg, "SERIAL_B", "phone-b")
        return self

    def __exit__(self, *args):
        phonesync.list_connected_devices = self._original_list
        FakeADB.reset_registry()
        global _fake_connected_devices
        _fake_connected_devices = []
        if self.tmpdir and self.tmpdir.exists():
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _phone_dir(self, phone):
        if phone == "a": return self.phone_a_dir
        if phone == "b": return self.phone_b_dir
        raise ValueError(f"Unknown phone: {phone}")

    def _phone_serial(self, phone):
        return "SERIAL_A" if phone == "a" else "SERIAL_B"

    def unapprove_device(self, phone):
        """Remove a device from the approved registry, so the next sync hits
        the first-time-device gate (#5)."""
        phonesync.forget_device(
            phonesync.load_config(), self._phone_serial(phone))

    def is_approved(self, phone):
        return phonesync.is_device_known(
            phonesync.load_config(), self._phone_serial(phone))

    def phone_write(self, phone, relpath, content, mtime=None):
        p = self._phone_dir(phone) / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        if mtime is not None:
            os.utime(str(p), (mtime, mtime))

    def phone_read(self, phone, relpath):
        return (self._phone_dir(phone) / relpath).read_bytes()

    def phone_exists(self, phone, relpath):
        return (self._phone_dir(phone) / relpath).exists()

    def phone_move(self, phone, src, dst):
        s = self._phone_dir(phone) / src
        d = self._phone_dir(phone) / dst
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))

    def phone_delete(self, phone, relpath):
        (self._phone_dir(phone) / relpath).unlink()

    def phone_list(self, phone, relpath=""):
        base = self._phone_dir(phone) / relpath
        if not base.exists(): return []
        result = []
        for root, dirs, files in os.walk(str(base)):
            for f in files:
                result.append(str((Path(root) / f).relative_to(
                    self._phone_dir(phone))))
        return sorted(result)

    def computer_write(self, relpath, content, mtime=None):
        p = self.data_dir / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        if mtime is not None:
            os.utime(str(p), (mtime, mtime))

    def computer_read(self, relpath):
        return (self.data_dir / relpath).read_bytes()

    def computer_exists(self, relpath):
        return (self.data_dir / relpath).exists()

    def computer_move(self, src, dst):
        s = self.data_dir / src
        d = self.data_dir / dst
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))

    def computer_delete(self, relpath):
        (self.data_dir / relpath).unlink()

    def computer_list(self, relpath=""):
        base = self.data_dir / relpath
        if not base.exists(): return []
        result = []
        for root, dirs, files in os.walk(str(base)):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                result.append(str((Path(root) / f).relative_to(self.data_dir)))
        return sorted(result)

    def get_state(self, phone):
        name = "phone-a" if phone == "a" else "phone-b"
        state_path = self.cfg_dir / f"state-{name}.json"
        if state_path.exists():
            with open(state_path) as f:
                return json.load(f)
        return {"files": {}}

    def state_file_count(self, phone):
        return len(self.get_state(phone).get("files", {}))

    def state_has_relpath(self, phone, relpath):
        return relpath in self.get_state(phone).get("files", {})

    def state_phone_path(self, phone, relpath):
        return self.get_state(phone)["files"][relpath]["phone_path"]

    def state_is_tombstoned(self, phone, relpath):
        files = self.get_state(phone).get("files", {})
        return files.get(relpath, {}).get("deleted_from_computer", False)

    def sync(self, phone, dry_run=False, adb_cls=None):
        self.cfg = phonesync.load_config()
        serial = self._phone_serial(phone)
        engine = phonesync.SyncEngine(
            self.cfg, serial, dry_run=dry_run,
            adb_cls=adb_cls or FakeADB)
        # Capture the run() boolean so tests can assert on abort behavior.
        engine.run_result = engine.run()
        return engine

    def adopt(self, phone, dry_run=False, adb_cls=None):
        self.cfg = phonesync.load_config()
        serial = self._phone_serial(phone)
        engine = phonesync.SyncEngine(
            self.cfg, serial, dry_run=dry_run,
            adb_cls=adb_cls or FakeADB)
        engine.run_result = engine.adopt_existing()
        return engine

    def sync_all(self, dry_run=False):
        return [self.sync("a", dry_run), self.sync("b", dry_run)]


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
