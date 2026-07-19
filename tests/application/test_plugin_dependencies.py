"""Tests for app.application.plugin_dependency (pure module).

Covers S2 (topological order + cycle detection), S3 (pip check matrix +
external normalization), S4 (dependency_status structure + status priority).
S1 (effective enabled set) is covered in test_plugin_service.py because it
exercises PluginService.scan orchestration.
"""

from __future__ import annotations

import importlib.metadata
from unittest.mock import patch

import pytest

from app.application.plugin_dependency import (
    DEP_CYCLE,
    DEP_DISABLED,
    DEP_LOAD_FAILED,
    DEP_MISSING,
    DEP_OK,
    DEP_UNAVAILABLE,
    DEP_UNSUPPORTED,
    PIP_INCOMPATIBLE,
    PIP_MISSING,
    PIP_OK,
    PIP_SKIPPED,
    STATUS_FAILED,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNSUPPORTED,
    DepAvailability,
    PipCheckResult,
    TopoResult,
    build_dependency_status,
    check_pip_dependency,
    classify_dep,
    compute_overall_status,
    cycle_error_message,
    highest_status,
    normalize_external,
    topological_order,
)
from app.domain.plugin import PluginKind, PluginManifest, PluginSource


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _manifest(
    key: str,
    *,
    requires_plugins: list[str] | None = None,
    pip_dependencies: list[str] | None = None,
    external_dependencies: list[dict] | None = None,
    kind: PluginKind = PluginKind.STANDALONE,
) -> PluginManifest:
    return PluginManifest(
        key=key,
        name=key,
        version="1.0.0",
        description="",
        source=PluginSource.BUNDLED,
        path=f"/p/{key}",
        kind=kind,
        requires_plugins=list(requires_plugins or []),
        pip_dependencies=list(pip_dependencies or []),
        external_dependencies=list(external_dependencies or []),
    )


# ===========================================================================
# S2: topological order + cycle detection
# ===========================================================================


class TestTopologicalOrder:
    def test_chain_orders_dependencies_first(self):
        # a requires b, b requires c -> order c, b, a
        res = topological_order(
            enabled_keys={"a", "b", "c"},
            requires_plugins_by_key={"a": ["b"], "b": ["c"]},
            discovery_index={"a": 0, "b": 1, "c": 2},
        )
        assert res.order == ["c", "b", "a"]
        assert res.cycles == []
        assert not res.cycle_members

    def test_only_enabled_winners_participate(self):
        # 'disabled' is not in enabled_keys; edges to it are ignored.
        res = topological_order(
            enabled_keys={"a", "b"},
            requires_plugins_by_key={"a": ["b", "disabled"]},
            discovery_index={"a": 0, "b": 1},
        )
        assert res.order == ["b", "a"]
        assert not res.cycle_members

    def test_same_layer_sorted_by_discovery_index_then_key(self):
        # x and y both have no deps; z requires both. Layer 0 = {x, y}.
        # discovery_index y=2 < x=5, so y before x; z last.
        res = topological_order(
            enabled_keys={"x", "y", "z"},
            requires_plugins_by_key={"z": ["x", "y"]},
            discovery_index={"x": 5, "y": 2, "z": 0},
        )
        assert res.order == ["y", "x", "z"]

    def test_same_layer_tie_discovery_index_equal_sorts_by_key(self):
        res = topological_order(
            enabled_keys={"b", "a"},
            requires_plugins_by_key={},
            discovery_index={"a": 0, "b": 0},
        )
        assert res.order == ["a", "b"]

    def test_single_cycle_members_excluded_and_sorted(self):
        res = topological_order(
            enabled_keys={"a", "b"},
            requires_plugins_by_key={"a": ["b"], "b": ["a"]},
            discovery_index={"a": 0, "b": 1},
        )
        assert res.order == []
        assert sorted(res.cycle_members) == ["a", "b"]
        assert len(res.cycles) == 1
        assert res.cycles[0] == ["a", "b"]

    def test_multi_cycle_independent_branch_continues(self):
        # Two disjoint cycles a<->b and c<->d, plus independent e.
        res = topological_order(
            enabled_keys={"a", "b", "c", "d", "e"},
            requires_plugins_by_key={
                "a": ["b"], "b": ["a"], "c": ["d"], "d": ["c"],
            },
            discovery_index={"a": 0, "b": 1, "c": 2, "d": 3, "e": 4},
        )
        assert res.order == ["e"]
        assert sorted(res.cycle_members) == ["a", "b", "c", "d"]

    def test_cycle_error_message_sorted_keys(self):
        msg = cycle_error_message(["b", "a", "c"])
        assert msg == "circular plugin dependency: a, b, c"

    def test_diamond_dependency(self):
        # d requires b and c; b requires a; c requires a.
        # Layer 0: a; Layer 1: b, c; Layer 2: d.
        res = topological_order(
            enabled_keys={"a", "b", "c", "d"},
            requires_plugins_by_key={
                "d": ["b", "c"], "b": ["a"], "c": ["a"],
            },
            discovery_index={"a": 0, "b": 1, "c": 2, "d": 3},
        )
        assert res.order[0] == "a"
        assert res.order[-1] == "d"
        assert set(res.order[1:3]) == {"b", "c"}
        assert not res.cycle_members

    def test_node_depending_on_cycle_is_not_cycle_member(self):
        """A requires B, B<->C cycle. A is NOT a cycle member; A is blocked."""
        res = topological_order(
            enabled_keys={"a", "b", "c"},
            requires_plugins_by_key={
                "a": ["b"], "b": ["c"], "c": ["b"],
            },
            discovery_index={"a": 0, "b": 1, "c": 2},
        )
        # B and C are cycle members; A is NOT
        assert "b" in res.cycle_members
        assert "c" in res.cycle_members
        assert "a" not in res.cycle_members
        # A is blocked (depends on cycle but not in it)
        assert "a" in res.blocked_keys
        # A is not in the topological order (couldn't be emitted)
        assert "a" not in res.order
        # The cycle has only B and C
        assert len(res.cycles) == 1
        assert res.cycles[0] == ["b", "c"]

    def test_self_loop_is_cycle_member(self):
        """A node that depends on itself is a cycle member (self-loop)."""
        res = topological_order(
            enabled_keys={"a"},
            requires_plugins_by_key={"a": ["a"]},
            discovery_index={"a": 0},
        )
        assert "a" in res.cycle_members
        assert "a" not in res.order
        assert "a" not in res.blocked_keys

    def test_transitive_blocked_node_classified(self):
        """A -> B -> C<->D cycle. B and A are blocked (not cycle members)."""
        res = topological_order(
            enabled_keys={"a", "b", "c", "d"},
            requires_plugins_by_key={
                "a": ["b"], "b": ["c"], "c": ["d"], "d": ["c"],
            },
            discovery_index={"a": 0, "b": 1, "c": 2, "d": 3},
        )
        # C and D are cycle members
        assert {"c", "d"} == set(res.cycle_members)
        # A and B are blocked
        assert "a" in res.blocked_keys
        assert "b" in res.blocked_keys
        assert "a" not in res.cycle_members
        assert "b" not in res.cycle_members


class TestClassifyDep:
    def test_dep_not_discovered_is_missing(self):
        d = classify_dep(
            "ghost",
            discovered_keys={"a"},
            enabled_keys={"a"},
            unsupported_keys=set(),
            cycle_members=set(),
            load_failed_keys=set(),
            unavailable_keys=set(),
        )
        assert d.available is False
        assert d.reason == DEP_MISSING
        assert d.diagnostic == "missing required plugin: ghost"

    def test_dep_disabled_is_missing_required(self):
        d = classify_dep(
            "dep",
            discovered_keys={"dep"},
            enabled_keys=set(),  # not enabled
            unsupported_keys=set(),
            cycle_members=set(),
            load_failed_keys=set(),
            unavailable_keys=set(),
        )
        assert d.available is False
        assert d.reason == DEP_DISABLED
        assert d.diagnostic == "missing required plugin: dep"

    def test_dep_unsupported_is_missing_required(self):
        d = classify_dep(
            "dep",
            discovered_keys={"dep"},
            enabled_keys={"dep"},
            unsupported_keys={"dep"},
            cycle_members=set(),
            load_failed_keys=set(),
            unavailable_keys=set(),
        )
        assert d.available is False
        assert d.reason == DEP_UNSUPPORTED
        assert d.diagnostic == "missing required plugin: dep"

    def test_dep_load_failed_is_missing_required(self):
        d = classify_dep(
            "dep",
            discovered_keys={"dep"},
            enabled_keys={"dep"},
            unsupported_keys=set(),
            cycle_members=set(),
            load_failed_keys={"dep"},
            unavailable_keys=set(),
        )
        assert d.available is False
        assert d.reason == DEP_LOAD_FAILED
        assert d.diagnostic == "missing required plugin: dep"

    def test_dep_cycle_is_unavailable(self):
        d = classify_dep(
            "dep",
            discovered_keys={"dep"},
            enabled_keys={"dep"},
            unsupported_keys=set(),
            cycle_members={"dep"},
            load_failed_keys=set(),
            unavailable_keys=set(),
        )
        assert d.available is False
        assert d.reason == DEP_CYCLE
        assert d.diagnostic == "required plugin unavailable: dep"

    def test_dep_transitive_failure_is_unavailable(self):
        d = classify_dep(
            "dep",
            discovered_keys={"dep"},
            enabled_keys={"dep"},
            unsupported_keys=set(),
            cycle_members=set(),
            load_failed_keys=set(),
            unavailable_keys={"dep"},
        )
        assert d.available is False
        assert d.reason == DEP_UNAVAILABLE
        assert d.diagnostic == "required plugin unavailable: dep"

    def test_dep_ok(self):
        d = classify_dep(
            "dep",
            discovered_keys={"dep"},
            enabled_keys={"dep"},
            unsupported_keys=set(),
            cycle_members=set(),
            load_failed_keys=set(),
            unavailable_keys=set(),
        )
        assert d.available is True
        assert d.reason == DEP_OK
        assert d.diagnostic == ""


# ===========================================================================
# S3: pip check matrix
# ===========================================================================


class TestPipCheck:
    def test_satisfied_dependency(self):
        # packaging is installed in the test env.
        r = check_pip_dependency("packaging>=23")
        assert r.status == PIP_OK
        assert r.name == "packaging"
        assert r.installed_version is not None
        assert r.diagnostic == ""

    def test_missing_distribution(self):
        r = check_pip_dependency("definitely-not-installed-pkg-xyz>=1.0")
        assert r.status == PIP_MISSING
        assert "missing pip dependency" in r.diagnostic
        assert "pip install" in r.diagnostic

    def test_version_mismatch(self):
        r = check_pip_dependency("packaging==1.0.0")
        assert r.status == PIP_INCOMPATIBLE
        assert "incompatible pip dependency" in r.diagnostic
        assert "packaging" in r.diagnostic
        assert r.installed_version is not None

    def test_pillow_pil_distribution_name_not_import_name(self):
        # Pillow distribution imports as PIL; the check must use the
        # distribution name "Pillow", not the import name "PIL".
        # Mock importlib.metadata.version to verify it's called with
        # "Pillow" (distribution name), never "PIL" (import name).
        with patch(
            "app.application.plugin_dependency.importlib.metadata.version",
            return_value="10.0.0",
        ) as mock_version:
            r = check_pip_dependency("Pillow>=1.0")
        assert r.status == PIP_OK
        assert r.name == "Pillow"
        mock_version.assert_called_once_with("Pillow")
        # Verify "PIL" was never queried (import name not used)
        for call in mock_version.call_args_list:
            assert call.args[0] != "PIL"

    def test_extras_parsed(self):
        r = check_pip_dependency("packaging[extra]>=23")
        assert r.status == PIP_OK
        assert r.name == "packaging"

    def test_marker_false_is_skipped(self):
        # Marker evaluates false in any Python 3 env.
        r = check_pip_dependency('packaging; python_version < "2.0"')
        assert r.status == PIP_SKIPPED
        assert r.diagnostic == ""

    def test_empty_spec_is_ok(self):
        r = check_pip_dependency("")
        assert r.status == PIP_OK
        assert r.name == ""

    def test_never_installs_packages(self):
        # The function must not attempt to install; a missing package stays
        # missing (no side effect). We verify by checking no pip subprocess
        # is spawned: the function only uses importlib.metadata.
        import app.application.plugin_dependency as mod
        assert not hasattr(mod, "pip")  # no pip import
        r = check_pip_dependency("another-missing-pkg-xyz-123>=1.0")
        assert r.status == PIP_MISSING

    def test_packaging_unavailable_falls_back_to_existence(self, monkeypatch):
        # Simulate packaging not being available.
        import app.application.plugin_dependency as mod
        monkeypatch.setattr(mod, "_PACKAGING_AVAILABLE", False)
        # packaging distribution exists -> OK with warning diagnostic.
        r = check_pip_dependency("packaging>=23")
        assert r.status == PIP_OK
        assert r.diagnostic == "dependency_version_check_unavailable"

        r2 = check_pip_dependency("no-such-dist-xyz-999>=1.0")
        assert r2.status == PIP_MISSING
        assert "missing pip dependency" in r2.diagnostic


class TestExternalNormalization:
    def test_external_only_name_install_check_projected(self):
        ext = normalize_external([
            {"name": "ffmpeg", "install": "apt install ffmpeg", "check": "ffmpeg -version"},
            {"name": "docker"},
        ])
        assert ext == [
            {"name": "ffmpeg", "install": "apt install ffmpeg", "check": "ffmpeg -version"},
            {"name": "docker"},
        ]

    def test_external_drops_unknown_keys(self):
        ext = normalize_external([
            {"name": "x", "dangerous": "rm -rf /", "install": "safe"},
        ])
        assert ext == [{"name": "x", "install": "safe"}]

    def test_external_empty_name_skipped(self):
        ext = normalize_external([{"name": ""}, {"name": "ok"}])
        assert ext == [{"name": "ok"}]

    def test_external_never_executes(self):
        # install/check strings are just data; no execution path exists.
        ext = normalize_external([{"name": "x", "install": "echo hi"}])
        assert ext[0]["install"] == "echo hi"  # unchanged string


# ===========================================================================
# S4: dependency_status structure + status priority
# ===========================================================================


class TestBuildDependencyStatus:
    def test_has_pip_requires_plugins_external_warnings(self):
        pip = [PipCheckResult("packaging>=23", "packaging", PIP_OK, "26.2", "")]
        deps = [DepAvailability("dep", True, DEP_OK, "")]
        ext = [{"name": "ffmpeg"}]
        status = build_dependency_status(
            pip_results=pip,
            dep_availability=deps,
            external=ext,
            warnings=["w1"],
        )
        assert set(status.keys()) == {"pip", "requires_plugins", "external", "warnings"}
        assert status["pip"][0]["spec"] == "packaging>=23"
        assert status["pip"][0]["status"] == PIP_OK
        assert status["requires_plugins"][0]["key"] == "dep"
        assert status["requires_plugins"][0]["available"] is True
        assert status["external"] == [{"name": "ffmpeg"}]
        assert status["warnings"] == ["w1"]

    def test_external_normalized_in_status(self):
        status = build_dependency_status(
            pip_results=[],
            dep_availability=[],
            external=[{"name": "x", "extra": "drop"}],
            warnings=[],
        )
        assert status["external"] == [{"name": "x"}]


class TestHighestStatus:
    def test_priority_missing_beats_all(self):
        assert highest_status([STATUS_OK, STATUS_MISSING, STATUS_FAILED]) == STATUS_MISSING

    def test_priority_unsupported_beats_failed_partial_ok(self):
        assert highest_status([STATUS_FAILED, STATUS_UNSUPPORTED, STATUS_PARTIAL]) == STATUS_UNSUPPORTED

    def test_priority_failed_beats_partial_ok(self):
        assert highest_status([STATUS_PARTIAL, STATUS_FAILED, STATUS_OK]) == STATUS_FAILED

    def test_priority_partial_beats_ok(self):
        assert highest_status([STATUS_OK, STATUS_PARTIAL]) == STATUS_PARTIAL

    def test_empty_is_ok(self):
        assert highest_status([]) == STATUS_OK

    def test_all_ok(self):
        assert highest_status([STATUS_OK, STATUS_OK]) == STATUS_OK


class TestComputeOverallStatus:
    def test_discovery_failed_is_failed(self):
        status, err = compute_overall_status(
            manifest_ok=False,
            is_unsupported=False,
            in_cycle=False,
            cycle_error="",
            dep_availability=[],
            pip_results=[],
            load_error=None,
            packaging_warnings=[],
        )
        assert status == STATUS_FAILED
        assert "discovery_failed" in err

    def test_unsupported_kind(self):
        status, err = compute_overall_status(
            manifest_ok=True,
            is_unsupported=True,
            in_cycle=False,
            cycle_error="",
            dep_availability=[],
            pip_results=[],
            load_error=None,
            packaging_warnings=[],
        )
        assert status == STATUS_UNSUPPORTED

    def test_cycle_is_failed(self):
        status, err = compute_overall_status(
            manifest_ok=True,
            is_unsupported=False,
            in_cycle=True,
            cycle_error="circular plugin dependency: a, b",
            dep_availability=[],
            pip_results=[],
            load_error=None,
            packaging_warnings=[],
        )
        assert status == STATUS_FAILED
        assert err == "circular plugin dependency: a, b"

    def test_load_error_is_failed(self):
        status, err = compute_overall_status(
            manifest_ok=True,
            is_unsupported=False,
            in_cycle=False,
            cycle_error="",
            dep_availability=[],
            pip_results=[],
            load_error="register_failed: boom",
            packaging_warnings=[],
        )
        assert status == STATUS_FAILED
        assert "register_failed" in err

    def test_dep_missing_is_partial(self):
        deps = [DepAvailability("ghost", False, DEP_MISSING, "missing required plugin: ghost")]
        status, err = compute_overall_status(
            manifest_ok=True,
            is_unsupported=False,
            in_cycle=False,
            cycle_error="",
            dep_availability=deps,
            pip_results=[],
            load_error=None,
            packaging_warnings=[],
        )
        assert status == STATUS_PARTIAL
        assert "missing required plugin: ghost" in err

    def test_dep_unavailable_is_partial(self):
        deps = [DepAvailability("dep", False, DEP_UNAVAILABLE, "required plugin unavailable: dep")]
        status, err = compute_overall_status(
            manifest_ok=True,
            is_unsupported=False,
            in_cycle=False,
            cycle_error="",
            dep_availability=deps,
            pip_results=[],
            load_error=None,
            packaging_warnings=[],
        )
        assert status == STATUS_PARTIAL
        assert "required plugin unavailable: dep" in err

    def test_pip_missing_is_partial(self):
        pip = [PipCheckResult("nope>=1", "nope", PIP_MISSING, None, "missing pip dependency: nope")]
        status, err = compute_overall_status(
            manifest_ok=True,
            is_unsupported=False,
            in_cycle=False,
            cycle_error="",
            dep_availability=[],
            pip_results=pip,
            load_error=None,
            packaging_warnings=[],
        )
        assert status == STATUS_PARTIAL
        assert "missing pip dependency" in err

    def test_pip_incompatible_is_partial(self):
        pip = [PipCheckResult("packaging==1", "packaging", PIP_INCOMPATIBLE, "26.2", "incompatible")]
        status, err = compute_overall_status(
            manifest_ok=True,
            is_unsupported=False,
            in_cycle=False,
            cycle_error="",
            dep_availability=[],
            pip_results=pip,
            load_error=None,
            packaging_warnings=[],
        )
        assert status == STATUS_PARTIAL

    def test_all_ok(self):
        deps = [DepAvailability("dep", True, DEP_OK, "")]
        pip = [PipCheckResult("packaging>=23", "packaging", PIP_OK, "26.2", "")]
        status, err = compute_overall_status(
            manifest_ok=True,
            is_unsupported=False,
            in_cycle=False,
            cycle_error="",
            dep_availability=deps,
            pip_results=pip,
            load_error=None,
            packaging_warnings=[],
        )
        assert status == STATUS_OK
        assert err == ""

    def test_packaging_unavailable_warning_recorded(self):
        # When packaging is unavailable and dist exists, pip result carries
        # a "dependency_version_check_unavailable" diagnostic on PIP_OK.
        pip = [PipCheckResult("packaging>=23", "packaging", PIP_OK, None, "dependency_version_check_unavailable")]
        warnings: list[str] = []
        status, err = compute_overall_status(
            manifest_ok=True,
            is_unsupported=False,
            in_cycle=False,
            cycle_error="",
            dep_availability=[],
            pip_results=pip,
            load_error=None,
            packaging_warnings=warnings,
        )
        assert status == STATUS_OK
        assert "dependency_version_check_unavailable" in warnings

    def test_priority_failed_over_partial(self):
        # Both a load_error (FAILED) and dep missing (PARTIAL): FAILED wins.
        deps = [DepAvailability("g", False, DEP_MISSING, "missing required plugin: g")]
        status, _ = compute_overall_status(
            manifest_ok=True,
            is_unsupported=False,
            in_cycle=False,
            cycle_error="",
            dep_availability=deps,
            pip_results=[],
            load_error="register_failed: x",
            packaging_warnings=[],
        )
        assert status == STATUS_FAILED
