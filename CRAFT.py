# CRAFT dialog loader — keep this cell short (LLM context budget).
# Core: gpudev_craft/   Addons: addons/ (pcviz, mojo, sslive)
#
#   %local
#   %run /path/to/gpudev/CRAFT.py
#   %run /path/to/gpudev/addons/pcviz.py    # optional
#   %run /path/to/gpudev/addons/mojo.py     # optional
#   %run /path/to/gpudev/addons/sslive.py   # optional (separate repo via link)
#   %gpu
#   %sslive

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gpudev_craft.magics import install_core  # noqa: E402

install_core()
