"""Ordering tests for the priority + sortOrder sort used to drain a cycle.

The function under test is pure (takes a list, returns a list), so these
tests construct stub Linear issue dicts directly rather than mocking the
GraphQL transport. The fetch wrapper ``pending_issues`` is a thin shim over
``_sort_pending_issues`` and is exercised end-to-end separately.
"""
from drain_cycle.linear import _sort_pending_issues


def _issue(identifier: str, priority: int, sort_order: float) -> dict:
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": f"Title {identifier}",
        "description": "",
        "priority": priority,
        "sortOrder": sort_order,
        "state": {"type": "unstarted", "name": "Todo"},
    }


def test_urgent_sorts_before_no_priority():
    issues = [_issue("NoPrio", 0, 0.0), _issue("Urgent", 1, 1.0)]
    assert [i["identifier"] for i in _sort_pending_issues(issues)] == ["Urgent", "NoPrio"]


def test_priority_ties_broken_by_sortOrder_ascending():
    issues = [
        _issue("A-late", 2, 3.0),
        _issue("B-early", 2, 1.0),
        _issue("C-mid", 2, 2.0),
    ]
    assert [i["identifier"] for i in _sort_pending_issues(issues)] == [
        "B-early",
        "C-mid",
        "A-late",
    ]


def test_no_priority_sorts_last_not_first():
    issues = [
        _issue("NoPrio", 0, 100.0),
        _issue("Low", 4, 100.0),
        _issue("Medium", 3, 100.0),
        _issue("High", 2, 100.0),
        _issue("Urgent", 1, 100.0),
    ]
    assert [i["identifier"] for i in _sort_pending_issues(issues)] == [
        "Urgent",
        "High",
        "Medium",
        "Low",
        "NoPrio",
    ]


def test_mixed_priorities_and_sortOrder():
    issues = [
        _issue("low-late",   4, 2.0),
        _issue("urgent",     1, 5.0),
        _issue("noprio",     0, 0.5),
        _issue("low-early",  4, 1.0),
        _issue("medium",     3, 3.0),
        _issue("high-late",  2, 4.0),
        _issue("high-early", 2, 1.0),
    ]
    assert [i["identifier"] for i in _sort_pending_issues(issues)] == [
        "urgent",
        "high-early",
        "high-late",
        "medium",
        "low-early",
        "low-late",
        "noprio",
    ]
