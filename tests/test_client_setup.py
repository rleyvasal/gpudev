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
    derive_domain,
    has_ssh_stanza,
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
    def test_setup_without_hostname_defers_the_ssh_stanza(self):
        # The notebook must be able to generate its key before the
        # administrator has provisioned anything: requiring --hostname was the
        # only reason the admin had to go first.
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            first = setup_client("solveite", home=home)
            self.assertTrue(first.key_created)
            self.assertFalse(first.ssh_config_written)
            self.assertEqual(first.hostname, "")
            self.assertFalse(has_ssh_stanza("solveite", home=home))

            # Hostname arrives later: same key, stanza now written.
            second = setup_client("solveite", "solveite.example.com", home=home)
            self.assertFalse(second.key_created)
            self.assertTrue(second.ssh_config_written)
            self.assertTrue(has_ssh_stanza("solveite", home=home))
            self.assertEqual(first.public_key_text, second.public_key_text)

    def test_client_add_rejects_bad_key_arguments_before_provisioning(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            config_dir = home / ".config" / "gpudev"
            config_dir.mkdir(parents=True)
            (config_dir / "host.json").write_text(
                json.dumps({"cf_domain": "example.com", "linux_user": "gpudev"})
            )
            (config_dir / "clients.json").write_text(json.dumps({"clients": []}))
            env = os.environ.copy()
            env["HOME"] = str(home)

            def run(*args):
                return subprocess.run(
                    [str(REPO_ROOT / "gpudev"), "client", "add", "t", *args],
                    cwd=REPO_ROOT, env=env, capture_output=True, text=True,
                )

            both = run("--key", "ssh-ed25519 AAAA x", "--key-file", "/tmp/x.pub")
            self.assertNotEqual(both.returncode, 0)
            self.assertIn("not both", both.stderr)

            missing = run("--key-file", str(home / "nope.pub"))
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("Key file not found", missing.stderr)

            malformed = run("--key", "not-a-key at all")
            self.assertNotEqual(malformed.returncode, 0)
            self.assertIn("does not look like an SSH public key", malformed.stderr)

    def test_domain_is_learned_from_an_existing_stanza(self):
        # The notebook cannot know the domain, which is the only reason
        # %gpu_setup ever needed --hostname. Any client already configured
        # records it, so only the first client on a machine has to be told.
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self.assertEqual(derive_domain(home=home), "")
            setup_client("solveitrc", "solveitrc.qsoftss.com", home=home)
            self.assertEqual(derive_domain(home=home), "qsoftss.com")

    def test_gpu_setup_parses_variant(self):
        from gpudev_craft.core import _parse_gpu_setup_args

        self.assertEqual(_parse_gpu_setup_args("a"), ("a", None, "default"))
        self.assertEqual(
            _parse_gpu_setup_args("a --variant cuda-dev"), ("a", None, "cuda-dev")
        )
        self.assertEqual(
            _parse_gpu_setup_args("a --variant=cuda-dev --hostname a.x.com"),
            ("a", "a.x.com", "cuda-dev"),
        )
        with self.assertRaises(ValueError):
            _parse_gpu_setup_args("a --variant bogus")

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
