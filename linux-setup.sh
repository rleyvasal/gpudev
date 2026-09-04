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

# ── cuda-dev variant ──────────────────────────────────────────────────────────
# An OPT-IN second base image for GPU profiling and custom-CUDA-op work. Not
# built by default: it is several GB larger and much slower to build than the
# default base, and most clients never need it.
#
# Why a CUDA image at all: the default base is python:3.12-slim and carries NO
# CUDA toolkit — torch ships its own runtime libraries, which is enough to RUN
# GPU code but not to compile it. That single gap is why nvcc, nsys and ncu are
# all missing there: one root cause, three symptoms.
#
# Why torch is pinned to the toolkit: building PyTorch CUDA extensions (mmcv,
# spconv, mmdetection3d's ops) requires the toolkit to match the CUDA version
# torch itself was built against. uv's automatic backend selection follows the
# DRIVER, and on a recent driver it picks cu130 — pairing a 12.8 toolkit with
# cu130 torch, a major-version mismatch that breaks extension builds in ways
# that are miserable to diagnose. Pin both sides, whichever line you choose.
#
# Why 12.8 rather than 13.x — measured, not assumed. CUDA 13 is NOT a blocker:
# mmcv 2.2.0 was built and run successfully on nvidia/cuda:13.0.3-devel with
# torch 2.14.0+cu130 for sm_120, producing results identical to the 12.8 build.
# The only obstacle there is a hardcoded compiler flag: mmcv's setup.py sets
# -std=c++17, while torch 2.14's headers require C++20, so the build dies inside
# ATen on std::strong_ordering and friends. Patching mmcv's setup.py c++17 ->
# c++20 (five lines) makes it compile in about 7 minutes.
#
# The decisive reason is spconv, not torch. spconv ships PREBUILT wheels and
# stops at spconv-cu126 — there is no cu128 and no cu130. The cu126 wheel works
# on a 12.8 runtime because CUDA 12.x guarantees minor-version compatibility.
# That guarantee does NOT span a major version, so on CUDA 13 spconv would have
# to be compiled from source. Verified: spconv-cu126 2.3.8 installs in 11s and
# runs a sparse conv3d on an RTX 5080 under 12.8.
#
# A secondary reason: the CUDA line drags torch with it (cu128 -> torch 2.11,
# cu130 -> 2.14), and the mm packages predate both, so 2.11 is the shorter jump.
#
# Verified working end to end on sm_120 under this pinning:
#   mmcv 2.1.0 (source build), mmengine 0.10.7, mmdet 3.3.0, mmdet3d 1.4.0,
#   spconv-cu126 2.3.8 — mmdet3d's own CUDA ops execute on the GPU.
# Revisit 13.x only if spconv publishes a cu13x wheel; the default base already
# runs cu130, so spconv is the only thing keeping two CUDA lines alive.
CUDA_DEV_IMAGE_NAME="gpudev-base-cuda-dev"
CUDA_DEV_BASE_IMAGE="nvidia/cuda:12.8.1-devel-ubuntu24.04"
CUDA_DEV_TORCH_BACKEND="cu128"
PROFILING_REQS="${CONFIG_DIR}/requirements-profiling.txt"

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
HOST_SCRIPTS=(linux-setup.sh gpudev gpudev-ssh-dispatch client-setup.sh kernel-manager.sh)

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

    # The admin key is NOT asked for here any more. It is enrolled in the admin
    # setup phase at the very end, where ssh-copy-id can supply it and where a
    # key can be proven to work before password login is disabled. Asking here
    # meant typing a key at a console, then hardening against it unverified.

}

validate_required_values() {
    [ -n "${CF_DOMAIN:-}" ] || fail "CF_DOMAIN is required."
    # ADMIN_SSH_KEY is optional here — admin_setup enrols it at the end. Still
    # validate one supplied up front (env var, or an existing host.json).
    if [ -n "${ADMIN_SSH_KEY:-}" ]; then
        validate_ssh_public_key "$ADMIN_SSH_KEY" || fail "Invalid admin SSH public key."
    fi
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

# Probe UNPRIVILEGED FIRST. Once the invoking user is in the docker group (or
# the host runs rootless docker), plain `docker` works and sudo buys nothing —
# but `sudo` needs a TTY, so leading with it breaks every non-interactive run
# (`gpudev image build cuda-dev` over ssh, CI, cron) with
# "sudo: A terminal is required to authenticate" even though the daemon is up
# and reachable. On a FIRST install the group membership is not active in this
# shell yet (configure_docker_group sets NEED_DOCKER_RELOGIN), so the plain
# probe fails and the sudo path below still carries the install.
docker_probe() {
    if docker info >/dev/null 2>&1; then
        DOCKER="docker"
        return 0
    fi
    if sudo -n true 2>/dev/null && sudo docker info >/dev/null 2>&1; then
        DOCKER="sudo docker"
        return 0
    fi
    if [ -t 0 ] && sudo docker info >/dev/null 2>&1; then
        DOCKER="sudo docker"
        return 0
    fi
    return 1
}

ensure_docker_running() {
    if docker_probe; then
        log "Docker daemon is running (via '${DOCKER}')."
        return 0
    fi

    log "Starting Docker daemon..."
    # systemd is guaranteed PID 1 here (require_systemd_pid1 in main()).
    sudo systemctl enable docker
    sudo systemctl start docker

    local tries=15
    while [ $tries -gt 0 ]; do
        docker_probe && break
        sleep 1
        tries=$((tries - 1))
    done

    docker_probe || fail "Docker daemon failed to start. Check: sudo systemctl status docker"
    log "Docker daemon is running (via '${DOCKER}')."
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

# BuildKit keeps uv's download/unpack cache between image builds, which makes a
# retried build cheap. A build interrupted mid-unpack (Ctrl-C, OOM kill, full
# disk) can leave a truncated entry there, and uv reuses it next build without
# revalidating, failing on a wheel that is well formed upstream:
#   error: Failed to install: nvidia_cuda_nvrtc-...whl
#     Caused by: The wheel is invalid: Invalid Wheel-Version in WHEEL file: None
# GPUDEV_UV_NO_CACHE=1 drops the cache mount from every uv layer in a generated
# Dockerfile, so that build refetches each wheel instead. No-op when unset.
strip_uv_cache_mounts() {
    local dockerfile="$1"
    [ "${GPUDEV_UV_NO_CACHE:-0}" = "1" ] || return 0
    warn "GPUDEV_UV_NO_CACHE=1 — $(basename "$dockerfile") built without uv's BuildKit cache mount (slower; refetches every wheel)."
    sed -i 's|^RUN --mount=type=cache,target=/root/.cache/uv \\$|RUN \\|' "$dockerfile"
}

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

    strip_uv_cache_mounts "$dockerfile"

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

# ── cuda-dev variant image ────────────────────────────────────────────────────

write_profiling_requirements() {
    # GENERAL profiling/inference tooling only. Project-specific packages
    # (nuscenes-devkit, mmcv, mmdetection3d, spconv) deliberately do NOT belong
    # here: they carry custom CUDA ops you will rebuild repeatedly, and baking
    # them in means a multi-GB image rebuild per attempt. Install those into the
    # client's own venv, which lives on its data volume and survives rebuilds.
    #
    # torch-tensorrt is left out of the image but IS known to work — verified at
    # 2.57x over eager on an RTX 5080. It needs three things that are easy to get
    # wrong, so it is installed per-client rather than baked in:
    #
    #   1. The wheel must come from the PyTorch cu128 index, NOT PyPI. PyPI ships
    #      a cu13-linked build that installs cleanly and dies at import on
    #      libcudart.so.13. Use --index-url with --no-deps so PyPI cannot win the
    #      resolution, then install its pure-python deps (dllist) separately.
    #   2. TensorRT must be on the 10.x line (see the cap above).
    #   3. Its version tracks torch exactly: torch 2.11 -> torch-tensorrt 2.11.
    #
    #   uv pip install --python $PY dllist
    #   uv pip install --python $PY --no-deps     #       --index-url https://download.pytorch.org/whl/cu128 torch-tensorrt==2.11.0
    #
    # ONNX -> TensorRT via onnxruntime's TensorrtExecutionProvider needs none of
    # this and works out of the box, so torch-tensorrt is a convenience, not a
    # missing capability.
    #
    # If you are installing the AV perception stack into a client venv, three
    # constraints are already known — all found the hard way, none CUDA-related:
    #
    #   mmcv MUST be 2.1.0, not 2.2.0. mmdet 3.3.0 and mmdet3d 1.4.0 assert
    #   mmcv < 2.2.0 at IMPORT time. The pin lives only in their 'mim' extra, so
    #   pip installs 2.2.0 happily and the failure appears later as an
    #   AssertionError, after a six-minute source build.
    #
    #   shapely must be overridden to 2.x. mmdet3d depends on lyft-dataset-sdk,
    #   which pins shapely 1.8.5.post1, which cannot build on PYTHON 3.12 — it
    #   uses pkgutil.ImpImporter, removed in 3.12. Install with
    #   `uv pip install --override <(echo 'shapely>=2.0.0') mmdet3d`.
    #
    #   spconv comes from the prebuilt spconv-cu126 wheel. See the CUDA note at
    #   the top of this file for why that ceiling pins the whole variant.
    mkdir -p "$CONFIG_DIR"
    # Pin the CUDA LINE, not just the version. The bare `tensorrt` package is a
    # meta-package that resolves to the NEWEST CUDA variant available, so it pulled
    # tensorrt-cu13 into this CUDA 12.8 image — the same class of toolkit/runtime
    # mismatch this variant exists to avoid, one layer down. It installs happily
    # and only fails at `import tensorrt`, against the 12.8 runtime.
    # TensorRT is capped BELOW 11 on purpose. torch-tensorrt links against
    # libnvinfer.so.10, so a TensorRT 11 runtime leaves it unloadable — and an
    # unbounded >=10.0.0 resolves to 11.x, which is how this image originally
    # shipped a TensorRT that torch-tensorrt could not use. ONNX Runtime's
    # TensorrtExecutionProvider works on either line, so capping costs nothing
    # there and buys torch-tensorrt compatibility.
    #
    # Note the CUDA line must be pinned too: the bare `tensorrt` meta-package
    # resolves to the newest CUDA variant and pulled cu13 into this cu128 image.
    # Both halves of this requirement have been wrong once.
    cat > "$PROFILING_REQS" <<'REQ'
onnx>=1.17.0
onnxruntime-gpu>=1.20.0
tensorrt-cu12>=10.0,<11
REQ
    log "Wrote profiling requirements to ${PROFILING_REQS}"
}

write_cuda_dev_dockerfile() {
    local dockerfile="${CONFIG_DIR}/Dockerfile.cuda-dev"
    # Mirrors the default base image's LAYOUT so client-setup.sh's start.sh runs
    # unchanged against either image: sshd + ssh-keygen present, /etc/ssh/sshd_config
    # sed-able, uv at /usr/local/bin, venv at /opt/venv, same profile.d venv hook.
    #
    # Two things the default base has are deliberately ABSENT, because start.sh
    # tolerates both: the pixi/Mojo layer (its seed copy is guarded by
    # `[ -d "$MOJO_SEED" ]`) and the gpudev user (start.sh useradds it at runtime).
    # Skipping them keeps this image meaningfully smaller and faster to build.
    cat > "$dockerfile" <<DOCKERFILE
# syntax=docker/dockerfile:1
FROM ${CUDA_DEV_BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \\
        openssh-server \\
        curl \\
        ca-certificates \\
        git \\
        build-essential \\
        libgl1 \\
        libglib2.0-0 \\
    && rm -rf /var/lib/apt/lists/*

# The CUDA -devel image ships nvcc but NOT the profilers. They come from the same
# CUDA apt repository the image already trusts, so no extra key setup is needed.
RUN apt-get update && apt-get install -y --no-install-recommends \\
        cuda-nsight-compute-12-8 \\
        cuda-nsight-systems-12-8 \\
    && rm -rf /var/lib/apt/lists/*

# sshd login shells do not inherit ENV, so PATH for nvcc/ncu/nsys goes in profile.d.
RUN printf '%s\\n' \\
        'export PATH="/usr/local/cuda/bin:/opt/nvidia/nsight-compute:/opt/nvidia/nsight-systems/bin:\$PATH"' \\
        > /etc/profile.d/05-gpudev-cuda.sh
ENV PATH="/usr/local/cuda/bin:\${PATH}"

RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

ENV UV_HTTP_TIMEOUT=300 \\
    UV_HTTP_RETRIES=5 \\
    UV_LINK_MODE=copy

RUN mkdir -p /run/sshd \\
    && sed -i 's/#PubkeyAuthentication yes/PubkeyAuthentication yes/' /etc/ssh/sshd_config \\
    && sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config \\
    && sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config

# uv fetches its own standalone CPython here, because this base image has no
# system Python 3.12 (unlike the default base's python:3.12-slim). By default it
# lands under /root, which is mode 700 — and containers run as the NON-ROOT
# gpudev user. Worse, every per-client venv records the interpreter path in its
# pyvenv.cfg, so a client venv built from such an image is unusable at runtime
# and fails with "Client venv not found", which points at the venv rather than at
# the unreadable interpreter behind it.
#
# Install managed Pythons somewhere world-readable instead. This MUST be ENV
# rather than a build-time ARG: client-setup.sh runs \`uv venv\` at RUNTIME from
# this image to create each client's venv, and that call has to resolve to the
# same readable location.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN uv python install 3.12 && chmod -R a+rX /opt/uv-python

RUN uv venv /opt/venv --python 3.12 --seed

# torch pinned to ${CUDA_DEV_TORCH_BACKEND} to MATCH the toolkit above. Do not
# switch this to automatic backend selection: uv follows the driver and would
# pick a newer CUDA line than the toolkit in this image.
RUN --mount=type=cache,target=/root/.cache/uv \\
    uv pip install --python /opt/venv/bin/python \\
        --torch-backend ${CUDA_DEV_TORCH_BACKEND} \\
        torch torchvision torchaudio

COPY requirements-base.txt requirements-profiling.txt /tmp/gpudev-req/
RUN --mount=type=cache,target=/root/.cache/uv \\
    uv pip install --python /opt/venv/bin/python -r /tmp/gpudev-req/requirements-base.txt
RUN --mount=type=cache,target=/root/.cache/uv \\
    uv pip install --python /opt/venv/bin/python -r /tmp/gpudev-req/requirements-profiling.txt

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:\${PATH}"

RUN printf '%s\\n' \\
        'export VIRTUAL_ENV=/home/gpudev/.venv' \\
        'export UV_PROJECT_ENVIRONMENT=/home/gpudev/.venv' \\
        'export PATH="/home/gpudev/.venv/bin:\$PATH"' \\
        > /etc/profile.d/10-gpudev-venv.sh

EXPOSE 22

CMD ["/usr/sbin/sshd", "-D"]
DOCKERFILE

    strip_uv_cache_mounts "$dockerfile"

    echo "$dockerfile"
}

build_cuda_dev_image() {
    write_base_requirements
    write_profiling_requirements

    local dockerfile
    dockerfile="$(write_cuda_dev_dockerfile | tail -n1)"
    [ -f "$dockerfile" ] || fail "cuda-dev Dockerfile not written (got: ${dockerfile:-empty})"

    log "Building ${CUDA_DEV_IMAGE_NAME}:latest from ${CUDA_DEV_BASE_IMAGE}"
    log "  This is several GB and takes a while — the CUDA toolkit and profilers dominate."

    $DOCKER build \
        --network=host \
        -f "$dockerfile" \
        -t "${CUDA_DEV_IMAGE_NAME}:latest" \
        "$CONFIG_DIR"

    log "Built ${CUDA_DEV_IMAGE_NAME}:latest"
    log ""
    log "Verify the toolchain:"
    log "  docker run --rm --gpus all ${CUDA_DEV_IMAGE_NAME}:latest bash -lc 'nvcc --version; ncu --version; nsys --version'"
    log ""
    log "Create a client on it:"
    log "  gpudev client add <name> --variant cuda-dev"
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

admin_key_present() {
    local file="$1" type blob
    type="$(printf '%s' "$ADMIN_SSH_KEY" | awk '{print $1}')"
    blob="$(printf '%s' "$ADMIN_SSH_KEY" | awk '{print $2}')"
    [ -n "$type" ] && [ -n "$blob" ] || return 1
    [ -f "$file" ] || return 1
    awk -v t="$type" -v b="$blob" \
        'index($0, t) && index($0, b) { found = 1 } END { exit !found }' "$file"
}

# ── Admin setup (last phase) ──────────────────────────────────────────────────
# Enrolls the admin key, then hands sshd hardening to `gpudev ssh lockdown`,
# which refuses to disable passwords until a key has actually worked.
#
# The key arrives by ssh-copy-id, never by typing. A copied key cannot carry a
# transcription error, which is the failure this whole ordering exists to avoid:
# hardening used to run mid-install against a hand-typed key, so one wrong
# character meant console-only recovery.

# Every non-comment key in authorized_keys, one per line, with any
# command="..." wrapper and trailing options stripped.
list_authorized_keys() {
    local file="$1"
    [ -f "$file" ] || return 0
    python3 - "$file" <<'AKEYS'
import pathlib, re, sys
pattern = re.compile(r"((?:ssh-[a-z0-9]+|ecdsa-sha2-[a-z0-9-]+)\s+[A-Za-z0-9+/=]+(?:\s+\S+)?)\s*$")
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    m = pattern.search(line)
    if m:
        print(m.group(1))
AKEYS
}

key_fingerprint() {
    printf '%s\n' "$1" > /tmp/.gpudev-fp.$$
    ssh-keygen -lf /tmp/.gpudev-fp.$$ 2>/dev/null | awk '{print $2, $NF}'
    rm -f /tmp/.gpudev-fp.$$
}

# Offer what is already in authorized_keys. ssh-copy-id put it there minutes
# ago, so on a fresh install this is a confirmation, not a search for leftovers.
pick_admin_key() {
    local file="$1" keys count
    keys="$(list_authorized_keys "$file")"
    [ -n "$keys" ] || return 1
    count="$(printf '%s\n' "$keys" | grep -c '')"

    if [ "$count" = "1" ]; then
        printf '%s
' "$keys"
        return 0
    fi

    echo "" >&2
    echo "Several keys are in ${file}:" >&2
    printf '%s\n' "$keys" | nl -w2 -s') ' >&2
    printf "Which is the admin key? [1-%s, or Enter to skip]: " "$count" >&2
    local choice
    IFS= read -r choice
    [ -n "$choice" ] || return 1
    printf '%s\n' "$keys" | sed -n "${choice}p"
}

admin_setup() {
    local authorized="${HOME}/.ssh/authorized_keys"
    mkdir -p "${HOME}/.ssh"
    touch "$authorized"
    chmod 700 "${HOME}/.ssh"
    chmod 600 "$authorized"

    local key="${ADMIN_SSH_KEY:-}"
    [ -n "$key" ] || key="$(pick_admin_key "$authorized" || true)"

    if [ -z "$key" ] && [ "${NON_INTERACTIVE:-}" != "true" ] && [ -t 0 ]; then
        local ip
        ip="$(ip -4 addr show scope global 2>/dev/null \
              | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)"
        echo ""
        echo "No admin key found. On your LAPTOP, run:"
        echo ""
        echo "    ssh-copy-id -i ~/.ssh/gpudev-admin.pub ${LINUX_USER}@${ip:-<this-host>}"
        echo ""
        echo "(no key yet?  ssh-keygen -t ed25519 -f ~/.ssh/gpudev-admin)"
        echo ""
        printf "Press Enter when it succeeds, 'p' to paste a key, or 's' to skip: "
        local answer
        IFS= read -r answer
        case "$answer" in
            p|P)
                printf "Paste the admin SSH public key: "
                IFS= read -r key
                ;;
            s|S) key="" ;;
            *)   key="$(pick_admin_key "$authorized" || true)" ;;
        esac
    fi

    if [ -z "$key" ]; then
        warn "Admin setup skipped — no key enrolled."
        warn "Password login is still ENABLED and sshd is on its current port."
        warn "When 'ssh ${LINUX_USER}@<this-host>' works with your key, finish with:"
        warn "  gpudev ssh lockdown"
        return 0
    fi

    validate_ssh_public_key "$key" || fail "That does not look like an SSH public key."

    # validate_ssh_public_key only checks the type prefix, so a mangled blob
    # passes it. ssh-keygen actually parses the key, which is the cheap way to
    # catch a truncated or mistyped paste at enrolment rather than discovering
    # it later. Lockdown's proof gate would also catch it, but by then the
    # operator has been told the key is recorded.
    local fingerprint
    fingerprint="$(key_fingerprint "$key")"
    [ -n "$fingerprint" ] || fail "ssh-keygen cannot read that key — it looks truncated or mistyped.
Re-copy the whole single line from ~/.ssh/gpudev-admin.pub."

    ADMIN_SSH_KEY="$key"
    write_host_config
    log "Admin key recorded: ${fingerprint}"

    if ! admin_key_present "$authorized"; then
        echo "$key" >> "$authorized"
        chmod 600 "$authorized"
        log "Admin key added to authorized_keys."
    fi

    # Wrap the key in the forced command so `ssh gpudev sleep|reboot` work. Its
    # own replace step is blob-based, so this also collapses any duplicate.
    if [ -x "${HOME}/bin/gpudev-ssh-dispatch" ]; then
        "${HOME}/bin/gpudev-ssh-dispatch" --install "$HOST_CONFIG" || true
    fi

    if [ "${GPUDEV_NO_LOCKDOWN:-0}" = "1" ]; then
        log "--no-lockdown: sshd left untouched. Password login is still enabled."
        log "Finish later with:  gpudev ssh lockdown"
        return 0
    fi

    if [ -x "${HOME}/bin/gpudev" ]; then
        "${HOME}/bin/gpudev" ssh lockdown || \
            warn "Lockdown did not complete. Re-run: gpudev ssh lockdown"
    else
        warn "gpudev CLI not installed — cannot lock down automatically."
        warn "Run once it is available:  gpudev ssh lockdown"
    fi
}

setup_host_ssh() {
    log "Installing and enabling host sshd (hardening happens at the end)..."

    sudo apt-get install -qy openssh-server 2>/dev/null || true

    # NOTE: this step deliberately does NOT harden sshd any more. Disabling
    # password auth and moving the port used to happen right here, mid-install,
    # against a key the operator had just typed at a console with no clipboard —
    # one wrong character in 80 of base64 and SSH was gone, console-only
    # recovery. Both now happen in `gpudev ssh lockdown`, at the very end, and
    # only after a key has demonstrably worked. See SPEC-admin-setup.md.

    sudo mkdir -p /run/sshd

    # Add admin key to authorized_keys.
    #
    # Match on the key's TYPE + BLOB, never the whole line. gpudev-ssh-dispatch
    # rewrites this entry as `command="<dispatcher>" <key>`, so a whole-line
    # match (grep -qxF) can never hit once that wrapper is installed — and this
    # re-run would then append a SECOND, unwrapped copy of the same key. sshd
    # honours the FIRST matching line, so a bare entry landing above the wrapped
    # one silently disables the `ssh gpudev sleep|reboot` shortcuts. Same
    # predicate gpudev-ssh-dispatch uses when it replaces the entry, so the two
    # agree on what "this key is already here" means.
    mkdir -p "${HOME}/.ssh"
    touch "${HOME}/.ssh/authorized_keys"
    if ! admin_key_present "${HOME}/.ssh/authorized_keys"; then
        echo "$ADMIN_SSH_KEY" >> "${HOME}/.ssh/authorized_keys"
    fi
    chmod 700 "${HOME}/.ssh"
    chmod 600 "${HOME}/.ssh/authorized_keys"

    sudo sshd -t || fail "sshd config test failed after gpudev changes."

    # systemd is guaranteed PID 1 here (require_systemd_pid1 in main()).
    sudo systemctl enable ssh
    sudo systemctl restart ssh
    log "Host sshd is persistent via systemd (auto-starts on boot)."

    log "Host sshd is running. Port and password policy are set by admin setup."
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
    grant_tunnel_reload_sudo

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
    for script in gpudev gpudev-ssh-dispatch client-setup.sh kernel-manager.sh; do
        if [ -f "${REPO_DIR}/${script}" ]; then
            cp "${REPO_DIR}/${script}" "${HOME}/bin/${script}"
            chmod +x "${HOME}/bin/${script}"
            log "${script} installed at ${HOME}/bin/${script}"
        else
            warn "${script} not found at ${REPO_DIR}/${script} — install manually."
        fi
    done

    pending_updates="$(count_pending_updates)"
    upd_total="${pending_updates%% *}"; upd_sec="${pending_updates##* }"
    if [ "${upd_sec:-0}" -gt 0 ] 2>/dev/null; then
        warn "  OS packages:              ${upd_total} pending, ${upd_sec} SECURITY"
        warn "                            gpudev does not upgrade the OS. Run: sudo apt upgrade"
    elif [ "${upd_total:-0}" -gt 0 ] 2>/dev/null; then
        log "  OS packages:              ${upd_total} pending (none security-flagged)"
    else
        log "  OS packages:              OK (up to date)"
    fi

    if [ -x "${HOME}/bin/gpudev-ssh-dispatch" ]; then
        "${HOME}/bin/gpudev-ssh-dispatch" --install "$HOST_CONFIG"
    fi
}

# ── Step 10: Power management ─────────────────────────────────────────────────

# Make the host stay awake on its own but reboot/suspend on demand (used by
# `gpudev power`, e.g. from CRAFT's %reboot / %sleep). On WSL2 the power action
# targets the *Windows* host via interop — gpudev handles that itself, so there
# is nothing to configure on the Linux side beyond a sanity check.
# `gpudev client add|remove` rewrites the tunnel ingress, then reloads the
# connector from a DETACHED setsid process (so the command can return before the
# connector drops the caller's own ssh session). That process has no TTY, so a
# password-prompting `sudo systemctl restart gpudev-tunnel` cannot authenticate:
# it failed with "sudo: A terminal is required to authenticate" into
# ~/.cloudflared/reload.log, which nobody reads. The visible symptom is a new
# client whose hostname never starts working — cloudflared keeps serving the
# ingress it loaded at boot, so the client's tunnel answers
# "websocket: bad handshake". Same narrow-grant pattern as gpudev-power above.
grant_tunnel_reload_sudo() {
    local sudoers_file="/etc/sudoers.d/gpudev-tunnel"
    sudo tee "$sudoers_file" >/dev/null <<EOF
# gpudev: allow ${LINUX_USER} to reload the tunnel connector after a client change.
${LINUX_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart gpudev-tunnel, /bin/systemctl restart gpudev-tunnel
EOF
    sudo chmod 440 "$sudoers_file"
    if sudo visudo -cf "$sudoers_file" >/dev/null 2>&1; then
        log "  sudoers: ${LINUX_USER} may restart gpudev-tunnel without a password."
    else
        warn "tunnel sudoers file failed validation — removing it to avoid breaking sudo."
        sudo rm -f "$sudoers_file"
    fi
}

configure_power_management() {
    # Scheduled sleep/reboot operations use the user's transient systemd
    # timers. Linger keeps that manager and its timers alive after SSH logout.
    sudo loginctl enable-linger "$LINUX_USER"
    log "User systemd timer manager: persistent after logout."

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

# ── Bare-Linux only: GPU profiling counters ───────────────────────────────────

# Nsight Compute reads GPU performance counters, which the NVIDIA driver
# restricts to root by default. On Linux the restriction is a KERNEL MODULE
# parameter, not a registry key as on Windows — and unlike WSL2, where the
# counters are simply not exposed to the guest at all, lifting it here actually
# works. This is the main reason to run gpudev on bare metal rather than WSL2:
# `ncu` cannot collect counters under WSL2 no matter how the container is
# configured, verified including --privileged.
#
# OPT-IN via GPUDEV_ENABLE_PROFILING=1. Lifting the restriction lets any local
# user read GPU performance counters, so on a multi-client host one client could
# observe another's GPU activity. Fine for a single operator, not a default.
#
# Takes effect only after the nvidia modules reload — in practice, a reboot.
configure_gpu_profiling() {
    [ "$HOST_ENV" = "linux" ] || return 0
    if [ "${GPUDEV_ENABLE_PROFILING:-0}" != "1" ]; then
        log "GPU performance counters: left restricted to root (default)."
        log "  ncu will fail with ERR_NVGPUCTRPERM for non-root users."
        log "  Re-run with GPUDEV_ENABLE_PROFILING=1 to allow all users."
        return 0
    fi

    local conf=/etc/modprobe.d/gpudev-nvidia-profiling.conf
    sudo tee "$conf" >/dev/null <<'EOF'
# gpudev: allow non-root users to read GPU performance counters, so Nsight
# Compute (ncu) can profile. Without this ncu fails with ERR_NVGPUCTRPERM.
options nvidia NVreg_RestrictProfilingToAdminUsers=0
EOF
    sudo chmod 644 "$conf"
    log "  Wrote $conf"

    # The setting lives in the initramfs too on distros that load nvidia early.
    if command_exists update-initramfs; then
        sudo update-initramfs -u >/dev/null 2>&1             && log "  initramfs updated."             || warn "  update-initramfs failed; the modprobe conf still applies after reboot."
    fi
    warn "  REBOOT REQUIRED before ncu can read counters."
}

test_gpu_profiling_enabled() {
    [ -f /etc/modprobe.d/gpudev-nvidia-profiling.conf ] || return 1
    grep -q 'NVreg_RestrictProfilingToAdminUsers=0' /etc/modprobe.d/gpudev-nvidia-profiling.conf
}

# ── Bare-Linux only: benchmark clock locking ──────────────────────────────────

# Locking GPU clocks removes the largest source of run-to-run variance in
# benchmarks. On Linux this works natively, unlike WSL2 where `nvidia-smi -lgc`
# fails as both user and root and the lock has to be set from Windows.
# Persistence mode also works here; on Windows/WDDM it silently no-ops.
#
# nvidia-smi needs root to change clocks, and a benchmark harness should not run
# as root, so grant exactly those two verbs passwordless.
configure_clock_locking() {
    [ "$HOST_ENV" = "linux" ] || return 0
    command_exists nvidia-smi || { warn "nvidia-smi not found — skipping clock-lock sudoers."; return 0; }

    local smi sudoers_file=/etc/sudoers.d/gpudev-clocks
    smi="$(command -v nvidia-smi)"
    sudo tee "$sudoers_file" >/dev/null <<EOF
# gpudev: let ${LINUX_USER} lock/reset GPU clocks for benchmark runs without a
# password. Scoped to nvidia-smi only.
${LINUX_USER} ALL=(root) NOPASSWD: ${smi} -pm *, ${smi} -lgc *, ${smi} -rgc, ${smi} --lock-gpu-clocks=*, ${smi} --reset-gpu-clocks
EOF
    sudo chmod 440 "$sudoers_file"
    if sudo visudo -cf "$sudoers_file" >/dev/null 2>&1; then
        log "  sudoers: ${LINUX_USER} may lock/reset GPU clocks for benchmarks."
        log "    sudo nvidia-smi -pm 1 && sudo nvidia-smi -lgc <mhz>   # before a run"
        log "    sudo nvidia-smi -rgc                                  # after"
    else
        warn "clock-lock sudoers failed validation — removing."
        sudo rm -f "$sudoers_file"
    fi
}

# ── Bare-Linux only: wake-on-LAN ──────────────────────────────────────────────

# Windows hosts get this from windows-setup.ps1. On Linux, ethtool settings do
# not survive a reboot or a link-down, so arm the NIC on every boot with a
# systemd unit. Magic packet only (`wol g`): the other wake modes fire on
# ordinary traffic and put the host into a sleep/wake loop.
configure_wake_on_lan() {
    [ "$HOST_ENV" = "linux" ] || return 0
    command_exists ethtool || sudo apt-get install -qy ethtool >/dev/null 2>&1
    command_exists ethtool || { warn "ethtool unavailable — skipping wake-on-LAN."; return 0; }

    # First wired interface that reports magic-packet support. Wireless is
    # excluded: WoWLAN rarely survives suspend and is the usual cause of a host
    # that refuses to stay asleep.
    local iface="" cand
    for cand in $(ls /sys/class/net); do
        case "$cand" in lo|docker*|veth*|br-*|virbr*|wl*) continue ;; esac
        if sudo ethtool "$cand" 2>/dev/null | grep -q 'Supports Wake-on.*g'; then
            iface="$cand"; break
        fi
    done
    [ -n "$iface" ] || { warn "No wired NIC supports magic-packet wake — skipping."; return 0; }

    sudo tee /etc/systemd/system/gpudev-wol.service >/dev/null <<EOF
[Unit]
Description=gpudev: arm wake-on-LAN (magic packet) on ${iface}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$(command -v ethtool) -s ${iface} wol g
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now gpudev-wol.service >/dev/null 2>&1
    local mac
    mac="$(cat /sys/class/net/${iface}/address 2>/dev/null)"
    log "  wake-on-LAN armed on ${iface} (MAC ${mac}), re-armed at every boot."
    log "    Firmware must also allow it: WoL/PME enabled, ErP disabled."
}

# ── OS package updates ────────────────────────────────────────────────────────

# gpudev installs the specific packages it needs and deliberately does NOT
# upgrade the rest of the system. That is a choice, not an oversight: a kernel or
# NVIDIA package moving underneath a compiled mmcv or spconv is exactly the
# silent breakage this stack is fragile to, and an upgrade landing mid-run ruins
# a benchmark.
#
# What it must not do is let the backlog go unnoticed. This host terminates a
# Cloudflare tunnel and runs sshd behind it, so the health check REPORTS pending
# updates and calls out security ones. Acting on them stays a deliberate act.
#
# GPUDEV_AUTO_SECURITY_UPDATES=1 opts into unattended-upgrades restricted to the
# security pocket, with kernel, NVIDIA and container packages held so the
# toolchain cannot shift under a build, and automatic reboots off.

# Prints "<total> <security>"; "0 0" when apt cannot be queried.
count_pending_updates() {
    local list total sec
    list="$(apt list --upgradable 2>/dev/null | tail -n +2)" || { echo "0 0"; return; }
    total="$(printf '%s' "$list" | grep -c . || true)"
    sec="$(printf '%s' "$list" | grep -ci security || true)"
    echo "${total:-0} ${sec:-0}"
}

configure_auto_security_updates() {
    [ "${GPUDEV_AUTO_SECURITY_UPDATES:-0}" = "1" ] || return 0

    log "Enabling unattended SECURITY updates (opt-in)..."
    sudo apt-get install -qy unattended-upgrades >/dev/null 2>&1 || {
        warn "  could not install unattended-upgrades"; return 0; }

    # Security pocket only, and hold everything this stack is pinned against. A
    # CUDA or driver bump invalidates a compiled mmcv; a docker bump restarts the
    # daemon and takes every client down with it.
    sudo tee /etc/apt/apt.conf.d/52gpudev-security >/dev/null <<'CONF'
Unattended-Upgrade::Allowed-Origins {
        "${distro_id}:${distro_codename}-security";
        "${distro_id}ESMApps:${distro_codename}-apps-security";
        "${distro_id}ESM:${distro_codename}-infra-security";
};

Unattended-Upgrade::Package-Blacklist {
        "linux-image";
        "linux-headers";
        "linux-generic";
        "nvidia-";
        "libnvidia";
        "cuda";
        "docker-ce";
        "containerd";
};

// A GPU host must never reboot itself: it could land mid-benchmark or mid-build.
Unattended-Upgrade::Automatic-Reboot "false";
CONF
    sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
    sudo systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
    log "  Security-only updates on; kernel/NVIDIA/docker held, no auto-reboot."
    if [ "$HOST_ENV" = "wsl2" ]; then
        warn "  Under WSL2 the apt timers only fire while the distro is running, so"
        warn "  updates can still lag. Watch the health check."
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

    [ -x "${HOME}/bin/gpudev-ssh-dispatch" ] \
        && log "  admin SSH shortcuts:      OK (sleep/reboot scheduling)" \
        || warn "  admin SSH shortcuts:      MISSING"

    if [ "$HOST_ENV" = "linux" ]; then
        if test_gpu_profiling_enabled; then
            log "  GPU perf counters:        OK (all users) — reboot first if just enabled"
        else
            log "  GPU perf counters:        root-only; ncu gives ERR_NVGPUCTRPERM."
            log "                            Re-run with GPUDEV_ENABLE_PROFILING=1 to allow."
        fi
        [ -f /etc/sudoers.d/gpudev-clocks ] \
            && log "  clock locking:            OK (passwordless nvidia-smi -lgc/-rgc)" \
            || warn "  clock locking:            not configured; boost adds benchmark variance"
        systemctl is-enabled gpudev-wol >/dev/null 2>&1 \
            && log "  wake-on-LAN:              OK (magic packet, re-armed each boot)" \
            || warn "  wake-on-LAN:              not armed; host cannot be woken remotely"
    fi

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
    # Sub-entry point: build ONLY the opt-in cuda-dev variant image. Invoked by
    # `gpudev image build cuda-dev` on a host that is already set up, so it skips
    # the full install and just does the docker build.
    if [ "${1:-}" = "--build-cuda-dev" ]; then
        assert_not_root
        detect_environment
        require_debian_family
        ensure_docker_running
        build_cuda_dev_image
        exit 0
    fi

    # Unattended runs must never be blocked by, or half-apply, sshd hardening.
    if [ "${1:-}" = "--no-lockdown" ]; then
        GPUDEV_NO_LOCKDOWN=1
        export GPUDEV_NO_LOCKDOWN
        shift
    fi

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
    configure_auto_security_updates

    # Bare metal only. Each is a no-op under WSL2, where the Windows side owns
    # wake-on-LAN and GPU counters are not reachable from the guest at all.
    if [ "$HOST_ENV" = "linux" ]; then
        step "gpudev Step 10b: GPU profiling counters (ncu)"
        configure_gpu_profiling

        step "gpudev Step 10c: Benchmark clock locking"
        configure_clock_locking

        step "gpudev Step 10d: Wake-on-LAN"
        configure_wake_on_lan
    fi

    # LAST, on purpose. Everything above must be finished before sshd changes:
    # lockdown restarts sshd and can drop the session running this installer.
    step "gpudev Step 11: Admin setup"
    admin_setup

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
