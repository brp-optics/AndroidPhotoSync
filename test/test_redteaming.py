"""
Additional red-team tests for only the ADB / FakeADB boundary.

Drop this file into your pytest test directory, next to conftest.py.
It is intentionally additive: it does not replace test_contract.py or
 test_real_adb.py.

Focus:
  - FakeADB vs Android-ish shell edge cases
  - external-SD path reconstruction
  - real ADB command construction, parser branches, and timeout kwargs
  - data-safety branches in ADB.move_safe()
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import phonesync  # noqa: E402
from conftest import FakeADB  # noqa: E402


# ---------------------------------------------------------------------------
# Local fixtures/helpers
# ---------------------------------------------------------------------------

class ADBFixture:
    """Standalone FakeADB fixture; mirrors the one in test_contract.py."""

    def __init__(self, external_sd: bool = False):
        self._want_sd = external_sd

    def __enter__(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="fakeadb_redteam_"))
        self.root = self.tmpdir / "internal"
        self.root.mkdir()
        self.ext_sd_dir = None
        if self._want_sd:
            self.ext_sd_dir = self.tmpdir / "ext_sd"
            self.ext_sd_dir.mkdir()

        FakeADB.reset_registry()
        FakeADB.register(
            "TEST",
            self.root,
            "TestPhone",
            external_sd=self.ext_sd_dir,
        )
        self.adb = FakeADB("TEST")
        return self

    def __exit__(self, *args):
        FakeADB.reset_registry()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def write(self, relpath: str, content: bytes, mtime: float | None = None):
        p = self.root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        if mtime is not None:
            os.utime(str(p), (mtime, mtime))
        return p

    def write_sd(self, relpath: str, content: bytes,
                 mtime: float | None = None):
        assert self.ext_sd_dir is not None, "ADBFixture(external_sd=True) needed"
        p = self.ext_sd_dir / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        if mtime is not None:
            os.utime(str(p), (mtime, mtime))
        return p

    def read(self, relpath: str) -> bytes:
        return (self.root / relpath).read_bytes()

    def exists(self, relpath: str) -> bool:
        return (self.root / relpath).exists()

    def q(self, phone_path: str) -> str:
        return self.adb._q(phone_path)


class SubprocessStub:
    """subprocess.run stub with queued responses and captured call kwargs."""

    def __init__(self):
        self.calls: list[dict] = []
        self._responses: list[tuple[int, str, str] | BaseException] = []
        self._default: tuple[int, str, str] | BaseException = (0, "", "")
        self._original = None

    def respond(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self._responses.append((returncode, stdout, stderr))
        return self

    def respond_default(self, returncode: int = 0, stdout: str = "",
                        stderr: str = ""):
        self._default = (returncode, stdout, stderr)
        return self

    def respond_exception(self, exc: BaseException):
        self._responses.append(exc)
        return self

    def respond_default_exception(self, exc: BaseException):
        self._default = exc
        return self

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        self.calls.append({
            "cmd": cmd,
            "capture_output": capture_output,
            "text": text,
            "timeout": timeout,
        })
        if self._responses:
            response = self._responses.pop(0)
        else:
            response = self._default
        if isinstance(response, BaseException):
            raise response
        returncode, stdout, stderr = response
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    def install(self):
        self._original = phonesync.subprocess.run
        phonesync.subprocess.run = self
        return self

    def restore(self):
        if self._original is not None:
            phonesync.subprocess.run = self._original
            self._original = None

    def __enter__(self):
        return self.install()

    def __exit__(self, *args):
        self.restore()

    @property
    def last(self) -> dict:
        assert self.calls, "No subprocess calls recorded"
        return self.calls[-1]

    @property
    def last_cmd(self):
        return self.last["cmd"]


# ---------------------------------------------------------------------------
# FakeADB: external SD must behave like a first-class phone volume
# ---------------------------------------------------------------------------

class TestFakeADBExternalSDRedTeam:
    def test_list_files_recursive_on_external_sd(self):
        with ADBFixture(external_sd=True) as f:
            f.write_sd("DCIM/Camera/sd_photo.jpg", b"sd data", 1700000000.0)

            files = f.adb.list_files_recursive("/storage/EXT_SD/DCIM/Camera")

            assert len(files) == 1
            assert files[0]["name"] == "sd_photo.jpg"
            assert files[0]["size"] == len(b"sd data")
            assert files[0]["mtime_epoch"] == 1700000000
            assert files[0]["path"] == "/storage/EXT_SD/DCIM/Camera/sd_photo.jpg"
            assert files[0]["relpath"] == "sd_photo.jpg"

    def test_shell_find_on_external_sd(self):
        with ADBFixture(external_sd=True) as f:
            f.write_sd("DCIM/Camera/sd_photo.jpg", b"sd data")

            out = f.adb.shell(
                "find /storage/EXT_SD/DCIM/Camera -type f 2>/dev/null"
            )

            assert "/storage/EXT_SD/DCIM/Camera/sd_photo.jpg" in out
            assert "/sdcard/" not in out

    def test_shell_find_wc_l_on_external_sd(self):
        with ADBFixture(external_sd=True) as f:
            f.write_sd("DCIM/Camera/a.jpg", b"a")
            f.write_sd("DCIM/Camera/b.jpg", b"b")

            out = f.adb.shell(
                "find /storage/EXT_SD/DCIM/Camera -type f 2>/dev/null | wc -l"
            )

            assert out.strip() == "2"

    def test_shell_find_stat_printf_on_external_sd(self):
        with ADBFixture(external_sd=True) as f:
            f.write_sd("DCIM/Camera/sd_photo.jpg", b"sd data", 1700000000.0)
            cmd = (
                "find /storage/EXT_SD/DCIM/Camera -maxdepth 10 "
                "-type f -exec sh -c "
                "'for f; do "
                "s=$(stat -c %s \"$f\") && "
                "m=$(stat -c %Y \"$f\") && "
                "printf \"%s\\0%s\\0%s\\n\" \"$s\" \"$m\" \"$f\"; "
                "done' _ {} +"
            )

            out = f.adb.shell(cmd)

            assert f"{len(b'sd data')}\0" in out
            assert f"\0{1700000000}\0" in out
            assert "\0/storage/EXT_SD/DCIM/Camera/sd_photo.jpg" in out

    def test_shell_ls_glob_on_external_sd(self):
        with ADBFixture(external_sd=True) as f:
            (f.ext_sd_dir / "DCIM" / "Camera").mkdir(parents=True)
            (f.ext_sd_dir / "DCIM" / "Screenshots").mkdir(parents=True)

            out = f.adb.shell(
                "ls -1d /storage/EXT_SD/DCIM/*/ 2>/dev/null | head -20"
            )

            assert "/storage/EXT_SD/DCIM/Camera/" in out
            assert "/storage/EXT_SD/DCIM/Screenshots/" in out


# ---------------------------------------------------------------------------
# FakeADB: shell metacharacter and parser red-team cases
# ---------------------------------------------------------------------------

class TestFakeADBShellParserRedTeam:
    def test_pipe_char_inside_quoted_filename_is_not_pipeline(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/photo|vacation.jpg", b"data")
            q = f.q("/sdcard/DCIM/Camera/photo|vacation.jpg")

            out = f.adb.shell(f"stat -c %s {q}")

            assert out.strip() == "4"

    def test_shell_or_operator_is_not_pipeline(self):
        with ADBFixture() as f:
            out = f.adb.shell(
                "[ -e /sdcard/nope.jpg ] && echo EXISTS || echo MISSING"
            )
            assert "MISSING" in out

    def test_filename_containing_stderr_redirect_literal(self):
        """A literal '2>/dev/null' inside quotes must not be stripped."""
        with ADBFixture() as f:
            name = "weird2>/dev/null.jpg"
            f.write(f"DCIM/Camera/{name}", b"x")
            q = f.q(f"/sdcard/DCIM/Camera/{name}")

            out = f.adb.shell(f"stat -c %s {q}")

            assert out.strip() == "1"

    def test_shell_cp_to_existing_directory(self):
        with ADBFixture() as f:
            f.write("src.jpg", b"data")
            (f.root / "destdir").mkdir()

            f.adb.shell("cp /sdcard/src.jpg /sdcard/destdir/")

            assert f.read("destdir/src.jpg") == b"data"
            assert f.exists("src.jpg")

    def test_shell_mv_to_existing_directory(self):
        with ADBFixture() as f:
            f.write("src.jpg", b"data")
            (f.root / "destdir").mkdir()

            f.adb.shell("mv /sdcard/src.jpg /sdcard/destdir/")

            assert f.read("destdir/src.jpg") == b"data"
            assert not f.exists("src.jpg")

    def test_getprop_unknown_property_returns_empty(self):
        with ADBFixture() as f:
            assert f.adb.shell("getprop ro.this.property.does.not.exist") == ""

    def test_binary_file_hash_and_pull_roundtrip(self):
        with ADBFixture() as f:
            data = bytes(range(256)) * 4
            f.write("binary.bin", data)
            dst = f.tmpdir / "pulled.bin"

            assert f.adb.pull("/sdcard/binary.bin", str(dst)) is True
            assert dst.read_bytes() == data
            assert f.adb.file_hash("/sdcard/binary.bin") == (
                hashlib.sha256(data).hexdigest()
            )


# ---------------------------------------------------------------------------
# FakeADB: high-level method failure behavior should match ADB wrappers
# ---------------------------------------------------------------------------

class TestFakeADBFailureSemantics:
    def test_mkdir_parent_component_is_file_returns_false_or_raises_adberror(self):
        """Real ADB.mkdir() catches shell failure and returns False.

        This test encodes the desired contract for the fake. If FakeADB raises
        FileExistsError here, it is leaking local pathlib semantics instead of
        modeling the ADB wrapper.
        """
        with ADBFixture() as f:
            f.write("parent_is_file", b"not a dir")
            try:
                ok = f.adb.mkdir("/sdcard/parent_is_file/child")
            except phonesync.ADBError:
                ok = False
            assert ok is False

    def test_delete_directory_returns_false_not_uncaught_isadirerror(self):
        with ADBFixture() as f:
            (f.root / "directory").mkdir()
            try:
                ok = f.adb.delete("/sdcard/directory")
            except phonesync.ADBError:
                ok = False
            assert ok is False
            assert (f.root / "directory").is_dir()

    def test_file_hash_directory_returns_none_not_uncaught_isadirerror(self):
        with ADBFixture() as f:
            (f.root / "directory").mkdir()
            assert f.adb.file_hash("/sdcard/directory") is None


# ---------------------------------------------------------------------------
# Real ADB: command construction, wrappers, and timeout kwargs
# ---------------------------------------------------------------------------

class TestRealADBWrappersRedTeam:
    def test_delete_command_success_and_timeout(self):
        with SubprocessStub().respond(stdout="") as stub:
            adb = phonesync.ADB("SER")
            assert adb.delete("/sdcard/a b.txt") is True

        assert stub.last_cmd == [
            "adb", "-s", "SER", "shell", "rm '/sdcard/a b.txt'"
        ]
        assert stub.last["timeout"] == 120

    def test_delete_failure_returns_false(self):
        with SubprocessStub().respond(returncode=1, stderr="rm failed") as stub:
            adb = phonesync.ADB("SER")
            assert adb.delete("/sdcard/no.txt") is False
        assert stub.last_cmd[-1] == "rm /sdcard/no.txt"

    def test_mkdir_command_success(self):
        with SubprocessStub().respond(stdout="") as stub:
            adb = phonesync.ADB("SER")
            assert adb.mkdir("/sdcard/a b/c") is True

        assert stub.last_cmd == [
            "adb", "-s", "SER", "shell", "mkdir -p '/sdcard/a b/c'"
        ]

    def test_mkdir_failure_returns_false(self):
        with SubprocessStub().respond(returncode=1, stderr="mkdir failed"):
            adb = phonesync.ADB("SER")
            assert adb.mkdir("/sdcard/file/child") is False

    def test_move_command_sequence_success(self):
        stub = SubprocessStub()
        stub.respond(stdout="")
        stub.respond(stdout="")
        with stub:
            adb = phonesync.ADB("SER")
            assert adb.move("/sdcard/old name.jpg", "/sdcard/New Dir/new.jpg") is True

        assert stub.calls[0]["cmd"] == [
            "adb", "-s", "SER", "shell", "mkdir -p '/sdcard/New Dir'"
        ]
        assert stub.calls[1]["cmd"] == [
            "adb", "-s", "SER", "shell",
            "mv '/sdcard/old name.jpg' '/sdcard/New Dir/new.jpg'",
        ]

    def test_move_mkdir_failure_returns_false_without_mv(self):
        with SubprocessStub().respond(returncode=1, stderr="mkdir failed") as stub:
            adb = phonesync.ADB("SER")
            assert adb.move("/sdcard/src.jpg", "/sdcard/Nope/dst.jpg") is False
        assert len(stub.calls) == 1

    def test_move_mv_failure_returns_false(self):
        stub = SubprocessStub()
        stub.respond(stdout="")
        stub.respond(returncode=1, stderr="mv failed")
        with stub:
            adb = phonesync.ADB("SER")
            assert adb.move("/sdcard/src.jpg", "/sdcard/dst.jpg") is False
        assert len(stub.calls) == 2

    def test_file_exists_true_false(self):
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS\n")
        stub.respond(stdout="MISSING\n")
        with stub:
            adb = phonesync.ADB("SER")
            assert adb.file_exists("/sdcard/a.jpg") is True
            assert adb.file_exists("/sdcard/no.jpg") is False

    def test_file_mtime_parses_epoch_and_invalid(self):
        stub = SubprocessStub()
        stub.respond(stdout="1700000000\n")
        stub.respond(stdout="not-an-int\n")
        stub.respond(stdout="")
        with stub:
            adb = phonesync.ADB("SER")
            assert adb.file_mtime("/sdcard/a.jpg") == 1700000000
            assert adb.file_mtime("/sdcard/bad.jpg") is None
            assert adb.file_mtime("/sdcard/missing.jpg") is None

    def test_list_files_wrapper_uses_maxdepth_one(self):
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS\n")
        stub.respond(stdout="10\0" "1700000000\0" "/sdcard/DCIM/top.jpg\n")
        with stub:
            adb = phonesync.ADB("SER")
            files = adb.list_files("/sdcard/DCIM")

        assert len(files) == 1
        find_cmd = stub.calls[1]["cmd"][-1]
        assert "-maxdepth 1" in find_cmd

    def test_list_storage_volumes_internal_only(self):
        with SubprocessStub().respond(stdout="emulated\nself\n") as stub:
            adb = phonesync.ADB("SER")
            vols = adb.list_storage_volumes()

        assert vols == [{
            "type": "internal",
            "path": "/sdcard",
            "label": "Internal Storage",
        }]
        assert stub.last_cmd == ["adb", "-s", "SER", "shell", "ls -1 /storage/"]

    def test_list_storage_volumes_external_sd(self):
        stub = SubprocessStub()
        stub.respond(stdout="emulated\nself\nABCD-1234\nUSB\n")
        stub.respond(stdout="EXISTS\n")
        stub.respond(stdout="MISSING\n")
        with stub:
            adb = phonesync.ADB("SER")
            vols = adb.list_storage_volumes()

        assert any(v["path"] == "/storage/ABCD-1234" for v in vols)
        assert any(v["type"] == "external_sd" for v in vols)
        assert not any(v.get("id") == "USB" for v in vols)

    def test_list_storage_volumes_timeout_keeps_internal(self):
        exc = subprocess.TimeoutExpired(["adb"], timeout=120)
        with SubprocessStub().respond_exception(exc):
            adb = phonesync.ADB("SER")
            vols = adb.list_storage_volumes()

        assert vols == [{
            "type": "internal",
            "path": "/sdcard",
            "label": "Internal Storage",
        }]


class TestRealADBTimeoutKwargs:
    def test_shell_default_timeout_and_kwargs(self):
        with SubprocessStub().respond(stdout="ok") as stub:
            adb = phonesync.ADB("SER")
            assert adb.shell("echo ok") == "ok"

        assert stub.last["cmd"] == ["adb", "-s", "SER", "shell", "echo ok"]
        assert stub.last["capture_output"] is True
        assert stub.last["text"] is True
        assert stub.last["timeout"] == 120

    def test_pull_push_use_long_timeout(self):
        stub = SubprocessStub()
        stub.respond(stdout="")
        stub.respond(stdout="")
        with stub:
            adb = phonesync.ADB("SER")
            assert adb.pull("/sdcard/a.jpg", "/tmp/a.jpg") is True
            assert adb.push("/tmp/b.jpg", "/sdcard/b.jpg") is True

        assert stub.calls[0]["timeout"] == 600
        assert stub.calls[1]["timeout"] == 600

    def test_list_files_recursive_find_uses_long_scan_timeout(self):
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS\n")
        stub.respond(stdout="")
        stub.respond(stdout="__PS_OK__\n")
        with stub:
            adb = phonesync.ADB("SER")
            assert adb.list_files_recursive("/sdcard/DCIM/Camera") == []

        assert stub.calls[0]["timeout"] == 120
        assert stub.calls[1]["timeout"] == 300
        assert stub.calls[2]["timeout"] == 15
        assert stub.calls[2]["cmd"] == [
            "adb", "-s", "SER", "shell", "echo __PS_OK__"
        ]
    
    def test_list_connected_devices_uses_short_timeout(self):
        with SubprocessStub().respond(stdout="List of devices attached\n\n") as stub:
            assert phonesync.list_connected_devices() == []

        assert stub.last["cmd"] == ["adb", "devices", "-l"]
        assert stub.last["timeout"] == 10


# ---------------------------------------------------------------------------
# Real ADB: list_files_recursive command safety properties
# ---------------------------------------------------------------------------

class TestRealADBListFilesCommandConstruction:
    def test_find_command_contains_quoted_path_prune_nulls_and_timeout(self):
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS\n")
        stub.respond(stdout="")
        stub.respond(stdout="__PS_OK__\n")
        
        with stub:
            adb = phonesync.ADB("SER")
            adb.list_files_recursive(
                "/sdcard/DCIM/My Camera",
                exclude_dirs=[".thumbs", "bad dir"],
                exclude_files=[],
                max_depth=7,
            )

        assert len(stub.calls) >= 3
        find_cmd = stub.calls[1]["cmd"][-1]
        assert "find '/sdcard/DCIM/My Camera' -maxdepth 7" in find_cmd
        assert "-name .thumbs -prune" in find_cmd
        assert "-name 'bad dir' -prune" in find_cmd
        assert "-type f" in find_cmd
        assert "-exec sh -c" in find_cmd
        assert "printf" in find_cmd
        assert "\\0" in find_cmd
        assert stub.calls[1]["timeout"] == 300
        assert stub.calls[2]["cmd"][-1] == "echo __PS_OK__"
        assert stub.calls[2]["timeout"] == 15

    def test_find_command_without_prune_is_simpler(self):
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS\n")
        stub.respond(stdout="")
        stub.respond(stdout="__PS_OK__\n")

        with stub:
            adb = phonesync.ADB("SER")
            adb.list_files_recursive(
                "/sdcard/DCIM/Camera",
                exclude_dirs=[],
                exclude_files=[],
                max_depth=3,
            )

        assert len(stub.calls) >= 3
        find_cmd = stub.calls[1]["cmd"][-1]
        assert "\\(" not in find_cmd
        assert "-maxdepth 3" in find_cmd
        assert "-type f" in find_cmd
        assert "-exec sh -c" in find_cmd
        assert "printf" in find_cmd
        assert "\\0" in find_cmd
        assert stub.calls[1]["timeout"] == 300
        assert stub.calls[2]["cmd"][-1] == "echo __PS_OK__"
        assert stub.calls[2]["timeout"] == 15
    
    def test_list_files_recursive_empty_find_unreachable_raises(self):
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS\n")
        stub.respond(stdout="")
        stub.respond(stdout="")  # reachability probe does not return marker
        with stub:
            adb = phonesync.ADB("SER")
            with pytest.raises(phonesync.ADBError):
                adb.list_files_recursive("/sdcard/DCIM/Camera")

        assert stub.calls[2]["cmd"] == [
            "adb", "-s", "SER", "shell", "echo __PS_OK__"
        ]
        assert stub.calls[2]["timeout"] == 15
        


# ---------------------------------------------------------------------------
# Real ADB: move_safe data-safety branches
# ---------------------------------------------------------------------------

class TestRealADBMoveSafeRedTeam:
    GOOD_HASH = "a" * 64
    BAD_HASH = "b" * 64

    def test_move_safe_destination_free_hash_matches(self):
        stub = SubprocessStub()
        stub.respond(stdout="FREE\n")
        stub.respond(stdout="")
        stub.respond(stdout="")
        stub.respond(stdout=f"{self.GOOD_HASH}  /sdcard/dst.jpg\n")
        stub.respond(stdout="")
        with stub:
            adb = phonesync.ADB("SER")
            result = adb.move_safe(
                "/sdcard/src.jpg", "/sdcard/dst.jpg", self.GOOD_HASH
            )

        assert result == {"ok": True, "action": "moved", "source_deleted": True}
        assert [c["cmd"][-1] for c in stub.calls] == [
            "[ -e /sdcard/dst.jpg ] && echo EXISTS || echo FREE",
            "mkdir -p /sdcard",
            "cp /sdcard/src.jpg /sdcard/dst.jpg",
            "sha256sum /sdcard/dst.jpg",
            "rm /sdcard/src.jpg",
        ]

    def test_move_safe_destination_exists_same_hash_removes_source(self):
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS\n")
        stub.respond(stdout=f"{self.GOOD_HASH}  /sdcard/dst.jpg\n")
        stub.respond(stdout="")
        with stub:
            adb = phonesync.ADB("SER")
            result = adb.move_safe(
                "/sdcard/src.jpg", "/sdcard/dst.jpg", self.GOOD_HASH
            )

        assert result == {
            "ok": True,
            "action": "already_there",
            "source_deleted": True,
        }
        assert stub.calls[-1]["cmd"][-1] == "rm /sdcard/src.jpg"

    def test_move_safe_destination_exists_same_hash_source_rm_fails(self):
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS\n")
        stub.respond(stdout=f"{self.GOOD_HASH}  /sdcard/dst.jpg\n")
        stub.respond(returncode=1, stderr="rm failed")
        with stub:
            adb = phonesync.ADB("SER")
            result = adb.move_safe(
                "/sdcard/src.jpg", "/sdcard/dst.jpg", self.GOOD_HASH
            )

        assert result == {
            "ok": True,
            "action": "already_there",
            "source_deleted": False,
        }

    def test_move_safe_destination_exists_different_hash_collision(self):
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS\n")
        stub.respond(stdout=f"{self.BAD_HASH}  /sdcard/dst.jpg\n")
        with stub:
            adb = phonesync.ADB("SER")
            result = adb.move_safe(
                "/sdcard/src.jpg", "/sdcard/dst.jpg", self.GOOD_HASH
            )

        assert result == {"ok": False, "action": "collision", "source_deleted": False}
        assert len(stub.calls) == 2

    def test_move_safe_copy_failure_cleans_destination(self):
        stub = SubprocessStub()
        stub.respond(stdout="FREE\n")
        stub.respond(stdout="")
        stub.respond(returncode=1, stderr="cp failed")
        stub.respond(stdout="")
        with stub:
            adb = phonesync.ADB("SER")
            result = adb.move_safe(
                "/sdcard/src.jpg", "/sdcard/dst.jpg", self.GOOD_HASH
            )

        assert result == {
            "ok": False,
            "action": "copy_failed",
            "source_deleted": False,
        }
        assert stub.calls[-1]["cmd"][-1] == "rm -f /sdcard/dst.jpg"

    def test_move_safe_hash_mismatch_removes_bad_copy_keeps_source(self):
        stub = SubprocessStub()
        stub.respond(stdout="FREE\n")
        stub.respond(stdout="")
        stub.respond(stdout="")
        stub.respond(stdout=f"{self.BAD_HASH}  /sdcard/dst.jpg\n")
        stub.respond(stdout="")
        with stub:
            adb = phonesync.ADB("SER")
            result = adb.move_safe(
                "/sdcard/src.jpg", "/sdcard/dst.jpg", self.GOOD_HASH
            )

        assert result == {
            "ok": False,
            "action": "hash_mismatch",
            "source_deleted": False,
        }
        assert stub.calls[-1]["cmd"][-1] == "rm -f /sdcard/dst.jpg"

    def test_move_safe_verified_copy_source_rm_fails_still_ok(self):
        stub = SubprocessStub()
        stub.respond(stdout="FREE\n")
        stub.respond(stdout="")
        stub.respond(stdout="")
        stub.respond(stdout=f"{self.GOOD_HASH}  /sdcard/dst.jpg\n")
        stub.respond(returncode=1, stderr="rm failed")
        with stub:
            adb = phonesync.ADB("SER")
            result = adb.move_safe(
                "/sdcard/src.jpg", "/sdcard/dst.jpg", self.GOOD_HASH
            )

        assert result == {"ok": True, "action": "moved", "source_deleted": False}

    def test_move_safe_no_expected_hash_skips_hash_verification(self):
        stub = SubprocessStub()
        stub.respond(stdout="FREE\n")
        stub.respond(stdout="")
        stub.respond(stdout="")
        stub.respond(stdout="")
        with stub:
            adb = phonesync.ADB("SER")
            result = adb.move_safe("/sdcard/src.jpg", "/sdcard/dst.jpg", None)

        assert result == {"ok": True, "action": "moved", "source_deleted": True}
        assert not any("sha256sum" in c["cmd"][-1] for c in stub.calls)


# ---------------------------------------------------------------------------
# Real ADB: device-list error paths
# ---------------------------------------------------------------------------

class TestListConnectedDevicesRedTeam:
    def test_adb_executable_missing_returns_empty(self):
        with SubprocessStub().respond_exception(FileNotFoundError("adb")):
            assert phonesync.list_connected_devices() == []

    def test_adb_devices_timeout_returns_empty(self):
        exc = subprocess.TimeoutExpired(["adb", "devices", "-l"], timeout=10)
        with SubprocessStub().respond_exception(exc):
            assert phonesync.list_connected_devices() == []

    def test_malformed_output_returns_empty(self):
        with SubprocessStub().respond(stdout="garbage\nnot enough\n"):
            assert phonesync.list_connected_devices() == []

    def test_line_with_device_word_but_not_state_device_is_skipped(self):
        stdout = (
            "List of devices attached\n"
            "ABC123 unauthorized model:device_named_phone\n"
            "DEF456 recovery model:Another_device\n"
        )
        with SubprocessStub().respond(stdout=stdout):
            assert phonesync.list_connected_devices() == []


# ---------------------------------------------------------------------------
# Optional live ADB smoke test. Skipped unless explicitly enabled.
# ---------------------------------------------------------------------------

def test_live_adb_push_hash_pull_delete_smoke(tmp_path):
    """Opt-in smoke test against a real connected device.

    Run manually with:
        PHONESYNC_RUN_LIVE_ADB=1 pytest test_redteaming_2.py::test_live_adb_push_hash_pull_delete_smoke
    """
    if os.environ.get("PHONESYNC_RUN_LIVE_ADB") != "1":
        pytest.skip("Set PHONESYNC_RUN_LIVE_ADB=1 to run live ADB smoke test")

    devices = phonesync.list_connected_devices()
    if not devices:
        pytest.skip("No live ADB device connected")

    serial = devices[0]["serial"]
    adb = phonesync.ADB(serial)
    remote_dir = "/sdcard/phonesync_redteam_tmp"
    remote_file = f"{remote_dir}/roundtrip.bin"
    local_src = tmp_path / "src.bin"
    local_dst = tmp_path / "dst.bin"
    data = bytes(range(256)) * 2
    local_src.write_bytes(data)

    try:
        assert adb.mkdir(remote_dir) is True
        assert adb.push(str(local_src), remote_file) is True
        assert adb.file_exists(remote_file) is True
        assert adb.file_hash(remote_file) == hashlib.sha256(data).hexdigest()
        assert adb.pull(remote_file, str(local_dst)) is True
        assert local_dst.read_bytes() == data
    finally:
        adb.delete(remote_file)
        adb.shell(f"rmdir {adb._q(remote_dir)}", check=False)
