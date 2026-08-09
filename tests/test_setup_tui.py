"""Tests for `agent setup`.

The terminal UI itself is expected to keep changing, so it is only smoke-tested
end to end: a real pty run proves that setup starts, reacts to keystrokes, and
writes (or refuses to write) the settings file.

Everything setup actually *decides* — which choices are offered, and how the
answers fold back into settings — is exercised directly against
`_ask_questions`, with the three prompt widgets replaced by stubs that answer
the way a user would. No terminal involved, so no timing to get wrong.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import struct
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Sequence
from typing import Any

import pytest

from tests.agent_module import AGENT_SCRIPT, agent
from tests.harness import Env

# --- stubbed prompts ---------------------------------------------------------


class FakeUI:
    """Stands in for the three prompt widgets `_ask_questions` drives.

    Every prompt is answered exactly as presented — a bare Enter everywhere —
    unless the test says otherwise: `pick` chooses a value in a radio prompt,
    and `turn_on`/`turn_off` flip named checkbox entries the way pressing space
    does. `cancel_at` aborts the nth prompt, like Ctrl-C. Prompts are recorded
    so tests can assert on what was offered."""

    def __init__(self, *, pick=(), turn_on=(), turn_off=(), cancel_at: int | None = None) -> None:
        self.pick = list(pick)
        self.turn_on = list(turn_on)
        self.turn_off = list(turn_off)
        self.cancel_at = cancel_at
        self.prompts: list[dict[str, Any]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeUI:
        monkeypatch.setattr(agent, "tui_select", self._select)
        monkeypatch.setattr(agent, "tui_tristate", self._tristate)
        monkeypatch.setattr(agent, "tui_checkbox", self._checkbox)
        return self

    def _cancelled(self, kind: str, choices: list, details: str | None = None) -> bool:
        self.prompts.append({"kind": kind, "values": [c[1] for c in choices], "details": details})
        return len(self.prompts) - 1 == self.cancel_at

    def _state(self, value: Any, state: str) -> str:
        if any(value == v for v in self.turn_on):
            return agent.GROUP_SELECTED
        if any(value == v for v in self.turn_off):
            return agent.GROUP_UNSELECTED
        return state

    def _select(self, message, choices, *, initial=None, summary_message=None):
        if self._cancelled("select", choices):
            return None
        values = [value for _, value, _ in choices]
        for wanted in self.pick:
            if wanted in values:
                return wanted
        return initial if initial in values else values[0]

    def _tristate(self, message, choices, *, summary_message=None, details=None):
        if self._cancelled("tristate", choices, details):
            return None
        return [(value, self._state(value, state)) for _, value, state in choices]

    def _checkbox(self, message, choices, *, summary_message=None, details=None):
        if self._cancelled("checkbox", choices, details):
            return None
        states = [(value, self._state(value, agent.GROUP_SELECTED if on else agent.GROUP_UNSELECTED)) for _, value, on in choices]
        return [value for value, state in states if state == agent.GROUP_SELECTED]


@pytest.fixture
def ask(monkeypatch, tmp_path):
    """Answer setup's questions and return (resulting settings, FakeUI)."""
    # A HOME with none of the allowlisted host paths present, so the read-only
    # mount prompt offers exactly what the settings under test ask for.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def _ask(current: agent.Settings | None = None, **kwargs) -> tuple[agent.Settings | None, FakeUI]:
        ui = FakeUI(**kwargs).install(monkeypatch)
        return agent._ask_questions(current if current is not None else agent.Settings()), ui

    return _ask


LATEX = "latex"


class TestDefaults:
    def test_fresh_settings_keep_the_catalog_defaults(self, ask):
        result, _ = ask()
        assert result is not None
        assert result.mount_mode == "repo_root"
        assert result.coding_agent == agent.DEFAULT_AGENT_ID
        for pkg in ("git", "python3", "ripgrep"):
            assert pkg in result.apt_packages
        assert "texlive-xetex" not in result.apt_packages
        for extra in ("uv", "claude-code", "typst", "ollama"):
            assert extra in result.extra_packages

    def test_existing_answers_are_prefilled(self, ask):
        current = agent.Settings(mount_mode="pwd", coding_agent="opencode")
        result, _ = ask(current)
        assert result is not None
        assert result.mount_mode == "pwd"
        assert result.coding_agent == "opencode"

    def test_an_agent_that_no_longer_exists_falls_back_to_the_default(self, ask):
        result, _ = ask(agent.Settings(coding_agent="gone-agent"))
        assert result is not None
        assert result.coding_agent == agent.DEFAULT_AGENT_ID

    def test_cancelling_any_prompt_discards_everything(self, ask):
        for n in range(4):
            result, ui = ask(cancel_at=n)
            assert result is None, f"prompt {n} was cancelled but setup still returned settings"
            assert len(ui.prompts) == n + 1


class TestAptGroups:
    def test_clearing_a_group_removes_every_member(self, ask):
        result, _ = ask(turn_off=[LATEX])
        assert result is not None
        assert "texlive-xetex" not in result.apt_packages
        assert "biber" not in result.apt_packages
        assert "git" in result.apt_packages

    def test_selecting_a_partial_group_expands_it(self, ask):
        result, _ = ask(agent.Settings(apt_packages=["biber"]), turn_on=[LATEX])
        assert result is not None
        assert {"biber", "texlive-xetex"} <= set(result.apt_packages)

    def test_an_untouched_partial_group_keeps_its_packages(self, ask):
        result, _ = ask(agent.Settings(apt_packages=["biber"]))
        assert result is not None
        assert result.apt_packages == ["biber"]

    def test_custom_package_names_survive_and_are_explained(self, ask):
        result, ui = ask(agent.Settings(apt_packages=["git", "my-internal-tool"]))
        assert result is not None
        assert "my-internal-tool" in result.apt_packages
        # Custom names are never offered as a togglable group, only mentioned.
        groups = next(p for p in ui.prompts if p["kind"] == "tristate")
        assert "my-internal-tool" not in groups["values"]
        assert "my-internal-tool" in (groups["details"] or "")


class TestExtras:
    def test_only_registry_entries_are_offered(self, ask):
        result, _ = ask(agent.Settings(extra_packages=["uv", "not-a-real-package"]))
        assert result is not None
        assert result.extra_packages == ["uv"]

    def test_an_extra_can_be_switched_on(self, ask):
        result, _ = ask(agent.Settings(extra_packages=[]), turn_on=["uv"])
        assert result is not None
        assert result.extra_packages == ["uv"]

    def test_choosing_a_coding_agent_does_not_install_it(self, ask):
        result, _ = ask(agent.Settings(coding_agent="claude-code", extra_packages=[]), pick=["codex"])
        assert result is not None
        assert result.coding_agent == "codex"
        assert result.extra_packages == []


class TestSharedMounts:
    def test_a_configured_builtin_mount_is_kept(self, ask):
        host, container, _ = agent.SHARED_HOST_PATHS_ALLOWLIST[0]
        mount = agent.SharedMount(host, container, builtin=True)
        result, _ = ask(agent.Settings(shared_host_paths=[mount]))
        assert result is not None
        assert result.shared_host_paths == [mount]

    def test_a_custom_mount_is_kept(self, ask):
        mount = agent.SharedMount("/etc/example.conf", "/home/agent/example.conf", builtin=False)
        result, _ = ask(agent.Settings(shared_host_paths=[mount]))
        assert result is not None
        assert result.shared_host_paths == [mount]

    def test_nothing_is_offered_when_nothing_applies(self, ask):
        result, ui = ask()
        assert result is not None
        assert result.shared_host_paths == []
        assert len(ui.prompts) == 4


# --- end-to-end through a real terminal --------------------------------------

KEY_ENTER = b"\r"
KEY_DOWN = b"j"
KEY_CTRLC = b"\x03"

SETTLE = 0.25  # output quiet for this long means the program is waiting on us
MAX_PROMPTS = 20  # cap on "press Enter until it exits", so a hang still fails
TIMEOUT = 120.0


def drive_setup(env: Env, keys: Sequence[bytes] = (), *, accept_rest: bool = True) -> tuple[int, str]:
    """Run `agent setup` under a pty, send `keys`, then (unless told not to)
    keep pressing Enter until it exits. Returns (exit code, everything printed).

    A key is sent only once the previous one has visibly landed — the prompt it
    produced is fully drawn and setup is back to waiting for input. Typing ahead
    is not an option: setup leaves raw mode between prompts, and the terminal
    drops whatever is queued when the mode changes."""
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child process
        try:
            os.execve(sys.executable, [sys.executable, str(AGENT_SCRIPT), "setup"], env.env())
        finally:
            os._exit(127)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

    captured = bytearray()
    lock = threading.Lock()
    last = [time.monotonic()]

    def pump() -> None:
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:  # master side EOF: the child is gone
                return
            if not chunk:
                return
            with lock:
                captured.extend(chunk)
                last[0] = time.monotonic()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.monotonic() + TIMEOUT

    def wait_for_prompt(seen: int) -> bool:
        """Wait until setup has printed past `seen` bytes and fallen silent, i.e.
        it is sitting at a prompt. False once it has exited (or timed out)."""
        while reader.is_alive() and time.monotonic() < deadline:
            with lock:
                grown, quiet = len(captured) > seen, time.monotonic() - last[0]
            if grown and quiet >= SETTLE:
                return True
            time.sleep(0.02)
        return False

    def finish(message: str | None) -> tuple[int, str]:
        reader.join(max(0.0, deadline - time.monotonic()))
        alive = reader.is_alive()
        if alive:
            os.kill(pid, 9)
        _, status = os.waitpid(pid, 0)
        os.close(fd)
        text = bytes(captured).decode(errors="replace")
        if alive or message:
            raise AssertionError(f"{message or 'agent setup did not exit'}\n--- output ---\n{text}")
        return os.waitstatus_to_exitcode(status), text

    if not wait_for_prompt(0):
        return finish("agent setup never reached its first prompt")
    for key in list(keys) + ([KEY_ENTER] * MAX_PROMPTS if accept_rest else []):
        with lock:
            seen = len(captured)
        try:
            os.write(fd, key)
        except OSError:  # it exited while we were reaching for the keyboard
            break
        if not wait_for_prompt(seen):
            break
    return finish(None)


@pytest.fixture
def setup_env(tmp_path) -> Env:
    env = Env(tmp_path)
    claude = env.home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "CLAUDE.md").write_text("test\n")
    return env


class TestEndToEnd:
    def test_accepting_every_prompt_saves_settings(self, setup_env: Env) -> None:
        code, out = drive_setup(setup_env)
        assert code == 0, out
        saved = json.loads(setup_env.settings_path.read_text())
        assert saved["mount_mode"] == "repo_root"
        assert saved["coding_agent"] == agent.DEFAULT_AGENT_ID
        assert saved["apt_packages"]
        # Configuration changes must never kick off a build or an install.
        assert not setup_env.log_path.exists()

    def test_keystrokes_change_the_answer(self, setup_env: Env) -> None:
        code, out = drive_setup(setup_env, [KEY_DOWN])
        assert code == 0, out
        saved = json.loads(setup_env.settings_path.read_text())
        assert saved["mount_mode"] == "pwd"

    def test_ctrl_c_cancels_without_saving(self, setup_env: Env) -> None:
        code, out = drive_setup(setup_env, [KEY_CTRLC], accept_rest=False)
        assert code != 0, out
        assert not setup_env.settings_path.exists()

    def test_non_tty_stdin_errors_cleanly(self, setup_env: Env) -> None:
        proc = subprocess.run(
            [sys.executable, str(AGENT_SCRIPT), "setup"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=setup_env.env(),
            check=False,
            timeout=60,
        )
        assert proc.returncode != 0
        assert b"TTY" in proc.stderr
