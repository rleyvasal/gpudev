# CRAFT dialog — load sequence

## First-time client setup (three steps)

1. In SolveIt, one cell — fetch, load, set up. One job per line:

   ```text
   !curl -fsSL https://raw.githubusercontent.com/rleyvasal/gpudev/main/client-bootstrap.sh -o /tmp/gpudev-bootstrap.sh && sh /tmp/gpudev-bootstrap.sh
   %run ~/.gpudev-client/CRAFT.py
   %gpudev_setup solveite --domain <domain>
   ```
2. Forward the single `gpudev client add solveite --key "..."` line it prints;
   the administrator runs it.
3. `%gpudev solveite`.

**`%gpudev_setup` runs once, ever.** `%run` only loads CRAFT, which is why the
returning-notebook cell below is `%run` + `%gpudev` and nothing more.

The bootstrap fetches only the ten files a client runs (~192 KB), and re-running
it is the update path — a 40-byte request when already current. `--verify`
re-hashes an install, `--force` repairs one, `GPUDEV_REF` pins to a commit.

Setup generates the private key locally and writes `~/.ssh/config`, recording a
new server fingerprint automatically. No JSON config, terminal login, or manual
first-connection confirmation is needed. Without `--domain` the key is still
generated; the stanza waits for `%gpudev solveite --hostname <host>`.

For a development client, add `--variant cuda-dev` to the `%gpudev_setup` line —
it is a setup-time choice. The host builds the opt-in image automatically on its
first request. An admin-first `gpudev client invite solveite` remains available
as an optional shortcut.

```text
gpudev/
  CRAFT.py
  gpudev_craft/
  addons/
    pcviz.py, mojo.py          # in-tree
    sslive.py + sslive/ → …    # thin loader + linked separate repo
    tidy3.py  + tidy3/  → …    # thin loader + linked separate repo
```

## Returning notebook (core)

```text
%local
%run ~/.gpudev-client/CRAFT.py
%gpudev solveite
```

## Optional addons (all under `%local`, same as core)

```text
%local
%run ~/.gpudev-client/addons/pcviz.py
%run ~/.gpudev-client/addons/mojo.py
%run ~/.gpudev-client/addons/sslive.py
%run ~/.gpudev-client/addons/tidy3.py
%gpudev solveite
%sslive
```

## Magics

| After load | Magics |
|------------|--------|
| core | `%gpudev <client>` `%gpudev_setup` `%local` `%kernel_status` `%restart_kernel` |
| pcviz | `%pointcloud` `%pointcloud_var` `%pointcloud_plotly` |
| mojo | `%gpum` `%mojo_*` `%bench` |
| sslive | `%sslive` `%sslive_export` |
| tidy3 | `tidy` / `>>` verbs, `%%tidy3_run`, `%tidy3_pipes` |

## Link separate repos

Side-by-side under e.g. `/app/data/gpudevd/`:

```bash
cd ~/.gpudev-client/addons
ln -sfn /app/data/gpudevd/sslive sslive
ln -sfn /app/data/gpudevd/tidy3 tidy3
```

Or git submodules (see `addons/README.md`).

**plot3** (own repo only):

```text
%run /app/data/gpudevd/plot3/plot3.py
```
