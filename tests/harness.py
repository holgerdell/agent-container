"""Shared harness for command-level tests: run the real `agent` script against
the fake Docker engine (`fake_docker.py`) on PATH, then assert on the operations
it performed and the durable state it left behind."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from tests.agent_module import AGENT_SCRIPT, REPO_ROOT

FAKE_DOCKER = Path(__file__).resolve().parent / "fake_docker.py"


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    log: list[dict]
    state: dict

    def ops(self, cmd: str) -> list[dict]:
        return [e for e in self.log if e.get("cmd") == cmd]

    def scripts(self) -> list[str]:
        return [e["script"] for e in self.log if e.get("cmd") == "run" and e.get("script")]

    @property
    def extras_state(self) -> dict:
        raw = self.state.get("extras_state", "")
        return json.loads(raw)["packages"] if raw.strip() else {}


class Env:
    """One isolated agent installation: its own HOME, settings file, fake-docker
    scenario, and operation log."""

    def __init__(self, tmp_path: Path, *, username: str = "hold") -> None:
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.username = username
        self.state_path = tmp_path / "docker-state.json"
        self.log_path = tmp_path / "docker-log.jsonl"
        self.settings_path = self.home / ".config" / "agent-container" / "settings.json"
        self.bin = tmp_path / "bin"
        self.bin.mkdir(exist_ok=True)
        shim = self.bin / "docker"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_DOCKER}" "$@"\n')
        shim.chmod(0o755)
        self.write_state({})

    def write_settings(self, **overrides) -> None:
        settings = {
            "mount_mode": "pwd",
            "coding_agent": "claude-code",
            "apt_packages": ["curl", "git"],
            "extra_packages": ["uv"],
            "shared_host_paths": [],
            "env_vars": [],
        }
        settings.update(overrides)
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(settings, indent=2))

    def write_state(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state, indent=2))

    def read_state(self) -> dict:
        return json.loads(self.state_path.read_text())

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin}:{env['PATH']}",
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "XDG_CACHE_HOME": str(self.home / ".cache"),
                "XDG_DATA_HOME": str(self.home / ".local/share"),
                "FAKE_DOCKER_STATE": str(self.state_path),
                "FAKE_DOCKER_LOG": str(self.log_path),
                "LOGNAME": self.username,
                "USER": self.username,
                "NO_COLOR": "1",
            }
        )
        env.pop("SSH_AUTH_SOCK", None)
        return env

    def run(self, *args: str, cwd: Path | None = None, timeout: float = 60) -> Result:
        proc = subprocess.run(
            [sys.executable, str(AGENT_SCRIPT), *args],
            capture_output=True,
            text=True,
            env=self.env(),
            cwd=str(cwd or self.root),
            check=False,
            timeout=timeout,
        )
        log: list[dict] = []
        if self.log_path.exists():
            log = [json.loads(line) for line in self.log_path.read_text().splitlines() if line.strip()]
        return Result(proc.returncode, proc.stdout, proc.stderr, log, self.read_state())

    def clear_log(self) -> None:
        self.log_path.unlink(missing_ok=True)


def image_state(tag: str, *, fingerprint: str, image_id: str = "sha256:" + "a" * 64) -> dict:
    return {tag: {"id": image_id, "labels": {"agent.apt.fingerprint": fingerprint}, "created_at": "2026-07-01T00:00:00Z"}}


assert REPO_ROOT.is_dir()
