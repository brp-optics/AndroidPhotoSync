"""Tests for the real ADB class using subprocess stubs.

These verify that ADB methods produce correct subprocess.run calls
and parse output correctly, without needing a real phone.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import phonesync


# ---------------------------------------------------------------------------
# SubprocessStub
# ---------------------------------------------------------------------------

class SubprocessStub:
    """Records subprocess.run calls and returns configurable results."""

    def __init__(self):
        self.calls = []
        self._responses = []
        self._default = (0, "", "")
        self._original = None

    def respond(self, returncode=0, stdout="", stderr=""):
        self._responses.append((returncode, stdout, stderr))
        return self

    def respond_default(self, returncode=0, stdout="", stderr=""):
        self._default = (returncode, stdout, stderr)
        return self

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        self.calls.append(cmd)
        if self._responses:
            rc, out, err = self._responses.pop(0)
        else:
            rc, out, err = self._default
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def install(self):
        self._original = phonesync.subprocess.run
        phonesync.subprocess.run = self
        return self

    def restore(self):
        if self._original:
            phonesync.subprocess.run = self._original
            self._original = None

    @property
    def last_call(self):
        return self.calls[-1] if self.calls else None


# ---------------------------------------------------------------------------
# _run() basics
# ---------------------------------------------------------------------------

class TestADBRun:
    def test_constructs_correct_command(self):
        stub = SubprocessStub().respond_default().install()
        try:
            adb = phonesync.ADB("ABC123")
            adb._run(["pull", "/sdcard/a.jpg", "/tmp/a.jpg"])
            assert stub.last_call == [
                "adb", "-s", "ABC123",
                "pull", "/sdcard/a.jpg", "/tmp/a.jpg"]
        finally:
            stub.restore()

    def test_nonzero_return_check_true_raises(self):
        stub = SubprocessStub().respond(returncode=1, stderr="error").install()
        try:
            adb = phonesync.ADB("SER")
            raised = False
            try:
                adb._run(["shell", "ls"], check=True)
            except phonesync.ADBError:
                raised = True
            assert raised
        finally:
            stub.restore()

    def test_nonzero_return_check_false_ok(self):
        stub = SubprocessStub().respond(returncode=1, stderr="err").install()
        try:
            adb = phonesync.ADB("SER")
            result = adb._run(["shell", "ls"], check=False)
            assert result.returncode == 1
        finally:
            stub.restore()

    def test_timeout_raises_adberror(self):
        def timeout_run(cmd, capture_output=True, text=True, timeout=None):
            raise subprocess.TimeoutExpired(cmd, timeout)
        original = phonesync.subprocess.run
        phonesync.subprocess.run = timeout_run
        try:
            adb = phonesync.ADB("SER")
            raised = False
            try:
                adb._run(["shell", "slow_cmd"])
            except phonesync.ADBError as e:
                raised = True
                assert "timed out" in str(e)
            assert raised
        finally:
            phonesync.subprocess.run = original


# ---------------------------------------------------------------------------
# shell()
# ---------------------------------------------------------------------------

class TestADBShell:
    def test_shell_returns_stdout(self):
        stub = SubprocessStub().respond(stdout="hello world\n").install()
        try:
            adb = phonesync.ADB("SER")
            out = adb.shell("echo hello world")
            assert out == "hello world\n"
            assert stub.last_call == [
                "adb", "-s", "SER", "shell", "echo hello world"]
        finally:
            stub.restore()

    def test_shell_check_false_on_failure(self):
        stub = SubprocessStub().respond(
            returncode=1, stdout="partial", stderr="err").install()
        try:
            adb = phonesync.ADB("SER")
            out = adb.shell("bad_cmd", check=False)
            assert out == "partial"
        finally:
            stub.restore()


# ---------------------------------------------------------------------------
# pull() and push()
# ---------------------------------------------------------------------------

class TestADBPullPush:
    def test_pull_command(self):
        stub = SubprocessStub().respond_default().install()
        try:
            adb = phonesync.ADB("SER")
            ok = adb.pull("/sdcard/photo.jpg", "/tmp/photo.jpg")
            assert ok is True
            assert stub.last_call == [
                "adb", "-s", "SER",
                "pull", "/sdcard/photo.jpg", "/tmp/photo.jpg"]
        finally:
            stub.restore()

    def test_pull_failure_returns_false(self):
        stub = SubprocessStub().respond(
            returncode=1, stderr="not found").install()
        try:
            adb = phonesync.ADB("SER")
            ok = adb.pull("/sdcard/no.jpg", "/tmp/x.jpg")
            assert ok is False
        finally:
            stub.restore()

    def test_push_command(self):
        stub = SubprocessStub().respond_default().install()
        try:
            adb = phonesync.ADB("SER")
            ok = adb.push("/tmp/local.jpg", "/sdcard/remote.jpg")
            assert ok is True
            assert stub.last_call == [
                "adb", "-s", "SER",
                "push", "/tmp/local.jpg", "/sdcard/remote.jpg"]
        finally:
            stub.restore()

    def test_push_failure_returns_false(self):
        stub = SubprocessStub().respond(
            returncode=1, stderr="read-only").install()
        try:
            adb = phonesync.ADB("SER")
            ok = adb.push("/tmp/x.jpg", "/sdcard/x.jpg")
            assert ok is False
        finally:
            stub.restore()


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------

class TestADBDelete:
    def test_delete_command(self):
        stub = SubprocessStub().respond_default().install()
        try:
            adb = phonesync.ADB("SER")
            ok = adb.delete("/sdcard/DCIM/Camera/old.jpg")
            assert ok is True
            shell_cmd = stub.last_call[-1]
            assert shell_cmd.startswith("rm ")
            assert "/sdcard/DCIM/Camera/old.jpg" in shell_cmd
        finally:
            stub.restore()

    def test_delete_quotes_special_chars(self):
        stub = SubprocessStub().respond_default().install()
        try:
            adb = phonesync.ADB("SER")
            adb.delete("/sdcard/DCIM/Camera/my photo (1).jpg")
            cmd = stub.last_call
            # Should be shlex-quoted
            assert "rm " in " ".join(cmd)
            assert "my photo (1).jpg" not in cmd[-1].split()  # not split by spaces
        finally:
            stub.restore()

    def test_delete_failure_returns_false(self):
        stub = SubprocessStub().respond(
            returncode=1, stderr="No such file").install()
        try:
            adb = phonesync.ADB("SER")
            ok = adb.delete("/sdcard/no.jpg")
            assert ok is False
        finally:
            stub.restore()


# ---------------------------------------------------------------------------
# mkdir()
# ---------------------------------------------------------------------------

class TestADBMkdir:
    def test_mkdir_command(self):
        stub = SubprocessStub().respond_default().install()
        try:
            adb = phonesync.ADB("SER")
            ok = adb.mkdir("/sdcard/DCIM/Camera/vacation")
            assert ok is True
            shell_cmd = stub.last_call[-1]
            assert "mkdir -p" in shell_cmd
            assert "/sdcard/DCIM/Camera/vacation" in shell_cmd
        finally:
            stub.restore()

    def test_mkdir_failure_returns_false(self):
        stub = SubprocessStub().respond(
            returncode=1, stderr="Permission denied").install()
        try:
            adb = phonesync.ADB("SER")
            ok = adb.mkdir("/sdcard/readonly/dir")
            assert ok is False
        finally:
            stub.restore()


# ---------------------------------------------------------------------------
# move()
# ---------------------------------------------------------------------------

class TestADBMove:
    def test_move_issues_mkdir_then_mv(self):
        stub = SubprocessStub().respond_default().install()
        try:
            adb = phonesync.ADB("SER")
            ok = adb.move(
                "/sdcard/DCIM/Camera/IMG.jpg",
                "/sdcard/DCIM/Camera/vacation/IMG.jpg")
            assert ok is True
            # Should have 2 calls: mkdir -p for parent, then mv
            assert len(stub.calls) == 2
            mkdir_cmd = stub.calls[0][-1]  # shell command string
            mv_cmd = stub.calls[1][-1]
            assert "mkdir -p" in mkdir_cmd
            assert "mv " in mv_cmd
        finally:
            stub.restore()

    def test_move_failure_returns_false(self):
        stub = SubprocessStub().respond(
            returncode=1, stderr="err").install()
        try:
            adb = phonesync.ADB("SER")
            ok = adb.move("/sdcard/a.jpg", "/sdcard/b.jpg")
            assert ok is False
        finally:
            stub.restore()


# ---------------------------------------------------------------------------
# move_safe()
# ---------------------------------------------------------------------------

class TestADBMoveSafe:
    def test_move_safe_normal_flow(self):
        """Normal flow: dest free → mkdir → cp → sha256sum → rm."""
        stub = SubprocessStub()
        stub.respond(stdout="FREE")          # [ -e dst ] check
        stub.respond(stdout="")              # mkdir -p
        stub.respond(stdout="")              # cp
        stub.respond(stdout="abc123  /dst")  # sha256sum
        stub.respond(stdout="")              # rm source
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            r = adb.move_safe("/sdcard/src.jpg", "/sdcard/dst.jpg", "abc123")
            assert r["ok"] is True
            assert r["action"] == "moved"
            assert len(stub.calls) >= 4
        finally:
            stub.restore()

    def test_move_safe_dest_exists_same_hash(self):
        """Dest exists with matching hash → delete source."""
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS")        # [ -e dst ] check
        stub.respond(stdout="abc123  /dst")  # sha256sum of dst
        stub.respond(stdout="")              # rm source
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            r = adb.move_safe("/sdcard/src.jpg", "/sdcard/dst.jpg", "abc123")
            assert r["ok"] is True
            assert r["action"] == "already_there"
        finally:
            stub.restore()

    def test_move_safe_dest_exists_different_hash(self):
        """Dest exists with different hash → refuse."""
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS")           # [ -e dst ]
        stub.respond(stdout="different  /dst")  # sha256sum
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            r = adb.move_safe("/sdcard/src.jpg", "/sdcard/dst.jpg", "abc123")
            assert r["ok"] is False
            assert r["action"] == "collision"
        finally:
            stub.restore()

    def test_move_safe_hash_mismatch_after_copy(self):
        """After cp, sha256sum doesn't match → rm -f dst, keep source."""
        stub = SubprocessStub()
        stub.respond(stdout="FREE")             # [ -e dst ]
        stub.respond(stdout="")                 # mkdir -p
        stub.respond(stdout="")                 # cp
        stub.respond(stdout="wrong123  /dst")   # sha256sum (mismatch!)
        stub.respond(stdout="")                 # rm -f dst (cleanup)
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            r = adb.move_safe("/sdcard/src.jpg", "/sdcard/dst.jpg", "abc123")
            assert r["ok"] is False
            assert r["action"] == "hash_mismatch"
        finally:
            stub.restore()

    def test_move_safe_cp_fails(self):
        """If cp fails (nonzero exit), should clean up and return failure."""
        stub = SubprocessStub()
        stub.respond(stdout="FREE")                          # [ -e dst ]
        stub.respond(stdout="")                              # mkdir -p
        stub.respond(returncode=1, stderr="No space left")   # cp fails
        stub.respond(stdout="")                              # rm -f dst
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            r = adb.move_safe("/sdcard/src.jpg", "/sdcard/dst.jpg", "abc123")
            assert r["ok"] is False
            assert r["action"] == "copy_failed"
        finally:
            stub.restore()

    def test_move_safe_no_hash(self):
        """Without expected_hash, skip verification."""
        stub = SubprocessStub()
        stub.respond(stdout="FREE")   # [ -e dst ]
        stub.respond(stdout="")       # mkdir -p
        stub.respond(stdout="")       # cp
        # No sha256sum call
        stub.respond(stdout="")       # rm source
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            r = adb.move_safe("/sdcard/src.jpg", "/sdcard/dst.jpg", None)
            assert r["ok"] is True
            assert r["action"] == "moved"
        finally:
            stub.restore()


# ---------------------------------------------------------------------------
# file_exists()
# ---------------------------------------------------------------------------

class TestADBFileExists:
    def test_file_exists_true(self):
        stub = SubprocessStub().respond(stdout="EXISTS").install()
        try:
            adb = phonesync.ADB("SER")
            assert adb.file_exists("/sdcard/photo.jpg") is True
            shell_cmd = stub.last_call[-1]
            assert "[ -e " in shell_cmd
            assert "EXISTS" in shell_cmd
        finally:
            stub.restore()

    def test_file_exists_false(self):
        stub = SubprocessStub().respond(stdout="MISSING").install()
        try:
            adb = phonesync.ADB("SER")
            assert adb.file_exists("/sdcard/no.jpg") is False
        finally:
            stub.restore()

    def test_file_exists_quotes_path(self):
        stub = SubprocessStub().respond(stdout="EXISTS").install()
        try:
            adb = phonesync.ADB("SER")
            adb.file_exists("/sdcard/my photo (1).jpg")
            shell_cmd = stub.last_call[-1]
            # Path should be shlex-quoted
            import shlex
            assert shlex.quote("/sdcard/my photo (1).jpg") in shell_cmd
        finally:
            stub.restore()


# ---------------------------------------------------------------------------
# file_mtime()
# ---------------------------------------------------------------------------

class TestADBFileMtime:
    def test_file_mtime_parses_output(self):
        stub = SubprocessStub().respond(stdout="1700000000\n").install()
        try:
            adb = phonesync.ADB("SER")
            mtime = adb.file_mtime("/sdcard/photo.jpg")
            assert mtime == 1700000000
            shell_cmd = stub.last_call[-1]
            assert 'stat -c "%Y"' in shell_cmd or "stat -c %Y" in shell_cmd
        finally:
            stub.restore()

    def test_file_mtime_returns_none_on_failure(self):
        stub = SubprocessStub().respond(
            returncode=1, stdout="", stderr="No such file").install()
        try:
            adb = phonesync.ADB("SER")
            assert adb.file_mtime("/sdcard/no.jpg") is None
        finally:
            stub.restore()

    def test_file_mtime_returns_none_on_garbage(self):
        stub = SubprocessStub().respond(stdout="not_a_number\n").install()
        try:
            adb = phonesync.ADB("SER")
            assert adb.file_mtime("/sdcard/photo.jpg") is None
        finally:
            stub.restore()


# ---------------------------------------------------------------------------
# list_storage_volumes()
# ---------------------------------------------------------------------------

class TestADBListStorageVolumes:
    def test_internal_only(self):
        """With no external SD entries, should return just internal."""
        stub = SubprocessStub()
        # ls -1 /storage/
        stub.respond(stdout="emulated\nself\n")
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            vols = adb.list_storage_volumes()
            assert len(vols) == 1
            assert vols[0]["type"] == "internal"
            assert vols[0]["path"] == "/sdcard"
        finally:
            stub.restore()

    def test_with_external_sd(self):
        """External SD card should be detected."""
        stub = SubprocessStub()
        # ls -1 /storage/
        stub.respond(stdout="emulated\nself\nABCD-1234\n")
        # [ -d /storage/ABCD-1234 ] check
        stub.respond(stdout="EXISTS")
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            vols = adb.list_storage_volumes()
            assert len(vols) == 2
            types = {v["type"] for v in vols}
            assert "internal" in types
            assert "external_sd" in types
            sd = [v for v in vols if v["type"] == "external_sd"][0]
            assert sd["path"] == "/storage/ABCD-1234"
            assert "ABCD-1234" in sd["label"]
        finally:
            stub.restore()

    def test_multiple_sd_cards(self):
        stub = SubprocessStub()
        stub.respond(stdout="emulated\nself\nSD1\nSD2\n")
        stub.respond(stdout="EXISTS")  # SD1
        stub.respond(stdout="EXISTS")  # SD2
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            vols = adb.list_storage_volumes()
            sd_vols = [v for v in vols if v["type"] == "external_sd"]
            assert len(sd_vols) == 2
        finally:
            stub.restore()

    def test_sd_dir_missing(self):
        """If /storage/XXX exists in ls but [ -d ] fails, skip it."""
        stub = SubprocessStub()
        stub.respond(stdout="emulated\nself\nGHOST\n")
        stub.respond(stdout="MISSING")  # [ -d /storage/GHOST ]
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            vols = adb.list_storage_volumes()
            assert len(vols) == 1  # only internal
        finally:
            stub.restore()

    def test_ls_failure(self):
        """If ls fails, should still return internal."""
        stub = SubprocessStub()
        stub.respond(returncode=1, stderr="err")
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            vols = adb.list_storage_volumes()
            assert len(vols) >= 1
            assert vols[0]["type"] == "internal"
        finally:
            stub.restore()


# ---------------------------------------------------------------------------
# list_connected_devices()
# ---------------------------------------------------------------------------

class TestListConnectedDevices:
    def test_parses_device_line(self):
        stub = SubprocessStub().respond(
            stdout=(
                "List of devices attached\n"
                "ABC123           device usb:1-2 product:raven "
                "model:Pixel_6_Pro transport_id:1\n"
            )).install()
        try:
            devices = phonesync.list_connected_devices()
            assert len(devices) == 1
            assert devices[0]["serial"] == "ABC123"
            assert devices[0]["model"] == "Pixel_6_Pro"
        finally:
            stub.restore()

    def test_parses_multiple_devices(self):
        stub = SubprocessStub().respond(
            stdout=(
                "List of devices attached\n"
                "SERIAL_A         device model:PhoneA\n"
                "SERIAL_B         device model:PhoneB\n"
            )).install()
        try:
            devices = phonesync.list_connected_devices()
            assert len(devices) == 2
            serials = {d["serial"] for d in devices}
            assert serials == {"SERIAL_A", "SERIAL_B"}
        finally:
            stub.restore()

    def test_skips_offline(self):
        stub = SubprocessStub().respond(
            stdout=(
                "List of devices attached\n"
                "ABC123           offline\n"
                "DEF456           device model:Phone\n"
            )).install()
        try:
            devices = phonesync.list_connected_devices()
            assert len(devices) == 1
            assert devices[0]["serial"] == "DEF456"
        finally:
            stub.restore()

    def test_skips_unauthorized(self):
        stub = SubprocessStub().respond(
            stdout=(
                "List of devices attached\n"
                "ABC123           unauthorized\n"
            )).install()
        try:
            devices = phonesync.list_connected_devices()
            assert len(devices) == 0
        finally:
            stub.restore()

    def test_empty_list(self):
        stub = SubprocessStub().respond(
            stdout="List of devices attached\n\n").install()
        try:
            devices = phonesync.list_connected_devices()
            assert len(devices) == 0
        finally:
            stub.restore()

    def test_model_extraction(self):
        stub = SubprocessStub().respond(
            stdout=(
                "List of devices attached\n"
                "XYZ              device product:oriole "
                "model:Pixel_6 transport_id:3\n"
            )).install()
        try:
            devices = phonesync.list_connected_devices()
            assert devices[0]["model"] == "Pixel_6"
        finally:
            stub.restore()

    def test_no_model_field(self):
        stub = SubprocessStub().respond(
            stdout=(
                "List of devices attached\n"
                "ABC123           device usb:1-2\n"
            )).install()
        try:
            devices = phonesync.list_connected_devices()
            assert len(devices) == 1
            assert devices[0]["model"] == "unknown"
        finally:
            stub.restore()


# ---------------------------------------------------------------------------
# list_files_recursive() output parsing
# ---------------------------------------------------------------------------

class TestADBListFilesParsing:
    def test_parses_null_separated_output(self):
        output = (
            "12345\x001700000000\x00/sdcard/DCIM/Camera/IMG.jpg\n"
            "67890\x001700000001\x00/sdcard/DCIM/Camera/VID.mp4\n"
        )
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS")
        stub.respond(stdout=output)
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            files = adb.list_files_recursive("/sdcard/DCIM/Camera")
            assert len(files) == 2
            assert files[0]["name"] == "IMG.jpg"
            assert files[0]["size"] == 12345
            assert files[0]["mtime_epoch"] == 1700000000
            assert files[0]["path"] == "/sdcard/DCIM/Camera/IMG.jpg"
            assert files[0]["relpath"] == "IMG.jpg"
        finally:
            stub.restore()

    def test_nested_relpath(self):
        output = "100\x001700000000\x00/sdcard/DCIM/Camera/sub/deep/IMG.jpg\n"
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS")
        stub.respond(stdout=output)
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            files = adb.list_files_recursive("/sdcard/DCIM/Camera")
            assert files[0]["relpath"] == "sub/deep/IMG.jpg"
        finally:
            stub.restore()

    def test_skips_malformed_lines(self):
        output = (
            "12345\x001700000000\x00/sdcard/DCIM/Camera/good.jpg\n"
            "not_a_number\n"
            "\n"
            "bad\x00line\n"
        )
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS")
        stub.respond(stdout=output)
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            files = adb.list_files_recursive("/sdcard/DCIM/Camera")
            assert len(files) == 1
            assert files[0]["name"] == "good.jpg"
        finally:
            stub.restore()

    def test_missing_directory_returns_empty(self):
        stub = SubprocessStub()
        stub.respond(stdout="MISSING")
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            files = adb.list_files_recursive("/sdcard/NoDir")
            assert files == []
        finally:
            stub.restore()

    def test_applies_file_exclusions(self):
        output = (
            "100\x001700000000\x00/sdcard/DCIM/Camera/photo.jpg\n"
            "50\x001700000000\x00/sdcard/DCIM/Camera/.nomedia\n"
        )
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS")
        stub.respond(stdout=output)
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            files = adb.list_files_recursive(
                "/sdcard/DCIM/Camera",
                exclude_files=[".nomedia"])
            names = [f["name"] for f in files]
            assert "photo.jpg" in names
            assert ".nomedia" not in names
        finally:
            stub.restore()

    def test_handles_pipe_in_filename(self):
        output = (
            "100\x001700000000\x00"
            "/sdcard/DCIM/Camera/photo|vacation.jpg\n"
        )
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS")
        stub.respond(stdout=output)
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            files = adb.list_files_recursive("/sdcard/DCIM/Camera")
            assert len(files) == 1
            assert files[0]["name"] == "photo|vacation.jpg"
        finally:
            stub.restore()

    def test_list_files_wrapper(self):
        """list_files() should delegate to list_files_recursive(max_depth=1)."""
        output = "100\x001700000000\x00/sdcard/DCIM/Camera/top.jpg\n"
        stub = SubprocessStub()
        stub.respond(stdout="EXISTS")
        stub.respond(stdout=output)
        stub.install()
        try:
            adb = phonesync.ADB("SER")
            files = adb.list_files("/sdcard/DCIM/Camera")
            assert len(files) == 1
        finally:
            stub.restore()


# ---------------------------------------------------------------------------
# file_hash() and get_model() parsing
# ---------------------------------------------------------------------------

class TestADBOutputParsing:
    def test_file_hash_parses_output(self):
        h = "a" * 64
        stub = SubprocessStub().respond(
            stdout=f"{h}  /sdcard/file.jpg\n").install()
        try:
            adb = phonesync.ADB("SER")
            result = adb.file_hash("/sdcard/file.jpg")
            assert result == h
        finally:
            stub.restore()

    def test_file_hash_empty_returns_none(self):
        stub = SubprocessStub().respond(stdout="").install()
        try:
            adb = phonesync.ADB("SER")
            assert adb.file_hash("/sdcard/no.jpg") is None
        finally:
            stub.restore()

    def test_file_hash_quotes_path(self):
        stub = SubprocessStub().respond(stdout="abc  /f\n").install()
        try:
            adb = phonesync.ADB("SER")
            adb.file_hash("/sdcard/my file.jpg")
            shell_cmd = stub.last_call[-1]
            import shlex
            assert shlex.quote("/sdcard/my file.jpg") in shell_cmd
        finally:
            stub.restore()

    def test_get_model(self):
        stub = SubprocessStub().respond(stdout="Pixel 6 Pro\n").install()
        try:
            adb = phonesync.ADB("SER")
            assert adb.get_model() == "Pixel 6 Pro"
        finally:
            stub.restore()
