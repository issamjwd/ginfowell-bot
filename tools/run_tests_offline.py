"""Run the test suite WITHOUT pytest installed (offline machines). CI uses real pytest (requirements-dev.txt).

    python tools/run_tests_offline.py [-k substring]

Provides a tiny stand-in for the parts of pytest the suite uses: fixtures (incl. tmp_path, monkeypatch),
pytest.raises(match=...), pytest.mark.parametrize. If real pytest is importable, it simply runs that instead.
"""
from __future__ import annotations

import contextlib
import importlib
import inspect
import os
import re
import shutil
import sys
import tempfile
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _shim() -> types.ModuleType:
    m = types.ModuleType("pytest")
    m._fixtures = {}

    def fixture(fn=None, **_kw):
        def reg(f):
            m._fixtures[f.__name__] = f
            return f
        return reg(fn) if fn else reg

    class _Raises:
        def __init__(self, exc, match=None):
            self.exc, self.match, self.value = exc, match, None

        def __enter__(self):
            return self

        def __exit__(self, et, ev, tb):
            if et is None:
                raise AssertionError(f"DID NOT RAISE {self.exc}")
            if not issubclass(et, self.exc):
                return False
            if self.match and not re.search(self.match, str(ev)):
                raise AssertionError(f"{ev!r} does not match {self.match!r}")
            self.value = ev
            return True

    class _Mark:
        def parametrize(self, names, values):
            names = [n.strip() for n in names.split(",")]

            def deco(f):
                f._params = getattr(f, "_params", []) + [(names, values)]
                return f
            return deco

        def skipif(self, cond, reason=""):
            def deco(f):
                if cond:
                    f._skip = reason or "skipif"
                return f
            return deco

        def __getattr__(self, _):
            return lambda *a, **k: (lambda f: f)

    class Skipped(Exception):
        pass

    def skip(reason=""):
        raise Skipped(reason)

    m.fixture, m.raises, m.mark, m.skip, m.Skipped = fixture, _Raises, _Mark(), skip, Skipped
    return m


class MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setenv(self, k, v):
        old = os.environ.get(k)
        os.environ[k] = v
        self._undo.append(lambda: os.environ.pop(k, None) if old is None else os.environ.__setitem__(k, old))

    def delenv(self, k, raising=False):
        if k in os.environ:
            old = os.environ.pop(k)
            self._undo.append(lambda: os.environ.__setitem__(k, old))

    def setattr(self, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        self._undo.append(lambda: setattr(obj, name, old))

    def undo(self):
        for u in reversed(self._undo):
            u()


def _cases(fn):
    combos = [({}, "")]
    for names, values in getattr(fn, "_params", []):
        new = []
        for base, label in combos:
            for i, v in enumerate(values):
                v = v if isinstance(v, tuple) and len(names) > 1 else (v,) if len(names) == 1 else v
                new.append(({**base, **dict(zip(names, v))}, f"{label}[{i}]"))
        combos = new
    return combos


def run(filter_k: str | None) -> int:
    sys.modules["pytest"] = pt = _shim()
    sys.path.insert(0, str(ROOT))
    passed = failed = skipped = 0
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        mod = importlib.import_module(f"tests.{path.stem}")
        fixtures = dict(pt._fixtures)
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if not name.startswith("test_") or fn.__module__ != mod.__name__:
                continue
            for params, label in _cases(fn):
                tid = f"{path.name}::{name}{label}"
                if filter_k and filter_k not in tid:
                    continue
                if getattr(fn, "_skip", None):
                    skipped += 1
                    print(f"SKIP {tid} ({fn._skip})")
                    continue
                tmp = Path(tempfile.mkdtemp(prefix="t_"))
                mp = MonkeyPatch()
                with contextlib.ExitStack() as stack:
                    def resolve(n):
                        if n in params:
                            return params[n]
                        if n == "tmp_path":
                            return tmp
                        if n == "monkeypatch":
                            return mp
                        f = fixtures[n]
                        v = f(*[resolve(a) for a in inspect.signature(f).parameters])
                        if inspect.isgenerator(v):
                            gen = v
                            v = next(gen)
                            stack.callback(lambda g=gen: next(g, None))
                        return v
                    try:
                        fn(**{a: resolve(a) for a in inspect.signature(fn).parameters})
                        passed += 1
                        print(f"PASS {tid}")
                    except pt.Skipped as e:
                        skipped += 1
                        print(f"SKIP {tid} ({e})")
                    except Exception:  # noqa: BLE001 - report every failure
                        failed += 1
                        print(f"FAIL {tid}\n{traceback.format_exc()}")
                    finally:
                        mp.undo()
                shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        import pytest  # noqa: F401
        raise SystemExit(pytest.main([str(ROOT / "tests"), "-q"]))
    except ImportError:
        k = sys.argv[sys.argv.index("-k") + 1] if "-k" in sys.argv else None
        raise SystemExit(run(k))
