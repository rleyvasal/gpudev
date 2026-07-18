# pcviz.py — reusable point-cloud viewer for SolveIt (FastHTML + three.js).
#
# The viewer server runs in the LOCAL SolveIt kernel.
# It can still be called while %gpu mode is active because %pointcloud,
# %pointcloud_var, and %pointcloud_plotly are registered as local passthrough
# magics in CRAFT.
#
# Remote .bin paths stream over SSH (not stored on SolveIt). Snapshots from
# %pointcloud_var write a temp file on the GPU box, stream it, then delete it.
#
# %pointcloud          → interactive Three.js (live SolveIt only; not portable)
# %pointcloud_plotly   → Plotly Scatter3d (live + portable sslive export)

from fasthtml.common import *
from fasthtml.jupyter import JupyUvi, HTMX
from starlette.responses import Response, StreamingResponse
from fastcore.utils import partial
import numpy as np
import subprocess
import shlex
import itertools
import json
import re
import socket
import uuid


# Prefer 8000 (SolveIt convention); if busy, try the next free ports — never kill -9.
_PREF_PORT = 8000
_PORT_RANGE = 50
_PORT = None
_CLOUDS = {}
_CURRENT = None
_ctr = itertools.count(1)
_app = _srv = _preview = _scene_lf = None

# Default interactive density for large clouds (override with sub=1 or max_points=0).
_DEFAULT_MAX_POINTS = 500_000
# Lower default for Plotly portable export (browser + HTML size).
_DEFAULT_PLOTLY_MAX_POINTS = 80_000


_HDRS = (
    Script(
        '{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.165.0/build/three.module.js",'
        '"three/addons/":"https://cdn.jsdelivr.net/npm/three@0.165.0/examples/jsm/"}}',
        type="importmap",
    ),
)


def _craft_keep_local(magic):
    """Keep `magic` local under %gpu. Durable across CRAFT re-runs when CRAFT supports it."""
    try:
        ns = get_ipython().user_ns
    except Exception:
        return

    reg = ns.get("register_local_magic")
    if callable(reg):
        reg(magic)
        return

    # Fallback: older CRAFT without register_local_magic
    be = ns.get("PY_BACKEND")
    if be is not None and hasattr(be, "_LOCAL") and magic not in be._LOCAL:
        be._LOCAL = tuple(be._LOCAL) + (magic,)


def _ssh_cfg():
    "Find CRAFT's SSH config whether this file was imported or run_cell'd into the kernel ns."
    nss = [globals()]

    try:
        nss.append(get_ipython().user_ns)
    except Exception:
        pass

    for ns in nss:
        if ns.get("SSH_HOST"):
            return ns["SSH_HOST"], ns.get("SSH_OPTS", "")

    raise RuntimeError("SSH_HOST not found — load CRAFT and run %gpu first.")


def _slug(path):
    """URL-safe short name (avoid '+' etc. which break /points/{name}.bin routes)."""
    import hashlib

    raw = str(path)
    base = raw.rsplit("/", 1)[-1]
    if base.endswith(".pcd.bin"):
        base = base[: -len(".pcd.bin")]
    elif base.endswith(".bin"):
        base = base[:-4]
    # Keep alnum / . _ - only — '+' and spaces break HTMX/fetch URLs
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "cloud"
    # Short hash keeps names unique after sanitizing
    tag = hashlib.md5(raw.encode("utf-8", "replace")).hexdigest()[:6]
    return f"{base[:60]}_{tag}"


def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_port(preferred=_PREF_PORT, span=_PORT_RANGE):
    for port in range(preferred, preferred + span):
        if _port_free(port):
            return port
    raise RuntimeError(
        f"No free TCP port in {preferred}..{preferred + span - 1} for pcviz"
    )


def _ssh_run(remote_cmd, check=False):
    """Run a remote command via CRAFT's SSH hop (argv list, no local shell)."""
    host, opts = _ssh_cfg()
    return subprocess.run(
        ["ssh", *shlex.split(opts), host, remote_cmd],
        check=check,
        capture_output=True,
        text=True,
    )


def _rm_remote(path):
    """Best-effort delete of a remote path (temp snapshots)."""
    try:
        _ssh_run("rm -f -- " + shlex.quote(path), check=False)
    except Exception:
        pass


def _subsample_params(sub, max_points, n_points):
    """Return effective row step for ~max_points (or explicit sub)."""
    sub = max(1, int(sub or 1))
    if max_points is None:
        max_points = _DEFAULT_MAX_POINTS
    max_points = int(max_points)
    if max_points > 0 and n_points is not None and n_points > max_points * sub:
        # Extra thinning so N/sub_eff <= max_points
        need = max(1, (n_points + max_points - 1) // max_points)
        sub = max(sub, need)
    return sub


def _remote_thin_cmd(src: str, *, stride: int, sub: int) -> str:
    """SSH remote command: stream thinned float32 rows (or full file if sub<=1)."""
    sub = max(1, int(sub or 1))
    stride = max(3, int(stride or 3))
    if sub <= 1:
        return "cat -- " + shlex.quote(src)
    # Same recipe the viewer has used successfully for large remote clouds
    return (
        "python3 -c "
        + shlex.quote(
            "import sys,numpy as np;"
            f"a=np.fromfile({src!r},dtype=np.float32);"
            f"s={stride};"
            "n=a.size//s;"
            f"a=a[:n*s].reshape(n,s)[::{sub}];"
            "sys.stdout.buffer.write(np.ascontiguousarray(a,dtype=np.float32).tobytes())"
        )
    )


def _ssh_collect_bytes(remote_cmd: str) -> bytes:
    """Run remote_cmd over CRAFT SSH; return full stdout bytes or raise with stderr."""
    host, opts = _ssh_cfg()
    opt_list = shlex.split(opts) if opts else []
    cmd = ["ssh", *opt_list, host, remote_cmd]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        # Some ssh failures only print to stdout
        if not err and proc.stdout:
            try:
                err = proc.stdout[:400].decode("utf-8", "replace").strip()
            except Exception:
                err = f"({len(proc.stdout)} bytes on stdout)"
        raise RuntimeError(
            f"ssh to {host!r} failed (rc={proc.returncode}): {err or 'no stderr'} "
            f"[cmd starts with: {remote_cmd[:80]!r}…]"
        )
    return proc.stdout or b""


def _data(name: str):
    "Serve a cloud's raw float32 bytes; remote clouds stream over ssh, never stored."
    c = _CLOUDS.get(name)

    if c is None:
        return Response(f"unknown cloud {name!r}", status_code=404)

    if c["kind"] == "remote":
        host, opts = _ssh_cfg()
        src = c["src"]
        sub = max(1, int(c.get("sub") or 1))
        remote_cmd = _remote_thin_cmd(src, stride=int(c["stride"]), sub=sub)
        opt_list = shlex.split(opts) if opts else []
        cmd = ["ssh", *opt_list, host, remote_cmd]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def gen():
            try:
                for ch in iter(lambda: proc.stdout.read(1 << 16), b""):
                    yield ch
            finally:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
                proc.wait()
                if c.get("cleanup"):
                    _rm_remote(src)

        return StreamingResponse(gen(), media_type="application/octet-stream")

    return Response(c["src"], media_type="application/octet-stream")


def _scene():
    "The three.js page for the currently-selected cloud."
    if not _CURRENT or _CURRENT not in _CLOUDS:
        return Div(
            Style("body{margin:0;background:#0b1020;color:#9ab;font:14px system-ui}"),
            P("No cloud selected — re-run %pointcloud …"),
        )
    c = _CLOUDS[_CURRENT]

    return Div(
        Style("body{margin:0;overflow:hidden;background:#0b1020;color:#9ab}"),
        Div(
            id="pcviz-status",
            style="position:absolute;z-index:2;left:12px;top:12px;font:13px/1.4 system-ui,sans-serif",
        )("Loading point cloud…"),
        Div(
            id="scene",
            style="width:100vw;height:100vh;margin:0",
            **{
                "data-url": f"points/{_CURRENT}.bin",
                "data-stride": str(c["stride"]),
                "data-size": str(c["size"]),
                "data-color": c["color"],
                "data-icol": str(c["icol"]),
            },
        ),
        Script(_VIEWER_JS, type="module"),
    )


def stop_server():
    """Stop the local Three.js viewer process (safe to call multiple times)."""
    global _app, _srv, _preview, _scene_lf, _PORT
    srv = _srv
    _srv = None
    _app = None
    _preview = None
    _scene_lf = None
    _PORT = None
    _publish_srv(None)
    if srv is None:
        return
    try:
        if hasattr(srv, "stop") and callable(srv.stop):
            srv.stop()
        elif getattr(srv, "server", None) is not None:
            # uvicorn Server under JupyUvi
            try:
                srv.server.should_exit = True
            except Exception:
                pass
            if hasattr(srv.server, "force_exit"):
                try:
                    srv.server.force_exit = True
                except Exception:
                    pass
    except Exception:
        pass


def restart_viewer(height="600px"):
    """Stop any viewer and start fresh (use after re-%run pcviz if HTMX 500s)."""
    stop_server()
    _ensure_server(height=height)


def _publish_srv(handle):
    """Expose the live server handle in user_ns so a re-load (any load path) can stop it."""
    try:
        ip = get_ipython()
        if ip is not None and isinstance(getattr(ip, "user_ns", None), dict):
            ip.user_ns["_pcviz_srv"] = handle
    except Exception:
        pass


def _ensure_server(height="600px"):
    "Start the singleton FastHTML server once; reused by every point_cloud() call."
    global _app, _srv, _preview, _scene_lf, _PORT

    if _srv is not None:
        # Keep existing server, but refresh HTMX height if needed
        if height:
            try:
                _preview = partial(HTMX, app=_app, host=None, port=_PORT, height=height)
            except Exception:
                pass
        return

    _PORT = _pick_port(_PREF_PORT, _PORT_RANGE)

    _app = FastHTML(hdrs=_HDRS, session_cookie="fh_template_session")
    _app.route("/points/{name}.bin")(_data)
    _scene_lf = _app.route("/pcviz_scene")(_scene)
    _srv = JupyUvi(_app, port=_PORT)
    _publish_srv(_srv)
    # Pass port explicitly so HTMX/proxy hits this server (not a stale 8000).
    _preview = partial(HTMX, app=_app, host=None, port=_PORT, height=height)


def point_cloud(
    src,
    *,
    stride=5,
    size=0.06,
    color="height",
    icol=3,
    remote=True,
    name=None,
    sub=1,
    max_points=None,
    cleanup=False,
    height="600px",
):
    """Visualise a point cloud and return the inline viewer.

    src        : remote path (default), local path with remote=False, or numpy (N,K).
    stride     : floats per point — nuScenes=5, KITTI=4, xyz=3.
    color      : "height", "intensity", or "mono".
    size       : point size in world units.
    sub        : keep every Nth point (1 = all). Applied on GPU for remote files.
    max_points : cap after sub (default 500_000). Set 0 to disable the cap.
    cleanup    : if True, delete remote src after first successful stream (temps).
    height     : iframe height for the SolveIt preview.
    """
    _ensure_server(height=height)

    global _CURRENT

    sub = max(1, int(sub or 1))
    if max_points is None:
        max_points = _DEFAULT_MAX_POINTS

    if isinstance(src, np.ndarray):
        arr = np.ascontiguousarray(src, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[1] < 3:
            raise ValueError(f"expected (N,K>=3) array, got shape {arr.shape!r}")
        stride = int(arr.shape[1])
        sub_eff = _subsample_params(sub, max_points, arr.shape[0])
        if sub_eff > 1:
            arr = np.ascontiguousarray(arr[::sub_eff])
        kind, data, name = "bytes", arr.tobytes(), name or f"array_{next(_ctr)}"
        sub = 1  # already applied locally

    elif remote:
        kind, data, name = "remote", src, name or _slug(src)
        # Optional max_points for remote files: estimate n via file size when possible.
        if max_points and max_points > 0 and sub == 1:
            try:
                out = _ssh_run(
                    "stat -c%s -- " + shlex.quote(src) + " 2>/dev/null || "
                    "stat -f%z -- " + shlex.quote(src),
                    check=False,
                )
                nbytes = int((out.stdout or "0").strip() or "0")
                n_pts = nbytes // (4 * max(1, int(stride)))
                sub = _subsample_params(1, max_points, n_pts)
            except Exception:
                pass

    else:
        with open(src, "rb") as f:
            raw = f.read()
        arr = np.frombuffer(raw, dtype=np.float32)
        n = arr.size // max(1, int(stride))
        arr = arr[: n * int(stride)].reshape(n, int(stride))
        sub_eff = _subsample_params(sub, max_points, n)
        if sub_eff > 1:
            arr = np.ascontiguousarray(arr[::sub_eff])
        kind, data, name = "bytes", arr.tobytes(), name or _slug(src)
        sub = 1

    _CLOUDS[name] = dict(
        kind=kind,
        src=data,
        stride=int(stride),
        size=float(size),
        color=color,
        icol=int(icol),
        sub=int(sub),
        cleanup=bool(cleanup),
    )

    _CURRENT = name
    return _preview(_scene_lf)


def show(name, height="600px"):
    "Re-show an already-registered cloud by name."
    global _CURRENT

    if name not in _CLOUDS:
        raise KeyError(f"{name!r} not registered; choose from {list(_CLOUDS)}")

    _ensure_server(height=height)
    _CURRENT = name
    return _preview(_scene_lf)


def clouds():
    "List registered cloud names."
    return list(_CLOUDS)


def clear_clouds():
    "Drop registered clouds from local memory (does not delete remote data files)."
    global _CURRENT
    # Best-effort cleanup of any temps still marked for delete.
    for c in list(_CLOUDS.values()):
        if c.get("kind") == "remote" and c.get("cleanup"):
            _rm_remote(c["src"])
    _CLOUDS.clear()
    _CURRENT = None
    return []


def _load_points_array(
    src,
    *,
    stride=5,
    sub=1,
    max_points=_DEFAULT_PLOTLY_MAX_POINTS,
    remote=True,
):
    """Load (N, stride) float32 points on the SolveIt host (thin on GPU when remote)."""
    stride = max(3, int(stride or 3))
    sub = max(1, int(sub or 1))
    max_points = int(max_points) if max_points is not None else 0

    if isinstance(src, np.ndarray):
        arr = np.ascontiguousarray(src, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[1] < 3:
            raise ValueError(f"expected (N,K>=3) array, got shape {arr.shape!r}")
        stride = int(arr.shape[1])
        sub_eff = _subsample_params(sub, max_points if max_points > 0 else 0, arr.shape[0])
        if sub_eff > 1:
            arr = np.ascontiguousarray(arr[::sub_eff])
        return arr

    if remote:
        # Estimate N from remote file size (same idea as point_cloud) → choose sub
        n_pts = None
        try:
            out = _ssh_run(
                "stat -c%s -- " + shlex.quote(str(src)) + " 2>/dev/null || "
                "stat -f%z -- " + shlex.quote(str(src)),
                check=False,
            )
            nbytes = int((out.stdout or "0").strip() or "0")
            if nbytes > 0:
                n_pts = nbytes // (4 * stride)
        except Exception:
            n_pts = None
        sub_eff = _subsample_params(sub, max_points if max_points > 0 else 0, n_pts)
        raw = _ssh_collect_bytes(
            _remote_thin_cmd(str(src), stride=stride, sub=sub_eff)
        )
        if not raw:
            raise RuntimeError(f"remote load returned empty data for {src!r}")
        arr = np.frombuffer(raw, dtype=np.float32)
        n = arr.size // stride
        if n < 1:
            raise RuntimeError(
                f"no points loaded from {src!r} "
                f"(got {len(raw)} bytes, stride={stride})"
            )
        return arr[: n * stride].reshape(n, stride).copy()

    # Local file on SolveIt host
    with open(src, "rb") as f:
        raw = f.read()
    arr = np.frombuffer(raw, dtype=np.float32)
    n = arr.size // stride
    arr = arr[: n * stride].reshape(n, stride)
    sub_eff = _subsample_params(sub, max_points if max_points > 0 else 0, n)
    if sub_eff > 1:
        arr = np.ascontiguousarray(arr[::sub_eff])
    return arr


def point_cloud_plotly(
    src,
    *,
    stride=5,
    size=1.5,
    color="height",
    icol=3,
    remote=True,
    sub=1,
    max_points=None,
    opacity=0.65,
    height=640,
    title=None,
):
    """Plot a point cloud with Plotly Scatter3d (portable for sslive export).

    Same CRAFT/host local pattern as ``point_cloud`` / ``%pointcloud``, but the
    result is a Plotly figure (``fig.show()``) that sslive can embed in export
    HTML — unlike the Three.js viewer, which needs a live localhost server.

    src        : remote path (default), local path with remote=False, or numpy (N,K).
    stride     : floats per point — nuScenes=5, KITTI=4, xyz=3.
    color      : "height", "intensity", or "mono".
    size       : Plotly marker size (screen units; ~1–3 typical).
    max_points : cap after thinning (default 80_000 for portable HTML).
    remote     : stream/thin from GPU box via SSH (default True).
    """
    try:
        import plotly.graph_objects as go
    except ImportError as e:
        raise RuntimeError(
            "plotly is required on the SolveIt host for %pointcloud_plotly — "
            "install with: pip install plotly"
        ) from e

    if max_points is None:
        max_points = _DEFAULT_PLOTLY_MAX_POINTS

    arr = _load_points_array(
        src, stride=stride, sub=sub, max_points=max_points, remote=remote
    )
    n0 = arr.shape[0]
    x, y, z = arr[:, 0], arr[:, 1], arr[:, 2]

    marker = dict(size=float(size), opacity=float(opacity))
    cmode = (color or "height").lower()
    if cmode == "height":
        marker["color"] = z
        marker["colorscale"] = "Viridis"
        marker["showscale"] = False
    elif cmode == "intensity" and arr.shape[1] > int(icol):
        marker["color"] = arr[:, int(icol)]
        marker["colorscale"] = "Viridis"
        marker["showscale"] = False
    # mono: leave default marker color

    if isinstance(src, np.ndarray):
        ttl = title or "point cloud"
    else:
        ttl = title or _slug(src)

    h = height
    if isinstance(h, str):
        h = int("".join(ch for ch in h if ch.isdigit()) or "640")
    h = max(240, int(h))

    fig = go.Figure(
        data=[
            go.Scatter3d(
                x=x,
                y=y,
                z=z,
                mode="markers",
                marker=marker,
                name=ttl,
            )
        ]
    )
    fig.update_layout(
        title=ttl,
        height=h,
        margin=dict(l=0, r=0, t=40, b=0),
        scene=dict(
            aspectmode="data",
            xaxis_title="x",
            yaxis_title="y",
            zaxis_title="z",
        ),
        template="plotly_dark",
        paper_bgcolor="#0b1020",
        font=dict(color="#9ab"),
    )
    print(f"pcviz plotly: {n0:,} points (max_points={int(max_points)}) → {ttl}")
    fig.show()
    # Do not return fig — IPython/SolveIt would auto-display it a second time
    return None


try:
    from IPython.core.magic import register_line_magic

    @register_line_magic
    def pointcloud(line):
        # Re-bind if CRAFT was loaded after pcviz (or re-run).
        _craft_keep_local("%pointcloud")
        parts = shlex.split(line)

        if not parts:
            raise ValueError(
                "usage: %pointcloud <path> [stride=N] [color=height|intensity|mono] "
                "[size=F] [icol=N] [remote=0|1] [sub=N] [max_points=N] [name=...]"
            )

        path, kw = parts[0], {}

        for tok in parts[1:]:
            k, _, v = tok.partition("=")

            if k in ("stride", "icol", "sub", "max_points"):
                kw[k] = int(v)
            elif k == "size":
                kw[k] = float(v)
            elif k == "remote":
                kw[k] = v.lower() in ("1", "true", "yes")
            elif k in ("color", "name"):
                kw[k] = v
            else:
                raise ValueError(f"unknown option {tok!r}")

        return point_cloud(path, **kw)

except Exception:
    pass

_craft_keep_local("%pointcloud")


try:
    from IPython.core.magic import register_line_magic

    @register_line_magic
    def pointcloud_plotly(line):
        """Portable LiDAR view via Plotly Scatter3d (works with sslive export).

        Usage:
          %pointcloud_plotly /home/gpudev/.../scan.pcd.bin
          %pointcloud_plotly PATH max_points=60000 stride=5 color=height size=1.5
        """
        _craft_keep_local("%pointcloud_plotly")
        parts = shlex.split(line)

        if not parts:
            raise ValueError(
                "usage: %pointcloud_plotly <path> [stride=N] [color=height|intensity|mono] "
                "[size=F] [icol=N] [remote=0|1] [sub=N] [max_points=N] [opacity=F] "
                "[height=N] [title=...]"
            )

        path, kw = parts[0], {}

        for tok in parts[1:]:
            k, _, v = tok.partition("=")

            if k in ("stride", "icol", "sub", "max_points", "height"):
                kw[k] = int(v)
            elif k in ("size", "opacity"):
                kw[k] = float(v)
            elif k == "remote":
                kw[k] = v.lower() in ("1", "true", "yes")
            elif k in ("color", "title", "name"):
                # name accepted as alias for title
                kw["title" if k == "name" else k] = v
            else:
                raise ValueError(f"unknown option {tok!r}")

        return point_cloud_plotly(path, **kw)

except Exception:
    pass

_craft_keep_local("%pointcloud_plotly")


try:
    from IPython.core.magic import register_line_magic

    @register_line_magic
    def pointcloud_var(line):
        """Snapshot an (N,K>=3) point-cloud variable from the %gpu kernel and view it locally.

        Usage:
          %pointcloud_var pts
          %pointcloud_var pts sub=4 color=intensity size=0.1 name=front max_points=200000
        """
        # Re-bind if CRAFT was loaded after pcviz (or re-run).
        _craft_keep_local("%pointcloud_var")
        ns = get_ipython().user_ns
        pc, rr = ns.get("point_cloud"), ns.get("remote_run_")

        if pc is None or rr is None:
            raise RuntimeError(
                "Load pcviz + CRAFT and run %gpu first "
                "(point_cloud / remote_run_ missing)."
            )

        parts = shlex.split(line)

        if not parts:
            raise ValueError(
                "usage: %pointcloud_var <expr> [sub=N] [max_points=N] [color=...] "
                "[size=F] [icol=N] [name=...]"
            )

        expr = parts[0]
        sub = 1
        max_points = _DEFAULT_MAX_POINTS
        opts = dict(color="height", size=0.06, icol=3, name=None)

        for tok in parts[1:]:
            k, _, v = tok.partition("=")

            if k == "sub":
                sub = max(1, int(v))
            elif k == "max_points":
                max_points = int(v)
            elif k == "size":
                opts["size"] = float(v)
            elif k == "icol":
                opts["icol"] = int(v)
            elif k in ("color", "name"):
                opts[k] = v
            else:
                raise ValueError(f"unknown option: {tok}")

        opts["name"] = opts["name"] or expr

        remote_path = f"/tmp/pcviz_{uuid.uuid4().hex}.bin"
        # Apply sub on the GPU; max_points applied as extra step if needed after shape known.
        code = f"""
import json, numpy as np, os
_obj = eval({expr!r})
if hasattr(_obj, "detach"): _obj = _obj.detach()
if hasattr(_obj, "cpu"):    _obj = _obj.cpu()
if hasattr(_obj, "numpy"):  _obj = _obj.numpy()
_arr = np.asarray(_obj, dtype=np.float32)
if _arr.ndim != 2 or _arr.shape[1] < 3:
    raise ValueError("expected (N,K) with K>=3, got %r" % (_arr.shape,))
_sub = {int(sub)}
_max = {int(max_points)}
if _max > 0 and _arr.shape[0] > _max * _sub:
    _sub = max(_sub, (_arr.shape[0] + _max - 1) // _max)
_arr = np.ascontiguousarray(_arr[::_sub])
_arr.tofile({remote_path!r})
print(json.dumps({{"shape": list(_arr.shape), "path": {remote_path!r}, "sub": _sub}}))
"""

        out = rr(code, max_chars=4000).strip()

        try:
            meta = json.loads(out.splitlines()[-1])
        except Exception:
            _rm_remote(remote_path)
            raise RuntimeError(f"remote snapshot failed:\n{out}")

        try:
            return pc(
                meta["path"],
                remote=True,
                stride=meta["shape"][1],
                sub=1,  # already thinned on GPU
                max_points=0,  # do not thin again when streaming
                cleanup=True,  # delete /tmp/pcviz_*.bin after stream
                **opts,
            )
        except Exception:
            _rm_remote(remote_path)
            raise

except Exception:
    pass

_craft_keep_local("%pointcloud_var")


# On re-%run of this file, stop the previous JupyUvi so the next %pointcloud
# binds /pcviz_scene and /points/* to *this* code (avoids Internal Server Error
# from a stale server still holding port 8000).
try:
    _ip = get_ipython()
    _ns = (_ip.user_ns or {}) if _ip is not None else {}
    # _srv is the pre-contract %run leak from an older loaded copy.
    _old = _ns.get("_pcviz_srv") or _ns.get("_srv")
    if _old is not None and _old is not _srv:
        try:
            if hasattr(_old, "stop") and callable(_old.stop):
                _old.stop()
            elif getattr(_old, "server", None) is not None:
                _old.server.should_exit = True
        except Exception:
            pass
except Exception:
    pass


_VIEWER_JS = """
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const el = document.getElementById('scene');
const status = document.getElementById('pcviz-status');
const URL_ = el.dataset.url;
const STRIDE = +el.dataset.stride;
const SIZE = +el.dataset.size;
const CMODE = el.dataset.color;
const ICOL = +el.dataset.icol;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b1020);

const camera = new THREE.PerspectiveCamera(
  60,
  Math.max(el.clientWidth, 1) / Math.max(el.clientHeight, 1),
  0.1,
  5000
);
camera.up.set(0, 0, 1);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(el.clientWidth, el.clientHeight);
renderer.setPixelRatio(window.devicePixelRatio);
el.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

function setStatus(t) {
  if (status) status.textContent = t || '';
}

let raw;
try {
  setStatus('Loading point cloud…');
  const resp = await fetch(URL_);
  if (!resp.ok) throw new Error('HTTP ' + resp.status);
  const len = +(resp.headers.get('content-length') || 0);
  // Progressive read so long SSH streams show progress (MB or % if length known).
  if (resp.body && typeof resp.body.getReader === 'function') {
    const reader = resp.body.getReader();
    const chunks = [];
    let received = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      received += value.byteLength;
      if (len > 0) {
        setStatus('Loading… ' + Math.min(100, Math.round((100 * received) / len)) + '%');
      } else {
        setStatus('Loading… ' + (received / 1048576).toFixed(1) + ' MB');
      }
    }
    const buf = new Uint8Array(received);
    let off = 0;
    for (const c of chunks) {
      buf.set(c, off);
      off += c.byteLength;
    }
    raw = new Float32Array(buf.buffer, buf.byteOffset, Math.floor(buf.byteLength / 4));
  } else {
    raw = new Float32Array(await resp.arrayBuffer());
  }
} catch (e) {
  setStatus('Failed to load: ' + e);
  throw e;
}

const n = Math.floor(raw.length / STRIDE);
if (n < 1) {
  setStatus('Empty cloud (0 points)');
} else {
  setStatus(n.toLocaleString() + ' points');
  setTimeout(() => setStatus(''), 2500);
}

const positions = new Float32Array(n * 3);

for (let i = 0; i < n; i++) {
  positions[i * 3] = raw[i * STRIDE];
  positions[i * 3 + 1] = raw[i * STRIDE + 1];
  positions[i * 3 + 2] = raw[i * STRIDE + 2];
}

let scal = null;

if (CMODE === 'height') {
  scal = (i) => positions[i * 3 + 2];
} else if (CMODE === 'intensity' && STRIDE > ICOL) {
  scal = (i) => raw[i * STRIDE + ICOL];
}

const colors = new Float32Array(n * 3);
const c = new THREE.Color();

if (scal && n > 0) {
  let lo = Infinity;
  let hi = -Infinity;

  for (let i = 0; i < n; i++) {
    const v = scal(i);
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }

  for (let i = 0; i < n; i++) {
    const t = (scal(i) - lo) / (hi - lo + 1e-6);
    c.setHSL((1 - t) * 0.66, 1.0, 0.5);
    colors[i * 3] = c.r;
    colors[i * 3 + 1] = c.g;
    colors[i * 3 + 2] = c.b;
  }
} else {
  colors.fill(0.8);
}

const geo = new THREE.BufferGeometry();
geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
geo.computeBoundingSphere();

scene.add(
  new THREE.Points(
    geo,
    new THREE.PointsMaterial({
      size: SIZE,
      vertexColors: true,
      sizeAttenuation: true,
    })
  )
);

scene.add(new THREE.AxesHelper(5));

const sphere = geo.boundingSphere || { radius: 1, center: new THREE.Vector3() };
const r = Math.max(sphere.radius || 1, 1e-3);
const ctr = sphere.center;

camera.position.set(ctr.x, ctr.y - r * 1.4, ctr.z + r * 0.8);
camera.far = r * 20;
camera.updateProjectionMatrix();

controls.target.copy(ctr);
controls.update();

function onResize() {
  const w = Math.max(el.clientWidth, 1);
  const h = Math.max(el.clientHeight, 1);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
}

if (typeof ResizeObserver !== 'undefined') {
  new ResizeObserver(onResize).observe(el);
} else {
  addEventListener('resize', onResize);
}

(function loop() {
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(loop);
})();
"""
