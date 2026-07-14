import html
import json
import re
import signal
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

# Must match host sanitize_name / DNS labels (gpudev client add).
_CLIENT_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _normalize_client_name(raw) -> str:
    """Return validated client name, or '' if unset. Raises ValueError if invalid."""
    name = (raw or "").strip().lower()
    if not name:
        return ""
    if len(name) > 63 or not _CLIENT_NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid client_name {raw!r}: use lowercase letters, digits, "
            f"hyphens (e.g. 'alice', 'solveit') — same as gpudev client add"
        )
    return name


try:
    CLIENT_NAME = _normalize_client_name(_cfg.get("client_name", ""))
except ValueError as e:
    CLIENT_NAME = ""
    _CLIENT_NAME_ERROR = str(e)
else:
    _CLIENT_NAME_ERROR = ""

# Inside every gpudev container the UNIX user is the fixed `gpudev`; the client
# identity lives in the container name and tunnel hostname. Paths are stable.
KERNEL_MANAGER = "/home/gpudev/bin/kernel-manager.sh"
KERNEL_RUNTIME = "/home/gpudev/.local/share/jupyter/runtime/kernel.json"

# SSH alias is derived from client_name — must match what `gpudev client info`
# prints and what client-setup.sh sets as the container hostname.
SSH_HOST = f"gpudev-{CLIENT_NAME}" if CLIENT_NAME else ""

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


# Keep debugging noise low: one attempt shows the first real failure cleanly.
CONNECT_ATTEMPTS = 1

CLOUDFLARED_PATH = Path(
    os.environ.get("CLOUDFLARED_PATH")
    or shutil.which("cloudflared")
    or (Path.home() / ".local" / "bin" / "cloudflared")
)

_cf_dir = str(CLOUDFLARED_PATH.parent)
if _cf_dir not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _cf_dir + os.pathsep + os.environ.get("PATH", "")

del _cfg


# ── Helpers ───────────────────────────────────────────────────────────────────
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\[[0-9;]*$|\x1b$")


def _strip_ansi(text):
    return ANSI_RE.sub("", text)


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
            "SSH_HOST is empty — set client_name in craft.json and re-load CRAFT"
        )
    if not CLIENT_NAME or not _CLIENT_NAME_RE.fullmatch(CLIENT_NAME):
        raise RuntimeError("CLIENT_NAME failed validation — refusing SSH")

    wrapped = f"GPUDEV_CLIENT={CLIENT_NAME} {cmd}"
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
        raise RuntimeError("SSH_HOST is empty")
    result = subprocess.run(
        ["ssh", *SSH_OPT_LIST, SSH_HOST, remote_cmd],
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
            ["ssh", SSH_HOST, remote_cmd],
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

    print("cloudflared not found — downloading a local copy...")
    CLOUDFLARED_PATH.parent.mkdir(parents=True, exist_ok=True)
    url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"

    try:
        _run_shell(f"curl -fsSL {url} -o {CLOUDFLARED_PATH} && chmod +x {CLOUDFLARED_PATH}")
    except Exception as e:
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
            if _CLIENT_NAME_ERROR:
                print(f"Invalid craft.json: {_CLIENT_NAME_ERROR}")
                print(f"Edit {CONFIG_PATH} and re-run CRAFT.")
                return False
            print(f'No "client_name" set in {CONFIG_PATH}')
            print('Set it like: {"client_name": "<your-name>"}')
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

        try:
            reply = self.remote_kc.execute_interactive(
                code=code,
                output_hook=self._output_hook,
            )
        except KeyboardInterrupt:
            print("Interrupted — stopping remote job...")
            msg = self.remote_kc.session.msg("interrupt_request")
            self.remote_kc.control_channel.send(msg)
            print("Remote job interrupted.")
            raise

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


# ── Mojo Execution Manager ────────────────────────────────────────────────────
_MOJO_NOISE = (
    "Failed to initialize Crashpad",
    "Crash reporting will not be available",
    "crashpad handler",
)


def _scrub_mojo_noise(text):
    if not text:
        return ""

    return "".join(
        ln
        for ln in text.splitlines(keepends=True)
        if not any(n in ln for n in _MOJO_NOISE)
    )


class RemoteMojoHelper:
    """Run / build / package Mojo inside the client container via pixi, over SSH."""

    PIXI = "/opt/pixi/bin/pixi"
    PROJ = "/opt/mojo-proj"

    def _runner(self):
        return f"{self.PIXI} run --manifest-path {self.PROJ}/pixi.toml"

    def run_source(self, src):
        _ssh_with_input(
            "mkdir -p /tmp/gpum && cat > /tmp/gpum/mojo_run.mojo",
            src,
            check=True,
        )

        return _ssh(
            f"{self._runner()} mojo run /tmp/gpum/mojo_run.mojo",
            capture_output=True,
            check=False,
        )

    def bench_source(self, src, n):
        _ssh_with_input(
            "mkdir -p /tmp/gpum && cat > /tmp/gpum/bench.mojo",
            src,
            check=True,
        )

        script = (
            "t0=$(date +%s%N); mojo build /tmp/gpum/bench.mojo -o /tmp/gpum/bench 1>&2 || exit 3\n"
            "echo COMPILE $(( $(date +%s%N) - t0 ))\n"
            "/tmp/gpum/bench >/dev/null 2>&1\n"
            f"for i in $(seq 1 {n}); do a=$(date +%s%N); /tmp/gpum/bench >/dev/null 2>&1; "
            "echo RUN $(( $(date +%s%N) - a )); done\n"
        )

        return _ssh_with_input(
            f"cat > /tmp/gpum/bench.sh && {self._runner()} bash /tmp/gpum/bench.sh",
            script,
            check=False,
        )

    def add_package(self, spec, pypi=False):
        flag = "--pypi " if pypi else ""
        return _ssh(
            f"{self.PIXI} add --manifest-path {self.PROJ}/pixi.toml {flag}{spec}",
            capture_output=True,
            check=False,
        )


class MojoExecutionManager:
    def __init__(self, root="/tmp/craft-mojo", helper=None):
        self.helper = helper or RemoteMojoHelper()
        self.root = Path(root)
        self.root.mkdir(exist_ok=True)
        self.history_path = self.root / "history.json"
        self.run_path = self.root / "mojo_run.mojo"
        self._counter = 0

    def load_history(self):
        if not self.history_path.exists():
            return []
        return json.loads(self.history_path.read_text())

    def save_history(self, cells):
        self.history_path.write_text(json.dumps(cells, indent=2))

    def first_meaningful_line(self, code):
        for line in code.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                return s
        return ""

    def defined_symbols(self, code):
        return re.findall(
            r"^\s*(?:fn|def|struct|trait|class|alias)\s+([A-Za-z_]\w*)",
            code,
            re.M,
        )

    def assigned_symbols(self, code):
        return re.findall(
            r"^\s*(?:let|alias|comptime)\s+([A-Za-z_]\w*)\b",
            code,
            re.M,
        )

    def has_main(self, code):
        return bool(re.search(r"^\s*(?:fn|def)\s+main\s*\(", code, re.M))

    def cell_kind(self, code):
        if self.has_main(code):
            return "command"

        line = self.first_meaningful_line(code)

        if self.defined_symbols(code):
            return "code"

        if line.startswith(("from ", "import ")):
            return "code"

        if self.assigned_symbols(code):
            return "state"

        return "command"

    def is_mixed_cell(self, code):
        if self.has_main(code):
            return False

        return bool(self.defined_symbols(code)) and "print(" in code

    async def current_msg_id(self):
        try:
            msg = await read_msg(0)
            return msg["id"]
        except Exception:
            self._counter += 1
            return f"cell-{self._counter}"

    def upsert_cell(self, msg_id, code):
        cells = self.load_history()
        now = time.time()
        kind = self.cell_kind(code)

        entry = {
            "msg_id": msg_id,
            "updated_at": now,
            "kind": kind,
            "defines": self.defined_symbols(code),
            "assigns": self.assigned_symbols(code),
            "code": code,
        }

        for cell in cells:
            if cell.get("msg_id") == msg_id:
                cell.update(entry)
                self.save_history(cells)
                return

        entry["index"] = len(cells)
        entry["created_at"] = now
        cells.append(entry)
        self.save_history(cells)

    def latest_wins_cells(self, cells):
        seen = set()
        kept = []

        for cell in reversed(cells):
            symbols = set(cell.get("defines", [])) | set(cell.get("assigns", []))

            if not symbols:
                kept.append(cell)
                continue

            if symbols & seen:
                continue

            seen.update(symbols)
            kept.append(cell)

        return list(reversed(kept))

    def build_source(self, current_code, current_msg_id):
        cells = [c for c in self.load_history() if c.get("msg_id") != current_msg_id]

        persistent = self.latest_wins_cells(
            [c for c in cells if c.get("kind") in ("code", "state")]
        )

        current_kind = self.cell_kind(current_code)

        if current_kind in ("code", "state"):
            current_entry = {
                "kind": current_kind,
                "defines": self.defined_symbols(current_code),
                "assigns": self.assigned_symbols(current_code),
                "code": current_code,
            }

            persistent = self.latest_wins_cells(persistent + [current_entry])
            preamble = "\n\n".join(c["code"] for c in persistent)
            return preamble + "\n\ndef main():\n    pass\n"

        preamble = "\n\n".join(c["code"] for c in persistent)

        if self.has_main(current_code):
            return ((preamble + "\n\n") if preamble else "") + current_code.rstrip() + "\n"

        expr = current_code.strip()

        if "\n" not in expr and not expr.startswith(
            ("print(", "for ", "if ", "while ", "var ", "let ")
        ):
            body = "    print(" + expr + ")"
        else:
            body = "\n".join("    " + line for line in current_code.splitlines())

        return ((preamble + "\n\n") if preamble else "") + "def main():\n" + body + "\n"

    async def execute_mojo(self, code):
        if self.is_mixed_cell(code):
            raise ValueError(
                "Mojo cells shouldn't mix definitions and commands — "
                "put defs and print/calls in separate cells."
            )

        msg_id = await self.current_msg_id()
        src = self.build_source(code, msg_id)
        self.run_path.write_text(src)

        t0 = time.perf_counter()
        r = self.helper.run_source(src)
        dt = time.perf_counter() - t0

        if r.stdout:
            print(r.stdout, end="")

        err = _scrub_mojo_noise(r.stderr)
        if err:
            print(err, end="")

        print(f"[mojo run: {dt:.3f}s]")

        if r.returncode != 0:
            raise RuntimeError("mojo run failed")
        self.upsert_cell(msg_id, code)

    def restart_mojo(self):
        if self.root.exists():
            shutil.rmtree(self.root)

        self.root.mkdir(exist_ok=True)
        print("Mojo restarted: history + generated source cleared")

    def show_history(self):
        print(self.history_path.read_text() if self.history_path.exists() else "[]")

    def show_run(self):
        print(self.run_path.read_text() if self.run_path.exists() else "")

    def add_package(self, spec):
        if not spec:
            print("usage: %mojo_add <package> [...]   (conda channels first, PyPI auto-fallback)")
            return

        print(f"pixi add {spec} … (downloading)")
        r = self.helper.add_package(spec)
        out = _scrub_mojo_noise((r.stdout or "") + (r.stderr or ""))

        if r.returncode != 0 and "No candidates" in out and "--" not in spec:
            print("  not on conda channels — trying PyPI…")
            r = self.helper.add_package(spec, pypi=True)
            out = _scrub_mojo_noise((r.stdout or "") + (r.stderr or ""))

        print(out.strip() or (f"added: {spec}" if r.returncode == 0 else "add failed"))

    def bench(self, n=20):
        import statistics

        src = self.run_path.read_text() if self.run_path.exists() else ""

        if not self.has_main(src):
            print("Nothing to benchmark — run a %gpum command cell first.")
            return

        r = self.helper.bench_source(src, max(1, n))
        out = r.stdout or ""

        runs = sorted(
            int(l.split()[1])
            for l in out.splitlines()
            if l.startswith("RUN")
        )

        comp = next(
            (int(l.split()[1]) for l in out.splitlines() if l.startswith("COMPILE")),
            None,
        )

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


# ── Mode Router ───────────────────────────────────────────────────────────────
class ModeRouter:
    def __init__(self):
        self.backend = None

    def _router_transform(self, lines):
        if self.backend is None:
            return lines

        code = "".join(lines)

        if self.backend.passthru(code):
            return lines

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
_DEFAULT_LOCAL_MAGICS = (
    "%gpu",
    "%gpum",
    "%local",
    "%restart_kernel",
    "%restart_mojo",
    "%kernel_status",
    "%mojo_history",
    "%mojo_run",
    "%bench",
    "%mojo_add",
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
        s = c.lstrip()

        return (
            s.startswith(tuple(_local_magic_set()))
            or "get_ipython()" in c
            or s.startswith(("await call_tool(", "_exec_mgr.", "remote_run_("))
        )


class MojoBackend:
    banner = "GPU Mojo mode — cells compiled & run in the GPU container"
    dispatch = "await _mojo_mgr.execute_mojo(ROUTER.backend.pending)"
    pending = None

    def passthru(self, c):
        s = c.lstrip()

        return (
            not s
            or s[0] in "%!?"
            or "get_ipython()" in c
            or "_mojo_mgr." in c
        )


if "_exec_mgr" in globals() and _exec_mgr is not None:
    _exec_mgr.shutdown_remote()

_exec_mgr = RemoteExecutionManager()
_mojo_mgr = MojoExecutionManager()

ModeRouter._detach()

ROUTER = ModeRouter()
PY_BACKEND, MOJO_BACKEND = PythonBackend(), MojoBackend()
_local_magic_set()  # seed defaults into user_ns immediately


# ── remote_run_ for tool-style local helpers ──────────────────────────────────
def remote_run_(code: str, max_chars: int = 2000) -> str:
    """Execute code on the remote kernel and return output as a string."""
    if not _exec_mgr._ensure_live():
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
    arg = line.strip()
    _mojo_mgr.bench(int(arg) if arg.isdigit() else 20)


@register_line_magic
def mojo_add(line):
    _mojo_mgr.add_package(line.strip())


@register_line_magic
def kernel_status(line):
    mode = (
        "mojo (GPU)"
        if ROUTER.backend is MOJO_BACKEND
        else "python (GPU)"
        if ROUTER.backend is PY_BACKEND
        else "local"
    )

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


# ── Manual connect only ───────────────────────────────────────────────────────
print("CRAFT ready")
print("  %gpu             connect Python cells to the GPU kernel")
print("  %gpum            connect Mojo execution to the GPU container")
print("  %local           run cells locally in this notebook again")
print("  %restart_kernel  restart the remote Python kernel")
print("  %restart_mojo    clear Mojo history + generated source")
print("  %mojo_history    show accumulated Mojo cells")
print("  %mojo_run        show the last generated Mojo source")
print("  %mojo_add <pkg>  pixi-add a Mojo/Python package into the GPU container")
print("  %bench [N]       compile last Mojo program once, time N runs (no compile)")
print("  %kernel_status   show mode + connection + GPU status")
