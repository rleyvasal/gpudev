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

    # Reload the tunnel service so the new ingress rule takes effect
    if command_exists systemctl && systemctl is-active gpudev-tunnel >/dev/null 2>&1; then
        sudo systemctl restart gpudev-tunnel
        log "gpudev-tunnel service restarted."
    else
        # WSL2: kill and relaunch in background
        pkill -f "cloudflared tunnel run ${tunnel_name}" 2>/dev/null || true
        sleep 1
        nohup cloudflared tunnel run "${tunnel_name}" \
            >"${HOME}/.cloudflared/tunnel.log" 2>&1 &
        log "Host tunnel restarted in background (pid $!)."
    fi
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

    cat > "$tmp_script" <<EOF
#!/bin/bash
set -e

# Ensure OS user exists (base image has no users beyond root)
useradd -M -s /bin/bash -d ${CONTAINER_HOME} ${CONTAINER_USER} 2>/dev/null || true

# Fix ownership of entire home dir (covers .local, .ssh, .venv, bin)
chown -R ${CONTAINER_USER}:${CONTAINER_USER} ${CONTAINER_HOME}

# Start SSH daemon
/usr/sbin/sshd

# Start Jupyter kernel as the gpudev user. GPUDEV_CLIENT identifies which
# client this container belongs to (for logs and 'gpudev kernel doctor').
export GPUDEV_CLIENT=${name}
su -s /bin/bash ${CONTAINER_USER} -c "GPUDEV_CLIENT=${name} ${CONTAINER_HOME}/bin/kernel-manager.sh start"

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
    docker run -d \
        --name "$name" \
        --hostname "gpudev-${name}" \
        --gpus all \
        --restart unless-stopped \
        -v "${name}-data:${CONTAINER_HOME}" \
        -p "127.0.0.1:${ssh_port}:22" \
        -e "GPUDEV_CLIENT=${name}" \
        "$BASE_IMAGE" \
        "${CONTAINER_HOME}/start.sh"

    log "Container '$name' started."
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

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
    local raw_name="${1:-}"
    local ssh_key_arg="${2:-}"
    [ -n "$raw_name" ] || fail "Usage: client-setup.sh <client_name> <ssh_public_key>"

    CLIENT_NAME="$(sanitize_name "$raw_name")"
    [ -n "$CLIENT_NAME" ] || fail "Invalid client name after sanitization."

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

    step "Step 1: Create Docker volume"
    docker volume create "${CLIENT_NAME}-data"
    log "Volume '${CLIENT_NAME}-data' ready."

    step "Step 2: Add client to host tunnel"
    add_client_to_host_tunnel "$CLIENT_NAME" "$SSH_PORT" "$CF_HOSTNAME"

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
}

main "$@"
