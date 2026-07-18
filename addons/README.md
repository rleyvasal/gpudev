# CRAFT addons

Optional tools loaded with the **same pattern as core**:

```text
%local
%run /path/to/gpudev/CRAFT.py              # core (required first for GPU)
%run /path/to/gpudev/addons/pcviz.py       # point clouds (in-tree)
%run /path/to/gpudev/addons/mojo.py        # Mojo (in-tree)
%run /path/to/gpudev/addons/sslive.py      # slides → linked sslive repo
%run /path/to/gpudev/addons/tidy3.py       # dplyr prep → linked tidy3 repo
%gpu                                       # then cells run on remote
```

| Addon | In gpudev | Full code | Provides |
|-------|-----------|-----------|----------|
| **pcviz** | `addons/pcviz.py` | this repo | `%pointcloud` … |
| **mojo** | `addons/mojo.py` | this repo | `%gpum` `%mojo_*` … |
| **sslive** | `addons/sslive.py` (thin) + `addons/sslive` → link | [sslive repo](https://github.com/rleyvasal/sslive) | `%sslive` … |
| **tidy3** | `addons/tidy3.py` (thin) + `addons/tidy3` → link | [tidy3](https://github.com/rleyvasal/tidy3) | `tidy` / `>>` / `%%tidy3_run` |

**plot3** is **not** under gpudev addons. It is only [rleyvasal/plot3](https://github.com/rleyvasal/plot3):

```text
%run /path/to/plot3/plot3.py
```

## Linking separate repos (recommended)

gpudev does **not** vendor full addon code for external projects. Keep a thin
`*.py` loader in `addons/` and point a **symlink** (or git submodule) at a
clone of the real repo.

### Side-by-side clones (simple)

```text
/app/data/gpudevd/
  gpudev/          # this repo
  tidy3/           # https://github.com/rleyvasal/tidy3
  sslive/          # https://github.com/rleyvasal/sslive
  plot3/           # https://github.com/rleyvasal/plot3  (load directly)
```

```bash
cd /path/to/gpudev/addons
ln -sfn ../../tidy3 tidy3     # or absolute path to your tidy3 clone
ln -sfn ../../sslive sslive
```

Loaders also search sibling directories without a symlink (e.g. `../tidy3`
next to `gpudev/`).

### Git submodules (optional, clone-friendly)

```bash
cd /path/to/gpudev
git submodule add https://github.com/rleyvasal/tidy3.git addons/tidy3
git submodule add https://github.com/rleyvasal/sslive.git addons/sslive
# later: git clone --recurse-submodules <gpudev-url>
```

Symlinks work better when every machine already has sibling checkouts;
submodules work better when you want one `git clone` to pull everything.

### Standalone (no gpudev)

```bash
pip install -e /path/to/tidy3
# notebook:
%load_ext tidy3.jupyter
```

```text
%run /path/to/plot3/plot3.py
%run /path/to/sslive/sslive.py
```

## Addon contract

An addon must register its **entire public surface itself** via `get_ipython()`:

- magics through the magics manager, plus `register_local_magic('%name')` so
  they run on the host under `%gpu`;
- any names meant for direct cell use written explicitly into `user_ns`.

Never rely on `%run` leaking module globals into the dialog namespace.

## Order

1. Always load **CRAFT core** first if you need `%gpu` / `remote_run_`.  
2. Then load any addons under **`%local`**.  
3. Then **`%gpu`** for remote Python cells.
