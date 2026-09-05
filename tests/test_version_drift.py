from __future__ import annotations

import io
import os
import re
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from gpudev_craft import core


REPO_ROOT = Path(__file__).resolve().parents[1]

CLIENT_SHA = "1111111111111111111111111111111111111111"
OTHER_SHA = "2222222222222222222222222222222222222222"


class ClientVersionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.version_file = Path(self._tmp.name) / "VERSION"
        self._saved = core.VERSION_FILE
        core.VERSION_FILE = self.version_file
        core._VERSION_CHECKED.clear()

    def tearDown(self):
        core.VERSION_FILE = self._saved
        core._VERSION_CHECKED.clear()
        self._tmp.cleanup()

    def write_version(self, sha: str) -> None:
        self.version_file.write_text(
            f"sha {sha}\nref main\nslug rleyvasal/gpudev\n"
            f"abc  CRAFT.py\n"
        )

    def test_client_version_reads_the_sha_line(self):
        self.write_version(CLIENT_SHA)
        self.assertEqual(core.client_version(), CLIENT_SHA)

    def test_client_version_is_empty_without_a_version_file(self):
        self.assertEqual(core.client_version(), "")


class DriftWarningTests(unittest.TestCase):
    """The two halves are updated by different people, so drift is expected.

    The check exists to name it, never to block a working session.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.version_file = Path(self._tmp.name) / "VERSION"
        self._saved_version = core.VERSION_FILE
        self._saved_ssh = core._ssh
        core.VERSION_FILE = self.version_file
        core._VERSION_CHECKED.clear()

    def tearDown(self):
        core.VERSION_FILE = self._saved_version
        core._ssh = self._saved_ssh
        core._VERSION_CHECKED.clear()
        self._tmp.cleanup()

    def stub_container(self, stdout: str):
        calls = []

        def fake_ssh(cmd, capture_output=False, check=True, **kw):
            calls.append(cmd)
            return SimpleNamespace(stdout=stdout, returncode=0)

        core._ssh = fake_ssh
        return calls

    def warn(self, name="alice") -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            core._warn_on_version_drift(name)
        return buf.getvalue()

    def test_mismatch_warns_and_names_both_fixes(self):
        self.version_file.write_text(f"sha {CLIENT_SHA}\n")
        self.stub_container(OTHER_SHA)

        out = self.warn()
        self.assertIn(CLIENT_SHA[:7], out)
        self.assertIn(OTHER_SHA[:7], out)
        # Both halves are somebody's responsibility; naming only one leaves the
        # reader unable to act on the other.
        self.assertIn("gpudev client rebuild alice", out)
        self.assertIn("re-run the bootstrap cell", out)

    def test_matching_versions_say_nothing(self):
        self.version_file.write_text(f"sha {CLIENT_SHA}\n")
        self.stub_container(CLIENT_SHA)
        self.assertEqual(self.warn(), "")

    def test_no_client_version_says_nothing(self):
        # A pre-bootstrap install (a git clone) has no VERSION. Unknown is not
        # a mismatch, and warning here would fire for every existing user.
        self.stub_container(OTHER_SHA)
        self.assertEqual(self.warn(), "")

    def test_unstamped_container_says_nothing(self):
        # An older container has no `version` subcommand: it prints usage and
        # exits nonzero. That is unknown, not mismatched.
        self.version_file.write_text(f"sha {CLIENT_SHA}\n")
        for reply in ("", "Usage: kernel-manager.sh <command>", "not-a-sha"):
            with self.subTest(reply=reply):
                core._VERSION_CHECKED.clear()
                self.stub_container(reply)
                self.assertEqual(self.warn(), "")

    def test_checks_once_per_client_per_session(self):
        self.version_file.write_text(f"sha {CLIENT_SHA}\n")
        calls = self.stub_container(OTHER_SHA)

        self.assertNotEqual(self.warn(), "")
        self.assertEqual(self.warn(), "")
        self.assertEqual(len(calls), 1)
        # A different client is a different container, so it is checked too.
        self.assertNotEqual(self.warn("bob"), "")
        self.assertEqual(len(calls), 2)

    def test_a_broken_connection_is_not_reported_here(self):
        self.version_file.write_text(f"sha {CLIENT_SHA}\n")

        def boom(*a, **kw):
            raise RuntimeError("connection lost")

        core._ssh = boom
        self.assertEqual(self.warn(), "")


class KernelManagerVersionTests(unittest.TestCase):
    """`kernel-manager.sh version` is the container half of the comparison."""

    def run_version(self, home: Path):
        script = REPO_ROOT / "kernel-manager.sh"
        # The script derives HOME_DIR from a fixed container path, so point it
        # at the temp tree by overriding that one assignment.
        source = script.read_text().replace(
            'HOME_DIR="/home/${CONTAINER_USER}"', f'HOME_DIR="{home}"'
        )
        patched = home / "km.sh"
        patched.write_text(source)
        patched.chmod(patched.stat().st_mode | stat.S_IEXEC)
        return subprocess.run(
            ["bash", str(patched), "version"], capture_output=True, text=True
        )

    def test_reports_the_stamp_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "bin").mkdir()
            (home / "bin" / "VERSION").write_text(f"{CLIENT_SHA}\n")

            result = self.run_version(home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), CLIENT_SHA)

    def test_unstamped_exits_nonzero_and_prints_nothing(self):
        # Reporting "unknown" as a version string would make the client compare
        # against a value that is not a commit. Silence plus nonzero is how a
        # container built before this existed says it does not know.
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "bin").mkdir()

            result = self.run_version(home)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "")

    def test_version_is_listed_in_usage(self):
        source = (REPO_ROOT / "kernel-manager.sh").read_text()
        usage = source[source.index("usage() {"):source.index("case \"${1:-}\"")]
        self.assertIn("version", usage)


class StampSitesTests(unittest.TestCase):
    def test_every_kernel_manager_install_site_also_stamps(self):
        # kernel-manager.sh reaches a container from two places: client-setup.sh
        # on create, and `gpudev client rebuild`. A site that refreshed the
        # script but left the old stamp would make the drift warning actively
        # wrong — worse than having no stamp at all.
        sites = []
        for name in ("client-setup.sh", "gpudev"):
            text = (REPO_ROOT / name).read_text()
            for match in re.finditer(r"cp /tmp/kernel-manager\.sh (\S+)", text):
                window = text[match.start():match.start() + 600]
                sites.append((name, "VERSION" in window))

        self.assertEqual(len(sites), 2, f"expected two install sites, found {sites}")
        for name, stamps in sites:
            self.assertTrue(stamps, f"{name} installs kernel-manager.sh without stamping")


if __name__ == "__main__":
    unittest.main()
