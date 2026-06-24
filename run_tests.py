#!/usr/bin/env python3
"""Minimal test runner for phonesync tests.

Runs pytest-style test classes without requiring pytest installed.
Supports fixtures via manual injection.

Usage: python3 run_tests.py [-v] [test_file_pattern]
"""
import importlib
import importlib.util
import inspect
import os
import sys
import traceback
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "test"))

# Import fixtures
from test.conftest import TestHarness


def make_img_data():
    """Create the img_data fixture."""
    counter = [0]
    from datetime import datetime

    def _make(name="photo", year=2025, month=1, day=15):
        counter[0] += 1
        content = (
            f"FAKE_IMAGE_{name}_{counter[0]}_{year}{month:02d}{day:02d}"
        ).encode()
        dt = datetime(year, month, day, 12, 0, 0)
        mtime = dt.timestamp()
        return content, mtime

    return _make


def run_test_method(cls, method_name, verbose=False):
    """Run a single test method with fixtures."""
    harness = TestHarness()
    harness.__enter__()
    try:
        instance = cls()
        method = getattr(instance, method_name)

        # Inspect method signature to inject fixtures
        sig = inspect.signature(method)
        kwargs = {}
        for param_name in sig.parameters:
            if param_name == "self":
                continue
            if param_name == "harness":
                kwargs["harness"] = harness
            elif param_name == "img_data":
                kwargs["img_data"] = make_img_data()

        method(**kwargs)
        return True, None
    except Exception as e:
        return False, (e, traceback.format_exc())
    finally:
        harness.__exit__(None, None, None)


def discover_tests(test_dir, pattern=None):
    """Discover test classes and methods."""
    tests = []
    for fname in sorted(os.listdir(test_dir)):
        if not fname.startswith("test_") or not fname.endswith(".py"):
            continue
        if pattern and pattern not in fname:
            continue
        module_name = fname[:-3]
        spec = importlib.util.spec_from_file_location(
            module_name, os.path.join(test_dir, fname))
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            print(f"  ERROR importing {fname}: {e}")
            continue

        for name, obj in inspect.getmembers(module, inspect.isclass):
            if name.startswith("Test"):
                for method_name in sorted(dir(obj)):
                    if method_name.startswith("test_"):
                        tests.append((fname, name, method_name, obj))
    return tests


def main():
    verbose = "-v" in sys.argv
    pattern = None
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            pattern = arg

    test_dir = os.path.join(os.path.dirname(__file__), "test")
    tests = discover_tests(test_dir, pattern)

    if not tests:
        print("No tests found!")
        return 1

    passed = 0
    failed = 0
    errors = []

    print(f"Running {len(tests)} tests...\n")

    current_file = None
    for fname, cls_name, method_name, cls in tests:
        if fname != current_file:
            current_file = fname
            print(f"  {fname}")

        test_id = f"{cls_name}.{method_name}"
        ok, err = run_test_method(cls, method_name, verbose)

        if ok:
            passed += 1
            if verbose:
                print(f"    ✓ {test_id}")
        else:
            failed += 1
            exc, tb = err
            print(f"    ✗ {test_id}")
            if verbose:
                for line in tb.strip().split("\n"):
                    print(f"      {line}")
            else:
                print(f"      {type(exc).__name__}: {exc}")
            errors.append((test_id, exc, tb))

    print(f"\n{'=' * 60}")
    print(f"  {passed} passed, {failed} failed, {passed + failed} total")
    print(f"{'=' * 60}")

    if errors and not verbose:
        print("\nFailed tests (run with -v for full tracebacks):")
        for test_id, exc, tb in errors:
            print(f"  ✗ {test_id}: {type(exc).__name__}: {exc}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
