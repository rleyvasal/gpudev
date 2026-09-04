#!/usr/bin/env bash
set -euo pipefail

# gpudev client-setup.sh
# Provisions a new client container on the gpudev host.
# Run on the WSL2 or bare Linux host (not inside a container).
#
# Usage: client-setup.sh <client_name>

CONFIG_DIR="${HOME}/.config/gpudev"
HOST_CONFIG="${CONFIG_DIR}/host.json"
CLIENTS_CONFIG="${CONFIG_DIR}/clients.json"
BASE_IMAGE="gpudev-base:latest"
KERNEL_MANAGER_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/kernel-manager.sh"

# ── Image variants ────────────────────────────────────────────────────────────
# GPUDEV_VARIANT selects which base image a client is built on. Empty/"default"
# keeps the standard image, so every existing caller is unaffected.
#
#   default   gpudev-base:latest            python:3.12-slim + torch (no CUDA toolkit)
#   cuda-dev  gpudev-base-cuda-dev:latest   CUDA 12.8 devel + nvcc/ncu/nsys + TensorRT
#
# Build the cuda-dev image first with: gpudev image build cuda-dev
GPUDEV_VARIANT="${GPUDEV_VARIANT:-default}"

# Shared memory for every client. Docker's 64MB default is far too small for
# PyTorch DataLoaders: each worker maps shared memory for batch handoff, so
# multi-worker loading dies with a bus error / "DataLoader worker killed" that
# names nothing about shm. Raising it is free when unused — it is a ceiling, not
# an allocation. --ipc=host would also fix it but drops IPC isolation between
# clients, which is a worse trade on a multi-tenant host.
CLIENT_SHM_SIZE="${GPUDEV_SHM_SIZE:-8g}"

resolve_variant_image() {
    case "$1" in
        ""|default) echo "gpudev-base:latest" ;;
        cuda-dev)   echo "gpudev-base-cuda-dev:latest" ;;
        *)          fail "Unknown variant '$1'. Known variants: default, cuda-dev." ;;
    esac
}

# Extra docker run flags a variant needs. SYS_ADMIN is granted ONLY to cuda-dev:
# Nsight Compute needs it to read GPU performance counters from inside a
# container, but it is close to root on the host, so it must never be the default
# for ordinary clients.
variant_run_flags() {
    case "$1" in
        cuda-dev) echo "--cap-add=SYS_ADMIN --cap-add=PERFMON" ;;
        *)        echo "" ;;
    esac
}

# Inside every gpudev container the UNIX user is uniform — `gpudev`. The client
# *identity* (which container, which volume, which DNS hostname) is carried by
# the container's --name / volume name / cf hostname instead. This makes prompts
# obviously different from the notebook side (gpudev@gpudev-<name> after SSH),
# keeps in-container paths stable (/home/gpudev/...), and lets CRAFT.py hardcode
# its kernel paths.
CONTAINER_USER="gpudev"
CONTAINER_HOME="/home/${CONTAINER_USER}"

log()  { echo "$*"; }
step() { echo ""; echo "=== $1 ==="; }
warn() { echo "Warning: $*" >&2; }
fail() { echo "Error: $*" >&2; exit 1; }

command_exists() { command -v "$1" >/dev/null 2>&1; }

# ── Validation ────────────────────────────────────────────────────────────────

sanitize_name() {
    printf '%s' "$1" \
      | tr '[:upper:]' '[:lower:]' \
      | sed -E 's/[^a-z0-9-]+/-/g; s/-+/-/g; s/^-+//; s/-+$//'
}

validate_public_key() {
    local key="$1"
    local type
    type="$(printf '%s' "$key" | awk '{print $1}')"
    case "$type" in
        ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521)
            return 0 ;;
        *)
            return 1 ;;
    esac
}

require_host_setup() {
    [ -f "$HOST_CONFIG" ] || fail "Host not set up. Run linux-setup.sh first."
    [ -f "$CLIENTS_CONFIG" ] || fail "clients.json missing. Run linux-setup.sh first."
    docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 || fail "Base image '$BASE_IMAGE' not found. Run linux-setup.sh first."
    command_exists cloudflared || fail "cloudflared not found. Run linux-setup.sh first."
}

# ── Host config helpers ───────────────────────────────────────────────────────

host_get() {
    local field="$1"
    python3 -c "
import json, pathlib
d = json.loads(pathlib.Path('${HOST_CONFIG}').read_text())
print(d.get('${field}', ''))
"
}

client_exists() {
    local name="$1"
    python3 -c "
import json, sys
data = json.loads(open('${CLIENTS_CONFIG}').read())
sys.exit(0 if any(c['name'] == '${name}' for c in data['clients']) else 1)
"
}

next_port() {
    python3 -c "
import json
data = json.loads(open('${CLIENTS_CONFIG}').read())
base = int('${PORT_BASE}')
used = {c['ssh_port'] for c in data['clients'] if 'ssh_port' in c}
port = base
while port in used:
    port += 1
print(port)
"
}

register_client() {
    local name="$1" ssh_port="$2" added_at="$3"
    python3 -c "
import json
path = '${CLIENTS_CONFIG}'
data = json.loads(open(path).read())
data['clients'] = [c for c in data['clients'] if c['name'] != '${name}']
data['clients'].append({
    'name':         '${name}',
    'ssh_port':     int('${ssh_port}'),
    'added_at':     '${added_at}',
    # Recorded so rebuild/restart put the client back on the SAME image it was
    # created on. Older records predate this field; readers treat a missing
    # value as 'default'.
    'variant':      '${GPUDEV_VARIANT}',
})
data['clients'].sort(key=lambda c: c['name'])
open(path, 'w').write(json.dumps(data, indent=2))
"
    chmod 600 "$CLIENTS_CONFIG"
}

# ── Cloudflare tunnel ─────────────────────────────────────────────────────────

add_client_to_host_tunnel() {
    local name="$1" ssh_port="$2" cf_hostname="$3"
    local config_yml="${HOME}/.cloudflared/config.yml"

    [ -f "$config_yml" ] || fail "Host tunnel config not found at $config_yml. Run linux-setup.sh first."

    # Add DNS route for the client hostname on the existing host tunnel.
    # Tunnel name matches the linux user set during linux-setup.sh.
    local tunnel_name
    tunnel_name="$(host_get linux_user)"
    cloudflared tunnel route dns --overwrite-dns "$tunnel_name" "$cf_hostname" \
        || log "DNS route for $cf_hostname could not be set — check Cloudflare dashboard."

    # Inject a new ingress rule before the catch-all if not already present
    if grep -qF "hostname: ${cf_hostname}" "$config_yml"; then
        log "Ingress rule for $cf_hostname already in host config.yml."
    else
        python3 -c "
import re, pathlib
p = pathlib.Path('${config_yml}')
content = p.read_text()
rule = '  - hostname: ${cf_hostname}\n    service: ssh://localhost:${ssh_port}\n'
# Insert before the catch-all '  - service: ...' line
content = re.sub(r'(  - service: http_status:404)', rule + r'\1', content, count=1)
p.write_text(content)
"
        log "Added ingress rule: $cf_hostname → localhost:$ssh_port"
    fi

    # DO NOT reload the connector here. `gpudev client add` is usually run over
    # `ssh gpudev`, carried by this very cloudflared connector; reloading it now —
    # restart OR SIGHUP, cloudflared re-dials its edge either way — drops this
    # session mid-setup and aborts the add before the container is built (that was
    # the original bug, and SIGHUP proved no gentler). The connector only needs the
    # new ingress once everything else is done, so we defer the reload to the very
    # end of main() and run it DETACHED (reload_tunnel_connector), where a dropped
    # session can no longer interrupt anything.
    NEED_TUNNEL_RELOAD=1
    RELOAD_TUNNEL_NAME="$tunnel_name"
    log "Ingress rule staged; tunnel reload deferred to the end (keeps this session alive during setup)."
}

# Restarting the connector drops every connection it carries — but ONLY those.
# cloudflared dials sshd on localhost, so a session riding the tunnel reports
# 127.0.0.1 as its peer, while a LAN or console session reports a real address.
# Measured on the host:
#   tunnel -> SSH_CONNECTION="127.0.0.1 55396 127.0.0.1 52100"
#   LAN    -> SSH_CONNECTION="192.168.10.59 51214 192.168.10.80 52100"
# Only the first case needs the deferred/detached dance; everywhere else the
# reload can run inline and be VERIFIED before the command returns.
session_rides_tunnel() {
    case "${SSH_CONNECTION%% *}" in
        127.0.0.1|::1) return 0 ;;
        *)             return 1 ;;
    esac
}

# The connector must have started AFTER the last ingress edit. If config.yml is
# newer than the service's start time, cloudflared is still serving the rules it
# loaded at boot and the newest client's hostname answers "websocket: bad
# handshake" — exactly the silent failure this whole path exists to prevent.
connector_is_current() {
    local config_yml="${HOME}/.cloudflared/config.yml" ts started changed
    [ -f "$config_yml" ] || return 0
    command_exists systemctl || return 0
    ts="$(systemctl show gpudev-tunnel -p ActiveEnterTimestamp --value 2>/dev/null)"
    [ -n "$ts" ] || return 0
    started="$(date -d "$ts" +%s 2>/dev/null)" || return 0
    changed="$(stat -c %Y "$config_yml" 2>/dev/null)" || return 0
    [ "$started" -ge "$changed" ]
}

# A host set up before the NOPASSWD grant existed should become self-sufficient
# at the first client change rather than needing a full linux-setup.sh re-run.
# Costs one interactive sudo; skipped silently when there is no TTY to ask on.
install_tunnel_reload_grant() {
    local sudoers_file="/etc/sudoers.d/gpudev-tunnel" user="${USER:-$(whoami)}"
    [ -t 0 ] || return 1
    log "Installing a NOPASSWD grant so future reloads need no password..."
    sudo tee "$sudoers_file" >/dev/null <<EOF || return 1
# gpudev: allow ${user} to reload the tunnel connector after a client change.
${user} ALL=(root) NOPASSWD: /usr/bin/systemctl restart gpudev-tunnel, /bin/systemctl restart gpudev-tunnel
EOF
    sudo chmod 440 "$sudoers_file" || return 1
    if sudo visudo -cf "$sudoers_file" >/dev/null 2>&1; then
        log "  sudoers: reloads no longer prompt for a password."
        return 0
    fi
    warn "tunnel sudoers file failed validation — removing it to avoid breaking sudo."
    sudo rm -f "$sudoers_file"
    return 1
}

# Can the detached reload run without a password? The grant is deliberately
# scoped to ONE command, so probing anything else answers the wrong question:
# `sudo -n systemctl show gpudev-tunnel` is DENIED on a correctly granted host,
# which made this path report "cannot run unattended" while the restart it
# actually needs was permitted. `sudo -l <cmd>` asks whether exactly that
# command is allowed, without running it. Blanket passwordless sudo counts too.
tunnel_restart_permitted() {
    sudo -n -l /usr/bin/systemctl restart gpudev-tunnel >/dev/null 2>&1 \
        || sudo -n -l /bin/systemctl restart gpudev-tunnel >/dev/null 2>&1 \
        || sudo -n true 2>/dev/null
}

reload_tunnel_connector() {
    local tunnel_name="$1"
    echo ""

    # Legacy non-systemd connector: unchanged detached pkill+respawn.
    if ! command_exists systemctl || ! systemctl is-active gpudev-tunnel >/dev/null 2>&1; then
        log "Reloading the cloudflared connector (detached, no systemd unit)."
        setsid bash -c "sleep 3
pkill -f 'cloudflared tunnel run ${tunnel_name}' 2>/dev/null
sleep 1
nohup cloudflared tunnel run '${tunnel_name}' >>'${HOME}/.cloudflared/tunnel.log' 2>&1" \
            >"${HOME}/.cloudflared/reload.log" 2>&1 </dev/null &
        log "Tunnel reload scheduled (detached) — log: ~/.cloudflared/reload.log"
        return 2
    fi

    if session_rides_tunnel; then
        # Restarting now would kill this very shell mid-command. Defer + detach,
        # but the detached process has NO TTY, so it can only use `sudo -n`:
        # make sure that will work before walking away from it.
        if ! tunnel_restart_permitted; then
            install_tunnel_reload_grant || true
        fi
        if ! tunnel_restart_permitted; then
            warn "Ingress updated, but the connector still serves the OLD config and this"
            warn "shell rides the tunnel, so the reload cannot run unattended."
            warn "Run this on the host (console or LAN ssh) to finish:"
            warn "  sudo systemctl restart gpudev-tunnel"
            return 2
        fi
        log "This shell rides the connector, so the restart is deferred a few seconds."
        log "The session will drop — reconnect and the new hostname is live."
        setsid bash -c 'sleep 3; sudo -n systemctl restart gpudev-tunnel' \
            >"${HOME}/.cloudflared/reload.log" 2>&1 </dev/null &
        return 2
    fi

    # Not riding the tunnel: restart inline and prove it worked.
    log "Restarting the cloudflared connector to publish the new ingress..."
    if ! sudo -n systemctl restart gpudev-tunnel 2>/dev/null; then
        if [ -t 0 ]; then
            install_tunnel_reload_grant || true
            sudo systemctl restart gpudev-tunnel || {
                warn "Could not restart gpudev-tunnel. Run: sudo systemctl restart gpudev-tunnel"
                return 2
            }
        else
            warn "Ingress updated, but restarting the connector needs a password."
            warn "Run this on the host to finish:"
            warn "  sudo systemctl restart gpudev-tunnel"
            return 2
        fi
    fi

    local tries=20
    while [ $tries -gt 0 ]; do
        if systemctl is-active gpudev-tunnel >/dev/null 2>&1 && connector_is_current; then
            log "Connector reloaded and serving the new ingress."
            return 0
        fi
        sleep 1
        tries=$((tries - 1))
    done
    warn "Connector did not report current within 20s."
    warn "Check: systemctl status gpudev-tunnel"
    return 2
}

# Prove the new client is actually reachable the way the notebook will reach it:
# DNS -> Cloudflare edge -> connector ingress -> the container's sshd. An HTTPS
# probe cannot show this — Cloudflare Access answers at the edge with a login
# page (HTTP 200) before the connector is consulted, and an unserved hostname
# falls to the http_status:404 catch-all, so both read as "reachable". Reading
# the origin's SSH banner takes the real path. ~0.6s.
verify_client_reachable() {
    local h="$1" port=$((40000 + RANDOM % 9000)) pid banner="" i
    command_exists cloudflared || return 0
    step "Verifying the client through the tunnel"
    timeout 20 cloudflared access tcp --hostname "$h" --url "127.0.0.1:${port}" \
        >/dev/null 2>&1 &
    pid=$!
    for i in $(seq 1 30); do
        (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null && break
        sleep 0.3
    done
    banner="$(timeout 8 bash -c "exec 3<>/dev/tcp/127.0.0.1/${port}; head -c 8 <&3" 2>/dev/null || true)"
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true

    # A hostname created moments ago is not reachable at Cloudflare's edge
    # immediately: `tunnel route dns` returns before the CNAME has propagated.
    # A single probe therefore reports NO SSH BANNER on a client that is
    # perfectly healthy, and sends the operator to restart a connector that is
    # already current. Retry before believing the failure.
    local attempt=2
    while [ -z "$banner" ] && [ $attempt -le 6 ]; do
        log "  not reachable yet — DNS may still be propagating (attempt ${attempt}/6)"
        sleep 10
        port=$((40000 + RANDOM % 9000))
        timeout 20 cloudflared access tcp --hostname "$h" --url "127.0.0.1:${port}" \
            >/dev/null 2>&1 &
        pid=$!
        for i in $(seq 1 30); do
            (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null && break
            sleep 0.3
        done
        banner="$(timeout 8 bash -c "exec 3<>/dev/tcp/127.0.0.1/${port}; head -c 8 <&3" 2>/dev/null || true)"
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        attempt=$((attempt + 1))
    done

    case "$banner" in
        SSH-2.0*)
            log "  ${h} → OK (the container's sshd answered through the tunnel)"
            log "  The client can connect now."
            return 0 ;;
        "")
            warn "  ${h} → NO SSH BANNER: the tunnel does not reach this client yet."
            warn "  The container is created; only the tunnel path is failing."
            warn "  Try:  sudo systemctl restart gpudev-tunnel"
            warn "  Then: gpudev cloudflare    (shows connector staleness per hostname)"
            return 1 ;;
        *)
            warn "  ${h} → unexpected reply through the tunnel: ${banner}"
            return 1 ;;
    esac
}

# ── Container init ────────────────────────────────────────────────────────────


setup_ssh_authorized_keys() {
    local name="$1" public_key="$2"

    docker run --rm \
        -v "${name}-data:${CONTAINER_HOME}" \
        "$BASE_IMAGE" bash -c "
useradd -M -s /bin/bash -d ${CONTAINER_HOME} ${CONTAINER_USER} 2>/dev/null || true
mkdir -p ${CONTAINER_HOME}/.ssh
echo '${public_key}' > ${CONTAINER_HOME}/.ssh/authorized_keys
chown -R ${CONTAINER_USER}:${CONTAINER_USER} ${CONTAINER_HOME}
chmod 700 ${CONTAINER_HOME}/.ssh
chmod 600 ${CONTAINER_HOME}/.ssh/authorized_keys
"
}

setup_client_venv() {
    local name="$1"

    log "Creating thin client venv at ${CONTAINER_HOME}/.venv (base packages overlaid from /opt/venv)..."

    docker run --rm \
        -v "${name}-data:${CONTAINER_HOME}" \
        "$BASE_IMAGE" bash -c "
if [ -x ${CONTAINER_HOME}/.venv/bin/python ]; then
    echo 'Client venv already exists, skipping.'
    exit 0
fi
# Thin per-client venv. Base packages (torch, ipykernel, numpy, ...) are NOT copied
# in — they're referenced from the image's read-only /opt/venv via the .pth overlay
# below. So this venv holds only the USER's own installs: it stays small, survives
# rebuilds on the data volume, and the image can update base packages independently.
# --seed gives it its own pip, so a bare 'pip install' also lands here (not ~/.local).
uv venv ${CONTAINER_HOME}/.venv --python 3.12 --seed
# Overlay: this .pth makes Python append /opt/venv's site-packages to sys.path at
# startup. Appended dirs rank BELOW the venv's own packages, so a user-installed
# version cleanly shadows the base one. Path is python3.12-specific, matching the
# pinned interpreter above (and the Dockerfile's /opt/venv).
echo /opt/venv/lib/python3.12/site-packages \
    > ${CONTAINER_HOME}/.venv/lib/python3.12/site-packages/zzz_base_overlay.pth
echo 'Client venv ready (thin + base overlay).'
"
}

install_kernel_manager() {
    local name="$1"

    [ -f "$KERNEL_MANAGER_SRC" ] || fail "kernel-manager.sh not found at $KERNEL_MANAGER_SRC"

    docker run --rm \
        -v "${name}-data:${CONTAINER_HOME}" \
        -v "${KERNEL_MANAGER_SRC}:/tmp/kernel-manager.sh:ro" \
        "$BASE_IMAGE" bash -c "
mkdir -p ${CONTAINER_HOME}/bin
cp /tmp/kernel-manager.sh ${CONTAINER_HOME}/bin/kernel-manager.sh
chmod +x ${CONTAINER_HOME}/bin/kernel-manager.sh
"
}

write_startup_script() {
    local name="$1"

    local tmp_script
    tmp_script="$(mktemp)"

    # Host keys live on the data volume so container recreate/rebuild keeps the
    # same SSH fingerprints — notebook known_hosts stays valid.
    cat > "$tmp_script" <<EOF
#!/bin/bash
set -e

# Ensure OS user exists (base image has no users beyond root)
useradd -M -s /bin/bash -d ${CONTAINER_HOME} ${CONTAINER_USER} 2>/dev/null || true

# Fix ownership of entire home dir (covers .local, .ssh, .venv, bin)
chown -R ${CONTAINER_USER}:${CONTAINER_USER} ${CONTAINER_HOME}

# ── Persistent SSH host keys (data volume) ──────────────────────────────────
# Keys under /etc/ssh are baked into the image layer and rotate on every
# container recreate. Store ed25519+rsa keys on the client volume instead and
# point sshd only at those so rebuilds do not break StrictHostKeyChecking.
HOSTKEY_DIR="${CONTAINER_HOME}/.local/share/ssh/hostkeys"
mkdir -p "\$HOSTKEY_DIR"
if [ ! -f "\$HOSTKEY_DIR/ssh_host_ed25519_key" ]; then
    ssh-keygen -t ed25519 -f "\$HOSTKEY_DIR/ssh_host_ed25519_key" -N "" -q
fi
if [ ! -f "\$HOSTKEY_DIR/ssh_host_rsa_key" ]; then
    ssh-keygen -t rsa -b 3072 -f "\$HOSTKEY_DIR/ssh_host_rsa_key" -N "" -q
fi
# sshd requires host private keys owned by root and not group/world-readable.
chown -R root:root "\$HOSTKEY_DIR"
chmod 700 "\$HOSTKEY_DIR"
chmod 600 "\$HOSTKEY_DIR"/ssh_host_*_key
chmod 644 "\$HOSTKEY_DIR"/ssh_host_*.pub 2>/dev/null || true

# Build a config that includes distro defaults but replaces HostKey lines so we
# never also advertise the ephemeral /etc/ssh/ssh_host_* keys from the image.
SSHD_CONFIG="\$HOSTKEY_DIR/sshd_config"
{
    sed '/^HostKey /d' /etc/ssh/sshd_config
    echo "HostKey \$HOSTKEY_DIR/ssh_host_ed25519_key"
    echo "HostKey \$HOSTKEY_DIR/ssh_host_rsa_key"
} > "\$SSHD_CONFIG"
chmod 600 "\$SSHD_CONFIG"

# Start SSH daemon (daemonizes by default)
/usr/sbin/sshd -f "\$SSHD_CONFIG"

# ── Persistent Mojo/pixi project (data volume) ──────────────────────────────
# Seed from the image (/opt/mojo-proj) once; runtime packages from %mojo_add
# live under ~/.mojo-proj and survive client rebuild.
MOJO_SEED=/opt/mojo-proj
MOJO_HOME="${CONTAINER_HOME}/.mojo-proj"
if [ -d "\$MOJO_SEED" ] && [ ! -f "\$MOJO_HOME/pixi.toml" ]; then
    cp -a "\$MOJO_SEED" "\$MOJO_HOME"
fi
if [ -d "\$MOJO_HOME" ]; then
    chown -R ${CONTAINER_USER}:${CONTAINER_USER} "\$MOJO_HOME" 2>/dev/null || true
fi
# Login shells + kernel process see the volume path (not the image seed).
printf '%s\n' \
    'export MOJO_PROJ=/home/gpudev/.mojo-proj' \
    'export PATH="/opt/pixi/bin:\$PATH"' \
    > /etc/profile.d/20-gpudev-mojo.sh

# Start Jupyter kernel as the gpudev user. GPUDEV_CLIENT identifies which
# client this container belongs to (for logs and 'gpudev kernel doctor').
export GPUDEV_CLIENT=${name}
export MOJO_PROJ=${CONTAINER_HOME}/.mojo-proj
su -s /bin/bash ${CONTAINER_USER} -c "GPUDEV_CLIENT=${name} MOJO_PROJ=${CONTAINER_HOME}/.mojo-proj ${CONTAINER_HOME}/bin/kernel-manager.sh start"

exec sleep infinity
EOF

    docker run --rm \
        -v "${name}-data:${CONTAINER_HOME}" \
        -v "${tmp_script}:/tmp/start.sh:ro" \
        "$BASE_IMAGE" bash -c "
cp /tmp/start.sh ${CONTAINER_HOME}/start.sh
chmod +x ${CONTAINER_HOME}/start.sh
"
    rm -f "$tmp_script"
}

start_container() {
    local name="$1" ssh_port="$2"

    if docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
        log "Container '$name' already exists — removing and recreating."
        docker rm -f "$name"
    fi

    # --hostname gpudev-<name>: prompt becomes gpudev@gpudev-<name>:~$ after
    # SSH, clearly different from the notebook side.
    # Word-splitting on $extra_flags is intended: it carries zero or more separate
    # docker flags, and quoting it would pass them as one bogus argument.
    local extra_flags
    extra_flags="$(variant_run_flags "$GPUDEV_VARIANT")"
    # shellcheck disable=SC2086
    docker run -d \
        --name "$name" \
        --hostname "gpudev-${name}" \
        --gpus all \
        --shm-size="$CLIENT_SHM_SIZE" \
        $extra_flags \
        --restart unless-stopped \
        -v "${name}-data:${CONTAINER_HOME}" \
        -p "127.0.0.1:${ssh_port}:22" \
        -e "GPUDEV_CLIENT=${name}" \
        -e "GPUDEV_VARIANT=${GPUDEV_VARIANT}" \
        "$BASE_IMAGE" \
        "${CONTAINER_HOME}/start.sh"

    log "Container '$name' started (variant: ${GPUDEV_VARIANT}, shm: ${CLIENT_SHM_SIZE})."
    # NOT `[ -n ... ] && log ...`: the default variant has no extra flags, so as
    # the last statement of this function the AND-list returns non-zero, the
    # function returns non-zero, and `set -e` aborts the caller — which fired the
    # EXIT trap and rolled the whole client back right after the container came
    # up. Only cuda-dev (non-empty flags) escaped it, which is why this survived.
    if [ -n "$extra_flags" ]; then
        log "  Extra capabilities: ${extra_flags}"
    fi
}

# ── Health check ──────────────────────────────────────────────────────────────

wait_for_container() {
    local name="$1"
    local tries=15

    log "Waiting for container to be ready..."
    while [ $tries -gt 0 ]; do
        if docker exec "$name" pgrep sshd >/dev/null 2>&1; then
            log "Container '$name' is ready."
            return 0
        fi
        sleep 2
        tries=$((tries - 1))
    done
    warn "Container '$name' may not be fully ready. Check: docker logs $name"
}

# ── Partial-provision cleanup ─────────────────────────────────────────────────
# If client add fails after creating a volume / tunnel rule / container but before
# clients.json registration, tear those down so a retry is clean.

remove_tunnel_ingress() {
    local cf_hostname="$1"
    local config_yml="${HOME}/.cloudflared/config.yml"
    [ -f "$config_yml" ] || return 0
    grep -qF "hostname: ${cf_hostname}" "$config_yml" || return 0

    python3 -c "
import pathlib
p = pathlib.Path('${config_yml}')
lines = p.read_text().splitlines(keepends=True)
out = []
skip_next = False
for line in lines:
    if 'hostname: ${cf_hostname}' in line:
        skip_next = True
        continue
    if skip_next:
        skip_next = False
        continue
    out.append(line)
p.write_text(''.join(out))
"
    log "  Removed ingress rule for $cf_hostname"

    local tunnel_name
    tunnel_name="$(host_get linux_user 2>/dev/null || true)"
    if [ -n "$tunnel_name" ] && command_exists systemctl && systemctl is-active gpudev-tunnel >/dev/null 2>&1; then
        sudo -n systemctl restart gpudev-tunnel 2>/dev/null || true
    fi
}

cleanup_partial_client() {
    # Only runs on failure (see trap). Never touches a fully registered client.
    [ "${_CLIENT_ADD_OK:-}" = "1" ] && return 0
    local name="${_PARTIAL_NAME:-}"
    local cf_hostname="${_PARTIAL_CF_HOSTNAME:-}"
    [ -n "$name" ] || return 0

    # If registration completed, leave everything alone.
    if client_exists "$name" 2>/dev/null; then
        return 0
    fi

    warn "Client add failed — cleaning up partial provision for '$name'..."

    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$name"; then
        docker rm -f "$name" >/dev/null 2>&1 || true
        log "  Removed container '$name'"
    fi

    if [ "${_PARTIAL_VOLUME:-}" = "1" ] \
        && docker volume ls --format '{{.Name}}' 2>/dev/null | grep -qx "${name}-data"; then
        docker volume rm "${name}-data" >/dev/null 2>&1 || true
        log "  Removed volume '${name}-data'"
    fi

    if [ "${_PARTIAL_TUNNEL:-}" = "1" ] && [ -n "$cf_hostname" ]; then
        remove_tunnel_ingress "$cf_hostname" || true
        warn "  DNS CNAME for $cf_hostname may still exist (delete manually if re-add fails)."
    fi

    warn "Cleanup done. Safe to re-run: gpudev client add $name"
}

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
    # Refresh only start.sh on an existing volume (used by `gpudev client rebuild`).
    if [ "${1:-}" = "--refresh-start" ]; then
        local refresh_name="${2:-}"
        [ -n "$refresh_name" ] || fail "Usage: client-setup.sh --refresh-start <client_name>"
        refresh_name="$(sanitize_name "$refresh_name")"
        write_startup_script "$refresh_name"
        log "Refreshed start.sh for '$refresh_name' (persistent SSH host keys)."
        return 0
    fi

    local raw_name="${1:-}"
    local ssh_key_arg="${2:-}"
    [ -n "$raw_name" ] || fail "Usage: client-setup.sh <client_name> <ssh_public_key>"

    CLIENT_NAME="$(sanitize_name "$raw_name")"
    [ -n "$CLIENT_NAME" ] || fail "Invalid client name after sanitization."

    # Resolve the variant BEFORE anything uses $BASE_IMAGE. GPUDEV_VARIANT is set
    # by `gpudev client add --variant`, and defaults to the standard image.
    BASE_IMAGE="$(resolve_variant_image "$GPUDEV_VARIANT")"
    docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 || fail "Image '$BASE_IMAGE' not found.
Variant '${GPUDEV_VARIANT}' needs its image built first:
  gpudev image build ${GPUDEV_VARIANT}"

    require_host_setup

    PORT_BASE="$(host_get port_base)"
    CF_DOMAIN="$(host_get cf_domain)"
    [ -n "$CF_DOMAIN" ] || fail "cf_domain not set in host.json. Re-run linux-setup.sh."

    CF_HOSTNAME="${CLIENT_NAME}.${CF_DOMAIN}"

    if client_exists "$CLIENT_NAME"; then
        fail "Client '$CLIENT_NAME' already exists. Use 'gpudev client remove $CLIENT_NAME' first."
    fi

    # SSH public key — passed as second argument or via GPUDEV_SSH_KEY env var
    if [ -n "$ssh_key_arg" ]; then
        SSH_PUBLIC_KEY="$ssh_key_arg"
    elif [ -n "${GPUDEV_SSH_KEY:-}" ]; then
        SSH_PUBLIC_KEY="$GPUDEV_SSH_KEY"
    else
        fail "Usage: client-setup.sh <client_name> <ssh_public_key>"
    fi
    validate_public_key "$SSH_PUBLIC_KEY" || fail "Invalid SSH public key format."

    SSH_PORT="$(next_port)"
    ADDED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

    echo ""
    log "Client:     $CLIENT_NAME"
    log "SSH port:   $SSH_PORT (host-local, tunnel only)"
    log "Hostname:   $CF_HOSTNAME"
    echo ""

    # Partial-failure tracking for cleanup_partial_client.
    _PARTIAL_NAME="$CLIENT_NAME"
    _PARTIAL_CF_HOSTNAME="$CF_HOSTNAME"
    _PARTIAL_VOLUME=0
    _PARTIAL_TUNNEL=0
    _CLIENT_ADD_OK=0
    trap cleanup_partial_client EXIT

    step "Step 1: Create Docker volume"
    docker volume create "${CLIENT_NAME}-data"
    _PARTIAL_VOLUME=1
    log "Volume '${CLIENT_NAME}-data' ready."

    step "Step 2: Add client to host tunnel"
    add_client_to_host_tunnel "$CLIENT_NAME" "$SSH_PORT" "$CF_HOSTNAME"
    _PARTIAL_TUNNEL=1

    step "Step 3: Initialize client volume"
    setup_ssh_authorized_keys "$CLIENT_NAME" "$SSH_PUBLIC_KEY"
    setup_client_venv "$CLIENT_NAME"
    install_kernel_manager "$CLIENT_NAME"
    write_startup_script "$CLIENT_NAME"

    step "Step 4: Start container"
    start_container "$CLIENT_NAME" "$SSH_PORT"
    wait_for_container "$CLIENT_NAME"

    step "Step 5: Register client"
    register_client "$CLIENT_NAME" "$SSH_PORT" "$ADDED_AT"
    _CLIENT_ADD_OK=1
    trap - EXIT

    step "Done"
    log "Client '$CLIENT_NAME' is ready."
    log ""
    log "  Container:  $CLIENT_NAME"
    log "  Volume:     ${CLIENT_NAME}-data (persistent, never deleted by gpudev)"
    log "  SSH:        ssh -p $SSH_PORT localhost  (or via tunnel)"
    log "  Tunnel:     $CF_HOSTNAME"
    log "  Kernel:     gpudev kernel status $CLIENT_NAME"

    # Show the client config right here so the operator doesn't have to run a
    # second command — this is always the next step anyway.
    if command -v gpudev >/dev/null 2>&1; then
        echo ""
        gpudev client info "$CLIENT_NAME"
    else
        log ""
        log "Next step: give the client their SSH config — run 'gpudev client info $CLIENT_NAME'"
    fi

    # Reload the connector LAST and detached, so applying the new route can't drop
    # this session before the client is built (see reload_tunnel_connector).
    if [ "${NEED_TUNNEL_RELOAD:-0}" = "1" ]; then
        if reload_tunnel_connector "$RELOAD_TUNNEL_NAME"; then
            # Connector confirmed current, so the probe is meaningful now.
            verify_client_reachable "$CF_HOSTNAME" || true
        else
            # Reload was deferred (this shell rides the tunnel) or needs a manual
            # step. Probing now would fail for a reason that says nothing useful.
            log ""
            log "Once the connector has reloaded, confirm the client is reachable with:"
            log "  gpudev cloudflare"
        fi
    fi
}

main "$@"
