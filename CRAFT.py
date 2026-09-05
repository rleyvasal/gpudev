# CRAFT dialog loader — keep this cell short (LLM context budget).
# Core: gpudev_craft/   Addons: addons/ (pcviz, mojo, sslive)
#
#   %local
#   %run /path/to/gpudev/CRAFT.py
#   %run /path/to/gpudev/addons/pcviz.py    # optional
#   %run /path/to/gpudev/addons/mojo.py     # optional
#   %run /path/to/gpudev/addons/sslive.py   # optional (separate repo via link)
#   %gpu solveite
#   %sslive
#
# First-time setup can ride the same line, because `%run script.py args` fills
# sys.argv and this file is already the %run entry point:
#
#   %run /path/to/gpudev/CRAFT.py alice --domain example.com
#
# That loads the magics AND runs %gpu_setup, so a new client needs one cell
# rather than two. Keeping the keygen here rather than in client-bootstrap.sh
# avoids a second implementation of tested Python in shell.

import importlib
import shlex
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


def _setup_args():
    """Setup arguments passed via ``%run CRAFT.py <name> [flags]``, if any.

    Only trust sys.argv when argv[0] is this file: ``%run`` sets argv to
    ``[script, *args]`` for the duration, but imported or exec'd another way
    the process argv belongs to the kernel launcher (``-f …kernel.json``), and
    treating that as a client name would be a confusing failure.
    """
    argv = sys.argv
    if len(argv) < 2:
        return None
    if Path(argv[0]).name != Path(__file__).name:
        return None
    return " ".join(shlex.quote(a) for a in argv[1:])


_args = _setup_args()
if _args:
    from gpudev_craft import core  # noqa: E402

    core.gpu_setup(_args)
