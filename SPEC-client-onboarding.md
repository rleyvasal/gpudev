# Spec — client onboarding without the paste round-trip

Status: implemented.
Scope: `gpudev` (`client add`, `client invite`), `gpudev_craft/core.py`,
`gpudev_craft/client_setup.py`, `LINUX-QUICKSTART.md` Part 2.

Covers Tier 1 (fewer pastes) and Tier 2 (notebook prints the admin's command),
with `--hostname` made optional. Tier 3 (an enrollment endpoint that removes
the key transfer entirely) is explicitly **out of scope** — see the end.

---

## Problem

Before this work, onboarding one notebook client cost two human round-trips and
three pastes:

| Transfer | Pastes |
|---|---|
| admin → user: `client invite` output | 2 — Cell 1, Cell 2 |
| user → admin: the public key | 1 — into the `client add` prompt |

The last one is the worst: an 80-character base64 blob, copied out of notebook
output, relayed over chat, and pasted into a prompt where nothing validates
that it landed in the right field.

Unlike the admin key, there is no `ssh-copy-id` equivalent available: the
notebook holds no credential on the gpudev host, so the public key has to cross
a human gap. The goal is to make that gap one line wide, not to close it.

## Goals

1. One paste in each direction, each a single self-contained line.
2. The user never handles a raw key.
3. The flow can start at the **client**, so onboarding does not require the
   admin to go first.
4. The interactive prompt keeps working for anyone who wants it.

## Non-goals

- Any new network surface. Nothing here opens a port or an endpoint.
- Changing how keys are generated or where the private key lives. It stays in
  the notebook, always.

---

## Tier 1 — `client add` accepts the key as an argument

```
gpudev client add <name> [--variant cuda-dev] [--key "<pubkey>" | --key-file <path>]
```

- `--key` takes the full `ssh-ed25519 AAAA... comment` string
- `--key-file` reads it from a path, for scripted use
- neither given → prompt exactly as today
- both given → error, do not guess
- the value is validated with the existing `validate_public_key` before any
  provisioning starts, so a malformed key fails immediately rather than after
  a container has been created

**A public key on the command line is not a secret.** It will appear in shell
history and in `ps` output, and that is fine — it is the half designed to be
published. This note exists so the argument form does not later get "fixed"
into a prompt-only path on security grounds.

## Tier 1 — one-cell bootstrap

`print_solveit_bootstrap` emits one cell: a shell escape that fetches the client
runtime, then a `%run` that both loads CRAFT and runs setup.

```python
!curl -fsSL https://raw.githubusercontent.com/rleyvasal/gpudev/main/client-bootstrap.sh -o /tmp/gpudev-bootstrap.sh && sh /tmp/gpudev-bootstrap.sh
%run /app/data/gpudevd/gpudev/CRAFT.py <name> --domain <domain>
```

This combination was verified in SolveIt. The former `%%bash` cell magic could
not coexist with the other lines, which was why the earlier flow required two
cells.

Two lines rather than three: `%run script.py args` fills `sys.argv` and
`CRAFT.py` is already the `%run` entry point, so setup rides the same line. Two
is the floor — a cold client must fetch something before it can run it. See
`SPEC-client-bootstrap.md` for the fetch, verification and version stamp.

---

## Tier 2 — the notebook prints the admin's command

Before this work, `%gpu_setup` printed the raw public key plus prose telling the
user to give it to an administrator. It now prints the exact command to run:

```
Send this line to your gpudev administrator:

  gpudev client add solveit --key "ssh-ed25519 AAAAC3Nza... solveit@abc123"
```

The user forwards one line. The admin pastes and runs it. Nobody selects a key
out of surrounding text, and nobody chooses which prompt to paste into.

Keep printing the key path and alias for diagnostics, but the command is the
thing the user is told to send.

---

## `--hostname` becomes optional

Before this work, `_parse_gpu_setup_args` **required** `--hostname`, and that
requirement was the only reason the admin had to go first: the hostname is
per-client and the user could not know it unaided.

Two facts make deferral safe:

- `%gpu` connects through the **SSH alias**, `gpudev-<name>`
  (`core.py`, `select_client`). It never reads the hostname.
- the hostname is needed only by `ensure_ssh_config`, when writing the
  `~/.ssh/config` stanza.

### New behavior

| Invocation | Effect |
|---|---|
| `%gpu_setup <name> --hostname <h>` | unchanged — key, stanza, admin command |
| `%gpu_setup <name>` | key + admin command; **no stanza written** |
| `%gpu <name> --hostname <h>` | writes the stanza, then connects |
| `%gpu <name>` with no stanza | refuse with the exact line to run |

`%gpu_setup <name>` must say plainly what is still missing:

```
SSH config not written yet — the hostname is not known.
When the administrator confirms, run:

  %gpu <name> --hostname <name>.<their-domain>
```

The refusal path for `%gpu <name>` without a stanza must name the same command
rather than failing with a generic SSH error.

### Resulting flow

One round trip in each direction, one line each:

1. user: paste the bootstrap cell, `%gpu_setup solveit`
2. user → admin: `gpudev client add solveit --key "ssh-ed25519 ..."`
3. admin runs it; `client add` prints the line to send back:
   `%gpu solveit --hostname solveit.qsoftss.com`
4. user pastes that; connected

If the domain is published once for the host — it is a constant, not
per-client — the user can supply `--hostname` at step 1 and step 4 collapses
into step 1. `client invite` then becomes optional rather than the mandatory
first step.

---

## Changes to existing code

| File | Change |
|---|---|
| `gpudev` | `cmd_client_add`: `--key` / `--key-file`, validate before provisioning |
| `gpudev` | `cmd_client_add`: print the `%gpu <name> --hostname <h>` line on success |
| `gpudev` | `print_solveit_bootstrap`: one cell; dropped "Step 3 — paste the public key", which told the admin to do a step they had just completed |
| `core.py` | `_parse_gpu_setup_args`: `--hostname` optional |
| `core.py` | `gpu_setup`: print the admin command; skip the stanza when no hostname |
| `core.py` | `gpu`: accept and honour `--hostname`; refuse clearly when no stanza |
| `client_setup.py` | `setup_client`: `hostname` optional; return whether the stanza was written |
| `gpudev` | a missing requested `cuda-dev` image is built once inside `client add`, which then continues provisioning |
| `LINUX-QUICKSTART.md` | Part 2 written against this flow |

## Failure modes

| Case | Behavior |
|---|---|
| `--key` malformed | fail before provisioning, with the expected shape |
| `--key` and `--key-file` both given | error, do not guess |
| `%gpu <name>`, no stanza, no `--hostname` | print the exact `%gpu … --hostname …` line |
| `%gpu_setup` re-run | remains idempotent (`ensure_client_key` reuses) and reprints the admin command |
| hostname supplied later differs from an existing stanza | rewrite the stanza, say that it changed |

## Out of scope — Tier 3

An enrollment endpoint would remove the key transfer entirely: `client invite`
mints a one-time, name-bound, expiring token; `%gpu_setup` POSTs its public key;
`client add` finds it waiting.

Deferred deliberately. It adds an internet-reachable endpoint that accepts
public keys, which needs one-shot semantics, short expiry, Access in front, and
admin confirmation of the received key before use. That is a security-sensitive
feature, and Tiers 1–2 already reduce the cost to one line each way. Revisit
only if that still proves painful in practice.
