import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = ROOT / "gpudev-ssh-dispatch"
CLI = ROOT / "gpudev"
LINUX_SETUP = (ROOT / "linux-setup.sh").read_text(encoding="utf-8")


class PowerSshDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        (self.home / "bin").mkdir()
        fake_gpudev = self.home / "bin" / "gpudev"
        fake_gpudev.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_gpudev.chmod(0o755)

    def tearDown(self):
        self.tempdir.cleanup()

    def dispatch(self, command):
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "SSH_ORIGINAL_COMMAND": command,
                "GPUDEV_SSH_DISPATCH_DRYRUN": "1",
            }
        )
        return subprocess.run(
            ["bash", str(DISPATCHER)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_natural_power_commands_route_to_gpudev(self):
        expected = {
            "sleep": "gpudev power sleep now",
            "sleep now": "gpudev power sleep now",
            "sleep 60m": "gpudev power sleep 60m",
            "reboot": "gpudev power reboot now",
            "reboot 2h": "gpudev power reboot 2h",
            "power setup": "gpudev power setup",
            "power status": "gpudev power status",
            "power cancel": "gpudev power cancel",
            "power cancel sleep": "gpudev power cancel sleep",
        }
        for command, output in expected.items():
            with self.subTest(command=command):
                result = self.dispatch(command)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), output)

    def test_duration_requires_an_explicit_unit(self):
        result = self.dispatch("sleep 60")
        self.assertEqual(result.returncode, 2)
        self.assertIn("30s, 60m, 2h", result.stderr)

    def test_dispatcher_is_executable(self):
        self.assertTrue(os.access(DISPATCHER, os.X_OK))

    def test_other_ssh_commands_pass_through(self):
        result = self.dispatch("gpudev status")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "shell: gpudev status")

    def test_install_replaces_raw_managed_key_and_preserves_other_keys(self):
        ssh_dir = self.home / ".ssh"
        ssh_dir.mkdir()
        keys = ssh_dir / "authorized_keys"
        admin_key = "ssh-ed25519 AAAAC3NzaAdmin gpudev-admin"
        other_key = "ssh-ed25519 AAAAC3NzaOther someone-else"
        keys.write_text(f"{other_key}\n{admin_key}\n", encoding="utf-8")
        config = self.home / "host.json"
        config.write_text(json.dumps({"admin_ssh_key": admin_key}), encoding="utf-8")

        result = subprocess.run(
            ["bash", str(DISPATCHER), "--install", str(config), str(keys)],
            env={**os.environ, "HOME": str(self.home)},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = keys.read_text(encoding="utf-8").splitlines()
        self.assertIn(other_key, lines)
        managed = [line for line in lines if "AAAAC3NzaAdmin" in line]
        self.assertEqual(len(managed), 1)
        self.assertTrue(managed[0].startswith('command="'))
        self.assertIn(str(DISPATCHER.resolve()), managed[0])


class PowerSchedulingTests(unittest.TestCase):
    def test_duration_parser_uses_unambiguous_units(self):
        script = CLI.read_text(encoding="utf-8").removesuffix('main "$@"\n')
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
            handle.write(script)
            path = handle.name
        try:
            command = f'source "{path}"; power_duration_seconds 60m'
            result = subprocess.run(
                ["bash", "-c", command],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "3600")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_scheduling_uses_persistent_user_systemd_timers(self):
        cli = CLI.read_text(encoding="utf-8")
        self.assertIn("systemd-run --user", cli)
        self.assertIn("--on-active=", cli)
        self.assertIn("gpudev-power-${action}-", cli)
        self.assertIn('loginctl enable-linger "$LINUX_USER"', LINUX_SETUP)
        self.assertIn('loginctl enable-linger "$(whoami)"', cli)
        self.assertIn("gpudev-ssh-dispatch", LINUX_SETUP)


if __name__ == "__main__":
    unittest.main()
