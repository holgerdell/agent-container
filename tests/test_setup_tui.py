"""Black-box tests for `agent setup` interactive prompts.

Spawns the script under a controlled pty, drives keystrokes, and verifies
the settings file produced. POSIX-only (depends on pty + termios).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pty
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = REPO_ROOT / "agent"

KEY_DOWN = b"\x1b[B"
KEY_UP = b"\x1b[A"
KEY_ENTER = b"\r"
KEY_SPACE = b" "
KEY_CTRLC = b"\x03"


def _drive(
    keys: Iterable[bytes],
    *,
    home: Path,
    cols: int = 80,
    rows: int = 24,
    timeout: float = 10.0,
) -> tuple[int, bytes]:
    """Run `agent setup` in a pty, send keys, return (exit_code, output)."""
    pid, fd = pty.fork()
    if pid == 0:
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["XDG_DATA_HOME"] = str(home / ".local/share")
        env["NO_COLOR"] = "1"
        try:
            os.execvpe(str(AGENT), [str(AGENT), "setup"], env)
        except OSError as e:
            sys.stderr.write(f"exec failed: {e}\n")
            os._exit(127)

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    captured = bytearray()

    def drain(idle: float, total: float) -> None:
        end = time.time() + total
        last = time.time()
        while time.time() < end and time.time() - last < idle:
            r, _, _ = select.select([fd], [], [], 0.05)
            if not r:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            captured.extend(chunk)
            last = time.time()

    drain(idle=0.3, total=3.0)
    for key in keys:
        os.write(fd, key)
        drain(idle=0.3, total=3.0)

    deadline = time.time() + timeout
    raw_status = 0
    exited = False
    while time.time() < deadline:
        wpid, st = os.waitpid(pid, os.WNOHANG)
        if wpid != 0:
            raw_status = st
            exited = True
            break
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if chunk:
                captured.extend(chunk)

    if not exited:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        os.close(fd)
        raise TimeoutError(f"agent setup did not exit within {timeout}s")

    with contextlib.suppress(OSError):
        os.close(fd)
    return os.waitstatus_to_exitcode(raw_status), bytes(captured)


class SetupTUITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-setup-test-"))
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".claude" / "CLAUDE.md").write_text("test\n")
        (self.home / ".claude" / "skills").mkdir()
        self.settings = self.home / ".config" / "agent-container" / "settings.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _saved(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.settings.read_text())
        return data

    def test_arrow_keys_navigate_select(self) -> None:
        # Accept default engine; default mount cursor is repo_root, UP → pwd.
        code, _ = _drive(
            [KEY_ENTER, KEY_UP, KEY_ENTER, KEY_ENTER],
            home=self.home,
        )
        self.assertEqual(code, 0)
        saved = self._saved()
        self.assertEqual(saved["container_engine"], "docker")
        self.assertEqual(saved["mount_mode"], "pwd")
        self.assertEqual(saved["shared_host_paths"], [])

    def test_vim_keys_navigate_select(self) -> None:
        code, _ = _drive(
            [KEY_ENTER, b"j", KEY_ENTER, KEY_ENTER],
            home=self.home,
        )
        self.assertEqual(code, 0)
        self.assertEqual(self._saved()["mount_mode"], "pwd")

    def test_default_mount_when_no_navigation(self) -> None:
        code, _ = _drive(
            [KEY_ENTER, KEY_ENTER, KEY_ENTER],
            home=self.home,
        )
        self.assertEqual(code, 0)
        self.assertEqual(self._saved()["mount_mode"], "repo_root")

    def test_engine_selection_saves_podman(self) -> None:
        # Default cursor is docker; DOWN → podman.
        code, _ = _drive(
            [KEY_DOWN, KEY_ENTER, KEY_ENTER, KEY_ENTER],
            home=self.home,
        )
        self.assertEqual(code, 0)
        self.assertEqual(self._saved()["container_engine"], "podman")

    def test_checkbox_toggles_two_entries(self) -> None:
        code, _ = _drive(
            [KEY_ENTER, KEY_ENTER, KEY_UP, KEY_SPACE, KEY_UP, KEY_SPACE, KEY_DOWN, KEY_DOWN, KEY_ENTER],
            home=self.home,
        )
        self.assertEqual(code, 0)
        saved = self._saved()
        self.assertEqual(saved["mount_mode"], "repo_root")
        self.assertEqual(
            saved["shared_host_paths"],
            ["~/.claude/CLAUDE.md", "~/.claude/skills/"],
        )

    def test_existing_settings_are_prefilled(self) -> None:
        # Pre-existing pwd overrides the repo_root default.
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(
            json.dumps(
                {
                    "container_engine": "docker",
                    "mount_mode": "pwd",
                    "shared_host_paths": ["~/.claude/skills/"],
                },
            ),
        )
        code, _ = _drive(
            [KEY_ENTER, KEY_ENTER, KEY_ENTER],
            home=self.home,
        )
        self.assertEqual(code, 0)
        saved = self._saved()
        self.assertEqual(saved["container_engine"], "docker")
        self.assertEqual(saved["mount_mode"], "pwd")
        self.assertEqual(saved["shared_host_paths"], ["~/.claude/skills/"])

    def test_existing_custom_shared_mount_is_preserved(self) -> None:
        custom_host = self.home / "custom-config"
        custom_host.write_text("token=1\n")
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(
            json.dumps(
                {
                    "container_engine": "docker",
                    "mount_mode": "repo_root",
                    "shared_host_paths": [{"host": str(custom_host), "container": "/home/agent/custom-config"}],
                }
            )
        )
        code, _ = _drive([KEY_ENTER, KEY_ENTER, KEY_ENTER], home=self.home)
        self.assertEqual(code, 0)
        self.assertEqual(
            self._saved()["shared_host_paths"],
            [{"host": str(custom_host), "container": "/home/agent/custom-config"}],
        )

    def test_narrow_terminal_does_not_crash(self) -> None:
        code, _ = _drive(
            [KEY_ENTER, KEY_ENTER, KEY_UP, KEY_SPACE, KEY_UP, KEY_SPACE, KEY_DOWN, KEY_DOWN, KEY_ENTER],
            home=self.home,
            cols=40,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(self._saved()["shared_host_paths"]), 2)

    def test_ctrl_c_cancels_cleanly(self) -> None:
        code, _ = _drive([KEY_CTRLC], home=self.home)
        self.assertNotEqual(code, 0)
        self.assertFalse(self.settings.exists())

    def test_non_tty_stdin_errors_cleanly(self) -> None:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["XDG_CONFIG_HOME"] = str(self.home / ".config")
        env["XDG_DATA_HOME"] = str(self.home / ".local/share")
        proc = subprocess.run(
            [str(AGENT), "setup"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=env,
            check=False,
            timeout=10,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"TTY", proc.stderr)


if __name__ == "__main__":
    unittest.main()
