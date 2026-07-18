# plot3 addon entry — separate repo, linked under addons/plot3/
#
#   %local
#   %run /path/to/gpudev/addons/plot3.py
#   %plot3 df x=a y=b color=c
#
# Or point at a clone elsewhere:
#   %run /path/to/plot3/plot3.py

import runpy
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_CANDIDATES = [
    _HERE / "plot3" / "plot3.py",  # symlink or submodule: addons/plot3/
    _HERE.parent.parent / "plot3" / "plot3.py",
    Path("/app/data/plot3/plot3.py"),
    Path.home() / "plot3" / "plot3.py",
]

_target = next((p for p in _CANDIDATES if p.is_file()), None)
if _target is None:
    raise FileNotFoundError(
        "plot3.py not found. Clone https://github.com/rleyvasal/plot3 and either:\n"
        "  ln -s ../../plot3 " + str(_HERE / "plot3") + "\n"
        "or:\n"
        "  %run /path/to/plot3/plot3.py"
    )

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
    runpy.run_path(str(_target), init_globals=ns, run_name="plot3")
else:
    runpy.run_path(str(_target), run_name="plot3")

print(f"CRAFT: plot3 loaded from {_target}")
print("  %plot3   ggplot(df, aes(...)) + geom_point()/geom_line()   read_bin()")
