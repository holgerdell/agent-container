"""Pure-logic tests for the persistent shared /nix store lifecycle.

These cover the tricky *rules* the shared-store commands hang off — generation
retention, session-root reaping, generation parsing, volume naming — as pure
functions, so the orchestration around them can be integration-verified instead
of mocked.
"""

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

# `agent` is an extensionless script, so load it by explicit source loader.
_loader = SourceFileLoader("agent_cli", str(Path(__file__).resolve().parent.parent / "agent"))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
assert _spec
agent = importlib.util.module_from_spec(_spec)
_loader.exec_module(agent)


class TestNixSharedVolume:
    def test_names_from_username(self):
        assert agent.nix_shared_volume("hold") == "agent-nix-hold"

    def test_distinct_from_build_cache(self):
        # The build cache lives outside the `agent-nix-` store namespace
        # (`agent-buildcache`), so no username can ever collide with it.
        assert agent.nix_shared_volume("alice") != agent.NIX_BUILD_CACHE_VOLUME
        assert not agent.NIX_BUILD_CACHE_VOLUME.startswith(agent.NIX_SHARED_VOLUME_PREFIX)


class TestParseGeneration:
    def test_bare_link_name(self):
        assert agent.parse_generation("default-1-link") == 1
        assert agent.parse_generation("default-42-link") == 42

    def test_full_path(self):
        assert agent.parse_generation("/nix/var/nix/profiles/default-7-link") == 7

    def test_trailing_whitespace(self):
        assert agent.parse_generation("default-3-link\n") == 3

    def test_non_generation(self):
        assert agent.parse_generation("default") is None
        assert agent.parse_generation("") is None
        assert agent.parse_generation("/nix/store/abc-agent-toolchain") is None


class TestToolchainDrift:
    """`_check_toolchain` folds the flake.lock-vs-store drift signal into its own
    check as an advisory `warn` — never a `fail` that would trip `agent doctor &&`."""

    def _probe(self, **kw):
        base = {"claude": "9", "opencode": "1", "nofile": "1048576", "generation": 3, "lock_date": "2026-07-19"}
        base.update(kw)
        return agent.Probe(**base)

    def test_in_sync_is_ok_and_quiet(self):
        p = self._probe(applied="/nix/store/aaa-toolchain", desired="/nix/store/aaa-toolchain")
        c = agent._check_toolchain(p)
        assert c.status == "ok"
        assert "sync" not in (c.detail or "")

    def test_drift_warns_and_points_at_update(self):
        p = self._probe(applied="/nix/store/aaa-toolchain", desired="/nix/store/bbb-toolchain")
        c = agent._check_toolchain(p)
        assert c.status == "warn"  # advisory, so `agent doctor && …` still passes
        # The drift reason is dejargoned (no `flake.lock`/nix in user-facing text) and
        # points at the routine, live-safe fix.
        assert "flake.lock" not in (c.fix or "")
        assert "agent update" in (c.fix or "")

    def test_unknown_never_claims_in_sync(self):
        # Seed store: eval couldn't answer offline → desired is None.
        p = self._probe(generation=3, applied="/nix/store/aaa-toolchain", desired=None)
        c = agent._check_toolchain(p)
        assert c.status == "ok"
        assert "sync unknown" in (c.detail or "")

    def test_gen1_seed_omits_redundant_sync_note(self):
        # The "seed" tag already explains why sync is unknown — don't repeat it.
        p = self._probe(generation=1, lock_date=None, applied="/nix/store/aaa", desired=None)
        c = agent._check_toolchain(p)
        assert "seed" in (c.detail or "")
        assert "sync unknown" not in (c.detail or "")


class TestGenerationsToDelete:
    def test_no_live_sessions_keeps_only_current(self):
        # gens 1,2,3 present, current 3, nothing live → drop 1 and 2.
        assert agent.generations_to_delete([1, 2, 3], current=3, live_gens=[]) == [1, 2]

    def test_keeps_current_even_if_listed(self):
        assert 3 not in agent.generations_to_delete([1, 2, 3], current=3, live_gens=[])

    def test_live_session_pins_its_generation_and_newer(self):
        # A session started on gen 2; current is 4. Keep 2,3,4; drop 1.
        assert agent.generations_to_delete([1, 2, 3, 4], current=4, live_gens=[2]) == [1]

    def test_oldest_live_session_sets_the_floor(self):
        # Two sessions on gens 2 and 3 → oldest is 2 → keep >= 2.
        assert agent.generations_to_delete([1, 2, 3, 4], current=4, live_gens=[3, 2]) == [1]

    def test_nothing_to_delete(self):
        assert agent.generations_to_delete([3], current=3, live_gens=[]) == []
        assert agent.generations_to_delete([2, 3], current=3, live_gens=[2]) == []


class TestSessionRootsToReap:
    def test_reaps_roots_for_stopped_containers(self):
        roots = ["session-agent-run-aaa", "session-agent-run-bbb"]
        running = {"agent-run-aaa"}
        assert agent.session_roots_to_reap(roots, running) == ["session-agent-run-bbb"]

    def test_keeps_all_when_all_running(self):
        roots = ["session-agent-run-aaa"]
        assert agent.session_roots_to_reap(roots, {"agent-run-aaa"}) == []

    def test_reaps_all_when_none_running(self):
        roots = ["session-agent-run-aaa", "session-agent-run-bbb"]
        assert agent.session_roots_to_reap(roots, set()) == roots

    def test_ignores_non_session_entries(self):
        roots = ["profiles", "docker", "session-agent-run-aaa"]
        assert agent.session_roots_to_reap(roots, set()) == ["session-agent-run-aaa"]
