# tidy3 addon entry — separate repo, linked under addons/tidy3/
#
#   %local
#   %run /path/to/gpudev/CRAFT.py
#   %run /path/to/gpudev/addons/tidy3.py   # ← own cell preferred
#   %gpu
#
# IMPORTANT (SolveIt on the GPU host):
#   Paths are /app/data/gpudevd/... on the *server*, not your Mac.
#   git pull both gpudev and tidy3 on that host, then re-%run this file.
#   You must see:  CRAFT: tidy3 … loaded (local) from …

from __future__ import annotations

import sys
import traceback
from pathlib import Path

print("CRAFT: tidy3 addon starting…", flush=True)

if __name__ == "tidy3":  # pragma: no cover
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

print("CRAFT: searching for tidy3 package…", flush=True)
for _p in _CANDIDATES:
    _ok = (_p / "src" / "tidy3").is_dir() or (_p / "tidy3").is_dir()
    print(f"  [{'ok' if _ok else '  '}] {_p}", flush=True)

_root = next(
    (p for p in _CANDIDATES if (p / "src" / "tidy3").is_dir() or (p / "tidy3").is_dir()),
    None,
)
_pkg_dir = None
if _root is None:
    try:
        import tidy3  # noqa: F401

        print(f"CRAFT: using installed tidy3 at {tidy3.__file__}", flush=True)
    except ImportError as e:
        raise FileNotFoundError(
            "tidy3 not found on this machine. On the SolveIt/GPU host run:\n"
            "  cd /app/data/gpudevd && git clone https://github.com/rleyvasal/tidy3.git\n"
            "  # or: ln -s /path/to/tidy3 /app/data/gpudevd/gpudev/addons/tidy3\n"
            "  cd tidy3 && git pull && git checkout expand-dplyr-parity  # if needed\n"
            "Then re-run this addon."
        ) from e
else:
    src = _root / "src"
    _pkg_dir = str(src if (src / "tidy3").is_dir() else _root)
    while _pkg_dir in sys.path:
        sys.path.remove(_pkg_dir)
    sys.path.insert(0, _pkg_dir)
    print(f"CRAFT: tidy3 root={_root.resolve()}  pkg_path={_pkg_dir}", flush=True)

try:
    from IPython import get_ipython
except Exception:  # pragma: no cover
    get_ipython = None

# Fresh import every %run so git pull takes effect without kernel restart.
for _m in [m for m in list(sys.modules) if m == "tidy3" or m.startswith("tidy3.")]:
    del sys.modules[_m]

try:
    import tidy3
except Exception:
    print("CRAFT: FAILED to import tidy3:", flush=True)
    traceback.print_exc()
    raise

print(
    f"CRAFT: imported tidy3 {getattr(tidy3, '__version__', '?')} "
    f"from {Path(tidy3.__file__).resolve()}",
    flush=True,
)

# Build user_ns from package __all__ only — never hard-require new symbols
# (old clones must still load; missing names are simply omitted).
_PUBLIC = {"tidy3": tidy3}
for _name in getattr(tidy3, "__all__", []):
    if _name.startswith("_"):
        continue
    try:
        _PUBLIC[_name] = getattr(tidy3, _name)
    except AttributeError:
        pass
# Always expose the entry helpers if present
for _name in ("tidy", "TidyFrame", "col", "filter", "select", "mutate", "arrange"):
    if hasattr(tidy3, _name):
        _PUBLIC[_name] = getattr(tidy3, _name)

_SEED_STATE = {"stamp": None, "kc_id": None, "ok": False}


def seed_remote(*, force: bool = False, quiet: bool = False, style_polars: bool = True) -> bool:
    """Ship tidy3 to the CRAFT remote kernel (idempotent)."""
    try:
        from tidy3 import craft
    except ImportError:
        if not quiet:
            print(
                "CRAFT: tidy3 remote seed unavailable (no craft module). "
                "git pull the tidy3 clone on this host and re-run addons/tidy3.py",
                flush=True,
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
                "will seed on first %gpu cell)",
                flush=True,
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

    ok, msg = craft.seed(rr, payload=payload, stamp=stamp, style_polars=style_polars)
    _SEED_STATE.update(stamp=stamp, kc_id=kc_id, ok=ok)
    if ok:
        if not quiet:
            print(f"CRAFT: {msg}", flush=True)
    else:
        print(
            "CRAFT: tidy3 remote seed FAILED — %gpu cells won't know tidy3.\n"
            + msg
            + "\nRetry with seed_tidy3_remote(force=True)",
            flush=True,
        )
    return ok


def _maybe_seed_on_cell(_info=None):
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
if ip is None:
    print(
        "CRAFT: WARNING — get_ipython() is None; magics/pipe rewriter not registered.\n"
        "  Are you running this with %run inside SolveIt/IPython?",
        flush=True,
    )
elif getattr(ip, "user_ns", None) is None:
    print("CRAFT: WARNING — no user_ns on IPython shell", flush=True)
else:
    ip.user_ns.update(_PUBLIC)
    ip.user_ns["seed_tidy3_remote"] = seed_remote
    print(f"CRAFT: injected {len(_PUBLIC)} names into user_ns", flush=True)

    # Register pipe rewriter + %tidy3_pipes / %%tidy3_run
    _ext_ok = False
    try:
        from tidy3.jupyter import ensure_ipython_integration, enable_pipe_transform

        _ext_ok = bool(ensure_ipython_integration(quiet=False))
        enable_pipe_transform(ip)
        # Verify magic
        _lines = getattr(ip.magics_manager, "magics", {}).get("line", {})
        if "tidy3_pipes" in _lines:
            print("CRAFT: %tidy3_pipes registered", flush=True)
        else:
            print(
                "CRAFT: WARNING — %tidy3_pipes still missing after ensure; "
                "trying load_extension…",
                flush=True,
            )
            _em = ip.extension_manager
            _loaded = getattr(_em, "loaded", set())
            _loaded.discard("tidy3.jupyter")
            _em.load_extension("tidy3.jupyter")
            ensure_ipython_integration(quiet=False)
            _lines = getattr(ip.magics_manager, "magics", {}).get("line", {})
            print(
                f"CRAFT: tidy3_pipes in magics: {'tidy3_pipes' in _lines}",
                flush=True,
            )
        _cleanup = getattr(ip, "input_transformers_cleanup", []) or []
        from tidy3.jupyter import _is_pipe_transformer

        _pos = next(
            (i for i, t in enumerate(_cleanup) if _is_pipe_transformer(t)), None
        )
        print(
            f"CRAFT: pipe transformer "
            f"{'ON at cleanup[' + str(_pos) + ']' if _pos is not None else 'OFF'}",
            flush=True,
        )
    except Exception as _ext_err:
        print(f"CRAFT: tidy3.jupyter setup FAILED: {_ext_err}", flush=True)
        traceback.print_exc()
        print(
            "  Workaround: wrap multi-line pipes in parentheses:\n"
            "    ( tidy(cars) >> filter(...) >> summarise(...) )",
            flush=True,
        )

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
    seed_remote(quiet=False)

print(
    f"CRAFT: tidy3 {tidy3.__version__} loaded (local) "
    f"from {Path(tidy3.__file__).resolve().parent}",
    flush=True,
)
print(
    "  multi-line >> auto-rewritten when pipe transformer is ON\n"
    "  fallback: ( tidy(df) >> filter(...) >> ... )\n"
    "  %tidy3_pipes status | on | off     seed_tidy3_remote(force=True)",
    flush=True,
)
