# CRAFT + sslive: how to start

## Mental model

| Piece | Repo / path | Essential? |
|-------|-------------|------------|
| **CRAFT / gpudev** | `gpudev/` | Yes — GPU connection |
| **sslive** | **separate** repo (e.g. `sslive/`) | No — slides addon |
| **pcviz** | lives next to gpudev (`pcviz.py`) | No — point clouds |

sslive can be used **alone** (just `%run sslive.py`) or as an **addon** after CRAFT.

---

## A) CRAFT only (GPU Python)

```text
%local
%run /app/data/gpudevd/gpudev/CRAFT.py
%gpu
```

---

## B) CRAFT + sslive (slides as addon) — **correct sequence**

```text
%local
%run /app/data/gpudevd/gpudev/CRAFT.py
%gpu

# Load sslive on the HOST (not the remote GPU kernel):
%load_sslive /app/data/sslive/sslive.py

# Or under %local:
# %local
# install_sslive("/app/data/sslive/sslive.py")

%sslive
```

### Why your error happened

```text
install_sslive()
NameError: name 'install_sslive' is not defined
```

1. Under **`%gpu`**, a plain Python call is sent to the **remote** kernel — that machine never imported CRAFT’s installers.  
2. Use **`%load_sslive`** (host-local magic) **or** `%local` then `install_sslive(...)`.

---

## C) sslive alone (no CRAFT / no GPU)

```text
%local
%run /app/data/sslive/sslive.py
%sslive
```

Slide **▶ Run** still needs a GPU kernel if you use CRAFT’s remote execution; for static decks / export-only you may only need host + last outputs.

---

## D) Full stack (CRAFT + pcviz + sslive)

```text
%local
%run /app/data/gpudevd/gpudev/CRAFT.py
%gpu
%load_pcviz
%load_sslive /app/data/sslive/sslive.py
%sslive
```

---

## Paths on your machine (from your screenshot)

| What | Typical path |
|------|----------------|
| CRAFT loader | `/app/data/gpudevd/gpudev/CRAFT.py` |
| sslive (separate) | `/app/data/sslive/sslive.py` (adjust if different) |

If `install_sslive()` can’t find the file, always pass the path explicitly.

---

## After load (all magics use the **sslive** prefix)

| Magic | Purpose |
|-------|---------|
| `%sslive` | Open live deck |
| `%sslive_export talk.html` | Portable HTML (host-local) |
| `%pointcloud_plotly …` | Portable lidar (pcviz) for export |
