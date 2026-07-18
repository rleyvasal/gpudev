# sslive addon entry — separate repo, linked under addons/sslive/
#
#   %local
#   %run /path/to/gpudev/addons/sslive.py
#   %gpu
#   %sslive
#
# Or point at a clone elsewhere:
#   %run /path/to/sslive/sslive.py

import runpy
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_CANDIDATES = [
    _HERE / "sslive" / "sslive.py",  # symlink or submodule: addons/sslive/
    _HERE.parent.parent / "sslive" / "sslive.py",
    Path("/app/data/sslive/sslive.py"),
    Path.home() / "sslive" / "sslive.py",
]

_target = next((p for p in _CANDIDATES if p.is_file()), None)
if _target is None:
    raise FileNotFoundError(
        "sslive.py not found. Clone the sslive repo and either:\n"
        "  ln -s /path/to/sslive " + str(_HERE / "sslive") + "\n"
        "or:\n"
        "  %run /path/to/sslive/sslive.py"
    )

# Ensure package parent on path if needed
root = str(_target.parent)
if root not in sys.path:
    sys.path.insert(0, root)

try:
    from IPython import get_ipython

    ip = get_ipython()
    ns = ip.user_ns if ip is not None else None
except Exception:
    ns = None

if ns is not None:
    runpy.run_path(str(_target), init_globals=ns, run_name="sslive")
else:
    runpy.run_path(str(_target), run_name="sslive")

print(f"CRAFT: sslive loaded from {_target}")
print("  %sslive  %sslive_export")
