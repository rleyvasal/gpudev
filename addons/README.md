# CRAFT addons

Optional tools loaded with the **same pattern as core**:

```text
%local
%run /path/to/gpudev/CRAFT.py              # core (required first for GPU)
%run /path/to/gpudev/addons/pcviz.py       # point clouds
%run /path/to/gpudev/addons/mojo.py        # Mojo language
%run /path/to/gpudev/addons/sslive.py      # slides (wraps separate sslive repo)
%gpu
```

| Addon | Path | Provides |
|-------|------|----------|
| **pcviz** | `addons/pcviz.py` | `%pointcloud` `%pointcloud_var` `%pointcloud_plotly` |
| **mojo** | `addons/mojo.py` | `%gpum` `%mojo_*` `%bench` |
| **sslive** | `addons/sslive.py` → linked repo | `%sslive` `%sslive_export` |

## Separate repos

**sslive** stays its own repository. In this tree it is linked for convenience:

```text
addons/sslive -> /path/to/sslive   # git submodule or symlink
```

If the link is missing, either:

```bash
cd /path/to/gpudev/addons
ln -s /path/to/sslive sslive
# or: git submodule add <sslive-url> addons/sslive
```

or run the real file directly:

```text
%run /path/to/sslive/sslive.py
```

**pcviz** currently lives in this repo under `addons/pcviz.py` (can move to its own repo later the same way as sslive).

## Order

1. Always load **CRAFT core** first if you need `%gpu` / `remote_run_`.  
2. Then load any addons under **`%local`**.  
3. Then **`%gpu`** for remote Python cells.
