"""SolveIt-side, idempotent setup for a gpudev client identity."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


_CLIENT_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_client_name(raw: str) -> str:
    """Validate and normalize the identity used by the host and SSH alias."""
    name = (raw or "").strip().lower()
    if not name or len(name) > 63 or not _CLIENT_NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid client name {raw!r}: use lowercase letters, digits, and "
            "single hyphens (for example: solveite)"
        )
    return name


def validate_hostname(raw: str) -> str:
    """Return a normalized DNS hostname, rejecting shell/SSH-config syntax."""
    hostname = (raw or "").strip().lower().rstrip(".")
    labels = hostname.split(".")
    if (
        not hostname
        or len(hostname) > 253
        or len(labels) < 2
        or any(not _HOST_LABEL_RE.fullmatch(label) for label in labels)
    ):
        raise ValueError(f"invalid hostname {raw!r} (expected e.g. solveite.example.com)")
    return hostname


@dataclass(frozen=True)
class ClientSetupResult:
    name: str
    hostname: str
    ssh_alias: str
    private_key: Path
    public_key: Path
    public_key_text: str
    key_created: bool
    ssh_config_changed: bool


def _atomic_write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _has_exact_host_alias(config_text: str, alias: str) -> bool:
    for raw_line in config_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        fields = line.split()
        if len(fields) >= 2 and fields[0].lower() == "host" and alias in fields[1:]:
            return True
    return False


def _resolved_hostname(alias: str) -> str:
    result = subprocess.run(
        ["ssh", "-G", alias],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        if line.lower().startswith("hostname "):
            return line.split(None, 1)[1].strip().lower().rstrip(".")
    return ""


def ensure_client_key(name: str, *, home: Path | None = None) -> tuple[Path, Path, bool]:
    """Create an Ed25519 client key once; never replace an existing key."""
    name = normalize_client_name(name)
    home = (home or Path.home()).expanduser()
    ssh_dir = home / ".ssh"
    private_key = ssh_dir / f"gpudev-{name}"
    public_key = private_key.with_name(private_key.name + ".pub")
    ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ssh_dir, 0o700)

    if public_key.exists() and not private_key.exists():
        raise RuntimeError(
            f"Found {public_key} but the private key is missing; refusing to replace it."
        )

    created = False
    if not private_key.exists():
        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                f"gpudev-{name}",
                "-f",
                str(private_key),
            ],
            check=True,
        )
        created = True
    elif not public_key.exists():
        result = subprocess.run(
            ["ssh-keygen", "-y", "-f", str(private_key)],
            check=True,
            capture_output=True,
            text=True,
        )
        _atomic_write(public_key, f"{result.stdout.strip()} gpudev-{name}\n", 0o644)

    os.chmod(private_key, 0o600)
    os.chmod(public_key, 0o644)
    return private_key, public_key, created


def ensure_ssh_config(
    name: str,
    hostname: str,
    private_key: Path,
    *,
    home: Path | None = None,
) -> bool:
    """Install or refresh CRAFT's marked SSH stanza without touching user entries."""
    name = normalize_client_name(name)
    hostname = validate_hostname(hostname)
    home = (home or Path.home()).expanduser()
    ssh_dir = home / ".ssh"
    config_path = ssh_dir / "config"
    ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ssh_dir, 0o700)

    alias = f"gpudev-{name}"
    start = f"# >>> gpudev managed: {name} >>>"
    end = f"# <<< gpudev managed: {name} <<<"
    identity = f"~/.ssh/{private_key.name}"
    block = (
        f"{start}\n"
        f"Host {alias}\n"
        f"  HostName {hostname}\n"
        "  User gpudev\n"
        f"  IdentityFile {identity}\n"
        "  IdentitiesOnly yes\n"
        "  ProxyCommand bash -c 'p=$(command -v cloudflared 2>/dev/null || echo "
        "\"$HOME/.local/bin/cloudflared\"); exec \"$p\" access tcp --hostname %h'\n"
        "  ServerAliveInterval 30\n"
        "  ServerAliveCountMax 3\n"
        f"{end}"
    )

    existing = config_path.read_text() if config_path.exists() else ""
    pattern = re.compile(
        rf"(?ms)^{re.escape(start)}\n.*?^{re.escape(end)}(?:\n|$)"
    )
    match = pattern.search(existing)
    if match:
        replacement = block + ("\n" if match.group(0).endswith("\n") else "")
        updated = existing[: match.start()] + replacement + existing[match.end() :]
    elif _has_exact_host_alias(existing, alias):
        resolved = _resolved_hostname(alias)
        if resolved and resolved != hostname:
            raise RuntimeError(
                f"SSH alias {alias!r} already exists and resolves to {resolved!r}, "
                f"not {hostname!r}. Update that entry or choose another client name."
            )
        os.chmod(config_path, 0o600)
        return False
    else:
        if not existing or existing.endswith("\n\n"):
            separator = ""
        elif existing.endswith("\n"):
            separator = "\n"
        else:
            separator = "\n\n"
        updated = existing + separator + block + "\n"

    if updated == existing:
        os.chmod(config_path, 0o600)
        return False
    _atomic_write(config_path, updated, 0o600)
    return True


def setup_client(name: str, hostname: str, *, home: Path | None = None) -> ClientSetupResult:
    """Ensure the local key and SSH alias for one gpudev client identity."""
    name = normalize_client_name(name)
    hostname = validate_hostname(hostname)
    private_key, public_key, key_created = ensure_client_key(name, home=home)
    changed = ensure_ssh_config(name, hostname, private_key, home=home)
    return ClientSetupResult(
        name=name,
        hostname=hostname,
        ssh_alias=f"gpudev-{name}",
        private_key=private_key,
        public_key=public_key,
        public_key_text=public_key.read_text().strip(),
        key_created=key_created,
        ssh_config_changed=changed,
    )
