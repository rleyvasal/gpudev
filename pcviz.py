# pcviz.py — reusable point-cloud viewer for SolveIt (FastHTML + three.js).
#
# Companion to CRAFT.py: it *uses* CRAFT's SSH hop (SSH_HOST / SSH_OPTS) to stream
# remote files, but keeps CRAFT itself viz-free.
#
# In SolveIt: paste this source into its own code cell in the same folder as the
# CRAFT cell, so every notebook in the folder inherits it (this file is the
# version-controlled source, exactly as CRAFT.py is the source for the CRAFT cell).
# The viewer's server runs in the LOCAL kernel, so the cell must run local — wrap it:
#       %local
#       <contents of this file>
#       %gpu          # restore the remote default
# Starting the cell with %local makes CRAFT keep the whole cell local (see
# PythonBackend.passthru), so these functions are defined in the SolveIt kernel.
# Then call point_cloud(...) under %local — or add '%pointcloud' to CRAFT's _LOCAL
# tuple and the magic works from any mode (the FastHTML server still lives local).
#
#   point_cloud("/data/scene.bin")             # remote file on the GPU box (default)
#   point_cloud("/data/kitti.bin", stride=4)   # KITTI layout (x,y,z,intensity)
#   point_cloud(arr)                            # a numpy (N,3+) array already in the kernel
#   point_cloud("/local/scene.bin", remote=False)            # a file local to SolveIt
#   point_cloud("/data/scene.bin", color="intensity")        # colour by intensity col
#   show("scene")                              # re-show an already-registered cloud
#   clouds()                                   # list registered names

from fasthtml.common import *
from fasthtml.jupyter import JupyUvi, HTMX
from starlette.responses import Response, StreamingResponse
from fastcore.utils import partial
import numpy as np, subprocess, shlex, itertools, json, uuid

_PORT    = 8000                 # SolveIt's preview proxy maps the iframe to port 8000
_CLOUDS  = {}                   # name -> spec dict
_CURRENT = None
_ctr     = itertools.count(1)
_app = _srv = _preview = _scene_lf = None

_HDRS = (
    Script('{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.165.0/build/three.module.js",'
           '"three/addons/":"https://cdn.jsdelivr.net/npm/three@0.165.0/examples/jsm/"}}',
           type='importmap'),
)

def _ssh_cfg():
    "Find CRAFT's SSH config whether this file was imported or run_cell'd into the kernel ns."
    nss = [globals()]
    try: nss.append(get_ipython().user_ns)
    except Exception: pass
    for ns in nss:
        if ns.get("SSH_HOST"): return ns["SSH_HOST"], ns.get("SSH_OPTS", "")
    raise RuntimeError("SSH_HOST not found — load CRAFT and run %gpu first.")

def _slug(path):
    base = str(path).rsplit('/', 1)[-1]
    base = base[:-4] if base.endswith('.bin') else base
    return base or f"cloud_{next(_ctr)}"

# ── routes (registered on the app in _ensure_server) ──────────────────────────
def _data(name: str):
    "Serve a cloud's raw float32 bytes; remote clouds stream over ssh, never stored."
    c = _CLOUDS.get(name)
    if c is None: return Response(f"unknown cloud {name!r}", status_code=404)
    if c["kind"] == "remote":
        host, opts = _ssh_cfg()
        cmd = ["ssh", *shlex.split(opts), host, "cat", shlex.quote(c["src"])]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        def gen():
            try:
                for ch in iter(lambda: proc.stdout.read(1 << 16), b''): yield ch
            finally:
                proc.stdout.close(); proc.wait()
        return StreamingResponse(gen(), media_type='application/octet-stream')
    return Response(c["src"], media_type='application/octet-stream')   # local/array: bytes

def _scene():
    "The three.js page for the currently-selected cloud."
    c = _CLOUDS[_CURRENT]
    return Div(
        Style('body{margin:0;overflow:hidden}'),
        Div(id='scene', style='width:100vw;height:100vh;margin:0', **{
            'data-url':    f'points/{_CURRENT}.bin',   # relative to /pcviz_scene -> /points/..
            'data-stride': str(c["stride"]),
            'data-size':   str(c["size"]),
            'data-color':  c["color"],
            'data-icol':   str(c["icol"]),
        }),
        Script(_VIEWER_JS, type='module'),
    )

def _ensure_server():
    "Start the singleton FastHTML server once; reused by every point_cloud() call."
    global _app, _srv, _preview, _scene_lf
    if _srv is not None: return
    subprocess.run(f"lsof -ti:{_PORT} | xargs -r kill -9", shell=True)
    _app = FastHTML(hdrs=_HDRS, session_cookie='fh_template_session')
    _app.route('/points/{name}.bin')(_data)
    # Keep the returned location-wrapper: str(_scene_lf) is the route path, which is
    # what HTMX stringifies into the iframe src. The bare _scene fn would not.
    _scene_lf = _app.route('/pcviz_scene')(_scene)
    _srv = JupyUvi(_app, port=_PORT)
    _preview = partial(HTMX, app=_app, host=None, port=None, height='600px')

# ── public API ────────────────────────────────────────────────────────────────
def point_cloud(src, *, stride=5, size=0.06, color="height", icol=3,
                remote=True, name=None):
    """Visualise a point cloud and return the inline viewer.

    src    : remote path (default), local path (remote=False), or a numpy (N,K) array.
    stride : floats per point — nuScenes=5, KITTI=4, xyz=3 (auto-detected for arrays).
    color  : "height" (z), "intensity" (column `icol`), or "mono".
    size   : point size in world units.
    """
    _ensure_server()
    global _CURRENT
    if isinstance(src, np.ndarray):
        arr = np.ascontiguousarray(src, dtype=np.float32)
        stride = arr.shape[1] if arr.ndim == 2 else stride
        kind, data, name = "bytes", arr.tobytes(), name or f"array_{next(_ctr)}"
    elif remote:
        kind, data, name = "remote", src, name or _slug(src)
    else:
        with open(src, "rb") as f: data = f.read()
        kind, name = "bytes", name or _slug(src)
    _CLOUDS[name] = dict(kind=kind, src=data, stride=stride, size=size,
                         color=color, icol=icol)
    _CURRENT = name
    return _preview(_scene_lf)

def show(name):
    "Re-show an already-registered cloud by name."
    global _CURRENT
    if name not in _CLOUDS:
        raise KeyError(f"{name!r} not registered; choose from {list(_CLOUDS)}")
    _ensure_server(); _CURRENT = name
    return _preview(_scene_lf)

def clouds():
    "List registered cloud names."
    return list(_CLOUDS)

# Optional magic so you can write, from any mode (once '%pointcloud' is in CRAFT's
# _LOCAL tuple):   %pointcloud /data/scene.bin
#                  %pointcloud /data/kitti.bin stride=4 color=intensity size=0.1
try:
    from IPython.core.magic import register_line_magic
    @register_line_magic
    def pointcloud(line):
        parts = shlex.split(line)
        if not parts:
            raise ValueError("usage: %pointcloud <path> [stride=N] [color=height|intensity|mono] "
                             "[size=F] [icol=N] [remote=0|1] [name=...]")
        path, kw = parts[0], {}
        for tok in parts[1:]:
            k, _, v = tok.partition('=')
            if   k in ('stride', 'icol'): kw[k] = int(v)
            elif k == 'size':             kw[k] = float(v)
            elif k == 'remote':           kw[k] = v.lower() in ('1', 'true', 'yes')
            elif k in ('color', 'name'):  kw[k] = v
            else: raise ValueError(f"unknown option {tok!r}")
        return point_cloud(path, **kw)
except Exception:
    pass

# %pointcloud_var bridges a point cloud living in the %gpu kernel to the local viewer:
# the remote kernel snapshots the variable to a temp .bin, which pcviz streams back over
# CRAFT's SSH hop and renders locally. Needs CRAFT loaded + %gpu (for remote_run_), and
# this cell must run AFTER the CRAFT cell so PY_BACKEND exists when we register the magic.
#   %pointcloud_var pts
#   %pointcloud_var pts sub=4 color=intensity size=0.1 name=front
try:
    from IPython.core.magic import register_line_magic

    def _craft_keep_local(magic):
        "Ask CRAFT's Python router to keep `magic` cells local; idempotent; no-op without CRAFT."
        be = get_ipython().user_ns.get("PY_BACKEND")
        if be is not None and magic not in be._LOCAL:
            be._LOCAL = tuple(be._LOCAL) + (magic,)

    @register_line_magic
    def pointcloud_var(line):
        """%pointcloud_var <expr> [sub=N] [color=height|intensity|mono] [size=F] [icol=N] [name=...]
        Snapshot an (N,K>=3) point-cloud variable from the %gpu kernel and view it locally.
        `sub` subsamples rows remotely (handy for huge clouds); floats-per-point is the
        array's own column count, so the viewer always parses correctly."""
        ns = get_ipython().user_ns
        pc, rr = ns.get("point_cloud"), ns.get("remote_run_")
        if pc is None or rr is None:
            raise RuntimeError("Load pcviz + CRAFT and run %gpu first (point_cloud / remote_run_ missing).")
        parts = shlex.split(line)
        if not parts:
            raise ValueError("usage: %pointcloud_var <expr> [sub=N] [color=...] [size=F] [icol=N] [name=...]")
        expr, sub, opts = parts[0], 1, dict(color='height', size=0.06, icol=3, name=None)
        for tok in parts[1:]:
            k, _, v = tok.partition('=')
            if   k == 'sub':  sub = max(1, int(v))
            elif k == 'size': opts['size'] = float(v)
            elif k == 'icol': opts['icol'] = int(v)
            elif k in ('color', 'name'): opts[k] = v
            else: raise ValueError(f"unknown option: {tok}")
        opts['name'] = opts['name'] or expr

        remote_path = f"/tmp/pcviz_{uuid.uuid4().hex}.bin"
        code = f"""
import json, numpy as np
_obj = eval({expr!r})
if hasattr(_obj, "detach"): _obj = _obj.detach()
if hasattr(_obj, "cpu"):    _obj = _obj.cpu()
if hasattr(_obj, "numpy"):  _obj = _obj.numpy()
_arr = np.asarray(_obj, dtype=np.float32)
if _arr.ndim != 2 or _arr.shape[1] < 3:
    raise ValueError("expected (N,K) with K>=3, got %r" % (_arr.shape,))
_arr = np.ascontiguousarray(_arr[::{sub}])
_arr.tofile({remote_path!r})
print(json.dumps({{"shape": list(_arr.shape), "path": {remote_path!r}}}))
"""
        out = rr(code, max_chars=4000).strip()
        try:
            meta = json.loads(out.splitlines()[-1])
        except Exception:
            raise RuntimeError(f"remote snapshot failed:\n{out}")
        # file is exactly (N,K) float32 → floats-per-point == K == columns
        return pc(meta["path"], remote=True, stride=meta["shape"][1], **opts)

    _craft_keep_local('%pointcloud_var')
except Exception:
    pass

_VIEWER_JS = """
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const el = document.getElementById('scene');
const URL_ = el.dataset.url, STRIDE = +el.dataset.stride, SIZE = +el.dataset.size;
const CMODE = el.dataset.color, ICOL = +el.dataset.icol;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b1020);
const camera = new THREE.PerspectiveCamera(60, el.clientWidth/el.clientHeight, 0.1, 5000);
camera.up.set(0, 0, 1);                          // LIDAR convention: +Z up
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(el.clientWidth, el.clientHeight);
renderer.setPixelRatio(window.devicePixelRatio);
el.appendChild(renderer.domElement);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

// stream raw float32 bytes, parse by stride, keep x,y,z
const raw = new Float32Array(await (await fetch(URL_)).arrayBuffer());
const n = Math.floor(raw.length / STRIDE);
const positions = new Float32Array(n * 3);
for (let i = 0; i < n; i++) {
  positions[i*3] = raw[i*STRIDE]; positions[i*3+1] = raw[i*STRIDE+1]; positions[i*3+2] = raw[i*STRIDE+2];
}

// choose the scalar to colour by
let scal = null;
if (CMODE === 'height') scal = (i) => positions[i*3+2];
else if (CMODE === 'intensity' && STRIDE > ICOL) scal = (i) => raw[i*STRIDE+ICOL];

const colors = new Float32Array(n * 3), c = new THREE.Color();
if (scal) {
  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < n; i++) { const v = scal(i); if (v<lo) lo=v; if (v>hi) hi=v; }
  for (let i = 0; i < n; i++) {
    const t = (scal(i) - lo) / (hi - lo + 1e-6);
    c.setHSL((1 - t) * 0.66, 1.0, 0.5);          // blue (low) .. red (high)
    colors[i*3] = c.r; colors[i*3+1] = c.g; colors[i*3+2] = c.b;
  }
} else {
  colors.fill(0.8);                              // mono
}

const geo = new THREE.BufferGeometry();
geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
geo.setAttribute('color',    new THREE.BufferAttribute(colors, 3));
geo.computeBoundingSphere();
scene.add(new THREE.Points(geo, new THREE.PointsMaterial({ size: SIZE, vertexColors: true, sizeAttenuation: true })));
scene.add(new THREE.AxesHelper(5));

const r = geo.boundingSphere.radius, ctr = geo.boundingSphere.center;
camera.position.set(ctr.x, ctr.y - r*1.4, ctr.z + r*0.8);
camera.far = r * 20; camera.updateProjectionMatrix();
controls.target.copy(ctr); controls.update();
addEventListener('resize', () => {
  camera.aspect = el.clientWidth/el.clientHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(el.clientWidth, el.clientHeight);
});
(function loop(){ controls.update(); renderer.render(scene, camera); requestAnimationFrame(loop); })();
"""
