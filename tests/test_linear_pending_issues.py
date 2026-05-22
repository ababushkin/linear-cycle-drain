"""Tests for ``linear.pending_issues`` projection (ABA-232).

The wire shape from Linear is ``labels { nodes { name } }`` — a nested
GraphQL connection. ``pending_issues`` flattens it to a plain
``list[str]`` so downstream code (``repos.Repos.resolve``) doesn't need
to know the wire shape. These tests pin both halves: the query includes
the ``labels { nodes { name } }`` projection, and the returned dict has
``labels`` as a plain list (empty list when an issue has no labels —
never a missing key).
"""
from __future__ import annotations

import pytest

from drain_cycle import linear


def _node(identifier: str, label_names: list[str]) -> dict:
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": f"Title for {identifier}",
        "description": "",
        "priority": 1,
        "sortOrder": 1.0,
        "state": {"type": "unstarted", "name": "Todo"},
        "labels": {"nodes": [{"name": n} for n in label_names]},
    }


def test_pending_issues_projection_requests_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_post(query: str, variables: dict | None = None) -> dict:
        captured.append(query)
        return {"issues": {"nodes": []}}

    monkeypatch.setattr(linear, "_post", fake_post)
    linear.pending_issues("cycle-id")
    (query,) = captured
    assert "labels" in query
    assert "nodes" in query
    assert "name" in query


def test_pending_issues_flattens_labels_to_list_of_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(query: str, variables: dict | None = None) -> dict:
        return {
            "issues": {
                "nodes": [_node("ABA-A", ["repo:drain-cycle", "bug"])]
            }
        }

    monkeypatch.setattr(linear, "_post", fake_post)
    issues = linear.pending_issues("cycle-id")
    assert len(issues) == 1
    # Plain list[str], not {"nodes": [...]}: callers must not need to
    # know the GraphQL wire shape.
    assert issues[0]["labels"] == ["repo:drain-cycle", "bug"]


def test_pending_issues_returns_empty_list_when_issue_has_no_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unlabelled issue must come back with ``labels: []``, not a
    missing key — ``repos.Repos.resolve`` uses ``issue.get("labels", [])``
    but the contract is that the key is always present after this call."""

    def fake_post(query: str, variables: dict | None = None) -> dict:
        return {"issues": {"nodes": [_node("ABA-A", [])]}}

    monkeypatch.setattr(linear, "_post", fake_post)
    issues = linear.pending_issues("cycle-id")
    assert issues[0]["labels"] == []


def test_pending_issues_preserves_other_fields_after_flattening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: flattening the labels block must not drop or
    reshape any of the other top-level issue fields."""

    def fake_post(query: str, variables: dict | None = None) -> dict:
        return {"issues": {"nodes": [_node("ABA-A", ["repo:x"])]}}

    monkeypatch.setattr(linear, "_post", fake_post)
    (issue,) = linear.pending_issues("cycle-id")
    assert issue["id"] == "id-ABA-A"
    assert issue["identifier"] == "ABA-A"
    assert issue["title"] == "Title for ABA-A"
    assert issue["state"] == {"type": "unstarted", "name": "Todo"}
    assert issue["priority"] == 1
    assert issue["sortOrder"] == 1.0
