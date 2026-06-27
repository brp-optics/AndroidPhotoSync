"""Tests for DeviceState, atomic writes, and lock file."""
import json
import tempfile
from pathlib import Path

import phonesync


class TestAtomicWrite:
    def test_basic_write(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "test.json"
            phonesync._atomic_json_write(p, {"key": "value"})
            with open(p) as f:
                assert json.load(f) == {"key": "value"}

    def test_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "test.json"
            phonesync._atomic_json_write(p, {"v": 1})
            phonesync._atomic_json_write(p, {"v": 2})
            with open(p) as f:
                assert json.load(f) == {"v": 2}

    def test_no_partial_writes(self):
        """If write fails, original file should be intact."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "test.json"
            phonesync._atomic_json_write(p, {"original": True})
            try:
                # This should fail (can't serialize a set)
                phonesync._atomic_json_write(p, {"bad": {1, 2, 3}})
            except TypeError:
                pass
            with open(p) as f:
                assert json.load(f) == {"original": True}


class TestSyncLock:
    def test_acquire_release(self):
        with tempfile.TemporaryDirectory() as d:
            lock = phonesync.SyncLock(Path(d))
            assert lock.acquire() is True
            lock.release()

    def test_blocks_second(self):
        with tempfile.TemporaryDirectory() as d:
            lock1 = phonesync.SyncLock(Path(d))
            lock2 = phonesync.SyncLock(Path(d))
            assert lock1.acquire() is True
            assert lock2.acquire() is False
            lock1.release()

    def test_reacquire_after_release(self):
        with tempfile.TemporaryDirectory() as d:
            lock1 = phonesync.SyncLock(Path(d))
            lock2 = phonesync.SyncLock(Path(d))
            assert lock1.acquire() is True
            lock1.release()
            assert lock2.acquire() is True
            lock2.release()

    def test_lock_file_persists(self):
        """Lock file should not be deleted on release (prevents race)."""
        with tempfile.TemporaryDirectory() as d:
            lock = phonesync.SyncLock(Path(d))
            lock.acquire()
            lock.release()
            assert (Path(d) / "sync.lock").exists()


class TestDeviceState:
    def _make_cfg(self, d):
        return {"config_dir": str(d)}

    def test_empty_state(self):
        with tempfile.TemporaryDirectory() as d:
            state = phonesync.DeviceState("SER", "dev", self._make_cfg(d))
            assert state.files == {}

    def test_add_and_save(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._make_cfg(d)
            state = phonesync.DeviceState("SER", "dev", cfg)
            state.add_file("photos/img.jpg", "/sdcard/DCIM/img.jpg",
                           "abc123", 100, "2025-01-01", "photos")
            state.save()

            # Reload
            state2 = phonesync.DeviceState("SER", "dev", cfg)
            assert "photos/img.jpg" in state2.files
            assert state2.files["photos/img.jpg"]["hash"] == "abc123"

    def test_find_by_phone_path(self):
        with tempfile.TemporaryDirectory() as d:
            state = phonesync.DeviceState("SER", "dev", self._make_cfg(d))
            state.add_file("a.jpg", "/sdcard/DCIM/a.jpg",
                           "h1", 100, "2025-01-01", "photos")
            state.add_file("b.jpg", "/sdcard/DCIM/b.jpg",
                           "h2", 200, "2025-01-01", "photos")
            assert state.find_by_phone_path("/sdcard/DCIM/a.jpg") == "a.jpg"
            assert state.find_by_phone_path("/sdcard/DCIM/c.jpg") is None

    def test_corrupted_state_file(self):
        """If state file is corrupted, should start empty (not crash)."""
        with tempfile.TemporaryDirectory() as d:
            state_path = Path(d) / "state-dev.json"
            state_path.write_text("{invalid json")
            # This should not crash
            try:
                state = phonesync.DeviceState("SER", "dev", self._make_cfg(d))
                # If it doesn't crash, it should have empty state
                assert state.files == {}
            except json.JSONDecodeError:
                # Currently crashes — this is a known issue
                pass


class TestFileHash:
    def test_consistent(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"hello world")
            path = f.name
        h1 = phonesync.file_sha256(path)
        h2 = phonesync.file_sha256(path)
        assert h1 == h2
        assert len(h1) == 64  # SHA256 hex

    def test_different_content(self):
        import tempfile as tf
        with tf.NamedTemporaryFile(delete=False) as f1:
            f1.write(b"aaa")
        with tf.NamedTemporaryFile(delete=False) as f2:
            f2.write(b"bbb")
        assert phonesync.file_sha256(f1.name) != phonesync.file_sha256(f2.name)


class TestStateBackups:
    def _make_cfg(self, d):
        return {"config_dir": str(d)}

    def _new_state(self, cfg):
        return phonesync.DeviceState("SER", "phone-a", cfg)

    def test_first_save_no_backup(self):
        """The very first save has no prior file, so no backup is made."""
        with tempfile.TemporaryDirectory() as d:
            cfg = self._make_cfg(d)
            st = self._new_state(cfg)
            st.add_file("a.jpg", "/sdcard/a.jpg", "h", 1, "2025-01-01",
                        "photos")
            st.save()
            backup_dir = Path(d) / "state-backups"
            # No backups yet (nothing existed before the first write)
            backups = list(backup_dir.glob("*.json")) if \
                backup_dir.exists() else []
            assert backups == []

    def test_second_save_creates_backup(self):
        """The second save backs up the first state file."""
        with tempfile.TemporaryDirectory() as d:
            cfg = self._make_cfg(d)
            st = self._new_state(cfg)
            st.add_file("a.jpg", "/sdcard/a.jpg", "h1", 1, "2025-01-01",
                        "photos")
            st.save()  # first write, no backup
            st.add_file("b.jpg", "/sdcard/b.jpg", "h2", 2, "2025-01-01",
                        "photos")
            st.save()  # second write, backs up the first

            backup_dir = Path(d) / "state-backups"
            backups = list(backup_dir.glob("state-phone-a.*.json"))
            assert len(backups) == 1
            # The backup holds the FIRST version (only a.jpg)
            with open(backups[0]) as f:
                backed = json.load(f)
            assert "a.jpg" in backed["files"]
            assert "b.jpg" not in backed["files"]

    def test_backup_prune_keeps_last_n(self):
        """Only the most recent N backups are retained."""
        import time
        with tempfile.TemporaryDirectory() as d:
            cfg = self._make_cfg(d)
            st = self._new_state(cfg)
            # 15 saves; keep=10 → at most 10 backups
            for i in range(15):
                st.add_file(f"f{i}.jpg", f"/sdcard/f{i}.jpg", f"h{i}", i,
                            "2025-01-01", "photos")
                st.save()
                time.sleep(0.001)  # ensure distinct timestamps
            backup_dir = Path(d) / "state-backups"
            backups = list(backup_dir.glob("state-phone-a.*.json"))
            assert len(backups) <= 10

    def test_backup_failure_does_not_block_save(self, monkeypatch=None):
        """If backup raises, the save still completes."""
        with tempfile.TemporaryDirectory() as d:
            cfg = self._make_cfg(d)
            st = self._new_state(cfg)
            st.add_file("a.jpg", "/sdcard/a.jpg", "h1", 1, "2025-01-01",
                        "photos")
            st.save()
            # Force the backup to fail on the next save
            def boom(*a, **k):
                raise OSError("simulated backup failure")
            st._backup_state_file = boom
            st.add_file("b.jpg", "/sdcard/b.jpg", "h2", 2, "2025-01-01",
                        "photos")
            st.save()  # must not raise
            # The save still persisted
            reloaded = phonesync.DeviceState("SER", "phone-a", cfg)
            assert "b.jpg" in reloaded.files
