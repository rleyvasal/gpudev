"""Install hooks: core CRAFT + optional addons (pcviz, sslive, mojo).

Dialog / LLM context should only import these — not pull in full source.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

# gpudev/ repo root (parent of this package)
_GPUDEV_ROOT = Path(__file__).resolve().parent.parent
# common sibling of gpudev (sslive next to gpudev)
_WORKSPACE = _GPUDEV_ROOT.parent


def install_core(*, quiet: bool = False) -> bool:
    """Load GPU connection magics (%gpu, %local, …) and remote_run_."""
    from . import core

    return core.install_core(quiet=quiet)


# Alias used by some docs / old muscle memory
install = install_core


def install_mojo(*, quiet: bool = False) -> bool:
    """Mojo magics ship with core for now (%gpum, %mojo_*, %bench).

    Call after install_core() if you only want to re-print Mojo help.
    """
    from . import core

    # Ensure core is up (idempotent)
    core.install_core(quiet=True)
    if not quiet:
        print("CRAFT Mojo ready (included in core)")
        print("  %gpum            Mojo mode on GPU container")
        print("  %restart_mojo    clear Mojo history")
        print("  %mojo_history  %mojo_run  %mojo_add  %bench")
    return True


def _run_addon_script(path: Path, *, label: str) -> bool:
    """%run-style load of a sibling .py into the interactive namespace."""
    path = path.expanduser().resolve()
    if not path.is_file():
        print(f"CRAFT: {label} not found at {path}")
        print(f"  Pass path=... or place the file next to gpudev/")
        return False
    try:
        from IPython import get_ipython as _gi

        ip = _gi()
    except Exception:
        ip = None
    if ip is None:
        # Non-interactive: still exec the module for side effects
        runpy.run_path(str(path), run_name=f"craft_addon_{label}")
        if not path.parent.as_posix() in sys.path:
            sys.path.insert(0, str(path.parent))
        print(f"CRAFT: loaded {label} from {path} (no IPython user_ns)")
        return True

    ns = ip.user_ns
    # Ensure gpudev root on path for any relative imports
    root = str(path.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        runpy.run_path(str(path), init_globals=ns, run_name=path.stem)
    except Exception as e:
        print(f"CRAFT: failed to load {label} from {path}: {e}")
        return False
    print(f"CRAFT: {label} loaded from {path}")
    return True


def install_pcviz(path: str | Path | None = None, *, quiet: bool = False) -> bool:
    """Load pcviz (%pointcloud, %pointcloud_plotly) — optional addon."""
    if path is not None:
        p = Path(path)
    else:
        p = _GPUDEV_ROOT / "pcviz.py"
    ok = _run_addon_script(p, label="pcviz")
    if ok and not quiet:
        print("  %pointcloud  %pointcloud_var  %pointcloud_plotly")
    return ok


def install_sslive(path: str | Path | None = None, *, quiet: bool = False) -> bool:
    """Load sslive (%slive, %slive_export) — optional slides addon."""
    if path is not None:
        candidates = [Path(path)]
    else:
        candidates = [
            _WORKSPACE / "sslive" / "sslive.py",
            _GPUDEV_ROOT / "sslive" / "sslive.py",
            Path.home() / "sslive" / "sslive.py",
            _WORKSPACE / "sslive.py",
        ]
    p = next((c for c in candidates if c.is_file()), candidates[0])
    ok = _run_addon_script(p, label="sslive")
    if ok and not quiet:
        print("  %slive  %slive_export  (host-local under %gpu)")
    return ok
