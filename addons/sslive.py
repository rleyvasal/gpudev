# sslive addon entry — thin wrapper around the package loader.
#
# Always the same one-liner (local *or* GPU)::
#
#   %run /path/to/gpudev/addons/sslive.py
#   # or directly:
#   %run /path/to/sslive/sslive.py
#
# CRAFT is optional. sslive auto-detects CRAFT for slide ▶ Run (GPU when
# connected, else host IPython). No second load recipe.

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "sslive":  # pragma: no cover
    raise ImportError(
        "addons/sslive.py was imported as module 'sslive' (sys.path shadowing); "
        "load it with %run — the real file lives in the sslive clone"
    )

_HERE = Path(__file__).resolve().parent
_CANDIDATES = [
    _HERE / "sslive" / "sslive.py",  # symlink: addons/sslive/ → clone
    _HERE / "sslive" / "load.py",
    _HERE.parent.parent / "sslive" / "sslive.py",
    _HERE.parent.parent / "sslive" / "load.py",
    Path("/app/data/gpudevd/sslive/sslive.py"),
    Path("/app/data/gpudevd/sslive/load.py"),
    Path("/app/data/sslive/sslive.py"),
    Path.home() / "sslive" / "sslive.py",
    Path("/home/gpudev/sslive/sslive.py"),
]

print("sslive: addon searching for loader…", flush=True)
for _p in _CANDIDATES:
    print(f"  [{'ok' if _p.is_file() else '  '}] {_p}", flush=True)

_target = next((p for p in _CANDIDATES if p.is_file()), None)
if _target is None:
    raise FileNotFoundError(
        "sslive not found. Clone https://github.com/rleyvasal/sslive and either:\n"
        f"  ln -s /path/to/sslive {_HERE / 'sslive'}\n"
        "or:\n"
        "  %run /path/to/sslive/sslive.py"
    )

root = str(_target.parent.resolve())
while root in sys.path:
    sys.path.remove(root)
sys.path.insert(0, root)

print(f"sslive: addon → {_target}", flush=True)
# Execute as __main__ so bootstrap treats it like a direct %run.
exec(compile(_target.read_text(encoding="utf-8"), str(_target), "exec"), globals())
