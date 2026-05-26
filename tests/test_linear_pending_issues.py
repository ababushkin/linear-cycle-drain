"""Tests for ``linear.pending_issues`` projection.

The wire shape from Linear has nested connection fields that ``pending_issues``
flattens to plain Python types so downstream code doesn't need to know the
GraphQL wire shape.

These tests pin:
- ``labels { nodes { name } }`` → ``labels: list[str]``
- ``inverseRelations { nodes { type issue { id identifier state { type } } } }``
  → ``blockers: list[{id, identifier, state_type}]`` (type == "blocks" only;
  raw ``inverseRelations`` key removed)
- The function returns an ``ExecutionPlan``, not a raw list.
"""
from __future__ import annotations

import pytest

from drain_cycle import linear
from drain_cycle.linear import ExecutionPlan


def _label_node(entry: "str | tuple[str, str]") -> dict:
    """Build a wire-shape label node.

    Pass a bare string for an ungrouped label (``parent`` is ``None``), or a
    ``(name, group)`` tuple for a grouped label (``parent.name`` set to
    ``group``).
    """
    if isinstance(entry, tuple):
        name, group = entry
        return {"name": name, "parent": {"name": group}}
    return {"name": entry}


def _node(identifier: str, label_names: "list[str | tuple[str, str]]", inverse_relations=None) -> dict:
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": f"Title for {identifier}",
        "description": "",
        "sortOrder": 1.0,
        "state": {"type": "unstarted", "name": "Todo"},
        "labels": {"nodes": [_label_node(e) for e in label_names]},
        "inverseRelations": {"nodes": inverse_relations or []},
    }


def _inverse_rel(blocker_identifier: str, rel_type: str, state_type: str = "completed") -> dict:
    """Wire-shape entry from ``inverseRelations.nodes``."""
    return {
        "type": rel_type,
        "issue": {
            "id": f"id-{blocker_identifier}",
            "identifier": blocker_identifier,
            "state": {"type": state_type},
        },
    }


def test_pending_issues_projection_requests_inverse_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_post(query: str, variables: dict | None = None, *, operation: str = "graphql") -> dict:
        captured.append(query)
        return {"issues": {"nodes": []}}

    monkeypatch.setattr(linear, "_post", fake_post)
    linear.pending_issues("cycle-id")
    (query,) = captured
    assert "inverseRelations" in query
    # The type field is required so client-side "blocks" filtering works.
    assert "nodes { type issue {" in query


def test_pending_issues_projection_does_not_request_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_post(query: str, variables: dict | None = None, *, operation: str = "graphql") -> dict:
        captured.append(query)
        return {"issues": {"nodes": []}}

    monkeypatch.setattr(linear, "_post", fake_post)
    linear.pending_issues("cycle-id")
    (query,) = captured
    assert "priority" not in query


def test_pending_issues_flattens_labels_to_list_of_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(query: str, variables: dict | None = None, *, operation: str = "graphql") -> dict:
        return {"issues": {"nodes": [_node("ABA-A", ["repo:drain-cycle", "bug"])]}}

    monkeypatch.setattr(linear, "_post", fake_post)
    plan = linear.pending_issues("cycle-id")
    assert isinstance(plan, ExecutionPlan)
    assert len(plan.order) == 1
    assert plan.order[0]["labels"] == ["repo:drain-cycle", "bug"]


def test_pending_issues_returns_empty_list_when_issue_has_no_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(query: str, variables: dict | None = None, *, operation: str = "graphql") -> dict:
        return {"issues": {"nodes": [_node("ABA-A", [])]}}

    monkeypatch.setattr(linear, "_post", fake_post)
    plan = linear.pending_issues("cycle-id")
    assert plan.order[0]["labels"] == []


def test_pending_issues_flattens_blockers_from_inverse_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(query: str, variables: dict | None = None, *, operation: str = "graphql") -> dict:
        return {
            "issues": {
                "nodes": [
                    _node("ABA-A", [], inverse_relations=[
                        _inverse_rel("EXT-1", "blocks", "started"),
                    ])
                ]
            }
        }

    monkeypatch.setattr(linear, "_post", fake_post)
    plan = linear.pending_issues("cycle-id")
    # ABA-A has external unresolved blocker → deferred
    assert plan.order == []
    assert len(plan.deferred) == 1
    entry = plan.deferred[0]
    assert entry["issue"]["identifier"] == "ABA-A"
    assert entry["blocker_identifier"] == "EXT-1"
    assert entry["blocker_state_type"] == "started"


def test_pending_issues_ignores_non_blocks_relation_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'related' and 'duplicate' inverse relations must not produce blockers."""
    def fake_post(query: str, variables: dict | None = None, *, operation: str = "graphql") -> dict:
        return {
            "issues": {
                "nodes": [
                    _node("ABA-A", [], inverse_relations=[
                        _inverse_rel("EXT-1", "related", "started"),
                        _inverse_rel("EXT-2", "duplicate", "started"),
                    ])
                ]
            }
        }

    monkeypatch.setattr(linear, "_post", fake_post)
    plan = linear.pending_issues("cycle-id")
    # Non-blocking relation types → issue runs normally
    assert len(plan.order) == 1
    assert plan.order[0]["blockers"] == []
    assert plan.deferred == []


def test_pending_issues_drops_raw_inverse_relations_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(query: str, variables: dict | None = None, *, operation: str = "graphql") -> dict:
        return {"issues": {"nodes": [_node("ABA-A", [])]}}

    monkeypatch.setattr(linear, "_post", fake_post)
    plan = linear.pending_issues("cycle-id")
    assert "inverseRelations" not in plan.order[0]


def test_pending_issues_preserves_other_fields_after_flattening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(query: str, variables: dict | None = None, *, operation: str = "graphql") -> dict:
        return {"issues": {"nodes": [_node("ABA-A", ["repo:x"])]}}

    monkeypatch.setattr(linear, "_post", fake_post)
    (issue,) = linear.pending_issues("cycle-id").order
    assert issue["id"] == "id-ABA-A"
    assert issue["identifier"] == "ABA-A"
    assert issue["title"] == "Title for ABA-A"
    assert issue["state"] == {"type": "unstarted", "name": "Todo"}
    assert issue["sortOrder"] == 1.0


def test_pending_issues_returns_execution_plan_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(query: str, variables: dict | None = None, *, operation: str = "graphql") -> dict:
        return {"issues": {"nodes": []}}

    monkeypatch.setattr(linear, "_post", fake_post)
    plan = linear.pending_issues("cycle-id")
    assert isinstance(plan, ExecutionPlan)
    assert plan.order == []
    assert plan.deferred == []


def test_pending_issues_projection_requests_label_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_post(query: str, variables: dict | None = None, *, operation: str = "graphql") -> dict:
        captured.append(query)
        return {"issues": {"nodes": []}}

    monkeypatch.setattr(linear, "_post", fake_post)
    linear.pending_issues("cycle-id")
    (query,) = captured
    assert "parent { name }" in query


def test_pending_issues_renders_grouped_labels_with_group_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(query: str, variables: dict | None = None, *, operation: str = "graphql") -> dict:
        return {
            "issues": {
                "nodes": [
                    _node("ABA-A", [
                        ("agent-skills-workflow", "repo"),
                        ("sonnet", "model"),
                        ("1-build-parallel", "wave"),
                    ])
                ]
            }
        }

    monkeypatch.setattr(linear, "_post", fake_post)
    plan = linear.pending_issues("cycle-id")
    assert plan.order[0]["labels"] == [
        "repo:agent-skills-workflow",
        "model:sonnet",
        "wave:1-build-parallel",
    ]


def test_pending_issues_keeps_ungrouped_label_name_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flat ``repo:<name>`` labels and bare labels survive unchanged (backward compat)."""
    def fake_post(query: str, variables: dict | None = None, *, operation: str = "graphql") -> dict:
        return {
            "issues": {
                "nodes": [_node("ABA-A", ["repo:drain-cycle", "bug"])]
            }
        }

    monkeypatch.setattr(linear, "_post", fake_post)
    plan = linear.pending_issues("cycle-id")
    assert plan.order[0]["labels"] == ["repo:drain-cycle", "bug"]
