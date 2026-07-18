# CRAFT dialog — load sequence

```text
gpudev/
  CRAFT.py                 # tiny core loader
  gpudev_craft/            # implementation package
  addons/
    pcviz.py               # point clouds
    mojo.py                # Mojo language
    sslive.py              # wrapper → linked sslive repo
    sslive/ → …/sslive     # symlink or git submodule (separate repo)
```

## Always (core)

```text
%local
%run /app/data/gpudevd/gpudev/CRAFT.py
%gpu
```

## Optional addons (same pattern)

```text
%local
%run /app/data/gpudevd/gpudev/addons/pcviz.py
%run /app/data/gpudevd/gpudev/addons/mojo.py
%run /app/data/gpudevd/gpudev/addons/sslive.py
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
| plot3 | `%plot3` + `ggplot(df, aes(...))` grammar (separate repo, linked) |

## sslive as separate repo

```bash
cd /path/to/gpudev/addons
ln -s /path/to/sslive sslive
# or: git submodule add <url> addons/sslive
```

If unlinked, run the real file:

```text
%run /path/to/sslive/sslive.py
```
