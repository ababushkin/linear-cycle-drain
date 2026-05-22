"""Unit tests for the ``set_state`` helper (Task 7 / ABA-209).

The helper performs two GraphQL calls: a ``workflowStates`` query to resolve
the state name to a state ID, then an ``issueUpdate`` mutation to apply it.
These tests stub ``linear._post`` rather than the HTTP transport — the layer
under test is the helper's query shape and error handling, not the wire
serialisation (covered by the integration test in Task 6 / ABA-203).
"""
from __future__ import annotations

import pytest

from drain_cycle import linear


def test_set_state_resolves_name_then_calls_issue_update(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_post(query: str, variables: dict | None = None) -> dict:
        calls.append((query, variables or {}))
        if "workflowStates" in query:
            return {"workflowStates": {"nodes": [{"id": "state-uuid-123"}]}}
        if "issueUpdate" in query:
            return {"issueUpdate": {"success": True}}
        raise AssertionError(f"unexpected query: {query!r}")

    monkeypatch.setattr(linear, "_post", fake_post)

    linear.set_state("issue-uuid-456", "In Progress")

    assert len(calls) == 2, calls
    resolve_query, resolve_vars = calls[0]
    update_query, update_vars = calls[1]
    assert "workflowStates" in resolve_query
    assert resolve_vars == {"team": linear._TEAM_NAME, "name": "In Progress"}
    assert "issueUpdate" in update_query
    assert update_vars == {
        "id": "issue-uuid-456",
        "input": {"stateId": "state-uuid-123"},
    }


def test_set_state_raises_when_state_name_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(query: str, variables: dict | None = None) -> dict:
        assert "workflowStates" in query  # mutation must not be reached
        return {"workflowStates": {"nodes": []}}

    monkeypatch.setattr(linear, "_post", fake_post)

    with pytest.raises(RuntimeError, match="workflow state 'Bogus' not found"):
        linear.set_state("issue-uuid", "Bogus")


def test_set_state_raises_when_mutation_returns_unsuccessful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(query: str, variables: dict | None = None) -> dict:
        if "workflowStates" in query:
            return {"workflowStates": {"nodes": [{"id": "state-id"}]}}
        return {"issueUpdate": {"success": False}}

    monkeypatch.setattr(linear, "_post", fake_post)

    with pytest.raises(RuntimeError, match="issueUpdate failed"):
        linear.set_state("issue-uuid", "In Progress")
