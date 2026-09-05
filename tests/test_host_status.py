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


class HostStatusTests(unittest.TestCase):
    def run_status(self, host_env: str, port: int) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            config = home / ".config" / "gpudev"
            config.mkdir(parents=True)
            (config / "host.json").write_text(
                json.dumps(
                    {
                        "cf_domain": "example.com",
                        "linux_user": "gpudev",
                        "host_env": host_env,
                        "port_base": 52200,
                        "host_ssh_port": port,
                        "host_cf_hostname": "gpudev.example.com",
                    }
                ),
                encoding="utf-8",
            )
            (config / "clients.json").write_text(
                json.dumps({"clients": []}), encoding="utf-8"
            )
            library = Path(td) / "gpudev-library.sh"
            library.write_text(
                GPUDEV.rsplit('\nmain "$@"', 1)[0] + "\n", encoding="utf-8"
            )
            harness = Path(td) / "harness.sh"
            harness.write_text(
                textwrap.dedent(
                    f"""
                    #!/usr/bin/env bash
                    source {library}
                    export HOME={home}
                    CONFIG_DIR="$HOME/.config/gpudev"
                    HOST_CONFIG="$CONFIG_DIR/host.json"
                    CLIENTS_CONFIG="$CONFIG_DIR/clients.json"
                    require_host_setup() {{ :; }}
                    docker_cmd() {{ return 1; }}
                    command_exists() {{ return 1; }}
                    ml_profile_summary() {{ echo unknown; }}
                    ip() {{ echo '    inet 192.168.10.80/24 scope global eth0'; }}
                    cmd_host_status
                    """
                ),
                encoding="utf-8",
            )
            return subprocess.run(
                ["bash", str(harness)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                env={**os.environ, "HOME": str(home)},
            )

    def test_bare_linux_prints_stable_lan_and_tunnel_aliases(self):
        result = self.run_status("linux", 22)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Host gpudev-lan", result.stdout)
        self.assertIn("HostName 192.168.10.80", result.stdout)
        self.assertIn("Port 22", result.stdout)
        self.assertIn("Host gpudev", result.stdout)
        self.assertNotIn("internal to WSL", result.stdout)

    def test_wsl_keeps_internal_port_guidance(self):
        result = self.run_status("wsl2", 52100)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Host gpudev-lan", result.stdout)
        self.assertIn("port 52100 is internal to WSL", result.stdout)


if __name__ == "__main__":
    unittest.main()
