# CRAFT dialog — load sequence

```text
gpudev/
  CRAFT.py
  gpudev_craft/
  addons/
    pcviz.py, mojo.py          # in-tree
    sslive.py + sslive/ → …    # thin loader + linked separate repo
    tidy3.py  + tidy3/  → …    # thin loader + linked separate repo
```

## Always (core)

```text
%local
%run /app/data/gpudevd/gpudev/CRAFT.py
%gpu
```

## Optional addons (all under `%local`, same as core)

```text
%local
%run /app/data/gpudevd/gpudev/addons/pcviz.py
%run /app/data/gpudevd/gpudev/addons/mojo.py
%run /app/data/gpudevd/gpudev/addons/sslive.py
%run /app/data/gpudevd/gpudev/addons/tidy3.py
%gpu
%sslive
```

## Magics

| After load | Magics |
|------------|--------|
| core | `%gpu` `%local` `%kernel_status` `%restart_kernel` |
| pcviz | `%pointcloud` `%pointcloud_var` `%pointcloud_plotly` |
| mojo | `%gpum` `%mojo_*` `%bench` |
| sslive | `%sslive` `%sslive_export` |
| tidy3 | `tidy` / `>>` verbs, `%%tidy3_run`, `%tidy3_pipes` |

## Link separate repos

Side-by-side under e.g. `/app/data/gpudevd/`:

```bash
cd /app/data/gpudevd/gpudev/addons
ln -sfn /app/data/gpudevd/sslive sslive
ln -sfn /app/data/gpudevd/tidy3 tidy3
```

Or git submodules (see `addons/README.md`).

**plot3** (own repo only):

```text
%run /app/data/gpudevd/plot3/plot3.py
```
