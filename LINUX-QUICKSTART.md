# gpudev on a standalone Linux host

A start-to-finish guide for a dedicated Linux box on your LAN. This is the
bare-metal path — it differs from the Windows/WSL2 path in `README.md`, mainly
because you have a physical console and no Windows host underneath.

**Part 1 — admin setup**: from a fresh Ubuntu install to a working `ssh gpudev`
and a finished `linux-setup.sh`.
**Part 2 — onboarding a notebook client**: `client invite` → `%gpu_setup` →
`client add` → `%gpu`.

---

# Part 1 — admin setup

## Before you touch the server

Do this on **your laptop**, not the server.

`linux-setup.sh` turns off SSH password authentication. After it runs, an SSH
key is the only way in. So the key has to exist first.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/gpudev-admin -C "gpudev-admin@$(hostname -s)"
```

That is the only prerequisite. Do **not** hand-edit `authorized_keys` on the
server — the steps below place the key for you.

---

## 1. Install Ubuntu

Ubuntu Server 24.04 LTS. During installation, tick **"Install OpenSSH server"**.

If you missed it, install it at the console afterwards:

```bash
sudo apt update && sudo apt install -y openssh-server
```

At this point the box accepts **password** logins. That is expected, and it is
what the next step uses. `linux-setup.sh` will close it later.

Note the machine's LAN address — `ip -4 addr show scope global | grep inet`.

---

## 2. Install your key from the laptop

One command, one password prompt, no copy-paste:

```bash
ssh-copy-id -i ~/.ssh/gpudev-admin.pub <user>@<server-ip>
```

`ssh-copy-id` appends the key to `~/.ssh/authorized_keys` on the server with the
right permissions. This is why you never need to paste a key into a console:
pasting into a physical TTY has no clipboard, but `ssh-copy-id` sidesteps the
problem entirely.

---

## 3. Prove key login works — before anything disables passwords

Do not skip this. It is the difference between a recoverable mistake and a
console-only rescue.

```bash
ssh -i ~/.ssh/gpudev-admin <user>@<server-ip>
```

You should land in a shell **without being asked for a password**. If you are
prompted, the key is not in place — fix that before continuing. Once
`linux-setup.sh` runs, a password prompt means you are locked out.

### Make it one command

Nothing below depends on the installer, so set this up now and use it for the
rest of the guide. Add to `~/.ssh/config` **on your laptop**:

```
Host gpudev
  HostName <server-ip>
  User <user>
  Port 22
  IdentityFile ~/.ssh/gpudev-admin
  IdentitiesOnly yes
```

```bash
ssh gpudev
```

`Port 22` is the pre-install default. The installer changes the port, so you
will revisit this one line in step 5 — everything else stays as written.

---

## 4. Run the installer

From `ssh gpudev`:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/rleyvasal/gpudev/main/linux-setup.sh)
```

It will ask for:

| Prompt | What to give it |
|---|---|
| `Cloudflare domain` | the domain managed in your Cloudflare account, e.g. `example.com` |
| `sudo` password | your user password, for apt installs and `/etc` writes |

It does **not** ask for your SSH key here. Step 2 already put it in
`authorized_keys`, and the final phase picks it up from there.

The run takes a while — it installs Docker and the NVIDIA container toolkit,
resolves a PyTorch build matched to your GPU, and builds the base image
(several GB). It finishes by verifying a real CUDA kernel on every GPU.

### The last phase: admin setup

The installer ends with **Step 11: Admin setup**. It finds the key
`ssh-copy-id` installed, shows its fingerprint, records it in `host.json`, and
wraps it so `ssh gpudev sleep` and `ssh gpudev reboot` work.

Then it hands over to `gpudev ssh lockdown`, which disables password login and
sets the port — but **only after confirming a key actually works**. If you are
connected over the key from step 3, that is already proven and it proceeds
silently. Otherwise it asks you to run `ssh gpudev` from another terminal and
waits.

If it cannot confirm, it changes nothing and tells you to run
`gpudev ssh lockdown` later. Password login stays on rather than being disabled
against an unverified key.

> Unattended installs: pass `--no-lockdown` to skip the phase entirely.

### What it changes

- installs Docker + NVIDIA Container Toolkit, adds you to the `docker` group
- builds the `gpudev-base` image
- creates a Cloudflare tunnel and a systemd unit for it
- configures wake-on-LAN and disables lid/idle suspend
- **sshd, in the final phase only: port 52100, `PubkeyAuthentication yes`,
  `PasswordAuthentication no`**

---

## 5. Update the port and reconnect

The installer moved sshd off port 22, so your `ssh gpudev` alias needs its
`Port` line updated — one line, while the rest of the block from step 3 stays
as it is.

The port is **52100**.

Ubuntu 24.04 activates sshd through `ssh.socket`, but `sshd_config` is still
authoritative: `openssh-server` ships a `sshd-socket-generator` that reads
`Port` and regenerates the socket's listener from it. Verified on a live
install — only 52100 listens, and 22 does not.

Confirm on your own host before disconnecting if you want to be sure:

```bash
gpudev ssh status
```

Edit the one line on your laptop:

```
  Port 52100
```

Keep the old session open until the new one works. If `ssh gpudev` fails, the
still-connected session is the cheapest way to fix the port without walking to
the console.

```bash
ssh gpudev
```

Once the Cloudflare tunnel is up you can also reach the host from outside the
LAN by pointing the same alias at the tunnel hostname with a `ProxyCommand` —
`gpudev status` prints the exact block.

---

## 6. Confirm the install is healthy

```bash
gpudev status
```

```bash
gpudev cloudflare
```

`gpudev cloudflare` should report the connector as **`Config: current`** and
show the host hostname answering. Anything marked `STALE` means the tunnel
connector is serving an older config — see `TROUBLESHOOTING.md`.

---

## If you get locked out

Everything above is recoverable from the physical console, which is never
affected by sshd config:

1. Log in at the console.
2. Re-enable passwords and restore port 22:

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
which a SolveIt notebook connects to with `%gpu <name>`.

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

## Step 1 (admin) — invite

```bash
gpudev client invite solveit
```

This makes **no changes** to the host. It prints the two cells the user pastes
into SolveIt, with the client's hostname already filled in. Send that output to
the user.

> Only needed the first time, or when you want the hostname filled in for
> someone. A user who already knows the domain can skip straight to Step 2.

## Step 2 (user) — run the two cells in SolveIt

Cell 1 installs or updates CRAFT:

```
%%bash
set -e
mkdir -p /app/data/gpudevd
if [ -d /app/data/gpudevd/gpudev/.git ]; then
  git -C /app/data/gpudevd/gpudev pull --ff-only
else
  git clone https://github.com/rleyvasal/gpudev.git /app/data/gpudevd/gpudev
fi
```

Cell 2 creates the key and the SSH entry:

```
%run /app/data/gpudevd/gpudev/CRAFT.py
%gpu_setup solveit --hostname solveit.example.com
```

`%gpu_setup` is idempotent — re-running it reuses the existing key rather than
replacing it, so it is safe to paste again.

### Without a hostname

If you do not know the hostname yet, leave it off:

```
%gpu_setup solveit
```

The key is still created and you still get the line to send. Only the SSH entry
waits — `%gpu` will tell you the exact command to finish it later.

## Step 3 (user → admin) — send one line

`%gpu_setup` prints the administrator's whole command:

```
Send this line to your gpudev administrator:

  gpudev client add solveit --key "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... gpudev-solveit"
```

Send **that line**. Do not send the private key path, and do not retype the
key — forward the line as printed.

## Step 4 (admin) — add the client

Paste it on the host:

```bash
gpudev client add solveit --key "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... gpudev-solveit"
```

Add `--variant cuda-dev` for a client that needs `nvcc`, `ncu`, `nsys` or
TensorRT. Build that image first with `gpudev image build cuda-dev`.

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

  %gpu solveit --hostname solveit.example.com
```

## Step 5 (user) — connect

```
%gpu solveit
```

Or, if you skipped the hostname in Step 2, the line the admin sent back:

```
%gpu solveit --hostname solveit.example.com
```

That writes the SSH entry and connects. Afterwards plain `%gpu solveit` is
enough, in this and every other notebook.

---

## Troubleshooting

**`No SSH config for 'solveit' yet`** — the hostname was never supplied. Run
the `%gpu <name> --hostname <host>` line the administrator printed.

**`Could not resolve hostname gpudev-solveit`** — you used the alias without
the entry existing. Same fix as above.

**`NO SSH BANNER` during Step 4** — the container is up but the tunnel does not
reach it. Usually the connector is serving an older config. On the host:

```bash
gpudev cloudflare
```

`Config: STALE` confirms it. Then:

```bash
sudo systemctl restart gpudev-tunnel
```

**Nothing happens on `%gpu`** — check the client is running:

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
