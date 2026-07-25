# CRAFT / SolveIt addons

Optional tools loaded with the **same pattern as core**:

```text
%local
# Always the same loaders (CRAFT optional — auto-detected for %gpu seed):
%run /path/to/gpudev/addons/tidy3.py       # or %run …/tidy3/tidy3.py
%run /path/to/gpudev/addons/plot3.py       # or %run …/plot3/plot3.py
# Only if you need GPU:
%run /path/to/gpudev/CRAFT.py
%gpu
```

gpudev `addons/tidy3.py` / `addons/plot3.py` are thin wrappers that locate the
clone and `%run` the package `tidy3.py` / `plot3.py` loaders.

| Addon | In gpudev | Full code | Provides |
|-------|-----------|-----------|----------|
| **pcviz** | `addons/pcviz.py` | this repo | `%pointcloud` … |
| **mojo** | `addons/mojo.py` | this repo | `%gpum` `%mojo_*` … |
| **sslive** | `addons/sslive.py` (thin) + `addons/sslive` → link | [sslive](https://github.com/rleyvasal/sslive) | `%sslive` … |
| **tidy3** | `addons/tidy3.py` + `addons/tidy3` → link | [tidy3](https://github.com/rleyvasal/tidy3) | `tidy` / `>>` / `%%tidy3_run`; remote seed under `%gpu` |
| **plot3** | `addons/plot3.py` + `addons/plot3` → link | [plot3](https://github.com/rleyvasal/plot3) | `ggplot` / `%plot3`; iframe + red-eye in SolveIt; remote seed under `%gpu` |

## SolveIt: tidy3 + plot3 together

```text
%local
%run ~/gpudev/CRAFT.py
%run ~/gpudev/addons/tidy3.py
%run ~/gpudev/addons/plot3.py
```

You should see:

```text
CRAFT: tidy3 0.x loaded (local) from ...
CRAFT: plot3 0.x loaded (local) from ...
```

Then (still under `%local`, or after `%gpu` once seeded):

```python
from tidy3 import tidy, filter, select, col   # often already in user_ns
# plot3 names (ggplot, aes, geom_point, …) are injected by the addon

tidy(cars)
>> filter(col("hp") < 250)
>> select("wt", "mpg", "cyl")
>> ggplot(aes(x="wt", y="mpg", colour="cyl"))
+ geom_point(size=5)
+ labs(title="Weight vs MPG")
+ theme_light()
```

In **SolveIt**, figures render as an **iframe** (WebGL). The cell is marked
`skipped=1` (red eye) so large HTML does not enter the LLM context.

Under **`%gpu`**:

- tidy3 / plot3 source is **seeded to the remote** automatically
- `%plot3` stays **host-local** (viewer + hide-from-AI on the dialog machine)
- After `%restart_kernel`: `seed_tidy3_remote(force=True)` /
  `seed_plot3_remote(force=True)` if needed

## Linking separate repos

```bash
cd /path/to/gpudev/addons
ln -sfn /path/to/tidy3 tidy3
ln -sfn /path/to/plot3 plot3
ln -sfn /path/to/sslive sslive
```

Side-by-side layout also works without symlinks when clones sit next to `gpudev/`
(`../tidy3`, `../plot3`).

### Standalone (no gpudev)

```bash
pip install -e /path/to/tidy3
pip install -e /path/to/plot3
```

```text
%load_ext tidy3.jupyter
%load_ext plot3
# or:
%run /path/to/plot3/load.py
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
