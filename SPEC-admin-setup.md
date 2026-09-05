# Spec — admin setup phase and SSH lockdown

Status: IMPLEMENTED — phase and `ssh` commands in `e2035b2`, port mechanism
corrected in `a955740`. Exercised on a live host: lockdown, unlock, and the
proof gate all ran; a fresh install has not.
Scope: `linux-setup.sh`, `gpudev`, `LINUX-QUICKSTART.md`.

---

## Problem

`linux-setup.sh` today does three risky things mid-run, inside
`setup_host_ssh()`:

```
Port 52100
PubkeyAuthentication yes
PasswordAuthentication no
```

It disables password authentication **before anything has proven the admin's
key works**, using a key the operator typed at a prompt. On a standalone box
that prompt is often a physical console with no clipboard, so the key is
transcribed by hand — ~80 characters of base64 where one wrong character means
SSH is now impossible and only the console can recover it.

It also moves the SSH port without reporting the result, and never tells the
operator what `~/.ssh/config` should look like.

## Goals

1. No hand-transcribed keys. A key arrives by copy, never by typing.
2. Nothing irreversible happens to sshd until a correct key is in place.
3. The operator is told the final port and the exact laptop config block.
4. Unattended installs remain possible.
5. Recovery does not require the console.

## Non-goals

- Changing client onboarding (`client invite` / `client add`).
- Managing more than one admin key. Multiple keys are detected and chosen
  between, not merged or synced.

---

## Design

### Key principle

A key installed by `ssh-copy-id` is correct **by construction** — it was
copied, not typed. That removes the transcription error that makes lockdown
dangerous, which is why the paste path becomes the fallback rather than the
default.

### New phase: Admin setup

Runs at the **end** of `linux-setup.sh`, after the image build and GPU
verification, as the last step before the summary.

```
=== gpudev Step N: Admin setup ===
```

#### State A — this session is already publickey-authenticated

Detected from `SSH_CONNECTION` (client IP + source port, unique per
connection) matched against the auth log:

```
Accepted publickey for <user> from <ip> port <port>
```

Then the admin key is already in place and proven. Skip to **Confirm**.

#### State B — `authorized_keys` has one or more keys, session is not proven

Show each key's fingerprint and comment. If exactly one, offer it as the
default; if several, ask which is the admin's. No free-text entry.

#### State C — no keys present

Print the exact command for the operator's laptop, with the real user and
address substituted:

```
No admin key found. On your LAPTOP, run:

    ssh-copy-id -i ~/.ssh/gpudev-admin.pub <user>@<ip>

(no key yet?  ssh-keygen -t ed25519 -f ~/.ssh/gpudev-admin)

Press Enter when it succeeds, or 'p' to paste a key instead, or 's' to skip.
```

On Enter, re-read `authorized_keys` and go to State B. On `p`, prompt for a
pasted key and validate it with `validate_ssh_public_key`. On `s`, skip the
whole phase (see **Skip**).

#### Confirm

- record the chosen key as `admin_ssh_key` in `host.json`
- run `gpudev-ssh-dispatch --install` to wrap it in the forced command
- print the laptop `~/.ssh/config` block, with the **pre-lockdown** port

#### Lockdown

Only after a key is confirmed:

- `PubkeyAuthentication yes`
- `PasswordAuthentication no`
- set the SSH port to `HOST_SSH_PORT` (see below)
- `sshd -t` before reloading; abort and roll back if it fails
- restart sshd, then **verify the new port is listening** before reporting
  success
- print the final `~/.ssh/config` block with the real port, and tell the
  operator to keep the current session open until the new one works

#### The port is a value, not a step (decided)

`HOST_SSH_PORT` feeds two places today:

```
linux-setup.sh:1206   set_sshd_option "Port" "$HOST_SSH_PORT"
linux-setup.sh:1297   ... ingress → ssh://localhost:${HOST_SSH_PORT}
```

The Cloudflare tunnel ingress points at it, so the port move is not optional
hardening: sshd on 22 while the tunnel dials 52100 breaks the tunnel, and
breaks it silently.

There is therefore **no "skip the port change" option**. Instead the port is a
value the operator controls, and `22` is legal. Lockdown writes whatever
`HOST_SSH_PORT` is set to into *both* sshd and the tunnel ingress, so the two
cannot desynchronize. An operator who wants to stay on 22 sets
`HOST_SSH_PORT=22` and gets password-disable with no port move; the ingress
follows automatically.

When the port actually changes, lockdown must update the ingress and reload the
connector via `reload_tunnel_connector`.

#### Skip

Chosen explicitly (`s`), or forced by `--no-lockdown`, or reached when no key
could be confirmed. Leaves sshd **completely untouched** — passwords still
work, port unchanged — and prints:

```
Admin setup skipped. Password login is still enabled.
Run 'gpudev ssh lockdown' once 'ssh gpudev' works with your key.
```

---

## New commands

Top-level noun `ssh`, parallel to the existing `cloudflare` and `power`.
Deliberately **not** `gpudev admin key add`: the established pattern is that a
key is supplied at creation time (`client add` prompts inline), so a `key add`
verb pair would exist nowhere else in the CLI.

### `gpudev ssh lockdown`

Runs the Lockdown step above, standalone. Refuses unless:

- `admin_ssh_key` is set in `host.json`, **and**
- that key is present in `authorized_keys`, **and**
- the proof gate below is satisfied

On refusal it says which condition failed and what to do. Idempotent: running
it on an already-locked-down host reports current state and changes nothing.

#### Proof gate (decided)

Stale evidence is never accepted — a past publickey login may have come from a
laptop that is gone, or with a key since removed from `authorized_keys`.

1. **This session is publickey-authenticated** → proceed immediately, no
   interaction. This is the happy path.
2. **Otherwise, wait for a fresh login.** Prompt, then watch the auth log for a
   publickey login that arrives *after the prompt was printed*:

   ```
   Cannot confirm a working key for this session.
   On your laptop, run:  ssh gpudev
   Waiting for a key-based login... (Ctrl-C to skip)
   ```

   Only a login observed after this moment counts. On timeout or Ctrl-C, skip
   lockdown and print the standard "run `gpudev ssh lockdown` later" message.

This gives case 1's guarantee with no interaction on the normal path, still
works from the console where no `SSH_CONNECTION` exists, and never accepts the
stale evidence that would let the gate pass while the operator is locked out.

`--force` skips the gate for scripted use. It must print exactly which checks
it is bypassing rather than proceeding silently.

### `gpudev ssh unlock`

Recovery. Re-enables `PasswordAuthentication yes` and restores port 22.
Intended to be run from the console after a lockout, or from a working session
before re-doing key setup. Prints a warning that the host is now
password-reachable.

### `gpudev ssh status`

Reports: effective port(s) actually listening, whether password auth is on,
whether `ssh.socket` is in play, whether `admin_ssh_key` is set and present in
`authorized_keys`.

---

## "Key is present" — one predicate everywhere (decided)

Match on the key's **type + blob** fields, ignoring any `command=` prefix and
any trailing comment. Never a whole-line match.

`gpudev-ssh-dispatch --install` rewrites the admin entry as:

```
command="<dispatcher>" ssh-ed25519 <blob> <comment>
```

A whole-line match can never hit once that wrapper is in place. The installer
used one, so a later run appended a second **unwrapped** copy of the same key;
sshd honours the first matching line, so a bare entry above the wrapped one
silently disabled the `ssh gpudev sleep|reboot` shortcuts. Fixed separately in
`a2e5efc` — this spec adopts the same predicate for the phase's own checks.

Consequences:

- **Dispatcher ordering is free.** Before or after lockdown, both correct.
  Install it during **Confirm**, before lockdown, so the wrapper is in place
  the first time the operator uses `ssh gpudev` — ergonomics, not correctness.
- Re-runs are idempotent rather than accumulating duplicates.
- The same predicate serves lockdown's precondition, the installer's append
  guard, and `gpudev ssh status`.

Note this does not clean up a duplicate that already exists on a host installed
before the fix. `gpudev ssh status` should report it, since the symptom
(shortcuts silently stop working) gives no other clue.

---

## Port and `ssh.socket` — must be resolved by this work

Ubuntu 23.04+ ships socket activation for SSH. When `ssh.socket` is enabled,
**`Port` in `sshd_config` is ignored** — the listening port comes from the
socket unit. `linux-setup.sh` has no handling for this today, which is
consistent with the observation that both `22` and `52100` answered on a real
install.

Lockdown must therefore:

1. detect socket activation — `systemctl is-enabled ssh.socket`
2. if enabled, either write a drop-in overriding `ListenStream`, or disable
   `ssh.socket` and enable `ssh.service`; pick one and do it consistently
3. verify the intended port is listening afterwards (`ss -ltn`) and report the
   truth, never the intent

Until this is settled, `LINUX-QUICKSTART.md` carries a `TODO` placeholder for
the port. Resolving it here removes that placeholder.

---

## Changes to existing code

| File | Change |
|---|---|
| `linux-setup.sh` | `ADMIN_SSH_KEY` becomes optional; drop it from `validate_required_values` |
| `linux-setup.sh` | `setup_host_ssh()` keeps `openssh-server` install and `authorized_keys` handling; **loses** the three sshd option writes |
| `linux-setup.sh` | new `admin_setup()` phase, called last in `main()` |
| `linux-setup.sh` | new `--no-lockdown` flag |
| `gpudev` | `cmd_ssh_lockdown` / `cmd_ssh_unlock` / `cmd_ssh_status` + dispatch |
| `LINUX-QUICKSTART.md` | rewrite steps 3–5 around the phase; remove the port TODO |

---

## Failure modes to handle

| Case | Behavior |
|---|---|
| `sshd -t` fails after edits | roll back the config, do not restart, report |
| new port not listening after restart | roll back to the previous config, report |
| operator answers `s` at every prompt | install completes, sshd untouched, clear next step printed |
| non-interactive run with no key | same as skip; never block an unattended install |
| `authorized_keys` contains several keys | list and choose; never guess |
| auth log unreadable (no permission/journal) | treat as "unproven", fall back to State B/C rather than failing |

---

## Decisions

1. **Proof gate** — this session if it is publickey-authenticated, otherwise
   wait for a *fresh* login observed after the prompt. Stale evidence is never
   accepted. `--force` retained, and must announce what it bypasses.
2. **Port** — a value, not a step. `HOST_SSH_PORT=22` is legal. Lockdown writes
   it to both `sshd_config` and the tunnel ingress so they cannot
   desynchronize. There is no "skip the port change" flag.
3. **Key presence** — one blob-based predicate everywhere, which makes
   dispatcher ordering free. Install it during Confirm for ergonomics.

## Still open

- **`ssh.socket` handling — RESOLVED.** Neither option was needed. Ubuntu's
  `openssh-server` ships `sshd-socket-generator`, which reads `Port` from
  `sshd_config` and regenerates the socket's `ListenStream` on every
  daemon-reload, so the config file is authoritative after all. Writing an
  `/etc` drop-in was actively wrong: the generator's `addresses.conf` sorts
  after a numeric prefix and its reset would win. Fixed in `a955740`; the port
  `TODO` in `LINUX-QUICKSTART.md` is gone.
- **Pre-existing duplicates — DECIDED: report only.** `gpudev ssh status` names
  the duplicate and the one command that fixes it
  (`gpudev-ssh-dispatch --install`, already idempotent and blob-based). It does
  not rewrite `authorized_keys` itself: a command called "status" that silently
  edits an auth file is a surprise that costs more trust than the keystroke
  saves, and `a2e5efc` stops new duplicates at the source.
