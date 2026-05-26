"""Ordering tests for ``_plan`` — topological sort with deferral.

The function under test is pure (takes a list, returns an ExecutionPlan), so
these tests construct stub Linear issue dicts directly rather than mocking
the GraphQL transport. ``pending_issues`` is exercised separately.
"""
import pytest

from drain_cycle.linear import DependencyCycleError, ExecutionPlan, _plan


def _issue(identifier: str, sort_order: float, blockers=None) -> dict:
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": f"Title {identifier}",
        "description": "",
        "sortOrder": sort_order,
        "state": {"type": "unstarted", "name": "Todo"},
        "blockers": blockers if blockers is not None else [],
    }


def _blocker(identifier: str, state_type: str = "completed") -> dict:
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "state_type": state_type,
    }


def test_manual_order_no_blocks():
    """No blockers: pure sortOrder ascending."""
    issues = [
        _issue("ABA-C", 3.0),
        _issue("ABA-A", 1.0),
        _issue("ABA-B", 2.0),
    ]
    plan = _plan(issues)
    assert isinstance(plan, ExecutionPlan)
    assert [i["identifier"] for i in plan.order] == ["ABA-A", "ABA-B", "ABA-C"]
    assert plan.deferred == []


def test_blocker_before_blocked_reversed_sort_order():
    """A blocks B; B has lower sortOrder than A — A must run before B."""
    issues = [
        _issue("ABA-A", 5.0),  # blocker, high sortOrder
        _issue("ABA-B", 1.0, blockers=[_blocker("ABA-A", "unstarted")]),  # blocked, low sortOrder
    ]
    plan = _plan(issues)
    assert [i["identifier"] for i in plan.order] == ["ABA-A", "ABA-B"]
    assert plan.deferred == []


def test_sort_order_id_tiebreak():
    """Two ready issues with same sortOrder — ``(sortOrder, id)`` is the tiebreak."""
    issues = [
        _issue("ABA-Z", 1.0),
        _issue("ABA-A", 1.0),
    ]
    plan = _plan(issues)
    # id-ABA-A < id-ABA-Z lexicographically
    assert [i["identifier"] for i in plan.order] == ["ABA-A", "ABA-Z"]
    assert plan.deferred == []


def test_external_unresolved_blocker_deferred():
    """Issue blocked by an external unresolved issue → deferred, not in order."""
    issues = [
        _issue("ABA-A", 1.0, blockers=[_blocker("EXT-1", "started")]),
    ]
    plan = _plan(issues)
    assert plan.order == []
    assert len(plan.deferred) == 1
    entry = plan.deferred[0]
    assert entry["issue"]["identifier"] == "ABA-A"
    assert entry["blocker_identifier"] == "EXT-1"
    assert entry["blocker_state_type"] == "started"


def test_external_done_blocker_ignored():
    """Issue blocked by external Done/Cancelled issue → runs normally."""
    issues = [
        _issue("ABA-A", 1.0, blockers=[_blocker("EXT-DONE", "completed")]),
        _issue("ABA-B", 2.0, blockers=[_blocker("EXT-CANCELLED", "canceled")]),
    ]
    plan = _plan(issues)
    assert [i["identifier"] for i in plan.order] == ["ABA-A", "ABA-B"]
    assert plan.deferred == []


def test_3_deep_cascade():
    """External unresolved → A deferred; A blocks B → B deferred; B blocks C → C deferred."""
    issues = [
        _issue("ABA-A", 1.0, blockers=[_blocker("EXT-1", "started")]),
        _issue("ABA-B", 2.0, blockers=[_blocker("ABA-A", "unstarted")]),
        _issue("ABA-C", 3.0, blockers=[_blocker("ABA-B", "unstarted")]),
    ]
    plan = _plan(issues)
    assert plan.order == []
    assert len(plan.deferred) == 3
    deferred_ids = {d["issue"]["identifier"] for d in plan.deferred}
    assert deferred_ids == {"ABA-A", "ABA-B", "ABA-C"}


def test_2_node_cycle_raises():
    """A blocks B, B blocks A → DependencyCycleError."""
    issues = [
        _issue("ABA-A", 1.0, blockers=[_blocker("ABA-B", "unstarted")]),
        _issue("ABA-B", 2.0, blockers=[_blocker("ABA-A", "unstarted")]),
    ]
    with pytest.raises(DependencyCycleError) as exc_info:
        _plan(issues)
    involved = exc_info.value.identifiers
    assert "ABA-A" in involved and "ABA-B" in involved


def test_self_loop_raises():
    """A blocks itself → DependencyCycleError."""
    issues = [
        _issue("ABA-A", 1.0, blockers=[_blocker("ABA-A", "unstarted")]),
    ]
    with pytest.raises(DependencyCycleError) as exc_info:
        _plan(issues)
    assert "ABA-A" in exc_info.value.identifiers


def test_in_progress_in_cycle_deferred():
    """Blocker is In Progress in the cycle (external, state_type 'started') → deferred."""
    issues = [
        _issue("ABA-A", 1.0, blockers=[_blocker("ABA-WIP", "started")]),
    ]
    plan = _plan(issues)
    assert len(plan.deferred) == 1
    entry = plan.deferred[0]
    assert entry["issue"]["identifier"] == "ABA-A"
    assert entry["blocker_state_type"] == "started"


def test_duplicate_edges_dont_crash():
    """Duplicate blocker entries for the same pair → no in-degree inflation."""
    issues = [
        _issue("ABA-A", 1.0),
        _issue("ABA-B", 2.0, blockers=[
            _blocker("ABA-A", "unstarted"),
            _blocker("ABA-A", "unstarted"),  # duplicate
        ]),
    ]
    plan = _plan(issues)
    assert [i["identifier"] for i in plan.order] == ["ABA-A", "ABA-B"]
    assert plan.deferred == []
