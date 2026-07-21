# plot3 addon entry — separate repo, linked under addons/plot3/
#
# Same pattern as tidy3 / sslive:
#
#   %local
#   %run /path/to/gpudev/CRAFT.py
#   %run /path/to/gpudev/addons/tidy3.py   # optional but usual for pipes
#   %run /path/to/gpudev/addons/plot3.py
#   %gpu                                    # optional remote compute
#
# plot3 is a *local* viewer by default (iframe in SolveIt, like pcviz).
# Under %gpu, %plot3 is registered as a host-local magic and can snapshot
# remote frames. ggplot(...) cells that run on the remote need plot3 seeded
# there — this loader does that automatically when CRAFT is connected.

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
    _HERE.parent.parent / "plot3",  # sibling …/gpudevd/plot3 or ~/plot3 next to gpudev
    Path("/app/data/gpudevd/plot3"),
    Path("/app/data/plot3"),
    Path.home() / "plot3",
    Path("/home/gpudev/plot3"),
]

_root = next(
    (
        p
        for p in _CANDIDATES
        if (p / "plot3" / "__init__.py").is_file() or (p / "load.py").is_file()
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
    _pkg_dir = None
else:
    _pkg_dir = str(_root.resolve())
    while _pkg_dir in sys.path:
        sys.path.remove(_pkg_dir)
    sys.path.insert(0, _pkg_dir)

try:
    from IPython import get_ipython
except Exception:  # pragma: no cover
    get_ipython = None

# Fresh import on every %run so a git pull of the plot3 clone takes effect.
for _m in [m for m in list(sys.modules) if m == "plot3" or m.startswith("plot3.")]:
    del sys.modules[_m]

import plot3  # noqa: E402
from plot3.jupyter import register_plot3  # noqa: E402

_PUBLIC = {
    name: getattr(plot3, name)
    for name in plot3.__all__
    if name != "load_ipython_extension" and hasattr(plot3, name)
}
_PUBLIC["plot3"] = plot3

# ── remote seed (optional; for ggplot under %gpu) ───────────────────────────
_SEED_STATE = {"stamp": None, "kc_id": None, "ok": False}


def seed_remote(*, force: bool = False, quiet: bool = False) -> bool:
    """Ship plot3 source to the CRAFT remote kernel (idempotent)."""
    try:
        from plot3 import craft
    except ImportError:
        if not quiet:
            print(
                "CRAFT: plot3 remote seed unavailable — package has no craft "
                "module yet. Local plot3 still works under %local / host magics."
            )
        return False

    ip = get_ipython() if get_ipython else None
    if ip is None:
        return False
    ns = ip.user_ns or {}
    rr = ns.get("remote_run_")
    mgr = ns.get("_exec_mgr")
    if not callable(rr) or mgr is None:
        if not quiet:
            print(
                "CRAFT: plot3 local only (remote not connected yet — "
                "will seed on first %gpu cell)"
            )
        return False

    payload, stamp = craft.build_payload()
    kc_id = id(getattr(mgr, "remote_kc", None))
    if (
        not force
        and _SEED_STATE["stamp"] == stamp
        and _SEED_STATE["kc_id"] == kc_id
    ):
        return _SEED_STATE["ok"]

    ok, msg = craft.seed(rr, payload=payload, stamp=stamp)
    _SEED_STATE.update(stamp=stamp, kc_id=kc_id, ok=ok)
    if ok:
        if not quiet:
            print(f"CRAFT: {msg}")
    else:
        print(
            "CRAFT: plot3 remote seed FAILED — remote ggplot cells won't work.\n"
            + msg
            + "\nRetry with seed_plot3_remote(force=True). "
            "Local %plot3 / host figures still work."
        )
    return ok


def _maybe_seed_on_cell(_info=None):
    """Before each cell: if in %gpu Python mode, ensure the remote has plot3."""
    try:
        import gpudev_craft.core as _core

        router = getattr(_core, "ROUTER", None)
        py_be = getattr(_core, "PY_BACKEND", None)
        if router is None or py_be is None or router.backend is not py_be:
            return
    except Exception:
        return
    seed_remote(quiet=True)


ip = get_ipython() if get_ipython else None
if ip is not None and getattr(ip, "user_ns", None) is not None:
    # Register magics + inject public API (addon contract — do not rely on %run)
    register_plot3(quiet=True)
    ip.user_ns.update(_PUBLIC)
    ip.user_ns["seed_plot3_remote"] = seed_remote
    # Keep %plot3 on the host under %gpu (viewer + hide-from-AI are local)
    try:
        reg = ip.user_ns.get("register_local_magic")
        if callable(reg):
            reg("%plot3")
    except Exception:
        pass
    try:
        _prev = ip.user_ns.get("_plot3_seed_cb")
        if _prev is not None:
            try:
                ip.events.unregister("pre_run_cell", _prev)
            except Exception:
                pass
        ip.events.register("pre_run_cell", _maybe_seed_on_cell)
        ip.user_ns["_plot3_seed_cb"] = _maybe_seed_on_cell
    except Exception:
        pass
    seed_remote(quiet=False)

print(
    f"CRAFT: plot3 {plot3.__version__} loaded (local) "
    f"from {Path(plot3.__file__).resolve().parent}"
)
print("  ggplot(df, aes(...)) + geom_point()   # iframe in SolveIt (red-eye hide)")
print("  %plot3 df x=a y=b [z=c] [color=d]     # host-local under %gpu")
print("  %gpu: plot3 is seeded to the remote for ggplot cells; after kernel")
print("        surgery use seed_plot3_remote(force=True)")
