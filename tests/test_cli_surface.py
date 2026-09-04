from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GPUDEV = (ROOT / "gpudev").read_text(encoding="utf-8")

# `gpudev help` and the login dashboard are two hand-maintained lists, which is
# how `gpudev ssh ...` came to exist in one and not the other: the commands that
# disable password login and recover from it were missing from the canonical
# reference. A curated list is a claim about what the tool can do, so a command
# the dispatcher accepts but help never mentions is a wrong claim.
#
# Listing "help" inside help would be noise, so it is exempt.
_EXEMPT = {"help", "-h", "--help"}


def usage_text() -> str:
    """The heredoc `usage()` prints — what `gpudev help` shows."""
    start = GPUDEV.index("usage() {")
    end = GPUDEV.index("\nEOF\n", start)
    return GPUDEV[start:end]


def main_body() -> str:
    start = GPUDEV.index("main() {")
    return GPUDEV[start:]


def dispatch_commands() -> list[str]:
    """Every command `main()` accepts, as the string a user would type.

    Top-level labels sit at 8 spaces; a noun that dispatches inline nests its
    sub-commands at 16. Nouns that delegate to a cmd_* function (image, kernel,
    power, cloudflare) are covered at noun level only — their sub-commands live
    in those functions, not here.
    """
    body = main_body()
    commands: list[str] = []
    noun = ""
    for line in body.splitlines():
        top = re.match(r"^ {8}([a-z][a-z|_-]*)\)", line)
        if top:
            noun = top.group(1)
            for alias in noun.split("|"):
                if alias not in _EXEMPT:
                    commands.append(alias)
            continue
        nested = re.match(r"^ {16}([a-z][a-z|_-]*)\)", line)
        if nested and noun:
            for alias in nested.group(1).split("|"):
                commands.append(f"{noun} {alias}")
    return commands


class CliSurfaceTests(unittest.TestCase):
    def test_every_dispatched_command_appears_in_help(self):
        text = usage_text()
        missing = [c for c in dispatch_commands() if f"gpudev {c}" not in text]
        self.assertEqual(
            missing,
            [],
            "these commands are dispatched but absent from `gpudev help`: "
            + ", ".join(missing),
        )

    def test_dispatch_parsing_actually_found_the_commands(self):
        # A regex that silently matched nothing would make the test above pass
        # for the wrong reason, which is the failure mode this guards.
        commands = dispatch_commands()
        self.assertGreater(len(commands), 15)
        for expected in ("status", "client add", "client remove", "ssh lockdown"):
            self.assertIn(expected, commands)


if __name__ == "__main__":
    unittest.main()
