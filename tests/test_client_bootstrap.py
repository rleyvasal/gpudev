from __future__ import annotations

import os
import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO_ROOT / "client-bootstrap.sh"

# The ten files a notebook actually runs. The whole point of the bootstrap is
# that this list — not the repository — is what reaches a client.
REQUIRED = (
    "CRAFT.py",
    "gpudev_craft/__init__.py",
    "gpudev_craft/core.py",
    "gpudev_craft/client_setup.py",
    "gpudev_craft/magics.py",
)

FAKE_SHA = "a" * 40


def build_fake_tarball(path: Path, *, sha: str = FAKE_SHA, omit: str = "") -> None:
    """A tarball shaped like GitHub's: one top dir named owner-repo-<short sha>."""
    top = f"rleyvasal-gpudev-{sha[:7]}"
    with tarfile.open(path, "w:gz") as tar:
        members = [
            *REQUIRED,
            "addons/pcviz.py",
            # Host-side files the client must never receive.
            "linux-setup.sh",
            "gpudev",
            "tests/test_thing.py",
        ]
        for rel in members:
            if rel == omit:
                continue
            blob = f"# {rel}\nvalue = 1\n".encode()
            src = path.parent / "stage" / rel
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_bytes(blob)
            tar.add(src, arcname=f"{top}/{rel}")


class FakeServer:
    """Stands in for curl, so these tests never touch the network.

    The script shells out to curl twice: once to resolve the ref to a SHA, once
    for the tarball. A fake curl on PATH is the seam.
    """

    def __init__(self, root: Path, tarball: Path, *, sha: str = FAKE_SHA):
        self.bin = root / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        curl = self.bin / "curl"
        curl.write_text(
            "#!/usr/bin/env bash\n"
            "# Resolve-the-ref request: the URL contains /commits/.\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in\n"
            "    */commits/*)\n"
            f"      if [ -n \"${{FAKE_BAD_REF:-}}\" ]; then exit 22; fi\n"
            f"      printf '%s' '{sha}'; exit 0 ;;\n"
            "  esac\n"
            "done\n"
            "# Tarball request: -o names the destination.\n"
            "prev=\"\"\n"
            "for a in \"$@\"; do\n"
            "  if [ \"$prev\" = \"-o\" ]; then\n"
            f"    cp '{tarball}' \"$a\"\n"
            "    if [ -n \"${FAKE_TRUNCATE:-}\" ]; then\n"
            "      head -c \"$FAKE_TRUNCATE\" \"$a\" > \"$a.t\" && mv \"$a.t\" \"$a\"\n"
            "    fi\n"
            "    exit 0\n"
            "  fi\n"
            "  prev=\"$a\"\n"
            "done\n"
            "exit 1\n"
        )
        curl.chmod(0o755)

    def env(self, **extra: str) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env.update(extra)
        return env


def run_bootstrap(env, *args):
    return subprocess.run(
        ["sh", str(BOOTSTRAP), *args], env=env, capture_output=True, text=True
    )


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tarball = self.root / "repo.tgz"
        build_fake_tarball(self.tarball)
        self.server = FakeServer(self.root, self.tarball)
        self.install = self.root / "install" / "gpudev"

    def tearDown(self):
        self._tmp.cleanup()

    def env(self, **extra):
        return self.server.env(GPUDEV_DIR=str(self.install), **extra)

    def test_installs_only_the_client_manifest(self):
        result = run_bootstrap(self.env())
        self.assertEqual(result.returncode, 0, result.stderr)

        got = {
            str(p.relative_to(self.install))
            for p in self.install.rglob("*")
            if p.is_file()
        }
        self.assertEqual(got, {*REQUIRED, "addons/pcviz.py", "VERSION"})

        # The point of the manifest: host-side code never reaches a notebook.
        for host_only in ("linux-setup.sh", "gpudev", "tests/test_thing.py"):
            self.assertNotIn(host_only, got)

    def test_version_records_the_commit_and_hashes(self):
        run_bootstrap(self.env())
        version = (self.install / "VERSION").read_text()
        self.assertIn(f"sha {FAKE_SHA}", version)
        self.assertIn("ref main", version)
        # A hash line per python file, in `sha256  path` form.
        hashes = re.findall(r"^([0-9a-f]{64})  (\S+)$", version, re.M)
        self.assertEqual(len(hashes), 6)

    def test_rerun_is_a_no_op_when_the_sha_matches(self):
        run_bootstrap(self.env())
        stamp = (self.install / "CRAFT.py").stat().st_mtime_ns

        second = run_bootstrap(self.env())
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Nothing to do", second.stdout)
        # A working install is never swapped for an identical one: re-running
        # the cell is the documented way to pick up fixes, so it must be cheap
        # and must not churn a live tree.
        self.assertEqual((self.install / "CRAFT.py").stat().st_mtime_ns, stamp)

    def test_force_refetches_when_already_current(self):
        run_bootstrap(self.env())
        result = run_bootstrap(self.env(), "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installed gpudev client runtime", result.stdout)

    def test_verify_passes_then_detects_truncation(self):
        run_bootstrap(self.env())

        ok = run_bootstrap(self.env(), "--verify")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("all files match", ok.stdout)

        # Truncation is the failure `py_compile` cannot see: a source file cut
        # at an arbitrary offset still parses, so only hashing catches it.
        core = self.install / "gpudev_craft" / "core.py"
        core.write_bytes(core.read_bytes()[:8])

        bad = run_bootstrap(self.env(), "--verify")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("CHANGED  gpudev_craft/core.py", bad.stdout)
        self.assertIn("--force", bad.stdout)

    def test_force_repairs_what_verify_found(self):
        run_bootstrap(self.env())
        core = self.install / "gpudev_craft" / "core.py"
        core.write_bytes(b"truncated")

        run_bootstrap(self.env(), "--force")
        after = run_bootstrap(self.env(), "--verify")
        self.assertEqual(after.returncode, 0, after.stdout)

    def test_truncated_tarball_never_reaches_the_install(self):
        run_bootstrap(self.env())
        good = (self.install / "gpudev_craft" / "core.py").read_bytes()

        # gzip's CRC catches this, but tar still writes partial files before
        # failing — so the guarantee comes from staging plus honouring the exit
        # status, not from tar alone.
        size = self.tarball.stat().st_size
        for cut in (60, size // 2, size - 40):
            with self.subTest(cut=cut):
                result = run_bootstrap(
                    self.env(FAKE_TRUNCATE=str(cut)), "--force"
                )
                self.assertNotEqual(result.returncode, 0)
                # Whichever layer catches it, the message must say the download
                # was damaged — never blame a stale CDN, which sends the reader
                # after an unrelated problem.
                self.assertIn("Nothing was changed", result.stderr)
                self.assertNotIn("stale CDN", result.stderr)
                self.assertEqual(
                    (self.install / "gpudev_craft" / "core.py").read_bytes(), good
                )

    def test_wrong_tree_is_rejected_before_the_swap(self):
        # A tarball whose top directory does not match the resolved SHA means a
        # stale CDN response or a ref that moved between the two requests.
        # Nothing is corrupt, so only the identity check can see it.
        mismatched = self.root / "mismatched.tgz"
        build_fake_tarball(mismatched, sha="b" * 40)
        server = FakeServer(self.root / "srv2", mismatched, sha=FAKE_SHA)
        env = server.env(GPUDEV_DIR=str(self.install))

        result = run_bootstrap(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale CDN or a moved ref", result.stderr)
        self.assertFalse(self.install.exists())

    def test_incomplete_manifest_is_rejected_before_the_swap(self):
        # An upstream rename extracts cleanly and exits 0. Completeness is the
        # only layer that notices.
        partial = self.root / "partial.tgz"
        build_fake_tarball(partial, omit="gpudev_craft/core.py")
        server = FakeServer(self.root / "srv3", partial)
        env = server.env(GPUDEV_DIR=str(self.install))

        result = run_bootstrap(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gpudev_craft/core.py", result.stderr)
        self.assertFalse(self.install.exists())

    def test_unresolvable_ref_leaves_an_existing_install_alone(self):
        run_bootstrap(self.env())
        before = (self.install / "VERSION").read_text()

        result = run_bootstrap(self.env(FAKE_BAD_REF="1", GPUDEV_REF="nope"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not resolve", result.stderr)
        self.assertEqual((self.install / "VERSION").read_text(), before)

    def test_no_temp_directories_are_left_behind(self):
        run_bootstrap(self.env())
        run_bootstrap(self.env(FAKE_TRUNCATE="120", GPUDEV_REF="x"), "--force")
        leftovers = [
            p.name
            for p in self.install.parent.iterdir()
            if p.name.startswith(".gpudev-boot") or p.name.endswith((".new", ".old"))
        ]
        self.assertEqual(leftovers, [])

    def test_default_install_is_home_relative_with_no_symlink(self):
        # SolveIt's persistent storage IS the home directory (`cd ~` lands in
        # /app/data there), so a home-relative default persists exactly as the
        # old hardcoded path did — and is also correct on a local Jupyter or
        # Colab, which have no such path. In this case ~/.gpudev-client is the
        # install itself, so no symlink should exist at all.
        home = self.root / "home"
        home.mkdir()
        env = self.server.env(HOME=str(home))  # deliberately no GPUDEV_DIR

        result = run_bootstrap(env)
        self.assertEqual(result.returncode, 0, result.stderr)

        install = home / ".gpudev-client"
        self.assertTrue(install.is_dir())
        self.assertFalse(install.is_symlink())
        self.assertTrue((install / "CRAFT.py").is_file())
        self.assertIn("%run ~/.gpudev-client/CRAFT.py", result.stdout)
        # No environment-specific path anywhere in the output.
        self.assertNotIn("/app/data", result.stdout)

    def test_upgrade_replaces_a_leftover_symlink_and_keeps_the_old_tree(self):
        # Installs from before the default moved left ~/.gpudev-client as a
        # SYMLINK. Renaming a directory onto a symlink would put the tree
        # inside the link's target instead of replacing it.
        home = self.root / "home"
        old = home / "gpudevd" / "gpudev"
        old.mkdir(parents=True)
        (old / "CRAFT.py").write_text("old install\n")
        (home / ".gpudev-client").symlink_to(old)

        result = run_bootstrap(self.server.env(HOME=str(home)))
        self.assertEqual(result.returncode, 0, result.stderr)

        entry = home / ".gpudev-client"
        self.assertFalse(entry.is_symlink())
        self.assertTrue(entry.is_dir())
        self.assertNotIn("old install", (entry / "CRAFT.py").read_text())
        # The old copy is named, not silently deleted — it is not this
        # script's tree to remove.
        self.assertEqual((old / "CRAFT.py").read_text(), "old install\n")
        self.assertIn(str(old), result.stdout)

    def test_stable_entry_point_points_at_the_install(self):
        # GPUDEV_DIR is configurable, but the cell's second line is a `%run`
        # with a literal path, and IPython expands $VAR from the PYTHON
        # namespace rather than the environment — so `%run $GPUDEV_DIR/...`
        # cannot work, and a `!` line cannot set a Python variable either.
        # The symlink is what lets the cell stay correct wherever you install.
        home = self.root / "home"
        home.mkdir()
        env = self.env(HOME=str(home))

        result = run_bootstrap(env)
        self.assertEqual(result.returncode, 0, result.stderr)

        link = home / ".gpudev-client"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), self.install.resolve())
        self.assertTrue((link / "CRAFT.py").is_file())
        # The cell it tells you to paste must use the stable path, not the
        # install path — otherwise the advice reintroduces the problem.
        self.assertIn("%run ~/.gpudev-client/CRAFT.py", result.stdout)

    def test_entry_point_follows_a_relocated_install(self):
        home = self.root / "home"
        home.mkdir()
        elsewhere = self.root / "elsewhere" / "gpudev"

        run_bootstrap(self.env(HOME=str(home)))
        run_bootstrap(self.server.env(HOME=str(home), GPUDEV_DIR=str(elsewhere)))

        self.assertEqual((home / ".gpudev-client").resolve(), elsewhere.resolve())

    def test_a_real_directory_at_the_entry_path_is_never_replaced(self):
        home = self.root / "home"
        (home / ".gpudev-client").mkdir(parents=True)
        (home / ".gpudev-client" / "mine.txt").write_text("do not delete")

        result = run_bootstrap(self.env(HOME=str(home)))
        self.assertEqual(result.returncode, 0, result.stderr)
        # Left intact, and the advice falls back to the literal path.
        self.assertEqual(
            (home / ".gpudev-client" / "mine.txt").read_text(), "do not delete"
        )
        self.assertFalse((home / ".gpudev-client").is_symlink())
        self.assertIn(f"%run {self.install}/CRAFT.py", result.stdout)

    def test_missing_home_fails_fast_rather_than_guessing(self):
        # The default install path is home-relative, so an unset HOME has no
        # sane fallback. Failing before anything is fetched beats installing
        # somewhere arbitrary that the cell's line 2 will not find.
        env = self.env()
        env.pop("HOME", None)
        result = run_bootstrap(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HOME", result.stderr)

    def test_patterns_are_not_glob_expanded_by_the_shell(self):
        # `$PATTERNS` must reach tar literally. Unquoted and unguarded, the
        # shell expands `*/CRAFT.py` against the CURRENT directory first and
        # hands tar whatever matched there — which silently works in a clean
        # directory and breaks in a dirty one.
        decoy = self.root / "cwd"
        (decoy / "gpudev_craft").mkdir(parents=True)
        (decoy / "CRAFT.py").write_text("decoy\n")
        (decoy / "gpudev_craft" / "core.py").write_text("decoy\n")

        result = subprocess.run(
            ["sh", str(BOOTSTRAP)],
            env=self.env(),
            cwd=decoy,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("decoy", (self.install / "CRAFT.py").read_text())


if __name__ == "__main__":
    unittest.main()
