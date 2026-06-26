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
# Helper: subprocess stub
# ---------------------------------------------------------------------------

class SubprocessStub:
    """Records subprocess.run calls and returns configurable results."""

    def __init__(self):
        self.calls = []
        self._responses = []  # stack of (returncode, stdout, stderr)
        self._default = (0, "", "")
        self._original = None

    def respond(self, returncode=0, stdout="", stderr=""):
        """Queue a response for the next subprocess.run call."""
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
        result = subprocess.CompletedProcess(cmd, rc, out, err)
        return result

    def install(self):
        """Monkey-patch subprocess.run in phonesync module."""
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
# shell() output parsing
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


# ---------------------------------------------------------------------------
# list_connected_devices() parsing
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
        """Verify parsing of the stat+printf null-separated format."""
        # Simulate the output our find+stat+printf command produces
        output = (
            "12345\x001700000000\x00/sdcard/DCIM/Camera/IMG.jpg\n"
            "67890\x001700000001\x00/sdcard/DCIM/Camera/VID.mp4\n"
        )
        stub = SubprocessStub()
        # First call: [ -d ... ] check
        stub.respond(stdout="EXISTS")
        # Second call: find+stat
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
            assert files[1]["name"] == "VID.mp4"
            assert files[1]["size"] == 67890
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
        """Filenames with | should parse correctly with null separator."""
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
            assert files[0]["size"] == 100
        finally:
            stub.restore()


# ---------------------------------------------------------------------------
# ADB.file_hash() and get_model() parsing
# ---------------------------------------------------------------------------

class TestADBOutputParsing:
    def test_file_hash_parses_output(self):
        stub = SubprocessStub().respond(
            stdout="abcdef1234567890abcdef1234567890"
                   "abcdef1234567890abcdef1234567890  "
                   "/sdcard/file.jpg\n").install()
        try:
            adb = phonesync.ADB("SER")
            h = adb.file_hash("/sdcard/file.jpg")
            assert h == ("abcdef1234567890abcdef1234567890"
                         "abcdef1234567890abcdef1234567890")
        finally:
            stub.restore()

    def test_file_hash_empty_returns_none(self):
        stub = SubprocessStub().respond(stdout="").install()
        try:
            adb = phonesync.ADB("SER")
            assert adb.file_hash("/sdcard/no.jpg") is None
        finally:
            stub.restore()

    def test_get_model(self):
        stub = SubprocessStub().respond(stdout="Pixel 6 Pro\n").install()
        try:
            adb = phonesync.ADB("SER")
            assert adb.get_model() == "Pixel 6 Pro"
        finally:
            stub.restore()
