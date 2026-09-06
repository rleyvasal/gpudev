# Spec — detached admin operations

Status: IMPLEMENTED AND VERIFIED ON A LIVE HOST — all three phases, with 37
tests in `tests/test_jobs.py`.

Confirmed on hardware: a job survives the SSH session that started it, `flock`
serializes client mutations, `jobs logs`/`cancel` work against a live unit, the
deferred install skips Step 5 and prints its closing note, and two image builds
run concurrently to completion (base 15m 45s, cuda-dev 29m 45s, together in
29m 45s against ~45m serial).

Four bugs were found only by running it, never by the tests: jobs resolved
`gpudev` through PATH instead of the running script; a cancelled job left no
record; a detached launcher blocked on the lock it had just handed off; and the
serialization decision below was based on an unmeasured assumption.

Not yet exercised: a `client add` against the new images.
Scope: `gpudev` (`image build`, `client add`, `client rebuild`, `status`, new
`jobs`), `client-setup.sh` (locking, base-image message), `linux-setup.sh`
(defer the base image build; tmux hint), `README.md`, `LINUX-QUICKSTART.md`.

Let the slow admin operations survive a dropped connection and release the
administrator's terminal, without turning the fast ones into background jobs
whose output nobody reads.

---

## Problem

Admin commands run in the foreground of an SSH session that rides the
Cloudflare tunnel. Two consequences:

1. **A dropped connection kills the work.** A 25-minute `cuda-dev` build
   ([gpudev:160](gpudev:160)) dies with the session, leaving a partial image
   and a client that was never provisioned.
2. **The administrator waits.** Nothing else can happen in that terminal
   during a multi-GB download and build.

The second is a nuisance; the first is data loss in the middle of a mutation.

### Why `nohup` by default is the wrong fix

The obvious answer — background everything — breaks three things, and the
first is specific to this project rather than general.

**Output loss lands on exactly the wrong command.** `client add` prints the two
things the administrator must act on: the `%gpudev <name> --hostname <h>` line
to send back to the user, and the edge-probe verdict (`OK` versus
`NO SSH BANNER`). Detaching *every* `client add` puts both in a log that has to
be hunted for, in the one command whose purpose is to produce a line for a
human to forward.

That is an argument about the common case, which is fast. The slow case is
detached deliberately below, and pays for it by surfacing that output through
`status` and `jobs` instead of dropping it.

**Four commands read stdin** and would hang or take an unchosen branch:

| Command | Prompt | Line |
|---|---|---|
| `client add` | public key, when `--key` is absent | [gpudev:521](gpudev:521) |
| `client remove` | type the client name to confirm | [gpudev:641](gpudev:641) |
| `cloudflare token-set` | the API token | [gpudev:1345](gpudev:1345) |
| `reset` / `uninstall` | destructive confirmation | [gpudev:2380](gpudev:2380) |

**Concurrency stops being hypothetical.** There is no locking in the codebase —
no `flock`, nothing guarding `clients.json`, the ingress `config.yml`, or the
tunnel reload. Serialization today is *accidental*: it holds because the
administrator waits. Removing the wait removes the guarantee.

---

## Mechanism: `systemd-run --user`, not `nohup`

Both survive a disconnect. Only one gives back the state the administrator
needs afterwards, and only one is already in this codebase.

| | `nohup` | `systemd-run --user` |
|---|---|---|
| Survives disconnect | yes | yes |
| Output | a file to name, rotate and clean up | journal, automatic |
| Exit status, after the fact | lost | `systemctl --user status` |
| Cancel | `kill` a PID you saved somewhere | `systemctl --user stop <unit>` |
| Already used here | no | yes — `gpudev power` |

Two facts make this nearly free:

- **Lingering is already enabled.** `linux-setup.sh` runs
  `loginctl enable-linger` ([linux-setup.sh:1764](linux-setup.sh:1764)), which
  is the prerequisite for user units to keep running after the last session
  closes. Without it, `nohup` is the shakier option anyway.
- **The pattern exists.** `power sleep 60m` already schedules through
  `systemd-run --user --unit … --collect` ([gpudev:2112](gpudev:2112)), and
  `power status` / `power cancel` already enumerate and stop those units by
  name prefix. Detached jobs reuse that shape rather than inventing one.

---

## Design

### Detaching follows duration, not command

The rule is one line: **anything that will take minutes runs detached; anything
that takes seconds does not.** For most commands that is a flag, because only
the operator knows whether they want to wait. For `client add` it is decided
automatically, because the command already knows whether a build is required.

| Operation | Typical | Detached |
|---|---|---|
| `image build cuda-dev` | ~25 min, several GB | **yes** |
| `client rebuild --all` | minutes × clients | **yes** |
| `client add` (image present, any variant) | seconds | no — and it must stay that way |
| `client add --variant cuda-dev` (image missing) | ~25 min | **automatic** — see below |
| `client remove`, `client info`, `status`, `ssh …` | seconds | no |
| anything that prompts | — | **never** |

Refuse `--detach` on a command that would prompt, naming the flag that makes it
non-interactive (`--key`, `--yes`). Silently hanging on stdin in the background
is the worst available outcome.

### Unit naming

`gpudev-job-<verb>-<target>-<epoch>`, mirroring `gpudev-power-<action>-<epoch>`
so one convention covers both and `jobs` can enumerate by prefix exactly as
`power_timer_units` does.

### `gpudev jobs`

```
gpudev jobs                 running and recently finished
gpudev jobs logs <unit>     journalctl --user -u <unit>
gpudev jobs cancel <unit>   systemctl --user stop <unit>
```

Finished units stay visible until the next `jobs` run reaps them, so a job that
completed while the administrator was away is still reportable. `--collect`
alone would drop the record at exit, which is the wrong trade here.

### The administrator finds out by coming back

`gpudev status` gains a **Jobs** section: running jobs, and any that finished
since the last look, with their exit status.

This costs nothing to reach. The dashboard already auto-runs on interactive SSH
login via the `~/.bashrc` hook, so reconnecting *is* the notification, and
`ssh gpudev status` already works as a one-liner from the laptop.

### `client add` detaches itself, but only when it will be slow

Cost is not uniform across variants, so the behaviour should not be either:

| Request | Image state | Duration |
|---|---|---|
| `client add <name>` | `gpudev-base` present | seconds |
| `client add <name> --variant cuda-dev` | image present | seconds |
| `client add <name> --variant cuda-dev` | **image missing** | **~25 min** |

(With the base image deferred, `gpudev-base` can also be missing on a
just-installed host. Same rule applies — the difference is only which image
is built.)

Only the last row is a problem, and today it silently converts a ten-second
command into a twenty-five-minute one with no warning
([gpudev:160](gpudev:160)).

**Behaviour:** when the requested variant's image is missing, `client add`
re-runs *itself* under `systemd-run --user` — build and provisioning together —
and returns immediately:

```
cuda-dev is not built yet, so this will take about 25 minutes.
Running it in the background — safe to disconnect.

  Job:  gpudev-job-add-alice-1789…

Check `gpudev status` when you reconnect; it will show the job's
result and the %gpudev line to send back to alice.
```

Nothing is printed for the operator to re-run, and nothing is left half-done.
The `%gpudev <name> --hostname <h>` line and the edge-probe verdict land in the
journal and are surfaced by `status` and `jobs`.

#### Why this is cheap, contrary to first appearances

It looks like the command must survive its own detachment and resume mid-way.
It does not: `systemd-run` launches a **fresh** `gpudev client add …` with the
same arguments, and a `GPUDEV_IN_JOB=1` guard stops that copy detaching again.
A wrapper and a recursion guard, not re-entrancy.

Two existing properties make it safe:

- **All input is gathered before the slow step.** In `cmd_client_add` the key
  prompt and validation both precede `ensure_client_variant_image`, so at the
  moment we learn the operation is slow there is nothing left to ask. A pasted
  key is re-passed to the detached copy; a prompted one is passed as `--key`.
- **A public key on a command line is not a secret.** Already decided and
  documented in `SPEC-client-onboarding.md` — it is the half designed to be
  published — so passing it to `systemd-run` introduces no new exposure.

`--wait` forces the foreground path for anyone who wants one blocking command,
and `--detach` forces the background path even when the image is present.

### The install — same want, three constraints `client add` does not have

`linux-setup.sh` is the longest operation in the project (a cold base image
build measured at ~13 minutes plus multi-GB downloads), so it is the most
painful thing to lose to a dropped connection. It does not follow that it
detaches the same way.

**1. It is interactive at three points, and one cannot be automated away.**

| Point | Line | Escapable? |
|---|---|---|
| Cloudflare domain | [linux-setup.sh:210](linux-setup.sh:210) | yes — `CF_DOMAIN=` |
| all prompts | [linux-setup.sh:206](linux-setup.sh:206) | yes — `NON_INTERACTIVE=true` |
| `cloudflared tunnel login` | [linux-setup.sh:1475](linux-setup.sh:1475) | **only by doing it first** — it prints a URL a human must open in a browser |
| Step 11 admin setup / lockdown | [linux-setup.sh:1343](linux-setup.sh:1343) | yes — `--no-lockdown` |

**2. Lingering is not yet enabled when the long part runs.**
`loginctl enable-linger` is called at [linux-setup.sh:1764](linux-setup.sh:1764)
— inside Step 10, *after* the build. So `systemd-run --user` cannot be assumed
to survive logout during the phase that most needs it. A detached install must
enable lingering itself, before launching.

**3. Detached sudo has already bitten this project.** The installer needs sudo
throughout (apt, `/etc` writes), and a detached process has **no TTY**, so it
can only use `sudo -n` — the exact constraint documented at
[gpudev:427](gpudev:427) for the tunnel reload, which had to be solved with a
narrow NOPASSWD grant. That approach does not generalise: NOPASSWD for
apt and arbitrary `/etc` writes is not a grant worth making. A sudo timestamp
also expires mid-run on a 25-minute install.

#### What this means

Detaching the *whole* installer runs into constraint 3 head-on. But the
constraint applies to the installer, not to the expensive work inside it — and
those can be separated.

#### Better: move the base image build past the re-login boundary

The single longest phase is Step 5, the base image build. It is also the phase
with the **least** need for sudo — and whether it needs sudo at all depends
entirely on *when* it runs.

`docker_probe` ([linux-setup.sh:347](linux-setup.sh:347)) picks between `docker`
and `sudo docker`, and its own comment says why:

> On a FIRST install the group membership is not active in this shell yet
> (`configure_docker_group` sets `NEED_DOCKER_RELOGIN`), so the plain probe
> fails and the sudo path below still carries the install.

| When the build runs | Docker access | Detachable? |
|---|---|---|
| inside the install, first run | `sudo docker` — group not yet active | no: needs a TTY for sudo |
| after the install, fresh login | plain `docker` — group active | **yes** |

So deferring the build to after the first reconnect removes the sudo problem
rather than working around it. Lingering is already on by then too — Step 10
([linux-setup.sh:1764](linux-setup.sh:1764)) runs before the install ends — so
constraint 2 dissolves as well.

**The operator has to reconnect regardless.** Lockdown moves the SSH port, and
the docker group needs a fresh session. That mandatory reconnect is the natural
seam.

Resulting shape:

1. `linux-setup.sh` completes everything except the base image, ending with
   Step 11 so SSH is settled while the operator is present.
2. It closes by saying the host is installed, the image is not built yet, and
   to reconnect.
3. On reconnect the dashboard — which already auto-runs on interactive login —
   reports the missing image and offers `gpudev image build base --detach`.
4. That build detaches cleanly: plain `docker`, no sudo, lingering on.

The cost is honest and must be stated in the closing message: **the host is not
usable until that build finishes.** Two things must change to keep that from
being confusing:

- `require_host_setup` fails today with "Base image not found. Run
  linux-setup.sh first." ([client-setup.sh:108](client-setup.sh:108)). That
  becomes actively wrong advice — the install *did* run. It must distinguish
  "never installed" from "installed, image not built yet" and name the build
  command in the second case.
- Step 5b (verify torch CUDA) depends on the image, so it moves into the build
  job rather than staying in the installer.

**Decided: deferred by default**, with `--build-base-image` forcing the old
inline behaviour. The remote-operator case is the one this project is built
around, and deferring lets lockdown finish while the operator is present rather
than competing with a 13-minute build.

#### Prewarming is the administrator's job, and it moves the wait off the user

Deferring the base image creates the right habit for a second reason: **the
person who should absorb a build is the administrator, not a notebook user
waiting to be onboarded.**

Today the wait can land in the worst place. A user forwards
`gpudev client add alice --variant cuda-dev --key "…"`, the administrator runs
it, and if that image was never built the user waits twenty-five minutes for
the `%gpudev` line that completes their setup — for a cost that had nothing to
do with them and could have been paid at any earlier idle moment.

So the post-install sequence becomes an explicit part of the guides:

```bash
gpudev image build base --detach       # required — the host is not usable without it
gpudev image build cuda-dev --detach   # optional, but do it now if profiling clients are coming
gpudev image list                      # confirm both are ready before onboarding anyone
```

With both images warm, **every `client add` is seconds**, the detached path in
the previous section never triggers, and onboarding is bounded by how fast two
humans exchange one line each.

That does not make the `client add` auto-detach redundant — someone will
eventually request a variant nobody prewarmed, and it must not lock up their
terminal for half an hour. It demotes it from expected path to safety net,
which is the right status for a behaviour that surprises people.

**Builds run in parallel — the opposite of what this spec first said.**

The original claim was that two concurrent builds contend for disk, CPU and the
layer cache with no benefit. It was asserted, never measured, and measurement
refuted it on every count:

| | One build | Two builds |
|---|---|---|
| CPU (24 cores) | 0.2% | still 99.8% idle |
| Network | 58 Mbps | **92 Mbps** of 1 Gbps |
| Disk write | 31 MB/s | NVMe does 1000+ |
| RAM | 1 GB | 1 GB of 29 GB |

They do not split a fixed resource; aggregate throughput rose ~1.6×, because
the work is upstream download latency. Confirmed by a real run: base 15m 45s,
cuda-dev 29m 45s, both finishing in **29m 45s** against ~45m serial — 35% saved.

**The lock was protecting something real, just not that.** Both builds call
`write_base_requirements`, and `cat > file` truncates before writing, so one
could read `requirements-torch.in` or `requirements-base.txt` mid-rewrite.
Identical content, so the failure would have been rare and baffling rather than
obvious. Those writes are now temp + rename, which removes the hazard without
removing the concurrency.

**The real cost of parallelism** is cache reuse, not contention. Both Dockerfiles
mount `type=cache,target=/root/.cache/uv`, so a *sequential* second build reads
the first's downloads. Run together, the ~21 non-torch packages they share may
be fetched twice — roughly 200–300 MB. The dominant wheels are not shared at
all: base installs torch cu130, cuda-dev pins cu128 to match its toolkit. On a
metered link, serial is the better trade.

Client mutations still serialize: they walk `clients.json`, the ingress and the
connector, which genuinely do not tolerate two writers.

**Ship first, because it is nearly free and needs no new mechanism:**

- **Say the install is resumable.** It already is, and nowhere says so:
  Docker ([linux-setup.sh:297](linux-setup.sh:297)), the NVIDIA toolkit
  ([:413](linux-setup.sh:413)), cloudflared ([:1122](linux-setup.sh:1122)) and
  the tunnel ([:1486](linux-setup.sh:1486)) all early-return when present, the
  ML lock is reused when unchanged (`ml_lock_is_current`), and Docker's layer
  cache makes the rebuild seconds rather than minutes. **Re-running after a
  dropped connection is cheap** — an operator who does not know that will
  reasonably fear starting over.
- **Run it under `tmux`.** One line in the guide. It survives the disconnect
  *and* keeps the three interactive points working, which no detached form
  does. `linux-setup.sh` should print a one-line hint when it detects it is not
  under `tmux`/`screen` — before the thirteen minutes, not after.

**Then:** defer the base image build past the reconnect, as above. That is the
change worth making — it removes the longest wait without detaching anything
that needs sudo or a browser, and lockdown stays foreground where it belongs.
Detaching the step that moves the SSH port and disables password auth, with
nobody present to satisfy its proof gate, is how an operator gets locked out of
the machine they just built.

### Locking, which `--detach` makes mandatory

A single lock (`flock` on `~/.config/gpudev/.lock`) around the mutating
sequence: `clients.json` write → ingress edit → tunnel reload. Image builds
**Image builds do not take it** — see the measurements above; they contend for
nothing, and the files they share are written atomically instead.

Not optional alongside this work. Two concurrent `client add`s can today
interleave a read-modify-write of `clients.json`
([client-setup.sh:145](client-setup.sh:145)) and two ingress rewrites of the
same `config.yml`; the only thing preventing it is that nobody has run two at
once. A corrupted `clients.json` costs far more than the wait it replaces.

Read-only commands (`status`, `client list`, `client info`) do not take the
lock.

---

## Why not email

Considered and deferred. It needs an MTA or SMTP credentials on a headless box:
new setup, a new secret to manage, and a new silent-failure mode — the classic
one being that nobody notices the notifications stopped.

The dashboard-on-login path above delivers the same information at zero
infrastructure cost, because the administrator has to reconnect anyway to act
on the result. Revisit only if that proves insufficient in practice.

---

## Changes to existing code

| File | Change |
|---|---|
| `gpudev` | `run_detached()` helper wrapping `systemd-run --user`, mirroring the power scheduler |
| `gpudev` | `--detach` / `--wait` on `image build`, `client rebuild`; refuse `--detach` when a prompt would follow |
| `gpudev` | `cmd_jobs` — list, logs, cancel; enumerate by unit prefix as `power_timer_units` does |
| `gpudev` | `cmd_status` — a Jobs section |
| `gpudev` | `ensure_client_variant_image`: start the build detached and stop, rather than blocking `client add` |
| `gpudev`, `client-setup.sh` | `flock` around clients.json + ingress + reload; read-only paths exempt |
| `gpudev` | `usage()` — `jobs`, and the new flags (the CLI-surface test enforces this) |
| `linux-setup.sh` | defer the base image build; Step 5b moves with it; closing message says the host is not usable until it finishes |
| `client-setup.sh` | `require_host_setup`: separate "never installed" from "image not built yet", and name the build command |
| `linux-setup.sh` | one-line hint when not running under tmux/screen |
| `README.md`, `LINUX-QUICKSTART.md` | the post-install prewarm sequence; the detached build flow; that a dropped session no longer loses work; that re-running the install is cheap |

## Failure modes

| Case | Behavior |
|---|---|
| connection drops mid-build | unit keeps running under lingering; `gpudev jobs` shows it on return |
| `--detach` on a command that would prompt | refuse, naming the flag that makes it non-interactive |
| `systemd-run` unavailable (no user manager) | refuse `--detach` and say why; foreground still works |
| lingering disabled after install | detached jobs die at logout — `jobs` reports the unit vanished rather than claiming success |
| two mutating commands at once | second blocks on the lock; neither corrupts `clients.json` |
| lock held by a dead process | `flock` releases on process exit; no stale-lock recovery needed |
| job fails while nobody is watching | non-zero exit retained; surfaced by `status` on next login |
| two image builds started at once | both run; the shared requirements files are written atomically |
| `client add` before the base image is built | refuse with the build command, not "run linux-setup.sh first" |
| `client add` for an unbuilt variant | the whole command re-runs detached; operator is told it is safe to disconnect |
| detached copy re-enters `client add` | `GPUDEV_IN_JOB=1` stops it detaching again |
| `--wait` and `--detach` both given | refuse; they are contradictory |

## Decisions

1. **Duration decides, not the command.** `--detach` is opt-in wherever only the
   operator knows whether they want to wait; it is automatic where the command
   can tell in advance that the work takes minutes. Detaching everything would
   background the majority, which finish in seconds and print output a human
   must read.
2. **`systemd-run --user`, not `nohup`.** Lingering is already on, the pattern
   already exists in `gpudev power`, and it keeps output and exit status.
3. **Notification is the dashboard, not email.** Reconnecting is already
   required to act on the result, and the login hook already runs `status`.
4. **Locking ships with this, not after.** Detaching removes the accidental
   serialization that currently protects `clients.json` and the ingress file.
5. **The install defers its base image build rather than detaching itself.**
   Moving that phase past the mandatory reconnect turns `sudo docker` into
   plain `docker`, which is what makes it detachable at all; the rest of the
   installer keeps its TTY and is covered by `tmux`.
6. **Image builds are concurrent; client mutations are not.** The claim that
   builds contend was asserted without measurement and proved false — two
   builds raised throughput 58→92 Mbps with the CPU 99.8% idle, finishing in
   30 minutes against 45 serial. What the lock was actually protecting, a
   truncating rewrite of two shared requirements files, is fixed by writing
   them atomically.
7. **Images are prewarmed by the administrator after install.** A build should
   be paid at an idle moment by the person running the host, never by a user
   waiting on the `%gpudev` line that finishes their onboarding. This makes the
   `client add` auto-detach a safety net rather than the expected path.
8. **A finished job's result is a file, not systemd unit state.** What the
   operator needs on return is a value — the `%gpudev` line — and a structured
   value should not have to be grepped back out of log text. Files also survive
   a reboot or user-manager restart, which transient units do not; `--collect`
   then lets systemd clean up normally. The journal keeps the full logs.
9. **Detaching the installer itself is rejected.** Deferring the base image is
   why it no longer matters: what remains in `linux-setup.sh` is apt, `/etc`
   writes and three interactive points, all of which want a TTY. `tmux` covers
   a dropped connection there.
10. **`client add` detaches itself exactly when it will be slow** — a missing
   variant image, nothing else. It does not print a command to re-run: the
   detached copy does the whole job, and `status` reports the result. The fast
   path, which is every other `client add`, is untouched.

## Open

- **Pruning `~/.config/gpudev/jobs/`.** Result files are small and never
  removed today. A cap (keep the last N, or drop anything older than a month)
  is worth adding before this has been running for a year, but it is not
  urgent and the right N is easier to pick with real usage.
