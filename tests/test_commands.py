"""Command-level tests for `agent`, executed against a controlled Docker engine.

These assert externally observable behavior — which engine operations happened and
what durable state they left — rather than flag order or temp-file details.
"""

from __future__ import annotations

import json

import pytest

from tests.agent_module import agent
from tests.harness import Env, image_state

IMAGE = "agent:hold"
HOME_VOLUME = "agent-home-hold"


def _is_version_probe(script: str) -> bool:
    """Identify the version-report run by its marker. Matching on `--version`
    would also catch install scripts, whose up-to-date guards call it too."""
    return script.lstrip().startswith(agent.VERSION_PROBE_MARKER)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.write_settings()
    return e


def fingerprint(*packages: str) -> str:
    return agent.apt_fingerprint(list(packages))


def built(env: Env) -> dict:
    """Scenario in which the image already exists and matches settings."""
    return {
        "images": image_state(IMAGE, fingerprint=fingerprint("curl", "git")),
        "volumes": [HOME_VOLUME],
    }


class TestBuild:
    def test_generated_package_list_reflects_settings(self, env):
        env.write_settings(apt_packages=["git", "curl", "curl"])
        r = env.run("build")
        assert r.returncode == 0
        (build,) = r.ops("build")
        assert build["packages"] == ["curl", "git"]
        assert build["dockerfile_exists"]

    def test_uses_fresh_pull_and_reuses_cached_layers(self, env):
        build = env.run("build").ops("build")[0]
        assert build["pull"] is True
        assert build["no_cache"] is False

    def test_refresh_discards_the_build_cache(self, env):
        build = env.run("build", "--refresh").ops("build")[0]
        assert build["pull"] is True
        assert build["no_cache"] is True

    def test_image_receives_the_current_user_tag(self, env):
        r = env.run("build")
        assert r.ops("build")[0]["tag"] == IMAGE
        assert IMAGE in r.state["images"]

    def test_image_metadata_and_label_carry_the_apt_fingerprint(self, env):
        env.write_settings(apt_packages=["curl", "git"])
        r = env.run("build")
        build = r.ops("build")[0]
        expected = fingerprint("curl", "git")
        assert build["metadata"]["apt_fingerprint"] == expected
        assert build["metadata"]["apt_packages"] == ["curl", "git"]
        assert build["build_args"]["AGENT_APT_FINGERPRINT"] == expected
        assert r.state["images"][IMAGE]["labels"]["agent.apt.fingerprint"] == expected

    def test_failed_apt_install_fails_the_build_cleanly(self, env):
        env.write_state({"fail": {"build": 1}})
        r = env.run("build")
        assert r.returncode == 1
        assert "image build failed" in r.stderr
        # No reconciliation is attempted after a failed build.
        assert r.scripts() == []

    def test_successful_build_reconciles_extras(self, env):
        env.write_settings(extra_packages=["uv", "ruff"])
        r = env.run("build")
        assert r.returncode == 0
        assert sorted(r.extras_state) == ["ruff", "uv"]

    def test_running_containers_are_neither_stopped_nor_recreated(self, env):
        env.write_state(
            {
                "images": image_state(IMAGE, fingerprint="old"),
                "containers": {
                    "agent-run-hold-abcdef123456": {
                        "running": True,
                        "image": "sha256:" + "a" * 64,
                        "labels": {"agent.fingerprint": "stale"},
                        "mounts": [["/repo", "/repo"]],
                    }
                },
            }
        )
        r = env.run("build")
        assert r.returncode == 0
        assert "agent-run-hold-abcdef123456" in r.state["containers"]
        assert r.state["containers"]["agent-run-hold-abcdef123456"]["running"] is True
        assert r.ops("rm") == []

    def test_superseded_image_in_use_by_a_container_is_preserved(self, env):
        env.write_state(
            {
                "images": image_state(IMAGE, fingerprint="old"),
                "containers": {
                    "agent-run-hold-abcdef123456": {
                        "running": True,
                        "image": "sha256:" + "a" * 64,
                        "labels": {"agent.fingerprint": "stale"},
                        "mounts": [["/repo", "/repo"]],
                    }
                },
            }
        )
        r = env.run("build")
        assert r.ops("rmi") == []

    def test_login_shell_path_is_generated_from_one_list(self, env):
        build = env.run("build").ops("build")[0]
        script = build["profile_script"]
        for directory in agent.HOME_BIN_DIRS:
            assert f'"$HOME/{directory}"' in script
            assert f"/home/agent/{directory}" in build["build_args"]["AGENT_PATH"]

    def test_empty_package_selection_is_rejected_before_building(self, env):
        env.write_settings(apt_packages=[])
        r = env.run("build")
        assert r.returncode == 1
        assert r.ops("build") == []


class TestUpdate:
    def test_builds_the_image_when_none_exists(self, env):
        r = env.run("update")
        assert r.returncode == 0
        assert len(r.ops("build")) == 1

    def test_does_not_rebuild_an_existing_image(self, env):
        env.write_state(built(env))
        r = env.run("update")
        assert r.returncode == 0
        assert r.ops("build") == []

    def test_never_upgrades_apt_packages(self, env):
        env.write_state(built(env))
        r = env.run("update")
        assert not any("apt" in s for s in r.scripts())

    def test_missing_selected_packages_are_installed(self, env):
        env.write_state(built(env))
        env.write_settings(extra_packages=["uv"])
        r = env.run("update")
        assert any("astral.sh/uv/install.sh" in s for s in r.scripts())
        assert r.extras_state == {"uv": {"status": "installed"}}

    def test_managed_packages_run_their_update_routine(self, env):
        state = built(env)
        state["extras_state"] = json.dumps({"schema": 1, "packages": {"uv": {"status": "installed"}}})
        env.write_state(state)
        env.write_settings(extra_packages=["uv"])
        r = env.run("update")
        assert any(agent.extra_by_id("uv").update in s for s in r.scripts())
        assert not any("astral.sh/uv/install.sh" in s for s in r.scripts())

    def test_deselected_managed_packages_are_removed(self, env):
        state = built(env)
        state["extras_state"] = json.dumps({"schema": 1, "packages": {"uv": {"status": "installed"}}})
        env.write_state(state)
        env.write_settings(extra_packages=[])
        r = env.run("update")
        assert any('rm -rf "$HOME/.local/bin/uv" "$HOME/.local/bin/uvx"' in s for s in r.scripts())
        assert r.extras_state == {}

    def test_removal_is_restricted_to_registry_owned_paths(self, env):
        state = built(env)
        state["extras_state"] = json.dumps({"schema": 1, "packages": {"bun": {"status": "installed"}}})
        env.write_state(state)
        env.write_settings(extra_packages=[])
        r = env.run("update")
        deletions = [line for s in r.scripts() for line in s.splitlines() if "rm -rf" in line]
        assert deletions == ['rm -rf "$HOME/.bun"']

    def test_untracked_tools_are_never_removed(self, env):
        # Nothing recorded as managed and nothing selected → no removal at all.
        env.write_state(built(env))
        env.write_settings(extra_packages=[])
        r = env.run("update")
        assert not any("rm -rf" in s for s in r.scripts())

    def test_unknown_state_entries_are_warned_about_not_executed(self, env):
        state = built(env)
        state["extras_state"] = json.dumps({"schema": 1, "packages": {"long-gone": {"status": "installed"}}})
        env.write_state(state)
        env.write_settings(extra_packages=[])
        r = env.run("update")
        assert "long-gone" in r.stderr
        assert not any("rm -rf" in s for s in r.scripts())
        # Reported per package as skipped, not silently dropped from the summary.
        assert "long-gone" in r.stdout
        assert "skipped" in r.stdout

    def test_state_from_an_unsupported_schema_is_not_acted_upon(self, env):
        state = built(env)
        state["extras_state"] = json.dumps({"schema": 99, "packages": {"bun": {"status": "installed"}}})
        env.write_state(state)
        env.write_settings(extra_packages=[])
        r = env.run("update")
        assert "schema" in r.stderr
        assert not any("rm -rf" in s for s in r.scripts())

    def test_an_installer_that_leaves_no_executable_counts_as_failed(self, env):
        state = built(env)
        # The installer "succeeds" but the verification step finds no binary.
        state["fail"] = {"scripts": ["command -v uv >/dev/null"]}
        env.write_state(state)
        env.write_settings(extra_packages=["uv"])
        r = env.run("update")
        assert r.returncode != 0
        assert "uv" not in r.extras_state

    def test_packages_run_in_stable_registry_order(self, env):
        env.write_state(built(env))
        env.write_settings(extra_packages=["typst", "uv", "ruff"])
        r = env.run("update")
        (script,) = [s for s in r.scripts() if "# op: " in s]
        positions = [script.index(f"# op: {pkg} ") for pkg in ["uv", "ruff", "typst"]]
        assert positions == sorted(positions)

    def test_one_failure_does_not_prevent_later_packages(self, env):
        state = built(env)
        state["fail"] = {"scripts": ["astral.sh/uv/install.sh"]}
        env.write_state(state)
        env.write_settings(extra_packages=["uv", "ruff"])
        r = env.run("update")
        assert any("astral.sh/ruff/install.sh" in s for s in r.scripts())
        assert "ruff" in r.extras_state

    def test_any_failure_yields_a_nonzero_result(self, env):
        state = built(env)
        state["fail"] = {"scripts": ["astral.sh/uv/install.sh"]}
        env.write_state(state)
        env.write_settings(extra_packages=["uv", "ruff"])
        assert env.run("update").returncode != 0

    def test_failed_install_is_not_recorded_as_managed(self, env):
        state = built(env)
        state["fail"] = {"scripts": ["astral.sh/uv/install.sh"]}
        env.write_state(state)
        env.write_settings(extra_packages=["uv"])
        r = env.run("update")
        assert "uv" not in r.extras_state

    def test_failed_removal_remains_pending(self, env):
        state = built(env)
        state["extras_state"] = json.dumps({"schema": 1, "packages": {"bun": {"status": "installed"}}})
        state["fail"] = {"scripts": ['rm -rf "$HOME/.bun"']}
        env.write_state(state)
        env.write_settings(extra_packages=[])
        r = env.run("update")
        assert "bun" in r.extras_state
        # A second run retries the removal rather than forgetting about it.
        env.clear_log()
        r2 = env.run("update")
        assert any('rm -rf "$HOME/.bun"' in s for s in r2.scripts())

    def test_every_package_outcome_is_reported_on_its_own_line(self, env):
        state = built(env)
        state["fail"] = {"scripts": ["astral.sh/uv/install.sh"]}
        env.write_state(state)
        env.write_settings(extra_packages=["uv", "ruff"])
        r = env.run("update")
        assert "uv... failed" in r.stdout
        assert "Ruff... installed" in r.stdout

    def test_summary_shows_the_version_move_of_an_update(self, env):
        state = built(env)
        state["extras_state"] = json.dumps({"schema": 1, "packages": {"uv": {"status": "installed"}}})
        state["versions"] = [{"uv": "uv 0.11.30"}, {"uv": "uv 0.11.32"}]
        env.write_state(state)
        env.write_settings(extra_packages=["uv"])
        r = env.run("update")
        assert "0.11.30 → 0.11.32" in r.stdout

    def test_an_update_that_changes_nothing_reads_as_up_to_date(self, env):
        state = built(env)
        state["extras_state"] = json.dumps({"schema": 1, "packages": {"uv": {"status": "installed"}}})
        state["versions"] = [{"uv": "uv 0.11.32"}]
        env.write_state(state)
        env.write_settings(extra_packages=["uv"])
        r = env.run("update")
        assert "0.11.32 is already the newest version" in r.stdout
        assert "updated" not in r.stdout

    def test_a_fresh_install_reports_just_the_version(self, env):
        state = built(env)
        state["versions"] = [{}, {"ruff": "ruff 0.16.0"}]
        env.write_state(state)
        env.write_settings(extra_packages=["ruff"])
        r = env.run("update")
        assert "installed" in r.stdout and "0.16.0" in r.stdout

    def test_updater_runs_with_agent_identity_home_volume_network_and_login_shell(self, env):
        env.write_state(built(env))
        env.write_settings(extra_packages=["uv"])
        r = env.run("update")
        installer = next(e for e in r.ops("run") if e.get("script") and "astral.sh/uv" in e["script"])
        assert installer["shell"] == ["bash", "-lc"]
        assert f"{HOME_VOLUME}:/home/agent" in installer["mounts"]
        assert installer["user"] == f"{__import__('os').getuid()}:{__import__('os').getgid()}"
        assert installer["network"] == "default"
        assert installer["detached"] is False

    def test_unknown_extra_ids_in_settings_are_warned_about_and_ignored(self, env):
        env.write_state(built(env))
        env.write_settings(extra_packages=["uv", "not-a-package"])
        r = env.run("update")
        assert "not-a-package" in r.stderr
        assert "not-a-package" not in r.extras_state


class TestLaunch:
    def test_matching_fingerprint_starts_normally(self, env):
        env.write_state(built(env))
        r = env.run()
        assert r.returncode == 0
        assert "different Debian package selection" not in r.stderr
        assert r.ops("exec")

    def test_apt_fingerprint_mismatch_warns_but_still_starts(self, env):
        env.write_state({"images": image_state(IMAGE, fingerprint="stale"), "volumes": [HOME_VOLUME]})
        r = env.run()
        assert r.returncode == 0
        assert "run `agent build`" in r.stderr
        assert r.ops("exec")
        assert r.ops("build") == []

    def test_first_launch_bootstraps_the_image_and_extras(self, env):
        r = env.run()
        assert r.returncode == 0
        assert len(r.ops("build")) == 1
        assert "uv" in r.extras_state
        assert r.ops("exec")

    def test_first_launch_still_starts_when_an_extra_fails(self, env):
        env.write_state({"fail": {"scripts": ["astral.sh/uv/install.sh"]}})
        r = env.run()
        assert r.returncode == 0
        assert "rerun `agent update`" in r.stderr
        assert r.ops("exec")

    def test_launch_never_reconciles_extras(self, env):
        env.write_state(built(env))
        r = env.run()
        assert r.scripts() == []

    def test_missing_default_agent_executable_is_not_pre_checked(self, env):
        env.write_state(built(env))
        env.write_settings(coding_agent="opencode", extra_packages=[])
        r = env.run()
        assert r.returncode == 0
        exec_op = r.ops("exec")[0]["argv"]
        assert "opencode" in exec_op

    def test_default_agent_may_be_selected_without_its_backing_package(self, env):
        env.write_state(built(env))
        env.write_settings(coding_agent="codex", extra_packages=["uv"])
        r = env.run()
        assert "codex" in r.ops("exec")[0]["argv"]

    def test_shell_subcommand_starts_bash(self, env):
        env.write_state(built(env))
        r = env.run("shell")
        assert "bash" in r.ops("exec")[0]["argv"]

    def _first_launch(self, env) -> str:
        """Launch once so the session container exists exactly as Agent creates
        it, and return its name."""
        env.write_state(built(env))
        r = env.run()
        (created,) = [e for e in r.ops("run") if e.get("detached")]
        env.clear_log()
        return created["name"]

    def test_stale_stopped_container_is_recreated(self, env):
        name = self._first_launch(env)
        state = env.read_state()
        state["containers"][name].update(running=False, labels={"agent.fingerprint": "stale"})
        env.write_state(state)
        r = env.run()
        assert any(e.get("cmd") == "rm" for e in r.log)
        assert any(e.get("detached") for e in r.ops("run"))
        assert r.state["containers"][name]["running"] is True

    def test_fresh_stopped_container_is_restarted_not_recreated(self, env):
        name = self._first_launch(env)
        state = env.read_state()
        state["containers"][name]["running"] = False
        env.write_state(state)
        r = env.run()
        assert not any(e.get("cmd") == "rm" for e in r.log)
        assert not any(e.get("detached") for e in r.ops("run"))
        assert any(e.get("cmd") == "start" for e in r.log)
        assert r.state["containers"][name]["running"] is True

    def test_stale_running_container_stays_alive_and_is_reused(self, env):
        name = self._first_launch(env)
        state = env.read_state()
        state["containers"][name]["labels"] = {"agent.fingerprint": "stale"}
        env.write_state(state)
        r = env.run()
        assert not any(e.get("cmd") == "rm" for e in r.log)
        assert not any(e.get("detached") for e in r.ops("run"))
        assert r.state["containers"][name]["running"] is True
        assert "still running" in r.stderr
        assert r.ops("exec")

    def test_container_is_named_for_the_user_and_repo_path(self, env):
        env.write_state(built(env))
        r = env.run()
        (created,) = [e for e in r.ops("run") if e.get("detached")]
        assert created["name"] == agent.run_container_name("hold", env.root.resolve())
        assert created["name"].startswith("agent-run-hold-")

    def test_home_volume_is_mounted_writable(self, env):
        env.write_state(built(env))
        r = env.run()
        (created,) = [e for e in r.ops("run") if e.get("detached")]
        assert f"{HOME_VOLUME}:/home/agent" in created["mounts"]
        assert not any(m.startswith(f"{HOME_VOLUME}:") and m.endswith(":ro") for m in created["mounts"])


class TestStop:
    def test_stop_removes_this_repos_container(self, env):
        state = built(env)
        name = agent.run_container_name("hold", env.root.resolve())
        state["containers"] = {name: {"running": True, "image": "x", "labels": {}, "mounts": []}}
        env.write_state(state)
        r = env.run("stop")
        assert name not in r.state["containers"]

    def test_stop_all_spans_every_visible_user(self, env):
        state = built(env)
        state["containers"] = {
            "agent-run-hold-aaaaaaaaaaaa": {"running": True, "image": "x", "labels": {}, "mounts": []},
            "agent-run-alice-bbbbbbbbbbbb": {"running": False, "image": "x", "labels": {}, "mounts": []},
        }
        env.write_state(state)
        r = env.run("stop", "--all")
        assert r.state["containers"] == {}


class TestClean:
    def _state(self):
        return {
            "images": {
                "agent:hold": {"id": "sha256:" + "1" * 64, "labels": {}, "created_at": ""},
                "agent:alice": {"id": "sha256:" + "2" * 64, "labels": {}, "created_at": ""},
                "agent:bob": {"id": "sha256:" + "3" * 64, "labels": {}, "created_at": ""},
            },
            "containers": {
                "agent-run-hold-aaaaaaaaaaaa": {"running": True, "image": "sha256:" + "1" * 64, "labels": {}, "mounts": []},
                "agent-run-alice-bbbbbbbbbbbb": {"running": False, "image": "sha256:" + "2" * 64, "labels": {}, "mounts": []},
            },
            "volumes": ["agent-home-hold", "agent-home-alice"],
        }

    def test_removes_every_visible_stopped_container(self, env):
        env.write_state(self._state())
        r = env.run("clean")
        assert "agent-run-alice-bbbbbbbbbbbb" not in r.state["containers"]

    def test_preserves_running_containers(self, env):
        env.write_state(self._state())
        r = env.run("clean")
        assert "agent-run-hold-aaaaaaaaaaaa" in r.state["containers"]

    def test_removes_unused_agent_images_across_users(self, env):
        env.write_state(self._state())
        r = env.run("clean")
        # alice's image is freed once her stopped container is gone; bob's was
        # never referenced.
        assert "agent:alice" not in r.state["images"]
        assert "agent:bob" not in r.state["images"]

    def test_preserves_images_used_by_running_containers(self, env):
        env.write_state(self._state())
        r = env.run("clean")
        assert "agent:hold" in r.state["images"]

    def test_never_touches_home_volumes(self, env):
        env.write_state(self._state())
        r = env.run("clean")
        assert set(r.state["volumes"]) == {"agent-home-hold", "agent-home-alice"}
        assert not any(e.get("cmd") == "volume" for e in r.log)

    def test_force_also_removes_running_containers(self, env):
        env.write_state(self._state())
        r = env.run("clean", "--force")
        assert r.state["containers"] == {}


class TestDoctor:
    def test_makes_no_networked_package_management_calls(self, env):
        state = built(env)
        state["extras_state"] = json.dumps({"schema": 1, "packages": {"uv": {"status": "installed"}}})
        env.write_state(state)
        r = env.run("doctor", "--json")
        assert r.ops("build") == []
        assert r.ops("pull") == []
        for op in r.ops("run"):
            assert op["pull"] == "never"
        # Doctor only reads its own state file and asks installed tools their version.
        assert all("state/agent-container/extras.json" in s or _is_version_probe(s) for s in r.scripts())

    def test_reports_image_drift(self, env):
        env.write_state({"images": image_state(IMAGE, fingerprint="stale"), "volumes": [HOME_VOLUME]})
        r = env.run("doctor", "--json")
        checks = {c["check"]: c for c in json.loads(r.stdout)["checks"]}
        assert checks["image"]["status"] == "warn"
        assert "agent build" in checks["image"]["fix"]

    def test_reports_container_state(self, env):
        state = built(env)
        state["containers"] = {
            "agent-run-hold-aaaaaaaaaaaa": {
                "running": True,
                "image": "sha256:" + "a" * 64,
                "labels": {"agent.fingerprint": "stale"},
                "mounts": [["/repo", "/repo"]],
            }
        }
        env.write_state(state)
        r = env.run("doctor", "--json")
        containers = json.loads(r.stdout)["containers"]
        assert containers[0]["name"] == "agent-run-hold-aaaaaaaaaaaa"
        assert containers[0]["running"] is True
        assert containers[0]["up_to_date"] is False

    def test_reports_home_volume_state(self, env):
        state = built(env)
        state["volume_sizes"] = {HOME_VOLUME: "1.5GB"}
        env.write_state(state)
        r = env.run("doctor", "--json")
        checks = {c["check"]: c for c in json.loads(r.stdout)["checks"]}
        assert checks["home volume"]["value"] == HOME_VOLUME
        assert checks["home volume"]["detail"] == "1.5 GB"

    def test_reports_reconciliation_state(self, env):
        state = built(env)
        state["extras_state"] = json.dumps({"schema": 1, "packages": {"uv": {"status": "installed"}}})
        env.write_state(state)
        env.write_settings(extra_packages=["uv"])
        r = env.run("doctor", "--json")
        assert json.loads(r.stdout)["extras"] == {"uv": {"status": "installed", "version": ""}}

    def test_reports_the_installed_version_of_each_extra(self, env):
        state = built(env)
        state["extras_state"] = json.dumps({"schema": 1, "packages": {"uv": {"status": "installed"}}})
        state["versions"] = [{"uv": "uv 0.11.32 (aarch64-unknown-linux-gnu)"}]
        env.write_state(state)
        env.write_settings(extra_packages=["uv"])
        r = env.run("doctor", "--json")
        assert json.loads(r.stdout)["extras"]["uv"]["version"] == "0.11.32"
        assert "0.11.32" in env.run("doctor").stdout

    def test_reports_pending_and_failed_extras(self, env):
        state = built(env)
        state["extras_state"] = json.dumps({"schema": 1, "packages": {"uv": {"status": "failed"}}})
        env.write_state(state)
        env.write_settings(extra_packages=["uv", "ruff"])
        r = env.run("doctor", "--json")
        checks = {c["check"]: c for c in json.loads(r.stdout)["checks"]}
        assert checks["extras"]["status"] == "warn"
        assert "pending" in checks["extras"]["detail"]
        assert "failed" in checks["extras"]["detail"]

    def test_a_failed_probe_degrades_one_field_not_the_report(self, env):
        state = built(env)
        state["fail"] = {"commands": ["system"]}  # volume-size sweep breaks
        state["volume_sizes"] = {HOME_VOLUME: "1.5GB"}
        env.write_state(state)
        r = env.run("doctor", "--json")
        assert r.returncode == 0
        checks = {c["check"]: c for c in json.loads(r.stdout)["checks"]}
        assert checks["home volume"]["detail"] == "size unavailable"
        assert checks["docker"]["status"] == "ok"

    def test_text_report_renders(self, env):
        env.write_state(built(env))
        r = env.run("doctor")
        assert r.returncode == 0
        assert "images & containers" in r.stdout
        assert "managed extras" in r.stdout


class TestSettings:
    def test_defaults_seed_both_supplied_catalogs(self, tmp_path):
        e = Env(tmp_path)
        e.settings_path.parent.mkdir(parents=True, exist_ok=True)
        e.settings_path.write_text("{}")
        e.write_state(built(e))
        # A settings file with no package keys still yields the full defaults.
        r = e.run("build")
        assert r.ops("build")[0]["packages"] == agent.default_apt_packages()
        assert sorted(r.extras_state) == sorted(agent.DEFAULT_EXTRA_PACKAGES)

    def test_custom_apt_names_reach_the_build_untouched(self, env):
        env.write_settings(apt_packages=["git", "my-internal-tool"])
        r = env.run("build")
        assert r.ops("build")[0]["packages"] == ["git", "my-internal-tool"]

    def test_unknown_coding_agent_falls_back_to_the_default(self, env):
        env.write_state(built(env))
        env.write_settings(coding_agent="not-an-agent")
        r = env.run()
        assert "not-an-agent" in r.stderr
        assert "claude" in r.ops("exec")[0]["argv"]
