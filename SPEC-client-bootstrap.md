# Spec — client bootstrap

Status: IMPLEMENTED — `client-bootstrap.sh`, the `%run` entry point, `--domain`,
the version stamp and the drift warning are in place, with 24 tests across
`tests/test_client_bootstrap.py` and `tests/test_version_drift.py`. Exercised
against the live GitHub endpoint (install, no-op re-run, `--verify`, `--force`,
truncation, pinning to an older SHA); a container has not yet been rebuilt with
a stamp, so the drift warning has run only against stubs.
Scope: new `client-bootstrap.sh`, `CRAFT.py`, `gpudev_craft/`, `gpudev`
(`client invite` output), `README.md`, `LINUX-QUICKSTART.md`, `CRAFT_DIALOG.md`.

Deliver the client's runtime with a small, named script instead of a repository
clone, and collapse first-run setup into one `%run` line.

---

## Problem

The client bootstrap is a 200-character `git` one-liner pasted into a notebook
cell. It works, but it ships roughly five times what it runs, depends on `git`,
hardcodes a SolveIt-specific path in four places, and cannot be read at a
glance.

Measured on this repository:

| | On the wire | On disk |
|---|---|---|
| Original clone | ~8.9 MB | 8.9 MB |
| `--depth 1` clone (current) | ~350 KB | 1.1 MB |
| Full repo tarball | 214 KB | — |
| Client subtree tarball | 51 KB | — |
| **What the client actually runs** | — | **192 KB** |

The excess is history plus host-side code the client can never execute:
`linux-setup.sh`, `windows-setup.ps1`, `client-setup.sh`, the `gpudev` CLI,
`tests/`, and the specs.

### The code is not the problem

`gpudev_craft/core.py` is 2,519 lines, and all of it is client runtime:

```
    62  Notebook-local client selection    681  Output Display
   264  Helpers                            446  Remote Execution Manager
    51  Cloudflared                        121  Package transform registry
   243  Kernel Management                  129  Mode Router
   407  Magics                              43  remote_run_
```

There is no host-side code to strip out. **Delivery is the only thing this spec
changes.** Shrinking the runtime itself would be a refactor, not a packaging
change, and is out of scope.

### Two incidental findings

1. **`curl` is already a hard client dependency.** `install_cloudflared()`
   shells out to it (`core.py`). So a curl-based bootstrap removes the `git`
   dependency and adds nothing.
2. **Every client clone carries three dangling symlinks.** `addons/plot3`,
   `addons/sslive` and `addons/tidy3` are mode-120000 entries pointing outside
   the repository; `plot3` points at an absolute path in the author's home
   directory. A file-manifest fetch drops them for free.

---

## What a client-side script can and cannot do

This is the boundary that shapes the whole design, so it is stated before the
design rather than discovered inside it.

**It can:** fetch the runtime, load CRAFT, install `cloudflared`, generate the
client keypair, and write the `~/.ssh/config` stanza.

**It cannot: enroll the client's public key on the host.** The client holds no
credential on the host — that is the Roles separation, not an oversight. The
private key never leaves the notebook, so the public key must cross to the host
through some channel, and that channel is a person forwarding one line.

Automating it would require a host-side enrollment endpoint reachable by an
unauthenticated client: a new listener, new attack surface, and a token
lifecycle, to save a single paste. **Rejected.** The round trip stays.

### The hostname, however, is avoidable

The stanza needs two things: the key enrolled (irreducible, above) and the
**domain**. The domain is not a secret — `<name>.<domain>` is public DNS, and
`gpudev client invite` already prints it to users.

The domain is the *only* reason `--hostname` appears at the end of the flow
today. If the bootstrap is told the domain, the stanza is written during step 1
and the final step is a bare `%gpu <name>`.

**Decision: the domain becomes a normal argument to setup, not an admin-first
special case.** An administrator tells a user the domain once; every client
afterwards is self-service.

---

## The client manifest

Ten files, 192 KB. This list is the single source of truth and lives in
`client-bootstrap.sh`.

```
CRAFT.py
gpudev_craft/__init__.py
gpudev_craft/core.py
gpudev_craft/client_setup.py
gpudev_craft/magics.py
addons/*.py                 # mojo, pcviz, plot3, sslive, tidy3
```

`addons/*.py` is matched by glob, deliberately: the addon loaders are thin and
new ones should not require a bootstrap change. The `addons/` **symlinks** are
excluded by matching `*.py` only.

---

## Design

### Transport

One request to GitHub's tarball endpoint, extracting only the manifest:

```
curl -fsSL https://api.github.com/repos/rleyvasal/gpudev/tarball/<ref>
  | tar xz --strip-components=1 --wildcards '*/CRAFT.py' '*/gpudev_craft/*' '*/addons/*.py'
```

Verified: yields exactly the ten files and 192 KB, no symlinks.

`--wildcards` is GNU tar. SolveIt is Linux, so this is correct there; the script
detects BSD tar (which globs by default) and drops the flag, so it also runs on
macOS for local testing.

### Staying current, cheaply

The cell **is** the update mechanism, so re-running it must both pick up `main`
and cost almost nothing when there is nothing to pick up.

Every run resolves the ref before deciding to do anything:

```
resolved=$(curl -fsSL -H "Accept: application/vnd.github.sha" \
             https://api.github.com/repos/rleyvasal/gpudev/commits/${GPUDEV_REF})

if [ "$resolved" = "$(sha_from "$GPUDEV_DIR/VERSION")" ] && [ -z "$FORCE" ]; then
    echo "Already at ${resolved%????????????????????????????????} (${GPUDEV_REF}). Nothing to do."
    exit 0
fi
```

Measured, that request is **40 bytes** against a **214,252-byte** tarball —
5,356× smaller. So an up-to-date client pays 40 bytes and does not touch a
working install; only an actually-changed `main` triggers a fetch and swap.

`--force` re-fetches regardless. Its main use pairs with layer 4 below: when
`--verify` reports local corruption, `--force` repairs it without needing a new
upstream commit.

#### Bounded staleness

Both endpoints are CDN-cached, measured:

| Request | `cache-control` | Worst-case staleness |
|---|---|---|
| `raw.githubusercontent.com/.../client-bootstrap.sh` | `max-age=300` | 5 minutes |
| `api.github.com/.../commits/<ref>` | `public, max-age=60` | 60 seconds |

So line 1 of the cell can fetch a script up to five minutes old, and the SHA
resolution can be up to a minute behind. Both are bounded and harmless — worth
stating precisely rather than claiming the cell is instantaneous with `main`.

### Download verification

Four layers, each catching something the others do not. All were tested against
the live endpoint rather than assumed.

#### 1. Transport integrity — check `tar`'s exit status

Gzip carries a CRC, so a truncated or corrupted tarball fails at extraction.
Measured, cutting a 214,252-byte tarball to 150,000 bytes:

```
rleyvasal-gpudev-a08cbc0/gpudev_craft/core.py: truncated gzip input
tar: Error exit delayed from previous errors.
exit code: 1
files extracted: 22
```

Two things matter here. The corruption **is** detected — for free, by the
transport. And tar still wrote **22 partial files before failing**, including a
truncated `core.py`.

So the requirement is not a new check; it is **never ignoring tar's exit
status**, and extracting into `.new` so those 22 partial files are discarded
rather than swapped in. This is the concrete reason the atomic swap below is
mandatory rather than defensive.

#### 2. Identity — resolve the ref, then verify the tree

One cheap request resolves a ref to a full SHA:

```
curl -fsSL -H "Accept: application/vnd.github.sha" \
  https://api.github.com/repos/rleyvasal/gpudev/commits/${GPUDEV_REF}
→ a08cbc08ef719987e1b802015322371d5926614a
```

GitHub names the tarball's top-level directory `rleyvasal-gpudev-<short sha>`:

```
rleyvasal-gpudev-a08cbc0/
```

Verifying that directory against the resolved SHA proves the archive is the tree
that was asked for. This is what catches a **stale CDN response** or a ref that
moved between the two requests — neither of which corrupts anything, so neither
is visible to layer 1.

#### 3. Completeness — the manifest must be fully satisfied

After extraction, every file in the manifest must exist and be non-empty.

Layer 1 only proves the archive arrived intact; it says nothing about the
archive *containing what this script needs*. An upstream rename or move would
extract cleanly, exit 0, and leave a half-installed tree. This check turns that
into a clear failure before the swap.

#### 4. Post-install self-check

`VERSION` records the resolved SHA plus a `sha256` per installed file.
`client-bootstrap.sh --verify` re-hashes an existing install against it.

This answers a question support cannot otherwise answer — *is this install
intact, or has something been edited or truncated since?* — without a network
round trip.

#### Rejected: a syntax check

The obvious reach is `python3 -m py_compile` on the extracted files. **Tested,
and it does not work.** `core.py` truncated from 89,122 bytes to 40,000 —
mid-file, at an arbitrary byte offset — compiles cleanly, as does a cut at a
top-level statement boundary.

Python source is a sequence of complete top-level statements, so cutting at a
random offset usually lands after one. The file imports, and the missing half
simply is not there. A syntax check gives the *appearance* of an integrity check
while catching almost nothing; layers 1 and 4 are definitive where it is not.

#### What this does not defend against

A compromised upstream repository. The code and any checksum published beside it
share one trust root, so verification cannot bootstrap trust it does not already
have — and `client-bootstrap.sh` itself arrives over the same channel.

Stated rather than implied: these layers defend against **truncation,
corruption, a stale CDN, a wrong or moved ref, an incomplete manifest, and
post-install drift.** They are not a supply-chain control.

### Atomic swap

Extract into `<dir>.new`, then swap:

```
rm -rf <dir>.old
mv <dir> <dir>.old        # if it exists
mv <dir>.new <dir>
rm -rf <dir>.old
```

A clone gets atomicity free from git; a tarball extract does not. Without this,
an interrupted update leaves a truncated `core.py` that may still import and
then misbehave — the worst failure mode available, because it looks like a bug
in gpudev rather than a bad download.

The swap is per-directory rename, so a reader mid-import either sees the whole
old tree or the whole new one.

### Path override

```
GPUDEV_DIR=${GPUDEV_DIR:-/app/data/gpudevd/gpudev}
```

`/app/data/gpudevd/gpudev` is SolveIt's persistent storage and stays the
default, so no existing instruction changes. It is currently hardcoded in four
places (`gpudev`, `README.md`, `LINUX-QUICKSTART.md`,
`SPEC-client-onboarding.md`); after this change the default lives in the script
and the docs reference the script.

The script prints the resolved directory, so a non-default install is never
silent.

#### The override is not enough on its own

Making `GPUDEV_DIR` configurable in the script leaves the cell's **second** line
hardcoded, and the two can silently disagree — set the variable, forget line 2,
and `%run` loads a stale install or fails.

The obvious repair does not work. Measured against a real IPython:

| `%run` argument | Result |
|---|---|
| `$GPUDEV_DIR/CRAFT.py` (shell env var) | **fails** — ``File `'$GPUDEV_DIR/CRAFT.py'` not found`` |
| `{GPUDEV_DIR}/CRAFT.py` (brace, env var) | **fails** |
| `$gd/CRAFT.py` (Python variable) | works |
| `{gd}/CRAFT.py` (brace, Python variable) | works |

`%run` expands from the **Python namespace**, not the environment. And line 1 is
a `!` shell escape, which cannot set a Python variable for line 2 to read. So
there is no expression line 2 can contain that follows `GPUDEV_DIR`.

#### Stable entry point

The bootstrap points `~/.gpudev-client` at whatever `GPUDEV_DIR` resolves to,
and every published cell says `~/.gpudev-client/CRAFT.py`. Verified: `%run`
expands `~`, and because `__file__.resolve()` follows the link, `_GPUDEV_ROOT`,
`_ADDONS` and `VERSION_FILE` all land on the real directory — addons load
through the symlink unchanged.

Two consequences beyond the configurability fix:

- The cell carries **no SolveIt-specific path**, so it works unchanged on a
  local Jupyter or anywhere `/app/data` does not exist. That is the case
  `GPUDEV_DIR` was for, and it now needs no second edit.
- Old cells keep working. The install still defaults to
  `/app/data/gpudevd/gpudev`, so a previously pasted literal path resolves to
  the same tree.

If `~/.gpudev-client` exists and is **not** a symlink, it is left untouched and
the literal path is printed instead — the script must never replace a directory
it did not create. A missing or unwritable `$HOME` degrades the same way.

### Version stamp

The script writes `<dir>/VERSION` containing the resolved commit SHA and the
fetch timestamp, and prints both.

This is not bookkeeping for its own sake — see the next two sections.

### Ref override

```
GPUDEV_REF=${GPUDEV_REF:-main}
```

The ref goes into the tarball URL, so it accepts a branch, a tag, or a commit
SHA. The default is `main`, which is exactly today's behaviour — **nothing
changes for anyone unless they set it.**

#### Why this is needed at this project's cadence

Measured on this repository:

```
tags                                                 0
commits on main                                    157
commits in the last 30 days                        100
commits touching gpudev_craft/ or CRAFT.py
  in the last 30 days                               18
branches                                          main
```

Roughly three commits a day, straight to `main`, with no staging and no tags.
The client runtime changes about every other day.

Every client tracks `main`, and the bootstrap cell **is** the update mechanism —
the guides tell users to re-run it to pick up fixes. So a re-run lands whatever
was committed minutes earlier, including a commit made mid-debugging.

This is not hypothetical here. `main` contains `e3c787f` ("Revert the two
wake-on-LAN fixes that addressed the wrong cause") and `e088ea7` ("Fix four
faults a full install run exposed"). A client re-running the cell between a bad
commit and its revert picks the bad commit up, and today has no way back.

#### It only pays off together with `VERSION`

Alone, `VERSION` is a diagnostic that can be read but not acted on: it names the
SHA you are running and stops there. With a ref override it becomes actionable —
read the SHA from a session that worked, pin to it, keep working. Neither half
is worth much without the other, which is why both land in this change.

A secondary effect: tracking `main` by default actively manufactures the drift
described in the next section. Two users who last re-ran the cell a month apart
are on different client versions against the same containers.

#### What is deliberately *not* included

Two separable things get conflated under "pinning":

| | What it is | Cost |
|---|---|---|
| **the ability to pin** | `GPUDEV_REF`, defaulting to `main` | ~3 lines |
| **a release process** | cutting tags, deciding when users move, announcing it | ongoing discipline this repo does not have |

Only the first is in scope. The second has its own failure mode — a client
pinned to a stale tag silently stops receiving fixes, which is quieter than
surprise breakage and can be worse. Real tags are a decision for when there are
users the maintainer does not personally know; by then the mechanism exists and
is tested.

`GPUDEV_REF` is therefore documented as a **recovery lever**, not as routine
usage: *if an update breaks you, pin to the SHA in your `VERSION` file.*

---

## Version drift is currently undetectable

The client and the container are updated by **different people through
different mechanisms**:

| Half | File | Updated by |
|---|---|---|
| client | `gpudev_craft/core.py` | the user, re-running the bootstrap |
| container | `/home/gpudev/bin/kernel-manager.sh` | the admin, via `client rebuild` / `self-update` |

They communicate over a fixed contract: `core.py` invokes
`/home/gpudev/bin/kernel-manager.sh start|restart|doctor` over SSH.

**There is no `__version__` anywhere in the project**, on either side. A
mismatch surfaces as a confusing runtime failure with no hint that the two
halves disagree — the "it worked yesterday" class of report.

Since the bootstrap must stamp a version anyway to report what it installed,
closing this is nearly free:

1. `client-bootstrap.sh` writes `<dir>/VERSION` (commit SHA).
2. `kernel-manager.sh` gains a `version` subcommand reporting the SHA it was
   built from; `client-setup.sh` stamps it at container-build time.
3. `%gpu` compares them **once per kernel session** and warns on mismatch,
   naming the fix (`gpudev client rebuild <name>` for the host half, re-running
   the bootstrap cell for the client half).

A warning, never a refusal. The contract is small and usually compatible across
versions; blocking a working session over a version string would cost more than
the drift does.

---

## The cell

Two lines. Two is the floor: a cold client must fetch something before it can
run it.

```text
!curl -fsSL https://raw.githubusercontent.com/rleyvasal/gpudev/main/client-bootstrap.sh -o /tmp/gb.sh && sh /tmp/gb.sh
%run ~/.gpudev-client/CRAFT.py alice --domain example.com
```

### Why `CRAFT.py` takes the arguments

`%run script.py args` populates `sys.argv`, and `CRAFT.py` is already the `%run`
entry point. Giving it optional arguments lets one line **load the magics and
run setup**.

The alternative — having `client-bootstrap.sh` do the keygen — would mean a
second implementation of `ensure_client_key` in shell, diverging from the tested
Python one. The script therefore does exactly one job: fetch and swap files.
Everything else stays where it already lives.

`%run CRAFT.py` with no arguments keeps its current behaviour exactly, so every
existing notebook and dialog cell is unaffected.

### Why `-o` and not `curl | sh`

Identical bytes either way, but `-o` leaves the script on disk where it can be
read after the fact.

### Resulting flow

| | Who | Action |
|---|---|---|
| **1** | user | the two-line cell — key, stanza, and the admin's command printed |
| **2** | ↔ admin | forwards that command; the admin runs it |
| **3** | user | `%gpu alice` |

---

## Changes to existing code

| File | Change |
|---|---|
| `client-bootstrap.sh` | **new** — manifest, SHA short-circuit, fetch, verification, atomic swap, `GPUDEV_DIR`, `GPUDEV_REF`, `VERSION`, `--verify`, `--force` |
| `CRAFT.py` | optional `sys.argv`: `<name> [--domain d] [--variant v]` → `gpu_setup` |
| `core.py` | `_parse_gpu_setup_args`: accept `--domain` alongside `--hostname` |
| `core.py` | `gpu`: warn once per session on client/container version mismatch |
| `kernel-manager.sh` | `version` subcommand |
| `client-setup.sh` | stamp the container's version at build time |
| `gpudev` | `client invite`: emit the two-line cell; fix the stale "Step N of 4" text |
| `README.md`, `LINUX-QUICKSTART.md`, `CRAFT_DIALOG.md`, `SPEC-client-onboarding.md` | the two-line cell; `--domain` as the normal path |

## Failure modes

| Case | Behavior |
|---|---|
| network failure mid-fetch | `.new` discarded, existing install untouched |
| truncated or corrupted tarball | gzip CRC fails, `tar` exits nonzero, `.new` discarded — **never swapped** |
| tarball top dir ≠ resolved SHA | stale CDN or moved ref; abort before the swap, naming both SHAs |
| manifest file missing or empty after extract | abort before the swap, naming the file |
| `--verify` finds a hash mismatch | report the changed files; change nothing (repair with `--force`) |
| resolved SHA already matches `VERSION` | report it and exit 0 — 40 bytes, no fetch, install untouched |
| `--force` with an unchanged ref | re-fetch and re-swap anyway; the repair path after `--verify` |
| interrupted between extract and swap | `.old` present → next run recovers to a complete tree |
| `tar` lacks `--wildcards` (BSD) | detected, flag omitted |
| `GPUDEV_DIR` not writable | fail with the resolved path named, before touching anything |
| GitHub rate-limited or unreachable | fail loudly; an existing install keeps working |
| `%run CRAFT.py` with no args | current behaviour, unchanged |
| client/container version mismatch | warn once, name both fixes, continue |
| `VERSION` absent (pre-existing install) | treated as unknown; no warning, no failure |
| `GPUDEV_REF` names a nonexistent ref | fetch fails before the swap; existing install untouched, the ref named in the error |
| `GPUDEV_REF` unset | resolves to `main` — today's behaviour exactly |

## Decisions

1. **No pip package.** Deferred deliberately. A package would add a build and
   release step for a benefit — dependency resolution — that a ten-file
   manifest does not need yet.
2. **The round trip stays.** Enrollment is two-party by design; automating it
   means a host-side listener for unauthenticated clients.
3. **The domain becomes an ordinary argument.** It is public DNS, not a
   secret, and it is the only thing forcing `--hostname` to the end of the flow.
4. **The fetch script does not generate keys.** One job, no second
   implementation of tested Python in shell.
5. **Version mismatch warns, never blocks.**
6. **`GPUDEV_REF` ships; a release process does not.** The override defaults to
   `main`, so default behaviour is unchanged, but the escape hatch exists.
   Without it a bad commit reaching a notebook has **no recovery at all** — the
   user waits for a fix to land on `main` — and at 100 commits a month straight
   to `main` with zero tags, that gap is real rather than theoretical. Cutting
   tags is deferred until there are users the maintainer does not personally
   know. Documented as a recovery lever, not as routine usage.
7. **Re-running the cell is the update path, and is nearly free.** The ref is
   resolved on every run (40 bytes) and the fetch is skipped when the resolved
   SHA already matches `VERSION`. A working install is never swapped for an
   identical one, so users can re-run the cell freely — which matters, because
   the guides tell them to do exactly that to pick up fixes.
8. **Download verification ships, scoped honestly.** Four layers — tar's exit
   status, SHA-vs-tree identity, manifest completeness, and a `--verify`
   self-check — chosen because each was *tested* against the live endpoint.
   Two results drove the design: a truncated tarball is caught for free by gzip
   but still leaves partial files on disk, which is why the swap must be gated
   on tar's exit status; and `py_compile` does **not** detect truncation, so the
   obvious syntax check was rejected as security theatre. The scope is
   corruption and staleness, **not** supply chain — the code and any checksum
   share a trust root, and that limit is stated in the spec rather than left
   for a reader to infer.

## Open

None.
