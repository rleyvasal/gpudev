import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "windows-setup.ps1").read_text(encoding="utf-8")


def function_body(name: str) -> str:
    match = re.search(
        rf"(?ms)^function {re.escape(name)} \{{(.*?)(?=^function |\Z)", SCRIPT
    )
    if not match:
        raise AssertionError(f"PowerShell function not found: {name}")
    return match.group(1)


class WindowsSetupTests(unittest.TestCase):
    def test_disables_distribution_and_vm_idle_timeouts(self):
        body = function_body("Set-WslGlobalConfig")
        self.assertIn("instanceIdleTimeout=-1", body)
        self.assertIn("vmIdleTimeout=-1", body)
        self.assertIn("[general]", body)
        self.assertIn("[wsl2]", body)

    def test_keepalive_invokes_wsl_directly(self):
        body = function_body("Register-KeepaliveTask")
        self.assertIn("New-ScheduledTaskAction -Execute 'wsl.exe'", body)
        self.assertIn("--exec /bin/true", body)
        self.assertNotIn("-Execute 'powershell.exe'", body)
        self.assertIn("Start-ScheduledTask", body)
        self.assertIn("LastTaskResult", body)


if __name__ == "__main__":
    unittest.main()
