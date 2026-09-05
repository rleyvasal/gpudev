# gpudev on a standalone Linux host

A start-to-finish guide for a dedicated Linux box on your LAN. This is the
bare-metal path — it differs from the Windows/WSL2 path in `README.md`, mainly
because you have a physical console and no Windows host underneath.

**Part 1 — admin setup**: from BIOS settings on bare hardware, through the
Ubuntu install and remote SSH access, to a finished `linux-setup.sh`.
**Part 2 — onboarding a notebook client**: `%gpudev_setup` → `client add` →
`%gpudev`.

---

# Part 1 — admin setup

## Before you touch the server

### What you need

| | |
|---|---|
| **The box** | an NVIDIA GPU, a wired Ethernet port, and a disk you are willing to erase |
| **A monitor and keyboard** | for steps 1–3 only; everything from step 4 onward runs from your laptop |
| **Ubuntu Server install media** | a USB stick written from the Ubuntu Server ISO |
| **Your laptop** | on the same LAN, with an SSH client |
| **A Cloudflare account** | with a domain whose nameservers point at Cloudflare — the installer creates a tunnel and DNS records under it |

Ubuntu Server **24.04 LTS is the verified release**. Later Ubuntu releases may
work, but installer screens, NVIDIA packages and systemd behavior can change;
do not treat "newer" as tested automatically.

### Make the admin key first

Do this on **your laptop**, not the server.

`linux-setup.sh` turns off SSH password authentication. After it runs, an SSH
key is the only way in. So the key has to exist first.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/gpudev-admin -C "gpudev-admin@$(hostname -s)"
```

Do **not** hand-edit `authorized_keys` on the server — the steps below place
the key for you.

---

## 1. BIOS / firmware settings

Review these before installing Ubuntu. The Wake-on-LAN entries are required
only if you want gpudev to wake the host remotely; they are not prerequisites
for running GPU clients. Firmware names and available settings vary by board.

Enter setup with `Del` or `F2` at power-on.

| Setting | Set to | Why |
|---|---|---|
| **Resume By PCI-E Device** *(a.k.a. Wake on PCIe, Power On By PCI-E)* | **Enabled** | **Wake-on-LAN does not work without it.** Onboard and add-in NICs alike sit on PCIe, so this is the entry that gates the wake. The packet arrives, the NIC is armed, and the board ignores it. |
| Wake on PCI / Power On By PCI | Enabled | Some boards expose a separate legacy-PCI entry. Enable both — only the PCI-E one matters for a PCIe NIC, and having just the legacy one on makes the BIOS *look* configured. |
| **ErP / ErP Ready** | **Disabled** | ErP cuts standby power to the PCIe slots and the NIC. With it on, the NIC is dead while the machine is asleep and no packet can reach it. |
| **Fast Boot / Ultra Fast Boot** | Disabled | Skips NIC initialisation, and makes it hard to get back into setup. |
| **Secure Boot** | Prefer enabled | Ubuntu's pre-built NVIDIA modules are signed and normally work with Secure Boot. If Ubuntu asks you to enrol a MOK, complete that step on reboot. Disabling Secure Boot avoids MOK enrollment, but is an optional simplicity tradeoff rather than a gpudev requirement. |
| Restore on AC Power Loss | Power On | Recommended for a headless box, so it comes back after an outage. |
| Boot mode | UEFI, CSM off | What the Ubuntu installer expects. |

> **A CLEAR CMOS can reset these settings.** The button or jumper is often on
> the rear I/O panel and is easy to trigger while carrying the machine, and
> `Resume By PCI-E Device` may return to *off*. A silently reset BIOS looks
> exactly like a Linux or driver problem: the NIC still reports itself as armed
> and the magic packet still arrives, but the board ignores the wake. After
> *any* physical handling of the box, re-check this table before suspecting
> software.

---

## 2. Install Ubuntu Server

**Server, not Desktop.** There is no GUI here — the GPU is for compute, and the
machine is used over SSH.

### The username becomes your public hostname

The installer asks for a username. gpudev derives the host's tunnel hostname
from it:

```
<username>.<your-cloudflare-domain>
```

and it is also the account the SSH alias logs into. `gpudev` is a good choice.
Changing it later means recreating the tunnel and its DNS record.

### Tick "Install OpenSSH server"

Easy to miss — it is a checkbox on its own screen, not a default. Without it
the box has no SSH at all and you are stuck at the console.

If you did miss it, at the console afterwards:

```bash
sudo apt update && sudo apt install -y openssh-server
```

### Choose how the admin key reaches the server

OpenSSH supports key authentication immediately, but a new server does not yet
have the public key you created on your laptop. Choose one of these paths:

- **Universal path used by this guide:** leave SSH password authentication
  enabled. Step 4 runs `ssh-copy-id`, which asks for the Ubuntu account password
  exactly once and installs `~/.ssh/gpudev-admin.pub`.
- **Installer import:** if this exact public key is already published in your
  GitHub or Launchpad account, select **Import SSH identity** in the Ubuntu
  installer. You can then skip `ssh-copy-id` in step 4 and go directly to the
  key-login proof in step 5.

Do not copy the private file `~/.ssh/gpudev-admin` to the server. Only its
`.pub` file belongs there. `linux-setup.sh` disables password authentication
after the key-login proof succeeds.

### Storage — the step the installer does not make obvious

On the *Guided storage configuration* screen:

1. **Use an entire disk** — and check *which* disk is selected. On a machine
   with more than one drive the installer's default may not be the one you
   intend, and this step erases it.
2. Tick **Set up this disk as an LVM group**. (Leave encryption off unless you
   specifically want it; an encrypted root needs a passphrase typed at the
   console on every boot, which defeats remote power management.)
3. **Now check the logical volume size.** This is the non-intuitive part: some
   installer versions allocate only part of the disk to `ubuntu-lv` and leave
   the rest free inside the volume group. The exact default varies, so read the
   summary rather than assuming the full disk was assigned.

   On the next screen, under **FILE SYSTEM SUMMARY / USED DEVICES**, find
   `ubuntu-lv` inside `ubuntu-vg`. Select it → **Edit**. The size field shows
   the small default, and the label beside it shows the maximum. Replace the
   value with that maximum and confirm.

4. Check the summary before continuing: on a dedicated gpudev disk, `/` should
   show the capacity you intended to allocate.

**How much space gpudev needs.** A single built image variant accounts for
roughly 60 GB of images plus another 25–30 GB of Docker build cache — the CUDA
and PyTorch wheels dominate both. A second variant roughly doubles the images,
and every client gets a data volume that grows with use.

Budget **250 GB minimum** for `/`. On a box dedicated to gpudev, give it the
whole disk. Check any time with `docker system df`.

> **If you already installed and `/` is too small**, nothing is lost — the free
> space is still in the volume group:
>
> ```bash
> sudo lvextend -l +100%FREE /dev/ubuntu-vg/ubuntu-lv && sudo resize2fs /dev/ubuntu-vg/ubuntu-lv
> ```
>
> This is safe on a mounted, live filesystem. Confirm with `df -h /`.

Do not assume the automated installer created swap. After the first boot, check
with `swapon --show`. gpudev does not create or manage swap; decide whether to
configure it based on the host's RAM and workloads.

---

## 3. First boot from the console or LAN

The NVIDIA driver must work before `linux-setup.sh` will run. Setting a stable
LAN address now is also strongly recommended so the permanent SSH alias does
not drift later.

### The NVIDIA driver

**`linux-setup.sh` does not install it.** Its own Step 4 verifies GPU
passthrough by running `nvidia-smi` inside a container, and that fails hard
without a working host driver.

```bash
sudo apt update && sudo apt upgrade -y
sudo reboot
```

Reconnect after the system update, then install the compute driver:

```bash
sudo ubuntu-drivers install --gpgpu
sudo reboot
```

After the reboot:

```bash
nvidia-smi
```

You should see the GPU, its driver version, and no errors.

A compute server should use Ubuntu's `--gpgpu` selection, which chooses the
server/compute driver branch. If it reports no suitable driver, stop and check
Ubuntu's supported NVIDIA packages for the GPU and release. Do not make a PPA
or NVIDIA `.run` installer the routine fallback: mixing driver sources can
overwrite Ubuntu packages and can break Secure Boot.

```bash
sudo ubuntu-drivers list --gpgpu
```

### A stable LAN address

Note the address and the interface:

```bash
ip -4 addr show scope global | grep inet
ip link show
```

The Ubuntu installer configures DHCP. Pin the address, either with a **DHCP
reservation on your router** keyed to the NIC's MAC (simplest, and the router
is where you will send magic packets from anyway) or with a static netplan
config. The stable address keeps the `gpudev-lan` alias dependable. Wake-on-LAN
itself targets the NIC's MAC address, although a router may associate its wake
entry with the reserved address.

---

## 4. Create the permanent LAN connection

Direct LAN SSH uses the standard port **22** throughout the life of a
bare-metal gpudev host: immediately after Ubuntu is installed, after gpudev is
set up, after a reset, and after an uninstall.

Add this permanent entry to `~/.ssh/config` **on your laptop**:

```
Host gpudev-lan
  HostName <server-ip>
  User <user>
  Port 22
  IdentityFile ~/.ssh/gpudev-admin
  IdentitiesOnly yes
```

Unless you imported the key in the Ubuntu installer, install it now with one
command and one password prompt:

```bash
ssh-copy-id -i ~/.ssh/gpudev-admin.pub gpudev-lan
```

`ssh-copy-id` appends the key to `~/.ssh/authorized_keys` on the server with the
right permissions. This is why you never need to paste a key into a console:
pasting into a physical TTY has no clipboard, but `ssh-copy-id` sidesteps the
problem entirely. If you imported the key during installation, skip this
command.

---

## 5. Prove key login works — before anything disables passwords

Do not skip this. It is the difference between a recoverable mistake and a
console-only rescue.

```bash
ssh gpudev-lan
```

You should land in a shell **without being asked for the Ubuntu account
password**. A passphrase for the private key is normal. If SSH falls back to
the account password, fix the key before continuing. After lockdown reports
success, password authentication is disabled; keep the original session open
until a new key-based login succeeds.

`gpudev-lan` always means this direct LAN route. The separate `gpudev` alias
created in step 7 always means the Cloudflare tunnel, so neither entry changes
meaning later.

Both `gpudev reset` and `gpudev uninstall` preserve this access. Reset converts
the managed admin key back to a normal `authorized_keys` entry before removing
the gpudev dispatcher; uninstall does the same and also re-enables password
login. Neither command changes or removes the `gpudev-lan` entry on your laptop.

---

## 6. Run the installer

From `ssh gpudev-lan`:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/rleyvasal/gpudev/main/linux-setup.sh)
```

It will ask for:

| Prompt | What to give it |
|---|---|
| `Cloudflare domain` | the domain managed in your Cloudflare account, e.g. `example.com` |
| `sudo` password | your user password, for apt installs and `/etc` writes |

It does **not** ask for your SSH key here. The Ubuntu installer import or step 4
already put it in `authorized_keys`, and the final phase picks it up from there.

### One browser step, partway through

At the Cloudflare tunnel step the installer runs `cloudflared tunnel login`,
which prints a URL and waits. Open that URL **on your laptop**, sign in, and
authorise the domain. The server is headless — it cannot open it for you.

When it succeeds, `~/.cloudflared/cert.pem` appears and the run continues. This
is a one-time authorisation; later reinstalls reuse the same file.

The run takes a while — it installs Docker and the NVIDIA container toolkit,
resolves a PyTorch build matched to your GPU, and builds the base image
(several GB). It finishes by verifying a real CUDA kernel on every GPU.

### The last phase: admin setup

The installer ends with **Step 11: Admin setup**. It finds the key installed by
Ubuntu or `ssh-copy-id`, shows its fingerprint, records it in `host.json`, and
wraps it so `ssh gpudev-lan sleep` and `ssh gpudev-lan reboot` work.

Then it hands over to `gpudev ssh lockdown`, which disables password login and
sets the port — but **only after confirming a key actually works**. If you are
connected over the key from step 5, that is already proven and it proceeds
silently. Otherwise it asks you to run `ssh gpudev-lan` from another terminal and
waits.

If it cannot confirm, it changes nothing and tells you to run
`gpudev ssh lockdown` later. Password login stays on rather than being disabled
against an unverified key.

> Unattended installs: pass `--no-lockdown` to skip the phase entirely.

### What it changes

- installs Docker + NVIDIA Container Toolkit, adds you to the `docker` group
- builds the `gpudev-base` image
- creates a Cloudflare tunnel and a systemd unit for it
- arms wake-on-LAN (`ethtool -s <iface> wol g`, re-armed at every boot by
  `gpudev-wol.service`) and disables lid/idle suspend
- **sshd, in the final phase only: LAN port 22, `PubkeyAuthentication yes`,
  `PasswordAuthentication no`**

Note what the wake-on-LAN step can and cannot do: it arms the NIC, which is the
software half. The firmware half is step 1 — **Resume By PCI-E Device enabled,
ErP disabled** — and no amount of `ethtool` makes up for it. `ethtool <iface> |
grep Wake-on` reporting `g` means the NIC is armed, *not* that the machine will
wake.

---

## 7. Confirm LAN access and add the tunnel alias

On bare Linux, the installer disables password authentication but leaves sshd
on LAN port **22**. Your `gpudev-lan` entry does not change.

Ubuntu 23.04+ activates sshd through `ssh.socket`, but `sshd_config` is still
authoritative: `openssh-server` ships a `sshd-socket-generator` that reads
`Port` and regenerates the socket's listener from it.

Confirm on your own host before disconnecting if you want to be sure:

```bash
gpudev ssh status
```

Keep the old session open until the new one works. If `ssh gpudev-lan` fails, the
still-connected session is the cheapest way to fix the port without walking to
the console.

```bash
ssh gpudev-lan
```

Once the Cloudflare tunnel is up, add the separate `Host gpudev` block printed
by `gpudev status`. It uses the tunnel hostname and a `ProxyCommand`; leave
`gpudev-lan` exactly as it is. Then verify the tunnel route separately:

```bash
ssh gpudev
```

---

## 8. Confirm the install is healthy

```bash
gpudev status
```

```bash
gpudev cloudflare
```

`gpudev cloudflare` should report the connector as **`Config: current`** and
show the host hostname answering. Anything marked `STALE` means the tunnel
connector is serving an older config — see `TROUBLESHOOTING.md`.

### Test the sleep/wake round trip

Worth doing once, while you are still next to the machine and a power button is
within reach. This is the only end-to-end proof that step 1's BIOS settings and
the installer's `ethtool` arming are both right.

```bash
ssh gpudev-lan sleep
```

Then send a magic packet to the host's MAC from another machine on the LAN (most
routers have a wake-on-LAN page; `wakeonlan` and `etherwake` also work), and
confirm `ssh gpudev-lan` comes back.

If the NIC reports `Wake-on: g` but the machine does not wake, check the
firmware and standby-power settings in step 1 first. That symptom proves only
that Linux armed the NIC, not that the firmware will honor the packet.

---

## If you get locked out

Everything above is recoverable from the physical console, which is never
affected by sshd config:

1. Log in at the console.
2. Re-enable passwords. The LAN port remains 22:

   ```bash
   gpudev ssh unlock
   ```

3. Fix the key from a normal session, then re-run:

   ```bash
   gpudev ssh lockdown
   ```

`unlock` is the supported path — it puts `sshd_config` back and reloads the
socket generator, rather than leaving a hand-edited config behind.

---

# Part 2 — onboarding a notebook client

Part 1 left you with a working host. This part adds one **client**: a container
with its own GPU access, its own SSH identity, and its own tunnel hostname,
which a SolveIt notebook connects to with `%gpudev <name>`.

Two people are usually involved — the **administrator** on the host, and the
**user** in the notebook. The steps say which is which. If you are both, you
still do all of them; you just email yourself less.

## The one thing that cannot be automated

The user's private key never leaves the notebook. Only the **public** key
travels to the host. There is no `ssh-copy-id` here — the notebook holds no
credential on the gpudev host, so nothing can push the key for you.

So exactly one line crosses the gap in each direction. That is the floor, and
the flow below hits it.

---

## Step 1 (user) — one-time setup, one cell in SolveIt

```
!curl -fsSL https://raw.githubusercontent.com/rleyvasal/gpudev/main/client-bootstrap.sh -o /tmp/gpudev-bootstrap.sh && sh /tmp/gpudev-bootstrap.sh
%run ~/.gpudev-client/CRAFT.py
%gpudev_setup solveit --domain example.com
```

Each line does exactly one job:

| Line | Job | How often |
|---|---|---|
| `!curl … sh` | fetch the runtime into `~/.gpudev-client` | once, plus for fixes |
| `%run …/CRAFT.py` | load the magics — nothing else | every session |
| `%gpudev_setup …` | make this client's key and SSH entry | **once, ever** |

The first line fetches only the ten files a client actually runs (~192 KB), not
the host-side scripts and not repository history, into **`~/.gpudev-client`** —
the same path line 2 loads from, so the destination is visible in the cell
rather than hidden in a default.

Home-relative on purpose. In SolveIt the home directory *is* the persistent
storage (`cd ~` lands in `/app/data`), so this survives kernel restarts, and the
same cell is correct on a local Jupyter or Colab where no SolveIt path exists.
It is why `~/.ssh/gpudev-<name>` persists too.

### Installing on another volume

Rarely needed. `GPUDEV_DIR` moves the files, and line 2 still does not change —
the bootstrap makes `~/.gpudev-client` a symlink to wherever you chose and
prints both paths:

```
!curl -fsSL https://raw.githubusercontent.com/rleyvasal/gpudev/main/client-bootstrap.sh -o /tmp/gpudev-bootstrap.sh && export GPUDEV_DIR=/data/gpudev && sh /tmp/gpudev-bootstrap.sh
%run ~/.gpudev-client/CRAFT.py
%gpudev_setup solveit --domain example.com
```

Choose somewhere persistent — a temp directory means re-fetching after every
kernel restart.

> **Keep the `export`.** `GPUDEV_DIR=… && sh …` sets a shell variable the `sh`
> child never inherits, so the install silently lands in the default directory
> instead. Use `export … && sh …`, or the prefix form `GPUDEV_DIR=… sh …` with
> no `&&` between them.

Setup is idempotent: re-running reuses the existing key rather than replacing
it, so it is safe to paste again.

### Which line takes which options

**Everything about the client goes on the `%gpudev_setup` line** — `--domain`,
`--variant`, `--hostname`. `%run` takes no arguments; the fetch line takes only
the `GPUDEV_*` environment variables, which are rarely needed.

For `nvcc`, `ncu`, `nsys`, TensorRT or custom CUDA compilation, ask for the
`cuda-dev` variant at setup time. Only the last line changes:

```
%gpudev_setup solveit --domain example.com --variant cuda-dev
```

The generated admin command carries the choice forward and warns that this
variant grants the container `SYS_ADMIN` and `PERFMON`.

### Updating later

Re-run the same cell. The bootstrap resolves the ref first — a 40-byte request —
and prints `Already at <sha> (main). Nothing to do.` when there is nothing to
fetch, leaving a working install untouched.

```bash
sh /tmp/gpudev-bootstrap.sh --verify   # re-hash the install against VERSION
sh /tmp/gpudev-bootstrap.sh --force    # re-fetch, repairing what --verify found
```

`GPUDEV_REF` pins to a tag or commit and `GPUDEV_DIR` changes the location. If
an update ever breaks a notebook, the commit that worked is recorded in
`<install>/VERSION` — pin to it and carry on.

`~/.gpudev-client` is the install itself in the normal case, so line 2 needs no
indirection. Only an overridden `GPUDEV_DIR` turns it into a symlink, which
exists because `%run` expands `$VAR` from the Python namespace rather than the
shell environment — `%run $GPUDEV_DIR/CRAFT.py` cannot work, and a `!` line
cannot set a Python variable for the next line to use.

Upgrading from an earlier layout is handled: a leftover symlink at that path is
replaced with the real install, and the old copy is named so you can delete it.

### Hostnames

`--domain` is the normal path: the client knows its own name, and the domain is
public DNS rather than a secret, so an administrator publishes it once.

You do not strictly need it. Without `--domain` the key is still generated and
the admin command still printed; if another gpudev client is already configured
on this notebook, CRAFT learns the domain from its SSH entry anyway. Otherwise
the administrator returns the hostname after provisioning and the user supplies
it once on the first `%gpudev`.

## Step 2 (user ↔ admin) — one round trip

`%gpudev_setup` prints the administrator's whole command:

```
Send this line to your gpudev administrator:

  gpudev client add solveit --key "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... gpudev-solveit"
```

Send **that line**. Do not send the private key path, and do not retype the
key — forward the line as printed.

### The admin side

The administrator pastes it on the host:

```bash
gpudev client add solveit --key "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... gpudev-solveit"
```

When the received command includes `--variant cuda-dev`, gpudev builds that
image automatically if it is not ready, then continues creating the client.
The first build can take about 25 minutes and uses several GB; later clients
reuse it. An administrator can optionally prewarm it with
`gpudev image build cuda-dev`.

This creates the volume, the container, the tunnel route and the ingress rule,
reloads the connector, and then proves the client answers through the tunnel:

```
=== Verifying the client through the tunnel ===
  solveit.example.com → OK (the container's sshd answered through the tunnel)
  The client can connect now.
```

If that says `NO SSH BANNER`, the container is fine but the tunnel path is not —
see **Troubleshooting** below.

It finishes by printing the line to send back:

```
Send this line back to the client:

  %gpudev solveit --hostname solveit.example.com
```

## Step 3 (user) — connect

If step 1 used `--domain`, the SSH entry already exists:

```
%gpudev solveit
```

If it did not, run the line the administrator sent back once, which writes the
entry and connects:

```
%gpudev solveit --hostname solveit.example.com
```

Either way, plain `%gpudev solveit` is the everyday command from then on, in this
and every other notebook that shares the same persistent SolveIt storage.

### Every session after this

Setup never runs again. A new notebook, or a restarted kernel, needs two lines:

```
%run ~/.gpudev-client/CRAFT.py
%gpudev solveit
```

`%run` reloads the magics into the fresh kernel; `%gpudev` reconnects. Put the
`!curl … sh` line back on top only when you want to pick up fixes — it costs a
40-byte request when already current.

### Version drift

On connecting, `%gpudev` compares this client's `VERSION` against the container's
`kernel-manager.sh version` and notes a mismatch once per session:

```
Note: client and container were built from different commits.
  this notebook: a08cbc0
  container:     cd8d68d
```

The two halves are maintained by different people — the user re-runs the
bootstrap cell, the administrator runs `gpudev client rebuild <name>` — so they
drift on their own. The contract between them is small and usually compatible,
so this is a note, not a refusal. A container built before this existed reports
nothing and the check stays quiet.

## Optional admin-first invitation

An administrator can run `gpudev client invite solveit` to print the same
two-line cell with `--domain` already filled in, rather than telling the user
the domain. The invitation makes no host changes. A convenience, not a
prerequisite.

---

## Troubleshooting

**`No SSH config for 'solveit' yet`** — the hostname was never supplied. Run
the `%gpudev <name> --hostname <host>` line the administrator printed.

**`Could not resolve hostname gpudev-solveit`** — you used the alias without
the entry existing. Same fix as above.

**`NO SSH BANNER` during `client add`** — the container is up but the tunnel does not
reach it. Usually the connector is serving an older config. On the host:

```bash
gpudev cloudflare
```

`Config: STALE` confirms it. Then:

```bash
sudo systemctl restart gpudev-tunnel
```

**Nothing happens on `%gpudev`** — check the client is running:

```bash
gpudev client list
```

---

## Admin reference

```bash
gpudev client list                      # every client and its status
gpudev client info solveit              # ssh config, hostnames, bootstrap
gpudev client rebuild solveit           # recreate the container, keep the volume
gpudev client remove solveit --yes      # delete the client AND its data
gpudev kernel status solveit            # the notebook kernel inside the client
```

`remove` deletes the volume, so the client's home directory goes with it. The
tunnel ingress is cleaned up automatically; so is the DNS record, using
cloudflared's own credentials.
