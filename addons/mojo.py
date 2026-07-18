"""Mojo addon for CRAFT — optional GPU Mojo mode.

Single file under ``addons/mojo.py``. Load with:

  %local
  %run /path/to/gpudev/CRAFT.py
  %run /path/to/gpudev/addons/mojo.py
  %gpu
  %gpum
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path

# Repo root (parent of addons/) so ``gpudev_craft`` imports work when %run this file
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Pull SSH helpers + router hooks from core (must install_core / CRAFT.py first)
from gpudev_craft import core as _core

_ssh = _core._ssh
_ssh_with_input = _core._ssh_with_input
read_msg = _core.read_msg
register_local_magic = _core.register_local_magic

# ── Mojo Execution Manager ────────────────────────────────────────────────────
_MOJO_NOISE = (
    "Failed to initialize Crashpad",
    "Crash reporting will not be available",
    "crashpad handler",
)


def _scrub_mojo_noise(text):
    if not text:
        return ""

    return "".join(
        ln
        for ln in text.splitlines(keepends=True)
        if not any(n in ln for n in _MOJO_NOISE)
    )


class RemoteMojoHelper:
    """Run / build / package Mojo inside the client container via pixi, over SSH.

    Project path is the per-client data volume (~/.mojo-proj), seeded from the
    image /opt/mojo-proj on first container start so %mojo_add survives rebuild.
    """

    PIXI = "/opt/pixi/bin/pixi"
    PROJ = "/home/gpudev/.mojo-proj"  # volume; start.sh seeds from /opt/mojo-proj
    PROJ_SEED = "/opt/mojo-proj"

    def _runner(self):
        # Ensure volume project exists (older containers pre-seed logic).
        ensure = (
            f"if [ ! -f {self.PROJ}/pixi.toml ] && [ -d {self.PROJ_SEED} ]; then "
            f"cp -a {self.PROJ_SEED} {self.PROJ}; fi"
        )
        return (
            f"{ensure} && "
            f"{self.PIXI} run --manifest-path {self.PROJ}/pixi.toml"
        )

    def run_source(self, src):
        _ssh_with_input(
            "mkdir -p /tmp/gpum && cat > /tmp/gpum/mojo_run.mojo",
            src,
            check=True,
        )

        return _ssh(
            f"{self._runner()} mojo run /tmp/gpum/mojo_run.mojo",
            capture_output=True,
            check=False,
        )

    def bench_source(self, src, n):
        _ssh_with_input(
            "mkdir -p /tmp/gpum && cat > /tmp/gpum/bench.mojo",
            src,
            check=True,
        )

        script = (
            "t0=$(date +%s%N); mojo build /tmp/gpum/bench.mojo -o /tmp/gpum/bench 1>&2 || exit 3\n"
            "echo COMPILE $(( $(date +%s%N) - t0 ))\n"
            "/tmp/gpum/bench >/dev/null 2>&1\n"
            f"for i in $(seq 1 {n}); do a=$(date +%s%N); /tmp/gpum/bench >/dev/null 2>&1; "
            "echo RUN $(( $(date +%s%N) - a )); done\n"
        )

        return _ssh_with_input(
            f"cat > /tmp/gpum/bench.sh && {self._runner()} bash /tmp/gpum/bench.sh",
            script,
            check=False,
        )

    def add_package(self, spec, pypi=False):
        flag = "--pypi " if pypi else ""
        ensure = (
            f"if [ ! -f {self.PROJ}/pixi.toml ] && [ -d {self.PROJ_SEED} ]; then "
            f"cp -a {self.PROJ_SEED} {self.PROJ}; fi"
        )
        return _ssh(
            f"{ensure} && "
            f"{self.PIXI} add --manifest-path {self.PROJ}/pixi.toml {flag}{spec}",
            capture_output=True,
            check=False,
        )


class MojoExecutionManager:
    def __init__(self, root="/tmp/craft-mojo", helper=None):
        self.helper = helper or RemoteMojoHelper()
        self.root = Path(root)
        self.root.mkdir(exist_ok=True)
        self.history_path = self.root / "history.json"
        self.run_path = self.root / "mojo_run.mojo"
        self._counter = 0

    def load_history(self):
        if not self.history_path.exists():
            return []
        return json.loads(self.history_path.read_text())

    def save_history(self, cells):
        self.history_path.write_text(json.dumps(cells, indent=2))

    def first_meaningful_line(self, code):
        for line in code.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                return s
        return ""

    def defined_symbols(self, code):
        return re.findall(
            r"^\s*(?:fn|def|struct|trait|class|alias)\s+([A-Za-z_]\w*)",
            code,
            re.M,
        )

    def assigned_symbols(self, code):
        return re.findall(
            r"^\s*(?:let|alias|comptime)\s+([A-Za-z_]\w*)\b",
            code,
            re.M,
        )

    def has_main(self, code):
        return bool(re.search(r"^\s*(?:fn|def)\s+main\s*\(", code, re.M))

    def cell_kind(self, code):
        if self.has_main(code):
            return "command"

        line = self.first_meaningful_line(code)

        if self.defined_symbols(code):
            return "code"

        if line.startswith(("from ", "import ")):
            return "code"

        if self.assigned_symbols(code):
            return "state"

        return "command"

    def is_mixed_cell(self, code):
        if self.has_main(code):
            return False

        return bool(self.defined_symbols(code)) and "print(" in code

    async def current_msg_id(self):
        try:
            msg = await read_msg(0)
            return msg["id"]
        except Exception:
            self._counter += 1
            return f"cell-{self._counter}"

    def upsert_cell(self, msg_id, code):
        cells = self.load_history()
        now = time.time()
        kind = self.cell_kind(code)

        entry = {
            "msg_id": msg_id,
            "updated_at": now,
            "kind": kind,
            "defines": self.defined_symbols(code),
            "assigns": self.assigned_symbols(code),
            "code": code,
        }

        for cell in cells:
            if cell.get("msg_id") == msg_id:
                cell.update(entry)
                self.save_history(cells)
                return

        entry["index"] = len(cells)
        entry["created_at"] = now
        cells.append(entry)
        self.save_history(cells)

    def latest_wins_cells(self, cells):
        seen = set()
        kept = []

        for cell in reversed(cells):
            symbols = set(cell.get("defines", [])) | set(cell.get("assigns", []))

            if not symbols:
                kept.append(cell)
                continue

            if symbols & seen:
                continue

            seen.update(symbols)
            kept.append(cell)

        return list(reversed(kept))

    def build_source(self, current_code, current_msg_id):
        cells = [c for c in self.load_history() if c.get("msg_id") != current_msg_id]

        persistent = self.latest_wins_cells(
            [c for c in cells if c.get("kind") in ("code", "state")]
        )

        current_kind = self.cell_kind(current_code)

        if current_kind in ("code", "state"):
            current_entry = {
                "kind": current_kind,
                "defines": self.defined_symbols(current_code),
                "assigns": self.assigned_symbols(current_code),
                "code": current_code,
            }

            persistent = self.latest_wins_cells(persistent + [current_entry])
            preamble = "\n\n".join(c["code"] for c in persistent)
            return preamble + "\n\ndef main():\n    pass\n"

        preamble = "\n\n".join(c["code"] for c in persistent)

        if self.has_main(current_code):
            return ((preamble + "\n\n") if preamble else "") + current_code.rstrip() + "\n"

        expr = current_code.strip()

        if "\n" not in expr and not expr.startswith(
            ("print(", "for ", "if ", "while ", "var ", "let ")
        ):
            body = "    print(" + expr + ")"
        else:
            body = "\n".join("    " + line for line in current_code.splitlines())

        return ((preamble + "\n\n") if preamble else "") + "def main():\n" + body + "\n"

    async def execute_mojo(self, code):
        if self.is_mixed_cell(code):
            raise ValueError(
                "Mojo cells shouldn't mix definitions and commands — "
                "put defs and print/calls in separate cells."
            )

        msg_id = await self.current_msg_id()
        src = self.build_source(code, msg_id)
        self.run_path.write_text(src)

        t0 = time.perf_counter()
        r = self.helper.run_source(src)
        dt = time.perf_counter() - t0

        if r.stdout:
            print(r.stdout, end="")

        err = _scrub_mojo_noise(r.stderr)
        if err:
            print(err, end="")

        print(f"[mojo run: {dt:.3f}s]")

        if r.returncode != 0:
            raise RuntimeError("mojo run failed")
        self.upsert_cell(msg_id, code)

    def restart_mojo(self):
        if self.root.exists():
            shutil.rmtree(self.root)

        self.root.mkdir(exist_ok=True)
        print("Mojo restarted: history + generated source cleared")

    def show_history(self):
        print(self.history_path.read_text() if self.history_path.exists() else "[]")

    def show_run(self):
        print(self.run_path.read_text() if self.run_path.exists() else "")

    def add_package(self, spec):
        if not spec:
            print("usage: %mojo_add <package> [...]   (conda channels first, PyPI auto-fallback)")
            return

        print(f"pixi add {spec} … (downloading)")
        r = self.helper.add_package(spec)
        out = _scrub_mojo_noise((r.stdout or "") + (r.stderr or ""))

        if r.returncode != 0 and "No candidates" in out and "--" not in spec:
            print("  not on conda channels — trying PyPI…")
            r = self.helper.add_package(spec, pypi=True)
            out = _scrub_mojo_noise((r.stdout or "") + (r.stderr or ""))

        print(out.strip() or (f"added: {spec}" if r.returncode == 0 else "add failed"))

    def bench(self, n=20):
        import statistics

        src = self.run_path.read_text() if self.run_path.exists() else ""

        if not self.has_main(src):
            print("Nothing to benchmark — run a %gpum command cell first.")
            return

        r = self.helper.bench_source(src, max(1, n))
        out = r.stdout or ""

        runs = sorted(
            int(l.split()[1])
            for l in out.splitlines()
            if l.startswith("RUN")
        )

        comp = next(
            (int(l.split()[1]) for l in out.splitlines() if l.startswith("COMPILE")),
            None,
        )

        if not runs:
            print("benchmark failed:")
            print(_scrub_mojo_noise(r.stderr or out) or "(no output)")
            return

        ms = lambda ns: ns / 1e6

        print(f"Mojo benchmark — {len(runs)} runs, warm-up discarded (compile excluded)")

        if comp is not None:
            print(f"  compile : {ms(comp):9.1f} ms  (once)")

        print(f"  min     : {ms(runs[0]):9.3f} ms")
        print(f"  median  : {ms(statistics.median(runs)):9.3f} ms")
        print(f"  mean    : {ms(statistics.mean(runs)):9.3f} ms")

        if len(runs) > 1:
            print(f"  stdev   : {ms(statistics.pstdev(runs)):9.3f} ms")



class MojoBackend:
    banner = "GPU Mojo mode — cells compiled & run in the GPU container"
    dispatch = "await _mojo_mgr.execute_mojo(ROUTER.backend.pending)"
    pending = None

    def passthru(self, c):
        s = c.lstrip()

        return (
            not s
            or s[0] in "%!?"
            or "get_ipython()" in c
            or "_mojo_mgr." in c
        )




_mojo_mgr = None
MOJO_BACKEND = None


def gpum(line=""):
    if not _core._ensure_connected():
        return
    if MOJO_BACKEND is None:
        install_mojo_addon(quiet=True)
    _core.ROUTER.set(MOJO_BACKEND)


def restart_mojo(line=""):
    if _mojo_mgr is None:
        print("Mojo not installed — %run addons/mojo.py first")
        return
    _mojo_mgr.restart_mojo()


def mojo_history(line=""):
    if _mojo_mgr:
        _mojo_mgr.show_history()


def mojo_run(line=""):
    if _mojo_mgr:
        _mojo_mgr.show_run()


def bench(line=""):
    if not _mojo_mgr:
        print("Mojo not installed")
        return
    arg = (line or "").strip()
    _mojo_mgr.bench(int(arg) if arg.isdigit() else 20)


def mojo_add(line=""):
    if not _mojo_mgr:
        print("Mojo not installed")
        return
    _mojo_mgr.add_package((line or "").strip())


_MOJO_MAGIC_FUNCS = (
    ("gpum", gpum),
    ("restart_mojo", restart_mojo),
    ("mojo_history", mojo_history),
    ("mojo_run", mojo_run),
    ("bench", bench),
    ("mojo_add", mojo_add),
)


def install_mojo_addon(*, quiet: bool = False) -> bool:
    """Register Mojo magics; requires install_core() first."""
    global _mojo_mgr, MOJO_BACKEND
    if _core._exec_mgr is None or _core.ROUTER is None:
        print("CRAFT: run install_core() / %run CRAFT.py first")
        return False
    if _mojo_mgr is None:
        _mojo_mgr = MojoExecutionManager()
    if MOJO_BACKEND is None:
        MOJO_BACKEND = MojoBackend()
    MOJO_BACKEND.dispatch = "await _mojo_mgr.execute_mojo(ROUTER.backend.pending)"
    _core._mojo_mgr = _mojo_mgr
    _core.MOJO_BACKEND = MOJO_BACKEND
    # local magics for host
    for name in ("%gpum", "%restart_mojo", "%mojo_history", "%mojo_run", "%bench", "%mojo_add"):
        register_local_magic(name)
    try:
        ip = _core.get_ipython()
        if ip is not None:
            mm = ip.magics_manager
            for name, fn in _MOJO_MAGIC_FUNCS:
                mm.register_function(fn, magic_kind="line", magic_name=name)
            ns = ip.user_ns
            ns["_mojo_mgr"] = _mojo_mgr
            ns["MOJO_BACKEND"] = MOJO_BACKEND
            for name, fn in _MOJO_MAGIC_FUNCS:
                ns[name] = fn
    except Exception as e:
        print(f"CRAFT mojo: magic register issue: {e}")
        return False
    if not quiet:
        print("CRAFT Mojo addon ready")
        print("  %gpum  %restart_mojo  %mojo_history  %mojo_run  %mojo_add  %bench")
    return True


# Auto-install when %run addons/mojo.py
try:
    install_mojo_addon(quiet=False)
except Exception as _e:
    print(f"CRAFT mojo: install failed: {_e}")
