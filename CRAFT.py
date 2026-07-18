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

# ── optional addons (uncomment when needed; host-only — use %local if needed) ─
# install_pcviz()
# install_sslive()                                 # auto-finds sibling sslive/
# install_sslive("/app/data/sslive/sslive.py")     # explicit path (separate repo)
# install_mojo()
#
# Or after %gpu, use local magics (stay on host):
#   %load_pcviz
#   %load_sslive
#   %load_sslive /app/data/sslive/sslive.py
