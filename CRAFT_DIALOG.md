# CRAFT dialog cell (copy into SolveIt)

Keep this cell **short**. Full source lives on disk under `gpudev/gpudev_craft/`.

```python
# %local first if you were on %gpu
%run /path/to/gpudev/CRAFT.py
%gpu
```

Or explicit:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path("/path/to/gpudev").resolve()))

from gpudev_craft.magics import install_core, install_pcviz, install_sslive, install_mojo

install_core()
# install_pcviz()
# install_sslive()
# install_mojo()

# %gpu
```

## Capabilities (for the assistant)

| Always (core) | Optional addon |
|---------------|----------------|
| `%gpu` `%local` `%kernel_status` `%restart_kernel` | **pcviz:** `%pointcloud` `%pointcloud_var` `%pointcloud_plotly` |
| `remote_run_(code)` `register_local_magic` | **sslive:** `%slive` `%slive_export` |
| Mojo: `%gpum` `%mojo_*` `%bench` (in core for now) | — |

After a stable load, mark this cell **skipped** (`skipped=1`) so it stays out of LLM context.
