# gpudev

Self-hosted GPU compute backend for Jupyter notebooks. One Windows or Linux
machine with an NVIDIA GPU becomes a shared host; isolated per-user Linux
containers run on it; remote notebooks (e.g. SolveIt) route cells to their
container over a Cloudflare tunnel and execute on the GPU as if it were local.

## SolveIt CRAFT loader (dialog stays tiny)

```text
gpudev/
  CRAFT.py                 # short %run entry (core only)
  CRAFT_DIALOG.md
  gpudev_craft/            # core package (GPU connect only)
  addons/                  # optional tools (%local + %run)
    pcviz.py, mojo.py      # in-tree
    sslive.py + sslive/    # thin loader + link → separate sslive repo
    tidy3.py  + tidy3/     # thin loader + link → separate tidy3 repo
    README.md
```

```python
%local
%run /path/to/gpudev/CRAFT.py
%run /path/to/gpudev/addons/pcviz.py
%run /path/to/gpudev/addons/mojo.py
%run /path/to/gpudev/addons/sslive.py
%run /path/to/gpudev/addons/tidy3.py
%gpu alice
%sslive
```

| Load | Provides |
|------|----------|
| `CRAFT.py` | `%gpu <client>` `%gpu_setup` `%local` `%kernel_status` `remote_run_` |
| `addons/pcviz.py` | `%pointcloud` `%pointcloud_var` `%pointcloud_plotly` |
| `addons/mojo.py` | `%gpum` `%mojo_*` `%bench` |
| `addons/sslive.py` | `%sslive` `%sslive_export` (link `addons/sslive` → sslive clone) |
| `addons/tidy3.py` | `tidy` / `>>` / `%%tidy3_run` (link `addons/tidy3` → [tidy3](https://github.com/rleyvasal/tidy3)) |

**plot3** is not a gpudev addon — clone [rleyvasal/plot3](https://github.com/rleyvasal/plot3) and `%run /path/to/plot3/plot3.py`.

Always **`%local` + `%run`**. Mark the CRAFT cell **skipped** after a stable load.

```
  ┌─────────────┐    Cloudflare    ┌─────────────────────────────────────┐
  │  Notebook   │ ───── tunnel ──→ │  Host (Windows + WSL2  OR  Linux)   │
  │  (SolveIt)  │                  │  ┌───────────────────────────────┐  │
  │  CRAFT.py   │                  │  │  Docker container per client  │  │
  └─────────────┘                  │  │  (kernel, venv, /home volume) │  │
                                   │  └───────────────────────────────┘  │
                                   │             NVIDIA GPU              │
                                   └─────────────────────────────────────┘
```

This README covers **setting up the Windows host** end-to-end. See `gpudev help`
on the host for day-to-day operation.

---

## Roles

The system has three roles. They are physically and cryptographically separated.

| Role | Machine | Holds | Can do |
|---|---|---|---|
| **Admin** | your laptop | admin SSH private key | provision/remove clients, reboot/sleep host, update host software, view all logs |
| **Host** | Windows + WSL2 (this guide) or bare Linux | Docker, the gpudev CLI, all client containers, Cloudflare connector | runs everything; no outbound calls except cloudflared |
| **Client** | a notebook machine (e.g. SolveIt cloud VM) | one locally generated SSH private key per client identity | runs CRAFT.py, routes each notebook kernel to the container named by `%gpu <client>`; cannot reach the host or other clients |

A client cannot become an admin: it gets its own SSH key (scoped to its
container's port-mapped sshd), no access to the host's admin port (52100), no
ability to modify the tunnel, and CRAFT.py contains no management magics.

---

## Before you start

Gather these **before** starting setup. Only needed for Phase B (`linux-setup.sh` inside WSL or on bare Linux)
- Cloudflare domain 
- Admin SSH public key

### On the Windows host

1. **Windows version**
   - Windows 10 **build 19041+** (20H1) or Windows 11.
   - Check: `winver` → look at "OS Build".
   - If older, run Windows Update first. `wsl --install` needs this baseline.

2. **Administrator account**
   - You'll run PowerShell as Administrator (the script enables Windows features
     and registers scheduled tasks). A standard user account cannot do this.

3. **NVIDIA driver for Windows (current version)**
   - The Windows-side NVIDIA driver provides GPU passthrough into WSL2 — the
     Linux driver is **not** installed inside WSL.
   - **Update to the latest** even if your machine shipped with NVIDIA Studio
     or Game Ready drivers. Older OEM-bundled drivers often predate WSL GPU
     support or ship with bugs that surface only inside WSL.
   - Download: https://www.nvidia.com/Download/index.aspx (pick your GPU model)
     or use GeForce Experience / NVIDIA App to update in place.
   - Verify after install: open PowerShell, run `nvidia-smi` — you should see
     your GPU's table with a recent driver version (596.x or newer at time of
     writing).

4. **Internet connection**
   - The setup downloads Docker, cloudflared, the NVIDIA container toolkit, and
     the gpudev base Docker image (~6–8 GB total). Reserve ~20 GB free disk.

### On the admin machine (your laptop)

5. **Cloudflare account with a domain**
   - You need a domain managed by Cloudflare (the free plan is enough).
   - Decide the domain — it'll be passed to setup as the `CF_DOMAIN`. The host
     gets `gpudev.<your-domain>`, and each client gets `<name>.<your-domain>`.
   - **No API token needed up front.** When `linux-setup.sh` reaches the tunnel
     step it prints a Cloudflare authorization URL. The flow is:
     1. Copy the URL from the WSL terminal and paste it into your browser.
     2. If you're already logged in to Cloudflare, you'll land on a page that
        lists your domains — click your domain, then click "Authorize".
     3. If you're **not** logged in, Cloudflare shows the login page first;
        after logging in, **paste the same URL into the address bar again**
        (Cloudflare drops you on your dashboard, not back into the auth flow)
        and then complete step 2.
     4. The terminal will print "You have successfully logged in." and
        `linux-setup.sh` continues.
   - The script captures a Cloudflare API token from the resulting tunnel
     credentials so `gpudev client remove` can later delete DNS records
     automatically — no manual token management.

6. **An admin SSH keypair**
   - This is the credential that authorizes you to manage the host.
   - On the admin machine (macOS/Linux example):
     ```bash
     ssh-keygen -t ed25519 -C "gpudev-admin@$(hostname -s)" -f ~/.ssh/gpudev-admin
     ```
   - You'll paste the **public** half (`~/.ssh/gpudev-admin.pub`) when
     `linux-setup.sh` prompts for it during Phase B.
   - Keep the private half safe; only the admin laptop needs it.

7. **`cloudflared` on the admin machine**
   - macOS:  `brew install cloudflared`
   - Linux:  https://github.com/cloudflare/cloudflared/releases
   - Windows: `winget install Cloudflare.cloudflared`
   - Needed so your admin machine can SSH through the tunnel after setup.

### What you'll have at the end

After the Windows host setup finishes you'll be able to, from the admin laptop:

```bash
ssh gpudev                    # open the host dashboard
ssh gpudev sleep              # sleep the Windows machine now
ssh gpudev sleep 60m          # sleep it in 60 minutes
ssh gpudev reboot 15m         # reboot it in 15 minutes
```

The setup keeps normal SSH commands working, so after opening the dashboard you
can still run `gpudev client add alice` to provision a client container. Each
notebook you onboard gets its own SSH key, container, and GPU access.

---

## Setup the host

Setup is split into **two phases**:

- **Phase A** prepares Windows (power settings, scheduled tasks) and imports the
  WSL distro + creates the Linux user — no interactive first-run.
- **Phase B** is the real gpudev install, running entirely inside WSL.

The Linux user (`gpudev`) is created automatically by Phase A when it
imports the distro via `wsl --import` — there is no Ubuntu first-run prompt to
hang on (the old OOBE-based flow could hang indefinitely on a fresh `Ubuntu`).

> On a **bare Linux host** (no Windows), skip Phase A entirely. `linux-setup.sh`
> is the whole installer; jump to Phase B.

---

### Phase A — Windows preparation

Open PowerShell **as Administrator** (right-click → "Run as administrator")
and run the one-liner. The script never writes anything outside `%ProgramData%`
and `%USERPROFILE%`, so it's safe to run from any directory:

```powershell
iex (irm https://raw.githubusercontent.com/rleyvasal/gpudev/main/windows-setup.ps1)
```

To override the defaults (distro name `gpudev`, Linux user `gpudev`, Ubuntu
series `noble` = 24.04 LTS), or to wipe an existing distro and re-import clean:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/rleyvasal/gpudev/main/windows-setup.ps1))) -Reinstall
# or e.g.:  -DistroName gpudev -LinuxUser gpudev -UbuntuSeries jammy
```

> **Note:** Running via `[scriptblock]::Create()` keeps the script in memory and bypasses the execution policy restriction, which is why this form is used instead of downloading the file. Pass `-Reinstall` for a fresh reinstall — it `wsl --unregister`s the existing distro first (this **erases** it) before re-importing.

What the script does, in order:

1. Verifies admin + Windows build 19041+.
2. Checks for `nvidia-smi.exe` (the Windows NVIDIA driver). Warns clearly if
   missing — WSL GPU passthrough requires the Windows driver and Phase B's
   GPU verification will fail without it.
3. Runs `wsl --update` to keep the WSL kernel current.
4. Configures Windows power settings (`powercfg`): disables auto-sleep /
   hibernate / disk-spindown on AC, sets High Performance. This includes the
   **system unattended sleep timeout**, which is hidden from the Settings UI and
   from `powercfg /query`, defaults to 120 s, and applies after any *device*
   wake — so a wake-on-LAN'd host would otherwise go back to sleep two minutes
   later while "Make my device sleep after" still reads *Never*. Also arms
   **wake-on-LAN** (magic packet, Ethernet) and disarms every other wake source,
   so the host sleeps on command and wakes only when you send it a packet.
5. Writes `%USERPROFILE%\.wslconfig` with both `instanceIdleTimeout=-1` and
   `vmIdleTimeout=-1`, keeping the gpudev distribution and its shared WSL2 VM
   alive between sessions. The values are written without inline comments so
   WSL cannot silently ignore them as malformed configuration.
6. Ensures the WSL2 **platform** is enabled (`wsl --install --no-distribution`).
   On a truly fresh machine that needs a reboot to turn the feature on, the
   script registers a logon scheduled task and reboots automatically, resuming
   after login. Then it **imports** the distro from a pinned Ubuntu LTS rootfs
   tarball (`wsl --import` — no OOBE, so nothing to hang on), creates the
   `gpudev` user with passwordless sudo, and writes `/etc/wsl.conf`
   (`[user] default=gpudev`, `[boot] systemd=true`).
7. Registers a **boot task** (`gpudev-wsl-boot`) that wakes the WSL VM at logon
   (`wsl -d <distro> --exec /bin/true`). It runs **as your Windows user, not
   SYSTEM** — WSL distros are per-user, so a SYSTEM task can't see or start them
   (that's the classic "nothing comes back after a reboot" failure). Phase B's
   systemd inside WSL then auto-starts ssh, docker, and the tunnel.
8. Registers a **keepalive task** (`gpudev-wsl-keepalive`, also as your user):
   every 5 min, runs a cheap `/bin/true` inside the distro, which wakes it if
   needed. The task invokes `wsl.exe` directly to avoid nested-shell quoting.
   Belt-and-suspenders against WSL crashes / background Windows updates.

9. Registers a **cold-boot task** (`gpudev-wsl-coldboot`) with logon type
   **S4U** — *run whether the user is logged on or not*, **without storing a
   password**. Fires one minute after boot and repeats every 5 min. This is the
   only one of the three that runs with **nobody signed in**, which is what
   brings the host back after a power cut. It doubles as the keepalive for that
   case, since the other two can only run inside a logon session.

> **Autologin is normally NOT needed.** Earlier versions of this README said it
> was required; that was wrong. Two mechanisms already restore the stack without
> a stored password:
>
> - **ARSO** (*Settings → Accounts → Sign-in options → "Use my sign-in info to
>   automatically finish setting up after an update"*) signs you back in after a
>   Windows Update or user-initiated restart and locks the screen. The session is
>   real, so `gpudev-wsl-boot` fires and WSL starts — it only *looks* like nobody
>   logged in. It does **not** survive power loss: Windows stashes the resume
>   credential during an orderly shutdown, which a power cut skips.
> - **`gpudev-wsl-coldboot`** (S4U, above) covers exactly that remaining case,
>   needing no session at all.
>
> Classic autologin buys nothing on top of those two, and costs a stored password
> plus a Microsoft→local account conversion. Phase A's health check reports which
> mechanism is active and only prints the autologin instructions if **neither**
> covers the host.

The script ends with a health check; everything should be `OK`:

```
Distro:                    gpudev
Linux user:                gpudev
NVIDIA driver (Windows):   OK
WSL2 distro installed:     OK (gpudev)
Linux user (gpudev):       OK (default user, sudo)
Boot task (wake on boot):  OK (gpudev-wsl-boot)
Keepalive task (5 min):    OK (gpudev-wsl-keepalive)
Host clock:                OK (zone honours DST; time service automatic)
Reboot recovery:           OK (ARSO re-signs in 'you' after a restart)
Power-loss recovery:       OK (gpudev-wsl-coldboot, S4U — needs no sign-in)
                           Allow 10-15 min after a power cut before calling it down.
Idle sleep (AC):           OK (never)
Unattended sleep (AC):     OK (never)
Wake-on-LAN:               OK (Realtek PCIe 2.5GbE Family Controller)
  Send the magic packet to MAC XX-XX-XX-XX-XX-XX (Ethernet)
.wslconfig (distro + VM idle): OK
```

If `NVIDIA driver (Windows)` is `MISSING`, install/update the driver before
Phase B. Everything else is fixable by re-running the same one-liner — Phase A
is idempotent.

---

### Phase A → Phase B: open WSL

Phase A already created your Linux user (`gpudev`) with passwordless sudo
and set it as the distro's default user — **there is no first-run prompt.**

(Recommended) Give the account a login password. It's created without one, and a
passwordless account can't set its own, so do it **as root**:

```powershell
wsl -d gpudev -u root -- passwd gpudev
```

This is optional — Phase B works without it because the account already has
passwordless sudo (so `sudo -v` succeeds non-interactively). It's just good
hygiene for a host you SSH into.

Then open WSL — you land straight at a shell as `gpudev`:

```powershell
wsl -d gpudev
```

That `gpudev@<host>:~$` prompt is your handoff point.

---

### Phase B — gpudev install inside WSL

This phase runs `linux-setup.sh` — the same script used on a bare Linux host.
It works regardless of which Linux username you picked at the prompt above.

#### B.1 — Make a checklist of values you'll paste

| Value | Example | Source |
|---|---|---|
| Cloudflare domain | `example.com` | your Cloudflare account |
| Admin public SSH key | `ssh-ed25519 AAAA…` | `cat ~/.ssh/gpudev-admin.pub` on the admin laptop |

#### B.2 — Bootstrap `linux-setup.sh`

You're already in WSL from the handoff above. Single command, no `git` needed:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/rleyvasal/gpudev/main/linux-setup.sh)
```

> Prefer to inspect before running? Two-step instead:
> ```bash
> curl -fsSL https://raw.githubusercontent.com/rleyvasal/gpudev/main/linux-setup.sh -o linux-setup.sh
> less linux-setup.sh
> bash linux-setup.sh
> ```
>
> Developing on the host (rare)? `git clone https://github.com/rleyvasal/gpudev.git ~/gpudev` and run from there — `linux-setup.sh` detects the git checkout and skips the curl-downloads.

#### B.3 — What the script does

In order:

1. **`assert_not_root`** — refuses to run as root. gpudev installs per-user
   configuration into `$HOME` (`~/.cloudflared/`, `~/.config/gpudev/`,
   `~/.ssh/authorized_keys`, `~/bin/gpudev`, a `.bashrc` hook). The systemd
   tunnel unit runs as that user, and admin SSH from your laptop lands as
   that user. Running as root puts everything in `/root/` and silently breaks
   the SSH admin path.
2. **`assert_sudo`** — runs `sudo -v`. On a WSL host set up by Phase A,
   `gpudev` has **passwordless sudo**, so this succeeds without a prompt.
   (On a bare Linux host with a normal sudoers user, it prompts for your
   password and opens a 15-minute sudo session.)
3. **First-run on WSL only: enable systemd.** On a Phase A host this is already
   done — Phase A pre-writes `[boot] systemd=true` to `/etc/wsl.conf`, so systemd
   is PID 1 from the first boot and this step is a no-op. If you somehow land
   without systemd (e.g. a hand-imported distro), the script writes the config,
   calls `/mnt/c/Windows/System32/wsl.exe --shutdown` via interop to restart the
   WSL VM, and exits cleanly. **Your terminal will close.** Re-open WSL
   (`wsl -d <distro>`) and run the same bootstrap again; the second invocation
   lands with systemd as PID 1 and proceeds with the full install.
4. **Prompts for the Cloudflare domain.** Paste it (e.g. `example.com`)
   and Enter.
5. **Prompts for the admin SSH public key.** Paste the full single-line
   `ssh-ed25519 AAAA... comment` value and Enter.
6. **Installs Docker + NVIDIA Container Toolkit + `cloudflared`.**
7. **Verifies GPU passthrough** by running `nvidia-smi` inside a test container.
8. **Detects every GPU and the NVIDIA driver**, automatically selects a
compatible official PyTorch backend, and locks the exact PyTorch package set.
9. **Builds the gpudev base image** (`gpudev-base:latest`): Python 3.12 +
   PyTorch + CUDA libs + transformers + datasets in `/opt/venv`. **~10–20 min
   on first run** — most of the install time. Subsequent client containers
   reuse this image. Setup runs a real CUDA operation on **each installed GPU**
   before accepting the image.
10. **Configures host sshd** on port 52100 (pubkey-only, your admin key
   authorized). Persistent via systemd.
11. **Creates the host Cloudflare tunnel.** When `cloudflared` prints an auth
    URL, complete the browser flow (see "Before you start" §5 for the exact
    click-by-click steps). Persistent via `systemd` (`gpudev-tunnel.service`).
12. **Installs the `gpudev` CLI** into `~/bin` and adds an interactive-login
    hook to `~/.bashrc` so `gpudev status` (the dashboard) renders
    automatically when you SSH in.
13. **Configures remote power management** and the admin SSH shortcuts. An
    immediate command is simply `ssh gpudev sleep` or `ssh gpudev reboot`;
    adding an explicit duration such as `60m` creates a persistent timer that
    survives the SSH disconnect.

Steps 4–13 only run when systemd is already PID 1, so on a fresh WSL
install you'll see steps 1–3 the first time and steps 1–2, 4–13 the second
time. On a bare Linux host (where systemd is already PID 1) it is a single pass.

For an existing gpudev host, install the shortcuts without rebuilding the base
image:

```bash
ssh gpudev
gpudev self-update
gpudev power setup       # one-time; may request the host sudo password
exit
```

Fresh installations perform this configuration automatically.

#### B.4 — Phase B health check

`linux-setup.sh` prints a full health check at the end:

```
docker:                   OK (29.x.x)
cloudflared (host):       OK
host tunnel:              active (persistent via systemd)
base image:               OK (gpudev-base:latest)
torch.cuda kernels:       OK (2 GPU(s))
host sshd:                OK (port 52100, persistent via systemd)
host.json:                OK
gpudev CLI:               OK
power control:            OK (gpudev power → Windows interop)
```

If any line is `MISSING` or `NOT PERSISTENT`, scroll up for the warning text —
it usually points at the fix. `linux-setup.sh` is idempotent: re-running fixes
incomplete state without breaking anything.

---

### Admin SSH access from your laptop

Add a stanza to your admin laptop's `~/.ssh/config`:

```sshconfig
Host gpudev
  HostName gpudev.example.com
  User gpudev
  IdentityFile ~/.ssh/gpudev-admin
  IdentitiesOnly yes
  ProxyCommand bash -c 'p=$(command -v cloudflared 2>/dev/null || echo "$HOME/.local/bin/cloudflared"); exec "$p" access tcp --hostname %h'
  ServerAliveInterval 30
  ServerAliveCountMax 3
```

(Substitute `example.com` for your domain, `gpudev` for the WSL user you chose.)

Do **not** set `Port 52100` here — that port is internal to the host/WSL. Traffic
reaches it only through the Cloudflare tunnel via `ProxyCommand`. The same
`ProxyCommand` form is printed by `linux-setup.sh` and `gpudev client info`
(PATH first, then `~/.local/bin` where CRAFT may install cloudflared).

Then:

```bash
ssh gpudev
```

You should see the dashboard render automatically:

```
═══════════════════════════════════════════════════════════════════
 GPUDEV HOST STATUS
═══════════════════════════════════════════════════════════════════
 Platform:  WSL2 (Windows host)
 Uptime:    5 minutes
 …
```

That's it — the host is ready.

---

## Add your first client

A "client" is an isolated Linux container with its own SSH key, its own home
volume, and the gpudev base image's Python environment.

### Four-step setup

1. **Admin creates an invitation:** `gpudev client invite <name>`.
2. **User pastes the two printed SolveIt cells:** the first installs/updates
   CRAFT; the second runs `%gpu_setup` to install `cloudflared`, generate the
   local key, and update `~/.ssh/config`.
3. **Admin enrolls the printed public key:** run `gpudev client add <name>` and
   paste it when prompted.
4. **User connects from their notebook:** `%gpu <name>`.

No `craft.json`, manual SSH-config editing, terminal login, private-key transfer,
or first-connect confirmation is required. New host fingerprints are accepted
and recorded automatically; unexpectedly changed fingerprints remain blocked.

### Naming convention

The client *name* (e.g. `alice`) is the internal identity — the Docker container
name, the volume name (`alice-data`), the DNS prefix (`alice.<domain>`).
What the notebook user *sees* — the SSH alias and the in-container username — is
deliberately different and prefixed with `gpudev-` / fixed at `gpudev`:

| What | Value | Why |
|---|---|---|
| Admin command | `gpudev client add alice` | the client identity |
| SSH alias on notebook | `ssh gpudev-alice` | unmistakably a gpudev resource (not a LAN host) |
| In-container user | `gpudev` | uniform across clients; makes the prompt obviously different from the notebook |
| Prompt after SSH | `gpudev@gpudev-alice:~$` | tells you "you are user gpudev on the gpudev box for alice" |
| DNS hostname | `alice.<domain>` | unchanged (the alias / HostName mismatch is fine — that's what `Host` is for) |

### Step 1 — Create an invitation on the host

```bash
ssh gpudev                         # opens admin shell on the host
gpudev client invite alice         # makes no changes; prints the SolveIt bootstrap
```

### Step 2 — Run the two-cell SolveIt bootstrap

Give the printed bootstrap to the SolveIt user. It contains two cells. The first
installs or updates this repository in SolveIt's persistent storage:

```python
%%bash
set -e
mkdir -p /app/data/gpudevd
if [ -d /app/data/gpudevd/gpudev/.git ]; then
  git -C /app/data/gpudevd/gpudev pull --ff-only
else
  git clone https://github.com/rleyvasal/gpudev.git /app/data/gpudevd/gpudev
fi
```

The second loads CRAFT and performs idempotent local setup:

```python
%run /app/data/gpudevd/gpudev/CRAFT.py
%gpu_setup alice --hostname alice.example.com
```

`%gpu_setup` installs/checks `cloudflared`, generates
`~/.ssh/gpudev-alice`, and installs a marked `Host gpudev-alice` SSH stanza.
It never replaces an existing private key. The private key stays in SolveIt;
the output contains only the public key to return to the administrator. New SSH
host fingerprints are accepted and recorded automatically; changed fingerprints
remain blocked and trigger CRAFT's targeted stale-key recovery.

### Step 3 — Enroll the public key on the host

On the host, provision the container and paste the public key when prompted:

```bash
gpudev client add alice
```

### Step 4 — Connect the notebook

The user can now connect any SolveIt notebook kernel independently:

```python
%gpu alice     # send subsequent cells to alice's GPU container
```

The first `%gpu alice` triggers an SSH to `gpudev-alice`, lands as user `gpudev` on
hostname `gpudev-alice`, attaches the kernel, and starts routing. From that
point on, every cell runs on the GPU container; `%local` flips back to the
notebook. A different notebook kernel can simultaneously use `%gpu bob`; the
client selection is kept only in that notebook kernel's memory.

### Long downloads, installs, and training progress

CRAFT uses a hybrid output renderer for remote cells:

- A silent job gets a local **GPU job in progress** display after one second,
  including elapsed time and time since the last remote output.
- Epoch loops such as `for epoch in range(5)` do not get a misleading
  indeterminate gray bar. CRAFT preserves each printed loss/metric line, infers
  fixed epoch totals when possible, starts a determinate bar at `0 / N`, and
  advances it whenever an `Epoch n:` line arrives. At completion the same bar
  shows **Epochs completed** and **Total elapsed** beneath it. If training code
  emits a real `tqdm`, fastprogress, or HTML bar, that progress is forwarded.
- Pip's byte counters become one determinate progress bar with transferred size,
  percentage, rate, and ETA. Newly created or rebuilt clients set
  `PIP_PROGRESS_BAR=raw` in the remote kernel automatically; an explicit pip
  `--progress-bar` option still wins.
- Curl's numeric meter becomes a labeled view showing the filename, downloaded
  amount, total size, speed, remaining time, and elapsed time. Its raw,
  unlabeled columns and orphaned column headings are hidden. Progress, Downloaded,
  Speed, and Remaining values appear together beneath the bar.
- Terminal bars that redraw with carriage returns (including normal `tqdm`
  output) update one display rather than flooding the cell. Percentages and
  item counts are shown with explicit labels.
- Ordinary stdout/stderr lines are preserved and batched per remote message.
  When real command or Python output arrives, it dismisses the generic activity
  card and remains the cell's final output; it is not replaced by a completion
  badge.
- Import-only Python cells remain silent when successful, like normal notebook
  imports. Slow imports can show a temporary **Loading Python packages** status,
  which disappears when loading finishes; import failures remain visible.
- Native HTML progress displays are forwarded unchanged. CRAFT detects them and
  suppresses its generic status so the notebook shows only one bar.

`fastprogress` is included in newly rebuilt base images. In SolveIt, explicitly
use its notebook renderer—the automatic selection may choose console mode:

```python
from fastprogress.fastprogress import NBProgressBar

for batch in NBProgressBar(range(100)):
    train_batch(batch)
```

For an existing host, pull and rebuild the base plus the clients that should get
`fastprogress`:

```bash
cd ~/gpudev && git pull --ff-only
bash linux-setup.sh
gpudev client rebuild --all
```

To deploy only the CRAFT renderer, update the repository in SolveIt and re-run
`CRAFT.py`; that part does not require rebuilding the GPU image. Rebuilding a
client is required for its next remote kernel to inherit pip's raw-progress
setting.

### One kernel per client (important)

Each client container runs **one** long-lived Jupyter kernel. Every notebook that
connects as that client (e.g. `solveit`) attaches to the **same** process:

- Variables, loaded models, and GPU memory are **shared** across tabs/notebooks.
- One notebook’s `%restart_kernel` clears state for everyone on that client.
- Concurrent cells from two notebooks interleave on one REPL.

For isolated work, use a **second client** (`gpudev client add bob`) or restart
when you need a clean slate.

### Rebuilds and SSH host keys

Client SSH host keys live on the data volume
(`/home/gpudev/.local/share/ssh/hostkeys/`). `gpudev client rebuild` keeps the
same fingerprint, so notebook `known_hosts` stays valid. Keys only rotate if you
`client remove` (volume deleted) or wipe the hostkeys directory. CRAFT will
auto-clear a stale `known_hosts` entry once if a key did change.

Point-cloud previews in the notebook: use **`pcviz.py`** (`%pointcloud` /
`%pointcloud_var`), not older demo scripts.

### Cloudflare API token (optional, for DNS cleanup)

`gpudev client remove` can delete the client CNAME when `host.json` has a
`cf_api_token` with **Zone.DNS Edit** on your domain. Tunnel login does not
always provide that token.

```bash
# on the host
gpudev cloudflare token-set    # paste token from dash.cloudflare.com → API Tokens
gpudev cloudflare              # shows whether token is present
```

### Automatic PyTorch selection and locking

No GPU model, CUDA toolkit, or PyTorch version needs to be entered during normal
setup. `linux-setup.sh` reads each card's model and compute capability plus the
NVIDIA driver version from inside Docker. uv then selects a compatible official
PyTorch backend and writes the exact result to
`~/.config/gpudev/pylock.gpudev-torch.toml`. The selected backend,
versions, hardware fingerprint, and validation result are also saved under
`ml_profile` in `host.json` and shown by `gpudev status`.

The lock is reused while the GPU/driver fingerprint is unchanged. Replacing a
card or updating the driver automatically triggers a new resolution on the next
setup run. To deliberately check for newer compatible packages without changing
hardware, run:

```bash
GPUDEV_ML_REFRESH=1 bash linux-setup.sh
```

Automatic selection should be used by most installations. For an unusual setup,
an administrator can override uv's backend explicitly:

```bash
GPUDEV_TORCH_BACKEND=cu128 GPUDEV_ML_REFRESH=1 bash linux-setup.sh
```

Pre-Turing cards (compute capability below 7.5) are automatically kept on
PyTorch's CUDA 12.6 legacy backend because CUDA 13 removed library and
offline-compilation support for those architectures.

The NVIDIA driver and the CUDA runtime bundled in PyTorch have different jobs;
installing the newest driver alone cannot add GPU architectures missing from a
PyTorch wheel. For that reason setup still runs and synchronizes a real tensor
operation separately on every installed card. A mixed set of GPUs is accepted
only when one resolved wheel works on all of them.

CUDA packages contain large wheels. The generated Dockerfile gives uv a
five-minute read timeout, retries failed requests five times, and keeps uv's
download cache across build attempts. If an upstream package server still times
out, pull the latest gpudev revision and rerun `bash linux-setup.sh`; completed
downloads are reused. After the base image succeeds, run the requested
`gpudev client rebuild <name>` command again.

It is safe to rerun `linux-setup.sh` through `ssh gpudev`. Step 8 keeps an
active Cloudflare connector running when its configuration is unchanged. If a
configuration change requires a restart, setup finishes first and schedules the
restart afterward; the SSH session may then disconnect briefly and can be
reconnected normally.

### Mojo packages

The image seeds Mojo at `/opt/mojo-proj`. Each client copies that seed once to
`/home/gpudev/.mojo-proj` on the **data volume**. `%mojo_add` / pixi installs
there survive `gpudev client rebuild` (but not `client remove`).

---

## GPU profiling and custom CUDA ops (`cuda-dev` variant)

The default base image is `python:3.12-slim` and carries **no CUDA toolkit** —
torch ships its own runtime libraries, which is enough to *run* GPU code but not
to compile it. That single gap is why `nvcc`, `nsys` and `ncu` are all absent:
one root cause, three symptoms.

For profiling work or building custom CUDA ops, use the opt-in `cuda-dev`
variant. It is several GB larger and much slower to build, so it is never built
by default.

```bash
gpudev image list                              # what is built
gpudev image build cuda-dev                    # ~25 min, several GB
gpudev client add bev --variant cuda-dev
```

It ships CUDA 12.8 devel (`nvcc`), Nsight Compute (`ncu`), Nsight Systems
(`nsys`), TensorRT (`tensorrt-cu12`), ONNX and onnxruntime-gpu, with torch
pinned to `cu128` to match the toolkit. A client on this variant also gets
`--cap-add=SYS_ADMIN --cap-add=PERFMON`; every client, on any variant, gets
`--shm-size=8g` because Docker's 64 MB default kills multi-worker PyTorch
DataLoaders with an error that mentions nothing about shared memory.

### Nsight Compute does not work under WSL2

**`ncu` cannot read GPU performance counters in WSL2.** This is a platform
limitation, not a configuration problem, and it is worth knowing before you plan
a profiling methodology around it.

The usual fix — NVIDIA Control Panel → Developer → Manage GPU Performance
Counters, or `RmProfilingAdminOnly=0`, which `windows-setup.ps1
-EnableGpuProfiling` sets — is necessary but **not sufficient**. That setting
governs native Windows processes; WSL2's GPU is paravirtualised and hardware
counters are not exposed to the guest. Verified: with the registry set, the host
rebooted, and `SYS_ADMIN` + `PERFMON` attached, `ncu` still returns
`ERR_NVGPUCTRPERM` — and it fails identically under `--privileged`, which rules
out container capabilities entirely.

| Tool | Under WSL2 | Why |
| --- | --- | --- |
| `nvcc` | works | plain compilation |
| `nsys` timeline | works | CUPTI *activity* tracing, no counters |
| `nsys --gpu-metrics` | fails | needs hardware counters |
| `ncu` | fails | needs hardware counters |
| `torch.profiler` | works | CUPTI activity |

So you get kernel durations, memory transfers and API overhead, but not
occupancy, memory-throughput or warp-stall analysis. For counter-level work you
need native Linux on the same card, or a cloud GPU (different silicon, which
muddies comparisons).

### Locking clocks for benchmarks

Clock locking works, but only from **Windows**, while benchmarks run inside WSL.
A benchmark harness therefore needs a Windows-side step:

```powershell
nvidia-smi -lgc 2100,2100     # Administrator PowerShell, before the run
nvidia-smi -rgc               # after
```

Two traps. `nvidia-smi -pm 1` prints *"not supported on this platform"* and
still **exits 0** on Windows/WDDM, so do not gate a harness on its exit code.
And pick a frequency below the sustained thermal ceiling rather than near the
maximum, or long runs throttle and reintroduce the variance you locked clocks to
remove. From inside WSL, `-lgc` fails as both user and root; telemetry
(`clocks.gr`, `temperature.gpu`, `power.draw`) reads fine.

### AV perception stack (mmdetection3d) — verified recipe

Verified end to end on an RTX 5080 (`sm_120`, capability 12.0) inside a
`cuda-dev` client. Install into the **client venv**, not the image: these carry
custom CUDA ops you will rebuild, and the client venv lives on the data volume,
so it survives `gpudev client rebuild`.

```bash
PY=/home/gpudev/.venv/bin/python

# Build deps. setuptools 81+ dropped pkg_resources, which mmcv's setup.py imports.
uv pip install --python $PY "setuptools<81" wheel ninja psutil

# mmcv from source, ~6 min. Pin the arch: without it the build may omit sm_120
# and fail at kernel launch instead of at build time.
MMCV_WITH_OPS=1 TORCH_CUDA_ARCH_LIST="12.0" MAX_JOBS=4 FORCE_CUDA=1   uv pip install --python $PY --no-build-isolation mmcv==2.1.0

uv pip install --python $PY spconv-cu126          # prebuilt, ~11s
uv pip install --python $PY mmengine "mmdet<3.3.1"
uv pip install --python $PY --override <(echo 'shapely>=2.0.0') mmdet3d
```

Three constraints, none of them CUDA, each of which fails in a misleading way:

| Constraint | What happens without it |
| --- | --- |
| **mmcv 2.1.0**, not 2.2.0 | mmdet/mmdet3d assert `mmcv<2.2.0` at *import*. The pin is only in their `mim` extra, so pip installs 2.2.0 happily and you discover it after a six-minute build. |
| **shapely overridden to 2.x** | mmdet3d → lyft-dataset-sdk → shapely 1.8.5.post1, which cannot build on **Python 3.12** (`pkgutil.ImpImporter` was removed). Reads as an mmdet3d failure. |
| **spconv-cu126** | No `cu128` or `cu130` wheel exists. cu126 works on 12.8 via CUDA 12.x minor-version compatibility. |

Verify:

```bash
$PY -c "
from mmdet3d.utils import register_all_modules; register_all_modules()
from mmdet3d.registry import MODELS; print(len(MODELS.module_dict), 'models')
from mmcv.ops import nms; import torch
print(torch.cuda.get_device_name(0), torch.cuda.get_device_capability(0))"
```

Expect 107 models and capability `(12, 0)`. A registry count of **0 is normal**
before `register_all_modules()` — mmdet3d registers lazily.

The `libGL.so.1` system library is already in the image; without it `import mmcv`
fails via OpenCV *after* a successful CUDA build, which reads as a broken build
rather than a missing system library.

---

## Day-to-day admin operations

From the admin computer, power commands do not require opening an interactive
host shell first:

```bash
ssh gpudev sleep                       # sleep now
ssh gpudev sleep 60m                   # sleep in 60 minutes
ssh gpudev reboot                      # reboot now
ssh gpudev reboot 15m                  # reboot in 15 minutes
ssh gpudev power status                # list pending timers
ssh gpudev power cancel                # cancel all pending timers
ssh gpudev power cancel sleep          # cancel only sleep timers
ssh gpudev power cancel <job-id>       # cancel one timer
```

Durations require a unit: `s` (seconds), `m` (minutes), `h` (hours), or `d`
(days). A bare value such as `sleep 60` is rejected to avoid an ambiguous or
accidental power action. `now` is also accepted but optional. The scheduled
jobs use the host user's systemd timer manager, so they keep running after the
SSH command exits. An immediate sleep or reboot normally closes the SSH
connection as the host powers down.

All on the host (via `ssh gpudev`):

```
gpudev status                         # dashboard — also auto-shows on login
gpudev client list                    # all clients with status + uptime
gpudev client invite <name>           # complete SolveIt bootstrap (no host changes)
gpudev client add <name>              # new client (prompts for pubkey)
gpudev client info <name>             # SSH stanza for an existing client
gpudev client restart <name>          # restart a stuck container
gpudev client logs <name>             # tail container logs
gpudev kernel doctor <name>           # diagnose a stuck Jupyter kernel
gpudev gpu                            # full nvidia-smi
gpudev cloudflare                     # tunnel + ingress + edge HTTP check
gpudev cloudflare token-set           # store Zone.DNS Edit token for client remove
gpudev disk                           # host + Docker volume usage
gpudev power reboot [15m]             # reboot now or schedule it
gpudev power sleep [60m]              # sleep now or schedule it
gpudev power status                   # list scheduled power jobs
gpudev power cancel [all|sleep|reboot|job-id]
gpudev self-update                    # pull latest CLI into ~/bin
gpudev help                           # full reference
```

`gpudev help` is the complete reference; the dashboard footer covers the
daily-driver subset.

---

## Troubleshooting

### `websocket: bad handshake` when SSH-ing as admin

The Cloudflare tunnel is up but the origin (host sshd:52100) isn't reachable.
Diagnose with `curl -I https://gpudev.<domain>`:
- **HTTP 502** → tunnel OK, sshd:52100 down. On the host (via `wsl -d gpudev`):
  `sudo systemctl status ssh` and `sudo systemctl start ssh`.
- **HTTP 530 / 1033** → no connected tunnel for that hostname. Two distinct cases:
  - **Connector down / crash-looping** (every hostname on the host is 530). On
    the host: `sudo systemctl status gpudev-tunnel`, then `sudo journalctl -u
    gpudev-tunnel -n 50`. A repeating "credentials file not found" means the
    tunnel exists on the account but its local `~/.cloudflared/<uuid>.json` was
    lost (e.g. WSL distro reinstalled) — re-run `linux-setup.sh` (it now detects
    this and recreates the tunnel).
  - **Stale DNS** (this hostname is 530 but another hostname on the *same*
    connector is 502/200). The hostname's CNAME points at an old tunnel UUID.
    The connector is fine; the DNS route is wrong. Re-point it on the host:
    ```bash
    cloudflared tunnel route dns --overwrite-dns <tunnel-name> <hostname>
    # tunnel-name is the Linux user, e.g.:
    cloudflared tunnel route dns --overwrite-dns gpudev gpudev.qsoftss.com
    ```
    `linux-setup.sh` now passes `--overwrite-dns` so a fresh install/rename can't
    leave the route pointing at a dead tunnel.

If **every hostname** returns `530 / 1033` after a Windows restart, check the
Windows-side WSL lifecycle from an elevated PowerShell:

```powershell
wsl -l --running
wsl -d gpudev -- bash -lc "systemctl is-active docker ssh gpudev-tunnel"
Get-ScheduledTaskInfo gpudev-wsl-boot
Get-ScheduledTaskInfo gpudev-wsl-keepalive
```

If `wsl -d gpudev --exec /bin/true` restores HTTP `200`, WSL was not staying
up. Ensure `%USERPROFILE%\.wslconfig` contains both settings, preserving any
other settings already present:

```ini
[general]
instanceIdleTimeout=-1

[wsl2]
vmIdleTimeout=-1
```

Apply changes with `wsl --shutdown` (this stops every WSL distribution), then
run `wsl -d gpudev --exec /bin/true`. A scheduled task result of `0` is success;
re-run the current `windows-setup.ps1` if either gpudev task is missing,
disabled, failing, or has a stale `NextRunTime`. Setup recreates and verifies
the keepalive task.

### `Permission denied (publickey)`

Tunnel works (sshd is responding) but your admin key isn't authorized.

```bash
# On the admin laptop — which key is ssh actually offering?
ssh -v gpudev 2>&1 | grep 'Offering public key'

# On the host (via wsl) — what keys are authorized?
cat ~/.ssh/authorized_keys
```

If you added a new admin key recently, append the public half to
`~/.ssh/authorized_keys` on the host (no need to remove the old one).

### `Host key verification failed` / `REMOTE HOST IDENTIFICATION HAS CHANGED` (after a host reinstall)

This is the **opposite** direction from `Permission denied`, and the two are easy
to confuse:

- *Your* admin **public** key lives in the host's `~/.ssh/authorized_keys` — it
  proves **you** to the host. Re-running `linux-setup.sh` re-adds it (you paste it,
  or it comes from `host.json`/`ADMIN_SSH_KEY`), so a reinstall never locks you out
  on this axis.
- The **host's** public key lives in *your* `~/.ssh/known_hosts` — it proves the
  **host** to you. Reinstalling the WSL distro regenerates `/etc/ssh/ssh_host_*`,
  so your cached entry no longer matches → SSH refuses to connect to defend against
  impersonation. Copying your admin key again does **not** fix this; it's the host's
  identity that changed, not yours.

Fix on the **admin machine** (safe — you reinstalled the host on purpose):

```bash
# Drop the stale host key, then reconnect and trust the new one.
ssh-keygen -R gpudev.qsoftss.com
ssh -o StrictHostKeyChecking=accept-new gpudev 'echo CONNECTED; hostname'
```

`accept-new` records an *unknown* key automatically but still refuses a *changed*
one — so the `ssh-keygen -R` is the part that consents to the reinstall. To make
reinstalls fully zero-touch (no `ssh-keygen -R`), the host's keys can be persisted
across reinstalls — see "Recovering after a restart or reinstall" below.

### Recovering after a restart or reinstall

What comes back on its own, and what needs a hand:

| Event | Auto-recovers | Why |
| --- | --- | --- |
| **Windows restart / Windows Update reboot** | ✅ everything | ARSO re-signs you in and locks the screen; the session is real, so `gpudev-wsl-boot` fires at logon and systemd auto-starts `ssh` + `gpudev-tunnel`. DNS is unchanged. No autologin or stored password needed. |
| **Power loss / dirty cold boot** | ✅ everything, but **slowly** | ARSO does not apply here. `gpudev-wsl-coldboot` (S4U) runs with no session and starts WSL a minute after boot. Measured end-to-end: ~7.5 min from Windows boot to a reachable tunnel, plus firmware time before that — **budget 10–15 min from pressing power**. See *Cold boot after power loss is slow* below. |
| **WSL `--shutdown`** | ✅ everything | `gpudev-wsl-keepalive` re-wakes WSL within 5 min while you are signed in; `gpudev-wsl-coldboot` does the same when nobody is. |
| **Distro reinstall** | ⚠️ mostly | Re-run `linux-setup.sh`: it re-creates the tunnel + credentials, re-points DNS with `--overwrite-dns`, and re-adds your admin key. The **one** manual step is the host-key trust on the admin side (`ssh-keygen -R`, above). |

The stale-DNS 530 that this was all about can no longer happen after a restart:
the systemd unit runs the tunnel **by UUID** (pinned to its credentials file), and
`linux-setup.sh` always `--overwrite-dns`-points the hostname at that same tunnel.

### Cold boot after power loss is slow

A dirty boot (power cut, or holding the power button) takes far longer than a
normal restart — often 10–15 minutes before the host answers SSH. It is not
hung. Two separate things are slow, and only one of them is Windows:

**1. Firmware memory training (the big one, on DDR5).** Boards store the results
of DDR5 memory training and reuse them across normal restarts. A power cut
discards that saved data, so firmware retrains from scratch on the next boot —
often several minutes of apparently dead black screen *before Windows starts at
all*. This is why only dirty boots are slow.

Most boards can cache the training data across power loss. The setting is called
**Memory Context Restore** (AMD / Gigabyte / ASRock / MSI) or **MRC Fast Boot**
(Intel / ASUS), usually under the memory or overclocking section of firmware
setup. Enabling it typically turns a multi-minute POST into seconds.

> It is a firmware setting, so no script here can set it — and it is a genuine
> trade-off. Reusing cached training results occasionally makes a marginal memory
> configuration less stable across cold boots. If the machine becomes flaky after
> enabling it, turn it back off and accept the slow cold boot. On a host you wake
> remotely and rarely power-cycle, slow-but-certain is a reasonable choice.

**2. The gpudev stack itself**, measured on a real recovery:

| From Windows boot | What happened |
| --- | --- |
| +1:00 | `gpudev-wsl-coldboot` fires (deliberate trigger delay — `wslservice`, Hyper-V and the network stack are not ready sooner) |
| ~+3:00 | `wslservice` up, WSL VM booting |
| ~+4:00 | WSL VM booted, systemd starting `ssh` / `docker` / `gpudev-tunnel` |
| ~+7:30 | `cloudflared` registers its edge connections — **only now is SSH reachable** |

So `ssh` failing at the 5-minute mark after a power cut is expected and means
nothing. Wait the full 15 before treating it as broken. If it is still down
after that, check `Get-ScheduledTaskInfo gpudev-wsl-coldboot` — `LastTaskResult`
should be `0` and `NextRunTime` must not be blank.

### Tunnel / sshd dies after `wsl --shutdown` or Windows reboot

Should not happen with a current install — systemd manages both. If you set up
the host before this was the default, re-run `bash ~/gpudev/linux-setup.sh`
inside WSL — it's idempotent and will install the systemd units retroactively.

### "nvidia-smi unavailable" in the dashboard

The Linux stub at `/usr/lib/wsl/lib/nvidia-smi` isn't present. Either the
NVIDIA Windows driver isn't installed, or this WSL distro was installed before
the driver. Fix: install the NVIDIA Windows driver (link above), then
`wsl --shutdown` from PowerShell.

### Setup needs to be re-run

All setup scripts (`windows-setup.ps1`, `linux-setup.sh`, `client-setup.sh`)
are idempotent. Re-running won't break anything — it'll skip what's already
done and fix what isn't.

---

## File layout

```
gpudev/
├── windows-setup.ps1     ← Phase A: Windows prep + OOBE-free distro import & user creation
├── linux-setup.sh        ← Phase B: full gpudev install (WSL2 or bare Linux)
├── client-setup.sh       ← per-client container provisioning (`gpudev client add`)
├── kernel-manager.sh     ← in-container Jupyter kernel lifecycle
├── gpudev                ← admin CLI (deployed to ~/bin on the host)
├── gpudev-ssh-dispatch   ← managed admin SSH shortcuts and command routing
├── CRAFT.py              ← notebook cell-routing magics (SolveIt craft / %run)
└── pcviz.py              ← local FastHTML + three.js point-cloud viewer magics
```

Configuration on the host:

```
~/.config/gpudev/host.json      ← domain, port base, admin key, CF API token
~/.config/gpudev/clients.json   ← registry of provisioned clients
~/.config/gpudev/gpu-inventory.csv
~/.config/gpudev/pylock.gpudev-torch.toml
~/.cloudflared/config.yml       ← tunnel ingress rules (one entry per client + the host)
/etc/systemd/system/gpudev-tunnel.service
~/bin/{gpudev,gpudev-ssh-dispatch,client-setup.sh,kernel-manager.sh}
```
