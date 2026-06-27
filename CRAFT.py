import html
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import os
import shutil
from IPython.core.magic import register_line_magic
from IPython.display import HTML, display, clear_output
from jupyter_client import BlockingKernelClient
try:
    from dialoghelper import read_msg          # SolveIt: id of the current cell
except Exception:
    read_msg = None

# ── Configuration ─────────────────────────────────────────────────────────────
CONFIG_PATH = Path.home() / ".config" / "gpudev" / "craft.json"
_cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}

CLIENT_NAME   = _cfg.get("client_name", "")
# Inside every gpudev container the UNIX user is the fixed `gpudev`; the client
# *identity* lives in the container name and tunnel hostname. Paths are stable.
KERNEL_MANAGER = "/home/gpudev/bin/kernel-manager.sh"
KERNEL_RUNTIME = "/home/gpudev/.local/share/jupyter/runtime/kernel.json"

# SSH alias is derived from client_name — must match what `gpudev client info`
# prints and what client-setup.sh sets as the container hostname. Single source
# of truth: client_name in craft.json.
SSH_HOST = f"gpudev-{CLIENT_NAME}" if CLIENT_NAME else ""

# Fixed kernel ports (set in kernel-manager.sh)
KERNEL_PORTS = {
    "shell_port":   54100,
    "iopub_port":   54101,
    "stdin_port":   54102,
    "control_port": 54103,
    "hb_port":      54104,
}

CLOUDFLARED_PATH = Path(
    os.environ.get("CLOUDFLARED_PATH")
    or shutil.which("cloudflared")
    or (Path.home() / ".local" / "bin" / "cloudflared")
)

# Ensure cloudflared's install directory is on PATH for this process and any
# SSH subprocesses it spawns (ProxyCommand runs in a non-interactive shell and
# won't see ~/.local/bin unless we add it here).
_cf_dir = str(CLOUDFLARED_PATH.parent)
if _cf_dir not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _cf_dir + os.pathsep + os.environ.get("PATH", "")

del _cfg

# ── Helpers ───────────────────────────────────────────────────────────────────
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\[[0-9;]*$|\x1b$")

def _strip_ansi(text):
    return ANSI_RE.sub("", text)

def _run(cmd, check=True, capture_output=False):
    return subprocess.run(cmd, shell=True, check=check,
                          capture_output=capture_output, text=True)

# Shared SSH options used by every CRAFT connection. ControlMaster is the
# important one: the first SSH call opens a single cloudflared tunnel and every
# later call — including the kernel port-forward — reuses it, so startup costs
# one handshake instead of four or five. ConnectTimeout keeps a dead host/tunnel
# from hanging for minutes.
SSH_OPT_LIST = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
]
# Connection multiplexing isn't supported by Windows' bundled OpenSSH, so only
# enable it on platforms where it works.
if not sys.platform.startswith("win"):
    SSH_OPT_LIST += [
        "-o", "ControlMaster=auto",
        "-o", "ControlPath=~/.ssh/craft-%C",
        "-o", "ControlPersist=300",
    ]
SSH_OPTS = " ".join(SSH_OPT_LIST)

def _ssh(cmd, capture_output=False, check=True):
    """Run a command inside the client's container via SSH.

    GPUDEV_CLIENT is set explicitly (not redundant): sshd does not pass the
    container's `docker run -e` environment into login sessions, and
    kernel-manager.sh's hostname fallback resolves to the container ID rather
    than the client name. Without this the wrong client would be targeted.
    """
    wrapped = f"GPUDEV_CLIENT={CLIENT_NAME} {cmd}"
    return _run(f"ssh {SSH_OPTS} {SSH_HOST} {json.dumps(wrapped)}",
                check=check, capture_output=capture_output)

# ── Cloudflared ───────────────────────────────────────────────────────────────
def install_cloudflared():
    """Ensure cloudflared is available. Returns True if present/installed."""
    if shutil.which("cloudflared"):
        return True
    if sys.platform == "darwin":
        print("cloudflared not found. Install it with:  brew install cloudflared")
        return False
    if sys.platform.startswith("win"):
        print("cloudflared not found. Install it from:")
        print("  https://developers.cloudflare.com/cloudflared/install/")
        return False
    print("cloudflared not found — downloading a local copy...")
    CLOUDFLARED_PATH.parent.mkdir(parents=True, exist_ok=True)
    url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
    try:
        _run(f"curl -fsSL {url} -o {CLOUDFLARED_PATH} && chmod +x {CLOUDFLARED_PATH}")
    except Exception as e:
        print(f"Could not install cloudflared automatically: {e}")
        print("Install it manually: https://developers.cloudflare.com/cloudflared/install/")
        return False
    return True

# ── Kernel Management ─────────────────────────────────────────────────────────
def ensure_kernel(force_restart=False):
    """Start the kernel (or force a fresh-key restart) inside the container."""
    _ssh(f"{KERNEL_MANAGER} {'restart' if force_restart else 'start'}")

def kernel_doctor():
    """Return the container-side kernel diagnostics as text."""
    try:
        result = _ssh(f"{KERNEL_MANAGER} doctor", capture_output=True)
        return result.stdout
    except Exception as e:
        return f"(could not run kernel doctor: {e})"

def gpu_status():
    """Return a list of per-GPU summary strings from the container.

    Returns None if nvidia-smi is unavailable (no GPU, driver missing, or the
    container can't be reached).
    """
    query = ("nvidia-smi --query-gpu=index,name,memory.used,memory.total,"
             "utilization.gpu,temperature.gpu --format=csv,noheader,nounits")
    try:
        result = _ssh(query, capture_output=True, check=False)
    except Exception:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    gpus = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        idx, name, used, total, util, temp = parts[:6]
        gpus.append(f"[{idx}] {name}  {used}/{total} MiB  {util}% util  {temp}°C")
    return gpus or None

def fetch_kernel_info():
    """Read the connection file from the container."""
    result = _ssh(f"cat {KERNEL_RUNTIME}", capture_output=True)
    info = json.loads(result.stdout)
    # Ports are fixed — override ip to localhost for forwarding
    info.update(KERNEL_PORTS)
    info["ip"] = "127.0.0.1"
    return info

def start_port_forwarding(kernel_info):
    """SSH-tunnel the kernel's ZMQ ports to localhost.

    stderr is captured to a temp file so a forward that genuinely dies on startup
    (a local port already bound, a ProxyCommand/auth failure, ...) can report WHY
    instead of a bare 'exited'. The path is stashed on the Popen for the caller.
    """
    args = ["ssh", "-N", *SSH_OPT_LIST]
    for port in KERNEL_PORTS.values():
        args.extend(["-L", f"{port}:127.0.0.1:{port}"])
    args.append(SSH_HOST)
    errf = tempfile.NamedTemporaryFile(prefix="craft-fwd-", suffix=".log", delete=False)
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=errf)
    proc.craft_stderr_path = errf.name
    errf.close()
    return proc

# ── Output Display ────────────────────────────────────────────────────────────
def _handle_output(msg):
    msg_type = msg["msg_type"]
    content = msg.get("content", {})

    if msg_type == "stream":
        # The frontend collapses carriage returns itself, so progress bars
        # (tqdm etc.) overwrite in place without any special handling.
        print(_strip_ansi(content.get("text", "")), end="")

    elif msg_type == "error":
        # Escape the traceback — it contains tokens like "<module>" / "<stdin>"
        # that the frontend would otherwise parse as HTML tags and drop.
        tb = "\n".join(content.get("traceback", []))
        display(HTML(f"<pre>{html.escape(_strip_ansi(tb))}</pre>"))

    elif msg_type == "clear_output":
        clear_output(wait=content.get("wait", False))

    elif msg_type in ("display_data", "update_display_data", "execute_result"):
        # Forward the remote kernel's full mime bundle to the local display
        # system instead of re-implementing a renderer. The frontend picks the
        # richest representation (html, markdown, latex, png, svg, json, …) and
        # routes update_display_data to the right output via its display_id.
        get_ipython().display_pub.publish(
            data=content.get("data", {}),
            metadata=content.get("metadata", {}),
            transient=content.get("transient", {}),
            update=(msg_type == "update_display_data"),
        )

# ── Remote Execution Manager ──────────────────────────────────────────────────
class RemoteExecutionManager:
    def __init__(self):
        self.remote_kc = None
        self._remote_active = False
        self._tunnel_proc = None

    _LOCAL_PREFIXES = (
        '%gpu', '%local', '%restart_kernel', '%kernel_status',
        'remote_on(', 'remote_off(', 'kernel_status(',
        'await call_tool(',
    )

    def _test_connection(self, kernel_info, timeout=3):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex(('127.0.0.1', kernel_info["shell_port"]))
            sock.close()
            return result == 0
        except Exception:
            return False

    def _connect_kernel(self, kernel_info, timeout=20):
        kc = BlockingKernelClient()
        kc.load_connection_info(kernel_info)
        kc.start_channels()
        try:
            kc.wait_for_ready(timeout=timeout)
        except Exception:
            kc.stop_channels()
            raise
        return kc

    def _transform_cell(self, lines):
        # Only route to the GPU while routing is on (the transformer is attached
        # only between %gpu and %local, but this keeps the contract explicit).
        if not self._remote_active:
            return lines
        code = ''.join(lines)
        stripped = code.strip()
        if stripped.startswith(self._LOCAL_PREFIXES) or 'get_ipython()' in code:
            return lines
        self._pending_code = code
        return ['_exec_mgr.execute_remote(_exec_mgr._pending_code)\n']

    def _check_ssh(self):
        """Verify we can reach the container over SSH; explain how to fix if not."""
        probe = _ssh("echo SSH_OK", capture_output=True, check=False)
        if probe.returncode == 0 and "SSH_OK" in (probe.stdout or ""):
            return True
        print(f"Cannot reach '{SSH_HOST}' over SSH. Check that:")
        print(f"  • ~/.ssh/config has a matching 'Host {SSH_HOST}' entry")
        print(f"    (the host can print it: gpudev client info {CLIENT_NAME})")
        print("  • cloudflared is installed and on your PATH")
        print("  • the container is running on the host")
        err = (probe.stderr or "").strip()
        if err:
            print("\nssh reported:")
            print("  " + err.replace("\n", "\n  "))
        return False

    def setup_remote(self):
        if self.remote_kc is not None:
            try:
                self.remote_kc.stop_channels()
            except Exception:
                pass
            self.remote_kc = None

        if not CONFIG_PATH.exists():
            print(f"Config not found at {CONFIG_PATH}")
            print('Create it with: {"client_name": "<your-name>"}')
            return False
        if not CLIENT_NAME:
            print(f'No "client_name" set in {CONFIG_PATH}')
            print('Set it like: {"client_name": "<your-name>"}')
            return False

        if not install_cloudflared():
            return False
        if not self._check_ssh():
            return False
        ensure_kernel()

        # setup_remote is the (re)connect path — never trust a leftover forward.
        # Tear it down so the loop below rebuilds it against the current container
        # (a `gpudev client rebuild` otherwise leaves a stale forward whose local
        # port still looks open). The %gpu fast path skips setup_remote when the
        # kernel is already healthy, so this only costs on an actual reconnect.
        self._kill_stale_forwards()

        # NON-DESTRUCTIVE reconnect. ensure_kernel() above already guaranteed a live
        # kernel listening on the fixed port whose key matches the connection file
        # (`kernel-manager.sh start` reuses a healthy kernel and waits until it's
        # listening; it self-heals genuine staleness by reaping + rekeying only when
        # the kernel is actually dead). So a failure to attach HERE can only be a
        # tunnel problem — a cold cloudflared handshake after idle, or a stale
        # forward — never a dead kernel. Rebuild the forward and retry; never
        # force-restart, which would wipe in-memory state. %restart_kernel is the
        # explicit "clean slate" path.
        last_err = None
        for attempt in range(2):
            try:
                kernel_info = fetch_kernel_info()
                self._ensure_tunnel(kernel_info)
                self.remote_kc = self._connect_kernel(kernel_info)
                print(f"Remote kernel '{CLIENT_NAME}' ready")
                return True
            except Exception as e:
                last_err = e
                self._kill_stale_forwards()   # drop the cold/half-open forward so the
                                              # next attempt builds a fresh one

        print(f"Could not attach to remote kernel '{CLIENT_NAME}': {last_err}")
        print("The kernel is likely still alive — your variables are preserved. "
              "Re-run the cell to retry, or %restart_kernel for a fresh kernel "
              "(clears state).")
        print(kernel_doctor())
        raise last_err

    def _kill_stale_forwards(self):
        """Tear down our port-forward AND any orphaned `ssh -N -L` forwards left by
        a previous CRAFT instance, then drop the SSH control master. After a
        `gpudev client rebuild` the old forward keeps the local port *open* (so
        _test_connection wrongly passes) while routing to the now-dead container —
        which is what causes the kernel-timeout + retry/kernel-pileup. This forces
        a fresh forward + master against the current container."""
        if self._tunnel_proc and self._tunnel_proc.poll() is None:
            self._tunnel_proc.terminate()
            try:
                self._tunnel_proc.wait(timeout=3)
            except Exception:
                self._tunnel_proc.kill()
        if self._tunnel_proc is not None:
            path = getattr(self._tunnel_proc, "craft_stderr_path", None)
            if path:
                try: os.unlink(path)
                except OSError: pass
        self._tunnel_proc = None
        if not sys.platform.startswith("win"):
            port = KERNEL_PORTS["shell_port"]
            _run(f"pkill -f 'ssh -N.*-L {port}:' 2>/dev/null", check=False)
            _run(f"ssh {SSH_OPTS} -O exit {SSH_HOST} 2>/dev/null", check=False)

    def _ensure_tunnel(self, kernel_info, timeout=25):
        """(Re)establish the SSH port-forward and wait until it actually carries
        traffic. Only trust an open local port when WE own the live forward;
        otherwise it may be an orphan from a rebuild that routes to a dead
        container, so rebuild it.

        Readiness is polled, not slept: a cold cloudflared+SSH handshake can take far
        longer than a couple of seconds (a fixed sleep under-waited it and pushed the
        warm-up onto _connect_kernel, costing a failed attempt), while a warm tunnel
        is ready in a fraction of a second (a fixed sleep needlessly stalled it)."""
        ours_alive = self._tunnel_proc is not None and self._tunnel_proc.poll() is None
        if ours_alive and self._test_connection(kernel_info):
            return
        self._kill_stale_forwards()
        self._tunnel_proc = start_port_forwarding(kernel_info)
        # Success signal is the LOCAL forwarded port accepting a connection — NOT our
        # child staying alive. With ControlMaster, the `ssh -N -L` slave can hand the
        # forward to the persistent master and exit cleanly while the master keeps the
        # listener open, so a dead child with an open port is still a good tunnel.
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._test_connection(kernel_info, timeout=1):
                return
            if self._tunnel_proc.poll() is not None:
                # Child exited and nothing is listening yet. Under ControlMaster the
                # master may still be finishing the hand-off, so give the port a brief
                # grace to appear before declaring a genuine startup failure.
                for _ in range(8):                       # ~2s
                    if self._test_connection(kernel_info, timeout=1):
                        return
                    time.sleep(0.25)
                raise RuntimeError(self._forward_failure_msg())
            time.sleep(0.25)
        raise TimeoutError(
            f"Tunnel to '{SSH_HOST}' not ready after {timeout}s "
            "(cold cloudflared handshake or unreachable host).")

    def _forward_failure_msg(self):
        """Build a useful error for a forward that exited without opening the port,
        including its captured stderr and exit code so the cause is visible."""
        rc = self._tunnel_proc.returncode if self._tunnel_proc else None
        detail = ""
        path = getattr(self._tunnel_proc, "craft_stderr_path", None)
        if path:
            try:
                detail = Path(path).read_text().strip()
            except Exception:
                pass
        msg = f"SSH port-forward to '{SSH_HOST}' exited (rc={rc}) without opening the port"
        return msg + (":\n" + detail if detail else " — check cloudflared / host reachability.")

    def shutdown_remote(self):
        if self.remote_kc is not None:
            try:
                self.remote_kc.stop_channels()
            except Exception:
                pass
        self.remote_kc = None
        self._kill_stale_forwards()

    def _output_hook(self, msg):
        _handle_output(msg)

    def _ensure_live(self):
        """Confirm the remote kernel is reachable right now; if an idle period
        dropped the tunnel, transparently reconnect to the SAME running kernel
        (variables/models preserved) before returning. Returns True when a usable
        connection is in place, False if reconnect failed.
        """
        if self.remote_kc is None:
            return self.reconnect()
        # Fast local check: if our forward is gone or its port is closed, the
        # tunnel died during idle (ServerAlive tears a dead forward down) —
        # reconnect without paying the kernel-ping timeout.
        tunnel_dead = (self._tunnel_proc is None
                       or self._tunnel_proc.poll() is not None
                       or not self._test_connection(KERNEL_PORTS))
        if tunnel_dead:
            return self.reconnect()
        # Forward is up locally; confirm the kernel actually answers — a half-open
        # forward keeps the local port open but routes nowhere — before we commit
        # real code to it (which would otherwise block forever).
        if self.kernel_health()[0]:
            return True
        return self.reconnect()

    def reconnect(self):
        """Rebuild the SSH tunnel and re-attach to the LIVE remote kernel WITHOUT
        restarting it, so in-memory state survives an idle-dropped connection.

        This is the non-destructive counterpart to restart_kernel(): ensure_kernel()
        runs `kernel-manager.sh start`, which REUSES a healthy kernel (only creating
        one if none exists), so the connection-file key still matches the running
        kernel and the same namespace comes back. Returns True on success.
        """
        # Drop our stale client object, but never signal the remote kernel.
        if self.remote_kc is not None:
            try:
                self.remote_kc.stop_channels()
            except Exception:
                pass
            self.remote_kc = None
        try:
            ensure_kernel()                 # start = reuse a live kernel; never wipes
            self._kill_stale_forwards()     # force a fresh forward, not a half-open one
            kernel_info = fetch_kernel_info()
            self._ensure_tunnel(kernel_info)
            self.remote_kc = self._connect_kernel(kernel_info)
        except Exception as e:
            print(f"Reconnect failed: {e}")
            return False
        print(f"Reconnected to live kernel '{CLIENT_NAME}' (variables preserved)")
        return True

    def execute_remote(self, code, verbose=False):
        # Don't send code into a dead tunnel (which blocks forever): make sure the
        # kernel is actually reachable first, reconnecting to the SAME running
        # kernel if an idle period dropped the connection (state is preserved).
        if not self._ensure_live():
            raise RuntimeError(
                "Remote kernel unreachable and automatic reconnect failed. "
                "Check %kernel_status, or run %restart_kernel for a fresh kernel.")
        try:
            reply = self.remote_kc.execute_interactive(
                code=code, output_hook=self._output_hook)
        except KeyboardInterrupt:
            print("Interrupted — stopping remote job...")
            msg = self.remote_kc.session.msg("interrupt_request")
            self.remote_kc.control_channel.send(msg)
            print("Remote job interrupted.")
            raise
        self.remote_kc.last_result = reply
        if verbose:
            return reply

    @staticmethod
    def _detach_transformer():
        """Remove our cell-routing transformer from IPython and return the shell."""
        ip = get_ipython()
        ip.input_transformers_cleanup[:] = [
            f for f in ip.input_transformers_cleanup
            if getattr(getattr(f, '__func__', None), '__name__', None)
               != '_transform_cell'
        ]
        return ip

    def remote_on(self):
        if self._remote_active:
            print("Already executing remotely")
            return
        if self.remote_kc is None:
            raise RuntimeError("Remote kernel not connected. Run %gpu first.")
        ip = self._detach_transformer()
        ip.input_transformers_cleanup.append(self._transform_cell)
        self._remote_active = True
        print("Remote execution enabled — all cells now run remotely")

    def remote_off(self):
        if not self._remote_active:
            print("Already executing locally")
            return
        self._detach_transformer()
        self._remote_active = False
        print("Remote execution disabled — cells now run locally")

    def restart_kernel(self):
        if self.remote_kc is None:
            print("No remote kernel connected")
            return
        self.remote_kc.stop_channels()
        self.remote_kc = None
        ensure_kernel(force_restart=True)
        kernel_info = fetch_kernel_info()
        self._ensure_tunnel(kernel_info)
        self.remote_kc = self._connect_kernel(kernel_info)
        print(f"Remote kernel '{CLIENT_NAME}' restarted")

    def kernel_health(self, timeout=5):
        if self.remote_kc is None:
            return False, "not connected"
        try:
            self.remote_kc.kernel_info()
            reply = self.remote_kc.get_shell_msg(timeout=timeout)
            if reply["msg_type"] == "kernel_info_reply":
                return True, "responsive"
            return False, f"unexpected reply: {reply['msg_type']}"
        except Exception as e:
            return False, str(e)


# ── Mojo Execution Manager ────────────────────────────────────────────────────
# Mojo is compiled, so there is no persistent kernel. Each cell becomes a
# generated .mojo source: prior fn/def/struct/alias/state cells are replayed as a
# preamble and command cells are wrapped in `def main()`. The source is shipped to
# the SAME container as the Python kernel and run with `$MOJO run`, so %gpum hits
# the same GPU as %gpu. Note `mojo run` compiles each time — for benchmarking,
# `mojo build` once then time the binary.
# Mojo in a slim container can't locate its Crashpad handler and prints a benign
# multi-line warning to stderr on every run. Strip just that noise, nothing else.
_MOJO_NOISE = ("Failed to initialize Crashpad", "Crash reporting will not be available",
               "crashpad handler")
def _scrub_mojo_noise(text):
    if not text:
        return ""
    return "".join(ln for ln in text.splitlines(keepends=True)
                   if not any(n in ln for n in _MOJO_NOISE))


class RemoteMojoHelper:
    """Run / build / package Mojo inside the client container via pixi, over SSH."""
    # Absolute paths: an sshd login session doesn't inherit the Dockerfile's ENV
    # (same reason _ssh sets GPUDEV_CLIENT). `pixi run` activates the env so Mojo's
    # runtime libs resolve — we never call a bare mojo binary.
    PIXI = "/opt/pixi/bin/pixi"
    PROJ = "/opt/mojo-proj"

    def _runner(self):
        return f"{self.PIXI} run --manifest-path {self.PROJ}/pixi.toml"

    def run_source(self, src):
        # write the program (piped over stdin), then run it inside the pixi env
        subprocess.run(
            f"ssh {SSH_OPTS} {SSH_HOST} "
            f"{json.dumps('mkdir -p /tmp/gpum && cat > /tmp/gpum/mojo_run.mojo')}",
            input=src, text=True, shell=True, check=True)
        return _ssh(f"{self._runner()} mojo run /tmp/gpum/mojo_run.mojo",
                    capture_output=True, check=False)

    def bench_source(self, src, n):
        # Compile ONCE, then time N runs. The build + loop run inside a SINGLE
        # `pixi run bash <script>` so the env is activated once (mojo + the built
        # binary on PATH, libs resolved) and per-iteration overhead is just the
        # binary launch — not pixi or SSH. date +%s%N → integer ns, pure-bash timing.
        subprocess.run(
            f"ssh {SSH_OPTS} {SSH_HOST} "
            f"{json.dumps('mkdir -p /tmp/gpum && cat > /tmp/gpum/bench.mojo')}",
            input=src, text=True, shell=True, check=True)
        script = (
            "t0=$(date +%s%N); mojo build /tmp/gpum/bench.mojo -o /tmp/gpum/bench 1>&2 || exit 3\n"
            "echo COMPILE $(( $(date +%s%N) - t0 ))\n"
            "/tmp/gpum/bench >/dev/null 2>&1\n"                      # warm-up (discarded)
            f"for i in $(seq 1 {n}); do a=$(date +%s%N); /tmp/gpum/bench >/dev/null 2>&1; "
            "echo RUN $(( $(date +%s%N) - a )); done\n"
        )
        # Pipe the script to a file then run it under the env; the $(...) expand
        # remotely when bash runs the file, never on the local shell.
        return subprocess.run(
            f"ssh {SSH_OPTS} {SSH_HOST} "
            f"{json.dumps(f'cat > /tmp/gpum/bench.sh && {self._runner()} bash /tmp/gpum/bench.sh')}",
            input=script, text=True, shell=True, check=False, capture_output=True)

    def add_package(self, spec, pypi=False):
        flag = "--pypi " if pypi else ""
        return _ssh(f"{self.PIXI} add --manifest-path {self.PROJ}/pixi.toml {flag}{spec}",
                    capture_output=True, check=False)


class MojoExecutionManager:
    def __init__(self, root="/tmp/craft-mojo", helper=None):
        self.helper = helper or RemoteMojoHelper()
        self.root = Path(root); self.root.mkdir(exist_ok=True)
        self.history_path = self.root / "history.json"
        self.run_path = self.root / "mojo_run.mojo"
        self._counter = 0

    def load_history(self):
        if not self.history_path.exists(): return []
        return json.loads(self.history_path.read_text())

    def save_history(self, cells):
        self.history_path.write_text(json.dumps(cells, indent=2))

    def first_meaningful_line(self, code):
        for line in code.splitlines():
            s = line.strip()
            if s and not s.startswith("#"): return s
        return ""

    def defined_symbols(self, code):
        # `def` included: Mojo 1.0 deprecated `fn`→`def`, so a top-level `def foo`
        # is a definition (preamble cell), not a command. Without it, def-based
        # function cells get misclassified as commands and never replayed.
        return re.findall(r"^\s*(?:fn|def|struct|trait|class|alias)\s+([A-Za-z_]\w*)", code, re.M)

    def assigned_symbols(self, code):
        return re.findall(r"^\s*(?:let|alias|comptime)\s+([A-Za-z_]\w*)\b", code, re.M)

    def has_main(self, code):
        # A cell that defines its own entry point is a complete program.
        return bool(re.search(r"^\s*(?:fn|def)\s+main\s*\(", code, re.M))

    def cell_kind(self, code):
        # An explicit-main cell is a full program → treat like a command (the
        # entry), so it runs as-is and isn't replayed into later cells' preamble.
        if self.has_main(code): return "command"
        line = self.first_meaningful_line(code)
        if self.defined_symbols(code): return "code"
        if line.startswith(("from ", "import ")): return "code"
        if self.assigned_symbols(code): return "state"
        return "command"

    def is_mixed_cell(self, code):
        # A self-contained program (its own `def main()`) is allowed, even with
        # prints — the "no mixing" rule is only for loose defs + top-level commands.
        if self.has_main(code): return False
        return bool(self.defined_symbols(code)) and "print(" in code

    async def current_msg_id(self):
        # SolveIt gives a stable id per cell so re-running a cell updates its
        # history entry instead of appending. Off SolveIt, fall back to a counter.
        try:
            msg = await read_msg(0); return msg["id"]
        except Exception:
            self._counter += 1; return f"cell-{self._counter}"

    def upsert_cell(self, msg_id, code):
        cells = self.load_history(); now = time.time(); kind = self.cell_kind(code)
        entry = {"msg_id": msg_id, "updated_at": now, "kind": kind,
                 "defines": self.defined_symbols(code),
                 "assigns": self.assigned_symbols(code), "code": code}
        for cell in cells:
            if cell.get("msg_id") == msg_id:
                cell.update(entry); self.save_history(cells); return
        entry["index"] = len(cells); entry["created_at"] = now
        cells.append(entry); self.save_history(cells)

    def latest_wins_cells(self, cells):
        seen = set(); kept = []
        for cell in reversed(cells):
            symbols = set(cell.get("defines", [])) | set(cell.get("assigns", []))
            if not symbols: kept.append(cell); continue
            if symbols & seen: continue
            seen.update(symbols); kept.append(cell)
        return list(reversed(kept))

    def build_source(self, current_code, current_msg_id):
        cells = [c for c in self.load_history() if c.get("msg_id") != current_msg_id]
        persistent = self.latest_wins_cells(
            [c for c in cells if c.get("kind") in ("code", "state")])
        current_kind = self.cell_kind(current_code)
        if current_kind in ("code", "state"):
            current_entry = {"kind": current_kind,
                             "defines": self.defined_symbols(current_code),
                             "assigns": self.assigned_symbols(current_code),
                             "code": current_code}
            persistent = self.latest_wins_cells(persistent + [current_entry])
            preamble = "\n\n".join(c["code"] for c in persistent)
            return preamble + "\n\ndef main():\n    pass\n"
        preamble = "\n\n".join(c["code"] for c in persistent)
        if self.has_main(current_code):
            # complete program with its own main(): run it as-is after the preamble
            return ((preamble + "\n\n") if preamble else "") + current_code.rstrip() + "\n"
        expr = current_code.strip()
        if "\n" not in expr and not expr.startswith(
                ("print(", "for ", "if ", "while ", "var ", "let ")):
            body = "    print(" + expr + ")"
        else:
            body = "\n".join("    " + line for line in current_code.splitlines())
        return ((preamble + "\n\n") if preamble else "") + "def main():\n" + body + "\n"

    async def execute_mojo(self, code):
        if self.is_mixed_cell(code):
            raise ValueError("Mojo cells shouldn't mix definitions and commands — "
                             "put defs and print/calls in separate cells.")
        msg_id = await self.current_msg_id()
        src = self.build_source(code, msg_id)
        self.run_path.write_text(src)
        t0 = time.perf_counter(); r = self.helper.run_source(src)
        dt = time.perf_counter() - t0
        if r.stdout: print(r.stdout, end="")
        err = _scrub_mojo_noise(r.stderr)
        if err: print(err, end="")
        print(f"[mojo run: {dt:.3f}s]")
        if r.returncode != 0: raise RuntimeError("mojo run failed")
        self.upsert_cell(msg_id, code)

    def restart_mojo(self):
        if self.root.exists(): shutil.rmtree(self.root)
        self.root.mkdir(exist_ok=True)
        print("Mojo restarted: history + generated source cleared")

    def show_history(self):
        print(self.history_path.read_text() if self.history_path.exists() else "[]")

    def show_run(self):
        print(self.run_path.read_text() if self.run_path.exists() else "")

    def add_package(self, spec):
        # `pixi add` into the container's Mojo project — works for Mojo packages
        # (the thing uv can't do) and Python deps. Persists for the container's
        # life (reset by `gpudev client rebuild`). Conda channels first, then PyPI.
        if not spec:
            print("usage: %mojo_add <package> [...]   (conda channels first, PyPI auto-fallback)")
            return
        print(f"pixi add {spec} … (downloading)")
        r = self.helper.add_package(spec)
        out = _scrub_mojo_noise((r.stdout or "") + (r.stderr or ""))
        # Conda channels don't have it (e.g. PyPI-only like cowsay) → retry as a
        # PyPI dependency. Skip if the user already passed an explicit flag.
        if r.returncode != 0 and "No candidates" in out and "--" not in spec:
            print("  not on conda channels — trying PyPI…")
            r = self.helper.add_package(spec, pypi=True)
            out = _scrub_mojo_noise((r.stdout or "") + (r.stderr or ""))
        print(out.strip() or (f"added: {spec}" if r.returncode == 0 else "add failed"))

    def bench(self, n=20):
        # Benchmark the LAST generated program (what %mojo_run shows): the fix for
        # `mojo run`'s per-cell compile cost — compile once, report run time alone.
        import statistics
        src = self.run_path.read_text() if self.run_path.exists() else ""
        if not self.has_main(src):    # `def main()` or `fn main()` — a runnable program
            print("Nothing to benchmark — run a %gpum command cell first.")
            return
        r = self.helper.bench_source(src, max(1, n))
        out = r.stdout or ""
        runs = sorted(int(l.split()[1]) for l in out.splitlines() if l.startswith("RUN"))
        comp = next((int(l.split()[1]) for l in out.splitlines() if l.startswith("COMPILE")), None)
        if not runs:
            print("benchmark failed:")
            print(_scrub_mojo_noise(r.stderr or out) or "(no output)")
            return
        ms = lambda ns: ns / 1e6
        print(f"Mojo benchmark — {len(runs)} runs, warm-up discarded (compile excluded)")
        if comp is not None:
            print(f"  compile : {ms(comp):9.1f} ms  (once)")
        print(f"  min     : {ms(runs[0]):9.3f} ms")
        print(f"  median  : {ms(statistics.median(runs)):9.3f} ms")
        print(f"  mean    : {ms(statistics.mean(runs)):9.3f} ms")
        if len(runs) > 1:
            print(f"  stdev   : {ms(statistics.pstdev(runs)):9.3f} ms")


# ── Mode Router (one transformer, three modes) ────────────────────────────────
# %gpu / %gpum / %local just swap the active backend, so Python-remote and
# Mojo-remote are mutually exclusive for free and there is a single source of
# truth for "what owns the cell pipeline".
class ModeRouter:
    def __init__(self): self.backend = None          # None → normal local IPython
    def _router_transform(self, lines):
        if self.backend is None: return lines
        code = "".join(lines)
        if self.backend.passthru(code): return lines
        self.backend.pending = code
        return [self.backend.dispatch + "\n"]
    @staticmethod
    def _detach():
        ip = get_ipython()
        ip.input_transformers_cleanup[:] = [
            f for f in ip.input_transformers_cleanup
            if getattr(getattr(f, "__func__", None), "__name__", "") != "_router_transform"]
    def set(self, backend):
        self.backend = backend
        self._detach()
        if backend is not None:
            get_ipython().input_transformers_cleanup.append(self._router_transform)
        print(backend.banner if backend else "Local Python mode — cells run in this notebook")

class PythonBackend:
    banner   = "GPU Python mode — cells run on the remote kernel"
    dispatch = "_exec_mgr.execute_remote(ROUTER.backend.pending)"
    pending  = None
    # Only CRAFT's own control magics (+ introspection / tool-use helpers) run
    # locally. Everything else — including !shell and %magics like %time — goes
    # to the remote kernel, which is a full ipykernel that runs them IN THE
    # container. (`s[0] in "%!?"` would wrongly keep !shell/%time local.)
    _LOCAL = ('%gpu', '%gpum', '%local', '%restart_kernel', '%restart_mojo',
              '%kernel_status', '%mojo_history', '%mojo_run', '%bench', '%mojo_add')
    def passthru(self, c):
        s = c.lstrip()
        return (s.startswith(self._LOCAL) or "get_ipython()" in c
                or s.startswith(("await call_tool(", "_exec_mgr.", "remote_run_(")))

class MojoBackend:
    banner   = "GPU Mojo mode — cells compiled & run in the GPU container"
    dispatch = "await _mojo_mgr.execute_mojo(ROUTER.backend.pending)"
    pending  = None
    def passthru(self, c):
        s = c.lstrip()
        return (not s) or s[0] in "%!?" or "get_ipython()" in c or "_mojo_mgr." in c


if '_exec_mgr' in globals() and _exec_mgr is not None:
    _exec_mgr.shutdown_remote()

_exec_mgr = RemoteExecutionManager()
_mojo_mgr = MojoExecutionManager()
ModeRouter._detach()                 # drop any router transformer from a prior %run
ROUTER = ModeRouter()
PY_BACKEND, MOJO_BACKEND = PythonBackend(), MojoBackend()

# ── remote_run_ (for tool use) ────────────────────────────────────────────────
def remote_run_(code: str, max_chars: int = 2000) -> str:
    """Execute code on the remote kernel and return output as a string."""
    collected = []

    def capturing_hook(msg):
        msg_type = msg["msg_type"]
        content = msg.get("content", {})
        if msg_type == "stream":
            collected.append(_strip_ansi(content.get("text", "")))
        elif msg_type == "error":
            collected.append(_strip_ansi("\n".join(content.get("traceback", []))))
        elif msg_type in ("display_data", "execute_result"):
            data = content.get("data", {})
            if "text/plain" in data:
                collected.append(data["text/plain"])
        _exec_mgr._output_hook(msg)

    _exec_mgr.remote_kc.execute_interactive(code=code, output_hook=capturing_hook)
    output = "".join(collected)
    if len(output) > max_chars:
        half = max_chars // 2
        output = (output[:half]
                  + f"\n\n... [{len(output) - max_chars} chars truncated] ...\n\n"
                  + output[-half:])
    return output

# ── Magics ────────────────────────────────────────────────────────────────────
def _ensure_connected():
    """Make sure the remote kernel + SSH tunnel are up. Returns True on success.

    Both %gpu and %gpum need this: Python runs on the kernel, Mojo runs over the
    same SSH path into the same container, so either way the tunnel must be live.
    """
    if _exec_mgr.remote_kc is not None and _exec_mgr.kernel_health()[0]:
        return True
    for attempt in range(3):
        try:
            if _exec_mgr.setup_remote():
                return True
            # setup_remote returned False: a config/connectivity problem it
            # already explained. Retrying won't fix it — stop here.
            return False
        except Exception as e:
            print(f"Attempt {attempt+1}/3 failed: {e}")
            if attempt < 2:
                time.sleep(5)
    print("Failed to connect after 3 attempts")
    return False

@register_line_magic
def gpu(line):
    if _ensure_connected():
        ROUTER.set(PY_BACKEND)

@register_line_magic
def gpum(line):
    if _ensure_connected():
        ROUTER.set(MOJO_BACKEND)

@register_line_magic
def local(line):
    ROUTER.set(None)

@register_line_magic
def restart_kernel(line):
    _exec_mgr.restart_kernel()

@register_line_magic
def restart_mojo(line):
    _mojo_mgr.restart_mojo()

@register_line_magic
def mojo_history(line):
    _mojo_mgr.show_history()

@register_line_magic
def mojo_run(line):
    _mojo_mgr.show_run()

@register_line_magic
def bench(line):
    # %bench [N]  — compile the last Mojo program once, then time N runs (default 20).
    arg = line.strip()
    _mojo_mgr.bench(int(arg) if arg.isdigit() else 20)

@register_line_magic
def mojo_add(line):
    # %mojo_add <pkg> — pixi add a Mojo (or Python) package into the GPU container.
    _mojo_mgr.add_package(line.strip())

@register_line_magic
def kernel_status(line):
    mode = ("mojo (GPU)" if ROUTER.backend is MOJO_BACKEND
            else "python (GPU)" if ROUTER.backend is PY_BACKEND else "local")
    print("=" * 40)
    print("KERNEL STATUS")
    print("=" * 40)
    print(f"Client:         {CLIENT_NAME}")
    print(f"Execution mode: {mode}")
    print(f"Connected:      {'yes' if _exec_mgr.remote_kc else 'no'}")
    if _exec_mgr.remote_kc:
        ok, detail = _exec_mgr.kernel_health()
        print(f"Kernel health:  {'OK' if ok else 'FAIL'} ({detail})")
        try:
            info = fetch_kernel_info()
            reachable = _exec_mgr._test_connection(info)
            print(f"Tunnel ports:   {'open' if reachable else 'closed'}")
        except Exception:
            print("Tunnel ports:   unknown")
    gpus = gpu_status()
    if gpus:
        print("GPU:")
        for g in gpus:
            print(f"  {g}")
    else:
        print("GPU:            (nvidia-smi unavailable)")
    print("=" * 40)

# ── Auto-connect ──────────────────────────────────────────────────────────────
# Function-call form, not a bare `%gpu`: keeps this file valid Python so it loads
# via %run / exec / run_cell alike (a bare magic line SyntaxErrors under %run).
get_ipython().run_line_magic("gpu", "")

print("CRAFT ready")
print("  %gpu             Python cells on the GPU kernel")
print("  %gpum            Mojo cells compiled & run on the GPU (same container)")
print("  %local           run cells locally in this notebook again")
print("  %restart_kernel  restart the remote Python kernel")
print("  %restart_mojo    clear Mojo history + generated source")
print("  %mojo_history    show accumulated Mojo cells")
print("  %mojo_run        show the last generated Mojo source")
print("  %mojo_add <pkg>  pixi-add a Mojo/Python package into the GPU container")
print("  %bench [N]       compile last Mojo program once, time N runs (no compile)")
print("  %kernel_status   show mode + connection + GPU status")
