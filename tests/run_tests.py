#!/usr/bin/env python3
"""零依賴測試執行器 (不需安裝 pytest)，自動發現 tests/test_*.py。

執行: python tests/run_tests.py
"""
from __future__ import annotations

import glob
import importlib
import inspect
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))


class _MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        old = getattr(target, name)
        self._undo.append((target, name, old))
        setattr(target, name, value)

    def undo(self):
        for target, name, old in reversed(self._undo):
            setattr(target, name, old)
        self._undo.clear()


def main() -> int:
    modules = []
    for path in sorted(glob.glob(os.path.join(_HERE, "test_*.py"))):
        modules.append(importlib.import_module("tests." + os.path.basename(path)[:-3]))

    passed = failed = 0
    for mod in modules:
        tests = [(n, f) for n, f in vars(mod).items()
                 if n.startswith("test_") and callable(f)]
        for name, fn in tests:
            mp = _MonkeyPatch()
            try:
                if "monkeypatch" in inspect.signature(fn).parameters:
                    fn(mp)
                else:
                    fn()
                print(f"  PASS  {mod.__name__}.{name}")
                passed += 1
            except Exception:
                print(f"  FAIL  {mod.__name__}.{name}")
                traceback.print_exc()
                failed += 1
            finally:
                mp.undo()
    print(f"\n結果: {passed} passed, {failed} failed (共 {passed + failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
