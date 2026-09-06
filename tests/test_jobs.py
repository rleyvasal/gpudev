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
    def test_mutating_commands_take_the_lock(self):
        # Detaching removes the accidental serialization that came from the
        # operator waiting, so every mutation must serialize.
        source = GPUDEV.read_text()
        self.assertIn("remove)  with_lock cmd_client_remove", source)
        # The three that can detach lock themselves, past the detach decision.
        self.assertGreaterEqual(source.count("\n    take_lock"), 2)
        self.assertGreaterEqual(source.count("take_lock\n"), 4)

    def test_a_launcher_does_not_hold_the_lock_it_just_handed_off(self):
        # Locking at dispatch meant `image build cuda-dev --detach` blocked
        # behind the 13-minute build that `image build base --detach` had just
        # started — the launcher waiting for the work it delegated. Launchers
        # must return immediately.
        source = GPUDEV.read_text()
        for dispatched in (
            "shift; with_lock cmd_image",
            "shift 2; with_lock cmd_client_add",
            "shift 2; with_lock cmd_client_rebuild",
        ):
            with self.subTest(dispatched=dispatched):
                self.assertNotIn(dispatched, source)

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

    def test_waiting_for_the_lock_announces_itself(self):
        # Two queued jobs both report ActiveState=active — systemd only knows
        # the process exists — so a job waiting its turn is indistinguishable
        # from one doing work, and silence reads as a stall. Observed with two
        # real builds on the host.
        source = GPUDEV.read_text()
        self.assertIn("flock -n 9", source)
        self.assertIn("Waiting for another gpudev operation", source)

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
        # It must re-run THIS script by absolute path. A bare `gpudev` resolves
        # through PATH — which the systemd user manager need not have ~/bin on,
        # and which picks whatever copy is installed rather than the one that
        # launched the job. On a live host that handed the work to an older
        # gpudev, which rejected the arguments and exited 1.
        self.assertIn(str(GPUDEV), launched)
        self.assertNotRegex(launched, r"job-exec \S+ gpudev ")
        self.assertIn("client add bob", launched)
        self.assertIn("--variant cuda-dev", launched)
        self.assertIn("--key ssh-ed25519 AAAA test", launched)
        self.assertIn("--wait", launched)

    def test_the_job_runs_this_script_not_the_installed_copy(self):
        # run_detached copied the power scheduler's binary lookup, which
        # prefers ~/bin/gpudev. That is right for a timer firing hours later —
        # it should run whatever is current then — and wrong for a job, which
        # is a continuation of THIS invocation. On a live host it handed a
        # newer script's work to an older installed copy that rejected the
        # arguments and exited 1, twice, before the cause was found.
        #
        # Earlier versions of this test missed it because HOME was a temp dir
        # with no ~/bin/gpudev, so the fallback masked the bug. The installed
        # copy has to exist for the test to mean anything.
        installed = self.home / "bin" / "gpudev"
        installed.parent.mkdir(exist_ok=True)
        installed.write_text("#!/usr/bin/env bash\necho 'the OLD installed copy'\n")
        installed.chmod(0o755)

        self.add("bob", "--variant", "cuda-dev", "--key", "ssh-ed25519 AAAA test")
        launched = self.launched()[0]
        self.assertNotIn(str(installed), launched)
        self.assertIn(str(GPUDEV), launched)

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


class DeferredBaseImageTests(unittest.TestCase):
    """The install defers its base image build so it can be detached at all.

    Inside the installer the docker group is not active yet, so docker_probe
    falls back to `sudo docker` and sudo needs a TTY. After the mandatory
    reconnect the group is active and the same build needs no sudo.
    """

    SETUP = REPO_ROOT / "linux-setup.sh"

    def test_install_skips_the_build_unless_asked(self):
        source = self.SETUP.read_text()
        self.assertIn('if [ "$GPUDEV_BUILD_BASE_INLINE" = "1" ]; then', source)
        self.assertIn("gpudev Step 5: Base image (deferred)", source)
        self.assertIn("--build-base-image)", source)

    def test_there_is_a_sub_entry_point_to_build_it_later(self):
        # `gpudev image build base` needs something to call.
        source = self.SETUP.read_text()
        self.assertIn('"${1:-}" = "--build-base"', source)
        # Step 5b moves with it; the verification is useless without the image.
        entry = source[source.index('"${1:-}" = "--build-base"'):]
        entry = entry[:entry.index("--no-lockdown")]
        self.assertIn("build_base_image", entry)
        self.assertIn("verify_torch_cuda", entry)

    def test_the_closing_note_names_the_command_and_the_consequence(self):
        source = self.SETUP.read_text()
        self.assertIn("gpudev image build base --detach", source)
        self.assertIn("is NOT built yet", source)

    def test_gpudev_can_build_the_base_image(self):
        source = (REPO_ROOT / "gpudev").read_text()
        self.assertIn("bash \"$setup\" --build-base", source)
        # The old refusal would strand a host installed with the new default.
        self.assertNotIn("The default image is built by linux-setup.sh", source)

    def test_missing_base_image_advice_is_actionable(self):
        # For a missing base image, "Run linux-setup.sh first" became wrong: it
        # already ran, and running it again would not build the image either.
        for path in (REPO_ROOT / "gpudev", REPO_ROOT / "client-setup.sh"):
            with self.subTest(path=path.name):
                source = path.read_text()
                self.assertNotIn("Base image '$BASE_IMAGE' not found", source)
                self.assertNotIn("Base image '$image' not found", source)
                self.assertIn("gpudev image build base --detach", source)

    def test_never_installed_still_says_to_run_the_installer(self):
        # The other half of the distinction: a host with no host.json really
        # does need linux-setup.sh, and that advice must survive.
        source = (REPO_ROOT / "client-setup.sh").read_text()
        self.assertIn('Host not set up. Run linux-setup.sh first.', source)


class PrewarmDocsTests(unittest.TestCase):
    def test_guides_tell_the_admin_to_build_images_after_installing(self):
        # A build should be paid at an idle moment by the administrator, never
        # by the first user waiting on the line that finishes their onboarding.
        for doc in ("README.md", "LINUX-QUICKSTART.md"):
            with self.subTest(doc=doc):
                text = (REPO_ROOT / doc).read_text()
                self.assertIn("gpudev image build base --detach", text)
                self.assertIn("gpudev image build cuda-dev --detach", text)
                self.assertIn("client add", text)


class InstallerFlagTests(unittest.TestCase):
    SETUP = REPO_ROOT / "linux-setup.sh"

    def run_setup(self, *args):
        return subprocess.run(
            ["bash", str(self.SETUP), *args], capture_output=True, text=True
        )

    def test_flags_are_accepted_in_either_order(self):
        # Positional parsing silently ignored whichever flag came second, and a
        # silently ignored --build-base-image would defer a build the operator
        # asked to run inline.
        for args in (
            ["--no-lockdown"],
            ["--build-base-image"],
            ["--no-lockdown", "--build-base-image"],
            ["--build-base-image", "--no-lockdown"],
        ):
            with self.subTest(args=args):
                # Stops at a later precondition (sudo/OS), never at parsing.
                self.assertNotIn("Unknown option", self.run_setup(*args).stderr)

    def test_an_unknown_flag_is_refused(self):
        result = self.run_setup("--bogus")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown option", result.stderr)
