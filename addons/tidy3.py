# tidy3 addon entry — thin wrapper around the package loader.
#
# Always the same one-liner (local *or* GPU)::
#
#   %run /path/to/gpudev/addons/tidy3.py
#   # or directly:
#   %run /path/to/tidy3/tidy3.py
#
# CRAFT is optional. The package loader auto-detects CRAFT and seeds the
# remote under %gpudev when connected; without CRAFT it is pure local.

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "tidy3":  # pragma: no cover
    raise ImportError(
        "addons/tidy3.py was imported as module 'tidy3' (sys.path shadowing); "
        "load it with %run — the real package lives in the tidy3 clone"
    )

_HERE = Path(__file__).resolve().parent
_CANDIDATES = [
    _HERE / "tidy3",  # symlink: addons/tidy3/ → tidy3 clone
    _HERE.parent.parent / "tidy3",
    Path("/app/data/gpudevd/tidy3"),
    Path("/app/data/tidy3"),
    Path.home() / "tidy3",
    Path("/home/gpudev/tidy3"),
]

print("tidy3: addon searching for package…", flush=True)
for _p in _CANDIDATES:
    _ok = (_p / "tidy3.py").is_file() or (_p / "src" / "tidy3").is_dir()
    print(f"  [{'ok' if _ok else '  '}] {_p}", flush=True)

_root = next(
    (
        p
        for p in _CANDIDATES
        if (p / "tidy3.py").is_file()
        or (p / "src" / "tidy3").is_dir()
        or (p / "tidy3").is_dir()
    ),
    None,
)
if _root is None:
    try:
        import tidy3  # noqa: F401
    except ImportError as e:
        raise FileNotFoundError(
            "tidy3 not found. Clone https://github.com/rleyvasal/tidy3 and either:\n"
            f"  ln -s /path/to/tidy3 {_HERE / 'tidy3'}\n"
            "or:\n"
            "  pip install -e /path/to/tidy3\n"
            "Then re-run this addon."
        ) from e
    from IPython import get_ipython
    from tidy3.jupyter import ensure_ipython_integration, inject_api

    ip = get_ipython()
    if ip is not None:
        inject_api(ip, force=True)
        ensure_ipython_integration(quiet=False)
    print(f"tidy3: using installed package at {tidy3.__file__}", flush=True)
else:
    _root = _root.resolve()
    loader = _root / "tidy3.py"
    if not loader.is_file():
        loader = _root / "load.py"
    if not loader.is_file():
        raise FileNotFoundError(
            f"tidy3 clone at {_root} has no tidy3.py/load.py — git pull expand-dplyr-parity"
        )
    print(f"tidy3: addon → {_root.name}/{loader.name}", flush=True)
    exec(compile(loader.read_text(encoding="utf-8"), str(loader), "exec"), globals())
