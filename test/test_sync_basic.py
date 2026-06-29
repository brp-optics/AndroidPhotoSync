"""Tests for basic sync operations: ingest, skip, re-pull, date sorting."""
import time
from datetime import datetime


class TestBasicIngest:
    def test_single_photo(self, harness, img_data):
        content, mtime = img_data("photo1", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg",
                            content, mtime)
        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 1
        assert harness.computer_exists("photos/2025/IMG_20250115_001.jpg")

    def test_multiple_categories(self, harness, img_data):
        c1, m1 = img_data("photo", 2025)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c1, m1)
        harness.phone_write("a", "Download/report.pdf", b"pdf data")
        harness.phone_write("a", "Recordings/voice.m4a", b"audio data")
        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 3
        assert harness.computer_exists("photos/2025/IMG_20250115_001.jpg")
        assert harness.computer_exists("downloads/phone-a/report.pdf")
        assert harness.computer_exists("recordings/phone-a/voice.m4a")

    def test_skip_already_synced(self, harness, img_data):
        content, mtime = img_data("photo1")
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg",
                            content, mtime)
        harness.sync("a")
        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 0
        assert engine.stats["files_skipped"] == 1

    def test_dry_run(self, harness, img_data):
        content, mtime = img_data("photo1")
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg",
                            content, mtime)
        engine = harness.sync("a", dry_run=True)
        assert engine.stats["files_copied"] == 1
        # But nothing should actually exist on the computer
        assert not harness.computer_exists("photos/2025/IMG_20250115_001.jpg")


class TestDateSorting:
    def test_date_from_filename(self, harness, img_data):
        content, mtime = img_data("p", 2024, 6, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20240615_001.jpg",
                            content, mtime)
        harness.sync("a")
        assert harness.computer_exists("photos/2024/IMG_20240615_001.jpg")

    def test_unsorted_fallback(self, harness):
        """Files with no date info go to unsorted/."""
        harness.phone_write("a", "DCIM/Camera/mystery.jpg",
                            b"no date info")
        harness.sync("a")
        # Should be in unsorted (no EXIF, no date in name, no mtime passed)
        files = harness.computer_list("photos")
        assert any("mystery.jpg" in f for f in files)

    def test_epoch_filename(self, harness):
        """Files with unix epoch in name get sorted by that date."""
        # 1719532800 = 2024-06-28
        harness.phone_write("a", "DCIM/Camera/1719532800000.jpg",
                            b"kakao export", 1719532800.0)
        harness.sync("a")
        assert harness.computer_exists("photos/2024/1719532800000.jpg")


class TestMtimeRepull:
    def test_changed_file_repulled(self, harness, img_data):
        content1, mtime1 = img_data("photo1", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg",
                            content1, mtime1)
        harness.sync("a")

        # Modify the file on phone (new content, new mtime)
        content2 = b"EDITED_CONTENT_NEW"
        mtime2 = mtime1 + 3600  # 1 hour later
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg",
                            content2, mtime2)
        engine = harness.sync("a")
        assert engine.stats["files_updated"] == 1

        # Computer should have the new content
        data = harness.computer_read("photos/2025/IMG_20250115_001.jpg")
        assert data == content2

    def test_unchanged_file_skipped(self, harness, img_data):
        content, mtime = img_data("photo1")
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg",
                            content, mtime)
        harness.sync("a")

        # Don't change anything
        engine = harness.sync("a")
        assert engine.stats["files_updated"] == 0
        assert engine.stats["files_skipped"] == 1


class TestSubdirectories:
    def test_recursive_scan(self, harness, img_data):
        content, mtime = img_data("sub")
        harness.phone_write(
            "a", "DCIM/Camera/WhatsApp/IMG_20250115_001.jpg",
            content, mtime)
        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 1

    def test_phone_subdir_preserved(self, harness, img_data):
        """A meaningful phone subdir (e.g. WhatsApp) is preserved and, under
        the transparent_dirs rule, REPLACES the year bucket (it's a
        hand-meaningful folder, not loose Camera content)."""
        content, mtime = img_data("sub")
        harness.phone_write(
            "a", "DCIM/Camera/WhatsApp/IMG_20250115_001.jpg",
            content, mtime)
        harness.sync("a")
        # Meaningful folder replaces the year: photos/WhatsApp/, not
        # photos/2025/WhatsApp/.
        assert harness.computer_exists(
            "photos/WhatsApp/IMG_20250115_001.jpg")
        assert not harness.computer_exists(
            "photos/2025/WhatsApp/IMG_20250115_001.jpg")


class TestCollisionSafety:
    def test_same_name_different_content(self, harness, img_data):
        """Two files with same name but different content from same phone."""
        harness.phone_write(
            "a", "DCIM/Camera/IMG_20250115_001.jpg",
            b"content_a", 1736899200.0)  # 2025-01-15
        harness.sync("a")

        # Second file in a different phone dir with same name
        harness.phone_write(
            "a", "Pictures/IMG_20250115_001.jpg",
            b"content_b", 1736899200.0)
        harness.sync("a")

        files = harness.computer_list("photos/2025")
        # Both should exist (one with device suffix or number)
        assert len(files) >= 2
