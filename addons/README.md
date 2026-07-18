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

The pcviz magics mark their own cell hidden-from-AI (`skipped=1`, red eye)
after rendering — viewer HTML, especially plotly's embedded point data, can be
megabytes of LLM context. Pass `hide=0` to keep a cell visible to the AI.

## Addon contract

An addon must register its **entire public surface itself** via `get_ipython()`:

- magics through the magics manager, plus `register_local_magic('%name')` so
  they run on the host under `%gpu`;
- any names meant for direct cell use written explicitly into `user_ns`
  (see mojo's handle injection, sslive's `_inject_public_api_into_user_ns`,
  pcviz's `_publish_srv`).

Never rely on `%run` leaking module globals into the dialog namespace: the
`install_*()` loaders run addons through `runpy` and discard the module
namespace, so anything not explicitly registered does not exist there — and
even under `%run`, leaked globals are a stale snapshot from load time, not
live state.

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
