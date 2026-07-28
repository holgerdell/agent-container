"""Pure-logic tests for the APT package catalog.

These cover the rules `agent setup` and `agent build` hang off — normalization,
the fingerprint that detects image drift, group-state derivation, and how group
toggles fold back into the flat settings list.
"""

from tests.agent_module import agent


class TestNormalization:
    def test_trims_dedupes_and_sorts(self):
        assert agent.normalize_apt_packages([" git ", "curl", "git", ""]) == ["curl", "git"]

    def test_non_list_is_empty(self):
        assert agent.normalize_apt_packages("git") == []
        assert agent.normalize_apt_packages(None) == []

    def test_drops_non_strings(self):
        assert agent.normalize_apt_packages(["git", 7, None]) == ["git"]


class TestFingerprint:
    def test_order_and_duplicates_do_not_matter(self):
        assert agent.apt_fingerprint(["b", "a"]) == agent.apt_fingerprint(["a", "b", "a"])

    def test_different_selections_differ(self):
        assert agent.apt_fingerprint(["a"]) != agent.apt_fingerprint(["a", "b"])

    def test_stable_across_calls(self):
        assert agent.apt_fingerprint(["git"]) == agent.apt_fingerprint(["git"])


class TestCatalog:
    def test_every_package_belongs_to_exactly_one_group(self):
        seen: dict[str, str] = {}
        for group in agent.APT_GROUPS:
            for pkg in group.packages:
                assert pkg not in seen, f"{pkg} is in both {seen.get(pkg)} and {group.id}"
                seen[pkg] = group.id

    def test_defaults_are_the_whole_catalog(self):
        assert set(agent.default_apt_packages()) == agent.catalog_packages()

    def test_defaults_cover_the_supplied_product_catalog(self):
        # The initial selection is seeded from the supplied packages.txt list,
        # minus `locales`: the image gets UTF-8 from glibc's built-in C.UTF-8, so
        # nothing needs generated locale data any more.
        supplied = {
            "biber",
            "build-essential",
            "curl",
            "git",
            "jq",
            "neovim",
            "nodejs",
            "npm",
            "python3",
            "ripgrep",
            "starship",
            "texlive-xetex",
            "zstd",
        }
        assert supplied <= set(agent.default_apt_packages())


class TestGroupState:
    def _group(self):
        return agent.AptGroup("g", "G", "", ("a", "b", "c"))

    def test_all_present_is_selected(self):
        assert agent.group_state(self._group(), ["a", "b", "c"]) == agent.GROUP_SELECTED

    def test_none_present_is_unselected(self):
        assert agent.group_state(self._group(), ["x"]) == agent.GROUP_UNSELECTED

    def test_some_present_is_partial(self):
        assert agent.group_state(self._group(), ["a", "x"]) == agent.GROUP_PARTIAL

    def test_hand_edited_single_package_renders_partial(self):
        latex = next(g for g in agent.APT_GROUPS if g.id == "latex")
        assert agent.group_state(latex, ["biber"]) == agent.GROUP_PARTIAL


class TestApplyGroupStates:
    GROUPS = (
        agent.AptGroup("g1", "G1", "", ("a", "b")),
        agent.AptGroup("g2", "G2", "", ("c",)),
    )

    def test_selecting_expands_to_all_members(self):
        result = agent.apply_group_states([], [("g1", agent.GROUP_SELECTED)], self.GROUPS)
        assert result == ["a", "b"]

    def test_selecting_a_partial_group_expands_it(self):
        result = agent.apply_group_states(["a"], [("g1", agent.GROUP_SELECTED)], self.GROUPS)
        assert result == ["a", "b"]

    def test_deselecting_removes_all_members(self):
        result = agent.apply_group_states(["a", "b", "c"], [("g1", agent.GROUP_UNSELECTED)], self.GROUPS)
        assert result == ["c"]

    def test_untouched_partial_group_keeps_its_packages(self):
        result = agent.apply_group_states(["a"], [("g1", agent.GROUP_PARTIAL)], self.GROUPS)
        assert result == ["a"]

    def test_custom_names_survive_every_toggle(self):
        result = agent.apply_group_states(
            ["a", "my-custom-pkg"],
            [("g1", agent.GROUP_UNSELECTED), ("g2", agent.GROUP_SELECTED)],
            self.GROUPS,
        )
        assert result == ["c", "my-custom-pkg"]

    def test_unknown_group_ids_are_ignored(self):
        assert agent.apply_group_states(["a"], [("nope", agent.GROUP_SELECTED)], self.GROUPS) == ["a"]


class TestCustomPackages:
    def test_reports_only_names_outside_the_catalog(self):
        selected = ["git", "my-tool", "another-tool"]
        assert agent.custom_apt_packages(selected) == ["another-tool", "my-tool"]

    def test_empty_when_all_known(self):
        assert agent.custom_apt_packages(agent.default_apt_packages()) == []
