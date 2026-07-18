# tidy3 addon entry — separate repo, linked under addons/tidy3/
#
# Same pattern as pcviz / mojo / sslive:
#
#   %local
#   %run /path/to/gpudev/CRAFT.py
#   %run /path/to/gpudev/addons/tidy3.py
#   %gpu
#
# Local inject + remote seed: under %gpu, cells run on the remote kernel
# (separate namespace). This loader puts tidy3 on the remote so `tidy` works
# there too — same UX as loading any other addon under %local.

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_CANDIDATES = [
    _HERE / "tidy3",  # symlink: addons/tidy3/ → tidy3 clone
    _HERE.parent.parent / "tidy3",  # sibling …/gpudevd/tidy3
    Path("/app/data/gpudevd/tidy3"),
    Path("/app/data/tidy3"),
    Path.home() / "tidy3",
    Path("/home/gpudev/tidy3"),
]

_root = next(
    (p for p in _CANDIDATES if (p / "src" / "tidy3").is_dir() or (p / "tidy3").is_dir()),
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
            "then re-run this addon."
        ) from e
else:
    src = _root / "src"
    if src.is_dir() and (src / "tidy3").is_dir():
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
    elif str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

try:
    from IPython import get_ipython
except Exception:  # pragma: no cover
    get_ipython = None

# Fresh import on every %run so a `git pull` of the tidy3 clone takes effect
# without restarting the kernel (this addon is the only loader).
for _m in [m for m in list(sys.modules) if m == "tidy3" or m.startswith("tidy3.")]:
    del sys.modules[_m]

import tidy3
from tidy3 import (  # noqa: E402
    TidyFrame,
    arrange,
    col,
    collect,
    count,
    desc,
    distinct,
    drop,
    filter,
    first,
    group_by,
    head,
    inner_join,
    last,
    left_join,
    max,
    mean,
    median,
    min,
    mutate,
    n,
    options,
    partial_run,
    peek,
    rename,
    sample_frac,
    sample_n,
    scan_csv,
    scan_ipc,
    scan_parquet,
    select,
    slice_head,
    std,
    sum,
    summarise,
    summarize,
    tidy,
    transmute,
    ungroup,
)

_PUBLIC = {
    "TidyFrame": TidyFrame,
    "tidy": tidy,
    "scan_parquet": scan_parquet,
    "scan_csv": scan_csv,
    "scan_ipc": scan_ipc,
    "col": col,
    "n": n,
    "mean": mean,
    "sum": sum,
    "min": min,
    "max": max,
    "median": median,
    "std": std,
    "first": first,
    "last": last,
    "desc": desc,
    "filter": filter,
    "mutate": mutate,
    "transmute": transmute,
    "select": select,
    "drop": drop,
    "rename": rename,
    "arrange": arrange,
    "distinct": distinct,
    "group_by": group_by,
    "ungroup": ungroup,
    "summarise": summarise,
    "summarize": summarize,
    "count": count,
    "head": head,
    "slice_head": slice_head,
    "sample_n": sample_n,
    "sample_frac": sample_frac,
    "left_join": left_join,
    "inner_join": inner_join,
    "collect": collect,
    "peek": peek,
    "partial_run": partial_run,
    "options": options,
    "tidy3": tidy3,
}

# Remote seeding: push the local tidy3 source to the remote kernel through
# the ZMQ channel (tidy3.craft). No shared filesystem or remote clone needed.
# Re-seeds automatically when the remote kernel changes (%restart_kernel /
# reconnect) or when the local source changes (content stamp).
_SEED_STATE = {"stamp": None, "kc_id": None, "ok": False}


def seed_remote(*, force: bool = False, quiet: bool = False, style_polars: bool = True) -> bool:
    """Ship tidy3 to the CRAFT remote kernel and load it there (idempotent)."""
    try:
        from tidy3 import craft
    except ImportError:
        print(
            "CRAFT: tidy3 remote seed unavailable — loaded tidy3 has no craft "
            "module (clone older than this addon). git pull the tidy3 clone "
            "and re-run addons/tidy3.py"
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
                "CRAFT: tidy3 local only (remote not connected yet — "
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
        return _SEED_STATE["ok"]  # same source + same kernel: keep last outcome

    ok, msg = craft.seed(rr, payload=payload, stamp=stamp, style_polars=style_polars)
    _SEED_STATE.update(stamp=stamp, kc_id=kc_id, ok=ok)
    if ok:
        if not quiet:
            print(f"CRAFT: {msg}")
    else:
        print(
            "CRAFT: tidy3 remote seed FAILED — %gpu cells won't know tidy3.\n"
            + msg
            + "\nRetry with seed_tidy3_remote(force=True)"
        )
    return ok


def _maybe_seed_on_cell(_info=None):
    """Before each cell: if in %gpu Python mode, ensure the remote has tidy3."""
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
    ip.user_ns.update(_PUBLIC)
    ip.user_ns["seed_tidy3_remote"] = seed_remote
    try:
        # reload (not load) when already loaded: after the sys.modules purge
        # above, plain load_ext would no-op and keep the stale extension
        _em = ip.extension_manager
        if "tidy3.jupyter" in getattr(_em, "loaded", set()):
            _em.reload_extension("tidy3.jupyter")
        else:
            _em.load_extension("tidy3.jupyter")
    except Exception:
        pass
    # Re-running the addon must not stack pre_run_cell callbacks
    try:
        _prev = ip.user_ns.get("_tidy3_seed_cb")
        if _prev is not None:
            try:
                ip.events.unregister("pre_run_cell", _prev)
            except Exception:
                pass
        ip.events.register("pre_run_cell", _maybe_seed_on_cell)
        ip.user_ns["_tidy3_seed_cb"] = _maybe_seed_on_cell
    except Exception:
        pass
    # If already connected (user re-ran addon after %gpu), seed now
    seed_remote(quiet=False)

print(
    f"CRAFT: tidy3 {tidy3.__version__} loaded (local) "
    f"from {Path(tidy3.__file__).resolve().parent}"
)
print("  tidy(df) >> filter(...) >> mutate(...)   # multi-line >> auto-rewritten")
print("  %gpu: source is pushed to the remote kernel automatically; after manual")
print("        kernel surgery use seed_tidy3_remote(force=True)")
print("  Partial: Run Selected Text / %%tidy3_run / own cell  |  %tidy3_pipes on|off")
