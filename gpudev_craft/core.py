import ast
import html
import json
import platform
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
import os
import shutil

from .client_setup import (
    derive_domain,
    has_ssh_stanza,
    normalize_client_name,
    setup_client,
)
try:
    from IPython.core.magic import register_line_magic
    from IPython.display import HTML, Markdown, display, clear_output

    # display() is a no-op in the fallback below, so anything rendered ONLY
    # through it would vanish outside a notebook. Callers check this flag and
    # keep a printed form for that case.
    _HAS_RICH_DISPLAY = True
except Exception:  # non-notebook import / tests
    _HAS_RICH_DISPLAY = False

    def register_line_magic(fn=None, **_kw):  # type: ignore[misc]
        if fn is None:
            return lambda f: f
        return fn

    def HTML(x):  # type: ignore[misc]
        return x

    def Markdown(x):  # type: ignore[misc]
        return x

    def display(*_a, **_k):
        pass

    def clear_output(**_k):
        pass

try:
    from jupyter_client import BlockingKernelClient
except Exception:
    BlockingKernelClient = None  # type: ignore[misc, assignment]

try:
    from dialoghelper import read_msg          # SolveIt: id of the current cell
except Exception:
    read_msg = None

def get_ipython():  # type: ignore[misc]
    """Always bound on this module so addons can call ``core.get_ipython()``."""
    try:
        from IPython import get_ipython as _gi

        return _gi()
    except Exception:
        return None


# ── Notebook-local client selection ──────────────────────────────────────────
# Each SolveIt notebook has its own Python kernel, so these values are naturally
# scoped to that notebook. ``%gpu <name>`` sets them for the life of the kernel;
# no shared craft.json/default is needed or consulted.
CLIENT_NAME = ""

# Inside every gpudev container the UNIX user is the fixed `gpudev`; the client
# identity lives in the container name and tunnel hostname. Paths are stable.
KERNEL_MANAGER = "/home/gpudev/bin/kernel-manager.sh"
KERNEL_RUNTIME = "/home/gpudev/.local/share/jupyter/runtime/kernel.json"

# SSH alias is derived from client_name — must match what `gpudev client info`
# prints and what client-setup.sh sets as the container hostname.
SSH_HOST = ""

# Remote ports inside the gpudev container.
REMOTE_KERNEL_PORTS = {
    "shell_port":   54100,
    "iopub_port":   54101,
    "stdin_port":   54102,
    "control_port": 54103,
    "hb_port":      54104,
}

# Local ports on the SolveIt side. These are what the local BlockingKernelClient
# connects to after SSH forwards 127.0.0.1:LOCAL -> 127.0.0.1:REMOTE.
# Automate port selection in case of port conflict or busy port
def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _find_free_kernel_ports(start=60000, stop=65000, step=100):
    names = ("shell_port", "iopub_port", "stdin_port", "control_port", "hb_port")
    for base in range(start, stop, step):
        ports = {name: base + i for i, name in enumerate(names)}
        if all(_port_free(p) for p in ports.values()):
            return ports
    raise RuntimeError(f"No free 5-port kernel block found in {start}-{stop}")


KERNEL_PORTS = _find_free_kernel_ports()


# Outer connect attempts for flaky tunnels (Cloudflare blips). Each attempt runs
# full setup_remote (SSH + attach + HMAC heal). Override: GPUDEV_CONNECT_ATTEMPTS=1
CONNECT_ATTEMPTS = max(1, int(os.environ.get("GPUDEV_CONNECT_ATTEMPTS", "3")))

CLOUDFLARED_PATH = Path(
    os.environ.get("CLOUDFLARED_PATH")
    or shutil.which("cloudflared")
    or (Path.home() / ".local" / "bin" / "cloudflared")
)

_cf_dir = str(CLOUDFLARED_PATH.parent)
if _cf_dir not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _cf_dir + os.pathsep + os.environ.get("PATH", "")

# ── Helpers ───────────────────────────────────────────────────────────────────
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\[[0-9;]*$|\x1b$")


def _strip_ansi(text):
    return ANSI_RE.sub("", text)


# cloudflared accepts the local connection, then finds no origin behind it.
# Seen when a client's DNS route has not propagated yet, or while the connector
# restarts to pick up new ingress — both transient, both clear in about a
# minute. Distinguished from a real misconfiguration, which fails earlier with
# "Could not resolve hostname" or a ProxyCommand error.
_TUNNEL_NOT_READY_SIGNS = (
    "kex_exchange_identification",
    "Connection closed by UNKNOWN",
    "websocket: bad handshake",
    "Connection closed by remote host",
)
_TUNNEL_SETTLE_ATTEMPTS = 4
_TUNNEL_SETTLE_DELAY = 10


def _looks_like_tunnel_not_ready(stderr: str) -> bool:
    return any(sign in (stderr or "") for sign in _TUNNEL_NOT_READY_SIGNS)


def select_client(raw_name: str, *, quiet: bool = False) -> str:
    """Select one remote identity for this notebook kernel.

    ``quiet`` suppresses the confirmation line. %gpu_setup already names the
    client in its own report, and printing after that report put a stray stream
    line inside the trailing code block the notebook had just rendered.
    """
    global CLIENT_NAME, SSH_HOST, _exec_mgr

    name = normalize_client_name(raw_name)
    if name == CLIENT_NAME:
        return name

    if _exec_mgr is not None:
        try:
            _exec_mgr.shutdown_remote()
        except Exception:
            pass

    if ROUTER is not None and ROUTER.backend is not None:
        ROUTER.set(None)

    CLIENT_NAME = name
    SSH_HOST = f"gpudev-{name}"
    _inject_user_ns()
    if not quiet:
        print(f"Selected gpudev client '{name}' for this notebook")
    return name


# Shell helper only for non-SSH one-liners (e.g. cloudflared install).
def _run_shell(cmd, check=True, capture_output=False):
    return subprocess.run(
        cmd,
        shell=True,
        check=check,
        capture_output=capture_output,
        text=True,
    )


SSH_OPT_LIST = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
]

if not sys.platform.startswith("win"):
    SSH_OPT_LIST += [
        "-o", "ControlMaster=auto",
        "-o", "ControlPath=~/.ssh/craft-%C",
        "-o", "ControlPersist=300",
    ]

# String form for pcviz / external tools that shlex-split SSH_OPTS.
SSH_OPTS = " ".join(SSH_OPT_LIST)

FORWARD_OPT_LIST = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
    "-o", "ControlMaster=no",
    "-o", "ControlPath=none",
    "-o", "ExitOnForwardFailure=yes",
]


def _is_host_key_changed(stderr: str) -> bool:
    """True if ssh failed because a known host key no longer matches."""
    s = stderr or ""
    return (
        "REMOTE HOST IDENTIFICATION HAS CHANGED" in s
        or "Host key verification failed" in s
    )


def _clear_stale_host_keys(stderr: str = "") -> None:
    """Remove known_hosts entries for this client after a container key rotation.

    Safe for personal lab use: only clears hosts derived from the SSH config for
    SSH_HOST plus any host/path named in the ssh error text. Retries once at the
    call site with accept-new so the new fingerprint is recorded.
    """
    hosts = set()
    paths = set()
    err = stderr or ""

    for m in re.finditer(r'ssh-keygen\s+-f\s+"([^"]+)"\s+-R\s+"([^"]+)"', err):
        paths.add(m.group(1))
        hosts.add(m.group(2))
    for m in re.finditer(r"ssh-keygen\s+-f\s+(\S+)\s+-R\s+(\S+)", err):
        paths.add(m.group(1).strip('"'))
        hosts.add(m.group(2).strip('"'))
    for m in re.finditer(r"Offending \S+ key in ([^:\n]+):", err):
        paths.add(m.group(1).strip())
    for m in re.finditer(r"Add correct host key in ([^\s]+) to get rid", err):
        paths.add(m.group(1).strip())
    for m in re.finditer(r"Host key for ([^\s]+) has changed", err):
        hosts.add(m.group(1).strip())

    if SSH_HOST:
        hosts.add(SSH_HOST)

    # Resolve HostName + UserKnownHostsFile from the user's ssh config.
    if SSH_HOST:
        try:
            r = subprocess.run(
                ["ssh", "-G", SSH_HOST],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            for line in (r.stdout or "").splitlines():
                low = line.lower()
                if low.startswith("hostname "):
                    hosts.add(line.split(None, 1)[1].strip())
                elif low.startswith("userknownhostsfile "):
                    for p in line.split()[1:]:
                        if p and p != "/dev/null":
                            paths.add(os.path.expanduser(p))
        except Exception:
            pass

    # Common notebook locations (SolveIt often uses /app/data/.ssh).
    paths.add(str(Path.home() / ".ssh" / "known_hosts"))
    paths.add("/app/data/.ssh/known_hosts")

    if not hosts:
        return

    print(
        "SSH host key changed (container rebuild or re-provision). "
        "Clearing stale known_hosts entries and retrying once…"
    )
    for host in sorted(hosts):
        for path in sorted(paths):
            if path and Path(path).is_file():
                subprocess.run(
                    ["ssh-keygen", "-f", path, "-R", host],
                    capture_output=True,
                    text=True,
                    check=False,
                )
        # Default known_hosts as well.
        subprocess.run(
            ["ssh-keygen", "-R", host],
            capture_output=True,
            text=True,
            check=False,
        )


def _ssh(cmd, capture_output=False, check=True, _hostkey_retried=False):
    """Run a command inside the client's container via SSH (no local shell).

    On host-key mismatch (common after client rebuild before persistent keys
    were enabled, or after volume wipe), clear known_hosts once and retry.
    """
    if not SSH_HOST:
        raise RuntimeError(
            "No gpudev client selected — use %gpu <client-name>"
        )
    normalize_client_name(CLIENT_NAME)

    # Use ``export …; cmd`` so compound scripts (if/then, &&) work.
    # ``GPUDEV_CLIENT=x if …`` is a bash syntax error (Mojo pixi seed path).
    wrapped = f"export GPUDEV_CLIENT={CLIENT_NAME}; {cmd}"
    # Always capture so we can detect host-key errors; re-emit stdout when the
    # caller did not ask for capture.
    result = subprocess.run(
        ["ssh", *SSH_OPT_LIST, SSH_HOST, wrapped],
        check=False,
        capture_output=True,
        text=True,
    )

    if (
        result.returncode != 0
        and not _hostkey_retried
        and _is_host_key_changed(result.stderr or "")
    ):
        _clear_stale_host_keys(result.stderr or "")
        return _ssh(
            cmd,
            capture_output=capture_output,
            check=check,
            _hostkey_retried=True,
        )

    if not capture_output and result.stdout:
        print(result.stdout, end="")
    if not capture_output and result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            ["ssh", SSH_HOST, wrapped],
            output=result.stdout,
            stderr=result.stderr,
        )
    return result


def _ssh_with_input(remote_cmd, input_text, check=True, _hostkey_retried=False):
    """SSH with stdin payload (Mojo source upload). Host-key auto-clear once."""
    if not SSH_HOST:
        raise RuntimeError("No gpudev client selected — use %gpu <client-name>")
    normalize_client_name(CLIENT_NAME)
    # Same export prefix as _ssh (compound remote commands + client env)
    wrapped = f"export GPUDEV_CLIENT={CLIENT_NAME}; {remote_cmd}"
    result = subprocess.run(
        ["ssh", *SSH_OPT_LIST, SSH_HOST, wrapped],
        input=input_text,
        text=True,
        check=False,
        capture_output=True,
    )
    if (
        result.returncode != 0
        and not _hostkey_retried
        and _is_host_key_changed(result.stderr or "")
    ):
        _clear_stale_host_keys(result.stderr or "")
        return _ssh_with_input(
            remote_cmd, input_text, check=check, _hostkey_retried=True
        )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            ["ssh", SSH_HOST, wrapped],
            output=result.stdout,
            stderr=result.stderr,
        )
    return result


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

    machine = platform.machine().lower()
    arch = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if not arch:
        print(f"cloudflared auto-install does not support architecture {machine!r}.")
        print("Install it manually: https://developers.cloudflare.com/cloudflared/install/")
        return False

    print(f"cloudflared not found — downloading a local {arch} copy...")
    CLOUDFLARED_PATH.parent.mkdir(parents=True, exist_ok=True)
    url = (
        "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        f"cloudflared-linux-{arch}"
    )
    tmp_path = CLOUDFLARED_PATH.with_name(CLOUDFLARED_PATH.name + ".download")

    try:
        subprocess.run(["curl", "-fsSL", url, "-o", str(tmp_path)], check=True)
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, CLOUDFLARED_PATH)
    except Exception as e:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        print(f"Could not install cloudflared automatically: {e}")
        print("Install it manually: https://developers.cloudflare.com/cloudflared/install/")
        return False

    return True


# ── Kernel Management ─────────────────────────────────────────────────────────
def ensure_kernel(force_restart=False):
    """Start the kernel, or force a fresh-key restart, inside the container."""
    _ssh(f"{KERNEL_MANAGER} {'restart' if force_restart else 'start'}")


def kernel_doctor():
    """Return the container-side kernel diagnostics as text."""
    try:
        result = _ssh(f"{KERNEL_MANAGER} doctor", capture_output=True)
        return result.stdout
    except Exception as e:
        return f"(could not run kernel doctor: {e})"


def gpu_status():
    """Return a list of per-GPU summary strings from the container."""
    query = (
        "nvidia-smi --query-gpu=index,name,memory.used,memory.total,"
        "utilization.gpu,temperature.gpu --format=csv,noheader,nounits"
    )

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
    """Read the remote connection file, but point the client at local forwards."""
    result = _ssh(f"cat {KERNEL_RUNTIME}", capture_output=True)
    info = json.loads(result.stdout)

    # Local forwarded listeners are what the local client must connect to.
    info.update(KERNEL_PORTS)
    info["ip"] = "127.0.0.1"
    return info


def start_port_forwarding(kernel_info):
    """SSH-tunnel the kernel's remote ZMQ ports to local forwarded ports."""
    args = ["ssh", "-N", *FORWARD_OPT_LIST]

    for name, remote_port in REMOTE_KERNEL_PORTS.items():
        local_port = kernel_info[name]
        args.extend(["-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}"])

    args.append(SSH_HOST)

    errf = tempfile.NamedTemporaryFile(prefix="craft-fwd-", suffix=".log", delete=False)
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=errf)
    proc.craft_stderr_path = errf.name
    errf.close()
    return proc


def _ports_to_inodes(ports):
    """Map socket inode -> port for loopback listeners/bound-idle sockets."""
    want = set(ports)
    inodes = {}

    for fn in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = Path(fn).read_text().splitlines()[1:]
        except Exception:
            continue

        for ln in rows:
            f = ln.split()
            if len(f) < 10:
                continue

            try:
                lport = int(f[1].rsplit(":", 1)[1], 16)
                rport = int(f[2].rsplit(":", 1)[1], 16)
            except Exception:
                continue

            if lport in want and rport == 0:
                inodes[f[9]] = lport

    return inodes


def _pids_holding_ports(ports, only_ssh=True):
    """PIDs bound to any of `ports` on loopback, excluding this process."""
    inodes = _ports_to_inodes(ports)
    if not inodes:
        return []

    me = os.getpid()
    pids = set()

    try:
        entries = os.listdir("/proc")
    except OSError:
        return []

    for e in entries:
        if not e.isdigit() or int(e) == me:
            continue

        if only_ssh:
            try:
                if Path(f"/proc/{e}/comm").read_text().strip() != "ssh":
                    continue
            except Exception:
                continue

        try:
            for fd in os.listdir(f"/proc/{e}/fd"):
                try:
                    tgt = os.readlink(f"/proc/{e}/fd/{fd}")
                except OSError:
                    continue

                if tgt.startswith("socket:[") and tgt[8:-1] in inodes:
                    pids.add(int(e))
                    break
        except OSError:
            continue

    return sorted(pids)


def _reap_local_forwards(ports):
    """Free local forward ports from stale holders."""
    pids = (
        _pids_holding_ports(ports, only_ssh=True)
        or _pids_holding_ports(ports, only_ssh=False)
    )

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    if pids:
        time.sleep(0.3)
        for pid in _pids_holding_ports(ports, only_ssh=False):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def _diagnose_port_holders(ports):
    """Human-readable account of what /proc reveals about our forward ports."""
    out = []
    rd = []

    for fn in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rd.append(f"{fn}={len(Path(fn).read_text().splitlines()) - 1} rows")
        except Exception as e:
            rd.append(f"{fn} UNREADABLE({type(e).__name__})")

    out.append("  /proc/net: " + ", ".join(rd))

    inodes = _ports_to_inodes(ports)
    out.append(
        "  bound-no-peer ports: "
        + (
            ", ".join(
                f"{p}(inode {i})"
                for i, p in sorted(inodes.items(), key=lambda x: x[1])
            )
            or "NONE FOUND (ports held outside this proc's /proc/net view)"
        )
    )

    found = False

    try:
        entries = [e for e in os.listdir("/proc") if e.isdigit()]
    except OSError as e:
        out.append(f"  /proc listing UNREADABLE({type(e).__name__})")
        return "\n".join(out)

    for e in entries:
        try:
            fds = os.listdir(f"/proc/{e}/fd")
        except OSError:
            continue

        for fd in fds:
            try:
                tgt = os.readlink(f"/proc/{e}/fd/{fd}")
            except OSError:
                continue

            if tgt.startswith("socket:[") and tgt[8:-1] in inodes:
                comm = uid = cmd = "?"

                try:
                    comm = Path(f"/proc/{e}/comm").read_text().strip()
                except Exception:
                    pass

                try:
                    uid = os.stat(f"/proc/{e}").st_uid
                except Exception:
                    pass

                try:
                    cmd = (
                        Path(f"/proc/{e}/cmdline")
                        .read_bytes()
                        .replace(b"\x00", b" ")
                        .decode("utf-8", "replace")
                        .strip()
                    )
                except Exception:
                    pass

                out.append(
                    f"  holder: port {inodes[tgt[8:-1]]} pid {e} "
                    f"comm={comm} uid={uid} cmd={cmd[:140]}"
                )
                found = True
                break

    if inodes and not found:
        out.append("  holder PID not found (socket owned by another pid-ns or /proc/*/fd hidden)")

    out.append(f"  self: pid {os.getpid()} uid {os.getuid()}")
    return "\n".join(out)


# ── Output Display ────────────────────────────────────────────────────────────
_PIP_RAW_PROGRESS_RE = re.compile(r"^Progress\s+(\d+)\s+of\s+(\d+)\s*$", re.I)
_PERCENT_PROGRESS_RE = re.compile(r"(?<!\d)(100(?:\.0+)?|\d{1,2}(?:\.\d+)?)%")
_CLOCK_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")
_URL_RE = re.compile(r"https?://[^\s'\"]+")


def _format_elapsed(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _format_bytes(value):
    value = float(max(0, value))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{value:.0f} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024


def _format_clock_label(value):
    match = _CLOCK_RE.fullmatch(value.strip())
    if not match:
        return value
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def _curl_quantity(value):
    """Convert curl meter quantities such as 351M or 9759k to readable bytes."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([kMGT]?)", value.strip())
    if not match:
        return value
    amount = float(match.group(1))
    multiplier = {"": 1, "k": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    return _format_bytes(amount * multiplier[match.group(2)])


def _parse_curl_progress(value):
    """Parse curl's 12-column transfer meter into a small labeled summary."""
    fields = value.split()
    if len(fields) != 12:
        return None
    try:
        percentages = [int(fields[index]) for index in (0, 2, 4)]
    except ValueError:
        return None
    if any(percent < 0 or percent > 100 for percent in percentages):
        return None
    if not all(_CLOCK_RE.fullmatch(fields[index]) for index in (8, 9, 10)):
        return None
    return {
        "percent": percentages[1],
        "total": _curl_quantity(fields[1]),
        "downloaded": _curl_quantity(fields[3]),
        "speed": _curl_quantity(fields[11]),
        "remaining": _format_clock_label(fields[10]),
    }


def _infer_job_label(code):
    code = code or ""
    url_match = _URL_RE.search(code)
    if url_match:
        filename = url_match.group(0).split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if filename:
            return f"Downloading {filename}"
    if re.search(
        r"(?:^|\s)(?:!?pip|%pip|!?python\s+-m\s+pip)\s+install\b",
        code,
    ):
        return "Installing Python packages"
    if _is_import_only_code(code):
        return "Loading Python packages"
    return ""


def _infer_epoch_total(code):
    """Infer a fixed epoch count from common ``for epoch in range(...)`` cells."""
    try:
        tree = ast.parse(code or "")
    except (SyntaxError, ValueError, TypeError):
        return None

    constants = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant) and (
                isinstance(node.value.value, int) and not isinstance(node.value.value, bool)
            ):
                constants[target.id] = node.value.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, int)
            and not isinstance(node.value.value, bool)
        ):
            constants[node.target.id] = node.value.value

    def integer_value(node):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, int)
            and not isinstance(node.value, bool)
        ):
            return node.value
        if isinstance(node, ast.Name):
            return constants.get(node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = integer_value(node.operand)
            if value is not None:
                return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
            left = integer_value(node.left)
            right = integer_value(node.right)
            if left is not None and right is not None:
                return left + right if isinstance(node.op, ast.Add) else left - right
        return None

    totals = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.AsyncFor)):
            continue
        if not isinstance(node.target, ast.Name) or "epoch" not in node.target.id.lower():
            continue
        iterator = node.iter
        if not (
            isinstance(iterator, ast.Call)
            and isinstance(iterator.func, ast.Name)
            and iterator.func.id == "range"
            and 1 <= len(iterator.args) <= 3
        ):
            continue
        values = [integer_value(arg) for arg in iterator.args]
        if any(value is None for value in values):
            continue
        try:
            total = len(range(*values))
        except (TypeError, ValueError):
            continue
        if total > 0:
            totals.append(total)
    return max(totals) if totals else None


def _is_import_only_code(code):
    """Whether a cell contains only Python import statements and comments."""
    try:
        body = ast.parse(code or "").body
    except (SyntaxError, ValueError, TypeError):
        return False
    return bool(body) and all(isinstance(node, (ast.Import, ast.ImportFrom)) for node in body)


def _summarize_terminal_progress(value, percent):
    parts = [f"Progress: {percent:g}%"]
    count_match = re.search(r"(?<![\d:])(\d[\d,]*)/(\d[\d,]*)(?![\d:])", value)
    if count_match:
        parts.append(f"Items: {count_match.group(1)} / {count_match.group(2)}")
    timing_match = re.search(r"\[([0-9:]+)<([0-9:]+)(?:,\s*([^\]]+))?\]", value)
    if timing_match:
        parts.append(f"Remaining: {_format_clock_label(timing_match.group(2))}")
        if timing_match.group(3):
            parts.append(f"Rate: {timing_match.group(3).strip()}")
    return " · ".join(parts)


def _is_structured_terminal_progress(value):
    """True only for terminal redraws CRAFT knows how to summarize safely."""
    value = value.strip()
    return bool(
        _PIP_RAW_PROGRESS_RE.fullmatch(value)
        or _parse_curl_progress(value)
        or _PERCENT_PROGRESS_RE.search(value)
    )


def _is_curl_meter_header(value):
    """Recognize curl's two heading rows so they do not become orphaned output."""
    words = value.split()
    return (
        words == ["Dload", "Upload", "Total", "Spent", "Left", "Speed"]
        or (
            "% Total" in value
            and "% Received" in value
            and "% Xferd" in value
            and "Average Speed" in value
            and "Current" in value
        )
    )


class _HybridOutputRenderer:
    """Render remote IOPub output without flooding the SolveIt cell.

    Normal lines remain ordinary notebook output. Terminal-style progress
    (carriage-return redraws and pip's ``--progress-bar=raw`` protocol) is
    collapsed into one local HTML ``<progress>`` display. A delayed status
    thread makes otherwise-silent remote work visibly alive.
    """

    def __init__(self, status_delay=1.0, status_interval=1.0, code=""):
        self.status_delay = max(0, float(status_delay))
        self.status_interval = max(0.1, float(status_interval))
        self.started_at = time.monotonic()
        self.last_activity_at = self.started_at
        self.display_id = f"gpudev-progress-{uuid.uuid4().hex}"
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread = None
        self._status_visible = False
        self._external_progress = False
        self._native_progress = False
        self._saw_normal_output = False
        self._saw_error = False
        self._finished = False
        self._stream_buffers = {"stdout": "", "stderr": ""}
        self._last_log_line = ""
        self._job_label = _infer_job_label(code)
        self._is_curl_job = bool(re.search(r"(?:^|\s)!?curl(?:\s|$)", code or ""))
        self._is_import_only = _is_import_only_code(code)
        self._epoch_total = _infer_epoch_total(code)
        self._epoch_current = None
        self._epoch_progress_visible = False
        self._is_epoch_job = self._epoch_total is not None or bool(
            re.search(r"\bepochs?\b", code or "", re.IGNORECASE)
        )
        self._progress_current = None
        self._progress_total = None
        self._progress_unit = None
        self._progress_text = ""
        self._rate = None
        self._rate_sample = None

    @property
    def saw_error(self):
        return self._saw_error

    def start(self):
        if get_ipython() is None:
            return
        # A generic indeterminate bar is misleading for epoch loops. Start one
        # determinate bar at 0/N, advance it from printed Epoch lines, and turn
        # it into the final elapsed-time summary in finish().
        if self._is_epoch_job:
            if self._epoch_total:
                with self._lock:
                    self._publish_epoch_progress_locked()
            return
        self._thread = threading.Thread(
            target=self._status_loop,
            name="gpudev-output-status",
            daemon=True,
        )
        self._thread.start()

    def _status_loop(self):
        if self._stop.wait(self.status_delay):
            return
        while not self._stop.is_set():
            with self._lock:
                if self._stop.is_set():
                    return
                if not self._external_progress:
                    self._publish_running_locked()
            if self._stop.wait(self.status_interval):
                return

    def _publish(self, html_value, plain_value, update=None):
        ip = get_ipython()
        publisher = getattr(ip, "display_pub", None) if ip is not None else None
        if publisher is None:
            return False
        if update is None:
            update = self._status_visible
        try:
            publisher.publish(
                data={"text/html": html_value, "text/plain": plain_value},
                metadata={},
                transient={"display_id": self.display_id},
                update=bool(update),
            )
            self._status_visible = True
            return True
        except Exception:
            # Progress UI must never turn a successful remote job into a local
            # execution failure. Ordinary output still flows through below.
            return False

    def _progress_label(self):
        if self._job_label:
            return self._job_label
        if self._last_log_line.lower().startswith(("downloading", "collecting")):
            return self._last_log_line[:240]
        if self._progress_unit == "bytes":
            return "Downloading packages on GPU"
        return "GPU job in progress"

    def _running_markup_locked(self):
        now = time.monotonic()
        elapsed = _format_elapsed(now - self.started_at)
        idle = max(0, int(now - self.last_activity_at))
        activity = "Last output: now" if idle < 2 else f"Last output: {idle}s ago"
        label = html.escape(self._progress_label())

        progress_attrs = ""
        detail = ""
        if self._progress_current is not None and self._progress_total:
            current = max(0, min(self._progress_current, self._progress_total))
            total = self._progress_total
            progress_attrs = f' value="{current}" max="{total}"'
            percent = 100 * current / total
            if self._progress_unit == "bytes":
                detail = (
                    f"Progress: {percent:.1f}% · "
                    f"Downloaded: {_format_bytes(current)} / {_format_bytes(total)}"
                )
                if self._rate:
                    detail += f" · Speed: {_format_bytes(self._rate)}/s"
                    if current < total:
                        detail += f" · Remaining: {_format_elapsed((total - current) / self._rate)}"
            else:
                detail = self._progress_text or f"{percent:.1f}%"
        elif self._progress_text:
            detail = self._progress_text

        detail_html = (
            f'<div style="margin-top:.3rem">{html.escape(detail)}</div>'
            if detail
            else ""
        )
        markup = (
            '<div role="status" aria-live="polite" style="font:13px/1.4 system-ui,sans-serif;'
            'padding:.45rem .65rem;border:1px solid #d0d7de;border-radius:6px;max-width:760px">'
            f'<div style="display:flex;justify-content:space-between;gap:1rem;margin-bottom:.3rem">'
            f'<strong>{label}</strong><span>Elapsed: {elapsed} · {activity}</span></div>'
            f'<progress{progress_attrs} style="width:100%;vertical-align:middle"></progress>'
            f'{detail_html}</div>'
        )
        plain = f"{self._progress_label()} — {detail or activity} ({elapsed})"
        return markup, plain

    def _publish_running_locked(self):
        markup, plain = self._running_markup_locked()
        self._publish(markup, plain)

    def _hide_status_locked(self):
        if self._status_visible:
            self._publish("", "", update=True)
            self._status_visible = False

    def _emit_normal_stream_locked(self, value):
        """Show ordinary command output after dismissing a generic status card."""
        if not value:
            return
        self._saw_normal_output = True
        if not self._native_progress and not self._epoch_progress_visible:
            self._hide_status_locked()
        print(value, end="")

    def _set_byte_progress_locked(self, current, total):
        now = time.monotonic()
        sample = self._rate_sample
        if sample and sample[2] == total and current >= sample[1]:
            delta_t = now - sample[0]
            if delta_t >= 0.05 and current > sample[1]:
                instantaneous = (current - sample[1]) / delta_t
                self._rate = instantaneous if self._rate is None else (0.7 * self._rate + 0.3 * instantaneous)
        else:
            self._rate = None
        self._rate_sample = (now, current, total)
        self._progress_current = current
        self._progress_total = total
        self._progress_unit = "bytes"
        self._progress_text = ""
        self._native_progress = True
        self._publish_running_locked()

    def _set_terminal_progress_locked(self, value):
        value = value.strip()
        curl_progress = _parse_curl_progress(value)
        if curl_progress:
            self._progress_current = float(curl_progress["percent"])
            self._progress_total = 100.0
            self._progress_unit = "percent"
            self._progress_text = (
                f'Progress: {curl_progress["percent"]}% · '
                f'Downloaded: {curl_progress["downloaded"]} / {curl_progress["total"]} · '
                f'Speed: {curl_progress["speed"]}/s · '
                f'Remaining: {curl_progress["remaining"]}'
            )
            self._native_progress = True
            self._publish_running_locked()
            return

        percent_match = _PERCENT_PROGRESS_RE.search(value)
        if percent_match:
            percent = float(percent_match.group(1))
            self._progress_current = percent
            self._progress_total = 100.0
            self._progress_unit = "percent"
            self._progress_text = _summarize_terminal_progress(value, percent)
        else:
            self._progress_current = None
            self._progress_total = None
            self._progress_unit = None
            self._progress_text = "Receiving data; total size is not available"
        self._native_progress = True
        self._publish_running_locked()

    def _consume_record_locked(self, record, had_carriage_return=False):
        revisions = record.split("\r")
        candidate = next((part.strip() for part in reversed(revisions) if part.strip()), "")
        if self._is_curl_job and _is_curl_meter_header(candidate):
            return None
        pip_match = _PIP_RAW_PROGRESS_RE.fullmatch(candidate)
        if pip_match:
            self._set_byte_progress_locked(int(pip_match.group(1)), int(pip_match.group(2)))
            return None
        epoch_match = re.match(
            r"^\s*Epoch\s+(\d+)(?:\s*/\s*(\d+))?\s*(?::|-)",
            candidate,
            re.IGNORECASE,
        )
        if epoch_match:
            self._is_epoch_job = True
            self._epoch_current = int(epoch_match.group(1))
            if epoch_match.group(2):
                self._epoch_total = int(epoch_match.group(2))
            self._publish_epoch_progress_locked()
        if had_carriage_return or len(revisions) > 1:
            if candidate and _is_structured_terminal_progress(candidate):
                self._set_terminal_progress_locked(candidate)
                return None
            # Remote shell streams commonly use CRLF. Unknown carriage-return
            # text is command output, not a progress protocol; preserve the
            # final terminal revision instead of silently discarding it.
            return candidate + "\n" if candidate else None

        if candidate:
            self._last_log_line = candidate
        return record + "\n"

    def _handle_stream(self, name, text):
        # Normalize complete CRLF records before interpreting bare carriage
        # returns as terminal redraws. ``\r`` and ``\n`` can still arrive in
        # separate IOPub messages; the buffering branch below handles that.
        text = re.sub(r"\r+\n", "\n", _strip_ansi(text or ""))
        if not text:
            return
        name = name if name in self._stream_buffers else "stdout"
        with self._lock:
            self.last_activity_at = time.monotonic()
            combined = self._stream_buffers[name] + text
            self._stream_buffers[name] = ""
            normal_output = []

            while "\n" in combined:
                record, combined = combined.split("\n", 1)
                rendered = self._consume_record_locked(record, "\r" in record)
                if rendered is not None:
                    normal_output.append(rendered)

            # One local write per remote IOPub message keeps large log bursts
            # responsive without dropping their content.
            if normal_output:
                self._emit_normal_stream_locked("".join(normal_output))

            if "\r" in combined:
                candidate = next(
                    (part.strip() for part in reversed(combined.split("\r")) if part.strip()),
                    "",
                )
                if candidate and _is_structured_terminal_progress(candidate):
                    self._set_terminal_progress_locked(candidate)
                    # Retain the CR marker so a later newline is recognized as
                    # the final redraw rather than printed as a duplicate.
                    self._stream_buffers[name] = f"\r{candidate}"
                else:
                    # Likely a CRLF split across messages. Wait for the newline
                    # (or finish()) before deciding, so ordinary CLI text wins.
                    self._stream_buffers[name] = combined
                return

            stripped = combined.strip()
            pip_match = _PIP_RAW_PROGRESS_RE.fullmatch(stripped)
            if pip_match:
                self._set_byte_progress_locked(int(pip_match.group(1)), int(pip_match.group(2)))
            elif combined and ("Progress ".startswith(combined) or combined.startswith("Progress ")):
                # Pip may split its raw protocol across IOPub messages.
                self._stream_buffers[name] = combined
            elif combined:
                # Preserve the old immediate behavior for explicit flushes that
                # do not end in a newline; only structured progress is buffered.
                self._emit_normal_stream_locked(combined)

    def _publish_epoch_progress_locked(self, outcome=None):
        elapsed = _format_elapsed(time.monotonic() - self.started_at)
        current = self._epoch_current or 0
        total = self._epoch_total
        if outcome == "completed":
            if total and not current:
                current = total
            elif current:
                total = total or current

        final_labels = {
            "completed": ("#1a7f37", "Training complete"),
            "failed": ("#cf222e", "Training failed"),
            "interrupted": ("#9a6700", "Training interrupted"),
        }
        color, label = final_labels.get(outcome, ("#0969da", "Training epochs"))
        progress = ""
        epoch_detail = ""
        if total:
            bounded = max(0, min(current, total))
            progress = (
                f'<progress value="{bounded}" max="{total}" '
                'style="display:block;width:100%;margin-top:.4rem;vertical-align:middle;accent-color:'
                f'{color}"></progress>'
            )
            epoch_detail = f"Epochs completed: {bounded} / {total}"
        elif current:
            epoch_detail = f"Epochs completed: {current}"

        detail = (
            f'<div style="margin-top:.35rem">{html.escape(epoch_detail)}</div>'
            if epoch_detail
            else ""
        )
        markup = (
            '<div role="status" style="font:13px/1.4 system-ui,sans-serif;'
            'padding:.45rem .65rem;border:1px solid #d0d7de;border-radius:6px;max-width:760px">'
            f'<strong style="color:{color}">{label}</strong>'
            f'{progress}{detail}'
            f'<div style="margin-top:.2rem">'
            f'{"Total elapsed" if outcome else "Elapsed"}: {elapsed}</div></div>'
        )
        plain_detail = f" — {epoch_detail}" if epoch_detail else ""
        self._publish(
            markup,
            f'{label}{plain_detail} — {"Total elapsed" if outcome else "Elapsed"}: {elapsed}',
            update=self._epoch_progress_visible,
        )
        self._epoch_progress_visible = True

    def handle(self, msg):
        msg_type = msg.get("msg_type", "")
        content = msg.get("content", {})
        self.last_activity_at = time.monotonic()

        if msg_type == "stream":
            self._handle_stream(content.get("name", "stdout"), content.get("text", ""))
            return

        if msg_type == "error":
            self._saw_error = True

        if msg_type == "clear_output":
            with self._lock:
                self._status_visible = False

        if msg_type in ("display_data", "update_display_data"):
            data = content.get("data", {})
            rich_html = str(data.get("text/html", "")).lower()
            if "<progress" in rich_html:
                with self._lock:
                    self._external_progress = True
                    if not self._native_progress:
                        self._hide_status_locked()
            else:
                with self._lock:
                    self._saw_normal_output = True
                    if not self._native_progress:
                        self._hide_status_locked()

        if msg_type == "execute_result":
            with self._lock:
                self._saw_normal_output = True
                if not self._native_progress:
                    self._hide_status_locked()

        _handle_output(msg)

    def finish(self, outcome="completed"):
        with self._lock:
            if self._finished:
                return
            self._finished = True
            self._stop.set()
            for name, pending in self._stream_buffers.items():
                if not pending:
                    continue
                if _PIP_RAW_PROGRESS_RE.fullmatch(pending.strip()) or "\r" in pending:
                    rendered = self._consume_record_locked(pending, "\r" in pending)
                    if rendered is not None:
                        self._emit_normal_stream_locked(rendered)
                else:
                    self._emit_normal_stream_locked(pending)
                self._stream_buffers[name] = ""

            if self._is_epoch_job:
                self._publish_epoch_progress_locked(outcome)
                return
            if self._external_progress and not self._native_progress:
                return
            if outcome == "completed" and self._is_import_only:
                # Successful imports are conventionally silent notebook cells.
                # A slow import may show temporary activity, but it should not
                # leave an artificial completion result behind.
                self._hide_status_locked()
                return
            if (
                outcome == "completed"
                and self._saw_normal_output
                and not self._native_progress
            ):
                # For CLI/print/result cells, their output is the useful final
                # state. A completion card can obscure it in SolveIt's display
                # update model, so reserve the card for silent/progress jobs.
                self._hide_status_locked()
                return
            if not self._status_visible:
                return

            elapsed = _format_elapsed(time.monotonic() - self.started_at)
            styles = {
                "completed": ("#1a7f37", "GPU job completed"),
                "failed": ("#cf222e", "GPU job failed"),
                "interrupted": ("#9a6700", "GPU job interrupted"),
            }
            color, label = styles.get(outcome, styles["completed"])
            markup = (
                f'<div role="status" style="font:13px/1.4 system-ui,sans-serif;color:{color};'
                f'padding:.35rem .65rem;border-left:4px solid {color}"><strong>{label}</strong>'
                f' · {elapsed}</div>'
            )
            self._publish(markup, f"{label} — {elapsed}", update=True)


def _handle_output(msg):
    msg_type = msg["msg_type"]
    content = msg.get("content", {})

    if msg_type == "stream":
        print(_strip_ansi(content.get("text", "")), end="")

    elif msg_type == "error":
        tb = "\n".join(content.get("traceback", []))
        display(HTML(f"<pre>{html.escape(_strip_ansi(tb))}</pre>"))

    elif msg_type == "clear_output":
        clear_output(wait=content.get("wait", False))

    elif msg_type in ("display_data", "update_display_data", "execute_result"):
        get_ipython().display_pub.publish(
            data=content.get("data", {}),
            metadata=content.get("metadata", {}),
            transient=content.get("transient", {}),
            update=(msg_type == "update_display_data"),
        )


# ── Remote Execution Manager ──────────────────────────────────────────────────
def _is_kernel_auth_failure(exc, ports_open: bool) -> bool:
    """True when attach failed in a way that often means HMAC / connection-file mismatch.

    Only considered when local ZMQ forward ports are open — otherwise the failure
    is tunnel/SSH, not a stale kernel key. See TROUBLESHOOTING.md (HMAC story).
    """
    if not ports_open:
        return False

    msg = str(exc).lower()
    if any(s in msg for s in ("signature", "hmac", "invalidsignature")):
        return True
    if isinstance(exc, TimeoutError):
        return True
    if any(
        s in msg
        for s in (
            "ready",
            "timeout",
            "timed out",
            "didn't respond",
            "did not respond",
            "kernel didn't",
            "kernel did not",
        )
    ):
        return True
    # jupyter_client wait_for_ready sometimes raises with an empty message.
    if not msg.strip():
        return True
    return False


class RemoteExecutionManager:
    def __init__(self):
        self.remote_kc = None
        self._tunnel_proc = None
        self._hmac_heal_attempted = False

    def _test_connection(self, kernel_info, timeout=3):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex(("127.0.0.1", kernel_info["shell_port"]))
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

    def _attach_kernel_once(self):
        """Fetch connection file, open tunnel, connect ZMQ client (one attempt)."""
        kernel_info = fetch_kernel_info()
        self._ensure_tunnel(kernel_info)
        return self._connect_kernel(kernel_info)

    def _attach_kernel(self, heal_hmac=True):
        """Attach to the remote kernel; optionally self-heal HMAC mismatch once.

        Returns:
            (kernel_client, healed) where healed is True if a forced remote
            kernel restart was performed before a successful attach.
        """
        self._hmac_heal_attempted = False
        last_err = None

        # Soft attempts: preserve remote state; covers flaky tunnel opens.
        for soft in range(2):
            try:
                return self._attach_kernel_once(), False
            except Exception as e:
                last_err = e
                self._kill_stale_forwards()
                ports_open = self._test_connection(KERNEL_PORTS)
                if heal_hmac and _is_kernel_auth_failure(e, ports_open):
                    break
                if soft == 0:
                    time.sleep(0.5)
                    continue
                raise

        # Auth-class failure with open ports: force new kernel + connection key.
        self._hmac_heal_attempted = True
        print(
            "Remote kernel did not accept the connection (likely a stale HMAC key).\n"
            "Restarting the GPU kernel once — variables and loaded models will be cleared."
        )
        ensure_kernel(force_restart=True)
        self._kill_stale_forwards()
        try:
            return self._attach_kernel_once(), True
        except Exception as e2:
            # Prefer the post-restart error (more relevant); chain the pre-heal one.
            raise e2 from last_err

    def _check_ssh(self):
        """Verify we can reach the container over SSH."""
        probe = _ssh("echo SSH_OK", capture_output=True, check=False)

        if probe.returncode == 0 and "SSH_OK" in (probe.stdout or ""):
            return True

        err = (probe.stderr or "").strip()

        # A hostname minted moments ago is not live at Cloudflare's edge yet:
        # `tunnel route dns` returns before the record propagates, and the
        # connector may still be restarting to pick up the new ingress. Both
        # look like this — cloudflared accepts the local connection and finds no
        # origin behind it — and both clear on their own within about a minute.
        # Printing the config checklist here sends the operator to audit a
        # config that is fine, so wait it out before saying anything.
        for attempt in range(1, _TUNNEL_SETTLE_ATTEMPTS + 1):
            if not _looks_like_tunnel_not_ready(err):
                break
            print(
                f"  tunnel is not serving '{CLIENT_NAME}' yet — "
                f"retrying in {_TUNNEL_SETTLE_DELAY}s "
                f"({attempt}/{_TUNNEL_SETTLE_ATTEMPTS})"
            )
            time.sleep(_TUNNEL_SETTLE_DELAY)
            probe = _ssh("echo SSH_OK", capture_output=True, check=False)
            if probe.returncode == 0 and "SSH_OK" in (probe.stdout or ""):
                return True
            err = (probe.stderr or "").strip()

        if _looks_like_tunnel_not_ready(err):
            print(f"Cannot reach '{SSH_HOST}': the tunnel is not serving it.")
            print("  Your ssh config and cloudflared are probably fine — this is")
            print("  what a client looks like before its DNS route goes live,")
            print("  which can take a minute after 'gpudev client add'.")
            print("  Wait a moment and run the same command again.")
            print(f"  Still failing? On the host:  gpudev cloudflare")
            print("  (it reports per-hostname reachability and connector staleness)")
        else:
            print(f"Cannot reach '{SSH_HOST}' over SSH. Check that:")
            print(f"  • ~/.ssh/config has a matching 'Host {SSH_HOST}' entry")
            print(f"    (the host can print it: gpudev client info {CLIENT_NAME})")
            print("  • cloudflared is installed and on your PATH")
            print("  • the container is running on the host")

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

        if not CLIENT_NAME:
            print("No gpudev client selected for this notebook.")
            print("Use: %gpu <client-name>")
            return False

        if not install_cloudflared():
            return False

        if not self._check_ssh():
            return False

        # Soft start first (preserve variables when kernel is healthy).
        ensure_kernel()
        self._kill_stale_forwards()

        try:
            self.remote_kc, healed = self._attach_kernel(heal_hmac=True)
        except Exception as last_err:
            print(f"Could not attach to remote kernel '{CLIENT_NAME}': {last_err}")
            if self._hmac_heal_attempted:
                print(
                    "A forced kernel restart was already tried. "
                    "On the host: gpudev kernel doctor " + (CLIENT_NAME or "<name>")
                )
            else:
                print(
                    "The kernel is likely still alive — your variables are preserved. "
                    "Re-run the cell to retry, or %restart_kernel for a fresh kernel "
                    "(clears state)."
                )
            print(kernel_doctor())
            raise last_err

        if healed:
            print(f"Remote kernel '{CLIENT_NAME}' ready (fresh after HMAC self-heal)")
        else:
            print(f"Remote kernel '{CLIENT_NAME}' ready")
        return True

    # TODO: Refactor tunnel lifecycle helpers into a TunnelManager class.
    # Active code: do not remove until each method is migrated and tested.
    def _kill_stale_forwards(self):
        """Tear down our port-forward and stale local forward holders."""
        if self._tunnel_proc and self._tunnel_proc.poll() is None:
            self._tunnel_proc.terminate()
            try:
                self._tunnel_proc.wait(timeout=3)
            except Exception:
                self._tunnel_proc.kill()

        if self._tunnel_proc is not None:
            path = getattr(self._tunnel_proc, "craft_stderr_path", None)
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

        self._tunnel_proc = None

        if not sys.platform.startswith("win"):
            if SSH_HOST:
                subprocess.run(
                    ["ssh", *SSH_OPT_LIST, "-O", "exit", SSH_HOST],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            _reap_local_forwards(list(KERNEL_PORTS.values()))

    def _ensure_tunnel(self, kernel_info, timeout=25):
        """Establish the SSH port-forward and wait until it carries traffic."""
        ours_alive = self._tunnel_proc is not None and self._tunnel_proc.poll() is None

        if ours_alive and self._test_connection(kernel_info):
            return

        hostkey_retried = False
        self._kill_stale_forwards()
        self._tunnel_proc = start_port_forwarding(kernel_info)

        deadline = time.time() + timeout

        while time.time() < deadline:
            if self._test_connection(kernel_info, timeout=1):
                return

            if self._tunnel_proc.poll() is not None:
                for _ in range(8):
                    if self._test_connection(kernel_info, timeout=1):
                        return
                    time.sleep(0.25)

                err_text = ""
                path = getattr(self._tunnel_proc, "craft_stderr_path", None)
                if path:
                    try:
                        err_text = Path(path).read_text()
                    except Exception:
                        pass

                if not hostkey_retried and _is_host_key_changed(err_text):
                    _clear_stale_host_keys(err_text)
                    hostkey_retried = True
                    self._kill_stale_forwards()
                    self._tunnel_proc = start_port_forwarding(kernel_info)
                    deadline = time.time() + timeout
                    continue

                raise RuntimeError(self._forward_failure_msg())

            time.sleep(0.25)

        if self._tunnel_proc and self._tunnel_proc.poll() is None:
            self._tunnel_proc.terminate()
            try:
                self._tunnel_proc.wait(timeout=2)
            except Exception:
                self._tunnel_proc.kill()
                try:
                    self._tunnel_proc.wait(timeout=2)
                except Exception:
                    pass

        raise RuntimeError(self._forward_failure_msg())

    def _forward_failure_msg(self):
        """Build a useful error for a forward that exited without opening the port."""
        rc = self._tunnel_proc.returncode if self._tunnel_proc else None
        detail = ""

        path = getattr(self._tunnel_proc, "craft_stderr_path", None)
        if path:
            try:
                detail = Path(path).read_text().strip()
            except Exception:
                pass

        msg = f"SSH port-forward to '{SSH_HOST}' exited (rc={rc}) without opening the port"
        msg += ":\n" + detail if detail else " — check cloudflared / host reachability."

        if not sys.platform.startswith("win"):
            msg += "\n[port holders]\n" + _diagnose_port_holders(list(KERNEL_PORTS.values()))

        return msg

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
        """Confirm the remote kernel is reachable, reconnecting if needed."""
        if self.remote_kc is None:
            return self.reconnect()

        tunnel_dead = (
            self._tunnel_proc is None
            or self._tunnel_proc.poll() is not None
            or not self._test_connection(KERNEL_PORTS)
        )

        if tunnel_dead:
            return self.reconnect()

        if self.kernel_health()[0]:
            return True

        return self.reconnect()

    def reconnect(self):
        """Rebuild the SSH tunnel and re-attach; HMAC self-heal once if needed."""
        if self.remote_kc is not None:
            try:
                self.remote_kc.stop_channels()
            except Exception:
                pass
            self.remote_kc = None

        try:
            ensure_kernel()
            self._kill_stale_forwards()
            self.remote_kc, healed = self._attach_kernel(heal_hmac=True)
        except Exception as e:
            print(f"Reconnect failed: {e}")
            if self._hmac_heal_attempted:
                print(
                    "Forced kernel restart was already tried. "
                    "Check %kernel_status or: gpudev kernel doctor "
                    + (CLIENT_NAME or "<name>")
                )
            return False

        if healed:
            print(
                f"Reconnected to fresh kernel '{CLIENT_NAME}' "
                "(state cleared by HMAC self-heal)"
            )
        else:
            print(f"Reconnected to live kernel '{CLIENT_NAME}' (variables preserved)")
        return True

    def execute_remote(self, code, verbose=False):
        if not self._ensure_live():
            raise RuntimeError(
                "Remote kernel unreachable and automatic reconnect failed. "
                "Check %kernel_status, or run %restart_kernel for a fresh kernel."
            )

        renderer = _HybridOutputRenderer(code=code)
        renderer.start()
        try:
            reply = self.remote_kc.execute_interactive(
                code=code,
                output_hook=renderer.handle,
            )
        except KeyboardInterrupt:
            print("Interrupted — stopping remote job...")
            msg = self.remote_kc.session.msg("interrupt_request")
            self.remote_kc.control_channel.send(msg)
            print("Remote job interrupted.")
            renderer.finish("interrupted")
            raise
        except Exception:
            renderer.finish("failed")
            raise

        outcome = (
            "failed"
            if renderer.saw_error or reply.get("content", {}).get("status") == "error"
            else "completed"
        )
        renderer.finish(outcome)

        self.remote_kc.last_result = reply

        if verbose:
            return reply

    def restart_kernel(self):
        if self.remote_kc is None:
            print("No remote kernel connected")
            return

        self.remote_kc.stop_channels()
        self.remote_kc = None

        ensure_kernel(force_restart=True)
        self._kill_stale_forwards()
        # Explicit restart: no second HMAC heal loop (kernel is already fresh).
        self.remote_kc, _ = self._attach_kernel(heal_hmac=False)

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



# ── Package transform registry ────────────────────────────────────────────────
# CRAFT core never rewrites package syntax. Addons register optional source
# transforms here; they run only while active, on the path:
#
#   raw_code → CRAFT router → active addon transforms → backend execute
#
# Default path (no addons / all inactive):
#
#   raw_code → CRAFT router → backend execute
#
# Transforms are not global language features: unloading or deactivating a
# package removes/bypasses its rewrite. PythonBackend.passthru stays boring
# (local vs remote only).

_TRANSFORMS: dict = {}  # name -> {"fn": callable, "active": bool}


def register_transform(name: str, fn, *, active: bool = True) -> None:
    """Register a package source transform. Idempotent per *name*.

    *fn* receives the full cell source ``str`` and returns a ``str``
    (same text if nothing to rewrite). Only applied when ``active`` is true
    and the cell is being routed to a backend (not passthru).
    """
    if not name or not callable(fn):
        raise ValueError("register_transform(name, fn) requires a non-empty name and callable")
    _TRANSFORMS[str(name)] = {"fn": fn, "active": bool(active)}


def unregister_transform(name: str) -> bool:
    """Remove a package transform. Returns True if it was present."""
    return _TRANSFORMS.pop(str(name), None) is not None


def set_transform_active(name: str, active: bool) -> bool:
    """Enable/disable a registered transform without unregistering. Returns True if found."""
    entry = _TRANSFORMS.get(str(name))
    if entry is None:
        return False
    entry["active"] = bool(active)
    return True


def list_transforms():
    """Return ``[(name, active), ...]`` for debugging / status magics."""
    return [(n, bool(e.get("active"))) for n, e in _TRANSFORMS.items()]


def _apply_active_transforms(code: str) -> str:
    """Run active package transforms in registration order. Core is a no-op."""
    if not _TRANSFORMS:
        return code
    out = code
    for _name, entry in list(_TRANSFORMS.items()):
        if not entry.get("active"):
            continue
        fn = entry.get("fn")
        if not callable(fn):
            continue
        try:
            rewritten = fn(out)
        except Exception as e:
            print(f"CRAFT: transform {_name!r} failed: {e}")
            continue
        if isinstance(rewritten, str):
            out = rewritten
        elif isinstance(rewritten, list):
            out = "".join(rewritten)
    return out


def _restore_mangled_shell_cell(code: str) -> str:
    """Undo ``!cmd`` → ``~cmd`` from global bang rewriters (stale tidy3, etc.).

    Jupyter shell cells must reach the remote as ``!whoami`` / ``!pip …``.
    A global ``!`` → ``~`` pass turns them into ``~whoami`` (NameError).
    Leave real Python/tidy3 invert alone: ``~(x)``, ``~starts_with(...)``.
    """
    if "~" not in code:
        return code
    lines = code.splitlines(keepends=True)
    if not lines:
        return code
    for i, ln in enumerate(lines):
        stripped = ln.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("!"):
            # Intact shell — nothing to restore.
            return code
        if not stripped.startswith("~"):
            return code
        rest = stripped[1:]
        # ``~(expr)`` or ``~name(`` — real invert / tidy3 sugar, not shell.
        if rest.startswith("("):
            return code
        j = 0
        while j < len(rest) and (rest[j].isalnum() or rest[j] in "_.-"):
            j += 1
        if j == 0:
            return code
        # After the command token: shell has space/args/end; call has ``(``.
        k = j
        while k < len(rest) and rest[k] in " \t":
            k += 1
        if k < len(rest) and rest[k] == "(":
            return code
        indent = ln[: len(ln) - len(stripped)]
        # Preserve ``!!cmd`` mangled as ``~~cmd``? only handle single ~.
        lines[i] = indent + "!" + rest
        return "".join(lines)
    return code


def _is_shell_escape_cell(code: str) -> bool:
    s = code.lstrip(" \t")
    while s.startswith("\n"):
        s = s[1:].lstrip(" \t")
    return s.startswith("!")


# ── Mode Router ───────────────────────────────────────────────────────────────
class ModeRouter:
    def __init__(self):
        self.backend = None

    def _router_transform(self, lines):
        if self.backend is None:
            return lines

        code = "".join(lines)

        # passthru: host-local only — no package rewriting, no remote.
        if self.backend.passthru(code):
            return lines

        # Repair shell cells mangled by a global ``!`` → ``~`` preparser that
        # ran earlier on input_transformers_cleanup (stale tidy3 masking).
        code = _restore_mangled_shell_cell(code)

        # Jupyter shell cells (``!cmd`` / ``!!cmd``): route raw to the remote
        # kernel — never run package transforms that might rewrite ``!`` → ``~``.
        if _is_shell_escape_cell(code):
            self.backend.pending = code
            return [self.backend.dispatch + "\n"]

        # Package addons own syntax transforms; core only routes.
        code = _apply_active_transforms(code)
        # Transforms can re-mangle shell-like lines; restore again.
        code = _restore_mangled_shell_cell(code)
        self.backend.pending = code
        return [self.backend.dispatch + "\n"]

    @staticmethod
    def _detach():
        ip = get_ipython()
        ip.input_transformers_cleanup[:] = [
            f
            for f in ip.input_transformers_cleanup
            if getattr(getattr(f, "__func__", None), "__name__", "") != "_router_transform"
        ]

    def set(self, backend):
        self.backend = backend
        self._detach()

        if backend is not None:
            get_ipython().input_transformers_cleanup.append(self._router_transform)

        print(backend.banner if backend else "Local Python mode — cells run in this notebook")


# Defaults for GPU Python mode. Extra prefixes (pcviz, user tools) register via
# register_local_magic() into IPython user_ns so they survive CRAFT re-runs.
# Core host magics only — Mojo lives in addons/mojo.py
_DEFAULT_LOCAL_MAGICS = (
    "%gpu",
    "%gpu_setup",
    "%local",
    "%restart_kernel",
    "%kernel_status",
)

_LOCAL_MAGICS_NS_KEY = "_gpudev_local_magics"


def _local_magic_set():
    """Mutable set of line-magic prefixes that must stay local under %gpu."""
    try:
        ip = get_ipython()
        ns = ip.user_ns
    except Exception:
        global _LOCAL_MAGICS_FALLBACK
        if "_LOCAL_MAGICS_FALLBACK" not in globals():
            _LOCAL_MAGICS_FALLBACK = set(_DEFAULT_LOCAL_MAGICS)
        s = _LOCAL_MAGICS_FALLBACK
    else:
        s = ns.setdefault(_LOCAL_MAGICS_NS_KEY, set())
    # Re-assert defaults every call so CRAFT re-run never drops core magics.
    s.update(_DEFAULT_LOCAL_MAGICS)
    return s


def register_local_magic(magic: str) -> None:
    """Register a line-magic prefix that stays local under %gpu. Idempotent."""
    m = magic if magic.startswith("%") else f"%{magic}"
    _local_magic_set().add(m)


class PythonBackend:
    banner = "GPU Python mode — cells run on the remote kernel"
    dispatch = "_exec_mgr.execute_remote(ROUTER.backend.pending)"
    pending = None

    # Back-compat: older pcviz did `be._LOCAL = tuple(be._LOCAL) + (magic,)`.
    # Property reads/writes the durable set.
    @property
    def _LOCAL(self):
        return tuple(_local_magic_set())

    @_LOCAL.setter
    def _LOCAL(self, value):
        _local_magic_set().update(value)

    def passthru(self, c):
        """Host-local only — no syntax rewriting.

        Decides whether a cell stays on the notebook kernel under ``%gpu``.
        Package transforms (tidy3, plot3, …) must not live here; they register
        via :func:`register_transform` and run only for non-passthru cells.
        """
        s = c.lstrip()

        return (
            s.startswith(tuple(_local_magic_set()))
            or "get_ipython()" in c
            or s.startswith(("await call_tool(", "_exec_mgr.", "remote_run_("))
        )




# Managers created in install_core() so re-import is safe
_exec_mgr = None
_mojo_mgr = None  # set by addons/mojo.py when loaded
ROUTER = None
PY_BACKEND = None
MOJO_BACKEND = None  # set by addons/mojo.py when loaded


# ── remote_run_ for tool-style local helpers ──────────────────────────────────
def remote_run_(code: str, max_chars: int = 2000) -> str:
    """Execute code on the remote kernel and return output as a string."""
    if _exec_mgr is None or not _exec_mgr._ensure_live():
        raise RuntimeError("Remote kernel unreachable and automatic reconnect failed.")

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

    _exec_mgr.remote_kc.execute_interactive(
        code=code,
        output_hook=capturing_hook,
    )

    output = "".join(collected)

    if len(output) > max_chars:
        half = max_chars // 2
        output = (
            output[:half]
            + f"\n\n... [{len(output) - max_chars} chars truncated] ...\n\n"
            + output[-half:]
        )

    return output


# ── Magics ────────────────────────────────────────────────────────────────────
def _ensure_connected():
    """Make sure the remote kernel + SSH tunnel are up. Returns True on success."""
    if _exec_mgr is None:
        return False
    if _exec_mgr.remote_kc is not None and _exec_mgr.kernel_health()[0]:
        return True

    for attempt in range(CONNECT_ATTEMPTS):
        try:
            if _exec_mgr.setup_remote():
                return True

            return False

        except Exception as e:
            print(f"Attempt {attempt + 1}/{CONNECT_ATTEMPTS} failed: {e}")

            if attempt < CONNECT_ATTEMPTS - 1:
                time.sleep(5)

    print(f"Failed to connect after {CONNECT_ATTEMPTS} attempt(s)")
    return False


# Kept in step with client-setup.sh's resolve_variant_image.
_KNOWN_VARIANTS = ("default", "cuda-dev")


def _parse_gpu_setup_args(line: str) -> tuple[str, str | None, str]:
    """Parse ``%gpu_setup <name> [--hostname <host>]``.

    ``--hostname`` is OPTIONAL. Requiring it was the only reason the
    administrator had to go first: the hostname is per-client, so the notebook
    could not know it unaided. Without it the key is still generated and can be
    sent onward; the ssh config stanza is written later, on the first
    ``%gpu <name> --hostname <host>``.
    """
    usage = ("Usage: %gpu_setup <client-name> [--variant cuda-dev] "
             "[--hostname <client.domain>]")
    tokens = shlex.split(line or "")
    if not tokens:
        raise ValueError(usage)

    name = tokens.pop(0)
    hostname = None
    variant = "default"
    while tokens:
        token = tokens.pop(0)
        if token == "--hostname" and tokens:
            hostname = tokens.pop(0)
        elif token.startswith("--hostname="):
            hostname = token.split("=", 1)[1]
        elif token == "--variant" and tokens:
            variant = tokens.pop(0)
        elif token.startswith("--variant="):
            variant = token.split("=", 1)[1]
        else:
            raise ValueError(f"Unknown argument {token!r}. {usage}")
    if hostname is not None and not hostname:
        raise ValueError("--hostname was given an empty value.")
    if variant not in _KNOWN_VARIANTS:
        raise ValueError(
            f"Unknown variant {variant!r}. Known variants: "
            + ", ".join(_KNOWN_VARIANTS)
        )
    return name, hostname, variant


def gpu_setup(line):
    """One-time, idempotent SolveIt-side SSH/key setup for a client."""
    try:
        name, hostname, variant = _parse_gpu_setup_args(line)
        name = normalize_client_name(name)
    except ValueError as e:
        print(e)
        return

    if not install_cloudflared():
        return

    # Any client already configured here records the domain in its stanza, so
    # only the very first client on a fresh notebook has to be told a hostname.
    if hostname is None:
        domain = derive_domain()
        if domain:
            hostname = f"{name}.{domain}"

    try:
        result = setup_client(name, hostname)
    except Exception as e:
        print(f"gpudev client setup failed: {e}")
        return

    variant_arg = f" --variant {variant}" if variant != "default" else ""
    admin_cmd = (
        f'gpudev client add {result.name}{variant_arg} '
        f'--key "{result.public_key_text}"'
    )

    # Build the whole report as ONE output. Interleaving print() with display()
    # put the fenced block at the top of the cell, above lines printed before
    # it: the notebook orders rich output ahead of stream text, and flushing
    # stdout first did not change that. With a single display there is nothing
    # to race. select_client() prints, so it runs after this rather than before.
    #
    # The command is fenced because it is long — a name, a variant and a full
    # public key — and hand-selecting it tends to clip the trailing quote,
    # which then fails at `client add` looking like a key problem.
    endpoint = f"\n- Endpoint: `{result.hostname}`" if result.ssh_config_written else ""
    note = ""
    if variant == "cuda-dev":
        # Make the privilege visible where it is approved: cuda-dev grants
        # SYS_ADMIN, which client-setup.sh calls "close to root on the host".
        note = (
            "\n_cuda-dev adds nvcc/ncu/nsys/TensorRT and grants SYS_ADMIN + "
            "PERFMON to the container — drop `--variant` for a standard client._\n"
        )
    if result.ssh_config_written:
        closing = (
            f"After the administrator confirms, connect with:\n\n"
            f"```\n%gpu {result.name}\n```"
        )
    else:
        closing = (
            "SSH config not written yet — this client's hostname is not known.\n\n"
            "When the administrator confirms, connect with the line their "
            "`client add` prints:\n\n"
            f"```\n%gpu {result.name} --hostname {result.name}.<their-domain>\n```"
        )

    if _HAS_RICH_DISPLAY:
        display(Markdown(
            f"**Local gpudev client setup is ready**\n\n"
            f"- SSH alias: `{result.ssh_alias}`{endpoint}\n"
            f"- Private key: `{result.private_key}` (kept only on this client)\n\n"
            f"Send this line to your gpudev administrator:\n\n"
            f"```bash\n{admin_cmd}\n```\n"
            f"{note}\n"
            f"{closing}"
        ))
    else:
        print("Local gpudev client setup is ready")
        print(f"  SSH alias:   {result.ssh_alias}")
        if result.ssh_config_written:
            print(f"  Endpoint:    {result.hostname}")
        print(f"  Private key: {result.private_key} (kept only on this client)")
        print("")
        print("Send this line to your gpudev administrator:")
        print("")
        print(f"  {admin_cmd}")
        print("")
        if variant == "cuda-dev":
            print("  (cuda-dev adds nvcc/ncu/nsys/TensorRT and grants SYS_ADMIN +")
            print("   PERFMON to the container — drop --variant for a standard client)")
            print("")
        if result.ssh_config_written:
            print("After the administrator confirms, connect with:")
            print(f"  %gpu {result.name}")
        else:
            print("SSH config not written yet — this client's hostname is not known.")
            print("When the administrator confirms, connect with:")
            print(f"  %gpu {result.name} --hostname {result.name}.<their-domain>")
            print("(the administrator's `client add` prints the exact line)")

    select_client(result.name, quiet=True)

def gpu(line):
    try:
        tokens = shlex.split(line or "")
    except ValueError as e:
        print(f"Invalid %gpu arguments: {e}")
        return

    # --hostname is accepted here so a notebook that ran `%gpu_setup <name>`
    # before the administrator provisioned anything can supply the hostname on
    # its first connect. Writing the stanza is all it is needed for.
    name = ""
    hostname = None
    while tokens:
        token = tokens.pop(0)
        if token == "--hostname" and tokens:
            hostname = tokens.pop(0)
        elif token.startswith("--hostname="):
            hostname = token.split("=", 1)[1]
        elif token.startswith("-"):
            print(f"Unknown argument {token!r}. Usage: %gpu <client-name> "
                  "[--hostname <client.domain>]")
            return
        elif not name:
            name = token
        else:
            print("Usage: %gpu <client-name> [--hostname <client.domain>]")
            return

    if not name and not CLIENT_NAME:
        print("No client selected. Use: %gpu <client-name>")
        return
    name = name or CLIENT_NAME

    if hostname:
        try:
            setup_client(name, hostname)
        except Exception as e:
            print(f"Could not write the SSH config for {name!r}: {e}")
            return
    elif not has_ssh_stanza(name):
        # Without a stanza ssh fails with "could not resolve hostname
        # gpudev-<name>", which names nothing about gpudev. Say what to run.
        print(f"No SSH config for '{name}' yet — its hostname is not known here.")
        print("Run this once, with the hostname your administrator confirmed:")
        print(f"  %gpu {name} --hostname {name}.<their-domain>")
        return

    try:
        select_client(name)
    except ValueError as e:
        print(e)
        return

    if _ensure_connected():
        ROUTER.set(PY_BACKEND)


def local(line):
    ROUTER.set(None)


def restart_kernel(line):
    if not CLIENT_NAME:
        print("No client selected. Use: %gpu <client-name>")
        return
    _exec_mgr.restart_kernel()


def kernel_status(line):
    mode = (
        "mojo (GPU)"
        if MOJO_BACKEND is not None and ROUTER and ROUTER.backend is MOJO_BACKEND
        else "python (GPU)"
        if ROUTER and ROUTER.backend is PY_BACKEND
        else "local"
    )

    print("=" * 40)
    print("KERNEL STATUS")
    print("=" * 40)
    print(f"Client:         {CLIENT_NAME}")
    print(f"Execution mode: {mode}")
    print(f"Connected:      {'yes' if _exec_mgr and _exec_mgr.remote_kc else 'no'}")

    if _exec_mgr and _exec_mgr.remote_kc:
        ok, detail = _exec_mgr.kernel_health()
        print(f"Kernel health:  {'OK' if ok else 'FAIL'} ({detail})")

        try:
            info = fetch_kernel_info()
            reachable = _exec_mgr._test_connection(info)
            print(f"Tunnel ports:   {'open' if reachable else 'closed'}")
        except Exception:
            print("Tunnel ports:   unknown")

    gpus = gpu_status() if CLIENT_NAME else None

    if gpus:
        print("GPU:")
        for g in gpus:
            print(f"  {g}")
    else:
        print("GPU:            (nvidia-smi unavailable)")

    print("=" * 40)


_CORE_MAGIC_FUNCS = (
    ("gpu", gpu),
    ("gpu_setup", gpu_setup),
    ("local", local),
    ("restart_kernel", restart_kernel),
    ("kernel_status", kernel_status),
)

# Names pcviz / sslive / dialog cells expect in the interactive namespace
_USER_NS_EXPORTS = (
    "SSH_HOST",
    "SSH_OPTS",
    "CLIENT_NAME",
    "KERNEL_PORTS",
    "select_client",
    "register_local_magic",
    "register_transform",
    "unregister_transform",
    "set_transform_active",
    "list_transforms",
    "remote_run_",
    "_exec_mgr",
    "_mojo_mgr",
    "ROUTER",
    "PY_BACKEND",
    "MOJO_BACKEND",
    "fetch_kernel_info",
    "gpu_status",
    "kernel_doctor",
)


def _inject_user_ns() -> None:
    """Expose core API on the IPython interactive namespace (for addons & cells)."""
    try:
        ip = get_ipython()
    except Exception:
        ip = None
    if ip is None:
        return
    ns = ip.user_ns
    g = globals()
    for name in _USER_NS_EXPORTS:
        if name in g:
            ns[name] = g[name]
    # Also export magic functions for rare direct calls
    for name, fn in _CORE_MAGIC_FUNCS:
        ns[name] = fn


def _register_core_magics() -> bool:
    try:
        ip = get_ipython()
    except Exception:
        ip = None
    if ip is None:
        return False
    ok = False
    try:
        mm = ip.magics_manager
        for name, fn in _CORE_MAGIC_FUNCS:
            mm.register_function(fn, magic_kind="line", magic_name=name)
        ok = True
    except Exception:
        try:
            for name, fn in _CORE_MAGIC_FUNCS:
                register_line_magic(fn)
            ok = True
        except Exception:
            ok = False
    return ok


def install_core(*, quiet: bool = False) -> bool:
    """Bootstrap GPU CRAFT: managers, magics, user_ns. Idempotent.

    Mojo is optional — ``%run addons/mojo.py`` after this.
    """
    global _exec_mgr, ROUTER, PY_BACKEND

    # Soft-restart managers on re-install (keep connection if healthy)
    if _exec_mgr is not None:
        try:
            ok, _ = _exec_mgr.kernel_health()
            if not ok:
                try:
                    _exec_mgr.shutdown_remote()
                except Exception:
                    pass
                _exec_mgr = RemoteExecutionManager()
        except Exception:
            try:
                _exec_mgr.shutdown_remote()
            except Exception:
                pass
            _exec_mgr = RemoteExecutionManager()
    else:
        _exec_mgr = RemoteExecutionManager()

    try:
        ModeRouter._detach()
    except Exception:
        pass

    if ROUTER is None:
        ROUTER = ModeRouter()
    if PY_BACKEND is None:
        PY_BACKEND = PythonBackend()

    # Fix dispatch strings to use live globals via user_ns names (same as before)
    PY_BACKEND.dispatch = "_exec_mgr.execute_remote(ROUTER.backend.pending)"

    _local_magic_set()
    magics_ok = _register_core_magics()
    _inject_user_ns()

    if not quiet:
        print("CRAFT core ready")
        print("  %gpu <client>  %gpu_setup <client> --hostname <host>")
        print("  %local  %kernel_status  %restart_kernel")
        print("  remote_run_(code)  register_local_magic('%name')")
        print("  register_transform(name, fn)  # package syntax only; core has none")
        print("  Addons (%local + %run):")
        print("    addons/pcviz.py   addons/mojo.py   addons/sslive.py")
        print("    addons/tidy3.py   addons/plot3.py")
        if not magics_ok:
            print("  warning: line magics may not have registered (not in IPython?)")

    return magics_ok


# Back-compat alias
install = install_core
