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

    def test_client_add_builds_missing_cuda_dev_once_then_continues(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            repo = root / "repo"
            fake_bin = root / "bin"
            state = root / "state"
            (home / ".config" / "gpudev").mkdir(parents=True)
            repo.mkdir()
            fake_bin.mkdir()
            state.mkdir()

            (home / ".config" / "gpudev" / "host.json").write_text(
                json.dumps({"cf_domain": "example.com", "linux_user": "gpudev"})
            )
            (home / ".config" / "gpudev" / "clients.json").write_text(
                json.dumps({"clients": []})
            )
            shutil.copy2(REPO_ROOT / "gpudev", repo / "gpudev")

            docker = fake_bin / "docker"
            docker.write_text(
                "#!/usr/bin/env bash\n"
                "if [ \"${1:-}\" = info ]; then exit 0; fi\n"
                "if [ \"${1:-} ${2:-}\" = 'image inspect' ]; then\n"
                "  [ \"${3:-}\" = gpudev-base-cuda-dev:latest ] || exit 0\n"
                "  test -f \"$TEST_STATE/cuda-built\"\n"
                "fi\n"
            )
            docker.chmod(0o755)

            setup = repo / "linux-setup.sh"
            setup.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$TEST_STATE/build-calls\"\n"
                "touch \"$TEST_STATE/cuda-built\"\n"
            )
            setup.chmod(0o755)

            client_setup = repo / "client-setup.sh"
            client_setup.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s|%s|%s\\n' \"$GPUDEV_VARIANT\" \"$1\" \"$2\" "
                ">> \"$TEST_STATE/client-calls\"\n"
            )
            client_setup.chmod(0o755)

            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "PATH": f"{fake_bin}:{env['PATH']}",
                "TEST_STATE": str(state),
            })
            command = [
                str(repo / "gpudev"), "client", "add", "alice",
                "--variant", "cuda-dev", "--key", "ssh-ed25519 AAAA test",
            ]

            first = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("Building it now", first.stdout)
            self.assertIn("continuing client provisioning", first.stdout)
            self.assertEqual((state / "build-calls").read_text().splitlines(), ["--build-cuda-dev"])
            self.assertIn("cuda-dev|alice|ssh-ed25519 AAAA test", (state / "client-calls").read_text())

            second = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotIn("Building it now", second.stdout)
            self.assertEqual((state / "build-calls").read_text().splitlines(), ["--build-cuda-dev"])

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

    def test_gpu_setup_expands_domain_into_a_hostname(self):
        # --domain is the normal path: the client already knows its own name,
        # and the domain is public DNS rather than a secret, so an admin can
        # publish it once instead of returning a hostname per client.
        from gpudev_craft.core import _parse_gpu_setup_args

        self.assertEqual(
            _parse_gpu_setup_args("alice --domain example.com"),
            ("alice", "alice.example.com", "default"),
        )
        self.assertEqual(
            _parse_gpu_setup_args("alice --domain=example.com --variant cuda-dev"),
            ("alice", "alice.example.com", "cuda-dev"),
        )
        # It must agree with the host, which routes <name>.<domain>, and reject
        # a name the host would refuse rather than minting a hostname for it.
        self.assertEqual(
            _parse_gpu_setup_args("ALICE --domain example.com")[1],
            "alice.example.com",
        )
        with self.assertRaises(ValueError):
            _parse_gpu_setup_args("Alice_B --domain example.com")
        # A leading dot is the obvious typo; silently doubling it would produce
        # a hostname that never resolves.
        self.assertEqual(
            _parse_gpu_setup_args("alice --domain .example.com")[1],
            "alice.example.com",
        )
        with self.assertRaises(ValueError):
            _parse_gpu_setup_args("alice --domain example.com --hostname a.b.com")
        with self.assertRaises(ValueError):
            _parse_gpu_setup_args("alice --domain ''")

    def test_craft_entrypoint_only_trusts_its_own_argv(self):
        # `%run CRAFT.py alice --domain x` fills sys.argv, which is what lets
        # one line load the magics AND run setup. Imported another way, argv
        # belongs to the kernel launcher (-f …kernel.json), and treating that
        # as a client name would fail confusingly.
        source = (REPO_ROOT / "CRAFT.py").read_text()
        self.assertIn("_setup_args", source)
        self.assertIn("Path(argv[0]).name != Path(__file__).name", source)

        import sys as _sys_mod

        namespace: dict = {
            "__file__": str(REPO_ROOT / "CRAFT.py"),
            "Path": Path,
            "shlex": __import__("shlex"),
            "sys": _sys_mod,
        }
        body = source[source.index("def _setup_args"):source.index("_args = _setup_args()")]
        exec(compile(body, "CRAFT.py", "exec"), namespace)
        setup_args = namespace["_setup_args"]

        import sys as _sys

        saved = _sys.argv
        try:
            _sys.argv = ["CRAFT.py"]
            self.assertIsNone(setup_args())
            _sys.argv = ["CRAFT.py", "alice", "--domain", "example.com"]
            self.assertEqual(setup_args(), "alice --domain example.com")
            # The kernel launcher's argv must never be read as setup arguments.
            _sys.argv = ["ipykernel_launcher.py", "-f", "/tmp/kernel.json"]
            self.assertIsNone(setup_args())
        finally:
            _sys.argv = saved

    def test_rebuild_still_accepts_all_as_a_target(self):
        # Adding --variant introduced an option parser, whose `-*` catch-all
        # swallowed `--all` and broke rebuilding every client. --all is a
        # TARGET that merely looks like a flag.
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
            result = subprocess.run(
                [str(REPO_ROOT / "gpudev"), "client", "rebuild", "--all"],
                cwd=REPO_ROOT, env=env, capture_output=True, text=True,
            )
            self.assertNotIn("Unknown option '--all'", result.stderr)

            # --variant is single-client only: switching every client at once
            # would rebuild every venv on the host.
            refused = subprocess.run(
                [str(REPO_ROOT / "gpudev"), "client", "rebuild", "--all",
                 "--variant", "cuda-dev"],
                cwd=REPO_ROOT, env=env, capture_output=True, text=True,
            )
            self.assertIn("applies to one client", refused.stderr)

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
            self.assertIn("client-bootstrap.sh", result.stdout)
            # One cell, not two. %%bash is a CELL magic and cannot share a cell
            # with %run, which is the only reason the bootstrap was ever split;
            # `!` escapes can, verified in SolveIt. Reintroducing %%bash would
            # silently split it again.
            self.assertNotIn("%%bash", result.stdout)
            # Two lines, not three: %run carries the setup arguments, because
            # `%run script.py args` fills sys.argv and CRAFT.py is already the
            # entry point. Splitting setup back onto its own %gpu_setup line
            # would be a regression, not a style choice.
            self.assertIn(
                "%run /app/data/gpudevd/gpudev/CRAFT.py solveite --domain example.com",
                result.stdout,
            )
            self.assertNotIn("%gpu_setup", result.stdout)
            self.assertIn("%gpu solveite", result.stdout)
            # The cell must not have drifted back to a repository clone.
            self.assertNotIn("git clone", result.stdout)
            self.assertEqual(clients_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
