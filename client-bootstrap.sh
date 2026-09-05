#!/bin/sh
# client-bootstrap.sh — install the gpudev CLIENT runtime into a notebook.
#
# The client runs ten files (~192 KB). A repository clone ships roughly five
# times that, plus history and host-side scripts a notebook can never execute
# (linux-setup.sh, windows-setup.ps1, client-setup.sh, the gpudev CLI, tests).
# This fetches only the manifest below.
#
#   sh client-bootstrap.sh              install or update
#   sh client-bootstrap.sh --verify     re-hash an install against its VERSION
#   sh client-bootstrap.sh --force      re-fetch even when already current
#
#   GPUDEV_DIR   where to install       (default /app/data/gpudevd/gpudev)
#   GPUDEV_REF   branch, tag or SHA     (default main)
#   GPUDEV_SLUG  owner/repo             (default rleyvasal/gpudev)
#
# POSIX sh on purpose: it is invoked as `sh client-bootstrap.sh` from a notebook
# cell, so the shebang is bypassed and bash cannot be assumed.

set -eu

GPUDEV_SLUG="${GPUDEV_SLUG:-rleyvasal/gpudev}"
GPUDEV_REF="${GPUDEV_REF:-main}"
GPUDEV_DIR="${GPUDEV_DIR:-/app/data/gpudevd/gpudev}"

# The client manifest. Exact files are required; addons are a glob so a new
# addon does not require a bootstrap change. Matching *.py also excludes the
# addons/ symlinks, which point outside the repository and dangle on a client.
REQUIRED="CRAFT.py gpudev_craft/__init__.py gpudev_craft/core.py gpudev_craft/client_setup.py gpudev_craft/magics.py"
PATTERNS="*/CRAFT.py */gpudev_craft/*.py */addons/*.py"

MODE=install
FORCE=""

die() { printf 'client-bootstrap: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

usage() {
    cat <<EOF
Usage: sh client-bootstrap.sh [--verify] [--force]

  (no flags)  install, or update when ${GPUDEV_REF} has moved
  --verify    re-hash the existing install against its VERSION file
  --force     re-fetch and reinstall even if already current

Environment:
  GPUDEV_DIR   install location   (default /app/data/gpudevd/gpudev)
  GPUDEV_REF   branch/tag/SHA     (default main)
  GPUDEV_SLUG  owner/repo         (default rleyvasal/gpudev)
EOF
}

for arg in "$@"; do
    case "$arg" in
        --verify)   MODE=verify ;;
        --force)    FORCE=1 ;;
        -h|--help)  usage; exit 0 ;;
        *)          usage >&2; die "unknown argument '$arg'" ;;
    esac
done

command -v curl >/dev/null 2>&1 || die "curl is required."
command -v tar  >/dev/null 2>&1 || die "tar is required."

# sha256: coreutils on Linux, shasum on macOS. Absence is not fatal — hashes
# are an integrity aid, and refusing to install without them would be worse
# than installing with the other three verification layers intact.
if command -v sha256sum >/dev/null 2>&1; then
    hash_of() { sha256sum "$1" | cut -d' ' -f1; }
    HAVE_HASH=1
elif command -v shasum >/dev/null 2>&1; then
    hash_of() { shasum -a 256 "$1" | cut -d' ' -f1; }
    HAVE_HASH=1
else
    hash_of() { echo "-"; }
    HAVE_HASH=""
fi

version_field() {
    # $1 = key, $2 = VERSION path
    [ -f "$2" ] || return 1
    awk -v k="$1" '$1 == k { print $2; found = 1; exit } END { exit !found }' "$2"
}

# ── --verify ─────────────────────────────────────────────────────────────────
# Answers "is this install intact?" with no network round trip: the question
# support cannot otherwise answer once a notebook starts behaving oddly.
if [ "$MODE" = verify ]; then
    vfile="${GPUDEV_DIR}/VERSION"
    [ -f "$vfile" ] || die "no VERSION at ${vfile} — nothing to verify."
    [ -n "$HAVE_HASH" ] || die "no sha256sum or shasum available."

    bad=0 checked=0
    while read -r want path; do
        case "$want" in ''|sha|ref|fetched|files|slug) continue ;; esac
        checked=$((checked + 1))
        if [ ! -f "${GPUDEV_DIR}/${path}" ]; then
            say "MISSING  ${path}"; bad=$((bad + 1)); continue
        fi
        got="$(hash_of "${GPUDEV_DIR}/${path}")"
        if [ "$got" != "$want" ]; then
            say "CHANGED  ${path}"; bad=$((bad + 1))
        fi
    done < "$vfile"

    say ""
    say "  install: ${GPUDEV_DIR}"
    say "  commit:  $(version_field sha "$vfile" 2>/dev/null || echo unknown)"
    say "  checked: ${checked} files"
    if [ "$bad" -gt 0 ]; then
        say ""
        say "${bad} file(s) differ from the recorded hashes."
        say "Repair with:  sh client-bootstrap.sh --force"
        exit 1
    fi
    say "  result:  OK — all files match"
    exit 0
fi

# ── Resolve the ref ──────────────────────────────────────────────────────────
# One 40-byte request against a 214 KB tarball. Doing this first is what makes
# re-running the notebook cell nearly free when nothing has changed — and the
# guides tell users to re-run it to pick up fixes, so it happens often.
#
# no-cache narrows the API's own 60s CDN window. It does not close it, so the
# resolved SHA is always reported rather than assumed.
sha="$(curl -fsSL --max-time 20 -H "Cache-Control: no-cache" \
        -H "Accept: application/vnd.github.sha" \
        "https://api.github.com/repos/${GPUDEV_SLUG}/commits/${GPUDEV_REF}" 2>/dev/null || true)"

case "$sha" in
    *[!0-9a-f]* | "") die "could not resolve '${GPUDEV_REF}' in ${GPUDEV_SLUG} (network, or no such ref)." ;;
esac
[ "${#sha}" -eq 40 ] || die "unexpected SHA for '${GPUDEV_REF}': ${sha}"

short="$(printf '%s' "$sha" | cut -c1-7)"

if [ -z "$FORCE" ] && [ "$(version_field sha "${GPUDEV_DIR}/VERSION" 2>/dev/null || echo)" = "$sha" ]; then
    say "Already at ${short} (${GPUDEV_REF}). Nothing to do."
    say "  ${GPUDEV_DIR}"
    exit 0
fi

# ── Fetch ────────────────────────────────────────────────────────────────────
parent="$(dirname "$GPUDEV_DIR")"
mkdir -p "$parent" || die "cannot create ${parent}"
[ -w "$parent" ] || die "${parent} is not writable."

tmp="$(mktemp -d "${parent}/.gpudev-boot.XXXXXX")" || die "cannot create a temp dir in ${parent}"
trap 'rm -rf "$tmp"' EXIT INT TERM

say "Fetching ${GPUDEV_SLUG} @ ${short} (${GPUDEV_REF})..."
curl -fsSL --max-time 120 \
    "https://api.github.com/repos/${GPUDEV_SLUG}/tarball/${sha}" \
    -o "${tmp}/repo.tgz" \
    || die "download failed."

# Layer 2 — identity. GitHub names the top-level directory <owner>-<repo>-<short
# sha>. Checking it proves this archive is the tree that was asked for, which is
# what catches a stale CDN response or a ref that moved between the two
# requests. Neither corrupts anything, so neither is visible to layer 1.
top="$(tar tzf "${tmp}/repo.tgz" 2>/dev/null | head -1 | cut -d/ -f1)"
if [ -z "$top" ]; then
    # Unreadable, not merely unexpected. Reporting this as a stale CDN would
    # send the reader after the wrong problem — the download is damaged, and
    # retrying is the fix for a different reason.
    die "archive is unreadable (truncated or corrupt download). Nothing was changed."
fi
case "$top" in
    *"$short") : ;;
    *) die "archive is '${top}', expected a tree for ${short} — stale CDN or a moved ref. Retry in a minute." ;;
esac

# Layer 1 — transport integrity. gzip carries a CRC, so a truncated or corrupted
# tarball fails here. It still writes partial files before failing, so this MUST
# extract into the staging dir and MUST NOT ignore the exit status: that pairing
# is what keeps a truncated core.py from ever reaching a live install.
mkdir -p "${tmp}/new"
# $PATTERNS must reach tar literally. Unquoted, the shell expands `*/CRAFT.py`
# against the CURRENT directory first and hands tar whatever happened to match
# there — silent in a clean directory, wrong in a dirty one. `set -f` disables
# globbing while still word-splitting, which is exactly what is wanted here.
set -f
if tar --version 2>/dev/null | head -1 | grep -qi gnu; then
    # shellcheck disable=SC2086
    tar xzf "${tmp}/repo.tgz" -C "${tmp}/new" --strip-components=1 --wildcards $PATTERNS \
        || die "archive did not extract cleanly (truncated or corrupt). Nothing was changed."
else
    # BSD tar globs by default and rejects --wildcards.
    # shellcheck disable=SC2086
    tar xzf "${tmp}/repo.tgz" -C "${tmp}/new" --strip-components 1 $PATTERNS \
        || die "archive did not extract cleanly (truncated or corrupt). Nothing was changed."
fi
set +f

# Layer 3 — completeness. Layer 1 proves the archive arrived intact; it says
# nothing about the archive containing what this script needs. An upstream
# rename would extract cleanly, exit 0, and leave a half-installed tree.
for f in $REQUIRED; do
    [ -s "${tmp}/new/${f}" ] || die "'${f}' is missing or empty in ${short} — refusing to install."
done
addons=0
for f in "${tmp}/new"/addons/*.py; do
    [ -f "$f" ] && addons=$((addons + 1))
done
[ "$addons" -gt 0 ] || die "no addons/*.py found in ${short} — refusing to install."

# ── Record VERSION ───────────────────────────────────────────────────────────
count=0
{
    say "sha ${sha}"
    say "ref ${GPUDEV_REF}"
    say "slug ${GPUDEV_SLUG}"
    say "fetched $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "${tmp}/new/VERSION"

if [ -n "$HAVE_HASH" ]; then
    ( cd "${tmp}/new" && find . -name '*.py' -type f | sed 's|^\./||' | sort ) \
    | while read -r rel; do
        printf '%s  %s\n' "$(hash_of "${tmp}/new/${rel}")" "$rel"
    done >> "${tmp}/new/VERSION"
fi
count="$( ( cd "${tmp}/new" && find . -name '*.py' -type f | wc -l ) | tr -d ' ')"

# ── Atomic swap ──────────────────────────────────────────────────────────────
# A clone gets atomicity free from git; a tarball extract does not. Without
# this, an interruption can leave a truncated core.py that still imports and
# then misbehaves — a failure that presents as a gpudev bug rather than a bad
# download. Directory renames mean a reader mid-import sees the whole old tree
# or the whole new one.
rm -rf "${GPUDEV_DIR}.old" "${GPUDEV_DIR}.new"
mv "${tmp}/new" "${GPUDEV_DIR}.new"
if [ -d "$GPUDEV_DIR" ]; then
    mv "$GPUDEV_DIR" "${GPUDEV_DIR}.old"
fi
mv "${GPUDEV_DIR}.new" "$GPUDEV_DIR"
rm -rf "${GPUDEV_DIR}.old"

# ── Stable entry point ───────────────────────────────────────────────────────
# GPUDEV_DIR is configurable, but the notebook's second line is a `%run` with a
# literal path — and IPython expands `$VAR` from the PYTHON namespace, not the
# shell environment, so `%run $GPUDEV_DIR/CRAFT.py` simply does not work
# (measured: "File `'$GPUDEV_DIR/CRAFT.py'` not found"). A shell escape cannot
# set a Python variable either, so line 1 has no way to tell line 2 where the
# install went.
#
# A symlink at a path that is always valid closes that gap: the install lives
# wherever GPUDEV_DIR says, and the cell always says ~/.gpudev-client. `%run`
# expands `~`, and __file__.resolve() follows the link, so imports and addons
# resolve against the real directory.
ENTRY=""
if [ -n "${HOME:-}" ] && [ -d "$HOME" ]; then
    link="${HOME}/.gpudev-client"
    if [ -e "$link" ] && [ ! -L "$link" ]; then
        # Never replace something that is not ours to replace.
        say "Note: ${link} exists and is not a symlink; leaving it alone."
    elif ln -sfn "$GPUDEV_DIR" "$link" 2>/dev/null; then
        ENTRY="~/.gpudev-client"
    fi
fi

say ""
say "Installed gpudev client runtime"
say "  commit:  ${short} (${GPUDEV_REF})"
say "  files:   ${count} python files + VERSION"
say "  path:    ${GPUDEV_DIR}"
[ -n "$ENTRY" ] && say "  entry:   ${ENTRY} → ${GPUDEV_DIR}"
say ""
say "Next:  %run ${ENTRY:-$GPUDEV_DIR}/CRAFT.py <client-name> --domain <your-domain>"
