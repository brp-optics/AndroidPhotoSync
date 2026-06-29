"""
Behavioral tests for phonesync.SyncEngine using FakeADB.

These are higher-level scenario tests. They intentionally exercise SyncEngine
through TestHarness.sync() instead of calling private phase methods unless a
behavior is hard to reach through a full sync.

Assumptions:
  - test/conftest.py provides TestHarness, FakeADB, and the `harness` fixture.
  - phonesync.SyncEngine accepts `adb_cls=FakeADB` (as in the current code).
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

import phonesync


# ---------------------------------------------------------------------------
# Small test helpers
# ---------------------------------------------------------------------------

def _save_cfg(h):
    """Persist harness config changes before the next h.sync()."""
    phonesync.save_config(h.cfg)


def _state_files(h, phone: str = "a") -> dict:
    return h.get_state(phone).get("files", {})


def _single_state_entry(h, phone: str = "a") -> tuple[str, dict]:
    files = _state_files(h, phone)
    assert len(files) == 1, files
    return next(iter(files.items()))


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mtime(year: int, month: int = 1, day: int = 1) -> float:
    return datetime(year, month, day, 12, 0, 0).timestamp()


def _write_state(h, phone: str, state: dict) -> None:
    name = "phone-a" if phone == "a" else "phone-b"
    path = h.cfg_dir / f"state-{name}.json"
    path.write_text(json.dumps(state, indent=2))


def _make_exif_jpeg_bytes(year: int, month: int, day: int) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    import io

    img = Image.new("RGB", (1, 1), (255, 0, 0))
    exif = Image.Exif()
    exif[36867] = f"{year:04d}:{month:02d}:{day:02d} 05:06:07"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# P0: initial ingest
# ---------------------------------------------------------------------------

class TestSyncEngineInitialIngest:
    def test_initial_photo_ingest_creates_local_file_and_state(self, harness):
        h = harness
        content = b"photo-a"
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", content,
            mtime=_mtime(2025, 1, 15),
        )

        engine = h.sync("a")

        relpath = "photos/2025/IMG_20250115.jpg"
        assert h.computer_exists(relpath)
        assert h.computer_read(relpath) == content
        assert engine.stats["files_copied"] == 1
        assert engine.stats["errors"] == 0

        state = _state_files(h, "a")
        assert set(state) == {relpath}
        info = state[relpath]
        assert info["phone_path"] == "/sdcard/DCIM/Camera/IMG_20250115.jpg"
        assert info["phone_source_dir"] == "/sdcard/DCIM/Camera"
        assert info["hash"] == _hash(content)
        assert info["size"] == len(content)
        assert info["category"] == "photos"
        assert info["device_name"] == "phone-a"

    def test_second_sync_skips_already_synced_file(self, harness):
        h = harness
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", b"photo",
            mtime=_mtime(2025, 1, 15),
        )
        first = h.sync("a")
        second = h.sync("a")

        assert first.stats["files_copied"] == 1
        assert second.stats["files_copied"] == 0
        assert second.stats["files_skipped"] == 1
        assert h.state_file_count("a") == 1
        assert h.computer_list() == ["photos/2025/IMG_20250115.jpg"]

    def test_download_goes_under_downloads_device_name(self, harness):
        h = harness
        h.phone_write("a", "Download/archive.weird", b"download data")

        engine = h.sync("a")

        relpath = "downloads/phone-a/archive.weird"
        assert h.computer_exists(relpath)
        assert h.computer_read(relpath) == b"download data"
        assert _state_files(h, "a")[relpath]["category"] == "downloads"
        assert engine.stats["files_copied"] == 1

    def test_recording_goes_under_recordings_device_name(self, harness):
        h = harness
        h.phone_write("a", "Recordings/voice.m4a", b"recording data")

        engine = h.sync("a")

        relpath = "recordings/phone-a/voice.m4a"
        assert h.computer_exists(relpath)
        assert h.computer_read(relpath) == b"recording data"
        assert _state_files(h, "a")[relpath]["category"] == "recordings"
        assert engine.stats["files_copied"] == 1


# ---------------------------------------------------------------------------
# P0: duplicates and two-device collision safety
# ---------------------------------------------------------------------------

class TestSyncEngineDuplicates:
    def test_same_phone_same_run_duplicate_content_keeps_both(self, harness):
        h = harness
        content = b"same duplicate content"
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", content,
            mtime=_mtime(2025, 1, 15),
        )
        h.phone_write(
            "a", "DCIM/Camera/Sub/IMG_20250115.jpg", content,
            mtime=_mtime(2025, 1, 15),
        )
        
        engine = h.sync("a")
        
        assert engine.stats["files_copied"] == 2
        assert h.computer_exists("photos/2025/IMG_20250115.jpg")
        assert h.computer_exists("photos/Sub/IMG_20250115.jpg")
        assert h.state_file_count("a") == 2

    def test_existing_library_content_does_not_suppress_normal_new_phone_file(
            self, harness
):
        h = harness
        content = b"already in library"

        h.computer_write("photos/2025/existing.jpg", content)
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", content,
            mtime=_mtime(2025, 1, 15),
        )

        engine = h.sync("a")

        assert engine.stats["files_copied"] == 1
        assert engine.stats["move_completions"] == 0
        assert h.computer_exists("photos/2025/existing.jpg")
        assert h.computer_exists("photos/2025/IMG_20250115.jpg")

    def test_two_phones_same_filename_different_content_are_collision_safe(
        self, harness
    ):
        h = harness
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", b"from phone a",
            mtime=_mtime(2025, 1, 15),
        )
        h.phone_write(
            "b", "DCIM/Camera/IMG_20250115.jpg", b"from phone b",
            mtime=_mtime(2025, 1, 15),
        )

        h.sync("a")
        h.sync("b")

        assert h.computer_read("photos/2025/IMG_20250115.jpg") == b"from phone a"
        assert h.computer_read(
            "photos/2025/IMG_20250115_phone-b.jpg"
        ) == b"from phone b"
        assert h.state_file_count("a") == 1
        assert h.state_file_count("b") == 1

    def test_two_phones_same_filename_same_content_are_collision_safe(
        self, harness
    ):
        h = harness
        content = b"same content on both phones"
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", content,
            mtime=_mtime(2025, 1, 15),
        )
        h.phone_write(
            "b", "DCIM/Camera/IMG_20250115.jpg", content,
            mtime=_mtime(2025, 1, 15),
        )

        h.sync("a")
        h.sync("b")

        assert h.computer_read("photos/2025/IMG_20250115.jpg") == content
        assert h.computer_read("photos/2025/IMG_20250115_phone-b.jpg") == content
        assert h.state_file_count("a") == 1
        assert h.state_file_count("b") == 1


# ---------------------------------------------------------------------------
# P0: phone-side moves
# ---------------------------------------------------------------------------

class TestSyncEnginePhoneSideMoves:
    def test_phone_side_move_updates_state_without_redownload(self, harness):
        h = harness
        content = b"phone move content"
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", content,
            mtime=_mtime(2025, 1, 15),
        )
        h.sync("a")

        h.phone_move(
            "a", "DCIM/Camera/IMG_20250115.jpg",
            "DCIM/Camera/Album/IMG_20250115.jpg",
        )
        engine = h.sync("a")

        relpath = "photos/2025/IMG_20250115.jpg"
        assert engine.stats["phone_moves_detected"] == 1
        assert engine.stats["files_copied"] == 0
        assert h.state_file_count("a") == 1
        assert h.state_phone_path(
            "a", relpath
        ) == "/sdcard/DCIM/Camera/Album/IMG_20250115.jpg"
        assert h.computer_read(relpath) == content

    def test_same_hash_tracked_path_is_not_stolen_as_move_target(self, harness):
        h = harness
        content = b"identical tracked content"
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115_A.jpg", content,
            mtime=_mtime(2025, 1, 15),
        )
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115_B.jpg", content,
            mtime=_mtime(2025, 1, 15),
        )
        h.sync("a")
        assert h.state_file_count("a") == 2

        h.phone_delete("a", "DCIM/Camera/IMG_20250115_A.jpg")
        engine = h.sync("a")

        assert engine.stats["phone_moves_detected"] == 0
        state = _state_files(h, "a")
        assert state["photos/2025/IMG_20250115_A.jpg"]["phone_path"] == (
            "/sdcard/DCIM/Camera/IMG_20250115_A.jpg"
        )
        assert state["photos/2025/IMG_20250115_B.jpg"]["phone_path"] == (
            "/sdcard/DCIM/Camera/IMG_20250115_B.jpg"
        )


# ---------------------------------------------------------------------------
# P0: computer-side moves and phone propagation
# ---------------------------------------------------------------------------

class TestSyncEngineComputerSideMoves:
    def test_computer_side_move_updates_state_and_moves_phone(self, harness):
        h = harness
        content = b"computer move content"
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", content,
            mtime=_mtime(2025, 1, 15),
        )
        h.sync("a")

        h.computer_move(
            "photos/2025/IMG_20250115.jpg",
            "photos/2025/Album/IMG_20250115.jpg",
        )
        engine = h.sync("a")

        new_relpath = "photos/2025/Album/IMG_20250115.jpg"
        assert engine.stats["moves_synced"] == 1
        assert engine.stats["errors"] == 0
        assert h.computer_exists(new_relpath)
        assert not h.phone_exists("a", "DCIM/Camera/IMG_20250115.jpg")
        assert h.phone_read("a", "DCIM/Camera/Album/IMG_20250115.jpg") == content
        assert h.state_file_count("a") == 1
        assert h.state_phone_path(
            "a", new_relpath
        ) == "/sdcard/DCIM/Camera/Album/IMG_20250115.jpg"

    def test_phone_destination_collision_does_not_lie_about_phone_path(
        self, harness
    ):
        h = harness
        content = b"source content"
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", content,
            mtime=_mtime(2025, 1, 15),
        )
        h.sync("a")

        h.computer_move(
            "photos/2025/IMG_20250115.jpg",
            "photos/2025/Album/IMG_20250115.jpg",
        )
        h.phone_write(
            "a", "DCIM/Camera/Album/IMG_20250115.jpg",
            b"different destination content",
            mtime=_mtime(2025, 1, 16),
        )

        engine = h.sync("a")

        new_relpath = "photos/2025/Album/IMG_20250115.jpg"
        assert engine.stats["errors"] == 1
        assert h.phone_exists("a", "DCIM/Camera/IMG_20250115.jpg")
        assert h.phone_read("a", "DCIM/Camera/Album/IMG_20250115.jpg") == (
            b"different destination content"
        )
        assert h.state_phone_path(
            "a", new_relpath
        ) == "/sdcard/DCIM/Camera/IMG_20250115.jpg"


# ---------------------------------------------------------------------------
# P0: local deletes and tombstones
# ---------------------------------------------------------------------------

class TestSyncEngineLocalDeletesAndTombstones:
    def test_local_delete_creates_tombstone_and_does_not_redownload(self, harness):
        h = harness
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", b"deleted locally",
            mtime=_mtime(2025, 1, 15),
        )
        h.sync("a")

        relpath = "photos/2025/IMG_20250115.jpg"
        h.computer_delete(relpath)
        engine = h.sync("a")

        assert engine.stats["local_deletions"] == 1
        assert h.state_is_tombstoned("a", relpath)
        assert not h.computer_exists(relpath)
        assert h.phone_exists("a", "DCIM/Camera/IMG_20250115.jpg")

        again = h.sync("a")
        assert again.stats["files_copied"] == 0
        assert not h.computer_exists(relpath)
        assert h.state_is_tombstoned("a", relpath)

    def test_removed_tombstone_entry_allows_redownload(self, harness):
        h = harness
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", b"redownload me",
            mtime=_mtime(2025, 1, 15),
        )
        h.sync("a")
        relpath = "photos/2025/IMG_20250115.jpg"
        h.computer_delete(relpath)
        h.sync("a")
        assert h.state_is_tombstoned("a", relpath)

        state = h.get_state("a")
        del state["files"][relpath]
        _write_state(h, "a", state)

        engine = h.sync("a")

        assert engine.stats["files_copied"] == 1
        assert h.computer_exists(relpath)
        assert not h.state_is_tombstoned("a", relpath)

    def test_local_file_reappearing_clears_tombstone(self, harness):
        h = harness
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", b"original",
            mtime=_mtime(2025, 1, 15),
        )
        h.sync("a")
        relpath = "photos/2025/IMG_20250115.jpg"
        h.computer_delete(relpath)
        h.sync("a")
        assert h.state_is_tombstoned("a", relpath)

        h.computer_write(relpath, b"restored by user")
        h.sync("a")

        assert not h.state_is_tombstoned("a", relpath)


# ---------------------------------------------------------------------------
# P1: phone edits
# ---------------------------------------------------------------------------

class TestSyncEnginePhoneEdits:
    def test_phone_file_changed_with_new_mtime_updates_local_copy(self, harness):
        h = harness
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", b"v1",
            mtime=_mtime(2025, 1, 15),
        )
        h.sync("a")
        relpath = "photos/2025/IMG_20250115.jpg"
        old_hash = _state_files(h, "a")[relpath]["hash"]

        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", b"v2 changed",
            mtime=_mtime(2025, 1, 16),
        )
        engine = h.sync("a")

        assert engine.stats["files_updated"] == 1
        assert h.computer_read(relpath) == b"v2 changed"
        assert _state_files(h, "a")[relpath]["hash"] != old_hash
        assert _state_files(h, "a")[relpath]["size"] == len(b"v2 changed")

    def test_phone_file_changed_while_local_copy_moved_updates_moved_copy(
        self, harness
    ):
        h = harness
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", b"v1",
            mtime=_mtime(2025, 1, 15),
        )
        h.sync("a")
        h.computer_move(
            "photos/2025/IMG_20250115.jpg",
            "photos/2025/Edited/IMG_20250115.jpg",
        )
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", b"v2 edited on phone",
            mtime=_mtime(2025, 1, 16),
        )

        engine = h.sync("a")

        new_relpath = "photos/2025/Edited/IMG_20250115.jpg"
        assert engine.stats["files_updated"] == 1
        assert h.computer_read(new_relpath) == b"v2 edited on phone"
        assert h.state_has_relpath("a", new_relpath)
        assert not h.state_has_relpath("a", "photos/2025/IMG_20250115.jpg")


# ---------------------------------------------------------------------------
# P1: relevant-file filtering and excludes
# ---------------------------------------------------------------------------

class TestSyncEngineFilteringAndExcludes:
    def test_photos_ignore_non_photo_extensions(self, harness):
        h = harness
        h.phone_write("a", "DCIM/Camera/not_a_photo.txt", b"text")

        engine = h.sync("a")

        assert engine.stats["files_copied"] == 0
        assert h.state_file_count("a") == 0
        assert h.computer_list() == []

    def test_recordings_ignore_non_recording_extensions(self, harness):
        h = harness
        h.phone_write("a", "Recordings/not_audio.jpg", b"not audio")

        engine = h.sync("a")

        assert engine.stats["files_copied"] == 0
        assert h.state_file_count("a") == 0
        assert h.computer_list() == []

    def test_downloads_accept_arbitrary_extensions(self, harness):
        h = harness
        h.phone_write("a", "Download/archive.nope", b"accepted")

        engine = h.sync("a")

        assert engine.stats["files_copied"] == 1
        assert h.computer_read("downloads/phone-a/archive.nope") == b"accepted"

    def test_exclude_files_are_not_ingested(self, harness):
        h = harness
        h.cfg["exclude_files"] = ["*.skipme"]
        _save_cfg(h)
        h.phone_write("a", "Download/keep.txt", b"keep")
        h.phone_write("a", "Download/drop.skipme", b"drop")

        h.sync("a")

        assert h.computer_exists("downloads/phone-a/keep.txt")
        assert not h.computer_exists("downloads/phone-a/drop.skipme")
        assert h.state_file_count("a") == 1

    def test_exclude_dirs_are_not_ingested(self, harness):
        h = harness
        h.cfg["exclude_dirs"] = ["SkipMe"]
        _save_cfg(h)
        h.phone_write("a", "Download/keep.txt", b"keep")
        h.phone_write("a", "Download/SkipMe/drop.txt", b"drop")

        h.sync("a")

        assert h.computer_exists("downloads/phone-a/keep.txt")
        assert not h.computer_exists("downloads/phone-a/SkipMe/drop.txt")
        assert h.state_file_count("a") == 1


# ---------------------------------------------------------------------------
# P1: recursive scan and subdir reporting
# ---------------------------------------------------------------------------

class TestSyncEngineRecursiveScan:
    def test_recursive_scan_true_ingests_subdirectories(self, harness):
        h = harness
        h.phone_write(
            "a", "DCIM/Camera/Sub/IMG_20250115.jpg", b"sub photo",
            mtime=_mtime(2025, 1, 15),
        )

        h.sync("a")

        assert h.computer_read("photos/Sub/IMG_20250115.jpg") == b"sub photo"

    def test_recursive_scan_false_reports_unscanned_subdirectories(self, harness):
        h = harness
        h.cfg["recursive_scan"] = False
        _save_cfg(h)
        h.phone_write(
            "a", "DCIM/Camera/Sub/IMG_20250115.jpg", b"sub photo",
            mtime=_mtime(2025, 1, 15),
        )

        engine = h.sync("a")

        assert engine.stats["files_copied"] == 0
        assert not h.computer_exists("photos/Sub/IMG_20250115.jpg")
        assert any(
            parent == "/sdcard/DCIM/Camera" and name == "Sub" and status == "not_scanned"
            for parent, name, count, status in engine.discovered_subdirs
        )

    def test_excluded_subdirectories_are_reported_as_excluded(self, harness):
        h = harness
        h.phone_write(
            "a", "DCIM/Camera/.thumbnails/thumb.jpg", b"thumb",
            mtime=_mtime(2025, 1, 15),
        )

        engine = h.sync("a")

        assert engine.stats["files_copied"] == 0
        assert any(
            parent == "/sdcard/DCIM/Camera"
            and name == ".thumbnails"
            and status == "excluded"
            for parent, name, count, status in engine.discovered_subdirs
        )


# ---------------------------------------------------------------------------
# P1: photo date organization and subdir preservation
# ---------------------------------------------------------------------------

class TestSyncEnginePhotoDateOrganization:
    def test_exif_date_beats_filename_date(self, harness):
        h = harness
        content = _make_exif_jpeg_bytes(2021, 3, 4)
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", content,
            mtime=_mtime(2026, 1, 1),
        )

        h.sync("a")

        assert h.computer_exists("photos/2021/IMG_20250115.jpg")
        assert not h.computer_exists("photos/2025/IMG_20250115.jpg")

    def test_filename_date_beats_phone_mtime(self, harness):
        h = harness
        h.phone_write(
            "a", "DCIM/Camera/IMG_20200102.jpg", b"not a real image",
            mtime=_mtime(2024, 1, 1),
        )

        h.sync("a")

        assert h.computer_exists("photos/2020/IMG_20200102.jpg")

    def test_phone_mtime_used_when_no_exif_or_filename_date(self, harness):
        h = harness
        h.phone_write(
            "a", "DCIM/Camera/no_date.jpg", b"not a real image",
            mtime=_mtime(2019, 5, 6),
        )

        h.sync("a")

        assert h.computer_exists("photos/2019/no_date.jpg")

    def test_unsorted_when_no_date_available(self, harness):
        h = harness
        # mtime=0 is falsey in get_photo_date() fallback, so it reaches unsorted.
        h.phone_write(
            "a", "DCIM/Camera/no_date.jpg", b"not a real image",
            mtime=0,
        )

        h.sync("a")

        assert h.computer_exists("photos/unsorted/no_date.jpg")

    def test_photo_date_folders_false_puts_photos_directly_under_photos(
        self, harness
    ):
        h = harness
        h.cfg["photo_date_folders"] = False
        _save_cfg(h)
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", b"photo",
            mtime=_mtime(2025, 1, 15),
        )

        h.sync("a")

        assert h.computer_exists("photos/IMG_20250115.jpg")

    def test_preserve_phone_subdirs_true_meaningful_folder_replaces_year(
        self, harness
    ):
        h = harness
        h.phone_write(
            "a", "DCIM/Camera/Album/IMG_20250115.jpg", b"photo",
            mtime=_mtime(2025, 1, 15),
        )

        h.sync("a")

        assert h.computer_exists("photos/Album/IMG_20250115.jpg")
        assert not h.computer_exists("photos/2025/Album/IMG_20250115.jpg")

    def test_preserve_phone_subdirs_false_flattens_photo_destination(
        self, harness
    ):
        h = harness
        h.cfg["preserve_phone_subdirs"] = False
        _save_cfg(h)
        h.phone_write(
            "a", "DCIM/Camera/Album/IMG_20250115.jpg", b"photo",
            mtime=_mtime(2025, 1, 15),
        )

        h.sync("a")

        assert h.computer_exists("photos/2025/IMG_20250115.jpg")
        assert not h.computer_exists("photos/2025/Album/IMG_20250115.jpg")


# ---------------------------------------------------------------------------
# P1: dry-run behavior
# ---------------------------------------------------------------------------

class TestSyncEngineDryRun:
    def test_dry_run_copies_no_files_and_saves_no_state(self, harness):
        h = harness
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", b"dry run photo",
            mtime=_mtime(2025, 1, 15),
        )

        engine = h.sync("a", dry_run=True)

        assert engine.stats["files_copied"] == 1  # would copy
        assert h.computer_list() == []
        assert h.state_file_count("a") == 0

    def test_dry_run_does_not_move_phone_file(self, harness):
        h = harness
        h.phone_write(
            "a", "DCIM/Camera/IMG_20250115.jpg", b"move later",
            mtime=_mtime(2025, 1, 15),
        )
        h.sync("a")
        h.computer_move(
            "photos/2025/IMG_20250115.jpg",
            "photos/2025/Album/IMG_20250115.jpg",
        )

        engine = h.sync("a", dry_run=True)

        assert engine.stats["moves_synced"] == 1
        assert h.phone_exists("a", "DCIM/Camera/IMG_20250115.jpg")
        assert not h.phone_exists("a", "DCIM/Camera/Album/IMG_20250115.jpg")
        # Dry-run also should not persist the state relpath change.
        assert h.state_has_relpath("a", "photos/2025/IMG_20250115.jpg")


# ---------------------------------------------------------------------------
# P2: _compute_desired_phone_path private-helper edge cases
# ---------------------------------------------------------------------------

class TestSyncEngineDesiredPhonePath:
    def test_compute_desired_phone_path_strips_photos_and_year(self, harness):
        h = harness
        engine = h.sync("a", dry_run=True)
        computer_path = h.data_dir / "photos" / "2025" / "Album" / "IMG.jpg"
        info = {
            "phone_source_dir": "/sdcard/DCIM/Camera",
            "phone_path": "/sdcard/DCIM/Camera/IMG.jpg",
            "category": "photos",
        }

        desired = engine._compute_desired_phone_path(computer_path, info)

        assert desired == "/sdcard/DCIM/Camera/Album/IMG.jpg"

    def test_compute_desired_phone_path_with_no_meaningful_subfolder(
        self, harness
    ):
        h = harness
        engine = h.sync("a", dry_run=True)
        computer_path = h.data_dir / "photos" / "2025" / "IMG.jpg"
        info = {
            "phone_source_dir": "/sdcard/DCIM/Camera",
            "phone_path": "/sdcard/DCIM/Camera/IMG.jpg",
            "category": "photos",
        }

        desired = engine._compute_desired_phone_path(computer_path, info)

        assert desired == "/sdcard/DCIM/Camera/IMG.jpg"


# ---------------------------------------------------------------------------
# P2: _find_file_by_hash search ordering and symlink handling
# ---------------------------------------------------------------------------

class TestSyncEngineFindFileByHash:
    def test_find_file_by_hash_prefers_previous_directory(self, harness):
        h = harness
        content = b"target"
        target_hash = _hash(content)
        h.computer_write("photos/2025/candidate.jpg", content)
        h.computer_write("downloads/phone-a/elsewhere.jpg", content)
        engine = h.sync("a", dry_run=True)

        found = engine._find_file_by_hash(
            target_hash,
            previous_path=h.data_dir / "photos" / "2025" / "missing.jpg",
        )

        assert found == h.data_dir / "photos" / "2025" / "candidate.jpg"

    def test_find_file_by_hash_ignores_hidden_directories(self, harness):
        h = harness
        content = b"hidden target"
        target_hash = _hash(content)
        h.computer_write(".hidden/target.jpg", content)
        engine = h.sync("a", dry_run=True)

        found = engine._find_file_by_hash(target_hash)

        assert found is None

    def test_find_file_by_hash_symlink_cycle_does_not_hang(self, harness):
        h = harness
        cycle = h.data_dir / "cycle"
        try:
            cycle.symlink_to(h.data_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        engine = h.sync("a", dry_run=True)

        found = engine._find_file_by_hash("0" * 64)

        assert found is None

    def test_find_file_by_hash_respects_symlink_depth_limit(self, harness):
        h = harness
        external = h.tmpdir / "external_target"
        external.mkdir()
        content = b"outside through symlink"
        (external / "target.jpg").write_bytes(content)
        link = h.data_dir / "linked_external"
        try:
            link.symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        h.cfg["max_symlink_depth"] = 0
        _save_cfg(h)
        engine = h.sync("a", dry_run=True)

        found = engine._find_file_by_hash(_hash(content))

        assert found is None
