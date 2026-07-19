"""Pure plugin dependency admission logic.

Stdlib-only (importlib.metadata + typing + dataclasses) with optional
``packaging.requirements`` / ``packaging.version`` fallback. No
``app.infrastructure`` import, no I/O, no plugin code execution.

Responsibilities:
- PEP 508 pip dependency check via ``packaging`` (distribution name, NOT
  import name) with a distribution-existence fallback when ``packaging`` is
  unavailable. NEVER installs packages.
- Stable Kahn topological sort over enabled winners; same-layer ties broken
  by ``(discovery_index, key)``. Cycle members excluded from the order and
  reported as sorted member lists.
- ``dependency_status`` construction for ``Plugin.capabilities`` and overall
  ``last_scan_status`` / ``last_scan_error`` derivation following the fixed
  priority MISSING > UNSUPPORTED > FAILED > PARTIAL > OK.

The module is deliberately side-effect free except for read-only
``importlib.metadata`` distribution lookups used by pip checks.
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from typing import Any, Iterable

from app.domain.plugin import PluginScanStatus


# ---------------------------------------------------------------------------
# Status constants and priority
# ---------------------------------------------------------------------------

STATUS_OK = PluginScanStatus.OK.value
STATUS_MISSING = PluginScanStatus.MISSING.value
STATUS_UNSUPPORTED = PluginScanStatus.UNSUPPORTED.value
STATUS_FAILED = PluginScanStatus.FAILED.value
STATUS_PARTIAL = PluginScanStatus.PARTIAL.value

# Priority: MISSING > UNSUPPORTED > FAILED > PARTIAL > OK
_STATUS_PRIORITY: dict[str, int] = {
    STATUS_MISSING: 4,
    STATUS_UNSUPPORTED: 3,
    STATUS_FAILED: 2,
    STATUS_PARTIAL: 1,
    STATUS_OK: 0,
}


def highest_status(statuses: Iterable[str]) -> str:
    """Return the highest-priority status from ``statuses``; empty -> OK."""
    best = STATUS_OK
    best_p = _STATUS_PRIORITY[STATUS_OK]
    for s in statuses:
        p = _STATUS_PRIORITY.get(s, 0)
        if p > best_p:
            best = s
            best_p = p
    return best


# ---------------------------------------------------------------------------
# packaging availability probe (module-level, once)
# ---------------------------------------------------------------------------

try:
    from packaging.requirements import Requirement as _Requirement
    from packaging.version import InvalidVersion as _InvalidVersion
    from packaging.version import Version as _Version

    _PACKAGING_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only when packaging missing
    _PACKAGING_AVAILABLE = False
    _Requirement = None  # type: ignore[assignment]
    _Version = None  # type: ignore[assignment]
    _InvalidVersion = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# pip dependency check
# ---------------------------------------------------------------------------

# pip status values (distinct from overall PluginScanStatus)
PIP_OK = "ok"
PIP_MISSING = "missing"
PIP_INCOMPATIBLE = "incompatible"
PIP_SKIPPED = "skipped"  # marker false in current environment


@dataclass(frozen=True)
class PipCheckResult:
    """Result of checking one ``pip_dependencies`` spec.

    ``status`` is one of ``PIP_OK`` / ``PIP_MISSING`` / ``PIP_INCOMPATIBLE``
    / ``PIP_SKIPPED``. ``diagnostic`` is a stable safe message (no
    traceback); empty when there is nothing to report.
    """

    spec: str
    name: str
    status: str
    installed_version: str | None
    diagnostic: str


def check_pip_dependency(spec: str) -> PipCheckResult:
    """Check a single PEP 508 requirement spec against installed distributions.

    Uses ``packaging.requirements.Requirement`` to parse the spec and resolve
    the distribution name (NOT the import name), so ``Pillow>=10`` checks the
    ``Pillow`` distribution even though it imports as ``PIL``. When
    ``packaging`` is unavailable, falls back to distribution-existence only
    and records a ``dependency_version_check_unavailable`` warning on the
    caller-side status (this function records status ``PIP_MISSING`` when the
    distribution is absent, ``PIP_OK`` when present but unverifiable).

    NEVER installs packages. Marker-evaluated-false requirements are skipped
    (treated as satisfied in the current environment).
    """
    spec = (spec or "").strip()
    if not spec:
        return PipCheckResult(
            spec=spec, name="", status=PIP_OK, installed_version=None,
            diagnostic="",
        )

    if not _PACKAGING_AVAILABLE:
        # Fallback: distribution existence only. We cannot verify version
        # ranges. The distribution name is best-effort: take the part before
        # the first comparator/extras/marker, stripped and normalized.
        name = _fallback_dist_name(spec)
        try:
            importlib.metadata.version(name)
            present = True
        except importlib.metadata.PackageNotFoundError:
            present = False
        except Exception:
            present = False
        if present:
            return PipCheckResult(
                spec=spec, name=name, status=PIP_OK, installed_version=None,
                diagnostic="dependency_version_check_unavailable",
            )
        return PipCheckResult(
            spec=spec, name=name, status=PIP_MISSING, installed_version=None,
            diagnostic=f"missing pip dependency: {name}; run: pip install '{spec}'",
        )

    # packaging available: full PEP 508 handling
    try:
        req = _Requirement(spec)  # type: ignore[misc]
    except Exception as exc:
        return PipCheckResult(
            spec=spec, name=spec, status=PIP_MISSING, installed_version=None,
            diagnostic=f"invalid pip requirement: {spec}: {type(exc).__name__}",
        )
    name = req.name
    # Marker false in current environment -> skip (treat as satisfied).
    if req.marker is not None and not req.marker.evaluate():
        return PipCheckResult(
            spec=spec, name=name, status=PIP_SKIPPED, installed_version=None,
            diagnostic="",
        )
    try:
        installed = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return PipCheckResult(
            spec=spec, name=name, status=PIP_MISSING, installed_version=None,
            diagnostic=f"missing pip dependency: {name}; run: pip install '{spec}'",
        )
    except Exception as exc:
        return PipCheckResult(
            spec=spec, name=name, status=PIP_MISSING, installed_version=None,
            diagnostic=f"pip dependency check error: {name}: {type(exc).__name__}",
        )
    # Version specifier check
    if req.specifier:
        try:
            installed_v = _Version(installed)  # type: ignore[misc]
        except _InvalidVersion:  # type: ignore[misc]
            # Installed version unparseable; cannot confirm compatibility.
            return PipCheckResult(
                spec=spec, name=name, status=PIP_INCOMPATIBLE,
                installed_version=installed,
                diagnostic=(
                    f"incompatible pip dependency: {name} {installed}; "
                    f"requires '{spec}'"
                ),
            )
        if not req.specifier.contains(installed_v, prereleases=True):
            return PipCheckResult(
                spec=spec, name=name, status=PIP_INCOMPATIBLE,
                installed_version=installed,
                diagnostic=(
                    f"incompatible pip dependency: {name} {installed}; "
                    f"requires '{spec}'"
                ),
            )
    return PipCheckResult(
        spec=spec, name=name, status=PIP_OK, installed_version=installed,
        diagnostic="",
    )


def _fallback_dist_name(spec: str) -> str:
    """Best-effort distribution name when ``packaging`` is unavailable.

    Strips extras (``[...]``), specifier (``>=...``), and marker
    (``; ...``) parts, returning the trimmed leading name. This is only
    used for the existence-check fallback.
    """
    s = spec
    for sep in (";", "[", ">", "<", "=", "!", "~", " "):
        idx = s.find(sep)
        if idx != -1:
            s = s[:idx]
    return s.strip() or spec


# ---------------------------------------------------------------------------
# Stable Kahn topological sort + cycle detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TopoResult:
    """Result of stable Kahn topological sort.

    ``order`` lists enabled, acyclic keys in dependency-first order.
    ``cycles`` is a list of cycles, each reported as a sorted list of member
    keys. ``cycle_members`` is the union of all cycle member keys.
    ``blocked_keys`` are nodes that could not be ordered because they depend
    (directly or transitively) on a cycle, but are NOT cycle members
    themselves. The admission layer should treat them as unavailable (PARTIAL)
    so their own dependents see ``required plugin unavailable``.
    """

    order: list[str]
    cycles: list[list[str]]
    cycle_members: frozenset[str]
    blocked_keys: frozenset[str]


def topological_order(
    enabled_keys: set[str],
    requires_plugins_by_key: dict[str, list[str]],
    discovery_index: dict[str, int],
) -> TopoResult:
    """Stable Kahn topological sort over ``enabled_keys``.

    Only ``enabled_keys`` participate as nodes. Edges are drawn from a
    dependency to its dependent (``dep -> dependent``), so a node with no
    unresolved dependencies has in-degree 0 and is emitted first. Edges to
    non-enabled or non-participating keys are ignored (those are handled by
    the admission layer, not the graph).

    Same-layer ties are broken by ``(discovery_index, key)`` for stable,
    reproducible ordering. Cycle members are excluded from ``order`` and
    reported in ``cycles`` (each cycle as a sorted member list; cycles
    themselves sorted by their first member for determinism).
    """
    nodes = set(enabled_keys)

    # Build adjacency: dep -> [dependents], and in-degree per node.
    # Only edges between enabled nodes count toward the graph structure;
    # deps outside the enabled set are admission concerns, not graph edges.
    adjacency: dict[str, list[str]] = {n: [] for n in nodes}
    indegree: dict[str, int] = {n: 0 for n in nodes}
    for dependent, deps in requires_plugins_by_key.items():
        if dependent not in nodes:
            continue
        for dep in deps:
            if dep in nodes:
                adjacency[dep].append(dependent)
                indegree[dependent] += 1

    # Kahn with stable same-layer ordering by (discovery_index, key).
    def _sort_key(key: str) -> tuple[int, str]:
        return (discovery_index.get(key, len(discovery_index)), key)

    order: list[str] = []
    remaining = set(nodes)
    # Track a node's remaining in-degree (mutable copy).
    indeg = dict(indegree)

    while remaining:
        # Current layer: zero in-degree among remaining.
        layer = sorted(
            (k for k in remaining if indeg[k] == 0),
            key=_sort_key,
        )
        if not layer:
            # All remaining nodes are in cycles. Extract cycles.
            break
        for k in layer:
            order.append(k)
            remaining.discard(k)
            for dependent in adjacency[k]:
                if dependent in remaining:
                    indeg[dependent] -= 1

    cycles: list[list[str]] = []
    if remaining:
        cycles = _extract_cycles(remaining, adjacency)

    cycle_members: set[str] = set()
    for c in cycles:
        cycle_members.update(c)

    # Blocked keys: remaining nodes that are NOT cycle members. These depend
    # on a cycle (directly or transitively) but are not part of any cycle.
    blocked_keys = remaining - cycle_members

    return TopoResult(
        order=order,
        cycles=cycles,
        cycle_members=frozenset(cycle_members),
        blocked_keys=frozenset(blocked_keys),
    )


def _extract_cycles(
    remaining: set[str],
    adjacency: dict[str, list[str]],
) -> list[list[str]]:
    """Extract true cycle member sets from the remaining (blocked) subgraph.

    Returns a list of cycles, each as a sorted list of member keys. Cycles
    are sorted by their first member for determinism.

    Only TRUE cycle members are included:
    - Multi-node SCCs (size > 1): all members participate in a cycle.
    - Single-node SCC with a self-loop (node depends on itself).

    Nodes that merely DEPEND ON a cycle (single-node SCC without self-loop)
    are NOT cycle members. They remain blocked (Kahn could not emit them)
    but are excluded from ``cycle_members`` so the admission layer
    classifies them as PARTIAL ``required plugin unavailable: {dep_key}``
    rather than FAILED ``circular plugin dependency``.
    """
    sccs = _tarjan_scc(remaining, adjacency)
    cycles: list[list[str]] = []
    for scc in sccs:
        if len(scc) > 1:
            # Multi-node SCC: all members are in a cycle.
            cycles.append(sorted(scc))
        elif len(scc) == 1:
            # Single node: only a cycle member if it has a self-loop
            # (i.e., the node depends on itself). Otherwise it merely
            # depends on a cycle and should be PARTIAL, not FAILED.
            node = scc[0]
            if node in adjacency.get(node, []):
                cycles.append(sorted(scc))
    cycles.sort(key=lambda c: (c[0] if c else ""))
    return cycles


def _tarjan_scc(
    nodes: set[str],
    adjacency: dict[str, list[str]],
) -> list[list[str]]:
    """Tarjan's SCC algorithm restricted to ``nodes``.

    Only edges whose both endpoints are in ``nodes`` are considered.
    Returns a list of SCCs (each a list of node keys).

    Uses recursive DFS; practical plugin counts (<100 per source, max 200
    total) are well within Python's default recursion limit of 1000, so no
    recursion-limit adjustment is needed.
    """
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    result: list[list[str]] = []

    # Restrict adjacency to nodes.
    adj: dict[str, list[str]] = {}
    for n in nodes:
        adj[n] = [d for d in adjacency.get(n, []) if d in nodes]

    def strongconnect(v: str) -> None:
        indices[v] = index_counter[0]
        lowlinks[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj.get(v, []):
            if w not in indices:
                strongconnect(w)
                lowlinks[v] = min(lowlinks[v], lowlinks[w])
            elif w in on_stack:
                lowlinks[v] = min(lowlinks[v], indices[w])
        if lowlinks[v] == indices[v]:
            scc: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            result.append(scc)

    for v in sorted(nodes):
        if v not in indices:
            strongconnect(v)
    return result


# ---------------------------------------------------------------------------
# Dependency availability classification
# ---------------------------------------------------------------------------

# Reasons a required plugin is unavailable.
DEP_OK = "ok"
DEP_MISSING = "missing"  # not discovered
DEP_DISABLED = "disabled"  # discovered but not effectively enabled
DEP_UNSUPPORTED = "unsupported"  # kind != standalone
DEP_LOAD_FAILED = "load_failed"  # load/register raised
DEP_CYCLE = "cycle"  # cycle member
DEP_UNAVAILABLE = "unavailable"  # transitive failure (PARTIAL/FAILED)


@dataclass(frozen=True)
class DepAvailability:
    """Availability of a single required plugin for a dependent."""

    key: str
    available: bool
    reason: str
    diagnostic: str  # stable safe message; empty when available


def classify_dep(
    dep_key: str,
    *,
    discovered_keys: set[str],
    enabled_keys: set[str],
    unsupported_keys: set[str],
    cycle_members: set[str],
    load_failed_keys: set[str],
    unavailable_keys: set[str],
) -> DepAvailability:
    """Classify why a required plugin is (un)available for its dependent.

    Returns a ``DepAvailability`` with a stable ``diagnostic``:
    - missing/disabled/unsupported/load_failed ->
      ``missing required plugin: {key}``
    - cycle/unavailable (transitive) -> ``required plugin unavailable: {key}``
    - ok -> empty diagnostic
    """
    if dep_key not in discovered_keys:
        return DepAvailability(
            dep_key, available=False, reason=DEP_MISSING,
            diagnostic=f"missing required plugin: {dep_key}",
        )
    if dep_key not in enabled_keys:
        return DepAvailability(
            dep_key, available=False, reason=DEP_DISABLED,
            diagnostic=f"missing required plugin: {dep_key}",
        )
    if dep_key in unsupported_keys:
        return DepAvailability(
            dep_key, available=False, reason=DEP_UNSUPPORTED,
            diagnostic=f"missing required plugin: {dep_key}",
        )
    if dep_key in load_failed_keys:
        return DepAvailability(
            dep_key, available=False, reason=DEP_LOAD_FAILED,
            diagnostic=f"missing required plugin: {dep_key}",
        )
    if dep_key in cycle_members:
        return DepAvailability(
            dep_key, available=False, reason=DEP_CYCLE,
            diagnostic=f"required plugin unavailable: {dep_key}",
        )
    if dep_key in unavailable_keys:
        return DepAvailability(
            dep_key, available=False, reason=DEP_UNAVAILABLE,
            diagnostic=f"required plugin unavailable: {dep_key}",
        )
    return DepAvailability(dep_key, available=True, reason=DEP_OK, diagnostic="")


# ---------------------------------------------------------------------------
# dependency_status + overall status construction
# ---------------------------------------------------------------------------


def normalize_external(
    external: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Project external_dependencies to a safe display-only shape.

    Only ``name`` / ``install`` / ``check`` are carried. Never executes or
    probes install/check; this is display-only normalization.
    """
    result: list[dict[str, str]] = []
    for item in external:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        entry: dict[str, str] = {"name": name.strip()}
        for opt_key in ("install", "check"):
            value = item.get(opt_key)
            if isinstance(value, str) and value:
                entry[opt_key] = value
        result.append(entry)
    return result


def build_dependency_status(
    *,
    pip_results: list[PipCheckResult],
    dep_availability: list[DepAvailability],
    external: list[dict[str, str]],
    warnings: list[str],
) -> dict[str, Any]:
    """Build the ``dependency_status`` dict stored in ``Plugin.capabilities``.

    Structure (at least ``pip`` / ``requires_plugins`` / ``external`` /
    ``warnings``):

    ::

        {
            "pip": [
                {"spec": "...", "name": "...", "status": "ok|missing|...",
                 "installed_version": "...|null", "diagnostic": "..."},
                ...
            ],
            "requires_plugins": [
                {"key": "...", "available": bool, "reason": "...",
                 "diagnostic": "..."},
                ...
            ],
            "external": [{"name": "...", "install": "...", "check": "..."}, ...],
            "warnings": ["...", ...],
        }
    """
    return {
        "pip": [
            {
                "spec": r.spec,
                "name": r.name,
                "status": r.status,
                "installed_version": r.installed_version,
                "diagnostic": r.diagnostic,
            }
            for r in pip_results
        ],
        "requires_plugins": [
            {
                "key": d.key,
                "available": d.available,
                "reason": d.reason,
                "diagnostic": d.diagnostic,
            }
            for d in dep_availability
        ],
        "external": normalize_external(external),
        "warnings": list(warnings),
    }


def compute_overall_status(
    *,
    manifest_ok: bool,
    is_unsupported: bool,
    in_cycle: bool,
    cycle_error: str,
    dep_availability: list[DepAvailability],
    pip_results: list[PipCheckResult],
    load_error: str | None,
    packaging_warnings: list[str],
) -> tuple[str, str]:
    """Compute a plugin's overall ``last_scan_status`` and ``last_scan_error``.

    Priority: MISSING > UNSUPPORTED > FAILED > PARTIAL > OK. ``last_scan_error``
    is a stable safe code/summary (no traceback). When multiple issues exist,
    the highest-priority status wins and its diagnostic is reported (cycle
    error takes precedence within FAILED).
    """
    if not manifest_ok:
        return STATUS_FAILED, "discovery_failed"
    if is_unsupported:
        return STATUS_UNSUPPORTED, "unsupported plugin kind"
    if in_cycle:
        return STATUS_FAILED, cycle_error
    if load_error:
        return STATUS_FAILED, load_error

    # Gather PARTIAL-level issues: dep unavailability + pip problems +
    # packaging-unavailable warnings.
    partial_errors: list[str] = []
    for dep in dep_availability:
        if not dep.available and dep.diagnostic:
            partial_errors.append(dep.diagnostic)
    for pip in pip_results:
        if pip.status == PIP_MISSING or pip.status == PIP_INCOMPATIBLE:
            if pip.diagnostic:
                partial_errors.append(pip.diagnostic)
        elif pip.status == PIP_OK and pip.diagnostic:
            # packaging-unavailable fallback recorded a warning diagnostic.
            packaging_warnings.append(pip.diagnostic)

    if partial_errors:
        # Report the first partial diagnostic as the summary; the full list
        # is available in dependency_status.
        return STATUS_PARTIAL, partial_errors[0]
    return STATUS_OK, ""


def cycle_error_message(members: list[str]) -> str:
    """Build the stable cycle error message for a set of member keys."""
    return "circular plugin dependency: " + ", ".join(sorted(members))


__all__ = [
    "DEP_CYCLE",
    "DEP_DISABLED",
    "DEP_LOAD_FAILED",
    "DEP_MISSING",
    "DEP_OK",
    "DEP_UNAVAILABLE",
    "DEP_UNSUPPORTED",
    "DepAvailability",
    "PipCheckResult",
    "PIP_INCOMPATIBLE",
    "PIP_MISSING",
    "PIP_OK",
    "PIP_SKIPPED",
    "STATUS_FAILED",
    "STATUS_MISSING",
    "STATUS_OK",
    "STATUS_PARTIAL",
    "STATUS_UNSUPPORTED",
    "TopoResult",
    "build_dependency_status",
    "check_pip_dependency",
    "classify_dep",
    "compute_overall_status",
    "cycle_error_message",
    "highest_status",
    "normalize_external",
    "topological_order",
]
