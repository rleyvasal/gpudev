# CRAFT dialog loader — keep this cell short (LLM context budget).
# Core: gpudev_craft/   Addons: addons/ (pcviz, mojo, sslive)
#
# This file does ONE thing: load CRAFT. It takes no arguments.
#
#   %local
#   %run /path/to/gpudev/CRAFT.py
#   %run /path/to/gpudev/addons/pcviz.py    # optional
#   %run /path/to/gpudev/addons/mojo.py     # optional
#   %run /path/to/gpudev/addons/sslive.py   # optional (separate repo via link)
#   %gpudev solveite
#   %sslive
#
# First-time setup is a separate, one-time step — `%gpudev_setup <name>
# --domain <d>` — deliberately NOT folded into this file. Folding it in would
# make `%run CRAFT.py` mean both "load CRAFT" (every session) and "set up a new
# client" (once), leaving no way to tell which line a flag belongs on. One
# command, one job.

import importlib
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_core_was_loaded = "gpudev_craft.core" in sys.modules
if _core_was_loaded:
    # A pulled repo must take effect in the current SolveIt kernel. Tear down
    # the old manager/router before reloading so no stale forward or transformer
    # survives while the module globals are replaced.
    _old_core = sys.modules["gpudev_craft.core"]
    try:
        if _old_core._exec_mgr is not None:
            _old_core._exec_mgr.shutdown_remote()
        _old_core.ModeRouter._detach()
    except Exception:
        pass
    if "gpudev_craft.client_setup" in sys.modules:
        importlib.reload(sys.modules["gpudev_craft.client_setup"])
    importlib.reload(_old_core)
    if "gpudev_craft.magics" in sys.modules:
        importlib.reload(sys.modules["gpudev_craft.magics"])

from gpudev_craft.magics import install_core  # noqa: E402

install_core()
