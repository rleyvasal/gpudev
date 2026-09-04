# gpudev on a standalone Linux host

A start-to-finish guide for a dedicated Linux box on your LAN. This is the
bare-metal path — it differs from the Windows/WSL2 path in `README.md`, mainly
because you have a physical console and no Windows host underneath.

> **Part 1 of 2 — admin setup.** Covers everything up to a working `ssh` login
> and a finished `linux-setup.sh`. Onboarding notebook clients is Part 2.

---

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
| `Paste the admin SSH public key` | the contents of `~/.ssh/gpudev-admin.pub` |
| `sudo` password | your user password, for apt installs and `/etc` writes |

For the key prompt, print it on your laptop and paste — you are in an SSH
session, so the clipboard works:

```bash
cat ~/.ssh/gpudev-admin.pub
```

Give it the **`.pub`** file. It is one line beginning `ssh-ed25519`. Never paste
the file without the `.pub` extension; that is your private key.

> This is the same key `ssh-copy-id` already installed. The installer records it
> in `host.json` so `gpudev` can manage SSH access later, and re-adds it to
> `authorized_keys` (harmless if already present).

The run takes a while — it installs Docker and the NVIDIA container toolkit,
resolves a PyTorch build matched to your GPU, and builds the base image
(several GB). It finishes by verifying a real CUDA kernel on every GPU.

### What it changes

- installs Docker + NVIDIA Container Toolkit, adds you to the `docker` group
- builds the `gpudev-base` image
- **sshd: sets a non-default port, `PubkeyAuthentication yes`,
  `PasswordAuthentication no`**
- creates a Cloudflare tunnel and a systemd unit for it
- configures wake-on-LAN and disables lid/idle suspend

---

## 5. Update the port and reconnect

The installer moved sshd off port 22, so your `ssh gpudev` alias needs its
`Port` line updated — one line, while the rest of the block from step 3 stays
as it is.

> **TODO — confirm the port.** `linux-setup.sh` writes `Port 52100` into
> `sshd_config`, but Ubuntu 24.04 ships `ssh.socket` activation, which ignores
> that setting. On at least one install both `22` and `52100` answered. Run this
> **on the host, before you disconnect**, and use what it reports:
>
> ```bash
> systemctl is-enabled ssh.socket 2>/dev/null; ss -ltnp | grep -E ':(22|52100)\b'
> ```

Edit the one line on your laptop:

```
  Port <PORT — see TODO above>
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
2. Re-enable passwords temporarily:
   `sudo sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config && sudo systemctl restart ssh`
3. Fix the key, then set it back to `no`.

---

## Next

Part 2 — onboarding a notebook client (`gpudev client invite` → `%gpu_setup` →
`gpudev client add`).
