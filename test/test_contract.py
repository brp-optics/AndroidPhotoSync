"""Contract tests for FakeADB: signatures AND behavioral correctness.

Behavioral tests verify that FakeADB produces the same observable
results a real ADB would for each operation. They use a standalone
FakeADB (no TestHarness) to isolate the ADB layer from sync logic.
"""
import hashlib
import inspect
import os
import shlex
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
    """Context manager that provides a FakeADB with a fresh temp dir."""
    def __enter__(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="fakeadb_test_"))
        FakeADB.reset_registry()
        FakeADB.register("TEST", self.tmpdir, "TestPhone")
        self.adb = FakeADB("TEST")
        return self

    def __exit__(self, *args):
        FakeADB.reset_registry()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def write(self, relpath: str, content: bytes, mtime: float = None):
        """Write a file under the fake phone root."""
        p = self.tmpdir / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        if mtime is not None:
            os.utime(str(p), (mtime, mtime))

    def exists(self, relpath: str) -> bool:
        return (self.tmpdir / relpath).exists()

    def read(self, relpath: str) -> bytes:
        return (self.tmpdir / relpath).read_bytes()

    def phone_path(self, relpath: str) -> str:
        """Convert a local relpath to /sdcard/... phone path."""
        return f"/sdcard/{relpath}"


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
        assert missing == set(), f"FakeADB is missing methods: {missing}"

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
        assert mismatches == [], (
            f"Signature mismatches:\n" + "\n".join(mismatches))

    def test_no_extra_public_methods(self):
        adb_methods = _public_methods(phonesync.ADB)
        fake_methods = _public_methods(FakeADB)
        extra = set(fake_methods) - set(adb_methods)
        assert extra == set(), (
            f"FakeADB has extra public methods: {extra}")


# ---------------------------------------------------------------------------
# Behavioral: list_files_recursive
# ---------------------------------------------------------------------------

class TestListFilesRecursive:
    def test_returns_expected_shape(self):
        """Each returned dict must have name, size, mtime_epoch, path, relpath."""
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
        """relpath should be relative to the scanned directory."""
        with ADBFixture() as f:
            f.write("DCIM/Camera/sub/deep/IMG.jpg", b"data")
            files = f.adb.list_files_recursive("/sdcard/DCIM/Camera")
            assert len(files) == 1
            assert files[0]["relpath"] == "sub/deep/IMG.jpg"
            assert files[0]["path"] == "/sdcard/DCIM/Camera/sub/deep/IMG.jpg"

    def test_respects_max_depth(self):
        """max_depth=1 should only return files in the immediate directory."""
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

    def test_excludes_dirs(self):
        """Files in excluded directories should be omitted."""
        with ADBFixture() as f:
            f.write("DCIM/Camera/keep.jpg", b"keep")
            f.write("DCIM/Camera/.thumbnails/thumb.jpg", b"skip")
            f.write("DCIM/Camera/.trashed/old.jpg", b"skip")
            files = f.adb.list_files_recursive(
                "/sdcard/DCIM/Camera",
                exclude_dirs=[".thumbnails", ".trashed"])
            names = [e["name"] for e in files]
            assert "keep.jpg" in names
            assert "thumb.jpg" not in names
            assert "old.jpg" not in names

    def test_excludes_files(self):
        """Files matching exclude patterns should be omitted."""
        with ADBFixture() as f:
            f.write("DCIM/Camera/photo.jpg", b"keep")
            f.write("DCIM/Camera/.nomedia", b"skip")
            f.write("DCIM/Camera/Thumbs.db", b"skip")
            files = f.adb.list_files_recursive(
                "/sdcard/DCIM/Camera",
                exclude_files=[".nomedia", "Thumbs.db"])
            names = [e["name"] for e in files]
            assert "photo.jpg" in names
            assert ".nomedia" not in names
            assert "Thumbs.db" not in names

    def test_nonexistent_dir_returns_empty(self):
        with ADBFixture() as f:
            files = f.adb.list_files_recursive("/sdcard/NoSuchDir")
            assert files == []

    def test_empty_dir_returns_empty(self):
        with ADBFixture() as f:
            (f.tmpdir / "DCIM" / "Camera").mkdir(parents=True)
            files = f.adb.list_files_recursive("/sdcard/DCIM/Camera")
            assert files == []

    def test_list_files_is_depth_one(self):
        """list_files() should be equivalent to max_depth=1."""
        with ADBFixture() as f:
            f.write("DCIM/Camera/top.jpg", b"top")
            f.write("DCIM/Camera/sub/nested.jpg", b"nested")
            files = f.adb.list_files("/sdcard/DCIM/Camera")
            assert len(files) == 1
            assert files[0]["name"] == "top.jpg"


# ---------------------------------------------------------------------------
# Behavioral: pull / push / delete / move
# ---------------------------------------------------------------------------

class TestFileOperations:
    def test_pull_copies_content(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/IMG.jpg", b"original content")
            dst = os.path.join(str(f.tmpdir), "_pulled.jpg")
            ok = f.adb.pull("/sdcard/DCIM/Camera/IMG.jpg", dst)
            assert ok is True
            assert open(dst, "rb").read() == b"original content"

    def test_pull_nonexistent_returns_false(self):
        with ADBFixture() as f:
            dst = os.path.join(str(f.tmpdir), "_pulled.jpg")
            ok = f.adb.pull("/sdcard/no/such/file.jpg", dst)
            assert ok is False

    def test_push_creates_file(self):
        with ADBFixture() as f:
            src = os.path.join(str(f.tmpdir), "_to_push.txt")
            with open(src, "wb") as fp:
                fp.write(b"pushed data")
            ok = f.adb.push(src, "/sdcard/Upload/pushed.txt")
            assert ok is True
            assert f.read("Upload/pushed.txt") == b"pushed data"

    def test_delete_removes_file(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/IMG.jpg", b"data")
            assert f.exists("DCIM/Camera/IMG.jpg")
            ok = f.adb.delete("/sdcard/DCIM/Camera/IMG.jpg")
            assert ok is True
            assert not f.exists("DCIM/Camera/IMG.jpg")

    def test_delete_nonexistent_returns_false(self):
        with ADBFixture() as f:
            ok = f.adb.delete("/sdcard/no/file.jpg")
            assert ok is False

    def test_move_relocates_file(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/IMG.jpg", b"data")
            ok = f.adb.move("/sdcard/DCIM/Camera/IMG.jpg",
                            "/sdcard/DCIM/Camera/sorted/IMG.jpg")
            assert ok is True
            assert not f.exists("DCIM/Camera/IMG.jpg")
            assert f.read("DCIM/Camera/sorted/IMG.jpg") == b"data"

    def test_move_nonexistent_returns_false(self):
        with ADBFixture() as f:
            ok = f.adb.move("/sdcard/no/file.jpg", "/sdcard/dest.jpg")
            assert ok is False

    def test_file_exists(self):
        with ADBFixture() as f:
            assert not f.adb.file_exists("/sdcard/DCIM/IMG.jpg")
            f.write("DCIM/IMG.jpg", b"data")
            assert f.adb.file_exists("/sdcard/DCIM/IMG.jpg")

    def test_file_mtime(self):
        with ADBFixture() as f:
            f.write("DCIM/IMG.jpg", b"data", mtime=1700000000.0)
            assert f.adb.file_mtime("/sdcard/DCIM/IMG.jpg") == 1700000000

    def test_file_mtime_nonexistent(self):
        with ADBFixture() as f:
            assert f.adb.file_mtime("/sdcard/no.jpg") is None

    def test_file_hash_matches_content(self):
        with ADBFixture() as f:
            content = b"hash me"
            f.write("DCIM/IMG.jpg", content)
            expected = hashlib.sha256(content).hexdigest()
            assert f.adb.file_hash("/sdcard/DCIM/IMG.jpg") == expected

    def test_file_hash_nonexistent(self):
        with ADBFixture() as f:
            assert f.adb.file_hash("/sdcard/no.jpg") is None

    def test_mkdir_creates_nested(self):
        with ADBFixture() as f:
            f.adb.mkdir("/sdcard/a/b/c/d")
            assert (f.tmpdir / "a" / "b" / "c" / "d").is_dir()

    def test_pull_preserves_mtime(self):
        """Pull should preserve modification time (via copy2)."""
        with ADBFixture() as f:
            f.write("DCIM/IMG.jpg", b"data", mtime=1700000000.0)
            dst = os.path.join(str(f.tmpdir), "_pulled.jpg")
            f.adb.pull("/sdcard/DCIM/IMG.jpg", dst)
            assert int(os.stat(dst).st_mtime) == 1700000000


# ---------------------------------------------------------------------------
# Behavioral: move_safe
# ---------------------------------------------------------------------------

class TestMoveSafe:
    def test_normal_move(self):
        """move_safe with no collision should copy-verify-delete."""
        with ADBFixture() as f:
            content = b"move me safely"
            expected_hash = hashlib.sha256(content).hexdigest()
            f.write("src.jpg", content)

            result = f.adb.move_safe(
                "/sdcard/src.jpg", "/sdcard/dst.jpg", expected_hash)

            assert result["ok"] is True
            assert result["action"] == "moved"
            assert result["source_deleted"] is True
            assert not f.exists("src.jpg")
            assert f.read("dst.jpg") == content

    def test_collision_different_content(self):
        """move_safe should refuse when dest exists with different hash."""
        with ADBFixture() as f:
            f.write("src.jpg", b"source content")
            f.write("dst.jpg", b"different content")
            src_hash = hashlib.sha256(b"source content").hexdigest()

            result = f.adb.move_safe(
                "/sdcard/src.jpg", "/sdcard/dst.jpg", src_hash)

            assert result["ok"] is False
            assert result["action"] == "collision"
            # Source untouched
            assert f.exists("src.jpg")
            assert f.read("src.jpg") == b"source content"
            # Dest untouched
            assert f.read("dst.jpg") == b"different content"

    def test_collision_same_hash_deletes_source(self):
        """move_safe should succeed and delete source when dest has same hash."""
        with ADBFixture() as f:
            content = b"identical content"
            expected_hash = hashlib.sha256(content).hexdigest()
            f.write("src.jpg", content)
            f.write("dst.jpg", content)

            result = f.adb.move_safe(
                "/sdcard/src.jpg", "/sdcard/dst.jpg", expected_hash)

            assert result["ok"] is True
            assert result["action"] == "already_there"
            assert result["source_deleted"] is True
            assert not f.exists("src.jpg")
            assert f.read("dst.jpg") == content

    def test_source_missing(self):
        with ADBFixture() as f:
            result = f.adb.move_safe(
                "/sdcard/no.jpg", "/sdcard/dst.jpg", "abc")
            assert result["ok"] is False
            assert result["action"] == "copy_failed"

    def test_no_hash_skips_verification(self):
        """Without expected_hash, move_safe should still copy-delete."""
        with ADBFixture() as f:
            f.write("src.jpg", b"data")
            result = f.adb.move_safe(
                "/sdcard/src.jpg", "/sdcard/dst.jpg", None)
            assert result["ok"] is True
            assert result["action"] == "moved"
            assert not f.exists("src.jpg")
            assert f.read("dst.jpg") == b"data"

    def test_creates_parent_dirs(self):
        with ADBFixture() as f:
            content = b"nested move"
            h = hashlib.sha256(content).hexdigest()
            f.write("src.jpg", content)
            result = f.adb.move_safe(
                "/sdcard/src.jpg", "/sdcard/a/b/c/dst.jpg", h)
            assert result["ok"] is True
            assert f.read("a/b/c/dst.jpg") == content


# ---------------------------------------------------------------------------
# Behavioral: shell command parsing
# ---------------------------------------------------------------------------

class TestShellCommands:
    def test_rejects_unknown_commands(self):
        with ADBFixture() as f:
            raised = False
            try:
                f.adb.shell("some_unknown_command --flag")
            except NotImplementedError:
                raised = True
            assert raised

    def test_rejects_unknown_piped_rhs(self):
        with ADBFixture() as f:
            (f.tmpdir / "DCIM").mkdir(parents=True)
            raised = False
            try:
                f.adb.shell("find /sdcard/DCIM -type f | sort -r")
            except NotImplementedError:
                raised = True
            assert raised

    def test_dir_exists(self):
        with ADBFixture() as f:
            (f.tmpdir / "DCIM" / "Camera").mkdir(parents=True)
            out = f.adb.shell(
                "[ -d '/sdcard/DCIM/Camera' ] && echo EXISTS || echo MISSING")
            assert "EXISTS" in out

    def test_dir_missing(self):
        with ADBFixture() as f:
            out = f.adb.shell(
                "[ -d '/sdcard/NoDir' ] && echo EXISTS || echo MISSING")
            assert "MISSING" in out

    def test_file_exists_check(self):
        with ADBFixture() as f:
            f.write("test.txt", b"hi")
            out = f.adb.shell(
                "[ -e '/sdcard/test.txt' ] && echo EXISTS || echo MISSING")
            assert "EXISTS" in out

    def test_stat_size(self):
        with ADBFixture() as f:
            f.write("test.txt", b"hello")  # 5 bytes
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
            assert out.strip().startswith(expected)

    def test_mkdir_p(self):
        with ADBFixture() as f:
            f.adb.shell("mkdir -p '/sdcard/a/b/c'")
            assert (f.tmpdir / "a" / "b" / "c").is_dir()

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
            # Should not raise
            f.adb.shell("rm -f '/sdcard/no.txt'")

    def test_cp(self):
        with ADBFixture() as f:
            f.write("src.txt", b"copy me")
            f.adb.shell("cp '/sdcard/src.txt' '/sdcard/dst.txt'")
            assert f.read("dst.txt") == b"copy me"
            assert f.exists("src.txt")  # source still exists

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
            (f.tmpdir / "DCIM" / "Camera" / "emptydir").mkdir()
            out = f.adb.shell(
                "find '/sdcard/DCIM/Camera' -maxdepth 10 -type f")
            assert "/sdcard/DCIM/Camera/a.jpg" in out
            assert "/sdcard/DCIM/Camera/sub/b.jpg" in out
            assert "emptydir" not in out

    def test_find_type_d(self):
        with ADBFixture() as f:
            (f.tmpdir / "DCIM" / "Camera" / "sub").mkdir(parents=True)
            f.write("DCIM/Camera/file.jpg", b"x")
            out = f.adb.shell(
                "find '/sdcard/DCIM/Camera' -maxdepth 1 -type d")
            assert "/sdcard/DCIM/Camera" in out
            assert "sub" in out

    def test_find_piped_wc_l(self):
        """find ... | wc -l should return a count, not raw output."""
        with ADBFixture() as f:
            f.write("DCIM/Camera/a.jpg", b"a")
            f.write("DCIM/Camera/b.jpg", b"b")
            f.write("DCIM/Camera/c.jpg", b"c")
            out = f.adb.shell(
                "find '/sdcard/DCIM/Camera' -type f 2>/dev/null | wc -l")
            assert out.strip() == "3"

    def test_find_piped_wc_l_empty(self):
        with ADBFixture() as f:
            (f.tmpdir / "DCIM" / "Camera").mkdir(parents=True)
            out = f.adb.shell(
                "find '/sdcard/DCIM/Camera' -type f 2>/dev/null | wc -l")
            assert out.strip() == "0"

    def test_getprop_model(self):
        with ADBFixture() as f:
            out = f.adb.shell("getprop ro.product.model")
            assert out == "TestPhone"

    def test_find_with_stat_printf(self):
        """The null-separated stat+printf pattern used by list_files_recursive."""
        with ADBFixture() as f:
            f.write("DCIM/Camera/IMG.jpg", b"photo", mtime=1700000000.0)
            # This is the actual command pattern phonesync generates
            cmd = (
                "find '/sdcard/DCIM/Camera' -maxdepth 10 "
                "-type f -exec sh -c "
                "'for f; do "
                "s=$(stat -c %s \"$f\") && "
                "m=$(stat -c %Y \"$f\") && "
                "printf \"%s\\0%s\\0%s\\n\" \"$s\" \"$m\" \"$f\"; "
                "done' _ {} +"
            )
            out = f.adb.shell(cmd)
            lines = [l for l in out.strip().split("\n") if l.strip()]
            assert len(lines) == 1
            parts = lines[0].split("\0", 2)
            assert len(parts) == 3
            assert parts[0] == "5"  # len(b"photo")
            assert parts[1] == "1700000000"
            assert parts[2] == "/sdcard/DCIM/Camera/IMG.jpg"


# ---------------------------------------------------------------------------
# Behavioral: special characters in paths
# ---------------------------------------------------------------------------

class TestSpecialCharPaths:
    def test_spaces_in_filename(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/my photo (1).jpg", b"spaced")
            # Use shlex.quote like real phonesync does
            path = shlex.quote("/sdcard/DCIM/Camera/my photo (1).jpg")

            # stat
            out = f.adb.shell(f"stat -c %s {path}")
            assert out.strip() == "6"

            # sha256sum
            out = f.adb.shell(f"sha256sum {path}")
            expected = hashlib.sha256(b"spaced").hexdigest()
            assert expected in out

            # exists check
            out = f.adb.shell(
                f"[ -e {path} ] && echo EXISTS || echo MISSING")
            assert "EXISTS" in out

            # list_files_recursive
            files = f.adb.list_files_recursive("/sdcard/DCIM/Camera")
            assert len(files) == 1
            assert files[0]["name"] == "my photo (1).jpg"

            # pull
            dst = os.path.join(str(f.tmpdir), "_pulled.jpg")
            ok = f.adb.pull(
                "/sdcard/DCIM/Camera/my photo (1).jpg", dst)
            assert ok is True
            assert open(dst, "rb").read() == b"spaced"

    def test_quotes_in_filename(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/it's a photo.jpg", b"quoted")
            path = shlex.quote("/sdcard/DCIM/Camera/it's a photo.jpg")
            out = f.adb.shell(f"stat -c %s {path}")
            assert out.strip() == "6"

            files = f.adb.list_files_recursive("/sdcard/DCIM/Camera")
            assert files[0]["name"] == "it's a photo.jpg"

    def test_parentheses_in_filename(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/IMG_001 (copy).jpg", b"parens")
            path = shlex.quote("/sdcard/DCIM/Camera/IMG_001 (copy).jpg")

            # cp via shell
            dst_path = shlex.quote("/sdcard/DCIM/Camera/IMG_001_backup.jpg")
            f.adb.shell(f"cp {path} {dst_path}")
            assert f.read("DCIM/Camera/IMG_001_backup.jpg") == b"parens"

            # mv via shell
            dst2 = shlex.quote("/sdcard/DCIM/Camera/moved.jpg")
            f.adb.shell(f"mv {dst_path} {dst2}")
            assert f.read("DCIM/Camera/moved.jpg") == b"parens"

    def test_unicode_in_filename(self):
        with ADBFixture() as f:
            f.write("DCIM/Camera/사진_2025.jpg", b"korean")
            path = shlex.quote("/sdcard/DCIM/Camera/사진_2025.jpg")

            out = f.adb.shell(f"stat -c %s {path}")
            assert out.strip() == "6"

            files = f.adb.list_files_recursive("/sdcard/DCIM/Camera")
            assert files[0]["name"] == "사진_2025.jpg"

            # file_hash
            expected = hashlib.sha256(b"korean").hexdigest()
            assert f.adb.file_hash(
                "/sdcard/DCIM/Camera/사진_2025.jpg") == expected

    def test_semicolons_in_filename(self):
        """Semicolons could cause shell injection if not quoted."""
        with ADBFixture() as f:
            f.write("DCIM/Camera/a;rm -rf.jpg", b"sneaky")
            path = shlex.quote("/sdcard/DCIM/Camera/a;rm -rf.jpg")

            out = f.adb.shell(f"stat -c %s {path}")
            assert out.strip() == "6"

            files = f.adb.list_files_recursive("/sdcard/DCIM/Camera")
            assert files[0]["name"] == "a;rm -rf.jpg"

    def test_pipe_in_filename(self):
        """The old | separator bug — verify it doesn't break anything."""
        with ADBFixture() as f:
            f.write("DCIM/Camera/photo|vacation.jpg", b"piped")

            files = f.adb.list_files_recursive("/sdcard/DCIM/Camera")
            assert len(files) == 1
            assert files[0]["name"] == "photo|vacation.jpg"
            assert files[0]["size"] == 5

            # pull
            dst = os.path.join(str(f.tmpdir), "_pulled.jpg")
            ok = f.adb.pull(
                "/sdcard/DCIM/Camera/photo|vacation.jpg", dst)
            assert ok is True

    def test_move_safe_with_special_chars(self):
        """move_safe should handle paths with spaces and quotes."""
        with ADBFixture() as f:
            content = b"special chars"
            h = hashlib.sha256(content).hexdigest()
            f.write("DCIM/Camera/my photo (1).jpg", content)

            result = f.adb.move_safe(
                "/sdcard/DCIM/Camera/my photo (1).jpg",
                "/sdcard/DCIM/Camera/sorted/my photo (1).jpg",
                h)
            assert result["ok"] is True
            assert f.read(
                "DCIM/Camera/sorted/my photo (1).jpg") == content
            assert not f.exists("DCIM/Camera/my photo (1).jpg")


# ---------------------------------------------------------------------------
# Behavioral: storage volumes
# ---------------------------------------------------------------------------

class TestStorageVolumes:
    def test_internal_only(self):
        with ADBFixture() as f:
            vols = f.adb.list_storage_volumes()
            assert len(vols) == 1
            assert vols[0]["type"] == "internal"
            assert vols[0]["path"] == "/sdcard"

    def test_with_external_sd(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "phone"
            root.mkdir()
            ext = Path(d) / "ext_sd"
            ext.mkdir()
            FakeADB.reset_registry()
            FakeADB.register("SDTEST", root, "SDPhone", external_sd=ext)
            try:
                adb = FakeADB("SDTEST")
                vols = adb.list_storage_volumes()
                assert len(vols) == 2
                types = {v["type"] for v in vols}
                assert "internal" in types
                assert "external_sd" in types
            finally:
                FakeADB.reset_registry()
