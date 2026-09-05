# plot3 addon entry — thin wrapper around the package loader.
#
# Always the same one-liner (local *or* GPU)::
#
#   %run /path/to/gpudev/addons/plot3.py
#   # or directly:
#   %run /path/to/plot3/plot3.py
#
# CRAFT is optional. The package loader auto-detects CRAFT and seeds the
# remote under %gpudev when connected; without CRAFT it is pure local.

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "plot3":  # pragma: no cover
    raise ImportError(
        "addons/plot3.py was imported as module 'plot3' (sys.path shadowing); "
        "load it with %run — the real package lives in the plot3 clone"
    )

_HERE = Path(__file__).resolve().parent
_CANDIDATES = [
    _HERE / "plot3",  # symlink: addons/plot3/ → plot3 clone
    _HERE.parent.parent / "plot3",
    Path("/app/data/gpudevd/plot3"),
    Path("/app/data/plot3"),
    Path.home() / "plot3",
    Path("/home/gpudev/plot3"),
]

_root = next(
    (
        p
        for p in _CANDIDATES
        if (p / "plot3.py").is_file()
        or (p / "load.py").is_file()
        or (p / "plot3" / "__init__.py").is_file()
    ),
    None,
)
if _root is None:
    try:
        import plot3  # noqa: F401
    except ImportError as e:
        raise FileNotFoundError(
            "plot3 not found. Clone https://github.com/rleyvasal/plot3 and either:\n"
            f"  ln -s /path/to/plot3 {_HERE / 'plot3'}\n"
            "or:\n"
            "  pip install -e /path/to/plot3\n"
            "then re-run this addon."
        ) from e
    # Installed package only — fall back to %load_ext path via a minimal inject.
    from IPython import get_ipython
    from plot3.jupyter import register_plot3

    register_plot3(quiet=False, r_style=True)
    print(
        f"plot3: using installed package at {plot3.__file__} "
        "(no plot3.py loader in a clone)",
        flush=True,
    )
else:
    _root = _root.resolve()
    loader = _root / "plot3.py"
    if not loader.is_file():
        loader = _root / "load.py"
    if not loader.is_file():
        # Older clone: put root on path and register directly
        pkg = str(_root)
        while pkg in sys.path:
            sys.path.remove(pkg)
        sys.path.insert(0, pkg)
        for _m in [
            m for m in list(sys.modules) if m == "plot3" or m.startswith("plot3.")
        ]:
            del sys.modules[_m]
        import plot3
        from plot3.jupyter import register_plot3
        from IPython import get_ipython

        ip = get_ipython()
        register_plot3(quiet=False, r_style=True)
        if ip is not None and getattr(ip, "user_ns", None) is not None:
            for name in plot3.__all__:
                if name == "load_ipython_extension":
                    continue
                if hasattr(plot3, name):
                    ip.user_ns[name] = getattr(plot3, name)
            ip.user_ns["plot3"] = plot3
        print(
            f"plot3: loaded from {_root} (legacy path — pull latest for plot3.py)",
            flush=True,
        )
    else:
        print(f"plot3: addon → {_root.name}/{loader.name}", flush=True)
        exec(compile(loader.read_text(encoding="utf-8"), str(loader), "exec"), globals())
