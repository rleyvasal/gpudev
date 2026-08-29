from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gpudev_craft.client_setup import (
    normalize_client_name,
    setup_client,
    validate_hostname,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class ClientSetupTests(unittest.TestCase):
    def test_name_and_hostname_validation(self):
        self.assertEqual(normalize_client_name("SolveIt-E"), "solveit-e")
        self.assertEqual(validate_hostname("SolveIt-E.Example.COM."), "solveit-e.example.com")
        for bad in ("", "two words", "-leading", "trailing-"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_client_name(bad)
        for bad in ("localhost", "bad host.example.com", "-bad.example.com"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_hostname(bad)

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is required")
    def test_setup_is_idempotent_and_keeps_private_key(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            first = setup_client("solveite", "solveite.example.com", home=home)
            original_private = first.private_key.read_bytes()
            second = setup_client("solveite", "solveite.example.com", home=home)

            self.assertTrue(first.key_created)
            self.assertTrue(first.ssh_config_changed)
            self.assertFalse(second.key_created)
            self.assertFalse(second.ssh_config_changed)
            self.assertEqual(second.private_key.read_bytes(), original_private)
            self.assertEqual(oct(second.private_key.stat().st_mode & 0o777), "0o600")

            config = (home / ".ssh" / "config").read_text()
            self.assertEqual(config.count("Host gpudev-solveite"), 1)
            self.assertIn("HostName solveite.example.com", config)
            self.assertIn("IdentityFile ~/.ssh/gpudev-solveite", config)
            self.assertIn("StrictHostKeyChecking accept-new", config)

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is required")
    def test_managed_stanza_updates_hostname_without_replacing_key(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            first = setup_client("solveite", "old.example.com", home=home)
            key = first.private_key.read_bytes()
            second = setup_client("solveite", "new.example.com", home=home)

            self.assertFalse(second.key_created)
            self.assertTrue(second.ssh_config_changed)
            self.assertEqual(second.private_key.read_bytes(), key)
            config = (home / ".ssh" / "config").read_text()
            self.assertNotIn("old.example.com", config)
            self.assertIn("new.example.com", config)


class ClientInviteTests(unittest.TestCase):
    def test_invite_prints_complete_non_mutating_bootstrap(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            config_dir = home / ".config" / "gpudev"
            config_dir.mkdir(parents=True)
            (config_dir / "host.json").write_text(
                json.dumps({"cf_domain": "example.com", "linux_user": "gpudev"})
            )
            clients_path = config_dir / "clients.json"
            clients_path.write_text(json.dumps({"clients": []}))
            before = clients_path.read_bytes()

            env = os.environ.copy()
            env["HOME"] = str(home)
            result = subprocess.run(
                [str(REPO_ROOT / "gpudev"), "client", "invite", "solveite"],
                cwd=REPO_ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertIn("solveite.example.com", result.stdout)
            self.assertIn("git clone https://github.com/rleyvasal/gpudev.git", result.stdout)
            self.assertIn("%run /app/data/gpudevd/gpudev/CRAFT.py", result.stdout)
            self.assertIn(
                "%gpu_setup solveite --hostname solveite.example.com", result.stdout
            )
            self.assertIn("%gpu solveite", result.stdout)
            self.assertEqual(clients_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
