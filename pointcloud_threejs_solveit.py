# Point cloud viewer: FastHTML + three.js, for the SolveIt environment.
#
# Each `# %% ===` block below is one SolveIt cell. Run them all with %local.
#
# The data NEVER gets copied into SolveIt. The FastHTML server runs locally (so
# preview() can reach it), but its /points.bin route streams the .bin straight
# from the GPU box over CRAFT's existing SSH hop. The kernel just pipes the bytes
# through to the browser, which feeds them into a three.js BufferGeometry.
#
#   GPU disk --ssh cat--> SolveIt kernel (pipe, not stored) --> browser (WebGL)
#
# Cell 4 is a registry of named clouds; show("name") (cell 7) swaps datasets with
# no other change. Cell 8 is a commented Option-B scaffold (serve from the GPU box,
# data never touches SolveIt) for when you outgrow the streaming-pipe approach.

# %% === cell 1: FastHTML setup ==============================================
from fastcore.utils import *
from fasthtml.common import *
from fasthtml.jupyter import *
import fasthtml.components as fc
import numpy as np
from starlette.responses import Response   # for the binary endpoint


# %% === cell 2: idempotent server startup ===================================
import subprocess

def kill_port(port=8000):
    subprocess.run(f"lsof -ti:{port} | xargs -r kill -9", shell=True)

kill_port()


# %% === cell 3: headers (import map) + app ==================================
hdrs = (
    Link(href='https://cdn.jsdelivr.net/npm/daisyui@5', rel='stylesheet', type='text/css'),
    Script(src='https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4'),
    Link(href='https://cdn.jsdelivr.net/npm/daisyui@5/themes.css', rel='stylesheet', type='text/css'),
    Script('{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.165.0/build/three.module.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@0.165.0/examples/jsm/"}}', type='importmap'),
)

# Custom session cookie so FastHTML sessions work inside SolveIt.
if 'srv' not in globals() or not srv:
    app = FastHTML(hdrs=hdrs, session_cookie='fh_template_session')
    rt = app.route
    srv = JupyUvi(app)

def get_preview(app): return partial(HTMX, app=app, host=None, port=None)
preview = get_preview(app)


# %% === cell 4: registry of remote clouds (no copy into SolveIt) ============
# One entry per cloud: a short name -> the file ON THE GPU box + its float32
# `stride` (floats per point): nuScenes=5 (x,y,z,intensity,ring), KITTI=4
# (x,y,z,intensity), plain xyz=3. Nothing is loaded here — files are streamed
# on demand. SSH_HOST / SSH_OPTS come from CRAFT (in scope once it has loaded).
import shlex
SSH_CMD = ["ssh", *shlex.split(SSH_OPTS), SSH_HOST]   # same hop CRAFT uses

CLOUDS = {
    # "name":   {"path": "/abs/path/on/gpu.bin", "stride": 5},
    "sample":   {"path": "/path/to/your_sweep.bin", "stride": 5},
}

def _ssh_out(*remote_cmd):
    return subprocess.run([*SSH_CMD, *remote_cmd], capture_output=True, text=True).stdout

def discover(remote_glob, stride=5):
    "Register every .bin matching a remote glob, keyed by filename stem."
    for line in _ssh_out("ls", "-1", remote_glob).split():
        if line.endswith(".bin"):
            CLOUDS[line.rsplit('/', 1)[-1][:-4]] = {"path": line, "stride": stride}
    return list(CLOUDS)

# e.g.  discover("/data/nuscenes/samples/LIDAR_TOP/*.bin")
print("registered:", list(CLOUDS))


# %% === cell 5: streaming endpoint, parameterized by cloud name =============
# /points/<name>.bin streams that file from the GPU box. Only names present in
# CLOUDS are servable, so the browser can't ask us to `cat` an arbitrary path.
from starlette.responses import StreamingResponse

@rt('/points/{name}.bin')
def points_bin(name: str):
    spec = CLOUDS.get(name)
    if spec is None:
        return Response(f"unknown cloud {name!r}", status_code=404)
    proc = subprocess.Popen([*SSH_CMD, "cat", shlex.quote(spec["path"])],
                            stdout=subprocess.PIPE)
    def gen():
        try:
            for chunk in iter(lambda: proc.stdout.read(1 << 16), b''):
                yield chunk
        finally:
            proc.stdout.close(); proc.wait()
    return StreamingResponse(gen(), media_type='application/octet-stream')


# %% === cell 6: the three.js scene (renders the selected cloud) =============
# CURRENT picks which cloud to draw. The data URL + stride ride on the <div> as
# data-* attributes, so the JS below stays a static constant — no string
# templating. The JS reads them via el.dataset.
CURRENT = None   # set by show(name) in the next cell

@rt()
def pointcloud():
    spec = CLOUDS[CURRENT]
    return Div(
        Style('body{margin:0;overflow:hidden}'),
        Div(id='scene', cls='w-screen h-screen m-0',
            **{'data-url': f'points/{CURRENT}.bin', 'data-stride': str(spec["stride"])}),
        Script("""
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const el = document.getElementById('scene');
const DATA_URL = el.dataset.url;              // e.g. points/sample.bin
const STRIDE   = parseInt(el.dataset.stride, 10);  // floats per point

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b1020);

const camera = new THREE.PerspectiveCamera(60, el.clientWidth/el.clientHeight, 0.1, 5000);
camera.up.set(0, 0, 1);                       // LIDAR convention: +Z is up

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(el.clientWidth, el.clientHeight);
renderer.setPixelRatio(window.devicePixelRatio);
el.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

// --- stream raw float32 bytes from the remote, parse by stride ---
const buf = await (await fetch(DATA_URL)).arrayBuffer();
const raw = new Float32Array(buf);            // STRIDE floats per point
const n   = raw.length / STRIDE;
const positions = new Float32Array(n * 3);    // keep x,y,z; drop the rest
for (let i = 0; i < n; i++) {
  positions[i*3] = raw[i*STRIDE]; positions[i*3+1] = raw[i*STRIDE+1]; positions[i*3+2] = raw[i*STRIDE+2];
}

// --- colour by height (z): blue (low) .. red (high) ---
let zmin = Infinity, zmax = -Infinity;
for (let i = 0; i < n; i++) { const z = positions[i*3+2]; if (z<zmin) zmin=z; if (z>zmax) zmax=z; }
const colors = new Float32Array(n * 3);
const c = new THREE.Color();
for (let i = 0; i < n; i++) {
  const t = (positions[i*3+2] - zmin) / (zmax - zmin + 1e-6);
  c.setHSL((1 - t) * 0.66, 1.0, 0.5);         // 0.66=blue -> 0=red
  colors[i*3] = c.r; colors[i*3+1] = c.g; colors[i*3+2] = c.b;
}

const geo = new THREE.BufferGeometry();
geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
geo.setAttribute('color',    new THREE.BufferAttribute(colors, 3));
geo.computeBoundingSphere();

const mat = new THREE.PointsMaterial({ size: 0.06, vertexColors: true, sizeAttenuation: true });
scene.add(new THREE.Points(geo, mat));
scene.add(new THREE.AxesHelper(5));

// --- auto-frame the cloud from its bounding sphere ---
const r = geo.boundingSphere.radius, ctr = geo.boundingSphere.center;
camera.position.set(ctr.x, ctr.y - r*1.4, ctr.z + r*0.8);
camera.far = r * 20; camera.updateProjectionMatrix();
controls.target.copy(ctr); controls.update();

window.addEventListener('resize', () => {
  camera.aspect = el.clientWidth/el.clientHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(el.clientWidth, el.clientHeight);
});

(function animate(){ controls.update(); renderer.render(scene, camera); requestAnimationFrame(animate); })();
""", type='module')
    )


# %% === cell 7: pick a cloud and show it ====================================
# Run under %local. Call show("name") for any registered cloud; call it again
# with a different name to swap datasets — no other cell needs to change.
def show(name):
    global CURRENT
    if name not in CLOUDS:
        raise KeyError(f"{name!r} not registered; choose from {list(CLOUDS)}")
    CURRENT = name
    return preview(pointcloud)

show("sample")     # <- swap the name to view a different cloud


# %% === cell 8 (OPTIONAL): Option B scaffold — serve from the GPU box =======
# Reach for this ONLY if the data must never transit SolveIt (governance), or you
# want the GPU to downsample/serve tiles. The server runs ON the GPU container;
# SolveIt only hosts an <iframe>. Data path:  GPU server -> tunnel -> browser.
#
# --- B.1  on the GPU kernel (run this block under %gpu) ---------------------
# from fasthtml.common import *
# from fasthtml.jupyter import JupyUvi
# import numpy as np
# bapp = FastHTML(hdrs=hdrs)                 # reuse the import-map hdrs from cell 3
# brt  = bapp.route
# CLOUDS = { "sample": {"path": "/abs/path/on/gpu.bin", "stride": 5} }  # local here
#
# @brt('/points/{name}.bin')
# def _pts(name:str):
#     pc = np.fromfile(CLOUDS[name]['path'], np.float32)   # file is LOCAL on the GPU
#     # optional GPU-side reduction BEFORE sending, e.g. downsample 4x:  pc = pc[::4]
#     return Response(pc.tobytes(), media_type='application/octet-stream')
#
# @brt('/pc/{name}')
# def _scene(name:str):
#     ...  # same Div + Script as cell 6 (data-url=f'points/{name}.bin', data-stride=...)
#
# bsrv = JupyUvi(bapp, port=8000)            # uvicorn now listening on GPU:8000
#
# --- B.2  forward the GPU port into SolveIt (run in a %local shell / terminal)
#   ssh -N -f -L 8000:localhost:8000  gpudev-<client>     # same alias CRAFT uses
#
# --- B.3  embed it from SolveIt (%local) -----------------------------------
# The browser must hit SolveIt's proxy for local port 8000. That URL is
# environment-specific (how preview() derives its own). Once you have it:
#   from IPython.display import IFrame, display
#   PROXY_8000 = "<SolveIt proxy URL for port 8000>"      # e.g. .../proxy/8000
#   display(IFrame(f"{PROXY_8000}/pc/sample", width='100%', height=600))
#
# PROXY_8000 is the only fiddly bit
