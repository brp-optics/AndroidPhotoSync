"""Contract tests for FakeADB: signatures AND behavioral correctness.

Behavioral tests verify that FakeADB produces the same observable
results a real ADB would for each operation. They use a standalone
ADBFixture (no TestHarness) to isolate the ADB layer from sync logic.
"""
import hashlib
import inspect
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import phonesync
from conftest import FakeADB


# ---------------------------------------------------------------------------
# Helper: standalone FakeADB bound to a temp directory
# ---------------------------------------------------------------------------

class ADBFixture:
    """Context manager providing a FakeADB with a fresh temp dir."""
    def __init__(self, external_sd=False):
        self._want_sd = external_sd

    def __enter__(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="fakeadb_test_"))
        FakeADB.reset_registry()
        ext = None
        if self._want_sd:
            ext = self.tmpdir / "ext_sd"
            ext.mkdir()
            self.ext_sd_dir = ext
        FakeADB.register("TEST", self.tmpdir / "internal",
                         "TestPhone", external_sd=ext)
        (self.tmpdir / "internal").mkdir()
        self.adb = FakeADB("TEST")
        self.root = self.tmpdir / "internal"
        return self

    def __exit__(self, *args):
        FakeADB.reset_registry()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def write(self, relpath: str, content: bytes, mtime: float = None):
        p = self.root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        if mtime is not None:
            os.utime(str(p), (mtime, mtime))

    def write_sd(self, relpath: str, content: bytes, mtime: float = None):
        p = self.ext_sd_dir / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        if mtime is not None:
            os.utime(str(p), (mtime, mtime))

    def exists(self, relpath: str) -> bool:
        return (self.root / relpath).exists()

    def read(self, relpath: str) -> bytes:
        return (self.root / relpath).read_bytes()

    def q(self, phone_path: str) -> str:
        """Quote a path through FakeADB._q (same as ADB._q)."""
        return self.adb._q(phone_path)


# ---------------------------------------------------------------------------
# Signature contract tests
# ---------------------------------------------------------------------------

def _public_methods(cls):
    return {
        name: inspect.signature(getattr(cls, name))
        for name in dir(cls)
        if not name.startswith("_")
        and callable(getattr(cls, name))
        and name not in ("register", "reset_registry")
    }


class TestSignatureContract:
    def test_all_public_methods_present(self):
        adb_methods = _public_methods(phonesync.ADB)
        fake_methods = _public_methods(FakeADB)
        missing = set(adb_methods) - set(fake_methods)
        assert missing == set(), f"FakeADB missing: {missing}"

    def test_method_signatures_match(self):
        adb_methods = _public_methods(phonesync.ADB)
        fake_methods = _public_methods(FakeADB)
        mismatches = []
        for name in adb_methods:
            if name not in fake_methods:
                continue
            if adb_methods[name] != fake_methods[name]:
                mismatches.append(
                    f"  {name}: ADB{adb_methods[name]} "
                    f"!= FakeADB{fake_methods[name]}")
        assert mismatches == [], "\n".join(mismatches)

    def test_no_extra_public_methods(self):
        adb_methods = _public_methods(phonesync.ADB)
        fake_methods = _public_methods(FakeADB)
        extra = set(fake_methods) - set(adb_methods)
        assert extra == set(), f"FakeADB extra: {extra}"


# ---------------------------------------------------------------------------
# _q quoting
# ---------------------------------------------------------------------------

class TestQuoting:
    def test_q_matches_shlex_quote(self):
        """FakeADB._q should produce the same output as shlex.quote."""
        import shlex
        cases = [
            "/sdcard/DCIM/Camera/IMG.jpg",
            "/sdcard/DCIM/Camera/my photo (1).jpg",
            "/sdcard/DCIM/Camera/it's a photo.jpg",
            '/sdcard/DCIM/Camera/double"quote.jpg',
            "/sdcard/DCIM/Camera/dollar$HOME.jpg",
            "/sdcard/DCIM/Camera/backtick`cmd`.jpg",
            "/sdcard/DCIM/Camera/ampersand&file.jpg",
            "/sdcard/DCIM/Camera/glob*.jpg",
            "/sdcard/DCIM/Camera/brackets[1].jpg",
            "/sdcard/DCIM/Camera/question?.jpg",
            "/sdcard/DCIM/Camera/-leading-dash.jpg",
            "/sdcard/DCIM/Camera/name with backslash\\.jpg",
            "/sdcard/DCIM/Camera/사진_2025.jpg",
            "/sdcard/DCIM/Camera/a;rm -rf.jpg",
        ]
        for path in cases:
            assert FakeADB._q(path) == shlex.quote(path), (
                f"_q mismatch for {path!r}")


# ---------------------------------------------------------------------------
# Pathological filenames
# ---------------------------------------------------------------------------

PATHOLOGICAL_FILES = [
    ("my photo (1).jpg",        b"spaces_parens"),
    ("it's a photo.jpg",        b"single_quote"),
    ('double"quote.jpg',        b"double_quote"),
    ("dollar$HOME.jpg",         b"dollar_sign"),
    ("backtick`cmd`.jpg",       b"backticks"),
    ("ampersand&file.jpg",      b"ampersand"),
    ("glob*.jpg",               b"glob_star"),
    ("brackets[1].jpg",         b"brackets"),
    ("question?.jpg",           b"question_mark"),
    ("-leading-dash.jpg",       b"leading_dash"),
    ("name with backslash\\.jpg", b"backslash"),
    ("사진_2025.jpg",            b"korean"),
    ("a;rm -f.jpg",            b"semicolon"),
    ("photo|vacation.jpg",      b"pipe_char"),
]


class TestPathologicalFilenames:
    def test_list_files_finds_all(self):
        """list_files_recursive should find every pathological filename."""
        with ADBFixture() as f:
            for name, content in PATHOLOGICAL_FILES:
                f.write(f"DCIM/Camera/{name}", content)
            files = f.adb.list_files_recursive("/sdcard/DCIM/Camera")
            found = {e["name"] for e in files}
            for name, _ in PATHOLOGICAL_FILES:
                assert name in found, f"Missing: {name!r}"
            assert len(files) == len(PATHOLOGICAL_FILES)

    def test_pull_each(self):
        """pull should work for every pathological filename."""
        with ADBFixture() as f:
            for name, content in PATHOLOGICAL_FILES:
                f.write(f"DCIM/Camera/{name}", content)
                dst = str(f.tmpdir / f"_pulled_{hash(name)}")
                ok = f.adb.pull(f"/sdcard/DCIM/Camera/{name}", dst)
                assert ok is True, f"pull failed: {name!r}"
                assert open(dst, "rb").read() == content

    def test_file_hash_each(self):
        with ADBFixture() as f:
            for name, content in PATHOLOGICAL_FILES:
                f.write(f"DCIM/Camera/{name}", content)
                expected = hashlib.sha256(content).hexdigest()
                got = f.adb.file_hash(f"/sdcard/DCIM/Camera/{name}")
                assert got == expected, f"hash mismatch: {name!r}"

    def test_stat_via_shell_with_q(self):
        """stat commands using _q quoting should work for all filenames."""
        with ADBFixture() as f:
            for name, content in PATHOLOGICAL_FILES:
                f.write(f"DCIM/Camera/{name}", content)
                phone_path = f"/sdcard/DCIM/Camera/{name}"
                q = f.q(phone_path)
                out = f.adb.shell(f"stat -c %s {q}")
                assert out.strip() == str(len(content)), (
                    f"stat size wrong for {name!r}: {out!r}")

    def test_move_safe_each(self):
        with ADBFixture() as f:
            for name, content in PATHOLOGICAL_FILES:
                f.write(f"DCIM/Camera/{name}", content)
                h = hashlib.sha256(content).hexdigest()
                result = f.adb.move_safe(
                    f"/sdcard/DCIM/Camera/{name}",
                    f"/sdcard/DCIM/Sorted/{name}",
                    h)
                assert result["ok"] is True, (
                    f"move_safe failed for {name!r}: {result}")

    def test_exists_check_via_shell_with_q(self):
        with ADBFixture() as f:
            for name, content in PATHOLOGICAL_FILES:
                f.write(f"DCIM/Camera/{name}", content)
                phone_path = f"/sdcard/DCIM/Camera/{name}"
                q = f.q(phone_path)
                out = f.adb.shell(
                    f"[ -e {q} ] && echo EXISTS || echo MISSING")
                assert "EXISTS" in out, (
                    f"exists check failed for {name!r}")

    def test_cp_mv_rm_via_shell_with_q(self):
        """cp, mv, rm through shell with quoted pathological names."""
        with ADBFixture() as f:
            for i, (name, content) in enumerate(PATHOLOGICAL_FILES):
                f.write(f"DCIM/Camera/{name}", content)
                src = f.q(f"/sdcard/DCIM/Camera/{name}")
                cp_dst = f.q(f"/sdcard/DCIM/Copy/{name}")
                mv_dst = f.q(f"/sdcard/DCIM/Moved/{name}")

                f.adb.shell(f"mkdir -p {f.q('/sdcard/DCIM/Copy')}")
                f.adb.shell(f"cp {src} {cp_dst}")
                assert f.exists(f"DCIM/Copy/{name}"), (
                    f"cp failed: {name!r}")

                f.adb.shell(f"mkdir -p {f.q('/sdcard/DCIM/Moved')}")
                f.adb.shell(f"mv {cp_dst} {mv_dst}")
                assert f.exists(f"DCIM/Moved/{name}"), (
                    f"mv failed: {name!r}")
                assert not f.exists(f"DCIM/Copy/{name}")

                f.adb.shell(f"rm {mv_dst}")
                assert not f.exists(f"DCIM/Moved/{name}"), (
                    f"rm failed: {name!r}")


# ---------------------------------------------------------------------------
# Path mapping: /sdcard/ vs /storage/emulated/0/
# ---------------------------------------------------------------------------

class TestPathMapping:
    def test_sdcard_and_storage_emulated_same_file(self):
        """Both /sdcard/... and /storage/emulated/0/... should hit same file."""
        with ADBFixture() as f:
            f.write("DCIM/Camera/IMG.jpg", b"same file")

            h1 = f.adb.file_hash("/sdcard/DCIM/Camera/IMG.jpg")
            h2 = f.adb.file_hash("/storage/emulated/0/DCIM/Camera/IMG.jpg")
            assert h1 is not None
            assert h1 == h2

    def test_pull_via_storage_emulated(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/IMG.jpg", b"content via emulated")
            dst = str(f.tmpdir / "_pulled.jpg")
            ok = f.adb.pull("/storage/emulated/0/DCIM/Camera/IMG.jpg", dst)
            assert ok is True
            assert open(dst, "rb").read() == b"content via emulated"

    def test_shell_stat_via_storage_emulated(self):
        with ADBFixture() as f:
            f.write("test.txt", b"12345")
            out = f.adb.shell(
                "stat -c %s '/storage/emulated/0/test.txt'")
            assert out.strip() == "5"

    def test_file_exists_both_paths(self):
        with ADBFixture() as f:
            f.write("file.txt", b"x")
            assert f.adb.file_exists("/sdcard/file.txt")
            assert f.adb.file_exists("/storage/emulated/0/file.txt")

    def test_list_files_via_storage_emulated(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/IMG.jpg", b"data")
            files = f.adb.list_files_recursive(
                "/storage/emulated/0/DCIM/Camera")
            assert len(files) == 1


# ---------------------------------------------------------------------------
# External SD card operations
# ---------------------------------------------------------------------------

class TestExternalSD:
    def test_write_and_read_sd(self):
        with ADBFixture(external_sd=True) as f:
            f.write_sd("DCIM/Camera/sd_photo.jpg", b"sd data")
            assert f.adb.file_exists("/storage/EXT_SD/DCIM/Camera/sd_photo.jpg")

    def test_pull_from_sd(self):
        with ADBFixture(external_sd=True) as f:
            f.write_sd("DCIM/Camera/sd_photo.jpg", b"sd content")
            dst = str(f.tmpdir / "_pulled_sd.jpg")
            ok = f.adb.pull(
                "/storage/EXT_SD/DCIM/Camera/sd_photo.jpg", dst)
            assert ok is True
            assert open(dst, "rb").read() == b"sd content"

    def test_file_hash_on_sd(self):
        with ADBFixture(external_sd=True) as f:
            content = b"sd hash me"
            f.write_sd("photo.jpg", content)
            expected = hashlib.sha256(content).hexdigest()
            assert f.adb.file_hash(
                "/storage/EXT_SD/photo.jpg") == expected

    def test_move_between_internal_and_sd(self):
        with ADBFixture(external_sd=True) as f:
            f.write("DCIM/Camera/IMG.jpg", b"internal photo")
            ok = f.adb.move(
                "/sdcard/DCIM/Camera/IMG.jpg",
                "/storage/EXT_SD/Backup/IMG.jpg")
            assert ok is True
            assert not f.exists("DCIM/Camera/IMG.jpg")
            assert (f.ext_sd_dir / "Backup" / "IMG.jpg").read_bytes() == (
                b"internal photo")

    def test_list_storage_volumes_with_sd(self):
        with ADBFixture(external_sd=True) as f:
            vols = f.adb.list_storage_volumes()
            assert len(vols) == 2
            types = {v["type"] for v in vols}
            assert "internal" in types
            assert "external_sd" in types

    def test_list_files_recursive_on_external_sd(self):
        with ADBFixture(external_sd=True) as f:
            f.write_sd("DCIM/Camera/sd_photo.jpg", b"sd data")
            files = f.adb.list_files_recursive("/storage/EXT_SD/DCIM/Camera")
            assert len(files) == 1
            assert files[0]["path"] == "/storage/EXT_SD/DCIM/Camera/sd_photo.jpg"
            assert files[0]["relpath"] == "sd_photo.jpg"

    def test_find_on_external_sd(self):
        with ADBFixture(external_sd=True) as f:
            f.write_sd("DCIM/Camera/sd_photo.jpg", b"sd data")
            out = f.adb.shell(
                "find /storage/EXT_SD/DCIM/Camera -type f 2>/dev/null"
            )
            assert "/storage/EXT_SD/DCIM/Camera/sd_photo.jpg" in out

    def test_ls_glob_on_external_sd(self):
        with ADBFixture(external_sd=True) as f:
            (f.ext_sd_dir / "DCIM" / "Camera").mkdir(parents=True)
            out = f.adb.shell(
                "ls -1d /storage/EXT_SD/DCIM/*/ 2>/dev/null | head -20"
            )
            assert "/storage/EXT_SD/DCIM/Camera/" in out
            
# ---------------------------------------------------------------------------
# Shell: ls -1d pattern (used by cmd_devices)
# ---------------------------------------------------------------------------

class TestShellLs:
    def test_ls_glob_dirs(self):
        """ls -1d <path>/*/ should list subdirectories."""
        with ADBFixture() as f:
            (f.root / "DCIM" / "Camera").mkdir(parents=True)
            (f.root / "DCIM" / "Screenshots").mkdir(parents=True)
            f.write("DCIM/Camera/img.jpg", b"x")
            out = f.adb.shell(
                "ls -1d /sdcard/DCIM/*/ 2>/dev/null | head -20")
            assert "Camera" in out
            assert "Screenshots" in out

    def test_ls_glob_no_dirs(self):
        with ADBFixture() as f:
            (f.root / "DCIM").mkdir(parents=True)
            # Only files, no subdirectories
            f.write("DCIM/file.txt", b"x")
            out = f.adb.shell(
                "ls -1d /sdcard/DCIM/*/ 2>/dev/null | head -20")
            # Should be empty or just whitespace
            assert "file.txt" not in out


# ---------------------------------------------------------------------------
# Shell: find with prune clauses
# ---------------------------------------------------------------------------

class TestFindWithPrune:
    def test_find_with_stat_excludes_pruned_dirs(self):
        """The full find+prune+stat command pattern used by phonesync."""
        with ADBFixture() as f:
            f.write("DCIM/Camera/keep.jpg", b"keep", 1700000000.0)
            f.write("DCIM/Camera/.thumbnails/skip.jpg", b"skip")
            f.write("DCIM/Camera/.trashed/old.jpg", b"old")
            f.write("DCIM/Camera/sub/also_keep.jpg", b"also", 1700000000.0)

            # This is the actual pattern phonesync generates: NUL-terminated
            # records (size\0 mtime\0 path\0)...
            cmd = (
                "find '/sdcard/DCIM/Camera' -maxdepth 10 "
                "\\( -name '.thumbnails' -prune "
                "-o -name '.trashed' -prune \\) -o "
                "-type f -exec sh -c 'for f; do "
                "s=$(stat -c %s \"$f\") && "
                "m=$(stat -c %Y \"$f\") && "
                "printf \"%s\\0%s\\0%s\\0\" \"$s\" \"$m\" \"$f\"; "
                "done' _ {} +"
            )
            out = f.adb.shell(cmd)
            tokens = out.split("\0")
            if tokens and tokens[-1] == "":
                tokens.pop()
            paths = [tokens[i + 2] for i in range(0, len(tokens) - 2, 3)]
            assert any("keep.jpg" in p for p in paths)
            assert any("also_keep.jpg" in p for p in paths)
            assert not any(".thumbnails" in p for p in paths)
            assert not any(".trashed" in p for p in paths)

    def test_list_files_recursive_with_excludes(self):
        """High-level API should respect exclude_dirs."""
        with ADBFixture() as f:
            f.write("DCIM/Camera/keep.jpg", b"keep")
            f.write("DCIM/Camera/.thumbnails/skip.jpg", b"skip")
            files = f.adb.list_files_recursive(
                "/sdcard/DCIM/Camera",
                exclude_dirs=[".thumbnails"])
            names = [e["name"] for e in files]
            assert "keep.jpg" in names
            assert "skip.jpg" not in names


# ---------------------------------------------------------------------------
# list_files_recursive: shape and depth
# ---------------------------------------------------------------------------

class TestListFilesRecursive:
    def test_returns_expected_shape(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/IMG.jpg", b"photo data", 1700000000.0)
            files = f.adb.list_files_recursive("/sdcard/DCIM/Camera")
            assert len(files) == 1
            entry = files[0]
            assert set(entry.keys()) == {
                "name", "size", "mtime_epoch", "path", "relpath"}
            assert entry["name"] == "IMG.jpg"
            assert entry["size"] == len(b"photo data")
            assert entry["mtime_epoch"] == 1700000000
            assert entry["path"] == "/sdcard/DCIM/Camera/IMG.jpg"
            assert entry["relpath"] == "IMG.jpg"

    def test_nested_relpath(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/sub/deep/IMG.jpg", b"data")
            files = f.adb.list_files_recursive("/sdcard/DCIM/Camera")
            assert files[0]["relpath"] == "sub/deep/IMG.jpg"

    def test_respects_max_depth(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/top.jpg", b"top")
            f.write("DCIM/Camera/sub/nested.jpg", b"nested")
            shallow = f.adb.list_files_recursive(
                "/sdcard/DCIM/Camera", max_depth=1)
            deep = f.adb.list_files_recursive(
                "/sdcard/DCIM/Camera", max_depth=10)
            assert len(shallow) == 1
            assert shallow[0]["name"] == "top.jpg"
            assert len(deep) == 2

    def test_nonexistent_dir_returns_empty(self):
        with ADBFixture() as f:
            assert f.adb.list_files_recursive("/sdcard/NoDir") == []

    def test_empty_dir_returns_empty(self):
        with ADBFixture() as f:
            (f.root / "DCIM" / "Camera").mkdir(parents=True)
            assert f.adb.list_files_recursive("/sdcard/DCIM/Camera") == []

    def test_list_files_is_depth_one(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/top.jpg", b"top")
            f.write("DCIM/Camera/sub/nested.jpg", b"nested")
            files = f.adb.list_files("/sdcard/DCIM/Camera")
            assert len(files) == 1
            assert files[0]["name"] == "top.jpg"

    def test_excludes_files_by_pattern(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/photo.jpg", b"keep")
            f.write("DCIM/Camera/.nomedia", b"skip")
            files = f.adb.list_files_recursive(
                "/sdcard/DCIM/Camera", exclude_files=[".nomedia"])
            names = [e["name"] for e in files]
            assert "photo.jpg" in names
            assert ".nomedia" not in names


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

class TestFileOperations:
    def test_pull_copies_content(self):
        with ADBFixture() as f:
            f.write("DCIM/IMG.jpg", b"original")
            dst = str(f.tmpdir / "_pulled.jpg")
            assert f.adb.pull("/sdcard/DCIM/IMG.jpg", dst) is True
            assert open(dst, "rb").read() == b"original"

    def test_pull_nonexistent_returns_false(self):
        with ADBFixture() as f:
            assert f.adb.pull("/sdcard/no.jpg",
                              str(f.tmpdir / "x")) is False

    def test_pull_preserves_mtime(self):
        with ADBFixture() as f:
            f.write("IMG.jpg", b"data", mtime=1700000000.0)
            dst = str(f.tmpdir / "_pulled.jpg")
            f.adb.pull("/sdcard/IMG.jpg", dst)
            assert int(os.stat(dst).st_mtime) == 1700000000

    def test_push_creates_file(self):
        with ADBFixture() as f:
            src = str(f.tmpdir / "_src.txt")
            with open(src, "wb") as fp:
                fp.write(b"pushed")
            assert f.adb.push(src, "/sdcard/Upload/pushed.txt") is True
            assert f.read("Upload/pushed.txt") == b"pushed"

    def test_push_overwrites_existing(self):
        with ADBFixture() as f:
            f.write("file.txt", b"old content")
            src = str(f.tmpdir / "_new.txt")
            with open(src, "wb") as fp:
                fp.write(b"new content")
            f.adb.push(src, "/sdcard/file.txt")
            assert f.read("file.txt") == b"new content"

    def test_push_preserves_mtime(self):
        with ADBFixture() as f:
            src = str(f.tmpdir / "_src.txt")
            with open(src, "wb") as fp:
                fp.write(b"data")
            os.utime(src, (1700000000.0, 1700000000.0))
            f.adb.push(src, "/sdcard/file.txt")
            assert int((f.root / "file.txt").stat().st_mtime) == 1700000000

    def test_delete_removes_file(self):
        with ADBFixture() as f:
            f.write("IMG.jpg", b"data")
            assert f.adb.delete("/sdcard/IMG.jpg") is True
            assert not f.exists("IMG.jpg")

    def test_delete_nonexistent_returns_false(self):
        with ADBFixture() as f:
            assert f.adb.delete("/sdcard/no.jpg") is False

    def test_move_relocates_file(self):
        with ADBFixture() as f:
            f.write("src.jpg", b"data")
            assert f.adb.move("/sdcard/src.jpg", "/sdcard/dst.jpg") is True
            assert not f.exists("src.jpg")
            assert f.read("dst.jpg") == b"data"

    def test_move_nonexistent_returns_false(self):
        with ADBFixture() as f:
            assert f.adb.move("/sdcard/no.jpg", "/sdcard/x.jpg") is False

    def test_move_overwrites_destination(self):
        """move() uses shutil.move which overwrites by default."""
        with ADBFixture() as f:
            f.write("src.jpg", b"new")
            f.write("dst.jpg", b"old")
            ok = f.adb.move("/sdcard/src.jpg", "/sdcard/dst.jpg")
            assert ok is True
            assert f.read("dst.jpg") == b"new"

    def test_move_creates_parent_dirs(self):
        with ADBFixture() as f:
            f.write("src.jpg", b"data")
            ok = f.adb.move("/sdcard/src.jpg", "/sdcard/a/b/c/dst.jpg")
            assert ok is True
            assert f.read("a/b/c/dst.jpg") == b"data"

    def test_file_exists(self):
        with ADBFixture() as f:
            assert not f.adb.file_exists("/sdcard/IMG.jpg")
            f.write("IMG.jpg", b"data")
            assert f.adb.file_exists("/sdcard/IMG.jpg")

    def test_file_mtime(self):
        with ADBFixture() as f:
            f.write("IMG.jpg", b"data", mtime=1700000000.0)
            assert f.adb.file_mtime("/sdcard/IMG.jpg") == 1700000000

    def test_file_mtime_nonexistent(self):
        with ADBFixture() as f:
            assert f.adb.file_mtime("/sdcard/no.jpg") is None

    def test_file_hash_matches_content(self):
        with ADBFixture() as f:
            content = b"hash me"
            f.write("IMG.jpg", content)
            expected = hashlib.sha256(content).hexdigest()
            assert f.adb.file_hash("/sdcard/IMG.jpg") == expected

    def test_file_hash_nonexistent(self):
        with ADBFixture() as f:
            assert f.adb.file_hash("/sdcard/no.jpg") is None

    def test_mkdir_creates_nested(self):
        with ADBFixture() as f:
            f.adb.mkdir("/sdcard/a/b/c/d")
            assert (f.root / "a" / "b" / "c" / "d").is_dir()


# ---------------------------------------------------------------------------
# move_safe
# ---------------------------------------------------------------------------

class TestMoveSafe:
    def test_normal_move(self):
        with ADBFixture() as f:
            content = b"move me"
            h = hashlib.sha256(content).hexdigest()
            f.write("src.jpg", content)
            r = f.adb.move_safe("/sdcard/src.jpg", "/sdcard/dst.jpg", h)
            assert r == {"ok": True, "action": "moved",
                         "source_deleted": True}
            assert not f.exists("src.jpg")
            assert f.read("dst.jpg") == content

    def test_collision_different_hash(self):
        with ADBFixture() as f:
            f.write("src.jpg", b"source")
            f.write("dst.jpg", b"different")
            h = hashlib.sha256(b"source").hexdigest()
            r = f.adb.move_safe("/sdcard/src.jpg", "/sdcard/dst.jpg", h)
            assert r["ok"] is False
            assert r["action"] == "collision"
            assert f.exists("src.jpg")
            assert f.read("dst.jpg") == b"different"

    def test_collision_same_hash_deletes_source(self):
        with ADBFixture() as f:
            content = b"identical"
            h = hashlib.sha256(content).hexdigest()
            f.write("src.jpg", content)
            f.write("dst.jpg", content)
            r = f.adb.move_safe("/sdcard/src.jpg", "/sdcard/dst.jpg", h)
            assert r["ok"] is True
            assert r["action"] == "already_there"
            assert r["source_deleted"] is True
            assert not f.exists("src.jpg")

    def test_source_missing(self):
        with ADBFixture() as f:
            r = f.adb.move_safe("/sdcard/no.jpg", "/sdcard/dst.jpg", "abc")
            assert r["ok"] is False
            assert r["action"] == "copy_failed"

    def test_no_hash_skips_verification(self):
        with ADBFixture() as f:
            f.write("src.jpg", b"data")
            r = f.adb.move_safe("/sdcard/src.jpg", "/sdcard/dst.jpg", None)
            assert r["ok"] is True
            assert r["action"] == "moved"

    def test_creates_parent_dirs(self):
        with ADBFixture() as f:
            content = b"nested"
            h = hashlib.sha256(content).hexdigest()
            f.write("src.jpg", content)
            r = f.adb.move_safe(
                "/sdcard/src.jpg", "/sdcard/a/b/c/dst.jpg", h)
            assert r["ok"] is True

    def test_hash_mismatch_cleans_up(self):
        """If hash verification fails, destination is removed and source kept."""
        with ADBFixture() as f:
            f.write("src.jpg", b"real content")
            # Pass a wrong hash to simulate corruption
            wrong_hash = hashlib.sha256(b"wrong").hexdigest()
            r = f.adb.move_safe(
                "/sdcard/src.jpg", "/sdcard/dst.jpg", wrong_hash)
            assert r["ok"] is False
            assert r["action"] == "hash_mismatch"
            # Source should still exist (wasn't deleted)
            assert f.exists("src.jpg")
            # Destination should be cleaned up
            assert not f.exists("dst.jpg")


# ---------------------------------------------------------------------------
# Shell commands
# ---------------------------------------------------------------------------

class TestShellCommands:
    def test_rejects_unknown(self):
        with ADBFixture() as f:
            raised = False
            try:
                f.adb.shell("some_unknown_command --flag")
            except NotImplementedError:
                raised = True
            assert raised

    def test_rejects_unknown_pipe_rhs(self):
        with ADBFixture() as f:
            (f.root / "DCIM").mkdir(parents=True)
            raised = False
            try:
                f.adb.shell("find /sdcard/DCIM -type f | sort -r")
            except NotImplementedError:
                raised = True
            assert raised

    def test_dir_exists(self):
        with ADBFixture() as f:
            (f.root / "DCIM" / "Camera").mkdir(parents=True)
            q = f.q("/sdcard/DCIM/Camera")
            out = f.adb.shell(
                f"[ -d {q} ] && echo EXISTS || echo MISSING")
            assert "EXISTS" in out

    def test_dir_missing(self):
        with ADBFixture() as f:
            q = f.q("/sdcard/NoDir")
            out = f.adb.shell(
                f"[ -d {q} ] && echo EXISTS || echo MISSING")
            assert "MISSING" in out

    def test_file_exists_check(self):
        with ADBFixture() as f:
            f.write("test.txt", b"hi")
            q = f.q("/sdcard/test.txt")
            out = f.adb.shell(
                f"[ -e {q} ] && echo EXISTS || echo MISSING")
            assert "EXISTS" in out

    def test_stat_size(self):
        with ADBFixture() as f:
            f.write("test.txt", b"hello")
            out = f.adb.shell("stat -c %s '/sdcard/test.txt'")
            assert out.strip() == "5"

    def test_stat_mtime(self):
        with ADBFixture() as f:
            f.write("test.txt", b"hello", mtime=1700000000.0)
            out = f.adb.shell("stat -c %Y '/sdcard/test.txt'")
            assert out.strip() == "1700000000"

    def test_sha256sum(self):
        with ADBFixture() as f:
            content = b"hash this"
            f.write("test.txt", content)
            expected = hashlib.sha256(content).hexdigest()
            out = f.adb.shell("sha256sum '/sdcard/test.txt'")
            assert expected in out

    def test_mkdir_p(self):
        with ADBFixture() as f:
            f.adb.shell("mkdir -p '/sdcard/a/b/c'")
            assert (f.root / "a" / "b" / "c").is_dir()

    def test_rm(self):
        with ADBFixture() as f:
            f.write("test.txt", b"bye")
            f.adb.shell("rm '/sdcard/test.txt'")
            assert not f.exists("test.txt")

    def test_rm_nonexistent_raises(self):
        with ADBFixture() as f:
            raised = False
            try:
                f.adb.shell("rm '/sdcard/no.txt'", check=True)
            except phonesync.ADBError:
                raised = True
            assert raised

    def test_rm_f_nonexistent_ok(self):
        with ADBFixture() as f:
            f.adb.shell("rm -f '/sdcard/no.txt'")  # should not raise

    def test_cp(self):
        with ADBFixture() as f:
            f.write("src.txt", b"copy me")
            f.adb.shell("cp '/sdcard/src.txt' '/sdcard/dst.txt'")
            assert f.read("dst.txt") == b"copy me"
            assert f.exists("src.txt")

    def test_mv(self):
        with ADBFixture() as f:
            f.write("src.txt", b"move me")
            f.adb.shell("mv '/sdcard/src.txt' '/sdcard/dst.txt'")
            assert f.read("dst.txt") == b"move me"
            assert not f.exists("src.txt")

    def test_find_type_f(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/a.jpg", b"a")
            f.write("DCIM/Camera/sub/b.jpg", b"b")
            (f.root / "DCIM" / "Camera" / "emptydir").mkdir()
            out = f.adb.shell(
                "find '/sdcard/DCIM/Camera' -maxdepth 10 -type f")
            assert "a.jpg" in out
            assert "b.jpg" in out
            assert "emptydir" not in out

    def test_find_type_d(self):
        with ADBFixture() as f:
            (f.root / "DCIM" / "Camera" / "sub").mkdir(parents=True)
            out = f.adb.shell(
                "find '/sdcard/DCIM/Camera' -maxdepth 1 -type d")
            assert "Camera" in out
            assert "sub" in out

    def test_find_piped_wc_l(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/a.jpg", b"a")
            f.write("DCIM/Camera/b.jpg", b"b")
            f.write("DCIM/Camera/c.jpg", b"c")
            out = f.adb.shell(
                "find '/sdcard/DCIM/Camera' -type f 2>/dev/null | wc -l")
            assert out.strip() == "3"

    def test_find_piped_wc_l_empty(self):
        with ADBFixture() as f:
            (f.root / "DCIM" / "Camera").mkdir(parents=True)
            out = f.adb.shell(
                "find '/sdcard/DCIM/Camera' -type f 2>/dev/null | wc -l")
            assert out.strip() == "0"

    def test_find_with_stat_printf(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/IMG.jpg", b"photo", mtime=1700000000.0)
            cmd = (
                "find '/sdcard/DCIM/Camera' -maxdepth 10 "
                "-type f -exec sh -c "
                "'for f; do "
                "s=$(stat -c %s \"$f\") && "
                "m=$(stat -c %Y \"$f\") && "
                "printf \"%s\\0%s\\0%s\\0\" \"$s\" \"$m\" \"$f\"; "
                "done' _ {} +"
            )
            out = f.adb.shell(cmd)
            tokens = out.split("\0")
            if tokens and tokens[-1] == "":
                tokens.pop()
            assert len(tokens) == 3  # exactly one record
            assert tokens == ["5", "1700000000",
                              "/sdcard/DCIM/Camera/IMG.jpg"]

    def test_getprop_model(self):
        with ADBFixture() as f:
            assert f.adb.shell("getprop ro.product.model") == "TestPhone"


# ---------------------------------------------------------------------------
# check=False behavior
# ---------------------------------------------------------------------------

class TestCheckFalse:
    def test_stat_missing_check_false(self):
        """stat on missing file with check=False should return empty."""
        with ADBFixture() as f:
            out = f.adb.shell(
                "stat -c %s '/sdcard/no.txt'", check=False)
            assert out.strip() == ""

    def test_stat_missing_check_true(self):
        """stat on missing file with check=True should also return empty.

        FakeADB.stat doesn't raise on missing files (matches real stat
        behavior where the exit code is nonzero but we use check=False
        in production). The shell dispatcher doesn't enforce check for
        stat since phonesync always calls stat with check=False.
        """
        with ADBFixture() as f:
            out = f.adb.shell(
                "stat -c %s '/sdcard/no.txt'", check=True)
            assert out.strip() == ""

    def test_sha256sum_missing_check_false(self):
        with ADBFixture() as f:
            out = f.adb.shell(
                "sha256sum '/sdcard/no.txt'", check=False)
            assert out.strip() == ""

    def test_cp_source_missing_raises(self):
        with ADBFixture() as f:
            raised = False
            try:
                f.adb.shell(
                    "cp '/sdcard/no.txt' '/sdcard/dst.txt'")
            except phonesync.ADBError:
                raised = True
            assert raised

    def test_mv_source_missing_raises(self):
        with ADBFixture() as f:
            raised = False
            try:
                f.adb.shell(
                    "mv '/sdcard/no.txt' '/sdcard/dst.txt'")
            except phonesync.ADBError:
                raised = True
            assert raised

    def test_mkdir_existing_is_ok(self):
        """mkdir -p on existing dir should not raise."""
        with ADBFixture() as f:
            (f.root / "existing").mkdir()
            f.adb.shell("mkdir -p '/sdcard/existing'")
            assert (f.root / "existing").is_dir()

    def test_rm_check_false_missing(self):
        """rm with check=False on missing file should not raise."""
        with ADBFixture() as f:
            # This should not raise
            f.adb.shell("rm '/sdcard/no.txt'", check=False)


# ---------------------------------------------------------------------------
# Storage volumes
# ---------------------------------------------------------------------------

class TestStorageVolumes:
    def test_internal_only(self):
        with ADBFixture() as f:
            vols = f.adb.list_storage_volumes()
            assert len(vols) == 1
            assert vols[0]["type"] == "internal"

    def test_with_external_sd(self):
        with ADBFixture(external_sd=True) as f:
            vols = f.adb.list_storage_volumes()
            assert len(vols) == 2
            types = {v["type"] for v in vols}
            assert "external_sd" in types
