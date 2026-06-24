#!/usr/bin/env bash
set -euo pipefail

# kernel-manager.sh — runs inside a gpudev container
# Manages the single Jupyter kernel for this client.
# The kernel stays running between client reconnects; clients attach via the
# connection file using ZeroMQ. State (variables, loaded models) is preserved
# as long as the kernel process is alive.

# The container's UNIX user is uniform across all gpudev clients (see
# client-setup.sh). The container's hostname is gpudev-<client>, so falling
# back to it for CLIENT_NAME gives a recognizable string if GPUDEV_CLIENT
# somehow isn't set (degraded path; the real source is start.sh's `-e`).
CONTAINER_USER="gpudev"
HOME_DIR="/home/${CONTAINER_USER}"
CLIENT_NAME="${GPUDEV_CLIENT:-$(hostname)}"

VENV="${HOME_DIR}/.venv"
RUNTIME_DIR="${HOME_DIR}/.local/share/jupyter/runtime"
CONNECTION_FILE="${RUNTIME_DIR}/kernel.json"
PID_FILE="${RUNTIME_DIR}/kernel.pid"
LOG_FILE="${RUNTIME_DIR}/kernel.log"
LOCK_DIR="${RUNTIME_DIR}/kernel-manager.lock.d"

# Fixed ZMQ ports (must match KERNEL_PORTS in CRAFT.py)
SHELL_PORT=54100
IOPUB_PORT=54101
STDIN_PORT=54102
CONTROL_PORT=54103
HB_PORT=54104
ALL_PORTS="$SHELL_PORT $IOPUB_PORT $STDIN_PORT $CONTROL_PORT $HB_PORT"

# ── Run as the gpudev user ──────────────────────────────────────────────────
# ssh, CRAFT, and `gpudev kernel` all operate as the gpudev user, so the kernel
# and its connection file must share that owner. Otherwise this script (running
# as gpudev) can neither see nor kill the kernel — exactly what left a
# root-owned kernel holding the ports with an HMAC key nothing else could match.
# If we were launched as root (start.sh's `su` failing to drop, or a bare
# `docker exec`), re-exec as gpudev, preserving GPUDEV_CLIENT.
if [ "$(id -u)" = "0" ] && id "$CONTAINER_USER" >/dev/null 2>&1 \
   && [ "$(id -un)" != "$CONTAINER_USER" ]; then
    if command -v runuser >/dev/null 2>&1; then
        exec runuser -u "$CONTAINER_USER" -- \
            /usr/bin/env "GPUDEV_CLIENT=$CLIENT_NAME" "$0" "$@"
    else
        exec su -s /bin/bash "$CONTAINER_USER" \
            -c "GPUDEV_CLIENT='$CLIENT_NAME' exec '$0' $*"
    fi
fi

log()  { echo "$*"; }
fail() { echo "Error: $*" >&2; exit 1; }

# The venv python is the only interpreter guaranteed to exist; use it for
# everything (never bare `python3`, which may be absent in the base image).
PY() { "${VENV}/bin/python" "$@"; }

require_venv() {
    [ -x "${VENV}/bin/python" ] || fail "Client venv not found at ${VENV}. Run client-setup.sh first."
}

ensure_dirs() {
    mkdir -p "$RUNTIME_DIR"
}

# Serialize every mutating operation (start/stop/restart). Without this, two
# concurrent invocations — a client firing several reconnects at once, or
# start.sh racing a client's first connect — interleave reap+write+launch and
# leave TWO kernels: the connection file has one key while another kernel holds
# the ports, so the client gets HMAC-signature timeouts. Atomic `mkdir` is the
# mutex (no flock dependency — the slim base image may lack it); the EXIT trap
# releases it; a dead holder's lock is stolen so a crash can't wedge it forever.
acquire_lock() {
    ensure_dirs
    local tries=120 holder      # ~60s at 0.5s/try
    while ! mkdir "$LOCK_DIR" 2>/dev/null; do
        holder="$(cat "${LOCK_DIR}/pid" 2>/dev/null || true)"
        if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
            rm -rf "$LOCK_DIR"          # holder is gone — steal the stale lock
            continue
        fi
        sleep 0.5
        tries=$((tries - 1))
        [ "$tries" -le 0 ] && fail "Timed out (60s) on kernel-manager lock. If stale: rm -rf '$LOCK_DIR'"
    done
    echo $$ > "${LOCK_DIR}/pid"
    trap 'rm -rf "$LOCK_DIR" 2>/dev/null || true' EXIT
}

kernel_pid() {
    [ -f "$PID_FILE" ] || return 1
    local pid
    pid="$(cat "$PID_FILE")"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

# Is something accepting TCP connections on 127.0.0.1:<port>?
port_listening() {
    PY - "$1" <<'PY'
import socket, sys
s = socket.socket(); s.settimeout(1)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

# Can we bind 127.0.0.1:<port>? (i.e. is it free)
port_free() {
    PY - "$1" <<'PY'
import socket, sys
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", int(sys.argv[1]))); s.close(); sys.exit(0)
except OSError:
    sys.exit(1)
PY
}

# Does the connection file pin the kernel to our fixed shell port?
file_on_fixed_port() {
    [ -f "$CONNECTION_FILE" ] || return 1
    grep -Eq "\"shell_port\"[[:space:]]*:[[:space:]]*${SHELL_PORT}\b" "$CONNECTION_FILE"
}

# List PIDs of ipykernel_launcher processes owned by the current uid, by
# scanning /proc (the base image has no procps, so no pgrep/pkill available).
list_kernels() {
    PY - <<'PY'
import os
me = os.getuid()
out = []
for e in os.listdir("/proc"):
    if not e.isdigit():
        continue
    pid = int(e)
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        if "ipykernel_launcher" in cmd and os.stat(f"/proc/{pid}").st_uid == me:
            out.append(str(pid))
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
print(" ".join(out))
PY
}

# Send a signal to every ipykernel_launcher process this uid owns.
signal_kernels() {
    local sig="$1"
    PY - "$sig" <<'PY'
import os, sys, signal
sig = getattr(signal, "SIG" + sys.argv[1], signal.SIGTERM)
me, mypid, ppid = os.getuid(), os.getpid(), os.getppid()
for e in os.listdir("/proc"):
    if not e.isdigit():
        continue
    pid = int(e)
    if pid in (mypid, ppid):
        continue
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        if "ipykernel_launcher" in cmd and os.stat(f"/proc/{pid}").st_uid == me:
            os.kill(pid, sig)
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
PY
}

# Kill every kernel this user owns and wait for the ports to clear. This is the
# key fix: we never relaunch onto a port a stale kernel still holds (which would
# leave the client talking to an old kernel with a mismatched HMAC key).
reap_kernels() {
    signal_kernels TERM
    rm -f "$PID_FILE"

    # Wait up to ~10s for all ports to be released.
    local tries=20 port busy
    while [ $tries -gt 0 ]; do
        busy=0
        for port in $ALL_PORTS; do
            port_free "$port" || busy=1
        done
        [ "$busy" -eq 0 ] && return 0
        # Escalate to SIGKILL halfway through.
        [ $tries -eq 10 ] && signal_kernels KILL
        sleep 0.5
        tries=$((tries - 1))
    done
    fail "Ports ($ALL_PORTS) still busy after reaping kernels. A process from another user may hold them — run 'gpudev kernel doctor $CLIENT_NAME'."
}

write_connection_file() {
    local key
    key="$(PY -c 'import uuid; print(uuid.uuid4())')"

    cat > "$CONNECTION_FILE" <<EOF
{
  "shell_port":   ${SHELL_PORT},
  "iopub_port":   ${IOPUB_PORT},
  "stdin_port":   ${STDIN_PORT},
  "control_port": ${CONTROL_PORT},
  "hb_port":      ${HB_PORT},
  "ip":           "127.0.0.1",
  "key":          "${key}",
  "transport":    "tcp",
  "signature_scheme": "hmac-sha256",
  "kernel_name":  "python3"
}
EOF
    chmod 600 "$CONNECTION_FILE"
}

launch_kernel() {
    # Start the kernel from $HOME so notebook cells (!pwd, open('file'), etc.)
    # land in a writable per-client workspace instead of "/" (which is where
    # sshd's forced-command shell would otherwise leave us).
    cd "$HOME_DIR"
    # Activate the venv for the kernel PROCESS so notebook `!pip install`,
    # `!python`, and `!uv pip install` all resolve to THIS venv — not /opt/venv or
    # the system Python. IPython's `!` spawns a non-login shell that inherits the
    # kernel's environment but does NOT source /etc/profile, so the profile.d
    # activation (which only fixes SSH login shells) never reaches it. Setting it
    # here — VIRTUAL_ENV + venv-first PATH — is the only thing that does.
    export VIRTUAL_ENV="${VENV}"
    export UV_PROJECT_ENVIRONMENT="${VENV}"
    export PATH="${VENV}/bin:${PATH}"
    nohup "${VENV}/bin/python" -m ipykernel_launcher -f "$CONNECTION_FILE" \
        >"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"

    # Wait until the kernel is actually accepting connections on the shell port.
    local pid tries=30
    pid="$(cat "$PID_FILE")"
    while [ $tries -gt 0 ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            log "----- kernel.log -----"
            tail -n 20 "$LOG_FILE" 2>/dev/null || true
            fail "Kernel (pid $pid) exited during startup. See log above."
        fi
        if port_listening "$SHELL_PORT"; then
            return 0
        fi
        sleep 0.5
        tries=$((tries - 1))
    done
    log "----- kernel.log -----"
    tail -n 20 "$LOG_FILE" 2>/dev/null || true
    fail "Kernel (pid $pid) did not start listening on $SHELL_PORT in time."
}

cmd_start() {
    require_venv
    acquire_lock          # exclusive: no concurrent reap/launch can race us

    # Reuse only a kernel that is alive, pinned to our fixed port in the file,
    # AND actually listening — otherwise the file key and the live kernel can
    # diverge and the client gets HMAC signature errors.
    local pid
    if pid="$(kernel_pid)" && file_on_fixed_port && port_listening "$SHELL_PORT"; then
        log "Kernel already running (pid $pid)."
        log "Connection file: $CONNECTION_FILE"
        return 0
    fi

    # Anything else: clean slate so file key == live kernel key.
    reap_kernels
    write_connection_file
    launch_kernel

    log "Kernel started (pid $(cat "$PID_FILE"))."
    log "Connection file: $CONNECTION_FILE"
}

cmd_stop() {
    acquire_lock          # exclusive: don't race a concurrent start/restart
    local pid
    if ! pid="$(kernel_pid)"; then
        log "Kernel is not running."
        rm -f "$PID_FILE"
        return 0
    fi
    reap_kernels
    log "Kernel stopped (was pid $pid)."
}

cmd_restart() {
    require_venv
    acquire_lock          # exclusive: serialize with any concurrent start/restart
    log "Restarting kernel..."
    reap_kernels
    write_connection_file
    launch_kernel
    log "Kernel restarted (pid $(cat "$PID_FILE"))."
    log "Connection file: $CONNECTION_FILE"
}

cmd_status() {
    local pid
    if pid="$(kernel_pid)"; then
        log "Kernel:           running (pid $pid)"
    else
        log "Kernel:           stopped"
        rm -f "$PID_FILE"
    fi
    log "Client:           $CLIENT_NAME"
    log "Venv:             $VENV"
    log "Connection file:  $CONNECTION_FILE"
}

# Diagnostics: prove (or disprove) the "stale kernel / key mismatch" theory.
cmd_doctor() {
    require_venv
    ensure_dirs
    log "=== gpudev kernel doctor ($CLIENT_NAME) ==="
    log "whoami:           $(id -un) (uid $(id -u))"
    local pid
    if pid="$(kernel_pid)"; then
        log "PID file kernel:  pid $pid (alive)"
    else
        log "PID file kernel:  none / dead"
    fi
    log "ipykernel procs:  $(list_kernels)"
    if [ -f "$CONNECTION_FILE" ]; then
        log "key (file):       $(PY -c "import json;print(json.load(open('$CONNECTION_FILE'))['key'])" 2>/dev/null)"
    else
        log "connection file:  MISSING"
    fi
    local port
    for port in $ALL_PORTS; do
        if port_listening "$port"; then
            log "port $port:       LISTENING"
        else
            log "port $port:       free"
        fi
    done
    log "----- last 15 lines of kernel.log -----"
    tail -n 15 "$LOG_FILE" 2>/dev/null || log "(no log)"
}

usage() {
    cat <<EOF
Usage: kernel-manager.sh <command>

Commands:
  start    Launch the Jupyter kernel (reuses a healthy one, else clean restart)
  stop     Stop the kernel and free its ports
  restart  Force a fresh kernel with a new connection key
  status   Show kernel state and connection file path
  doctor   Print diagnostics (pids, ports, key, log tail)
EOF
}

case "${1:-}" in
    start)   cmd_start   ;;
    stop)    cmd_stop    ;;
    restart) cmd_restart ;;
    status)  cmd_status  ;;
    doctor)  cmd_doctor  ;;
    *)       usage; exit 1 ;;
esac
