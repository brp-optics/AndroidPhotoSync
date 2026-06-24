"""Contract tests verifying FakeADB matches the real ADB interface."""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import phonesync
from conftest import FakeADB


def _public_methods(cls):
    """Get public methods (not starting with _) and their signatures."""
    return {
        name: inspect.signature(getattr(cls, name))
        for name in dir(cls)
        if not name.startswith("_")
        and callable(getattr(cls, name))
        and name not in ("register", "reset_registry")  # FakeADB-only
    }


class TestFakeADBContract:
    def test_all_public_methods_present(self):
        """FakeADB should implement every public method of ADB."""
        adb_methods = _public_methods(phonesync.ADB)
        fake_methods = _public_methods(FakeADB)

        missing = set(adb_methods) - set(fake_methods)
        assert missing == set(), (
            f"FakeADB is missing methods: {missing}")

    def test_method_signatures_match(self):
        """FakeADB method signatures should match ADB."""
        adb_methods = _public_methods(phonesync.ADB)
        fake_methods = _public_methods(FakeADB)

        mismatches = []
        for name in adb_methods:
            if name not in fake_methods:
                continue  # caught by test above
            adb_sig = adb_methods[name]
            fake_sig = fake_methods[name]
            if adb_sig != fake_sig:
                mismatches.append(
                    f"  {name}: ADB{adb_sig} != FakeADB{fake_sig}")

        assert mismatches == [], (
            f"Signature mismatches:\n" + "\n".join(mismatches))

    def test_no_extra_public_methods(self):
        """FakeADB should not add public methods beyond ADB's interface.

        Extra methods could mask test bugs where tests call
        FakeADB-specific methods that don't exist on real ADB.
        (Excludes register/reset_registry which are test infrastructure.)
        """
        adb_methods = _public_methods(phonesync.ADB)
        fake_methods = _public_methods(FakeADB)

        extra = set(fake_methods) - set(adb_methods)
        assert extra == set(), (
            f"FakeADB has extra public methods not in ADB: {extra}")

    def test_shell_rejects_unknown_commands(self):
        """FakeADB.shell() should raise NotImplementedError for unknown."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            FakeADB.reset_registry()
            FakeADB.register("TEST", Path(d), "TestPhone")
            adb = FakeADB("TEST")

            raised = False
            try:
                adb.shell("some_unknown_command --flag")
            except NotImplementedError:
                raised = True
            finally:
                FakeADB.reset_registry()

            assert raised, (
                "FakeADB.shell() should raise NotImplementedError "
                "for unknown commands")
