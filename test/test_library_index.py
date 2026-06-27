"""Unit tests for LibraryIndex — the on-disk content index that lets the
sync engine skip pulling content already present in the library.

Covers: build/hashing, the persistent cache (hit avoids re-hash, invalidation
on size/mtime change), the pre-run snapshot semantics (so same-run dups don't
suppress each other), the cheap size pre-filter, add/remove mutation, hidden-
directory skipping, and corrupt-cache resilience.
"""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

import phonesync


def _mk(data_dir, relpath, content):
    p = Path(data_dir) / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def _cfg(config_dir):
    return {"config_dir": str(config_dir)}


class TestLibraryIndexBuild:
    def test_build_indexes_content(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d) / "data"
            cfgd = Path(d) / "cfg"
            cfgd.mkdir()
            content = b"HELLO_LIBRARY"
            _mk(data, "photos/2025/a.jpg", content)
            idx = phonesync.LibraryIndex(data, _cfg(cfgd))
            idx.build()
            h = hashlib.sha256(content).hexdigest()
            assert idx.contains_hash(h)
            assert idx.paths_for_hash(h) == ["photos/2025/a.jpg"]

    def test_build_maps_multiple_paths_same_hash(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d) / "data"
            cfgd = Path(d) / "cfg"
            cfgd.mkdir()
            content = b"DUP"
            _mk(data, "photos/2025/a.jpg", content)
            _mk(data, "photos/2025/b.jpg", content)
            idx = phonesync.LibraryIndex(data, _cfg(cfgd))
            idx.build()
            h = hashlib.sha256(content).hexdigest()
            assert idx.paths_for_hash(h) == [
                "photos/2025/a.jpg", "photos/2025/b.jpg"]

    def test_build_skips_hidden_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d) / "data"
            cfgd = Path(d) / "cfg"
            cfgd.mkdir()
            _mk(data, "photos/real.jpg", b"real")
            _mk(data, ".stversions/old.jpg", b"hidden")
            idx = phonesync.LibraryIndex(data, _cfg(cfgd))
            idx.build()
            assert idx.contains_hash(
                hashlib.sha256(b"real").hexdigest())
            assert not idx.contains_hash(
                hashlib.sha256(b"hidden").hexdigest())

    def test_build_on_missing_data_dir(self):
        with tempfile.TemporaryDirectory() as d:
            cfgd = Path(d) / "cfg"
            cfgd.mkdir()
            idx = phonesync.LibraryIndex(Path(d) / "nope", _cfg(cfgd))
            idx.build()  # should not raise
            assert idx.paths_for_hash("x") == []


class TestLibraryIndexCache:
    def test_cache_written(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d) / "data"
            cfgd = Path(d) / "cfg"
            cfgd.mkdir()
            _mk(data, "a.jpg", b"content")
            idx = phonesync.LibraryIndex(data, _cfg(cfgd))
            idx.build()
            cache_file = cfgd / "library-index.json"
            assert cache_file.exists()
            with open(cache_file) as f:
                data_json = json.load(f)
            assert "a.jpg" in data_json["files"]

    def test_cache_hit_avoids_rehash(self):
        """An unchanged file uses the cached hash rather than re-reading."""
        with tempfile.TemporaryDirectory() as d:
            data = Path(d) / "data"
            cfgd = Path(d) / "cfg"
            cfgd.mkdir()
            p = _mk(data, "a.jpg", b"content")
            idx = phonesync.LibraryIndex(data, _cfg(cfgd))
            idx.build()

            # Count file_sha256 calls on the second build.
            calls = {"n": 0}
            orig = phonesync.file_sha256

            def counting(path):
                calls["n"] += 1
                return orig(path)

            phonesync.file_sha256 = counting
            try:
                idx2 = phonesync.LibraryIndex(data, _cfg(cfgd))
                idx2.build()
            finally:
                phonesync.file_sha256 = orig
            # No re-hash: the cache (same size+mtime) was used.
            assert calls["n"] == 0
            assert idx2.contains_hash(
                hashlib.sha256(b"content").hexdigest())

    def test_cache_invalidated_on_mtime_change(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d) / "data"
            cfgd = Path(d) / "cfg"
            cfgd.mkdir()
            p = _mk(data, "a.jpg", b"v1")
            idx = phonesync.LibraryIndex(data, _cfg(cfgd))
            idx.build()

            # Change content AND mtime.
            time.sleep(0.01)
            p.write_bytes(b"v2_longer")
            os.utime(str(p), (time.time() + 5, time.time() + 5))

            idx2 = phonesync.LibraryIndex(data, _cfg(cfgd))
            idx2.build()
            # Old hash gone, new hash present.
            assert not idx2.contains_hash(
                hashlib.sha256(b"v1").hexdigest())
            assert idx2.contains_hash(
                hashlib.sha256(b"v2_longer").hexdigest())

    def test_corrupt_cache_rebuilds(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d) / "data"
            cfgd = Path(d) / "cfg"
            cfgd.mkdir()
            _mk(data, "a.jpg", b"content")
            (cfgd / "library-index.json").write_text("{ this is not json")
            idx = phonesync.LibraryIndex(data, _cfg(cfgd))
            idx.build()  # must not raise; rebuilds from disk
            assert idx.contains_hash(
                hashlib.sha256(b"content").hexdigest())


class TestPrerunSnapshot:
    def test_prerun_reflects_build_time_only(self):
        """contains_hash_prerun reflects what was on disk at build(), not
        what's added afterward via add()."""
        with tempfile.TemporaryDirectory() as d:
            data = Path(d) / "data"
            cfgd = Path(d) / "cfg"
            cfgd.mkdir()
            _mk(data, "a.jpg", b"present")
            idx = phonesync.LibraryIndex(data, _cfg(cfgd))
            idx.build()

            h_present = hashlib.sha256(b"present").hexdigest()
            h_later = hashlib.sha256(b"later").hexdigest()
            assert idx.contains_hash_prerun(h_present)
            assert not idx.contains_hash_prerun(h_later)

            # add() updates the live index but NOT the prerun snapshot.
            idx.add("b.jpg", h_later, 5, 123)
            assert idx.contains_hash(h_later)            # live
            assert not idx.contains_hash_prerun(h_later)  # not prerun

    def test_size_prefilter(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d) / "data"
            cfgd = Path(d) / "cfg"
            cfgd.mkdir()
            _mk(data, "a.jpg", b"12345")  # size 5
            idx = phonesync.LibraryIndex(data, _cfg(cfgd))
            idx.build()
            assert idx.maybe_prerun_size(5) is True
            assert idx.maybe_prerun_size(999) is False


class TestLibraryIndexMutation:
    def test_add_then_query(self):
        with tempfile.TemporaryDirectory() as d:
            cfgd = Path(d) / "cfg"
            cfgd.mkdir()
            idx = phonesync.LibraryIndex(Path(d) / "data", _cfg(cfgd))
            idx.build()
            idx.add("x.jpg", "deadbeef", 10, 100)
            assert idx.contains_hash("deadbeef")
            assert idx.paths_for_hash("deadbeef") == ["x.jpg"]

    def test_remove_relpath(self):
        with tempfile.TemporaryDirectory() as d:
            cfgd = Path(d) / "cfg"
            cfgd.mkdir()
            idx = phonesync.LibraryIndex(Path(d) / "data", _cfg(cfgd))
            idx.build()
            idx.add("x.jpg", "h1", 10, 100)
            idx.add("y.jpg", "h1", 10, 100)
            idx.remove_relpath("x.jpg")
            assert idx.paths_for_hash("h1") == ["y.jpg"]
            idx.remove_relpath("y.jpg")
            assert not idx.contains_hash("h1")
