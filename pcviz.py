# Back-compat shim — real file is addons/pcviz.py
# Prefer: %local then %run /path/to/gpudev/addons/pcviz.py
import runpy
from pathlib import Path

runpy.run_path(
    str(Path(__file__).resolve().parent / "addons" / "pcviz.py"),
    init_globals=globals(),
    run_name="pcviz",
)
