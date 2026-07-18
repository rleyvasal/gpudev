# gpudev

Self-hosted GPU compute backend for Jupyter notebooks. One Windows or Linux
machine with an NVIDIA GPU becomes a shared host; isolated per-user Linux
containers run on it; remote notebooks (e.g. SolveIt) route cells to their
container over a Cloudflare tunnel and execute on the GPU as if it were local.

## SolveIt CRAFT loader (dialog stays tiny)

Implementation lives in the **`gpudev_craft/`** package. The dialog cell should
only load it — not paste the full source (LLM context budget).

```text
gpudev/
  CRAFT.py              # short %run entry (core + commented addons)
  CRAFT_DIALOG.md       # copy-paste notes for SolveIt
  gpudev_craft/
    core.py             # %gpu, tunnel, remote_run_, Mojo
    magics.py           # install_core / install_pcviz / install_sslive
  pcviz.py              # optional point-cloud addon
```

```python
%local
%run /path/to/gpudev/CRAFT.py   # install_core()
%gpu

# In CRAFT.py, uncomment when needed:
# install_pcviz()
# install_sslive()               # finds ../sslive/sslive.py if present
# install_mojo()                 # help only; Mojo magics ship in core
```

| Install | Provides |
|---------|----------|
| `install_core()` | `%gpu` `%local` `%kernel_status` `remote_run_` (+ Mojo `%gpum` …) |
| `install_pcviz()` | `%pointcloud` `%pointcloud_var` `%pointcloud_plotly` |
| `install_sslive()` | `%slive` `%slive_export` |

After a stable load, mark the CRAFT cell **skipped** so it stays out of AI context.

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
| **Client** | a notebook machine (e.g. SolveIt cloud VM) | client SSH private key + `craft.json` | runs CRAFT.py, routes cells to *its own* container; cannot reach the host or other clients |

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
ssh gpudev          # opens the host dashboard automatically
gpudev client add alice  # provisions a new client container
gpudev power sleep       # remotely sleep the Windows machine
```

…and any notebook you onboard gets a per-client container with its own SSH key
and GPU access.

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
   hibernate / disk-spindown on AC, sets High Performance.
5. Writes `%USERPROFILE%\.wslconfig` with `vmIdleTimeout=-1` so the WSL2 VM
   doesn't auto-shut-down between sessions.
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
   every 5 min, checks if the distro is running and wakes it if not.
   Belt-and-suspenders against WSL crashes / background Windows updates.

> **Manual step — enable autologin (required for unattended reboot recovery).**
> Both tasks fire at **logon**, so for WSL to come back after a reboot *with
> nobody signing in*, Windows must auto-log-in your user. Phase A can't do this
> (it needs your password) — it detects it and prints instructions. One-time:
> if your account is a **Microsoft account**, first convert it to a **local
> account** (Settings → Accounts → Your info → *Sign in with a local account
> instead*; keep the username, set a simple local password) so you're not storing
> your Microsoft password — then enable autologin with
> [Sysinternals Autologon](https://learn.microsoft.com/sysinternals/downloads/autologon)
> (`Autologon.exe -accepteula <user> . <local-password>`), which stores it as an
> encrypted LSA secret. Without this, the stack only comes up when you manually
> sign in or run `wsl`.

The script ends with a health check; everything should be `OK`:

```
Distro:                    gpudev
Linux user:                gpudev
NVIDIA driver (Windows):   OK
WSL2 distro installed:     OK (gpudev)
Linux user (gpudev):       OK (default user, sudo)
Boot task (wake on boot):  OK (gpudev-wsl-boot)
Keepalive task (5 min):    OK (gpudev-wsl-keepalive)
Autologin (unattended boot): OK
.wslconfig (idle=disabled): OK
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
8. **Builds the gpudev base image** (`gpudev-base:latest`): Python 3.12 +
   PyTorch + CUDA libs + transformers + datasets in `/opt/venv`. **~10–20 min
   on first run** — most of the install time. Subsequent client containers
   reuse this image.
9. **Configures host sshd** on port 52100 (pubkey-only, your admin key
   authorized). Persistent via systemd.
10. **Creates the host Cloudflare tunnel.** When `cloudflared` prints an auth
    URL, complete the browser flow (see "Before you start" §5 for the exact
    click-by-click steps). Persistent via `systemd` (`gpudev-tunnel.service`).
11. **Installs the `gpudev` CLI** into `~/bin` and adds an interactive-login
    hook to `~/.bashrc` so `gpudev status` (the dashboard) renders
    automatically when you SSH in.
12. **Configures power management** for `gpudev power reboot|sleep`.

Steps 4–12 only run when systemd is already PID 1, so on a fresh WSL
install you'll see steps 1–3 the first time and steps 1–2, 4–12 the second
time. On a bare Linux host (where systemd is already PID 1) it's a single
pass through all twelve steps.

#### B.4 — Phase B health check

`linux-setup.sh` prints a full health check at the end:

```
docker:                   OK (29.x.x)
cloudflared (host):       OK
host tunnel:              active (persistent via systemd)
base image:               OK (gpudev-base:latest)
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

### On the admin laptop

```bash
ssh gpudev                          # opens admin shell on the host
gpudev client add alice                  # provisions container 'alice'
# When prompted, paste alice's PUBLIC SSH key
gpudev client info alice                 # prints SSH stanza + craft.json to share
```

`gpudev client info` outputs both blocks the notebook user needs:

```sshconfig
Host gpudev-alice
  HostName alice.example.com
  User gpudev
  IdentityFile ~/.ssh/gpudev-alice
  IdentitiesOnly yes
  ProxyCommand bash -c 'p=$(command -v cloudflared 2>/dev/null || echo "$HOME/.local/bin/cloudflared"); exec "$p" access tcp --hostname %h'
  ServerAliveInterval 30
  ServerAliveCountMax 3
```

```json
{
  "client_name": "alice"
}
```

CRAFT.py derives the SSH alias as `gpudev-alice` automatically — the same
pattern `client-setup.sh` uses for the container hostname and `gpudev client
info` uses for the SSH stanza. One value, one source of truth.

### On the notebook machine

Hand the notebook user:
- alice's **private** SSH key → `~/.ssh/gpudev-alice` (chmod 600)
- the SSH stanza → append to `~/.ssh/config`
- the JSON above → `~/.config/gpudev/craft.json`
- `cloudflared` installed and on PATH

Then in a notebook cell:

```python
%run CRAFT.py
%gpu          # send subsequent cells to the GPU container
```

The first `%gpu` triggers an SSH to `gpudev-alice`, lands as user `gpudev` on
hostname `gpudev-alice`, attaches the kernel, and starts routing. From that
point on, every cell runs on the GPU container; `%local` flips back to the
notebook.

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

### Base image package pins

`linux-setup.sh` writes pinned `requirements-torch.txt` + `requirements-base.txt`
into `~/.config/gpudev/` when building `gpudev-base`. Edit those files and
re-run setup to bump versions after testing `torch.cuda` on your driver.

### Mojo packages

The image seeds Mojo at `/opt/mojo-proj`. Each client copies that seed once to
`/home/gpudev/.mojo-proj` on the **data volume**. `%mojo_add` / pixi installs
there survive `gpudev client rebuild` (but not `client remove`).

---

## Day-to-day admin operations

All on the host (via `ssh gpudev`):

```
gpudev status                         # dashboard — also auto-shows on login
gpudev client list                    # all clients with status + uptime
gpudev client add <name>              # new client (prompts for pubkey)
gpudev client info <name>             # SSH stanza for an existing client
gpudev client restart <name>          # restart a stuck container
gpudev client logs <name>             # tail container logs
gpudev kernel doctor <name>           # diagnose a stuck Jupyter kernel
gpudev gpu                            # full nvidia-smi
gpudev cloudflare                     # tunnel + ingress + edge HTTP check
gpudev cloudflare token-set           # store Zone.DNS Edit token for client remove
gpudev disk                           # host + Docker volume usage
gpudev power reboot                   # reboot the Windows host (via WSL interop)
gpudev power sleep                    # sleep the Windows host
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
| **Windows restart** | ✅ everything *(if autologin is on)* | At logon the `gpudev-wsl-boot` task (runs as your user) wakes WSL; systemd auto-starts `ssh` + `gpudev-tunnel`; DNS is unchanged. **Requires Windows autologin** (above) so the logon happens unattended — without it, WSL stays down until you sign in or run `wsl`. |
| **WSL `--shutdown`** | ✅ everything | `gpudev-wsl-keepalive` re-wakes WSL within 5 min (or next access); systemd restarts the services. |
| **Distro reinstall** | ⚠️ mostly | Re-run `linux-setup.sh`: it re-creates the tunnel + credentials, re-points DNS with `--overwrite-dns`, and re-adds your admin key. The **one** manual step is the host-key trust on the admin side (`ssh-keygen -R`, above). |

The stale-DNS 530 that this was all about can no longer happen after a restart:
the systemd unit runs the tunnel **by UUID** (pinned to its credentials file), and
`linux-setup.sh` always `--overwrite-dns`-points the hostname at that same tunnel.

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
├── CRAFT.py              ← notebook cell-routing magics (SolveIt craft / %run)
└── pcviz.py              ← local FastHTML + three.js point-cloud viewer magics
```

Configuration on the host:

```
~/.config/gpudev/host.json      ← domain, port base, admin key, CF API token
~/.config/gpudev/clients.json   ← registry of provisioned clients
~/.cloudflared/config.yml       ← tunnel ingress rules (one entry per client + the host)
/etc/systemd/system/gpudev-tunnel.service
~/bin/{gpudev,client-setup.sh,kernel-manager.sh}
```
