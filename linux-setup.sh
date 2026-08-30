#!/usr/bin/env bash
set -euo pipefail

# gpudev linux-setup.sh
# Sets up Docker + NVIDIA Container Toolkit on a WSL2 or bare Linux host,
# then builds the gpudev base image.
# Target: Ubuntu/Debian-based systems.

CONFIG_DIR="${HOME}/.config/gpudev"
HOST_CONFIG="${CONFIG_DIR}/host.json"
CLIENTS_CONFIG="${CONFIG_DIR}/clients.json"
BASE_IMAGE_NAME="gpudev-base"
BASE_IMAGE_TAG="latest"
HOST_SSH_PORT=52100
PORT_BASE=52200
GPU_INVENTORY="${CONFIG_DIR}/gpu-inventory.csv"
TORCH_INPUT="${CONFIG_DIR}/requirements-torch.in"
TORCH_LOCK="${CONFIG_DIR}/pylock.gpudev-torch.toml"
UV_RESOLVER_BIN="${CONFIG_DIR}/bin/uv"
ML_PROFILE_SCHEMA=1

# Source-of-truth for the host's gpudev scripts. fetch_companions() populates
# this dir on first run (or self-update refreshes it). install_gpudev_cli copies
# from here into ~/bin. Overridable for forks / private mirrors.
REPO_DIR="${HOME}/gpudev"
REPO_RAW_URL="${GPUDEV_REPO_RAW:-https://raw.githubusercontent.com/rleyvasal/gpudev/main}"

# Host-side scripts (NOT CRAFT.py, NOT windows-setup.ps1 — those don't belong
# on the host). gpudev self-update fetches the same set.
HOST_SCRIPTS=(linux-setup.sh gpudev client-setup.sh kernel-manager.sh)

log()  { echo "$*"; }
step() { echo ""; echo "=== $1 ==="; }
warn() { echo "Warning: $*" >&2; }
fail() { echo "Error: $*" >&2; exit 1; }

command_exists() { command -v "$1" >/dev/null 2>&1; }

append_line_once() {
    local line="$1" file="$2"
    touch "$file"
    grep -Fqx "$line" "$file" || echo "$line" >> "$file"
}

# True if systemd is PID 1 (works on bare Linux and on WSL2 with systemd
# enabled). Cheaper and more reliable than `systemctl is-system-running`,
# which fails with "Failed to connect to bus" when there is no systemd.
is_systemd_active() {
    [ "$(ps -p 1 -o comm= 2>/dev/null)" = "systemd" ]
}

# gpudev is intentionally designed around "one normal user IS the admin":
# the script writes ~/.config/gpudev/, ~/.cloudflared/, ~/.ssh/authorized_keys,
# ~/bin/gpudev, and the .bashrc dashboard hook into the current user's $HOME.
# The systemd tunnel unit runs as that user, and admin SSH from the operator's
# laptop lands as that user. Running this as root puts every per-user
# configuration into /root/, which then doesn't match the user the systemd
# units and the SSH admin path expect — the resulting setup is broken in
# subtle ways that only surface later. Better to fail loudly up front.
assert_not_root() {
    if [ "$(id -u)" -eq 0 ]; then
        fail "Don't run linux-setup.sh as root.

gpudev installs per-user configuration into the running user's \$HOME
(~/.cloudflared/, ~/.config/gpudev/, ~/.ssh/, ~/bin/gpudev, .bashrc hook).
The systemd services and the admin SSH path are wired to that user.

Run as a regular Linux user with sudo. Inside WSL, that's whoever you
created at Ubuntu's first-run prompt. On a bare Linux host, your normal
admin user."
    fi
}

# Sudo is used throughout for apt installs, systemctl, and writes to /etc.
# `sudo -v` is the canonical "this script needs sudo throughout — get me a
# session" pattern: prompts for password if the user is in standard sudoers
# (Ubuntu first-run default), refreshes cached creds if they have NOPASSWD,
# and fails clearly via sudo's own error message if the user has no sudo
# at all. Inside-Linux, no environment-specific recovery instructions.
assert_sudo() {
    command_exists sudo || fail "sudo not found. Install sudo first."
    echo ""
    echo "linux-setup.sh needs sudo for apt installs and /etc writes."
    echo "If asked, please enter your password."
    sudo -v || fail "Could not obtain sudo. Add yourself to the sudo group and re-run."
}

# ── Environment detection ─────────────────────────────────────────────────────

detect_environment() {
    if grep -qi microsoft /proc/version 2>/dev/null; then
        HOST_ENV="wsl2"
        log "Environment: WSL2"
    else
        HOST_ENV="linux"
        log "Environment: bare Linux"
    fi
}

require_debian_family() {
    [ -f /etc/debian_version ] || fail "gpudev supports Ubuntu/Debian-based systems only."
}

# ── Step 1: Configuration ─────────────────────────────────────────────────────

load_host_config() {
    [ -f "$HOST_CONFIG" ] || return 0
    CF_DOMAIN="${CF_DOMAIN:-$(python3 -c "
import json, pathlib
d = json.loads(pathlib.Path('$HOST_CONFIG').read_text())
print(d.get('cf_domain', ''))
" 2>/dev/null || true)}"
    ADMIN_SSH_KEY="${ADMIN_SSH_KEY:-$(python3 -c "
import json, pathlib
d = json.loads(pathlib.Path('$HOST_CONFIG').read_text())
print(d.get('admin_ssh_key', ''))
" 2>/dev/null || true)}"
}

validate_ssh_public_key() {
    local key="$1"
    [ -n "$key" ] || return 1
    local type
    type="$(printf '%s' "$key" | awk '{print $1}')"
    case "$type" in
        ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521)
            return 0 ;;
        *)
            return 1 ;;
    esac
}

prompt_for_missing_values() {
    [ "${NON_INTERACTIVE:-}" = "true" ] && return 0

    echo ""
    if [ -z "${CF_DOMAIN:-}" ]; then
        read -r -p "Cloudflare domain (e.g. example.com): " CF_DOMAIN
        [ -n "$CF_DOMAIN" ] || fail "Cloudflare domain is required."
    fi

    if [ -z "${ADMIN_SSH_KEY:-}" ]; then
        echo ""
        echo "Paste the admin SSH public key for host access:"
        echo "(This grants SSH access to the WSL/Linux host for management)"
        read -r ADMIN_SSH_KEY
        validate_ssh_public_key "$ADMIN_SSH_KEY" || fail "Invalid SSH public key."
    fi

}

validate_required_values() {
    [ -n "${CF_DOMAIN:-}" ]     || fail "CF_DOMAIN is required."
    [ -n "${ADMIN_SSH_KEY:-}" ] || fail "Admin SSH public key is required."
    validate_ssh_public_key "$ADMIN_SSH_KEY" || fail "Invalid admin SSH public key."
}

ensure_clients_config() {
    mkdir -p "$CONFIG_DIR"
    if [ ! -f "$CLIENTS_CONFIG" ]; then
        printf '{\n  "clients": []\n}\n' > "$CLIENTS_CONFIG"
        chmod 600 "$CLIENTS_CONFIG"
    fi
}

write_host_config() {
    mkdir -p "$CONFIG_DIR"
    chmod 700 "$CONFIG_DIR"

    CF_DOMAIN_VAL="$CF_DOMAIN" \
    LINUX_USER_VAL="$LINUX_USER" \
    HOST_ENV_VAL="$HOST_ENV" \
    PORT_BASE_VAL="$PORT_BASE" \
    HOST_SSH_PORT_VAL="$HOST_SSH_PORT" \
    HOST_CF_HOSTNAME_VAL="${LINUX_USER}.${CF_DOMAIN}" \
    ADMIN_SSH_KEY_VAL="$ADMIN_SSH_KEY" \
    python3 - "$HOST_CONFIG" <<'PY'
import json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
existing = json.loads(path.read_text()) if path.exists() else {}
existing.update({
    "cf_domain":        os.environ["CF_DOMAIN_VAL"],
    "linux_user":       os.environ["LINUX_USER_VAL"],
    "host_env":         os.environ["HOST_ENV_VAL"],
    "port_base":        int(os.environ["PORT_BASE_VAL"]),
    "host_ssh_port":    int(os.environ["HOST_SSH_PORT_VAL"]),
    "host_cf_hostname": os.environ["HOST_CF_HOSTNAME_VAL"],
    "admin_ssh_key":    os.environ["ADMIN_SSH_KEY_VAL"],
})
path.write_text(json.dumps(existing, indent=2))
PY
    chmod 600 "$HOST_CONFIG"
}

# ── Step 2: Docker ────────────────────────────────────────────────────────────

install_docker() {
    if command_exists docker; then
        log "Docker already installed: $(docker --version)"
        return 0
    fi

    log "Installing Docker Engine..."
    sudo apt-get update -q
    sudo apt-get install -qy ca-certificates curl gnupg lsb-release

    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | sudo gpg --yes --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg

    local os_id codename
    os_id="$(. /etc/os-release && echo "$ID")"
    codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
       https://download.docker.com/linux/${os_id} \
       ${codename} stable" \
      | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

    sudo apt-get update -q
    sudo apt-get install -qy docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    command_exists docker || fail "Docker install failed."
    log "Docker installed: $(docker --version)"
}

configure_docker_group() {
    if groups | grep -qw docker; then
        log "User already in docker group."
        return 0
    fi
    sudo usermod -aG docker "$LINUX_USER"
    log "Added $LINUX_USER to docker group."
    NEED_DOCKER_RELOGIN=true
}

ensure_docker_running() {
    if sudo docker info >/dev/null 2>&1; then
        log "Docker daemon is running."
        DOCKER="sudo docker"
        return 0
    fi

    log "Starting Docker daemon..."
    # systemd is guaranteed PID 1 here (require_systemd_pid1 in main()).
    sudo systemctl enable docker
    sudo systemctl start docker

    local tries=15
    while [ $tries -gt 0 ]; do
        sudo docker info >/dev/null 2>&1 && break
        sleep 1
        tries=$((tries - 1))
    done

    sudo docker info >/dev/null 2>&1 || fail "Docker daemon failed to start. Check: sudo systemctl status docker"
    DOCKER="sudo docker"
    log "Docker daemon is running."
}

restart_docker() {
    sudo systemctl restart docker
    local tries=15
    while [ $tries -gt 0 ]; do
        sudo docker info >/dev/null 2>&1 && return 0
        sleep 1
        tries=$((tries - 1))
    done
    fail "Docker daemon failed to restart."
}

# ── Step 3: NVIDIA Container Toolkit ─────────────────────────────────────────

# True if the toolkit package is installed. Prefer dpkg-query over
# `dpkg -l | grep "^ii  …"` — column spacing in dpkg -l is not stable and
# produced false MISSING health-check results with a working toolkit.
nvidia_toolkit_installed() {
    local status
    status="$(dpkg-query -W -f='${Status}' nvidia-container-toolkit 2>/dev/null || true)"
    case "$status" in
        *"install ok installed"*) return 0 ;;
    esac
    # Fallback: CLI present (some installs / partial package sets).
    command_exists nvidia-ctk
}

install_nvidia_container_toolkit() {
    if nvidia_toolkit_installed; then
        log "NVIDIA Container Toolkit already installed."
        return 0
    fi

    log "Installing NVIDIA Container Toolkit..."

    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
      | sudo gpg --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null

    sudo apt-get update -q
    sudo apt-get install -qy nvidia-container-toolkit
    sudo nvidia-ctk runtime configure --runtime=docker
    restart_docker

    log "NVIDIA Container Toolkit installed."
}

verify_gpu_passthrough() {
    if $DOCKER run --rm --gpus all nvidia/cuda:12.3.0-base-ubuntu22.04 nvidia-smi >/dev/null 2>&1; then
        log "GPU passthrough verified: nvidia-smi works inside Docker."
        return 0
    fi

    if [ "${SKIP_GPU_CHECK:-}" = "1" ]; then
        warn "GPU passthrough check failed, but SKIP_GPU_CHECK=1 — continuing."
        return 0
    fi

    if [ "$HOST_ENV" = "wsl2" ]; then
        fail "GPU passthrough check failed.
Ensure the NVIDIA *Windows* driver is installed/updated, then from PowerShell: wsl --shutdown
Re-open WSL and re-run linux-setup.sh.
To skip (not recommended): SKIP_GPU_CHECK=1 bash linux-setup.sh"
    else
        fail "GPU passthrough check failed.
Install NVIDIA drivers + nvidia-container-toolkit, load the kernel module, re-run.
To skip (not recommended): SKIP_GPU_CHECK=1 bash linux-setup.sh"
    fi
}

# ── Step 4b: Resolve the ML stack for this host ───────────────────────────────

write_base_requirements() {
    # Torch starts as a small, unpinned intent file. resolve_ml_stack() detects
    # this host's GPUs/driver and turns it into a fully pinned pylock.toml.
    # Do NOT pin numpy here — torch pulls a compatible numpy (often 2.x).
    # numba must support that numpy: 0.60.x only allows numpy<2.1 → use >=0.61.
    mkdir -p "$CONFIG_DIR"
    cat > "$TORCH_INPUT" <<'REQ'
torch
torchvision
torchaudio
REQ
    cat > "${CONFIG_DIR}/requirements-base.txt" <<'REQ'
ipykernel==6.29.5
jupyter_client==8.6.3
# numpy: left unpinned — already installed by the torch layer
numba>=0.61.0,<0.63
pandas>=2.2.0,<2.3
scipy>=1.14.0,<1.16
scikit-learn>=1.5.0,<1.7
matplotlib>=3.9.0,<3.11
plotly>=5.24.0,<6
# pillow: torch may already install a newer build; allow either
pillow>=10.0.0
tqdm>=4.66.0
# 1.1+ pulls a web-framework stack; 1.0.x has the notebook bars CRAFT needs.
fastprogress>=1.0.3,<1.1
httpx>=0.27.0
requests>=2.32.0
transformers>=4.46.0,<4.50
datasets>=3.0.0,<3.3
REQ
    log "Wrote ML requirements intent to ${TORCH_INPUT} and requirements-base.txt"
}

detect_gpu_inventory() {
    mkdir -p "$CONFIG_DIR"
    local inventory
    inventory="$($DOCKER run --rm --gpus all nvidia/cuda:12.3.0-base-ubuntu22.04 \
        nvidia-smi --query-gpu=index,name,compute_cap,driver_version \
        --format=csv,noheader,nounits 2>/dev/null || true)"

    if [ -z "$inventory" ]; then
        # compute_cap is unavailable on a few older nvidia-smi builds. Keep the
        # remaining fingerprint useful and let the final runtime test decide.
        inventory="$($DOCKER run --rm --gpus all nvidia/cuda:12.3.0-base-ubuntu22.04 \
            nvidia-smi --query-gpu=index,name,driver_version \
            --format=csv,noheader,nounits 2>/dev/null \
            | awk -F', *' '{ print $1 ", " $2 ", unknown, " $3 }' || true)"
    fi

    if [ -z "$inventory" ]; then
        if [ "${SKIP_GPU_CHECK:-}" = "1" ]; then
            if [ -s "$GPU_INVENTORY" ]; then
                warn "Could not refresh the GPU inventory; SKIP_GPU_CHECK=1 — reusing the previous inventory and lock."
                return 0
            fi
            warn "Could not inventory GPUs; SKIP_GPU_CHECK=1 — an explicit GPUDEV_TORCH_BACKEND is required."
            : > "$GPU_INVENTORY"
            return 0
        fi
        fail "Could not read the GPU inventory from Docker. Check NVIDIA passthrough and re-run."
    fi

    printf '%s\n' "$inventory" > "$GPU_INVENTORY"
    chmod 600 "$GPU_INVENTORY"
    log "Detected GPU inventory:"
    sed 's/^/  /' "$GPU_INVENTORY"
}

gpu_fingerprint() {
    if [ -s "$GPU_INVENTORY" ]; then
        sha256sum "$GPU_INVENTORY" | awk '{print $1}'
    else
        printf 'unavailable'
    fi
}

install_uv_resolver() {
    if [ -x "$UV_RESOLVER_BIN" ] && "$UV_RESOLVER_BIN" pip compile --help 2>/dev/null | grep -q -- '--torch-backend'; then
        return 0
    fi

    log "Installing the uv package resolver..."
    mkdir -p "$(dirname "$UV_RESOLVER_BIN")"
    curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR="$(dirname "$UV_RESOLVER_BIN")" UV_NO_MODIFY_PATH=1 sh
    [ -x "$UV_RESOLVER_BIN" ] || fail "uv resolver installation failed."
    "$UV_RESOLVER_BIN" pip compile --help 2>/dev/null | grep -q -- '--torch-backend' \
        || fail "Installed uv is too old to select a PyTorch backend automatically."
}

select_torch_backend() {
    local requested="$1"
    if [ "$requested" != "auto" ]; then
        printf '%s' "$requested"
        return 0
    fi

    # CUDA 13 no longer supports offline compilation for pre-Turing GPUs. uv's
    # auto mode primarily follows the driver, so keep those older cards on the
    # latest CUDA 12 backend instead of selecting CUDA 13 merely
    # because the host has a new driver. Runtime verification remains final.
    if [ -s "$GPU_INVENTORY" ] && python3 - "$GPU_INVENTORY" <<'PY'
import csv, sys
rows = list(csv.reader(open(sys.argv[1], newline="")))
caps = []
for row in rows:
    try:
        caps.append(float(row[2].strip()))
    except (IndexError, ValueError):
        pass
raise SystemExit(0 if caps and min(caps) < 7.5 else 1)
PY
    then
        warn "Pre-Turing GPU detected; selecting PyTorch's CUDA 12.6 legacy backend instead of CUDA 13."
        printf 'cu126'
    else
        printf 'auto'
    fi
}

ml_lock_is_current() {
    local fingerprint="$1" requested="$2"
    [ -s "$TORCH_LOCK" ] || return 1
    [ "${GPUDEV_ML_REFRESH:-0}" != "1" ] || return 1
    python3 - "$HOST_CONFIG" "$fingerprint" "$requested" "$ML_PROFILE_SCHEMA" "$TORCH_LOCK" <<'PY'
import hashlib, json, pathlib, sys
path = pathlib.Path(sys.argv[1])
if not path.exists():
    raise SystemExit(1)
profile = json.loads(path.read_text()).get("ml_profile", {})
lock_hash = hashlib.sha256(pathlib.Path(sys.argv[5]).read_bytes()).hexdigest()
ok = (profile.get("gpu_fingerprint") == sys.argv[2]
      and profile.get("requested_backend") == sys.argv[3]
      and profile.get("schema") == int(sys.argv[4])
      and profile.get("lock_sha256") == lock_hash)
raise SystemExit(0 if ok else 1)
PY
}

persist_ml_profile() {
    local fingerprint="$1" requested="$2" resolver_backend="$3"
    local resolver_version lock_hash
    resolver_version="$($UV_RESOLVER_BIN --version | awk '{print $2}')"
    lock_hash="$(sha256sum "$TORCH_LOCK" | awk '{print $1}')"

    GPU_FINGERPRINT_VAL="$fingerprint" \
    REQUESTED_BACKEND_VAL="$requested" \
    RESOLVER_BACKEND_VAL="$resolver_backend" \
    RESOLVER_VERSION_VAL="$resolver_version" \
    LOCK_HASH_VAL="$lock_hash" \
    ML_PROFILE_SCHEMA_VAL="$ML_PROFILE_SCHEMA" \
    python3 - "$HOST_CONFIG" "$GPU_INVENTORY" "$TORCH_LOCK" <<'PY'
import csv, datetime, json, os, pathlib, re, sys

config_path, inventory_path, lock_path = map(pathlib.Path, sys.argv[1:])
config = json.loads(config_path.read_text()) if config_path.exists() else {}

gpus = []
if inventory_path.exists():
    for row in csv.reader(inventory_path.read_text().splitlines()):
        if len(row) >= 4:
            gpus.append({
                "index": row[0].strip(),
                "name": row[1].strip(),
                "compute_capability": row[2].strip(),
                "driver_version": row[3].strip(),
            })

lock_text = lock_path.read_text()
versions = {}
for block in re.split(r"(?m)^\[\[packages\]\]\s*$", lock_text)[1:]:
    name = re.search(r'(?m)^name\s*=\s*"([^"]+)"', block)
    version = re.search(r'(?m)^version\s*=\s*"([^"]+)"', block)
    if name and version:
        versions[name.group(1).lower().replace("_", "-")] = version.group(1)
backends = sorted(set(re.findall(r"download\.pytorch\.org/whl/(cu\d+|cpu)", lock_text)))
resolved = ",".join(backends) or os.environ["RESOLVER_BACKEND_VAL"]

config["ml_profile"] = {
    "schema": int(os.environ["ML_PROFILE_SCHEMA_VAL"]),
    "requested_backend": os.environ["REQUESTED_BACKEND_VAL"],
    "resolved_backend": resolved,
    "gpu_fingerprint": os.environ["GPU_FINGERPRINT_VAL"],
    "gpus": gpus,
    "torch": versions.get("torch", ""),
    "torchvision": versions.get("torchvision", ""),
    "torchaudio": versions.get("torchaudio", ""),
    "lock_file": str(lock_path),
    "lock_sha256": os.environ["LOCK_HASH_VAL"],
    "resolver": f"uv {os.environ['RESOLVER_VERSION_VAL']}",
    "resolved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
config_path.write_text(json.dumps(config, indent=2) + "\n")
PY
    chmod 600 "$HOST_CONFIG" "$TORCH_LOCK"
}

print_ml_profile() {
    python3 - "$HOST_CONFIG" <<'PY'
import json, sys
p = json.load(open(sys.argv[1])).get("ml_profile", {})
versions = ", ".join(
    f"{name} {p.get(name)}" for name in ("torch", "torchvision", "torchaudio") if p.get(name)
)
print(f"  Backend: {p.get('resolved_backend', 'unknown')} (requested: {p.get('requested_backend', 'auto')})")
if versions:
    print(f"  Packages: {versions}")
print(f"  Detected GPUs: {len(p.get('gpus', []))}")
PY
}

mark_ml_profile_validated() {
    python3 - "$HOST_CONFIG" <<'PY'
import datetime, json, pathlib, sys
path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
data.setdefault("ml_profile", {})["validated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
data["ml_profile"]["validated_gpu_count"] = len(data["ml_profile"].get("gpus", []))
path.write_text(json.dumps(data, indent=2) + "\n")
PY
    chmod 600 "$HOST_CONFIG"
}

resolve_ml_stack() {
    write_base_requirements
    detect_gpu_inventory

    local requested resolver_backend fingerprint
    requested="${GPUDEV_TORCH_BACKEND:-auto}"
    case "$requested" in
        auto|cpu|cu[0-9][0-9][0-9]) ;;
        *) fail "Invalid GPUDEV_TORCH_BACKEND='$requested'. Use auto, cpu, or a uv CUDA backend such as cu128." ;;
    esac

    fingerprint="$(gpu_fingerprint)"
    if ml_lock_is_current "$fingerprint" "$requested"; then
        log "GPU and driver are unchanged; reusing the locked ML stack."
        print_ml_profile
        return 0
    fi

    resolver_backend="$(select_torch_backend "$requested")"
    if [ "$fingerprint" = "unavailable" ] && [ "$resolver_backend" = "auto" ]; then
        fail "Automatic PyTorch selection needs a visible GPU.
Fix GPU passthrough, or rerun with an explicit override, for example:
  GPUDEV_TORCH_BACKEND=cu128 SKIP_GPU_CHECK=1 bash linux-setup.sh"
    fi

    install_uv_resolver
    log "Resolving PyTorch for backend '${resolver_backend}' (requested: '${requested}')..."
    local gpu_args=()
    [ "$fingerprint" = "unavailable" ] || gpu_args=(--gpus all)
    if ! $DOCKER run --rm "${gpu_args[@]}" \
        --user "$(id -u):$(id -g)" \
        -e HOME=/tmp \
        -e UV_HTTP_TIMEOUT=300 \
        -e UV_HTTP_RETRIES=5 \
        -v "$UV_RESOLVER_BIN:/usr/local/bin/uv:ro" \
        -v "$CONFIG_DIR:/work" \
        -w /work \
        python:3.12-slim \
        uv pip compile requirements-torch.in \
            --output-file pylock.gpudev-torch.toml \
            --format pylock.toml \
            --python-version 3.12 \
            --torch-backend "$resolver_backend" \
            --upgrade; then
        fail "Could not resolve a compatible PyTorch stack.
Check network access and the detected GPUs above. Advanced override example:
  GPUDEV_TORCH_BACKEND=cu128 GPUDEV_ML_REFRESH=1 bash linux-setup.sh"
    fi

    [ -s "$TORCH_LOCK" ] || fail "uv completed without writing ${TORCH_LOCK}."
    persist_ml_profile "$fingerprint" "$requested" "$resolver_backend"
    log "Locked the resolved ML stack:"
    print_ml_profile
}

# ── Step 5: Build base image ──────────────────────────────────────────────────

write_dockerfile() {
    local dockerfile="${CONFIG_DIR}/Dockerfile.base"
    cat > "$dockerfile" <<'DOCKERFILE'
# syntax=docker/dockerfile:1
FROM python:3.12-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-server \
        curl \
        ca-certificates \
        git \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# uv — installed to /usr/local/bin (already on the default PATH) so it's reachable
# from BOTH the Dockerfile CMD and interactive `gpudev` SSH login shells. The
# installer's default dir is /root/.local/bin, which only lands on PATH via the
# Dockerfile ENV below — and sshd login sessions DON'T inherit that ENV.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# CUDA wheels are large and the NVIDIA package index can respond slowly.
# Keep completed downloads across failed/retried builds and tolerate slow reads.
ENV UV_HTTP_TIMEOUT=300 \
    UV_HTTP_RETRIES=5 \
    UV_LINK_MODE=copy

# SSH: pubkey auth only, no passwords
RUN mkdir -p /run/sshd \
    && sed -i 's/#PubkeyAuthentication yes/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
    && sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config \
    && sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config

# Base ML venv at /opt/venv — built once into the image, available read-only to all containers.
# PyTorch bundles its own CUDA runtime so no CUDA base image is needed.
# The host-specific PyTorch backend and exact versions were resolved before the
# build. pylock.toml records both package versions and their wheel sources.
# Per-client venvs are created on their data volumes by client-setup.sh and persist indefinitely.
COPY pylock.gpudev-torch.toml requirements-base.txt /tmp/gpudev-req/
RUN uv venv /opt/venv --python 3.12 --seed
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python \
        -r /tmp/gpudev-req/pylock.gpudev-torch.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python \
        -r /tmp/gpudev-req/requirements-base.txt

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Point interactive `gpudev` SSH login shells at the per-client venv (~/.venv =
# /home/gpudev/.venv), NOT the system Python. sshd login sessions don't inherit
# the ENV above, so we set it via /etc/profile.d, which login shells source.
# Result: `python`/`pip` resolve to ~/.venv (it has pip via --seed in
# client-setup.sh) and `uv pip install` targets it too — the SAME interpreter the
# kernel runs (kernel-manager.sh: ${VENV}/bin/python). The venv is created later
# by client-setup.sh; a missing dir here is harmless (PATH entry is just skipped).
RUN printf '%s\n' \
        'export VIRTUAL_ENV=/home/gpudev/.venv' \
        'export UV_PROJECT_ENVIRONMENT=/home/gpudev/.venv' \
        'export PATH="/home/gpudev/.venv/bin:$PATH"' \
        > /etc/profile.d/10-gpudev-venv.sh

# Mojo via pixi (Modular's package manager). Seed project at /opt/mojo-proj (image).
# At container start, client start.sh copies the seed to /home/gpudev/.mojo-proj on
# the data volume if missing — so %mojo_add / pixi packages survive client rebuild.
RUN curl -fsSL https://pixi.sh/install.sh | PIXI_HOME=/opt/pixi PIXI_NO_PATH_UPDATE=1 bash
ENV PATH="/opt/pixi/bin:${PATH}"
# Pin STABLE Mojo: `modular<26.3` resolves to 25.4.x (Mojo 25.4) on Python 3.12,
# NOT the 1.0.0b1 beta (modular 26.3, Python 3.14).
RUN pixi init /opt/mojo-proj \
        -c https://conda.modular.com/max \
        -c https://repo.prefix.dev/modular-community \
        -c conda-forge \
    && pixi add --manifest-path /opt/mojo-proj/pixi.toml "modular<26.3"
RUN pixi add --manifest-path /opt/mojo-proj/pixi.toml \
        numpy pandas matplotlib scipy \
    && chmod -R a+rX /opt/mojo-proj
# Runtime default is the volume path; seed remains at /opt/mojo-proj.
ENV MOJO_PROJ=/home/gpudev/.mojo-proj
RUN printf '%s\n' \
        'export MOJO_PROJ="${MOJO_PROJ:-/home/gpudev/.mojo-proj}"' \
        'export PATH="/opt/pixi/bin:$PATH"' \
        > /etc/profile.d/20-gpudev-mojo.sh

EXPOSE 22

CMD ["/usr/sbin/sshd", "-D"]
DOCKERFILE

    echo "$dockerfile"
}

build_base_image() {
    # write_dockerfile prints only the Dockerfile path on stdout (logs go to stderr).
    local dockerfile
    dockerfile="$(write_dockerfile | tail -n1)"
    [ -f "$dockerfile" ] || fail "Dockerfile not written (got path: ${dockerfile:-empty})"
    [ -s "$TORCH_LOCK" ] \
        && [ -f "${CONFIG_DIR}/requirements-base.txt" ] \
        || fail "Resolved ML lock or base requirements missing in ${CONFIG_DIR} — resolve_ml_stack failed."

    $DOCKER build \
        --network=host \
        -f "$dockerfile" \
        -t "${BASE_IMAGE_NAME}:${BASE_IMAGE_TAG}" \
        "$CONFIG_DIR"

    log "Base image built: ${BASE_IMAGE_NAME}:${BASE_IMAGE_TAG}"
}

# Confirm the base image's torch build can execute a real GPU kernel on every
# installed card. Merely checking is_available() is insufficient: a wheel can
# see a device while lacking a kernel image for that device's architecture.
check_all_torch_gpus() {
    [ -s "$GPU_INVENTORY" ] || return 1
    local indices index
    indices="$(cut -d',' -f1 "$GPU_INVENTORY" | sed 's/[[:space:]]//g')"
    [ -n "$indices" ] || return 1

    while IFS= read -r index; do
        [ -n "$index" ] || continue
        log "  Testing physical GPU ${index}..."
        $DOCKER run --rm --gpus "device=${index}" "${BASE_IMAGE_NAME}:${BASE_IMAGE_TAG}" \
            python -c '
import torch
assert torch.cuda.is_available(), (torch.__version__, torch.version.cuda)
assert torch.cuda.device_count() == 1, torch.cuda.device_count()
device = torch.cuda.current_device()
x = torch.arange(4, device=device, dtype=torch.float32)
result = (x * x).sum()
torch.cuda.synchronize(device)
assert result.item() == 14.0, result
print("torch", torch.__version__, "cuda", torch.version.cuda)
print("gpu", torch.cuda.get_device_name(device), "capability", torch.cuda.get_device_capability(device))
print("architectures", " ".join(torch.cuda.get_arch_list()))
' || return 1
    done <<< "$indices"
}

verify_torch_cuda() {
    if [ "${SKIP_GPU_CHECK:-}" = "1" ]; then
        warn "SKIP_GPU_CHECK=1 — skipping torch.cuda check"
        return 0
    fi

    if ! $DOCKER image inspect "${BASE_IMAGE_NAME}:${BASE_IMAGE_TAG}" >/dev/null 2>&1; then
        fail "Base image missing; cannot verify torch.cuda."
    fi

    log "Verifying real torch CUDA operations on every installed GPU..."
    if check_all_torch_gpus; then
        mark_ml_profile_validated
        log "torch CUDA kernel execution: OK on every GPU"
        return 0
    fi

fail "torch cannot execute a GPU kernel inside the base image.
Passthrough may work while the selected wheel lacks one of the installed GPU architectures.
The detected inventory and locked ML profile are in ${CONFIG_DIR}.
Fix: update the NVIDIA driver and refresh automatic selection:
  GPUDEV_ML_REFRESH=1 bash linux-setup.sh
Advanced override example:
  GPUDEV_TORCH_BACKEND=cu128 GPUDEV_ML_REFRESH=1 bash linux-setup.sh
To skip (not recommended): SKIP_GPU_CHECK=1 bash linux-setup.sh"
}

# ── Step 5: Install cloudflared on host ──────────────────────────────────────

install_cloudflared_host() {
    if command_exists cloudflared; then
        log "cloudflared already installed: $(cloudflared --version)"
        return 0
    fi

    log "Installing cloudflared on host..."
    local tmp_deb
    tmp_deb="$(mktemp /tmp/cloudflared.XXXXXX.deb)"
    curl -fsSL \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb" \
        -o "$tmp_deb"
    sudo dpkg -i "$tmp_deb" || sudo apt-get install -f -y
    rm -f "$tmp_deb"

    command_exists cloudflared || fail "cloudflared install failed."
    log "cloudflared installed: $(cloudflared --version)"
}

# ── Step 6: Host SSH setup ────────────────────────────────────────────────────

# Ensure /etc/wsl.conf has [boot] systemd=true so sshd / docker / cloudflared
# can be managed by systemctl and survive reboots. Idempotent and section-aware:
# preserves [user]/default= and any other existing sections instead of clobbering
# the file. Takes effect after `wsl --shutdown` is run from Windows.
ensure_wsl_systemd_enabled() {
    local conf="/etc/wsl.conf"
    sudo touch "$conf"
    sudo python3 - "$conf" <<'PY'
import sys, pathlib, re
p = pathlib.Path(sys.argv[1])
text = p.read_text() if p.stat().st_size > 0 else ""
lines = text.splitlines()

boot_idx = None
section_end = len(lines)
for i, line in enumerate(lines):
    s = line.strip()
    if s == '[boot]':
        boot_idx = i
    elif boot_idx is not None and s.startswith('[') and s.endswith(']'):
        section_end = i
        break

changed = False
if boot_idx is None:
    if lines and lines[-1].strip() != '':
        lines.append('')
    lines.append('[boot]')
    lines.append('systemd=true')
    changed = True
else:
    found = False
    for i in range(boot_idx + 1, section_end):
        if re.match(r'\s*systemd\s*=', lines[i]):
            found = True
            if not re.match(r'\s*systemd\s*=\s*true\s*$', lines[i]):
                lines[i] = 'systemd=true'
                changed = True
            break
    if not found:
        lines.insert(boot_idx + 1, 'systemd=true')
        changed = True

if changed:
    p.write_text('\n'.join(lines) + '\n')
PY
    log "  /etc/wsl.conf: [boot] systemd=true ensured."
}

# Guarantee systemd is PID 1 before any install step touches the system.
# gpudev's service model (Restart=always, WantedBy=multi-user.target) depends
# on systemd-as-init. Without this gate, downstream functions would need
# fallback branches for the no-systemd case — dead complexity we don't want.
#
#   WSL2 + no systemd  → write /etc/wsl.conf, invoke wsl.exe --shutdown via
#                        interop, exit cleanly. User re-opens WSL and re-runs
#                        the script; the second invocation lands with systemd
#                        as PID 1 and proceeds with the full install.
#   bare Linux + no systemd → fail. No fallback path; gpudev requires systemd.
#   Either + systemd PID 1 → no-op, return.
require_systemd_pid1() {
    is_systemd_active && return 0

    if [ "$HOST_ENV" = "wsl2" ]; then
        step "Enable systemd in WSL (one-time)"
        log "systemd is not yet PID 1 in this WSL session — enabling it now."
        ensure_wsl_systemd_enabled
        echo ""
        echo "═══════════════════════════════════════════════════════════════════"
        echo " systemd has been enabled in /etc/wsl.conf."
        echo " The WSL VM must restart for systemd to become PID 1."
        echo "═══════════════════════════════════════════════════════════════════"
        echo ""
        echo " Restarting the WSL VM now via interop (this terminates your"
        echo " current WSL session). Re-open your WSL terminal and run"
        echo " linux-setup.sh again — the second invocation will do the full"
        echo " install with systemd available."
        echo ""

        # Invoke wsl.exe via interop from /mnt/c/Windows/System32/. This is a
        # WSL platform action (restart the VM), not Linux-configuring-Windows.
        # Best-effort: if interop is unavailable, fall back to a clear message.
        local wsl_exe="/mnt/c/Windows/System32/wsl.exe"
        if [ -x "$wsl_exe" ]; then
            log "Calling $wsl_exe --shutdown in 3 seconds..."
            sleep 3
            "$wsl_exe" --shutdown 2>/dev/null || true
            # If we somehow reach this line (interop succeeded but didn't kill
            # us), exit cleanly so no install steps run on the stale session.
            exit 0
        fi

        echo " WSL interop (wsl.exe) not reachable. Run this from any shell"
        echo " to restart the WSL VM, then re-open WSL:"
        echo ""
        echo "   wsl --shutdown"
        echo ""
        exit 0
    fi

    fail "systemd is required but is not PID 1 on this host.

gpudev's service model (Restart=always, WantedBy=multi-user.target) depends
on systemd-as-init. Enable systemd and re-run linux-setup.sh."
}

setup_host_ssh() {
    log "Configuring host sshd on port $HOST_SSH_PORT..."

    sudo apt-get install -qy openssh-server 2>/dev/null || true

    set_sshd_option() {
        local key="$1" value="$2"
        local config="/etc/ssh/sshd_config"
        if sudo grep -Eq "^[#[:space:]]*${key}[[:space:]]+" "$config"; then
            sudo sed -i -E "s|^[#[:space:]]*${key}[[:space:]]+.*|${key} ${value}|" "$config"
        else
            echo "${key} ${value}" | sudo tee -a "$config" >/dev/null
        fi
    }

    set_sshd_option "Port"                  "$HOST_SSH_PORT"
    set_sshd_option "PubkeyAuthentication"  "yes"
    set_sshd_option "PasswordAuthentication" "no"

    sudo mkdir -p /run/sshd

    # Add admin key to authorized_keys
    mkdir -p "${HOME}/.ssh"
    touch "${HOME}/.ssh/authorized_keys"
    grep -qxF "$ADMIN_SSH_KEY" "${HOME}/.ssh/authorized_keys" \
        || echo "$ADMIN_SSH_KEY" >> "${HOME}/.ssh/authorized_keys"
    chmod 700 "${HOME}/.ssh"
    chmod 600 "${HOME}/.ssh/authorized_keys"

    sudo sshd -t || fail "sshd config test failed after gpudev changes."

    # systemd is guaranteed PID 1 here (require_systemd_pid1 in main()).
    sudo systemctl enable ssh
    sudo systemctl restart ssh
    log "Host sshd is persistent via systemd (auto-starts on boot)."

    log "Host sshd configured on port $HOST_SSH_PORT."
}

# ── Step 7: Host Cloudflare tunnel ────────────────────────────────────────────

setup_host_cf_tunnel() {
    local tunnel_name="${LINUX_USER}"
    local cf_hostname="${LINUX_USER}.${CF_DOMAIN}"
    local config_yml="${HOME}/.cloudflared/config.yml"
    local unit_file="/etc/systemd/system/gpudev-tunnel.service"
    local config_before="missing" unit_before="missing"

    [ -f "$config_yml" ] && config_before="$(sha256sum "$config_yml" | awk '{print $1}')"
    [ -f "$unit_file" ] && unit_before="$(sha256sum "$unit_file" | awk '{print $1}')"

    # Authenticate with Cloudflare if cert.pem is missing.
    # This prints a browser URL — the operator must visit it and authorise the
    # domain before the script can continue.
    local cert_pem="${HOME}/.cloudflared/cert.pem"
    if [ ! -f "$cert_pem" ]; then
        log "No Cloudflare credentials found. Launching browser login..."
        log "→ A URL will appear below. Open it in your browser and authorise the domain."
        cloudflared tunnel login
        [ -f "$cert_pem" ] || fail "cloudflared login did not produce cert.pem — authorisation may have been skipped."
        log "Cloudflare login successful (cert.pem saved)."
    else
        log "Cloudflare credentials already present — skipping login."
    fi

    if ! cloudflared tunnel list 2>/dev/null | awk '{print $2}' | grep -qx "$tunnel_name"; then
        cloudflared tunnel create "$tunnel_name"
        log "Cloudflare tunnel '$tunnel_name' created."
    else
        log "Cloudflare tunnel '$tunnel_name' already exists."
    fi

    local tunnel_id
    tunnel_id="$(cloudflared tunnel list | awk -v t="$tunnel_name" '$2 == t {print $1; exit}')"
    [ -n "$tunnel_id" ] || fail "Could not determine tunnel ID for '$tunnel_name'."

    # A tunnel can exist on the Cloudflare account while its local credentials
    # JSON is gone — e.g. the WSL distro was reinstalled, or the home dir wiped.
    # `cloudflared tunnel run` then crash-loops on the missing credentials-file
    # (systemd shows "active (running)" because Restart=always keeps respawning
    # it) and the edge returns HTTP 530. Detect the missing creds file and
    # recreate the tunnel from scratch so a fresh UUID + JSON is produced.
    if [ ! -f "${HOME}/.cloudflared/${tunnel_id}.json" ]; then
        warn "Tunnel '$tunnel_name' exists but its credentials file is missing — recreating."
        cloudflared tunnel delete -f "$tunnel_name" 2>/dev/null || true
        cloudflared tunnel create "$tunnel_name"
        tunnel_id="$(cloudflared tunnel list | awk -v t="$tunnel_name" '$2 == t {print $1; exit}')"
        [ -n "$tunnel_id" ] || fail "Could not determine tunnel ID for '$tunnel_name' after recreate."
    fi

    # Point the hostname's CNAME at THIS tunnel. --overwrite-dns is essential:
    # without it `route dns` refuses when a CNAME for $cf_hostname already exists
    # (from a prior install, or after the tunnel was renamed/recreated) and the
    # hostname keeps resolving to the OLD, now-dead tunnel UUID. The connector is
    # healthy but the edge still answers HTTP 530 for that one hostname — exactly
    # the failure we hit after renaming the tunnel to the Linux user. Fail loud
    # rather than swallowing the error and shipping a silently-broken route.
    cloudflared tunnel route dns --overwrite-dns "$tunnel_name" "$cf_hostname" \
        || fail "Could not route ${cf_hostname} → tunnel ${tunnel_name}. Run: cloudflared tunnel route dns --overwrite-dns ${tunnel_name} ${cf_hostname}"

    mkdir -p "${HOME}/.cloudflared"
    # Rewrite config.yml but PRESERVE existing client ingress rules. client-setup.sh
    # injects each client (e.g. solveit → ssh://localhost:52200) before the catch-all;
    # a plain `cat >` here wipes them, silently breaking every client tunnel on every
    # linux-setup.sh re-run. Merge instead: host rule first, existing client rules
    # kept, catch-all last.
    python3 - "$config_yml" "$tunnel_id" "$cf_hostname" "$HOST_SSH_PORT" "$HOME" <<'PY'
import sys, re, pathlib
config_path, tunnel_id, host_host, host_port, home = sys.argv[1:6]
p = pathlib.Path(config_path)
existing = re.findall(r"-\s*hostname:\s*(\S+)\s*\n\s*service:\s*(\S+)",
                      p.read_text()) if p.exists() else []
rules = [(host_host, f"ssh://localhost:{host_port}")]           # host rule first
for h, s in existing:                                           # then client rules
    if h != host_host and (h, s) not in rules:
        rules.append((h, s))
lines = [f"tunnel: {tunnel_id}",
         f"credentials-file: {home}/.cloudflared/{tunnel_id}.json", "", "ingress:"]
for h, s in rules:
    lines += [f"  - hostname: {h}", f"    service: {s}"]
lines.append("  - service: http_status:404")                   # catch-all last
p.write_text("\n".join(lines) + "\n")
print(f"config.yml: {len(rules)} hostname rule(s) kept (host + {len(rules)-1} client)")
PY
    chmod 600 "$config_yml"

    # systemd is guaranteed PID 1 here (require_systemd_pid1 in main()).
    sudo tee "$unit_file" >/dev/null <<EOF
[Unit]
Description=gpudev host Cloudflare tunnel
After=network.target

[Service]
User=${LINUX_USER}
# Run by UUID, not name: if the account ever has two tunnels with the same name
# (a real hazard we hit), \`tunnel run <name>\` is ambiguous and can come back on
# the wrong tunnel after a reboot, while DNS points at the other one → HTTP 530.
# The UUID is pinned to the credentials-file we wrote, so this is unambiguous.
ExecStart=$(command -v cloudflared) tunnel run ${tunnel_id}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    local config_after unit_after config_changed=0 unit_changed=0
    config_after="$(sha256sum "$config_yml" | awk '{print $1}')"
    unit_after="$(sha256sum "$unit_file" | awk '{print $1}')"
    [ "$config_before" = "$config_after" ] || config_changed=1
    [ "$unit_before" = "$unit_after" ] || unit_changed=1

    if [ "$unit_changed" -eq 1 ]; then
        sudo systemctl daemon-reload
    fi
    sudo systemctl enable gpudev-tunnel

    if systemctl is-active gpudev-tunnel >/dev/null 2>&1; then
        if [ "$config_changed" -eq 1 ] || [ "$unit_changed" -eq 1 ]; then
            # This SSH session may use the connector itself. Restart only after
            # every setup step and the health report have completed.
            NEED_HOST_TUNNEL_RESTART=1
            log "Tunnel configuration changed; connector restart deferred until setup completes."
        else
            log "Tunnel configuration unchanged; keeping the active connector (SSH stays connected)."
        fi
    else
        # Starting a missing systemd connector does not disturb an existing
        # setup-run SSH session. Do not pkill a possible legacy connector here.
        sudo systemctl start gpudev-tunnel
    fi
    log "Host tunnel is persistent via systemd (auto-starts on boot)."

    log "Host tunnel:  $cf_hostname → localhost:${HOST_SSH_PORT}"

    # Extract API token from tunnel credentials and save to host.json
    local creds_file="${HOME}/.cloudflared/${tunnel_id}.json"
    if [ -f "$creds_file" ]; then
        local api_token
        api_token="$(python3 -c "
import json, pathlib
d = json.loads(pathlib.Path('${creds_file}').read_text())
print(d.get('APIToken', d.get('api_token', '')))
" 2>/dev/null || true)"
        if [ -n "$api_token" ]; then
            python3 -c "
import json, pathlib
p = pathlib.Path('${HOST_CONFIG}')
d = json.loads(p.read_text())
d['cf_api_token'] = '${api_token}'
p.write_text(json.dumps(d, indent=2))
"
            chmod 600 "$HOST_CONFIG"
            log "Cloudflare API token saved to host.json."
        else
            warn "Could not extract a DNS-capable API token from tunnel credentials."
            warn "  client remove will not auto-delete CNAMEs until you store one:"
            warn "    gpudev cloudflare token-set"
            warn "  Create token: dash.cloudflare.com → API Tokens → Edit zone DNS"
            warn "  (zone = your CF_DOMAIN only)."
        fi
    fi
}

schedule_host_tunnel_restart() {
    local restart_unit="gpudev-tunnel-restart-$(date +%s)"
    echo ""
    log "Setup is complete. Applying the changed Cloudflare tunnel configuration"
    log "in five seconds. Tunnel SSH sessions will briefly disconnect; reconnect normally."
    sudo systemd-run --quiet \
        --unit="$restart_unit" \
        --on-active=5s \
        /bin/systemctl restart gpudev-tunnel
}

# ── Step 8: Install gpudev CLI ────────────────────────────────────────────────

# Download the scripts the host needs (and only those) into REPO_DIR. Phase B's
# bootstrap is just `curl … linux-setup.sh | bash` — this fills in the rest, so
# the host never has to clone a full repo. CRAFT.py and windows-setup.ps1 are
# deliberately excluded.
#
# Backward compatible: if REPO_DIR is already a git checkout (operator did
# `git clone`), or all companions are already present, this is a no-op. Use
# `gpudev self-update` for an explicit refresh.
fetch_companions() {
    mkdir -p "$REPO_DIR"

    # Skip if it's a git checkout — `git pull` is the operator's update path.
    if [ -d "${REPO_DIR}/.git" ]; then
        log "REPO_DIR is a git checkout — leaving it alone. Use 'git pull' to update."
        return 0
    fi

    # Figure out which files are missing (skip linux-setup.sh check if we're
    # already running from REPO_DIR, since BASH_SOURCE is then us).
    local me self_in_repo=0
    me="$(realpath "${BASH_SOURCE[0]}" 2>/dev/null || true)"
    [ "$me" = "${REPO_DIR}/linux-setup.sh" ] && self_in_repo=1

    local missing=() f
    for f in "${HOST_SCRIPTS[@]}"; do
        if [ "$f" = "linux-setup.sh" ] && [ "$self_in_repo" = "1" ]; then continue; fi
        [ -f "${REPO_DIR}/${f}" ] || missing+=("$f")
    done

    if [ ${#missing[@]} -eq 0 ]; then
        log "Host scripts already present in ${REPO_DIR}"
        return 0
    fi

    log "Fetching host scripts into ${REPO_DIR} from ${REPO_RAW_URL}..."
    command_exists curl || fail "curl is required to fetch host scripts."
    local tmp
    for f in "${missing[@]}"; do
        tmp="$(mktemp)"
        if curl -fsSL "${REPO_RAW_URL}/${f}" -o "$tmp"; then
            chmod +x "$tmp"
            mv -f "$tmp" "${REPO_DIR}/${f}"
            log "  Downloaded ${f}"
        else
            rm -f "$tmp"
            fail "Failed to download ${f} from ${REPO_RAW_URL}/${f}"
        fi
    done
}

install_gpudev_cli() {
    mkdir -p "${HOME}/bin"
    append_line_once 'export PATH="$HOME/.local/bin:$HOME/bin:$PATH"' "${HOME}/.bashrc"
    # Auto-show the dashboard on interactive SSH login. The $PS1 guard keeps
    # scripted SSH (`ssh host cmd`) silent so it doesn't break automation.
    append_line_once 'if [ -n "$PS1" ] && command -v gpudev >/dev/null 2>&1; then gpudev status 2>/dev/null; fi' "${HOME}/.bashrc"
    export PATH="$HOME/.local/bin:$HOME/bin:$PATH"

    # Source scripts from REPO_DIR (populated by fetch_companions or a git
    # checkout) — not from BASH_SOURCE-relative, since with `bash <(curl …)`
    # BASH_SOURCE is /dev/fd/* and has no companions next to it.
    for script in gpudev client-setup.sh kernel-manager.sh; do
        if [ -f "${REPO_DIR}/${script}" ]; then
            cp "${REPO_DIR}/${script}" "${HOME}/bin/${script}"
            chmod +x "${HOME}/bin/${script}"
            log "${script} installed at ${HOME}/bin/${script}"
        else
            warn "${script} not found at ${REPO_DIR}/${script} — install manually."
        fi
    done
}

# ── Step 10: Power management ─────────────────────────────────────────────────

# Make the host stay awake on its own but reboot/suspend on demand (used by
# `gpudev power`, e.g. from CRAFT's %reboot / %sleep). On WSL2 the power action
# targets the *Windows* host via interop — gpudev handles that itself, so there
# is nothing to configure on the Linux side beyond a sanity check.
configure_power_management() {
    if [ "$HOST_ENV" = "wsl2" ]; then
        if [ -x /mnt/c/Windows/System32/shutdown.exe ] || command_exists shutdown.exe; then
            log "WSL2 interop OK — 'gpudev power' will drive the Windows host."
        else
            warn "Windows interop not detected (shutdown.exe unreachable)."
            warn "'gpudev power' needs WSL interop enabled (default) and /mnt/c mounted."
        fi
        return 0
    fi

    log "Configuring power management for on-demand reboot/suspend..."

    # 1. Don't let the host suspend itself on idle or lid events. A headless GPU
    #    host should only ever sleep when explicitly told to.
    sudo mkdir -p /etc/systemd/logind.conf.d
    sudo tee /etc/systemd/logind.conf.d/gpudev.conf >/dev/null <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
EOF
    log "  logind: lid/idle auto-suspend disabled (applies after reboot)."

    # 2. Let the admin user trigger reboot/suspend over SSH without a password.
    #    An SSH session is not a polkit "active" session, so `systemctl suspend`
    #    would otherwise prompt for authentication and fail non-interactively.
    #    Scope is limited to exactly these two commands; both /usr/bin and /bin
    #    paths are listed to cover usr-merged and non-merged layouts.
    local sudoers_file="/etc/sudoers.d/gpudev-power"
    sudo tee "$sudoers_file" >/dev/null <<EOF
# gpudev: allow ${LINUX_USER} to reboot/suspend the host on demand.
${LINUX_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl suspend, /bin/systemctl reboot, /bin/systemctl suspend
EOF
    sudo chmod 440 "$sudoers_file"
    if sudo visudo -cf "$sudoers_file" >/dev/null 2>&1; then
        log "  sudoers: ${LINUX_USER} may run 'systemctl reboot|suspend' without a password."
    else
        warn "sudoers file failed validation — removing it to avoid breaking sudo."
        sudo rm -f "$sudoers_file"
    fi
}

# ── Health check ──────────────────────────────────────────────────────────────

run_health_check() {
    step "Health check"

    # Read the tunnel hostname that was actually written to host.json so the
    # health check always reflects the deployed config, not a recomputed value.
    local host_cf_hostname
    if [ -f "$HOST_CONFIG" ]; then
        host_cf_hostname="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('host_cf_hostname',''))" "$HOST_CONFIG" 2>/dev/null)"
    fi
    host_cf_hostname="${host_cf_hostname:-${LINUX_USER}.${CF_DOMAIN}}"  # fallback if host.json missing

    log "  Environment:              $HOST_ENV"
    log "  Linux user:               $LINUX_USER"
    log "  Cloudflare domain:        $CF_DOMAIN"
    log "  Host SSH port:            $HOST_SSH_PORT"
    log "  Host tunnel:              $host_cf_hostname"
    log "  Client port base:         $PORT_BASE"
    echo ""

    command_exists docker \
        && log "  docker:                   OK ($(docker --version | cut -d' ' -f3 | tr -d ','))" \
        || warn "  docker:                   MISSING"

    if command_exists cloudflared; then
        log "  cloudflared (host):       OK ($(cloudflared --version 2>&1 | head -1))"
        # systemd is guaranteed PID 1 by main()'s require_systemd_pid1.
        if systemctl is-enabled gpudev-tunnel >/dev/null 2>&1; then
            local tunnel_state
            tunnel_state="$(systemctl is-active gpudev-tunnel 2>/dev/null || echo unknown)"
            log "  host tunnel:              ${tunnel_state} (persistent via systemd)"
        else
            warn "  host tunnel:              systemd unit not enabled"
        fi
    else
        warn "  cloudflared (host):       MISSING"
    fi

    if nvidia_toolkit_installed; then
        log "  nvidia-container-toolkit: OK"
    else
        warn "  nvidia-container-toolkit: MISSING"
    fi

    $DOCKER image inspect "${BASE_IMAGE_NAME}:${BASE_IMAGE_TAG}" >/dev/null 2>&1 \
        && log "  base image:               OK (${BASE_IMAGE_NAME}:${BASE_IMAGE_TAG})" \
        || warn "  base image:               NOT BUILT"

    if $DOCKER image inspect "${BASE_IMAGE_NAME}:${BASE_IMAGE_TAG}" >/dev/null 2>&1; then
        if check_all_torch_gpus >/dev/null 2>&1; then
            local validated_count
            validated_count="$(wc -l < "$GPU_INVENTORY" | tr -d ' ')"
            log "  torch.cuda kernels:       OK (${validated_count} GPU(s))"
        else
            warn "  torch.cuda kernels:       FAIL (one or more GPU architectures unsupported)"
        fi
    fi

    if sudo sshd -t >/dev/null 2>&1; then
        if systemctl is-enabled ssh >/dev/null 2>&1; then
            log "  host sshd:                OK (port $HOST_SSH_PORT, persistent via systemd)"
        else
            warn "  host sshd:                OK (port $HOST_SSH_PORT) but systemd unit not enabled"
        fi
    else
        warn "  host sshd:                config error"
    fi

    [ -f "$HOST_CONFIG" ] \
        && log "  host.json:                OK" \
        || warn "  host.json:                MISSING"

    [ -f "$CLIENTS_CONFIG" ] \
        && log "  clients.json:             OK" \
        || warn "  clients.json:             MISSING"

    command_exists gpudev \
        && log "  gpudev CLI:               OK" \
        || warn "  gpudev CLI:               not found in PATH yet (re-login or: source ~/.bashrc)"

    if [ "$HOST_ENV" = "wsl2" ]; then
        { [ -x /mnt/c/Windows/System32/shutdown.exe ] || command_exists shutdown.exe; } \
            && log "  power control:            OK (gpudev power → Windows interop)" \
            || warn "  power control:            shutdown.exe unreachable via interop"
    else
        [ -f /etc/sudoers.d/gpudev-power ] \
            && log "  power control:            OK (sudoers: reboot/suspend)" \
            || warn "  power control:            sudoers rule not configured"
    fi

    echo ""
    log "gpudev host setup complete."
    echo ""
    log "To connect from your admin machine, add this to its ~/.ssh/config"
    log "(access is via the Cloudflare tunnel — port $HOST_SSH_PORT is internal to WSL and"
    log " is NOT reachable directly, so do not use 'ssh -p $HOST_SSH_PORT ...'):"
    echo ""
    # Canonical ProxyCommand — same form as `gpudev client info` and README.
    log "  Host $LINUX_USER"
    log "    HostName $host_cf_hostname"
    log "    User $LINUX_USER"
    log "    IdentityFile ~/.ssh/<your-admin-key>"
    log "    IdentitiesOnly yes"
    log "    ProxyCommand bash -c 'p=\$(command -v cloudflared 2>/dev/null || echo \"\$HOME/.local/bin/cloudflared\"); exec \"\$p\" access tcp --hostname %h'"
    log "    ServerAliveInterval 30"
    log "    ServerAliveCountMax 3"
    echo ""
    log "  then connect with:  ssh $LINUX_USER"
    log "  (after a distro reinstall, first run: ssh-keygen -R $host_cf_hostname)"
    echo ""
    log "Next step: gpudev client add <name>"
}

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
    assert_not_root
    assert_sudo
    require_debian_family
    detect_environment
    require_systemd_pid1   # exits cleanly on WSL2+nosystemd after triggering VM restart

    LINUX_USER="${LINUX_USER:-$(whoami)}"
    CF_DOMAIN="${CF_DOMAIN:-}"
    ADMIN_SSH_KEY="${ADMIN_SSH_KEY:-}"
    NEED_DOCKER_RELOGIN=false
    NEED_HOST_TUNNEL_RESTART=0
    DOCKER="docker"

    step "gpudev Step 1: Configure"
    load_host_config
    prompt_for_missing_values
    validate_required_values
    ensure_clients_config
    write_host_config

    step "gpudev Step 2: Install Docker"
    install_docker
    configure_docker_group
    ensure_docker_running

    step "gpudev Step 3: Install NVIDIA Container Toolkit"
    install_nvidia_container_toolkit

    step "gpudev Step 4: Verify GPU passthrough"
    verify_gpu_passthrough

    step "gpudev Step 4b: Detect GPUs and resolve ML stack"
    resolve_ml_stack

    step "gpudev Step 5: Build base image"
    build_base_image

    step "gpudev Step 5b: Verify torch CUDA"
    verify_torch_cuda

    step "gpudev Step 6: Install cloudflared on host"
    install_cloudflared_host

    step "gpudev Step 7: Configure host SSH"
    setup_host_ssh

    step "gpudev Step 8: Configure host Cloudflare tunnel"
    setup_host_cf_tunnel

    step "gpudev Step 9: Install gpudev CLI"
    fetch_companions
    install_gpudev_cli

    step "gpudev Step 10: Configure power management"
    configure_power_management

    run_health_check

    if [ "$NEED_DOCKER_RELOGIN" = "true" ]; then
        echo ""
        echo "NOTE: You were added to the docker group."
        echo "      Run 'newgrp docker' or re-login before using Docker without sudo."
    fi

    if [ "$NEED_HOST_TUNNEL_RESTART" -eq 1 ]; then
        schedule_host_tunnel_restart
    fi
}

main "$@"
