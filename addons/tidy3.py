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

if __name__ == "tidy3":  # pragma: no cover
    # %run prepends addons/ to sys.path, so this file can shadow the real
    # tidy3 package. The path fix below prevents it; fail loudly if it recurs.
    raise ImportError(
        "addons/tidy3.py was imported as module 'tidy3' (sys.path shadowing); "
        "load it with %run — the real package lives in the tidy3 clone"
    )

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
    _pkg_dir = str(src if (src / "tidy3").is_dir() else _root)
    # ALWAYS re-insert at position 0: every %run prepends addons/ to sys.path,
    # and addons/tidy3.py would shadow the tidy3 package after the purge below
    # (self-import recursion). Front position must be reclaimed each load.
    while _pkg_dir in sys.path:
        sys.path.remove(_pkg_dir)
    sys.path.insert(0, _pkg_dir)

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
    anti_join,
    arrange,
    bind_cols,
    bind_rows,
    col,
    collect,
    count,
    cross_join,
    desc,
    distinct,
    drop,
    filter,
    filter_out,
    first,
    full_join,
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
    pull,
    rename,
    right_join,
    sample_frac,
    sample_n,
    scan_csv,
    scan_ipc,
    scan_parquet,
    select,
    semi_join,
    slice,
    slice_head,
    slice_max,
    slice_min,
    slice_sample,
    slice_tail,
    std,
    sum,
    summarise,
    summarize,
    tally,
    tidy,
    transmute,
    ungroup,
)

# Inject the full public API so SolveIt cells need no re-imports after %run.
_PUBLIC = {name: getattr(tidy3, name) for name in tidy3.__all__}
_PUBLIC.update(
    {
        "TidyFrame": TidyFrame,
        "tidy": tidy,
        "tidy3": tidy3,
        # Ensure the verbs we demo most often are always present even if
        # __all__ drifts (belt-and-suspenders for SolveIt user_ns).
        "col": col,
        "desc": desc,
        "filter": filter,
        "filter_out": filter_out,
        "select": select,
        "mutate": mutate,
        "arrange": arrange,
        "slice_max": slice_max,
        "slice_min": slice_min,
        "slice_head": slice_head,
        "slice_tail": slice_tail,
        "slice": slice,
        "slice_sample": slice_sample,
        "group_by": group_by,
        "summarise": summarise,
        "summarize": summarize,
        "collect": collect,
        "n": n,
        "mean": mean,
        "sum": sum,
        "min": min,
        "max": max,
        "median": median,
        "std": std,
        "first": first,
        "last": last,
        "count": count,
        "tally": tally,
        "head": head,
        "pull": pull,
        "drop": drop,
        "rename": rename,
        "distinct": distinct,
        "ungroup": ungroup,
        "transmute": transmute,
        "sample_n": sample_n,
        "sample_frac": sample_frac,
        "left_join": left_join,
        "inner_join": inner_join,
        "right_join": right_join,
        "full_join": full_join,
        "anti_join": anti_join,
        "semi_join": semi_join,
        "cross_join": cross_join,
        "bind_rows": bind_rows,
        "bind_cols": bind_cols,
        "scan_parquet": scan_parquet,
        "scan_csv": scan_csv,
        "scan_ipc": scan_ipc,
        "peek": peek,
        "partial_run": partial_run,
        "options": options,
    }
)

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
        # After the sys.modules purge, "tidy3.jupyter" may be marked loaded in
        # the extension manager while absent from sys.modules — plain load_ext
        # would no-op and keep the stale extension. Clear the mark, then load.
        _em = ip.extension_manager
        _loaded = getattr(_em, "loaded", set())
        if "tidy3.jupyter" in _loaded and "tidy3.jupyter" not in sys.modules:
            _loaded.discard("tidy3.jupyter")
        if "tidy3.jupyter" in _loaded:
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
