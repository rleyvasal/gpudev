# tidy3 addon entry — separate repo, linked under addons/tidy3/
#
#   %local
#   %run /path/to/gpudev/addons/tidy3.py
#
# Or point at a clone elsewhere:
#   %run /path/to/tidy3/...  (after pip install -e)

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_CANDIDATES = [
    _HERE / "tidy3",  # symlink or submodule: addons/tidy3/ → tidy3 clone
    _HERE.parent.parent / "tidy3",  # sibling of gpudev/ (…/gpudevd/tidy3)
    Path("/app/data/gpudevd/tidy3"),
    Path("/app/data/tidy3"),
    Path.home() / "tidy3",
]

_root = next((p for p in _CANDIDATES if (p / "src" / "tidy3").is_dir() or (p / "tidy3").is_dir()), None)
if _root is None:
    # Fall back to already-installed package
    try:
        import tidy3  # noqa: F401
    except ImportError as e:
        raise FileNotFoundError(
            "tidy3 not found. Clone the tidy3 repo and either:\n"
            f"  ln -s ../../tidy3 {_HERE / 'tidy3'}\n"
            "or:\n"
            "  pip install -e /path/to/tidy3\n"
            "then re-run this addon."
        ) from e
else:
    src = _root / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    elif str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

try:
    from IPython import get_ipython
except Exception:  # pragma: no cover
    get_ipython = None

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
    "partial_run": partial_run,
    "options": options,
    "tidy3": tidy3,
}

ip = get_ipython() if get_ipython else None
if ip is not None and getattr(ip, "user_ns", None) is not None:
    ip.user_ns.update(_PUBLIC)
    try:
        ip.run_line_magic("load_ext", "tidy3.jupyter")
    except Exception:
        pass  # IPython magics optional

print(f"CRAFT: tidy3 {tidy3.__version__} loaded (Jupyter/SolveIt kernel integration)")
print("  tidy(df) >> filter(col('x') > 0) >> mutate(...)   # multi-line >> auto-rewritten")
print("  Partial run: put a pipe prefix in a cell and run it, or %%tidy3_run")
print("  %tidy3_pipes on|off   # toggle auto-rewrite of multi-line >> pipes")
