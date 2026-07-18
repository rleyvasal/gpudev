"""Install hooks: core CRAFT + optional addons under ``gpudev/addons/``."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

_GPUDEV_ROOT = Path(__file__).resolve().parent.parent
_ADDONS = _GPUDEV_ROOT / "addons"


def _inject_installers_into_user_ns() -> None:
    try:
        from IPython import get_ipython

        ip = get_ipython()
    except Exception:
        return
    if ip is None:
        return
    ns = ip.user_ns
    ns["install_core"] = install_core
    ns["install"] = install_core
    ns["install_pcviz"] = install_pcviz
    ns["install_sslive"] = install_sslive
    ns["install_mojo"] = install_mojo
    ns["install_plot3"] = install_plot3


def install_core(*, quiet: bool = False) -> bool:
    """Load GPU connection magics (%gpu, %local, …) and remote_run_."""
    from . import core

    ok = core.install_core(quiet=quiet)
    _inject_installers_into_user_ns()
    return ok


install = install_core


def _run_addon_script(path: Path, *, label: str) -> bool:
    path = path.expanduser().resolve()
    if not path.is_file():
        print(f"CRAFT: {label} not found at {path}")
        print(f"  Expected under {_ADDONS}/ or pass an absolute path")
        return False
    try:
        from IPython import get_ipython as _gi

        ip = _gi()
    except Exception:
        ip = None
    if ip is None:
        runpy.run_path(str(path), run_name=f"craft_addon_{label}")
        if str(path.parent) not in sys.path:
            sys.path.insert(0, str(path.parent))
        print(f"CRAFT: loaded {label} from {path} (no IPython user_ns)")
        return True

    ns = ip.user_ns
    # gpudev root on path for gpudev_craft imports (mojo)
    if str(_GPUDEV_ROOT) not in sys.path:
        sys.path.insert(0, str(_GPUDEV_ROOT))
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
    """Load pcviz — prefer ``%local`` + ``%run …/addons/pcviz.py``."""
    p = Path(path) if path else _ADDONS / "pcviz.py"
    ok = _run_addon_script(p, label="pcviz")
    if ok and not quiet:
        print("  %pointcloud  %pointcloud_var  %pointcloud_plotly")
    return ok


def install_mojo(path: str | Path | None = None, *, quiet: bool = False) -> bool:
    """Load Mojo addon — prefer ``%local`` + ``%run …/addons/mojo.py``."""
    p = Path(path) if path else _ADDONS / "mojo.py"
    ok = _run_addon_script(p, label="mojo")
    if ok and not quiet:
        print("  %gpum  %restart_mojo  %mojo_*  %bench")
    return ok


def install_plot3(path: str | Path | None = None, *, quiet: bool = False) -> bool:
    """Load plot3 — prefer ``%local`` + ``%run …/addons/plot3.py``."""
    # The wrapper owns the candidate list for locating the plot3 repo and
    # prints the resolved path + API names itself.
    p = Path(path) if path else _ADDONS / "plot3.py"
    ok = _run_addon_script(p, label="plot3")
    if not ok and not quiet:
        print(
            "  Tip:\n"
            "    %local\n"
            "    %run /path/to/gpudev/addons/plot3.py\n"
            "  or clone https://github.com/rleyvasal/plot3 next to gpudev"
        )
    return ok


def install_sslive(path: str | Path | None = None, *, quiet: bool = False) -> bool:
    """Load sslive — prefer ``%local`` + ``%run …/addons/sslive.py``."""
    # The wrapper owns the candidate list for locating the sslive repo and
    # prints the resolved path + magic names itself.
    p = Path(path) if path else _ADDONS / "sslive.py"
    ok = _run_addon_script(p, label="sslive")
    if not ok and not quiet:
        print(
            "  Tip:\n"
            "    %local\n"
            "    %run /path/to/gpudev/addons/sslive.py\n"
            "  or clone sslive into addons/sslive (see addons/README.md)"
        )
    return ok
