# Spec — `gpudev reset` and `gpudev uninstall`

Status: IMPLEMENTED — `reset` in `8cbd3cc`, `uninstall` and provenance in
`1e5659c`, `--cold` in `1f4a5b7`. Guards are tested; the destructive paths have
not been run against a live host.
Scope: `gpudev`, `linux-setup.sh`, `LINUX-QUICKSTART.md`.

Two commands. `reset` puts the box back to "before `linux-setup.sh`" so a clean
install can be tested in seconds. `uninstall` decommissions gpudev entirely.

---

## The trap that shapes everything

The admin key is not a plain `authorized_keys` entry. `gpudev-ssh-dispatch
--install` rewrites it as a forced command:

```
command="/home/gpudev/bin/gpudev-ssh-dispatch" ssh-ed25519 AAAAC3Nza... gpudev-admin@x0
```

Delete `~/bin/` and **every login runs a missing binary**. The key is still
valid, sshd still accepts it, and there is still no shell. An uninstall that
"only removed gpudev's own files" locks the operator out, silently, discovered
on the next connection.

So the key must be **unwrapped back to a bare entry before `~/bin` is touched**,
and access must be proven before anything irreversible happens. This is the same
ordering discipline as `ssh lockdown`: no destructive step ahead of its proof.

---

## What exists

| Tier | Items |
|---|---|
| **Access** | sshd `Port 52100`, `PasswordAuthentication no`, the wrapped admin key |
| **gpudev state** | `~/.config/gpudev`, `~/gpudev`, `~/bin/{gpudev,gpudev-ssh-dispatch,client-setup.sh,kernel-manager.sh}`, `/etc/systemd/system/gpudev-tunnel.service`, `gpudev-wol.service`, `/etc/sudoers.d/gpudev-{tunnel,power,clocks}`, `/etc/systemd/logind.conf.d/gpudev.conf`, `/etc/apt/apt.conf.d/{52gpudev-security,20auto-upgrades}`, `/etc/modprobe.d/gpudev-nvidia-profiling.conf`, the `~/.bashrc` dashboard hook |
| **Cloud state** | the Cloudflare tunnel, host + client CNAMEs |
| **Containers/images** | client containers, `gpudev-base`, `gpudev-base-cuda-dev` |
| **Client data** | `<name>-data` volumes — users' home directories |
| **Shared infra** | Docker, NVIDIA Container Toolkit, cloudflared, `docker` group |

---

## `gpudev reset`

For iterating on the installer. Removes gpudev's own state and leaves the
machine usable and reachable.

**Removes:** clients (containers + ingress + CNAMEs), the tunnel, gpudev state
and system files from the table above, `~/.config/gpudev`, `~/bin` scripts.

**Keeps:** Docker, the toolkit, cloudflared, the sshd port and password policy,
the admin public key, and the build cache (unless `--cold`).

**Does not touch sshd.** The operator is staying on the box and reinstalling
immediately; changing the port or password policy mid-reset only creates work.
Before deleting `~/bin/gpudev-ssh-dispatch`, reset removes that forced-command
wrapper from the admin key and verifies the same type+blob remains as a normal
`authorized_keys` entry. This keeps ordinary SSH access working while still
removing all gpudev executables.

### Why it keeps the build cache and the lock

Keeping the tagged images is nearly pointless — a reinstall regenerates
`Dockerfile.base` and rebuilds, replacing `gpudev-base:latest`. What makes the
rebuild fast is the **build cache**, which is a separate and larger artifact.
Measured on a live host:

```
Images         58.03GB
Build Cache    86.87GB
```

With a warm cache the base image rebuilt in **6.8 seconds**; cold it is ~13
minutes plus ~2 GB of CUDA wheels.

The hinge is one line in the generated Dockerfile:

```
COPY pylock.gpudev-torch.toml requirements-base.txt /tmp/gpudev-req/
```

Every layer after it is keyed on those file contents. A reset that deletes
`~/.config/gpudev` leaves `ml_lock_is_current` with no profile, so the next
install re-resolves with `--upgrade`. Identical upstream pins give a full cache
hit; a new torch release gives a miss from that COPY down.

**Not implemented, and deliberately so.** The plan was to stash the lock and
`ml_profile` and restore them, but the stash only helps if `linux-setup.sh`
reads it back, which would couple reset to the installer for a benefit that
usually is not needed: `uv pip compile` is deterministic, so an unchanged
upstream re-resolves to byte-identical pins and the COPY layer hits cache
anyway. The build cache carries the speed on its own. If a torch release ever
does land mid-iteration the cost is one slow rebuild, which `--cold` makes
deliberate rather than accidental.

---

## `gpudev uninstall`

Everything `reset` does, plus images, build cache, and the sshd policy revert.
Both paths unwrap the admin key before removing the dispatcher; uninstall does
it earlier because its fresh-login proof depends on the unwrapped key.

### Flags

| Flag | Effect |
|---|---|
| `--dry-run` | print the manifest, change nothing |
| `--purge-data` | also delete `<name>-data` volumes (**user home directories**) |
| `--keep-ssh` | skip the SSH revert; leave port 52100 and key-only auth |
| `--force` | bypass the password precondition and the proof gate, announcing both |

Client data volumes are **kept by default**. They are the one thing here that
cannot be rebuilt.

### The SSH revert

Reverts to `Port 22` and `PasswordAuthentication yes`, so the machine is left in
its stock state rather than carrying gpudev's sshd settings forever.

**Precondition — the account must have a usable password.** Re-enabling password
auth on an account that has none gives false comfort: the port moves, the key
wrapper goes, and there is no way in at all. Check first:

```
passwd -S <user>     # P = usable, L = locked, NP = none
```

Refuse to revert on anything but `P`, unless `--force`. Suggest `--keep-ssh`
instead, which is the right answer on a key-only cloud image.

### Order of operations

Nothing irreversible before step 5.

1. **Confirm** — operator types the hostname, as `client remove` does
2. **Preconditions** — usable password; foreign-workload scan (below)
3. **Unwrap the key** — rewrite `authorized_keys` from `command="…" <key>` to
   bare `<key>`, matching on type + blob so a wrapped entry is recognised
4. **Provisionally revert sshd** — back up `sshd_config` and `authorized_keys`,
   then set `Port 22`, passwords on, run `sshd -t`, restart, and confirm 22 is
   listening. Any failure or interruption restores both backups and reloads the
   original listener.
5. **Prove access** — a *fresh* login on port 22, observed after the prompt.
   Unlike `ssh lockdown`, **either** publickey or password counts: the goal here
   is access, not key auth. Stale evidence is still never accepted. A timeout
   restores the original sshd configuration and forced-command key wrapper.
6. **Remove** gpudev state, units, sudoers, config, `~/bin`, `~/gpudev`
7. **Cloud state** — delete client CNAMEs and the tunnel, using the `apiToken`
   in `~/.cloudflared/cert.pem`
8. **Containers, images, build cache**; volumes only with `--purge-data`
9. **Shared components** — per provenance and prompt (below)
10. **Final message** — keep-this-session-open warning, and what was left behind

If step 5 fails, restore the original sshd configuration and exact
`authorized_keys`, reload the original listener, remove nothing, and report
whether that listener was verified.

---

## Shared components: provenance, then evidence

Docker may be carrying other people's workloads. Removing it because gpudev
happens to be going away would take them down.

### Record provenance at install time

The installer already knows — every shared component has an early return:

```
install_docker:                   "Docker already installed: …"
install_nvidia_container_toolkit: "NVIDIA Container Toolkit already installed."
install_cloudflared_host:         "cloudflared already installed: …"
```

It simply never records which branch it took. Add to `host.json`:

```json
"installed_by_gpudev": {
  "docker": true,
  "nvidia_container_toolkit": true,
  "cloudflared": false
}
```

**First run wins.** Provenance cannot be inferred retroactively, and a re-run
will lie: a second `linux-setup.sh` sees Docker present and would record
`false`, erasing the truth. Write each key only when that branch actually
installs, and never downgrade `true → false`.

**Absent means unknown means keep.** Existing hosts have no such key — a live
host today has only `['admin_ssh_key', 'cf_domain', 'host_cf_hostname',
'host_env', 'host_ssh_port', 'linux_user', 'ml_profile', 'port_base']`. Unknown
must default to keeping the component.

### Then check for evidence

Provenance can be correct and still wrong for the situation: someone may have
started using that Docker *after* gpudev installed it. Before removing, scan for
anything that is not gpudev's:

- containers not listed in `clients.json`
- volumes not named `<client>-data`
- images beyond `gpudev-base*` and the images gpudev itself pulls

Anything foreign → refuse regardless of provenance, and name what was found.
This check fails safe when the flag is missing or stale, which is exactly the
situation on every host installed before this spec.

### The prompt

Per component, pre-answered by evidence rather than one blanket question:

```
Shared components:
  docker                    installed by gpudev — remove? [y/N]
  nvidia-container-toolkit  installed by gpudev — remove? [y/N]
  cloudflared               was already present — keeping
```

Default **no** on every one.

---

## Changes to existing code

| File | Change |
|---|---|
| `linux-setup.sh` | record `installed_by_gpudev` per component, first-run-wins |
| `gpudev` | `cmd_reset`, `cmd_uninstall`, dispatch, help |
| `gpudev` | `unwrap_admin_key` — strip a `command="…"` prefix, matching on type + blob |
| `gpudev` | `foreign_docker_workloads` — anything not in `clients.json` |
| `gpudev` | reuse `wait_for_fresh_publickey`, relaxed to accept password logins |
| `LINUX-QUICKSTART.md` | a short "starting over" section |

## Failure modes

| Case | Behavior |
|---|---|
| account has no usable password | refuse the revert; suggest `--keep-ssh` |
| port 22 not listening after revert | roll back sshd, abort, remove nothing |
| no fresh login within the timeout | abort, remove nothing, box still working |
| foreign containers or images present | keep Docker regardless of provenance |
| `installed_by_gpudev` absent | treat every component as unknown → keep |
| Cloudflare API unreachable | remove local state, warn that CNAMEs remain |
| run twice | idempotent; absent files are not errors |

## Decisions

1. **`reset --cold` exists.** The default keeps the build cache because that is
   what makes iterating on the installer cheap. `--cold` prunes it, because a
   warm cache skips the layers where image builds actually fail — the uv install
   of the CUDA wheels among them, which is where this project's first observed
   build failure lived. Shipped in `1f4a5b7`.
2. **The `docker` group membership is left.** It is a change to the operator's
   account rather than to gpudev, and removing it would strip Docker access
   while leaving Docker installed — the normal outcome, since Docker is kept
   whenever anything else uses it. The closing output names it and gives the
   command to undo it, so it is stated rather than silent. Shipped in `2cd53cd`.
3. **Client data volumes are kept.** A reinstall re-adopts them by name, and
   they are the one artifact here that cannot be rebuilt. `--purge-data` opts
   out and the manifest marks it as data loss.
