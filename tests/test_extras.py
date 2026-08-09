"""Pure-logic tests for the native extra-package registry and reconciliation.

The planner and state machine are tested against a small synthetic registry so
they stay independent of the twelve real installers; the curated registry itself
is checked for the invariants that keep removal safe.
"""

import subprocess

from tests.agent_module import agent

FAKE_REGISTRY = (
    agent.ExtraPackage(
        id="alpha",
        label="Alpha",
        description="",
        install="install-alpha",
        update="update-alpha",
        owned_paths=(".local/bin/alpha",),
        executables=("alpha",),
    ),
    agent.ExtraPackage(
        id="beta",
        label="Beta",
        description="",
        install="install-beta",
        update="update-beta",
        uninstall="uninstall-beta",
        owned_paths=(".beta",),
        executables=("beta",),
        agent=agent.AgentLaunch("beta", "Beta Agent", ("--yes",), "a coding agent"),
    ),
    agent.ExtraPackage(
        id="gamma",
        label="Gamma",
        description="",
        install="install-gamma",
        update="update-gamma",
        owned_paths=(".local/bin/gamma",),
        executables=("gamma",),
    ),
)


def _actions(ops):
    return [(op.package_id, op.action) for op in ops]


class TestSelectionParsing:
    def test_splits_known_from_unknown(self):
        known, unknown = agent.normalize_extra_packages(["beta", "nope", "alpha"], FAKE_REGISTRY)
        assert known == ["beta", "alpha"]
        assert unknown == ["nope"]

    def test_deduplicates(self):
        known, _ = agent.normalize_extra_packages(["alpha", "alpha"], FAKE_REGISTRY)
        assert known == ["alpha"]

    def test_defaults_are_the_whole_registry(self):
        assert list(agent.DEFAULT_EXTRA_PACKAGES) == agent.registry_ids()

    def test_supplied_product_catalog_is_present(self):
        supplied = {"bun", "claude-code", "codex", "mistral-vibe", "ollama", "opencode", "ruff", "tectonic", "ty", "typos", "typst", "uv"}
        assert supplied == set(agent.registry_ids())


class TestPlanReconciliation:
    def test_missing_selected_packages_are_installed(self):
        ops, _ = agent.plan_reconciliation(["alpha"], [], FAKE_REGISTRY)
        assert _actions(ops) == [("alpha", agent.ACTION_INSTALL)]

    def test_managed_selected_packages_are_updated(self):
        ops, _ = agent.plan_reconciliation(["alpha"], ["alpha"], FAKE_REGISTRY)
        assert _actions(ops) == [("alpha", agent.ACTION_UPDATE)]

    def test_deselected_managed_packages_are_removed(self):
        ops, _ = agent.plan_reconciliation([], ["beta"], FAKE_REGISTRY)
        assert _actions(ops) == [("beta", agent.ACTION_REMOVE)]

    def test_untracked_tools_are_never_touched(self):
        # `gamma` is neither selected nor managed: it produces no operation at all.
        ops, _ = agent.plan_reconciliation(["alpha"], ["alpha"], FAKE_REGISTRY)
        assert "gamma" not in {op.package_id for op in ops}

    def test_uses_registry_order_not_settings_order(self):
        ops, _ = agent.plan_reconciliation(["gamma", "alpha", "beta"], [], FAKE_REGISTRY)
        assert [op.package_id for op in ops] == ["alpha", "beta", "gamma"]

    def test_unknown_state_entries_are_reported_not_executed(self):
        ops, stale = agent.plan_reconciliation([], ["ghost"], FAKE_REGISTRY)
        assert ops == []
        assert stale == ["ghost"]

    def test_unknown_selection_ids_are_ignored(self):
        ops, _ = agent.plan_reconciliation(["ghost"], [], FAKE_REGISTRY)
        assert ops == []


class TestStateTransitions:
    def _op(self, pkg, action):
        return agent.ExtraOp(pkg, action)

    def test_successful_install_is_recorded(self):
        state = agent.apply_op_result({}, self._op("alpha", agent.ACTION_INSTALL), ok=True)
        assert state == {"alpha": {"status": agent.RESULT_INSTALLED}}

    def test_failed_install_is_not_recorded(self):
        state = agent.apply_op_result({}, self._op("alpha", agent.ACTION_INSTALL), ok=False)
        assert state == {}

    def test_failed_update_keeps_the_package_managed_but_not_successful(self):
        before = {"alpha": {"status": agent.RESULT_INSTALLED}}
        state = agent.apply_op_result(before, self._op("alpha", agent.ACTION_UPDATE), ok=False)
        assert state["alpha"]["status"] == agent.RESULT_FAILED

    def test_successful_removal_drops_the_entry(self):
        before = {"alpha": {"status": agent.RESULT_INSTALLED}}
        state = agent.apply_op_result(before, self._op("alpha", agent.ACTION_REMOVE), ok=True)
        assert state == {}

    def test_failed_removal_stays_pending(self):
        before = {"alpha": {"status": agent.RESULT_INSTALLED}}
        state = agent.apply_op_result(before, self._op("alpha", agent.ACTION_REMOVE), ok=False)
        assert "alpha" in state
        # Still deselected + still managed, so the next reconcile retries removal.
        ops, _ = agent.plan_reconciliation([], list(state), FAKE_REGISTRY)
        assert _actions(ops) == [("alpha", agent.ACTION_REMOVE)]

    def test_does_not_mutate_the_input(self):
        before = {"alpha": {"status": agent.RESULT_INSTALLED}}
        agent.apply_op_result(before, self._op("alpha", agent.ACTION_REMOVE), ok=True)
        assert before == {"alpha": {"status": agent.RESULT_INSTALLED}}


class TestStateSerialization:
    def test_round_trips(self):
        import json

        state = {"alpha": {"status": agent.RESULT_INSTALLED}}
        assert agent.parse_extras_state(json.loads(agent.extras_state_json(state))) == state

    def test_records_the_schema_version(self):
        import json

        assert json.loads(agent.extras_state_json({}))["schema"] == agent.EXTRAS_STATE_SCHEMA

    def test_malformed_state_degrades_to_nothing_managed(self):
        assert agent.parse_extras_state(None) == {}
        assert agent.parse_extras_state({"packages": "nope"}) == {}
        assert agent.parse_extras_state({}) == {}


class TestRemovalSafety:
    def test_removal_only_touches_declared_paths(self):
        cmd = agent.remove_command(FAKE_REGISTRY[0])
        assert cmd.strip() == 'rm -rf "$HOME/.local/bin/alpha"'

    def test_uninstaller_runs_first_and_cannot_block_cleanup(self):
        cmd = agent.remove_command(FAKE_REGISTRY[1])
        assert cmd.startswith("uninstall-beta || true")
        assert 'rm -rf "$HOME/.beta"' in cmd

    def test_no_registry_entry_claims_a_broad_shared_directory(self):
        forbidden = {"", ".", "..", ".local", ".local/bin", ".local/share", ".local/state", ".cache", ".config"}
        for entry in agent.EXTRA_REGISTRY:
            for path in entry.owned_paths:
                assert path not in forbidden, f"{entry.id} claims shared path {path!r}"
                assert not path.startswith("/"), f"{entry.id} claims absolute path {path!r}"
                assert ".." not in path.split("/"), f"{entry.id} escapes home with {path!r}"

    def test_every_registry_removal_mentions_only_its_own_paths(self):
        for entry in agent.EXTRA_REGISTRY:
            deletions = agent.remove_command(entry).split("rm -rf ", 1)[1].strip()
            assert deletions == " ".join(f'"$HOME/{p}"' for p in entry.owned_paths)


class TestLaunchMetadata:
    def test_only_agent_entries_are_launchable(self):
        assert [e.id for e in agent.launchable_agents(FAKE_REGISTRY)] == ["beta"]

    def test_non_agent_extras_are_not_launch_targets(self):
        launchable = {e.id for e in agent.launchable_agents()}
        assert launchable == {"claude-code", "codex", "mistral-vibe", "opencode"}
        assert {"ollama", "uv", "ruff", "ty", "typos", "typst", "bun"} & launchable == set()

    def test_default_agent_is_launchable(self):
        assert agent.DEFAULT_AGENT_ID in {e.id for e in agent.launchable_agents()}

    def test_launch_falls_back_for_an_unknown_agent(self):
        assert agent._launch_agent("gone").executable == "claude"

    def test_launch_falls_back_for_a_non_agent_extra(self):
        assert agent._launch_agent("ollama").executable == "claude"


class TestOpCommands:
    def test_install_and_update_use_their_own_routines(self):
        assert "\ninstall-alpha\n" in agent.op_command(agent.ExtraOp("alpha", agent.ACTION_INSTALL), FAKE_REGISTRY)
        assert "\nupdate-alpha\n" in agent.op_command(agent.ExtraOp("alpha", agent.ACTION_UPDATE), FAKE_REGISTRY)

    def test_a_failing_routine_fails_the_operation(self):
        # Without `set -e` the trailing executable check has the last word, so an
        # update that failed while leaving the old binary in place would pass.
        for action in (agent.ACTION_INSTALL, agent.ACTION_UPDATE):
            assert agent.op_command(agent.ExtraOp("alpha", action), FAKE_REGISTRY).startswith("set -e\n")

    def test_install_verifies_the_declared_executables(self):
        # An installer that exits 0 without producing a working tool must fail.
        cmd = agent.op_command(agent.ExtraOp("alpha", agent.ACTION_INSTALL), FAKE_REGISTRY)
        assert "command -v alpha >/dev/null ||" in cmd
        assert "exit 1" in cmd

    def test_every_registry_entry_declares_an_executable_to_verify(self):
        for entry in agent.EXTRA_REGISTRY:
            assert entry.executables, f"{entry.id} declares no executable"
            for exe in entry.executables:
                assert f"command -v {exe} " in agent.op_command(agent.ExtraOp(entry.id, agent.ACTION_INSTALL))

    def test_removal_is_not_followed_by_a_verification(self):
        assert "command -v" not in agent.op_command(agent.ExtraOp("alpha", agent.ACTION_REMOVE), FAKE_REGISTRY)

    def test_unknown_package_has_no_command(self):
        assert agent.op_command(agent.ExtraOp("ghost", agent.ACTION_INSTALL), FAKE_REGISTRY) is None


class TestNaming:
    def test_image_home_and_container_names_are_user_scoped(self):
        assert agent.image_tag("hold") == "agent:hold"
        assert agent.home_volume("hold") == "agent-home-hold"

    def test_run_container_name_is_user_plus_path_hash(self):
        name = agent.run_container_name("hold", agent.Path("/repo"))
        assert name.startswith("agent-run-hold-")
        assert len(name.removeprefix("agent-run-hold-")) == 12

    def test_run_container_names_differ_per_repo_and_per_user(self):
        a = agent.run_container_name("hold", agent.Path("/repo/a"))
        b = agent.run_container_name("hold", agent.Path("/repo/b"))
        c = agent.run_container_name("other", agent.Path("/repo/a"))
        assert len({a, b, c}) == 3

    def test_every_run_container_matches_the_general_prefix(self):
        assert agent.run_container_name("hold", agent.Path("/x")).startswith(agent.RUN_CONTAINER_PREFIX)


class TestVersionReporting:
    def _outcome(self, action, ok, before, after):
        return agent.op_outcome(agent.ExtraOp("alpha", action), ok, before, after)

    def test_version_is_extracted_from_any_decorated_output(self):
        report = agent.parse_version_report(
            "uv\tuv 0.11.32 (aarch64-unknown-linux-gnu)\n"
            "typos\ttypos-cli 1.48.0\n"
            "ollama\tWarning: could not connect Warning: client version is 0.32.4\n"
        )
        assert report == {"uv": "0.11.32", "typos": "1.48.0", "ollama": "0.32.4"}

    def test_a_tool_that_reports_nothing_has_no_version(self):
        assert agent.parse_version_report("uv\t\nbun\tcommand not found\n") == {}

    def test_a_moved_version_is_reported_as_a_transition(self):
        assert self._outcome(agent.ACTION_UPDATE, True, {"alpha": "1.0"}, {"alpha": "1.1"}) == (agent.RESULT_UPDATED, "1.0 → 1.1")

    def test_an_unmoved_version_is_reported_as_up_to_date(self):
        assert self._outcome(agent.ACTION_UPDATE, True, {"alpha": "1.0"}, {"alpha": "1.0"}) == (agent.RESULT_UNCHANGED, "1.0")

    def test_an_install_reports_only_the_new_version(self):
        assert self._outcome(agent.ACTION_INSTALL, True, {}, {"alpha": "1.0"}) == (agent.RESULT_INSTALLED, "1.0")

    def test_a_failure_reports_no_version_at_all(self):
        assert self._outcome(agent.ACTION_UPDATE, False, {"alpha": "1.0"}, {"alpha": "1.1"}) == (agent.RESULT_FAILED, "")

    def test_removal_reports_no_version(self):
        assert self._outcome(agent.ACTION_REMOVE, True, {"alpha": "1.0"}, {}) == (agent.RESULT_REMOVED, "")

    def test_an_unreportable_tool_still_gets_its_outcome(self):
        assert self._outcome(agent.ACTION_UPDATE, True, {}, {}) == (agent.RESULT_UPDATED, "")

    def test_the_probe_asks_every_requested_package_and_nothing_else(self):
        script = agent.version_probe_script(["uv", "typst"])
        assert '"uv:uv"' in script and '"typst:typst"' in script
        assert "ruff" not in script

    def test_ollama_does_not_redownload_when_it_is_already_the_latest_release(self):
        script = agent.extra_by_id("ollama").update
        guard, _, download = script.partition("set -e\n")
        assert "exit 0" in guard and "releases/latest" in guard
        # The exit is reached before anything is fetched or deleted.
        assert "tar --zstd" in download and "rm -rf" in download

    def test_tectonic_does_not_redownload_when_it_is_already_the_latest_release(self):
        script = agent.extra_by_id("tectonic").update
        guard, _, download = script.partition("set -e\n")
        assert "exit 0" in guard and "releases/latest" in guard
        assert "tar -xz" in download and "rm -rf" in download

    def test_tectonic_downloads_the_release_the_guard_resolved(self):
        # Tectonic's asset names embed the version, so the tag the guard resolved
        # has to reach the URL — a second lookup would be one more chance to fail.
        assert "$latest" in agent.extra_by_id("tectonic").install.partition("set -e\n")[2]


class TestReleaseVersionParsing:
    """The guard resolves a release version out of the URL that
    github.com/<repo>/releases/latest redirects to. Tag conventions differ
    between projects, and a version that fails to parse silently disables the
    guard, so the extraction is checked against the conventions in the registry."""

    def _parse(self, url: str) -> str:
        done = subprocess.run(
            ["sh", "-c", agent._TAG_URL_TO_VERSION],
            input=url,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def test_reads_a_v_prefixed_tag(self):
        assert self._parse("https://github.com/typst/typst/releases/tag/v0.13.1") == "0.13.1"

    def test_reads_a_name_prefixed_tag(self):
        url = "https://github.com/tectonic-typesetting/tectonic/releases/tag/tectonic@0.17.0"
        assert self._parse(url) == "0.17.0"

    def test_reads_a_tag_the_release_feed_percent_encoded(self):
        url = "https://github.com/tectonic-typesetting/tectonic/releases/tag/tectonic%400.17.0"
        assert self._parse(url) == "0.17.0"

    def test_reports_nothing_for_a_url_without_a_version(self):
        assert self._parse("https://github.com/tectonic-typesetting/tectonic/releases") == ""


class TestMonorepoTagResolution:
    """Tectonic publishes sibling crates — `tectonic_pdf_io@…` and a dozen more —
    under the same tag namespace as the program, so /releases/latest can name a
    crate release. Its version would then fail the guard and, worse, build a
    download URL for an asset that does not exist."""

    def test_tectonic_falls_back_to_the_release_feed(self):
        guard = agent.extra_by_id("tectonic").install
        assert "releases.atom" in guard
        assert "releases/tag/tectonic(@|%40)" in guard

    def test_single_project_repos_ask_only_for_the_redirect(self):
        # The feed lists -rc tags that typst and ollama never publish downloads
        # for, so consulting it unconditionally would break their guards.
        for entry_id in ("typst", "ollama"):
            assert "releases.atom" not in agent.extra_by_id(entry_id).install
