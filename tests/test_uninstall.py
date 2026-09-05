from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GPUDEV = (ROOT / "gpudev").read_text(encoding="utf-8")


class UninstallExecutionTests(unittest.TestCase):
    def run_harness(
        self,
        body: str,
        *,
        provenance: dict[str, bool] | None = None,
        clients: list[dict] | None = None,
        input_text: str = "",
    ) -> tuple[subprocess.CompletedProcess[str], Path, tempfile.TemporaryDirectory]:
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        home = root / "home"
        config = home / ".config" / "gpudev"
        config.mkdir(parents=True)
        (config / "host.json").write_text(
            json.dumps(
                {
                    "linux_user": "gpudev",
                    "host_cf_hostname": "gpudev.example.com",
                    "host_ssh_port": 52100,
                    "admin_ssh_key": "ssh-ed25519 AAAATEST admin",
                    "installed_by_gpudev": provenance or {},
                }
            ),
            encoding="utf-8",
        )
        (config / "clients.json").write_text(
            json.dumps({"clients": clients or []}), encoding="utf-8"
        )

        # Source the production functions without dispatching main(). System
        # boundaries are overridden below; cmd_uninstall itself is executed.
        library = root / "gpudev-library.sh"
        library.write_text(GPUDEV.rsplit('\nmain "$@"', 1)[0] + "\n", encoding="utf-8")
        harness = root / "harness.sh"
        harness.write_text(
            textwrap.dedent(
                f"""
                #!/usr/bin/env bash
                source {library}
                export HOME={home}
                CONFIG_DIR="$HOME/.config/gpudev"
                HOST_CONFIG="$CONFIG_DIR/host.json"
                CLIENTS_CONFIG="$CONFIG_DIR/clients.json"
                {body}
                """
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            ["bash", str(harness)],
            cwd=ROOT,
            input=input_text,
            text=True,
            capture_output=True,
            env={**os.environ, "HOME": str(home)},
        )
        return result, root, td

    def test_default_uninstall_does_not_forward_purge_data(self):
        body = r'''
        require_interactive_sudo() { :; }
        session_rides_tunnel() { return 1; }
        foreign_docker_workloads() { return 1; }
        reset_manifest() { :; }
        cmd_reset() { printf '%s\n' "$*" > "$HOME/reset-args"; }
        docker_cmd() { :; }
        cmd_uninstall --keep-ssh --yes
        '''
        result, root, td = self.run_harness(body)
        self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((root / "home/reset-args").read_text().strip(), "--yes --force")

    def test_reset_unwraps_admin_key_before_removing_dispatcher(self):
        body = r'''
        mkdir -p "$HOME/.ssh" "$HOME/bin"
        printf '%s\n' \
          'command="/home/gpudev/bin/gpudev-ssh-dispatch" ssh-ed25519 AAAATEST admin' \
          'ssh-ed25519 AAAAOTHER other' > "$HOME/.ssh/authorized_keys"
        touch "$HOME/bin/gpudev-ssh-dispatch"
        require_interactive_sudo() { :; }
        session_rides_tunnel() { return 1; }
        reset_manifest() { :; }
        cf_api_context() { return 1; }
        all_client_names() { :; }
        command_exists() { return 1; }
        gpudev_system_files() { :; }
        docker_cmd() { :; }
        cmd_reset --yes
        '''
        result, root, td = self.run_harness(body)
        self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        authorized = (root / "home/.ssh/authorized_keys").read_text()
        self.assertIn("ssh-ed25519 AAAATEST admin", authorized)
        self.assertNotIn("gpudev-ssh-dispatch", authorized)
        self.assertIn("ssh-ed25519 AAAAOTHER other", authorized)
        self.assertFalse((root / "home/bin/gpudev-ssh-dispatch").exists())

    def test_reset_stops_before_removal_when_admin_key_cannot_be_preserved(self):
        body = r'''
        mkdir -p "$HOME/bin"
        touch "$HOME/bin/gpudev-ssh-dispatch"
        require_interactive_sudo() { :; }
        session_rides_tunnel() { return 1; }
        reset_manifest() { :; }
        command_exists() { return 1; }
        cmd_reset --yes
        '''
        result, root, td = self.run_harness(body)
        self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Nothing has been removed", result.stderr)
        self.assertTrue((root / "home/bin/gpudev-ssh-dispatch").exists())
        self.assertTrue((root / "home/.config/gpudev/host.json").exists())

    def test_dry_run_does_not_probe_sudo_or_docker(self):
        body = r'''
        require_interactive_sudo() { touch "$HOME/sudo-called"; }
        foreign_docker_workloads() { touch "$HOME/docker-called"; }
        reset_manifest() { :; }
        cmd_uninstall --dry-run
        '''
        result, root, td = self.run_harness(
            body, provenance={"docker": True}
        )
        self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((root / "home/sudo-called").exists())
        self.assertFalse((root / "home/docker-called").exists())
        self.assertIn("--dry-run: nothing was changed", result.stdout)

    def test_purge_data_is_forwarded_only_when_requested(self):
        body = r'''
        require_interactive_sudo() { :; }
        session_rides_tunnel() { return 1; }
        foreign_docker_workloads() { return 1; }
        reset_manifest() { :; }
        cmd_reset() { printf '%s\n' "$*" > "$HOME/reset-args"; }
        docker_cmd() { :; }
        cmd_uninstall --keep-ssh --yes --purge-data
        '''
        result, root, td = self.run_harness(body)
        self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (root / "home/reset-args").read_text().strip(),
            "--yes --force --purge-data",
        )

    def test_provenance_survives_reset_deleting_host_config(self):
        body = r'''
        require_interactive_sudo() { :; }
        session_rides_tunnel() { return 1; }
        foreign_docker_workloads() { return 1; }
        reset_manifest() { :; }
        cmd_reset() { rm -f "$HOST_CONFIG"; }
        docker_cmd() { :; }
        cmd_uninstall --keep-ssh --yes
        '''
        result, _, td = self.run_harness(
            body,
            provenance={
                "docker": True,
                "nvidia_container_toolkit": False,
                "cloudflared": False,
            },
            input_text="n\n",
        )
        self.addCleanup(td.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Remove docker (gpudev installed it)?", result.stdout)
        self.assertIn("docker kept", result.stdout)

    def test_reset_failure_stops_before_images_are_removed(self):
        body = r'''
        require_interactive_sudo() { :; }
        session_rides_tunnel() { return 1; }
        foreign_docker_workloads() { return 1; }
        reset_manifest() { :; }
        cmd_reset() { return 42; }
        docker_cmd() { printf '%s\n' "$*" >> "$HOME/docker-calls"; }
        cmd_uninstall --keep-ssh --yes
        '''
        result, root, td = self.run_harness(body)
        self.addCleanup(td.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / "home/docker-calls").exists())

    def test_failed_access_proof_restores_sshd_and_wrapped_key(self):
        body = r'''
        mkdir -p "$HOME/.ssh"
        SSHD_CONFIG="$HOME/sshd_config"
        printf '%s\n' \
          'Port 52100' \
          'PubkeyAuthentication yes' \
          'PasswordAuthentication no' > "$SSHD_CONFIG"
        printf '%s\n' \
          'command="/home/gpudev/bin/gpudev-ssh-dispatch" ssh-ed25519 AAAATEST admin' \
          > "$HOME/.ssh/authorized_keys"
        cp "$SSHD_CONFIG" "$HOME/sshd-original"
        cp "$HOME/.ssh/authorized_keys" "$HOME/keys-original"

        require_interactive_sudo() { :; }
        session_rides_tunnel() { return 1; }
        foreign_docker_workloads() { return 1; }
        reset_manifest() { :; }
        ssh_socket_activated() { return 1; }
        ssh_port_listening() { return 0; }
        sleep() { :; }
        journalctl() { return 1; }
        cmd_reset() { touch "$HOME/reset-called"; }
        docker_cmd() { :; }
        set_sshd_option() {
            local key="$1" value="$2" tmp="${SSHD_CONFIG}.tmp"
            sed -E "s|^[#[:space:]]*${key}[[:space:]]+.*|${key} ${value}|" \
                "$SSHD_CONFIG" > "$tmp"
            mv "$tmp" "$SSHD_CONFIG"
        }
        sudo() {
            if [ "${1:-}" = "-n" ]; then shift; fi
            case "${1:-}" in
                passwd) printf '%s\n' 'gpudev P 2026-09-05 0 99999 7 -1' ;;
                sshd|systemctl) return 0 ;;
                cp|grep|sed|tee) command "$@" ;;
                *) command "$@" ;;
            esac
        }

        cmd_uninstall --yes
        '''
        result, root, td = self.run_harness(body)
        self.addCleanup(td.cleanup)
        home = root / "home"
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            (home / "sshd_config").read_text(), (home / "sshd-original").read_text()
        )
        self.assertEqual(
            (home / ".ssh/authorized_keys").read_text(),
            (home / "keys-original").read_text(),
        )
        self.assertFalse((home / "reset-called").exists())
        self.assertIn("Original SSH access restored on port 52100", result.stdout)
        self.assertIn("original SSH", result.stderr)

    def test_successful_access_proof_commits_revert_before_reset(self):
        body = r'''
        mkdir -p "$HOME/.ssh"
        SSHD_CONFIG="$HOME/sshd_config"
        printf '%s\n' \
          'Port 52100' \
          'PubkeyAuthentication yes' \
          'PasswordAuthentication no' > "$SSHD_CONFIG"
        printf '%s\n' \
          'command="/home/gpudev/bin/gpudev-ssh-dispatch" ssh-ed25519 AAAATEST admin' \
          > "$HOME/.ssh/authorized_keys"

        require_interactive_sudo() { :; }
        session_rides_tunnel() { return 1; }
        foreign_docker_workloads() { return 1; }
        reset_manifest() { :; }
        ssh_socket_activated() { return 1; }
        ssh_port_listening() { return 0; }
        sleep() { :; }
        journalctl() { printf '%s\n' 'Accepted password for gpudev from 192.0.2.1'; }
        cmd_reset() {
            cp "$SSHD_CONFIG" "$HOME/sshd-at-reset"
            cp "$HOME/.ssh/authorized_keys" "$HOME/keys-at-reset"
        }
        docker_cmd() { :; }
        set_sshd_option() {
            local key="$1" value="$2" tmp="${SSHD_CONFIG}.tmp"
            sed -E "s|^[#[:space:]]*${key}[[:space:]]+.*|${key} ${value}|" \
                "$SSHD_CONFIG" > "$tmp"
            mv "$tmp" "$SSHD_CONFIG"
        }
        sudo() {
            if [ "${1:-}" = "-n" ]; then shift; fi
            case "${1:-}" in
                passwd) printf '%s\n' 'gpudev P 2026-09-05 0 99999 7 -1' ;;
                sshd|systemctl) return 0 ;;
                cp|grep|sed|tee) command "$@" ;;
                *) command "$@" ;;
            esac
        }

        cmd_uninstall --yes
        '''
        result, root, td = self.run_harness(body)
        self.addCleanup(td.cleanup)
        home = root / "home"
        self.assertEqual(result.returncode, 0, result.stderr)
        sshd = (home / "sshd-at-reset").read_text()
        keys = (home / "keys-at-reset").read_text()
        self.assertIn("Port 22", sshd)
        self.assertIn("PasswordAuthentication yes", sshd)
        self.assertEqual(keys, "ssh-ed25519 AAAATEST admin\n")
        self.assertIn("Login confirmed. Proceeding.", result.stdout)


if __name__ == "__main__":
    unittest.main()
