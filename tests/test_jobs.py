from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
GPUDEV = REPO_ROOT / "gpudev"


class JobsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        cfg = self.home / ".config" / "gpudev"
        cfg.mkdir(parents=True)
        (cfg / "host.json").write_text(
            json.dumps({"cf_domain": "example.com", "linux_user": "gpudev"})
        )
        (cfg / "clients.json").write_text(json.dumps({"clients": []}))
        self.jobs_dir = cfg / "jobs"

    def tearDown(self):
        self._tmp.cleanup()

    def run_gpudev(self, *args):
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        return subprocess.run(
            [str(GPUDEV), *args], env=env, capture_output=True, text=True
        )

    def result_for(self, unit):
        return json.loads((self.jobs_dir / f"{unit}.json").read_text())


class JobResultTests(JobsTestCase):
    def test_success_records_the_line_the_operator_must_forward(self):
        # The point of a result file rather than the journal: the %gpudev line
        # is a value to act on, not log text to grep back out.
        self.run_gpudev(
            "job-exec", "gpudev-job-add-alice-1",
            "bash", "-c", 'echo "  %gpudev alice --hostname alice.example.com"',
        )
        r = self.result_for("gpudev-job-add-alice-1")
        self.assertEqual(r["exit"], 0)
        self.assertEqual(r["summary"], "%gpudev alice --hostname alice.example.com")
        self.assertTrue(r["started"] and r["finished"])

    def test_failure_is_recorded_not_lost(self):
        # `set -euo pipefail` aborted before write_job_result ran, so a failed
        # job left NO record — losing exactly the case an operator needs to
        # find when they come back.
        result = self.run_gpudev(
            "job-exec", "gpudev-job-build-cuda-dev-2",
            "bash", "-c", 'echo boom >&2; exit 3',
        )
        self.assertEqual(result.returncode, 3)
        r = self.result_for("gpudev-job-build-cuda-dev-2")
        self.assertEqual(r["exit"], 3)

    def test_exit_code_is_propagated_to_the_caller(self):
        self.assertEqual(
            self.run_gpudev("job-exec", "gpudev-job-t-3", "bash", "-c", "exit 7").returncode,
            7,
        )

    def test_job_exec_rejects_missing_arguments(self):
        self.assertNotEqual(self.run_gpudev("job-exec").returncode, 0)
        self.assertNotEqual(self.run_gpudev("job-exec", "gpudev-job-x-1").returncode, 0)


class JobsListingTests(JobsTestCase):
    def test_finished_jobs_list_even_without_systemd(self):
        # Results are files, so they are readable anywhere. Only the running
        # set needs systemd; an early return on "no systemd" hid recorded
        # results that were sitting right there.
        self.run_gpudev(
            "job-exec", "gpudev-job-add-alice-1",
            "bash", "-c", 'echo "%gpudev alice --hostname alice.example.com"',
        )
        out = self.run_gpudev("jobs").stdout
        self.assertIn("gpudev-job-add-alice-1", out)
        self.assertIn("%gpudev alice --hostname alice.example.com", out)

    def test_failed_jobs_are_visibly_distinct(self):
        self.run_gpudev("job-exec", "gpudev-job-b-2", "bash", "-c", "exit 4")
        out = self.run_gpudev("jobs").stdout
        self.assertIn("FAILED", out)
        self.assertIn("4", out)

    def test_empty_state_says_so(self):
        out = self.run_gpudev("jobs").stdout
        self.assertIn("None recorded", out)

    def test_logs_and_cancel_require_a_unit(self):
        for sub in ("logs", "cancel"):
            with self.subTest(sub=sub):
                r = self.run_gpudev("jobs", sub)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("Usage", r.stderr)

    def test_unknown_subcommand_is_rejected(self):
        self.assertNotEqual(self.run_gpudev("jobs", "bogus").returncode, 0)


class DashboardTests(JobsTestCase):
    """`gpudev status` auto-runs on interactive login, so it is where an
    operator learns how a job they walked away from turned out."""

    SECTION_FUNCS = (
        "jobs_supported", "job_active_units", "job_result_files",
        "job_finished_rows", "status_jobs_section",
    )

    def render_section(self):
        source = GPUDEV.read_text().splitlines()
        out, keep = [], False
        for line in source:
            if any(line.startswith(f"{fn}() {{") for fn in self.SECTION_FUNCS):
                keep = True
            if keep:
                out.append(line)
            if keep and line == "}":
                keep = False
        harness = self.home / "section.sh"
        harness.write_text("\n".join(out))

        script = (
            f'CONFIG_DIR="$HOME/.config/gpudev"; GPUDEV_JOBS_DIR="$CONFIG_DIR/jobs"\n'
            'command_exists() { command -v "$1" >/dev/null 2>&1; }\n'
            f'source "{harness}"\n'
            "status_jobs_section\n"
        )
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        return subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True
        ).stdout

    def test_section_is_called_from_the_dashboard(self):
        body, keep = [], False
        for line in GPUDEV.read_text().splitlines():
            if line.startswith("cmd_status() {"):
                keep = True
            if keep:
                body.append(line)
            if keep and line == "}":
                break
        self.assertIn("    status_jobs_section", body)

    def test_section_is_silent_with_no_jobs(self):
        self.assertEqual(self.render_section().strip(), "")

    def test_section_shows_the_line_to_forward(self):
        self.run_gpudev(
            "job-exec", "gpudev-job-add-alice-1",
            "bash", "-c", 'echo "%gpudev alice --hostname alice.example.com"',
        )
        out = self.render_section()
        self.assertIn("Jobs:", out)
        self.assertIn("send back: %gpudev alice --hostname alice.example.com", out)

    def test_section_marks_a_failure(self):
        self.run_gpudev("job-exec", "gpudev-job-b-9", "bash", "-c", "exit 5")
        out = self.render_section()
        self.assertIn("FAILED (exit 5)", out)


class LockTests(JobsTestCase):
    def test_mutating_commands_are_wrapped_in_the_lock(self):
        # Detaching removes the accidental serialization that came from the
        # operator waiting, so every mutating dispatch must take the lock.
        source = GPUDEV.read_text()
        for dispatch in (
            "add)     shift 2; with_lock cmd_client_add",
            "remove)  with_lock cmd_client_remove",
            "rebuild) shift 2; with_lock cmd_client_rebuild",
            "with_lock cmd_image",
        ):
            with self.subTest(dispatch=dispatch):
                self.assertIn(dispatch, source)

    def test_read_only_commands_do_not_take_the_lock(self):
        source = GPUDEV.read_text()
        for readonly in ("with_lock cmd_status", "with_lock cmd_client_list",
                         "with_lock cmd_client_info"):
            with self.subTest(readonly=readonly):
                self.assertNotIn(readonly, source)

    def test_missing_flock_warns_but_still_runs(self):
        # macOS has no flock and the suite runs there. Silently skipping the
        # lock would claim a guarantee that is not held, so it must say so and
        # still do the work.
        if shutil.which("flock"):
            self.skipTest("flock is present here; the degraded path cannot run")
        result = self.run_gpudev("client", "remove", "nosuch", "--yes")
        self.assertIn("flock unavailable", result.stderr)
        # Degraded, not disabled: the command still reached its real work.
        self.assertIn("not found", result.stderr)

    def test_lock_degradation_is_a_warning_not_a_refusal(self):
        source = GPUDEV.read_text()
        self.assertIn("flock unavailable", source)
        self.assertIn("GPUDEV_LOCK_WAIT", source)


if __name__ == "__main__":
    unittest.main()


class DetachDecisionTests(JobsTestCase):
    """Detaching follows duration: slow work goes to the background, fast work
    stays in front of the operator who is waiting to read its output."""

    def setUp(self):
        super().setUp()
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.state = self.home / "state"
        self.state.mkdir()
        (self.home / ".config" / "gpudev" / "host.json").write_text(
            json.dumps({"cf_domain": "example.com", "linux_user": "gpudev",
                        "port_base": 52200})
        )
        # systemd present, but launches nothing — just records the request.
        (self.bin / "systemd-run").write_text(
            "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$TEST_STATE/launched\"\n"
        )
        (self.bin / "systemctl").write_text("#!/usr/bin/env bash\nexit 0\n")
        # base image built, cuda-dev not.
        (self.bin / "docker").write_text(
            '#!/usr/bin/env bash\n'
            'if [ "$1 $2" = "image inspect" ]; then\n'
            '  [ "$3" = "gpudev-base:latest" ] && exit 0\n'
            '  exit 1\n'
            'fi\n'
            'exit 0\n'
        )
        for f in self.bin.iterdir():
            f.chmod(0o755)

    def add(self, *args, **env_extra):
        env = os.environ.copy()
        env.update({
            "HOME": str(self.home),
            "PATH": f"{self.bin}:{env['PATH']}",
            "TEST_STATE": str(self.state),
        })
        env.update(env_extra)
        return subprocess.run(
            [str(GPUDEV), "client", "add", *args],
            env=env, capture_output=True, text=True,
        )

    def launched(self):
        f = self.state / "launched"
        return f.read_text().splitlines() if f.exists() else []

    def test_fast_path_stays_in_the_foreground(self):
        # The image is present, so this is seconds. Detaching it would bury the
        # %gpudev line the operator has to forward.
        self.add("alice", "--key", "ssh-ed25519 AAAA test")
        self.assertEqual(self.launched(), [])

    def test_missing_image_detaches_automatically(self):
        result = self.add("bob", "--variant", "cuda-dev", "--key", "ssh-ed25519 AAAA test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("safe to disconnect", result.stdout)
        self.assertEqual(len(self.launched()), 1)

    def test_the_detached_copy_gets_everything_it_needs(self):
        # It is a fresh `client add`, not a resumption, so the key must travel
        # with it — including a key that arrived by prompt or --key-file.
        self.add("bob", "--variant", "cuda-dev", "--key", "ssh-ed25519 AAAA test")
        launched = self.launched()[0]
        self.assertIn("client add bob", launched)
        self.assertIn("--variant cuda-dev", launched)
        self.assertIn("--key ssh-ed25519 AAAA test", launched)
        self.assertIn("--wait", launched)

    def test_wait_beats_the_automatic_decision(self):
        # --wait once lost to auto-detect, so an operator who asked to watch
        # the build was detached anyway.
        self.add("bob", "--variant", "cuda-dev", "--key", "ssh-ed25519 AAAA test", "--wait")
        self.assertEqual(self.launched(), [])

    def test_a_detached_copy_never_detaches_again(self):
        self.add("bob", "--variant", "cuda-dev", "--key", "ssh-ed25519 AAAA test",
                 GPUDEV_IN_JOB="1")
        self.assertEqual(self.launched(), [])

    def test_detach_forces_background_even_when_fast(self):
        self.add("alice", "--key", "ssh-ed25519 AAAA test", "--detach")
        self.assertEqual(len(self.launched()), 1)

    def test_contradictory_flags_are_refused(self):
        for cmd in (
            ["client", "add", "a", "--key", "ssh-ed25519 AAAA t", "--detach", "--wait"],
            ["client", "rebuild", "a", "--detach", "--wait"],
            ["image", "build", "cuda-dev", "--detach", "--wait"],
        ):
            with self.subTest(cmd=cmd):
                env = os.environ.copy()
                env.update({"HOME": str(self.home),
                            "PATH": f"{self.bin}:{env['PATH']}",
                            "TEST_STATE": str(self.state)})
                r = subprocess.run([str(GPUDEV), *cmd], env=env,
                                   capture_output=True, text=True)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("contradictory", r.stderr)
