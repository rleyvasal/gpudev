# CRAFT dialog loader — keep this cell short (LLM context budget).
# Implementation: gpudev_craft/  |  optional addons: pcviz, sslive, mojo
#
# Usage in SolveIt:
#   %local
#   %run /path/to/gpudev/CRAFT.py
#   %gpu

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gpudev_craft.magics import (  # noqa: E402
    install_core,
    install_mojo,
    install_pcviz,
    install_sslive,
)

# ── always: GPU connection spine ─────────────────────────────────────────────
install_core()

# ── optional addons (uncomment when needed) ───────────────────────────────────
# install_pcviz()                          # %pointcloud / %pointcloud_plotly
# install_sslive()                         # %slive / %slive_export
# install_sslive("/path/to/sslive/sslive.py")  # if not next to gpudev/
# install_mojo()                           # re-print Mojo help (already in core)
