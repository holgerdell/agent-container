"""Concurrency tests for the shared home volume.

All repositories for one user share a single mutable home volume and a single
extras-state file, so `agent build` and `agent update` must serialize per user.
"""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from tests.agent_module import AGENT_SCRIPT, agent
from tests.harness import Env, image_state

IMAGE = "agent:hold"
HOME_VOLUME = "agent-home-hold"

LOCK_PROBE = """
import importlib.util, os, sys, time
from importlib.machinery import SourceFileLoader
loader = SourceFileLoader("agent_cli", sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
with mod.maintenance_lock(sys.argv[2]):
    print(f"ENTER {time.time()}", flush=True)
    time.sleep(0.4)
    print(f"EXIT {time.time()}", flush=True)
"""


def _probe(env: Env, username: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", LOCK_PROBE, str(AGENT_SCRIPT), username],
        stdout=subprocess.PIPE,
        text=True,
        env=env.env(),
    )


def _window(proc: subprocess.Popen[str]) -> tuple[float, float]:
    out, _ = proc.communicate(timeout=30)
    times = [float(line.split()[1]) for line in out.splitlines() if line.startswith(("ENTER", "EXIT"))]
    return times[0], times[1]


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


class TestMaintenanceLock:
    def test_same_user_serializes_across_processes(self, tmp_path):
        env = Env(tmp_path)
        p1, p2 = _probe(env, "hold"), _probe(env, "hold")
        assert not _overlap(_window(p1), _window(p2))

    def test_different_users_do_not_block_each_other(self, tmp_path):
        env = Env(tmp_path)
        p1, p2 = _probe(env, "hold"), _probe(env, "alice")
        assert _overlap(_window(p1), _window(p2))

    def test_lock_is_shared_regardless_of_the_repository_it_runs_from(self, tmp_path):
        env = Env(tmp_path)
        (tmp_path / "repo-a").mkdir()
        (tmp_path / "repo-b").mkdir()
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", LOCK_PROBE, str(AGENT_SCRIPT), "hold"],
                stdout=subprocess.PIPE,
                text=True,
                env=env.env(),
                cwd=str(tmp_path / name),
            )
            for name in ("repo-a", "repo-b")
        ]
        assert not _overlap(_window(procs[0]), _window(procs[1]))


class TestConcurrentMaintenance:
    def test_concurrent_build_and_update_do_not_interleave_or_clobber_state(self, tmp_path):
        env = Env(tmp_path)
        env.write_settings(apt_packages=["curl", "git"], extra_packages=["uv", "ruff"])
        env.write_state(
            {
                "images": image_state(IMAGE, fingerprint=agent.apt_fingerprint(["curl", "git"])),
                "volumes": [HOME_VOLUME],
                "delay": 0.15,
            }
        )
        (tmp_path / "repo-a").mkdir()
        (tmp_path / "repo-b").mkdir()

        def launch(command: str, log_name: str):
            e = Env(tmp_path, username="hold")
            e.state_path = env.state_path
            e.settings_path = env.settings_path
            e.log_path = tmp_path / log_name
            return e.run(command, cwd=tmp_path / "repo-a")

        with ThreadPoolExecutor(max_workers=2) as ex:
            # `build` rebuilds and retags the image while `update` reconciles
            # against it — the pair the shared lock exists to separate.
            f1 = ex.submit(launch, "build", "log-a.jsonl")
            f2 = ex.submit(launch, "update", "log-b.jsonl")
            r1, r2 = f1.result(), f2.result()

        assert r1.returncode == 0 and r2.returncode == 0

        def window(log_name: str) -> tuple[float, float]:
            lines = [json.loads(x) for x in (tmp_path / log_name).read_text().splitlines() if x.strip()]
            times = [e["at"] for e in lines if e.get("cmd") == "run" and e.get("script")]
            return min(times), max(times)

        assert not _overlap(window("log-a.jsonl"), window("log-b.jsonl"))

        # The last writer's state is complete, not a partial merge of both runs.
        state = json.loads(env.read_state()["extras_state"])
        assert sorted(state["packages"]) == ["ruff", "uv"]
        assert all(v["status"] == "installed" for v in state["packages"].values())
