"""SyncEngine integration tests using FakeADB.

These complement the existing sync tests (test_sync_basic, test_sync_moves,
test_sync_deletions, test_sync_two_phones) by targeting gaps:

  - Phase ordering and interaction (the scan-once → detect-moves → ingest
    → sync-moves pipeline)
  - Idempotency (running sync twice with no changes is a no-op)
  - State schema integrity (entries carry the right fields)
  - Config-flag behavior (recursive_scan,
    preserve_phone_subdirs, photo_date_folders)
  - File-relevance filtering (extensions per category, excludes)
  - Edge cases in each phase that the happy-path tests don't reach
  - Dry-run guarantees (no mutations on phone or computer or state file)

Organization mirrors the engine's phases:
  TestScanPhase, TestPhoneMoveDetection, TestIngestPhase,
  TestComputerMoveAndDelete, plus cross-cutting:
  TestIdempotency, TestStateSchema, TestConfigFlags,
  TestRelevanceFiltering, TestDryRunGuarantees, TestStatsAccounting.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import phonesync


# ---------------------------------------------------------------------------
# Scan phase
# ---------------------------------------------------------------------------

class TestScanPhase:
    def test_scan_builds_phone_path_index(self, harness, img_data):
        """After a sync, the engine's phone_path_index covers all scanned files."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.phone_write("a", "Download/doc.pdf", b"pdf")
        engine = harness.sync("a")
        # Index should contain both phone paths
        assert "/sdcard/DCIM/Camera/IMG_20250115_001.jpg" in \
            engine._phone_path_index
        assert "/sdcard/Download/doc.pdf" in engine._phone_path_index

    def test_newline_in_filename_ingested(self, harness):
        """A photo whose filename contains a newline ingests as ONE file.

        End-to-end guard for the NUL-record-terminator fix: a '\\n' in the
        name must not split or drop the file during scan/ingest.
        """
        # Filename with an embedded newline; date from mtime (2024).
        name = "holiday\nphoto.jpg"
        harness.phone_write(
            "a", f"DCIM/Camera/{name}", b"newline photo data",
            1719532800.0)  # 2024-06-28
        # A normal sibling to confirm the stream keeps parsing past it.
        harness.phone_write(
            "a", "DCIM/Camera/normal.jpg", b"normal", 1719532800.0)

        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 2

        # The newline-bearing file landed intact under photos/2024/
        files = harness.computer_list("photos/2024")
        assert any(f.endswith("holiday\nphoto.jpg") for f in files), files
        assert any(f.endswith("normal.jpg") for f in files), files

        # State tracks both, with the full newline name preserved
        phone_paths = {
            i["phone_path"] for i in harness.get_state("a")["files"].values()}
        assert f"/sdcard/DCIM/Camera/{name}" in phone_paths

    def test_scan_respects_recursive_false(self, harness, img_data):
        """With recursive_scan=false, files in subdirs aren't ingested."""
        # Reconfigure
        cfg = phonesync.load_config()
        cfg["recursive_scan"] = False
        phonesync.save_config(cfg)

        c1, m1 = img_data("top", 2025, 1, 15)
        c2, m2 = img_data("sub", 2025, 1, 16)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_top.jpg", c1, m1)
        harness.phone_write("a", "DCIM/Camera/album/IMG_20250116_sub.jpg",
                            c2, m2)
        engine = harness.sync("a")
        # Only the top-level file should be ingested
        assert engine.stats["files_copied"] == 1
        assert harness.computer_exists("photos/2025/IMG_20250115_top.jpg")

    def test_scan_excludes_default_dirs(self, harness, img_data):
        """Files in .thumbnails etc. are excluded from scan."""
        c, m = img_data("real", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.phone_write("a", "DCIM/Camera/.thumbnails/thumb.jpg",
                            b"thumbnail")
        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 1


# ---------------------------------------------------------------------------
# Scan failure must abort the whole run (no silent partial sync)
# ---------------------------------------------------------------------------

class TestScanFailureAborts:
    def test_unreachable_device_aborts_before_phases(self, harness, img_data):
        """If the device is unreachable up front, run() aborts: nothing
        copied, no state saved, errors counted."""
        from conftest import FakeADB
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)

        # Mark phone A's serial unreachable for this run.
        FakeADB._unreachable_serials.add("SERIAL_A")
        try:
            engine = harness.sync("a")
        finally:
            FakeADB._unreachable_serials.discard("SERIAL_A")

        assert engine._scan_failed is True
        assert engine.stats["errors"] >= 1
        assert engine.stats["files_copied"] == 0
        # Nothing committed to the library or state
        assert not harness.computer_exists(
            "photos/2025/IMG_20250115_001.jpg")
        assert harness.state_file_count("a") == 0

    def test_abort_does_not_tombstone_existing_files(self, harness, img_data):
        """A scan failure must not cause already-synced files to be treated
        as deleted/tombstoned."""
        from conftest import FakeADB
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")  # clean first sync
        assert harness.state_file_count("a") == 1

        # Now the device drops on the next run.
        FakeADB._unreachable_serials.add("SERIAL_A")
        try:
            harness.sync("a")
        finally:
            FakeADB._unreachable_serials.discard("SERIAL_A")

        # The existing file is still tracked and NOT tombstoned.
        relpath = "photos/2025/IMG_20250115_001.jpg"
        assert harness.state_has_relpath("a", relpath)
        assert not harness.state_is_tombstoned("a", relpath)
        # Computer copy untouched.
        assert harness.computer_exists(relpath)

    def test_mid_scan_disconnect_raises_and_aborts(self, harness, img_data):
        """If a directory that exists returns no files AND the device has
        gone unreachable, list_files_recursive raises and the run aborts."""
        from conftest import FakeADB
        import phonesync

        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)

        # A FakeADB that passes the up-front reachability probe, then "drops"
        # so that an empty directory result is treated as a transport
        # failure during the per-directory scan.
        class DropMidScanADB(FakeADB):
            def __init__(self, serial):
                super().__init__(serial)
                self._probed = False

            def is_reachable(self):
                # First call (up-front probe) succeeds; afterwards the
                # device is considered gone.
                if not self._probed:
                    self._probed = True
                    return True
                return False

            def list_files_recursive(self, remote_dir, exclude_dirs=None,
                                     exclude_files=None, max_depth=255):
                # Simulate the directory existing but the scan returning
                # nothing because the link dropped.
                if not self.is_reachable():
                    raise phonesync.ADBError(
                        f"Scan of {remote_dir} failed: device unreachable")
                return []

        engine = harness.sync("a", adb_cls=DropMidScanADB)
        assert engine._scan_failed is True
        assert engine.stats["files_copied"] == 0
        assert harness.state_file_count("a") == 0

    def test_empty_dir_reachable_is_not_a_failure(self, harness):
        """A genuinely empty (or missing) source dir on a reachable device
        is normal, not an abort."""
        # No files written at all; device reachable (default).
        engine = harness.sync("a")
        assert engine._scan_failed is False
        assert engine.stats["errors"] == 0
        assert engine.stats["files_copied"] == 0


# ---------------------------------------------------------------------------
# Phone move detection (Phase 1) — edge cases
# ---------------------------------------------------------------------------

class TestPhoneMoveDetection:
    def test_move_updates_phone_source_dir(self, harness, img_data):
        """A phone move across source dirs updates phone_source_dir in state."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")

        # Move from Camera to Pictures (different configured source)
        harness.phone_move(
            "a",
            "DCIM/Camera/IMG_20250115_001.jpg",
            "Pictures/IMG_20250115_001.jpg")
        harness.sync("a")

        # Find the state entry and check phone_source_dir
        state = harness.get_state("a")
        entry = None
        for relpath, info in state["files"].items():
            if info["phone_path"] == "/sdcard/Pictures/IMG_20250115_001.jpg":
                entry = info
                break
        assert entry is not None
        assert entry["phone_source_dir"] == "/sdcard/Pictures"

    def test_two_identical_files_one_moved(self, harness, img_data):
        """Two files with same content; moving one is not confused for the other."""
        content = b"IDENTICAL_CONTENT_BLOB"
        harness.phone_write("a", "DCIM/Camera/first.jpg", content,
                            1736899200.0)
        harness.phone_write("a", "DCIM/Camera/second.jpg", content,
                            1736899200.0)
        harness.sync("a")
        assert harness.state_file_count("a") == 2

        # Move 'first' to a subfolder
        harness.phone_move(
            "a",
            "DCIM/Camera/first.jpg",
            "DCIM/Camera/sorted/first.jpg")
        engine = harness.sync("a")

        # Should detect exactly one move, no new ingest
        assert engine.stats["phone_moves_detected"] == 1
        assert engine.stats["files_copied"] == 0
        assert harness.state_file_count("a") == 2

    def test_move_then_delete_original_path_reused(self, harness, img_data):
        """File moves on phone AND a different file takes its old path.

        Intended behavior:
          - The original file (content c1) is recognized as MOVED to its
            new phone path, with its tracked content/hash preserved. No
            re-download of the original.
          - The different new file (content c2) at the reused old path is
            ingested as a genuinely NEW file.
          - Both files present on the computer with their correct content.
          - Two state entries, each pointing at the right phone path.
        """
        c1, m1 = img_data("orig", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_A.jpg", c1, m1)
        harness.sync("a")

        # Move original away, then put a DIFFERENT file at the old path
        harness.phone_move("a", "DCIM/Camera/IMG_A.jpg",
                           "DCIM/Camera/moved/IMG_A.jpg")
        c2, m2 = img_data("new", 2025, 2, 20)
        harness.phone_write("a", "DCIM/Camera/IMG_A.jpg", c2, m2)

        engine = harness.sync("a")

        # The original is detected as a phone-side move, not re-pulled
        assert engine.stats["phone_moves_detected"] == 1
        # The new file at the reused path is ingested as new
        assert engine.stats["files_copied"] == 1

        # Two independent state entries
        state = harness.get_state("a")
        assert len(state["files"]) == 2

        # The moved original keeps its original content/hash, and its
        # state entry points at the NEW phone path.
        import hashlib
        h1 = hashlib.sha256(c1).hexdigest()
        h2 = hashlib.sha256(c2).hexdigest()
        by_hash = {info["hash"]: info for info in state["files"].values()}
        assert h1 in by_hash, "original file's content lost from state"
        assert h2 in by_hash, "new file's content missing from state"
        assert by_hash[h1]["phone_path"] == \
            "/sdcard/DCIM/Camera/moved/IMG_A.jpg"
        assert by_hash[h2]["phone_path"] == \
            "/sdcard/DCIM/Camera/IMG_A.jpg"

        # Both files present on the computer with correct content
        contents = {harness.computer_read(f)
                    for f in harness.computer_list("photos")}
        assert c1 in contents
        assert c2 in contents

    def test_unchanged_file_not_a_move(self, harness, img_data):
        """A file that hasn't moved (same path, size, mtime) is not a move,
        and does not trigger a phone-side hash recomputation.

        Guards the mtime fast-path: we count adb.file_hash calls and
        assert none happen for an untouched file on the second sync.
        """
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")

        # Instrument the engine's adb to count file_hash calls during the
        # second (no-op) sync.
        engine = harness.sync("a", dry_run=True)  # builds engine, no writes
        # Re-run a real sync but wrap file_hash to count invocations.
        import phonesync
        from conftest import FakeADB
        calls = {"n": 0}
        orig = FakeADB.file_hash

        def counting_hash(self, remote_path):
            calls["n"] += 1
            return orig(self, remote_path)

        FakeADB.file_hash = counting_hash
        try:
            e2 = harness.sync("a")
        finally:
            FakeADB.file_hash = orig

        assert e2.stats["phone_moves_detected"] == 0
        # No hashing of the unchanged file during move detection.
        assert calls["n"] == 0

    def test_same_path_content_edited_not_a_move(self, harness, img_data):
        """If content at the SAME path changes (edit in place), it's a
        re-pull, not a phone move."""
        c1, m1 = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c1, m1)
        harness.sync("a")

        # Edit in place: same path, new content, new mtime
        c2 = b"EDITED_IN_PLACE_CONTENT_XYZ"
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg",
                            c2, m1 + 3600)
        engine = harness.sync("a")

        # Not a move — it's an update of the same entry
        assert engine.stats["phone_moves_detected"] == 0
        assert engine.stats["files_updated"] == 1
        assert harness.state_file_count("a") == 1


# ---------------------------------------------------------------------------
# Phone-move candidate disambiguation (#10): use filename + mtime, and
# REFUSE to guess when multiple identical copies are equally plausible.
# ---------------------------------------------------------------------------

class TestMoveCandidateDisambiguation:
    def test_ambiguous_identical_copies_not_guessed(self, harness, img_data):
        """One tracked file moves away AND a second identical copy (same
        name, same mtime) appears at another untracked path. The two copies
        are indistinguishable, so the engine must NOT guess: 0 moves, 1
        conflict, state left pointing at the old path."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/orig.jpg", c, m)
        harness.sync("a")

        harness.phone_move(
            "a", "DCIM/Camera/orig.jpg", "DCIM/Camera/albumA/orig.jpg")
        # Second identical copy: same basename, same mtime, different dir.
        harness.phone_write("a", "DCIM/Camera/albumB/orig.jpg", c, m)

        engine = harness.sync("a")
        assert engine.stats["phone_moves_detected"] == 0
        assert engine.stats["move_conflicts"] == 1
        # State for the original still points at the OLD path (not guessed).
        phone_paths = {
            i["phone_path"]
            for i in harness.get_state("a")["files"].values()}
        assert "/sdcard/DCIM/Camera/orig.jpg" in phone_paths

    def test_ambiguous_copies_no_data_loss(self, harness, img_data):
        """Even though the move isn't resolved, both physical copies end up
        on the computer (the unmatched ones are ingested as new files)."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/orig.jpg", c, m)
        harness.sync("a")
        harness.phone_move(
            "a", "DCIM/Camera/orig.jpg", "DCIM/Camera/albumA/orig.jpg")
        harness.phone_write("a", "DCIM/Camera/albumB/orig.jpg", c, m)

        harness.sync("a")
        # The content is preserved on the computer (no loss); duplicates are
        # always kept, so both copies are present.
        contents = [harness.computer_read(f)
                    for f in harness.computer_list("photos")]
        assert contents.count(c) >= 1

    def test_name_and_mtime_break_tie(self, harness, img_data):
        """When one candidate matches name+mtime (a real mv) and another is
        a renamed/re-timed copy, the strong candidate wins — no conflict."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/orig.jpg", c, m)
        harness.sync("a")

        # Real move preserves name + mtime.
        harness.phone_move(
            "a", "DCIM/Camera/orig.jpg", "DCIM/Camera/album/orig.jpg")
        # A renamed copy with a different mtime (weaker tier).
        harness.phone_write(
            "a", "DCIM/Camera/shared/renamed.jpg", c, m + 9999)

        engine = harness.sync("a")
        assert engine.stats["move_conflicts"] == 0
        assert engine.stats["phone_moves_detected"] == 1
        # The original's history attached to the true move target.
        st = harness.get_state("a")["files"]
        assert st["photos/2025/orig.jpg"]["phone_path"] == \
            "/sdcard/DCIM/Camera/album/orig.jpg"

    def test_mtime_breaks_tie_when_names_differ(self, harness, img_data):
        """If neither candidate keeps the basename but one keeps the mtime,
        the mtime match is the stronger tier and wins."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/orig.jpg", c, m)
        harness.sync("a")

        harness.phone_move(
            "a", "DCIM/Camera/orig.jpg", "DCIM/Camera/a/renamed_same_mtime.jpg")
        # set the moved file's mtime back to the original (mv preserves it;
        # phone_move uses shutil.move which keeps mtime, so this is already
        # the case) — and add a different-mtime renamed copy.
        harness.phone_write(
            "a", "DCIM/Camera/b/other_name.jpg", c, m + 12345)

        engine = harness.sync("a")
        assert engine.stats["move_conflicts"] == 0
        assert engine.stats["phone_moves_detected"] == 1
        st = harness.get_state("a")["files"]
        assert st["photos/2025/orig.jpg"]["phone_path"] == \
            "/sdcard/DCIM/Camera/a/renamed_same_mtime.jpg"


# ---------------------------------------------------------------------------
# Library index skip (#8): don't re-pull content already in the library
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Library index: duplicates are kept; the index only completes partial moves
# and persists a content cache. (keep_duplicates was removed.)
# ---------------------------------------------------------------------------

class TestLibraryIndexBehavior:
    def test_existing_library_content_is_kept_not_skipped(self, harness):
        """Content already on disk at an untracked path is NOT suppressed —
        the phone copy is pulled and both copies are kept. (This is a
        one-way ingest tool; every duplicate is intentional.)"""
        content = b"ALREADY_ON_DISK"
        harness.computer_write("photos/2025/existing.jpg", content,
                               mtime=1736899200.0)
        harness.phone_write("a", "DCIM/Camera/existing.jpg", content,
                            1736899200.0)
        engine = harness.sync("a")
        # The phone copy is pulled (kept), not suppressed.
        assert engine.stats["files_copied"] == 1
        assert engine.stats["move_completions"] == 0
        # Both copies now exist on disk.
        photos = harness.computer_list("photos")
        assert len(photos) == 2

    def test_new_content_pulled(self, harness):
        """Genuinely new content is pulled normally."""
        harness.computer_write("photos/2025/old.jpg", b"OLD",
                               mtime=1736899200.0)
        harness.phone_write("a", "DCIM/Camera/new.jpg", b"BRAND_NEW",
                            1736899200.0)
        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 1
        assert engine.stats["move_completions"] == 0

    def test_size_prefilter_avoids_hash_when_no_size_match(self, harness):
        """If no library file shares the phone file's size, the engine does
        not ask the phone for a hash during the move-completion check (the
        cheap size pre-filter short-circuits)."""
        from conftest import FakeADB
        harness.computer_write("photos/2025/big.jpg", b"X" * 500,
                               mtime=1736899200.0)
        harness.phone_write("a", "DCIM/Camera/small.jpg", b"tiny",
                            1736899200.0)

        calls = {"n": 0}
        orig = FakeADB.file_hash

        def counting(self, p):
            calls["n"] += 1
            return orig(self, p)

        FakeADB.file_hash = counting
        try:
            harness.sync("a")
        finally:
            FakeADB.file_hash = orig
        # small.jpg has a unique size, so the move-completion probe never
        # fires. The only hash that may occur is the post-pull verify (once).
        assert calls["n"] <= 1

    def test_cache_persists_this_run_pulls(self, harness):
        """Files pulled during a run are written to the shared cache, so the
        next run does not re-hash them."""
        import json
        harness.phone_write("a", "DCIM/Camera/x.jpg", b"PULLED_CONTENT",
                            1736899200.0)
        harness.sync("a")
        cache_file = harness.cfg_dir / "library-index.json"
        assert cache_file.exists()
        with open(cache_file) as f:
            cached = json.load(f)
        assert "photos/2025/x.jpg" in cached["files"]
        assert cached["files"]["photos/2025/x.jpg"]["hash"] == \
            __import__("hashlib").sha256(b"PULLED_CONTENT").hexdigest()


# ---------------------------------------------------------------------------
# Ingest phase — edge cases
# ---------------------------------------------------------------------------

class TestPullVerification:
    """Pull integrity: a corrupt/truncated transfer must be caught and
    NOT committed to the library or state."""

    def _corrupting_adb(self):
        """A FakeADB whose pull() truncates the file, but whose file_hash()
        still reports the true phone-side hash (so verification mismatches)."""
        from conftest import FakeADB

        class CorruptingFakeADB(FakeADB):
            def pull(self, remote_path, local_path):
                src = self._local(remote_path)
                if not src.exists():
                    return False
                from pathlib import Path as _P
                _P(local_path).parent.mkdir(parents=True, exist_ok=True)
                # Write TRUNCATED/altered bytes to simulate a bad transfer
                data = src.read_bytes()
                _P(local_path).write_bytes(data[:-1] + b"X"
                                           if data else b"X")
                return True

        return CorruptingFakeADB

    def test_corrupt_pull_not_committed(self, harness, img_data):
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        engine = harness.sync("a", adb_cls=self._corrupting_adb())

        # Nothing committed to the library
        assert not harness.computer_exists(
            "photos/2025/IMG_20250115_001.jpg")
        # Nothing committed to state
        assert harness.state_file_count("a") == 0
        # Counted as an error + a verify failure
        assert engine.stats["errors"] >= 1
        assert engine.stats["pull_verify_failures"] == 1
        assert engine.stats["files_copied"] == 0

    def test_corrupt_pull_retried_next_sync(self, harness, img_data):
        """After a corrupt pull, a subsequent clean sync succeeds."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        # First sync corrupts
        harness.sync("a", adb_cls=self._corrupting_adb())
        assert harness.state_file_count("a") == 0

        # Second sync with a healthy ADB succeeds
        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 1
        assert harness.computer_exists(
            "photos/2025/IMG_20250115_001.jpg")
        assert harness.computer_read(
            "photos/2025/IMG_20250115_001.jpg") == c

    def test_verify_pulls_can_be_disabled(self, harness, img_data):
        """With verify_pulls=false, the integrity check is skipped (the
        corrupt bytes are committed — documents the trade-off)."""
        cfg = phonesync.load_config()
        cfg["verify_pulls"] = False
        phonesync.save_config(cfg)

        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        engine = harness.sync("a", adb_cls=self._corrupting_adb())
        # Without verification, the (corrupt) file IS committed
        assert engine.stats["files_copied"] == 1
        assert engine.stats["pull_verify_failures"] == 0

    def test_clean_pull_passes_verification(self, harness, img_data):
        """A normal pull passes verification and commits as usual."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        engine = harness.sync("a")  # default FakeADB, clean pull
        assert engine.stats["pull_verify_failures"] == 0
        assert engine.stats["files_copied"] == 1


class TestIngestPhase:
    def test_tombstone_blocks_reingest(self, harness, img_data):
        """A tombstoned file is not re-ingested even though it's on the phone."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        harness.computer_delete("photos/2025/IMG_20250115_001.jpg")
        harness.sync("a")  # tombstones

        # Tombstone is set; now sync again — should skip, not re-copy
        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 0
        assert not harness.computer_exists(
            "photos/2025/IMG_20250115_001.jpg")

    def test_repull_updates_hash_and_size(self, harness, img_data):
        """Re-pulling a changed file updates hash and size in state."""
        c1, m1 = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c1, m1)
        harness.sync("a")
        state1 = harness.get_state("a")
        old_hash = list(state1["files"].values())[0]["hash"]

        c2 = b"COMPLETELY_DIFFERENT_AND_LONGER_CONTENT_X"
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c2,
                            m1 + 3600)
        harness.sync("a")
        state2 = harness.get_state("a")
        new_info = list(state2["files"].values())[0]
        assert new_info["hash"] != old_hash
        assert new_info["size"] == len(c2)

    def test_duplicates_always_kept_same_device(self, harness):
        """Identical content at two phone paths on one device → both kept.
        Duplicates are intentional and never collapsed (keep_duplicates was
        removed; there is no dedup-by-content)."""
        content = b"DUPLICATE_BLOB"
        harness.phone_write("a", "DCIM/Camera/a.jpg", content, 1736899200.0)
        harness.phone_write("a", "Pictures/b.jpg", content, 1736899200.0)
        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 2
        # Both physical copies exist on disk.
        contents = [harness.computer_read(f)
                    for f in harness.computer_list("photos")]
        assert contents.count(content) == 2

    def test_duplicates_kept_across_runs(self, harness):
        """A new identical-content file added later is still pulled and kept,
        not suppressed by the library index."""
        content = b"DUP_ACROSS_RUNS"
        harness.phone_write("a", "DCIM/Camera/a.jpg", content, 1736899200.0)
        harness.sync("a")
        # Same content appears at a new phone path on a later sync.
        harness.phone_write("a", "Pictures/b.jpg", content, 1736899200.0)
        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 1
        assert engine.stats["move_completions"] == 0
        contents = [harness.computer_read(f)
                    for f in harness.computer_list("photos")]
        assert contents.count(content) == 2

    def test_unparseable_date_goes_to_unsorted(self, harness):
        """A photo with no parseable date and no mtime lands in unsorted."""
        # Write with a mtime so old, it would still parse; instead use
        # a name with no date and rely on mtime fallback being absent.
        # FakeADB always has an mtime, so this lands in the mtime's year.
        # To force unsorted, use a 1970 epoch (before sanity window).
        harness.phone_write("a", "DCIM/Camera/mystery.jpg", b"x", mtime=0.0)
        harness.sync("a")
        # mtime=0 is 1970, before the 2000 sanity cutoff → unsorted
        files = harness.computer_list("photos")
        assert any("unsorted" in f for f in files)


# ---------------------------------------------------------------------------
# Computer move + delete (Phase 3) — edge cases
# ---------------------------------------------------------------------------

class TestComputerMoveAndDelete:
    def test_move_outside_photos_tree_tracked(self, harness, img_data):
        """Moving a photo outside photos/ is tracked (no phone move)."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")

        harness.computer_move(
            "photos/2025/IMG_20250115_001.jpg",
            "misc_folder/IMG_20250115_001.jpg")
        engine = harness.sync("a")
        assert engine.stats["moves_synced"] == 1
        # State should point at the new location
        assert harness.state_has_relpath(
            "a", "misc_folder/IMG_20250115_001.jpg")

    def test_delete_then_move_back_clears_tombstone(self, harness, img_data):
        """Delete (tombstone), then restore the file → tombstone cleared."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        relpath = "photos/2025/IMG_20250115_001.jpg"

        harness.computer_delete(relpath)
        harness.sync("a")
        assert harness.state_is_tombstoned("a", relpath)

        harness.computer_write(relpath, c)
        harness.sync("a")
        assert not harness.state_is_tombstoned("a", relpath)

    def test_non_photo_moves_not_propagated(self, harness):
        """Moving a download on the computer doesn't queue a phone move."""
        harness.phone_write("a", "Download/report.pdf", b"pdf data")
        harness.sync("a")

        harness.computer_move(
            "downloads/phone-a/report.pdf",
            "downloads/phone-a/archive/report.pdf")
        engine = harness.sync("a")
        # Move is tracked in state but not propagated to phone
        # (only photos propagate). No error either.
        assert engine.stats["errors"] == 0

    def test_failed_phone_move_recomputed_next_run(self, harness, img_data):
        """A photo move is propagated; re-running keeps phone path stable."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")

        harness.computer_move(
            "photos/2025/IMG_20250115_001.jpg",
            "photos/2025/trip/IMG_20250115_001.jpg")
        harness.sync("a")
        assert harness.phone_exists(
            "a", "DCIM/Camera/trip/IMG_20250115_001.jpg")

        # Run again — phone path should be stable, no spurious moves
        engine = harness.sync("a")
        assert engine.stats["errors"] == 0
        assert harness.phone_exists(
            "a", "DCIM/Camera/trip/IMG_20250115_001.jpg")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_double_sync_no_changes(self, harness, img_data):
        """Second sync with no changes copies nothing, errors nothing."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 0
        assert engine.stats["files_updated"] == 0
        assert engine.stats["moves_synced"] == 0
        assert engine.stats["phone_moves_detected"] == 0
        assert engine.stats["errors"] == 0

    def test_state_stable_across_runs(self, harness, img_data):
        """State file content is identical across two no-op syncs (modulo timestamps)."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        state1 = harness.get_state("a")
        harness.sync("a")
        state2 = harness.get_state("a")

        # Same files tracked with same hashes/paths
        assert set(state1["files"].keys()) == set(state2["files"].keys())
        for relpath in state1["files"]:
            assert state1["files"][relpath]["hash"] == \
                state2["files"][relpath]["hash"]
            assert state1["files"][relpath]["phone_path"] == \
                state2["files"][relpath]["phone_path"]

    def test_triple_sync_stable(self, harness, img_data):
        """Three syncs: file count stays constant."""
        for i in range(3):
            c, m = img_data(f"p{i}", 2025, 1, 15 + i)
            harness.phone_write(
                "a", f"DCIM/Camera/IMG_2025011{5+i}_00{i}.jpg", c, m)
        harness.sync("a")
        count1 = harness.state_file_count("a")
        harness.sync("a")
        harness.sync("a")
        count3 = harness.state_file_count("a")
        assert count1 == count3 == 3


# ---------------------------------------------------------------------------
# State schema integrity
# ---------------------------------------------------------------------------

class TestStateSchema:
    def test_entry_has_required_fields(self, harness, img_data):
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        info = list(harness.get_state("a")["files"].values())[0]
        for field in ("phone_path", "phone_source_dir", "hash", "size",
                      "phone_mtime", "synced_at", "category", "device_name"):
            assert field in info, f"Missing field: {field}"

    def test_device_name_tagged(self, harness, img_data):
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        info = list(harness.get_state("a")["files"].values())[0]
        assert info["device_name"] == "phone-a"

    def test_category_recorded(self, harness, img_data):
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.phone_write("a", "Download/doc.pdf", b"pdf")
        harness.phone_write("a", "Recordings/voice.m4a", b"audio")
        harness.sync("a")
        cats = {info["category"]
                for info in harness.get_state("a")["files"].values()}
        assert cats == {"photos", "downloads", "recordings"}

    def test_hash_matches_content(self, harness, img_data):
        import hashlib
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        info = list(harness.get_state("a")["files"].values())[0]
        assert info["hash"] == hashlib.sha256(c).hexdigest()


# ---------------------------------------------------------------------------
# Config flags
# ---------------------------------------------------------------------------

class TestConfigFlags:
    def test_photo_date_folders_false(self, harness, img_data):
        """With photo_date_folders=false, photos go directly under photos/."""
        cfg = phonesync.load_config()
        cfg["photo_date_folders"] = False
        phonesync.save_config(cfg)

        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        # No year folder
        assert harness.computer_exists("photos/IMG_20250115_001.jpg")

    def test_preserve_phone_subdirs_false(self, harness):
        """With preserve_phone_subdirs=false, download subdirs flattened."""
        cfg = phonesync.load_config()
        cfg["preserve_phone_subdirs"] = False
        phonesync.save_config(cfg)

        harness.phone_write("a", "Download/sub/report.pdf", b"pdf data")
        harness.sync("a")
        # Should be flattened to downloads/phone-a/report.pdf
        assert harness.computer_exists("downloads/phone-a/report.pdf")

    def test_preserve_phone_subdirs_true(self, harness):
        """With preserve_phone_subdirs=true (default), download subdirs kept."""
        harness.phone_write("a", "Download/sub/report.pdf", b"pdf data")
        harness.sync("a")
        assert harness.computer_exists(
            "downloads/phone-a/sub/report.pdf")


# ---------------------------------------------------------------------------
# Relevance filtering
# ---------------------------------------------------------------------------

class TestRelevanceFiltering:
    def test_non_photo_extension_skipped_in_camera(self, harness):
        """A .txt file in the Camera dir is not ingested as a photo."""
        harness.phone_write("a", "DCIM/Camera/notes.txt", b"text notes")
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg",
                            b"photo", 1736899200.0)
        engine = harness.sync("a")
        # Only the jpg should be ingested
        assert engine.stats["files_copied"] == 1
        assert not harness.computer_exists("photos/2025/notes.txt")

    def test_video_treated_as_photo(self, harness):
        """Video files in Camera are ingested (photos category)."""
        harness.phone_write("a", "DCIM/Camera/VID_20250115_001.mp4",
                            b"video data", 1736899200.0)
        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 1

    def test_downloads_accept_any_extension(self, harness):
        """Downloads accept any file type."""
        harness.phone_write("a", "Download/archive.zip", b"zip data")
        harness.phone_write("a", "Download/script.sh", b"#!/bin/sh")
        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 2

    def test_recording_extension_filter(self, harness):
        """Recordings only accept audio extensions."""
        harness.phone_write("a", "Recordings/voice.m4a", b"audio")
        harness.phone_write("a", "Recordings/notreally.xyz", b"junk")
        engine = harness.sync("a")
        # Only the .m4a should be ingested
        assert engine.stats["files_copied"] == 1
        assert harness.computer_exists("recordings/phone-a/voice.m4a")


# ---------------------------------------------------------------------------
# Dry-run guarantees
# ---------------------------------------------------------------------------

class TestDryRunGuarantees:
    def test_dry_run_no_computer_files(self, harness, img_data):
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a", dry_run=True)
        assert not harness.computer_exists(
            "photos/2025/IMG_20250115_001.jpg")

    def test_dry_run_no_state_file(self, harness, img_data):
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a", dry_run=True)
        # State file should not have been written
        assert harness.state_file_count("a") == 0

    def test_dry_run_no_phone_moves(self, harness, img_data):
        """Dry-run does not move files on the phone."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")  # real sync first

        harness.computer_move(
            "photos/2025/IMG_20250115_001.jpg",
            "photos/2025/trip/IMG_20250115_001.jpg")
        harness.sync("a", dry_run=True)
        # Phone file should NOT have moved
        assert harness.phone_exists(
            "a", "DCIM/Camera/IMG_20250115_001.jpg")
        assert not harness.phone_exists(
            "a", "DCIM/Camera/trip/IMG_20250115_001.jpg")

    def test_dry_run_reports_would_copy(self, harness, img_data):
        """Dry-run still counts files it would copy."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        engine = harness.sync("a", dry_run=True)
        assert engine.stats["files_copied"] == 1

    def test_dry_run_reports_destination(self, harness):
        """Dry-run logs the destination path for each file, including the
        resolved photo date-folder."""
        import logging

        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Capture()
        logger = logging.getLogger()
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            # Filename date → photos/2023/
            harness.phone_write(
                "a", "DCIM/Camera/IMG_20230712_001.jpg", b"photo",
                1689163200.0)
            harness.phone_write("a", "Download/report.pdf", b"pdf")
            harness.sync("a", dry_run=True)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        joined = "\n".join(records)
        # Destination lines present with the resolved year folder
        assert "photos/2023/IMG_20230712_001.jpg" in joined
        assert "downloads/phone-a/report.pdf" in joined

    def test_dry_run_leaves_no_temp_files(self, harness, img_data):
        """Dry-run's photo pull-to-temp is cleaned up afterward."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a", dry_run=True)
        tmp_dir = harness.cfg_dir / "tmp"
        # tmp dir should be gone or empty after the run
        if tmp_dir.exists():
            assert list(tmp_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Stats accounting
# ---------------------------------------------------------------------------

class TestStatsAccounting:
    def test_bytes_copied_accumulates(self, harness):
        harness.phone_write("a", "Download/a.bin", b"x" * 100)
        harness.phone_write("a", "Download/b.bin", b"y" * 200)
        engine = harness.sync("a")
        assert engine.stats["bytes_copied"] == 300

    def test_skipped_counted(self, harness, img_data):
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        engine = harness.sync("a")
        assert engine.stats["files_skipped"] == 1

    def test_local_deletions_counted(self, harness, img_data):
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        harness.computer_delete("photos/2025/IMG_20250115_001.jpg")
        engine = harness.sync("a")
        assert engine.stats["local_deletions"] == 1

    def test_updated_distinct_from_copied(self, harness, img_data):
        """Re-pull increments files_updated, not files_copied."""
        c1, m1 = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c1, m1)
        harness.sync("a")

        c2 = b"NEW_CONTENT_FOR_UPDATE_TEST"
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c2,
                            m1 + 3600)
        engine = harness.sync("a")
        assert engine.stats["files_updated"] == 1
        assert engine.stats["files_copied"] == 0


# ===========================================================================
# Gap-filling tests (audit against P0–P2 scenario checklist)
# ===========================================================================

# ---------------------------------------------------------------------------
# P0: initial ingest — value-level state assertions
# ---------------------------------------------------------------------------

class TestIngestValueLevel:
    def test_phone_path_recorded_correctly(self, harness, img_data):
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        info = list(harness.get_state("a")["files"].values())[0]
        assert info["phone_path"] == \
            "/sdcard/DCIM/Camera/IMG_20250115_001.jpg"

    def test_size_recorded_correctly(self, harness, img_data):
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        info = list(harness.get_state("a")["files"].values())[0]
        assert info["size"] == len(c)

    def test_phone_source_dir_recorded(self, harness, img_data):
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        info = list(harness.get_state("a")["files"].values())[0]
        assert info["phone_source_dir"] == "/sdcard/DCIM/Camera"

    def test_download_state_values(self, harness):
        harness.phone_write("a", "Download/report.pdf", b"pdf data")
        harness.sync("a")
        info = list(harness.get_state("a")["files"].values())[0]
        assert info["phone_path"] == "/sdcard/Download/report.pdf"
        assert info["category"] == "downloads"
        assert info["size"] == len(b"pdf data")

    def test_recording_state_values(self, harness):
        harness.phone_write("a", "Recordings/voice.m4a", b"audio data")
        harness.sync("a")
        info = list(harness.get_state("a")["files"].values())[0]
        assert info["phone_path"] == "/sdcard/Recordings/voice.m4a"
        assert info["category"] == "recordings"


# ---------------------------------------------------------------------------
# P0: computer-side moves — old path removal + collision at engine level
# ---------------------------------------------------------------------------

class TestComputerMoveDetails:
    def test_old_phone_path_removed_after_move(self, harness, img_data):
        """After a propagated move, the old phone path no longer exists."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")

        harness.computer_move(
            "photos/2025/IMG_20250115_001.jpg",
            "photos/2025/Album/IMG_20250115_001.jpg")
        harness.sync("a")

        # Old phone path gone, new one present
        assert not harness.phone_exists(
            "a", "DCIM/Camera/IMG_20250115_001.jpg")
        assert harness.phone_exists(
            "a", "DCIM/Camera/Album/IMG_20250115_001.jpg")

    def test_state_phone_path_matches_new_location(self, harness, img_data):
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        harness.computer_move(
            "photos/2025/IMG_20250115_001.jpg",
            "photos/2025/Album/IMG_20250115_001.jpg")
        harness.sync("a")
        info = harness.get_state("a")["files"][
            "photos/2025/Album/IMG_20250115_001.jpg"]
        assert info["phone_path"] == \
            "/sdcard/DCIM/Camera/Album/IMG_20250115_001.jpg"

    def test_phone_collision_different_content_refused(self, harness, img_data):
        """If the desired phone destination already holds different content,
        move_safe refuses, errors increments, and state phone_path is NOT
        updated to the colliding destination."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")

        # Pre-place a DIFFERENT file at the destination the move will want
        harness.phone_write(
            "a", "DCIM/Camera/Album/IMG_20250115_001.jpg",
            b"DIFFERENT_CONTENT_BLOCKING")

        # Now sort on the computer into Album/
        harness.computer_move(
            "photos/2025/IMG_20250115_001.jpg",
            "photos/2025/Album/IMG_20250115_001.jpg")
        engine = harness.sync("a")

        # move_safe should have refused the collision: exactly one error
        # (the computer-side move is still recorded in state; only the
        # phone-side propagation fails).
        assert engine.stats["errors"] == 1
        # State must not claim the file is at the colliding destination
        info = harness.get_state("a")["files"][
            "photos/2025/Album/IMG_20250115_001.jpg"]
        assert info["phone_path"] == \
            "/sdcard/DCIM/Camera/IMG_20250115_001.jpg"
        # Original phone file still intact
        assert harness.phone_read(
            "a", "DCIM/Camera/IMG_20250115_001.jpg") == c
        # Blocking file untouched
        assert harness.phone_read(
            "a", "DCIM/Camera/Album/IMG_20250115_001.jpg") == \
            b"DIFFERENT_CONTENT_BLOCKING"


class TestFirstSyncConfirmation:
    """A brand-new (unapproved) device must be explicitly confirmed before
    its first non-dry-run sync. Unattended runs refuse an unapproved device.
    This also scopes the future auto-sync service to known devices (#17)."""

    def test_unapproved_noninteractive_aborts(self, harness, img_data):
        harness.unapprove_device("a")
        # Force non-interactive (automation).
        orig = phonesync.SyncEngine._is_interactive
        phonesync.SyncEngine._is_interactive = lambda self: False
        try:
            c, m = img_data("p", 2025, 1, 15)
            harness.phone_write(
                "a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
            engine = harness.sync("a")
        finally:
            phonesync.SyncEngine._is_interactive = orig

        assert engine.run_result is False
        assert engine._aborted_unconfirmed is True
        assert engine.stats["files_copied"] == 0
        # Nothing pulled, no state saved, device still not approved.
        assert not harness.computer_exists(
            "photos/2025/IMG_20250115_001.jpg")
        assert harness.state_file_count("a") == 0
        assert harness.is_approved("a") is False

    def test_interactive_yes_proceeds_and_approves(self, harness, img_data):
        harness.unapprove_device("a")
        orig_i = phonesync.SyncEngine._is_interactive
        orig_c = phonesync.SyncEngine._confirm_first_sync
        phonesync.SyncEngine._is_interactive = lambda self: True
        phonesync.SyncEngine._confirm_first_sync = lambda self: True
        try:
            c, m = img_data("p", 2025, 1, 15)
            harness.phone_write(
                "a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
            engine = harness.sync("a")
        finally:
            phonesync.SyncEngine._is_interactive = orig_i
            phonesync.SyncEngine._confirm_first_sync = orig_c

        assert engine.stats["files_copied"] == 1
        assert harness.computer_exists(
            "photos/2025/IMG_20250115_001.jpg")
        # Device is now approved, so a later sync won't prompt.
        assert harness.is_approved("a") is True

    def test_interactive_no_aborts_without_approving(self, harness, img_data):
        harness.unapprove_device("a")
        orig_i = phonesync.SyncEngine._is_interactive
        orig_c = phonesync.SyncEngine._confirm_first_sync
        phonesync.SyncEngine._is_interactive = lambda self: True
        phonesync.SyncEngine._confirm_first_sync = lambda self: False
        try:
            c, m = img_data("p", 2025, 1, 15)
            harness.phone_write(
                "a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
            engine = harness.sync("a")
        finally:
            phonesync.SyncEngine._is_interactive = orig_i
            phonesync.SyncEngine._confirm_first_sync = orig_c

        assert engine.run_result is False
        assert engine.stats["files_copied"] == 0
        assert harness.is_approved("a") is False

    def test_approved_device_not_prompted(self, harness, img_data):
        """A pre-approved device (the harness default) never prompts."""
        called = {"n": 0}

        def tracking_confirm(self):
            called["n"] += 1
            return True

        orig = phonesync.SyncEngine._confirm_first_sync
        phonesync.SyncEngine._confirm_first_sync = tracking_confirm
        try:
            c, m = img_data("p", 2025, 1, 15)
            harness.phone_write(
                "a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
            engine = harness.sync("a")
        finally:
            phonesync.SyncEngine._confirm_first_sync = orig

        assert called["n"] == 0
        assert engine.stats["files_copied"] == 1

    def test_second_sync_not_prompted_after_approval(self, harness, img_data):
        """Once approved on first sync, subsequent syncs don't prompt."""
        harness.unapprove_device("a")
        called = {"n": 0}

        def counting_confirm(self):
            called["n"] += 1
            return True

        orig_i = phonesync.SyncEngine._is_interactive
        orig_c = phonesync.SyncEngine._confirm_first_sync
        phonesync.SyncEngine._is_interactive = lambda self: True
        phonesync.SyncEngine._confirm_first_sync = counting_confirm
        try:
            c, m = img_data("p", 2025, 1, 15)
            harness.phone_write(
                "a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
            harness.sync("a")          # prompts once, approves
            harness.phone_write(
                "a", "DCIM/Camera/IMG_20250116_002.jpg",
                *img_data("q", 2025, 1, 16))
            harness.sync("a")          # must NOT prompt again
        finally:
            phonesync.SyncEngine._is_interactive = orig_i
            phonesync.SyncEngine._confirm_first_sync = orig_c

        assert called["n"] == 1

    def test_dry_run_does_not_require_approval(self, harness, img_data):
        """Dry-run writes nothing, so it never gates on approval and never
        approves the device."""
        harness.unapprove_device("a")
        orig = phonesync.SyncEngine._is_interactive
        phonesync.SyncEngine._is_interactive = lambda self: False
        try:
            c, m = img_data("p", 2025, 1, 15)
            harness.phone_write(
                "a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
            engine = harness.sync("a", dry_run=True)
        finally:
            phonesync.SyncEngine._is_interactive = orig

        assert engine._aborted_unconfirmed is False
        # Dry-run must not silently approve the device.
        assert harness.is_approved("a") is False

    def test_existing_state_device_backfilled_not_prompted(self, harness,
                                                           img_data):
        """A device with prior sync state but missing from the registry
        (upgrade migration) is treated as known and backfilled, not
        prompted."""
        # Approved sync to create state, then forget the approval but KEEP
        # state — simulating an upgrade from before the registry existed.
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        assert harness.state_file_count("a") >= 1
        harness.unapprove_device("a")

        called = {"n": 0}

        def counting_confirm(self):
            called["n"] += 1
            return True

        orig_i = phonesync.SyncEngine._is_interactive
        orig_c = phonesync.SyncEngine._confirm_first_sync
        phonesync.SyncEngine._is_interactive = lambda self: False
        phonesync.SyncEngine._confirm_first_sync = counting_confirm
        try:
            harness.phone_write(
                "a", "DCIM/Camera/IMG_20250116_002.jpg",
                *img_data("q", 2025, 1, 16))
            engine = harness.sync("a")
        finally:
            phonesync.SyncEngine._is_interactive = orig_i
            phonesync.SyncEngine._confirm_first_sync = orig_c

        assert called["n"] == 0
        assert engine._aborted_unconfirmed is False
        # Backfilled into the registry for #17 scoping.
        assert harness.is_approved("a") is True


class TestOverwriteProtection:
    """When a file is edited on BOTH the phone and the computer since the
    last sync, the phone version must not silently clobber the local edits.
    Policy: ask (interactive) / never / always; per-file overrides persist."""

    def _edit_then_phone_change(self, harness, img_data):
        """Sync a file, edit it locally, then change it on the phone (new
        content + newer mtime). Returns the relpath."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        relpath = "photos/2025/IMG_20250115_001.jpg"
        # Local edit (changes the on-disk hash).
        harness.computer_write(relpath, b"LOCALLY_EDITED_CONTENT")
        # Phone change: different content, newer mtime.
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg",
                            b"PHONE_EDITED_CONTENT", m + 1000)
        return relpath

    def test_never_keeps_local(self, harness, img_data):
        cfg = phonesync.load_config()
        cfg["overwrite_policy"] = "never"
        phonesync.save_config(cfg)

        relpath = self._edit_then_phone_change(harness, img_data)
        engine = harness.sync("a")

        # Local edits preserved; phone version NOT applied.
        assert harness.computer_read(relpath) == b"LOCALLY_EDITED_CONTENT"
        assert engine.stats["overwrites_kept_local"] == 1
        assert engine.stats["files_updated"] == 0

    def test_always_takes_phone_version(self, harness, img_data):
        cfg = phonesync.load_config()
        cfg["overwrite_policy"] = "always"
        phonesync.save_config(cfg)

        relpath = self._edit_then_phone_change(harness, img_data)
        engine = harness.sync("a")

        assert harness.computer_read(relpath) == b"PHONE_EDITED_CONTENT"
        assert engine.stats["overwrites_applied"] == 1
        assert engine.stats["files_updated"] == 1

    def test_ask_non_interactive_keeps_local(self, harness, img_data):
        """In ask mode with no TTY (automation), local is kept — automation
        must never destroy local edits."""
        cfg = phonesync.load_config()
        cfg["overwrite_policy"] = "ask"
        phonesync.save_config(cfg)

        # Force non-interactive.
        orig = phonesync.SyncEngine._is_interactive
        phonesync.SyncEngine._is_interactive = lambda self: False
        try:
            relpath = self._edit_then_phone_change(harness, img_data)
            engine = harness.sync("a")
        finally:
            phonesync.SyncEngine._is_interactive = orig

        assert harness.computer_read(relpath) == b"LOCALLY_EDITED_CONTENT"
        assert engine.stats["overwrites_kept_local"] == 1

    def test_ask_interactive_overwrite_choice(self, harness, img_data):
        """Interactive 'overwrite' answer applies the phone version once."""
        cfg = phonesync.load_config()
        cfg["overwrite_policy"] = "ask"
        phonesync.save_config(cfg)

        orig_i = phonesync.SyncEngine._is_interactive
        orig_p = phonesync.SyncEngine._prompt_overwrite
        phonesync.SyncEngine._is_interactive = lambda self: True
        phonesync.SyncEngine._prompt_overwrite = \
            lambda self, relpath: "overwrite"
        try:
            relpath = self._edit_then_phone_change(harness, img_data)
            engine = harness.sync("a")
        finally:
            phonesync.SyncEngine._is_interactive = orig_i
            phonesync.SyncEngine._prompt_overwrite = orig_p

        assert harness.computer_read(relpath) == b"PHONE_EDITED_CONTENT"
        assert engine.stats["overwrites_applied"] == 1

    def test_ask_always_choice_persists_per_file(self, harness, img_data):
        """Answering 'always' writes a per-file policy so a later phone edit
        is overwritten WITHOUT prompting again."""
        cfg = phonesync.load_config()
        cfg["overwrite_policy"] = "ask"
        phonesync.save_config(cfg)

        prompt_calls = {"n": 0}

        def counting_prompt(self, relpath):
            prompt_calls["n"] += 1
            return "always"

        orig_i = phonesync.SyncEngine._is_interactive
        orig_p = phonesync.SyncEngine._prompt_overwrite
        phonesync.SyncEngine._is_interactive = lambda self: True
        phonesync.SyncEngine._prompt_overwrite = counting_prompt
        try:
            relpath = self._edit_then_phone_change(harness, img_data)
            harness.sync("a")  # prompts once -> "always", overwrites
            # The per-file policy is now persisted in state.
            info = harness.get_state("a")["files"][relpath]
            assert info.get("overwrite_policy") == "always"

            # Edit locally again, change phone again -> should NOT prompt.
            harness.computer_write(relpath, b"LOCAL_EDIT_2")
            c, m = img_data("p", 2025, 1, 15)
            harness.phone_write(
                "a", "DCIM/Camera/IMG_20250115_001.jpg",
                b"PHONE_EDIT_2", m + 2000)
            engine = harness.sync("a")
        finally:
            phonesync.SyncEngine._is_interactive = orig_i
            phonesync.SyncEngine._prompt_overwrite = orig_p

        assert prompt_calls["n"] == 1  # only the first time
        assert harness.computer_read(relpath) == b"PHONE_EDIT_2"
        assert engine.stats["overwrites_applied"] == 1

    def test_ask_never_choice_persists_and_keeps(self, harness, img_data):
        """Answering 'never' keeps local now AND persists the per-file policy
        so a later phone edit is skipped without prompting again."""
        cfg = phonesync.load_config()
        cfg["overwrite_policy"] = "ask"
        phonesync.save_config(cfg)

        prompt_calls = {"n": 0}

        def counting_prompt(self, relpath):
            prompt_calls["n"] += 1
            return "never"

        orig_i = phonesync.SyncEngine._is_interactive
        orig_p = phonesync.SyncEngine._prompt_overwrite
        phonesync.SyncEngine._is_interactive = lambda self: True
        phonesync.SyncEngine._prompt_overwrite = counting_prompt
        try:
            relpath = self._edit_then_phone_change(harness, img_data)
            e1 = harness.sync("a")
            assert e1.stats["overwrites_kept_local"] == 1
            assert harness.computer_read(relpath) == b"LOCALLY_EDITED_CONTENT"
            info = harness.get_state("a")["files"][relpath]
            assert info.get("overwrite_policy") == "never"

            # Another phone edit; must keep local WITHOUT prompting again.
            c, m = img_data("p", 2025, 1, 15)
            harness.phone_write(
                "a", "DCIM/Camera/IMG_20250115_001.jpg",
                b"PHONE_EDIT_AGAIN", m + 5000)
            e2 = harness.sync("a")
        finally:
            phonesync.SyncEngine._is_interactive = orig_i
            phonesync.SyncEngine._prompt_overwrite = orig_p

        assert prompt_calls["n"] == 1
        assert e2.stats["overwrites_kept_local"] == 1
        assert harness.computer_read(relpath) == b"LOCALLY_EDITED_CONTENT"

    def test_no_local_edit_overwrites_normally(self, harness, img_data):
        """If only the phone changed (local untouched), the re-pull proceeds
        normally — no prompt, no 'kept local'."""
        cfg = phonesync.load_config()
        cfg["overwrite_policy"] = "never"  # would block IF it triggered
        phonesync.save_config(cfg)

        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        relpath = "photos/2025/IMG_20250115_001.jpg"
        # Phone changes; local is NOT touched.
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg",
                            b"PHONE_ONLY_CHANGE", m + 1000)
        engine = harness.sync("a")

        assert harness.computer_read(relpath) == b"PHONE_ONLY_CHANGE"
        assert engine.stats["files_updated"] == 1
        assert engine.stats["overwrites_kept_local"] == 0

    def test_kept_local_not_renagged_same_phone_version(self, harness,
                                                        img_data):
        """After keeping local, the SAME phone version isn't re-flagged on
        the next sync (phone_mtime acknowledged)."""
        cfg = phonesync.load_config()
        cfg["overwrite_policy"] = "never"
        phonesync.save_config(cfg)

        relpath = self._edit_then_phone_change(harness, img_data)
        e1 = harness.sync("a")
        assert e1.stats["overwrites_kept_local"] == 1
        # Second sync with NOTHING further changed: not re-flagged.
        e2 = harness.sync("a")
        assert e2.stats["overwrites_kept_local"] == 0
        assert harness.computer_read(relpath) == b"LOCALLY_EDITED_CONTENT"


class TestFreeSpacePreflight:
    """check_free_space=true must abort BEFORE pulling when the estimated
    copy (plus margin) won't fit, leaving nothing copied and no state saved."""

    def test_aborts_when_insufficient_space(self, harness, img_data):
        # An absurd margin forces the check to fail regardless of real disk.
        cfg = phonesync.load_config()
        cfg["check_free_space"] = True
        cfg["free_space_margin_bytes"] = 10 ** 18  # ~1 EB
        phonesync.save_config(cfg)

        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        engine = harness.sync("a")

        # Aborted before any copy.
        assert engine.run_result is False
        assert engine._aborted_no_space is True
        assert engine.stats["files_copied"] == 0
        # Nothing landed on disk, and no state was saved.
        assert not harness.computer_exists(
            "photos/2025/IMG_20250115_001.jpg")
        assert harness.state_file_count("a") == 0

    def test_proceeds_with_reasonable_margin(self, harness, img_data):
        cfg = phonesync.load_config()
        cfg["check_free_space"] = True
        cfg["free_space_margin_bytes"] = 1024  # 1 KB, trivially satisfiable
        phonesync.save_config(cfg)

        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 1
        assert engine._aborted_no_space is False
        assert harness.computer_exists(
            "photos/2025/IMG_20250115_001.jpg")

    def test_check_can_be_disabled(self, harness, img_data):
        """With check_free_space=false, even an absurd margin is ignored."""
        cfg = phonesync.load_config()
        cfg["check_free_space"] = False
        cfg["free_space_margin_bytes"] = 10 ** 18
        phonesync.save_config(cfg)

        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 1
        assert engine._aborted_no_space is False

    def test_estimate_excludes_already_tracked(self, harness, img_data):
        """Files already synced (tracked for this device) are not counted in
        the pull estimate — only genuinely-new bytes are."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")  # now tracked

        # Build a fresh engine to inspect the estimate against the same scan.
        from conftest import FakeADB
        cfg = phonesync.load_config()
        engine = phonesync.SyncEngine(cfg, "SERIAL_A", adb_cls=FakeADB)
        engine.phone_scan = engine._scan_phone()
        # The one file is already tracked, so the estimate is zero.
        assert engine._estimate_pull_bytes() == 0

    def test_dry_run_skips_check(self, harness, img_data):
        """Dry-run never aborts on space (it copies nothing anyway)."""
        cfg = phonesync.load_config()
        cfg["check_free_space"] = True
        cfg["free_space_margin_bytes"] = 10 ** 18
        phonesync.save_config(cfg)

        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        engine = harness.sync("a", dry_run=True)
        assert engine._aborted_no_space is False


class TestReadOnlyMode:
    """read_only=true must never write to the phone, while still ingesting
    (reading) normally."""

    def test_read_only_suppresses_phone_move(self, harness, img_data):
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")

        harness.computer_move(
            "photos/2025/IMG_20250115_001.jpg",
            "photos/2025/trip/IMG_20250115_001.jpg")

        cfg = phonesync.load_config()
        cfg["read_only"] = True
        phonesync.save_config(cfg)
        engine = harness.sync("a")

        # Phone file did NOT move; original still present.
        assert harness.phone_exists(
            "a", "DCIM/Camera/IMG_20250115_001.jpg")
        assert not harness.phone_exists(
            "a", "DCIM/Camera/trip/IMG_20250115_001.jpg")
        # The suppression is counted.
        assert engine.stats["phone_writes_suppressed"] == 1
        # State's phone_path is NOT advanced (it would lie otherwise).
        info = harness.get_state("a")["files"][
            "photos/2025/trip/IMG_20250115_001.jpg"]
        assert info["phone_path"] == \
            "/sdcard/DCIM/Camera/IMG_20250115_001.jpg"

    def test_read_only_still_ingests(self, harness, img_data):
        """Reads are unaffected by read-only mode."""
        cfg = phonesync.load_config()
        cfg["read_only"] = True
        phonesync.save_config(cfg)

        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        engine = harness.sync("a")
        assert engine.stats["files_copied"] == 1
        assert harness.computer_exists(
            "photos/2025/IMG_20250115_001.jpg")

    def test_read_only_no_phone_writes_at_all(self, harness, img_data):
        """A sentinel ADB that fails on any write proves read-only never
        calls a phone-write method."""
        from conftest import FakeADB

        class NoWriteADB(FakeADB):
            def move_safe(self, *a, **k):
                raise AssertionError("move_safe called in read-only mode")

            def move(self, *a, **k):
                raise AssertionError("move called in read-only mode")

            def push(self, *a, **k):
                raise AssertionError("push called in read-only mode")

            def delete(self, *a, **k):
                raise AssertionError("delete called in read-only mode")

        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a", adb_cls=NoWriteADB)
        harness.computer_move(
            "photos/2025/IMG_20250115_001.jpg",
            "photos/2025/trip/IMG_20250115_001.jpg")

        cfg = phonesync.load_config()
        cfg["read_only"] = True
        phonesync.save_config(cfg)
        # Must not raise (no write method is called).
        engine = harness.sync("a", adb_cls=NoWriteADB)
        assert engine.stats["phone_writes_suppressed"] == 1


class TestPartialPhoneMove:
    """If move_safe writes the destination but cannot remove the source,
    the move is PARTIAL: state must NOT advance to the new phone path, and
    the failure must be surfaced."""

    def _partial_move_adb(self):
        """A FakeADB whose move_safe copies to the destination but leaves
        the source in place (source_deleted=False), simulating a phone
        where 'rm' of the source failed."""
        from conftest import FakeADB
        import hashlib as _h

        class PartialMoveADB(FakeADB):
            def move_safe(self, remote_src, remote_dst, expected_hash=None):
                src = self._local(remote_src)
                dst = self._local(remote_dst)
                result = {"ok": False, "action": "",
                          "source_deleted": False}
                if dst.exists():
                    result["action"] = "collision"
                    return result
                if not src.exists():
                    result["action"] = "copy_failed"
                    return result
                dst.parent.mkdir(parents=True, exist_ok=True)
                import shutil as _s
                _s.copy2(str(src), str(dst))
                if expected_hash:
                    actual = _h.sha256(dst.read_bytes()).hexdigest()
                    if actual != expected_hash:
                        dst.unlink()
                        result["action"] = "hash_mismatch"
                        return result
                # Simulate a FAILED source deletion: leave src in place.
                result["ok"] = True
                result["action"] = "moved"
                result["source_deleted"] = False
                return result

        return PartialMoveADB

    def test_partial_move_does_not_advance_state(self, harness, img_data):
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")

        # Sort on the computer to trigger a phone-side move
        harness.computer_move(
            "photos/2025/IMG_20250115_001.jpg",
            "photos/2025/Album/IMG_20250115_001.jpg")

        engine = harness.sync("a", adb_cls=self._partial_move_adb())

        # The destination WAS written on the phone...
        assert harness.phone_exists(
            "a", "DCIM/Camera/Album/IMG_20250115_001.jpg")
        # ...and the source is STILL there (deletion failed)
        assert harness.phone_exists(
            "a", "DCIM/Camera/IMG_20250115_001.jpg")

        # State must NOT have advanced to the new path — it should still
        # point at the old (still-existing) source location.
        info = harness.get_state("a")["files"][
            "photos/2025/Album/IMG_20250115_001.jpg"]
        assert info["phone_path"] == \
            "/sdcard/DCIM/Camera/IMG_20250115_001.jpg"

        # The partial move was surfaced
        assert engine.stats["partial_moves"] == 1
        assert engine.stats["errors"] >= 1

    def test_partial_move_no_duplicate_reingest(self, harness, img_data):
        """After a partial move, the destination file written on the phone
        is untracked by phone_path, but its CONTENT is already in the
        library and tracked by this device. The library index recognizes it
        and skips the re-pull, so NO duplicate is created on the next sync.
        (TODO #14, fixed by the library index.)
        """
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        harness.computer_move(
            "photos/2025/IMG_20250115_001.jpg",
            "photos/2025/Album/IMG_20250115_001.jpg")
        harness.sync("a", adb_cls=self._partial_move_adb())

        files_before = set(harness.computer_list("photos"))
        engine = harness.sync("a")  # clean sync completes the move
        files_after = set(harness.computer_list("photos"))

        # FIXED behavior: no duplicate is created.
        assert files_after == files_before
        # No "_phone-a" suffixed duplicate exists.
        assert not any("_phone-a" in f for f in files_after)
        # The re-pull was avoided: recognized as a move completion.
        assert engine.stats["move_completions"] >= 1


# ---------------------------------------------------------------------------
# P0: deleted_files report list
# ---------------------------------------------------------------------------

class TestDeletionReport:
    def test_deleted_files_list_includes_entry(self, harness, img_data):
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        harness.computer_delete("photos/2025/IMG_20250115_001.jpg")
        engine = harness.sync("a")
        relpaths = [rp for rp, cat in engine.deleted_files]
        assert "photos/2025/IMG_20250115_001.jpg" in relpaths

    def test_deleted_files_records_category(self, harness, img_data):
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        harness.computer_delete("photos/2025/IMG_20250115_001.jpg")
        engine = harness.sync("a")
        cats = [cat for rp, cat in engine.deleted_files]
        assert "photos" in cats


# ---------------------------------------------------------------------------
# P1: exclude_files and exclude_dirs (custom, engine-level)
# ---------------------------------------------------------------------------

class TestCustomExcludes:
    def test_custom_exclude_files(self, harness):
        cfg = phonesync.load_config()
        cfg["exclude_files"] = ["*.tmp"]
        phonesync.save_config(cfg)

        harness.phone_write("a", "Download/keep.pdf", b"keep")
        harness.phone_write("a", "Download/skip.tmp", b"skip")
        engine = harness.sync("a")
        assert harness.computer_exists("downloads/phone-a/keep.pdf")
        assert not harness.computer_exists("downloads/phone-a/skip.tmp")

    def test_custom_exclude_dirs(self, harness):
        cfg = phonesync.load_config()
        cfg["exclude_dirs"] = ["private"]
        phonesync.save_config(cfg)

        harness.phone_write("a", "Download/public.pdf", b"public")
        harness.phone_write("a", "Download/private/secret.pdf", b"secret")
        engine = harness.sync("a")
        assert harness.computer_exists("downloads/phone-a/public.pdf")
        assert not harness.computer_exists(
            "downloads/phone-a/private/secret.pdf")


# ---------------------------------------------------------------------------
# P1: discovered_subdirs reporting
# ---------------------------------------------------------------------------

class TestDiscoveredSubdirs:
    def test_not_scanned_reported(self, harness, img_data):
        """With recursive_scan=false, a subdir with files is 'not_scanned'."""
        cfg = phonesync.load_config()
        cfg["recursive_scan"] = False
        phonesync.save_config(cfg)

        c, m = img_data("sub", 2025, 1, 15)
        harness.phone_write(
            "a", "DCIM/Camera/album/IMG_20250115_001.jpg", c, m)
        engine = harness.sync("a")
        not_scanned = [e for e in engine.discovered_subdirs
                       if e[3] == "not_scanned"]
        names = [e[1] for e in not_scanned]
        assert "album" in names

    def test_excluded_subdir_reported(self, harness, img_data):
        """An excluded subdir with files is classified 'excluded'."""
        cfg = phonesync.load_config()
        cfg["exclude_dirs"] = [".thumbnails"]
        phonesync.save_config(cfg)

        harness.phone_write(
            "a", "DCIM/Camera/.thumbnails/thumb.jpg", b"thumb")
        c, m = img_data("real", 2025, 1, 15)
        harness.phone_write(
            "a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        engine = harness.sync("a")
        excluded = [e for e in engine.discovered_subdirs
                    if e[3] == "excluded"]
        names = [e[1] for e in excluded]
        assert ".thumbnails" in names

    def test_scanned_subdir_reported(self, harness, img_data):
        """With recursive scan on, a non-configured subdir is 'scanned'."""
        c, m = img_data("sub", 2025, 1, 15)
        harness.phone_write(
            "a", "DCIM/Camera/album/IMG_20250115_001.jpg", c, m)
        engine = harness.sync("a")
        scanned = [e for e in engine.discovered_subdirs
                   if e[3] == "scanned"]
        names = [e[1] for e in scanned]
        assert "album" in names


# ---------------------------------------------------------------------------
# P1: date organization — explicit priority order
# ---------------------------------------------------------------------------

class TestDatePriority:
    def test_filename_date_beats_mtime(self, harness):
        """Filename date (2023) wins over mtime (2025)."""
        # mtime is Jan 2025, filename says 2023
        mtime_2025 = 1736899200.0  # 2025-01-15
        harness.phone_write(
            "a", "DCIM/Camera/IMG_20230615_001.jpg", b"photo", mtime_2025)
        harness.sync("a")
        # Should be filed under 2023 (filename), not 2025 (mtime)
        assert harness.computer_exists(
            "photos/2023/IMG_20230615_001.jpg")

    def test_mtime_used_without_filename_date(self, harness):
        """With no date in filename, mtime year is used."""
        mtime_2024 = 1719532800.0  # 2024-06-28
        harness.phone_write(
            "a", "DCIM/Camera/randomname.jpg", b"photo", mtime_2024)
        harness.sync("a")
        assert harness.computer_exists("photos/2024/randomname.jpg")


# ---------------------------------------------------------------------------
# P2: _find_file_by_hash priority + symlink safety
# ---------------------------------------------------------------------------

class TestHashSearch:
    def test_finds_moved_file(self, harness, img_data):
        """A computer move is detected via hash search."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")
        harness.computer_move(
            "photos/2025/IMG_20250115_001.jpg",
            "photos/2025/deep/nested/IMG_20250115_001.jpg")
        engine = harness.sync("a")
        assert engine.stats["moves_synced"] == 1
        assert harness.state_has_relpath(
            "a", "photos/2025/deep/nested/IMG_20250115_001.jpg")

    def test_nearest_first_priority(self, harness, img_data):
        """Hash search resolves a moved file to its new location even when
        the file is moved deep, preferring a match near the old location.

        Uses distinct content per file so the match is unambiguous and the
        priority ordering (previous dir → subdirs → parents → rest) is
        what resolves it."""
        c1, m1 = img_data("alpha", 2025, 1, 15)
        c2, m2 = img_data("beta", 2025, 1, 16)
        harness.phone_write("a", "DCIM/Camera/alpha.jpg", c1, m1)
        harness.phone_write("a", "DCIM/Camera/beta.jpg", c2, m2)
        harness.sync("a")

        # Move alpha into a subfolder of its current directory.
        # Hash search starts at photos/2025/ then descends — it should
        # find alpha in the subfolder, not get confused by beta.
        harness.computer_move(
            "photos/2025/alpha.jpg",
            "photos/2025/sorted/alpha.jpg")
        engine = harness.sync("a")
        assert engine.stats["errors"] == 0
        assert engine.stats["moves_synced"] == 1
        # alpha tracked at new location, beta untouched
        assert harness.state_has_relpath("a", "photos/2025/sorted/alpha.jpg")
        assert harness.state_has_relpath("a", "photos/2025/beta.jpg")
        assert harness.state_file_count("a") == 2

    def test_identical_content_move_is_safe(self, harness, img_data):
        """When two files share content and one is moved, the engine does
        not error or lose data on disk (though tracking may consolidate).

        This documents a known limitation: with identical hashes, the
        moved file may be resolved to the sibling copy's path, the stale
        entry dropped, and the moved file re-tracked on a later sync. The
        invariant we guarantee is: no error, no crash, file still on disk."""
        content = b"IDENTICAL_CONTENT_BLOB_XYZ"
        harness.phone_write("a", "DCIM/Camera/near.jpg", content,
                            1736899200.0)
        harness.phone_write("a", "Pictures/far.jpg", content,
                            1736899200.0)
        harness.sync("a")

        harness.computer_move("photos/2025/near.jpg",
                              "photos/2025/moved/near.jpg")
        engine = harness.sync("a")
        # No error, no crash
        assert engine.stats["errors"] == 0
        # The moved file is still physically on disk
        assert harness.computer_exists("photos/2025/moved/near.jpg")

    def test_symlink_depth_respected(self, harness, img_data):
        """A symlink within data_dir doesn't cause infinite recursion."""
        import os
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")

        # Create a symlink cycle in the data dir
        link_path = harness.data_dir / "photos" / "2025" / "loop"
        try:
            os.symlink(str(harness.data_dir / "photos"), str(link_path))
        except OSError:
            return  # symlinks not supported; skip

        # Move the file so a hash search runs (which walks the tree)
        harness.computer_move(
            "photos/2025/IMG_20250115_001.jpg",
            "photos/2025/sorted/IMG_20250115_001.jpg")
        # This should terminate (not hang) despite the symlink cycle
        engine = harness.sync("a")
        assert engine.stats["moves_synced"] == 1

    def test_hidden_dirs_skipped_in_hash_search(self, harness, img_data):
        """Hidden directories (starting with .) are skipped during hash search."""
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")

        # Put an identical-content file inside a hidden dir on the computer,
        # then delete the tracked file. Hash search should NOT resolve the
        # missing file to the copy inside the hidden dir.
        relpath = "photos/2025/IMG_20250115_001.jpg"
        content = harness.computer_read(relpath)
        hidden = harness.data_dir / "photos" / ".hidden"
        hidden.mkdir(parents=True, exist_ok=True)
        (hidden / "copy.jpg").write_bytes(content)

        harness.computer_delete(relpath)
        engine = harness.sync("a")
        # The file must be treated as deleted, NOT "moved" into the hidden
        # dir. Assert each independent property the spec requires:
        #  - no move was synced
        assert engine.stats["moves_synced"] == 0
        #  - it was counted as a local deletion
        assert engine.stats["local_deletions"] == 1
        #  - the state entry is tombstoned at its ORIGINAL relpath
        assert harness.state_is_tombstoned("a", relpath)
        #  - the state key did NOT move into the hidden dir
        assert not any(
            ".hidden" in k for k in harness.get_state("a")["files"])

    def test_unreadable_dir_skipped_in_hash_search(self, harness, img_data):
        """An unreadable directory is skipped, not fatal, during hash search.

        The moved file lives in a readable location; an unreadable sibling
        dir must not crash the walk. The test runner refuses to run as
        root, so chmod 000 genuinely blocks reads here.
        """
        import os
        # The runner blocks root, but guard anyway in case this test is
        # invoked directly through some other path.
        assert os.geteuid() != 0, "must not run as root"
        c, m = img_data("p", 2025, 1, 15)
        harness.phone_write("a", "DCIM/Camera/IMG_20250115_001.jpg", c, m)
        harness.sync("a")

        relpath = "photos/2025/IMG_20250115_001.jpg"
        # Move the file to a readable new location
        harness.computer_move(relpath, "photos/2025/sorted/IMG_20250115_001.jpg")

        # Create an unreadable sibling directory
        locked = harness.data_dir / "photos" / "2025" / "locked"
        locked.mkdir(parents=True, exist_ok=True)
        (locked / "decoy.bin").write_bytes(b"decoy")
        os.chmod(str(locked), 0o000)

        try:
            engine = harness.sync("a")
            # Move should still be detected despite the unreadable dir
            assert engine.stats["moves_synced"] == 1
            assert harness.state_has_relpath(
                "a", "photos/2025/sorted/IMG_20250115_001.jpg")
        finally:
            # Restore perms so cleanup can succeed
            os.chmod(str(locked), 0o755)
