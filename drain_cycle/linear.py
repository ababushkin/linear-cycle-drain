"""Minimal Linear GraphQL client.

Reads ``LINEAR_API_KEY`` from the environment. The CLI entrypoint
(``cli.main``) loads ``.env`` from the drain-cycle repo root before this
module is exercised; a shell-exported value still takes precedence.
Single-team scope: hardcoded to the ``Personal`` team per README §1 and
the US-A spec.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

_ENDPOINT = "https://api.linear.app/graphql"
_TEAM_NAME = "Personal"
_PENDING_STATE_TYPES = ["backlog", "unstarted"]


def _post(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        raise RuntimeError("LINEAR_API_KEY is not set")
    resp = httpx.post(
        _ENDPOINT,
        headers={"Authorization": key, "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=30.0,
    )
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        raise RuntimeError(f"Linear GraphQL errors: {body['errors']}")
    return body["data"]


def current_cycle_id() -> str:
    """Return the active cycle id for the configured team."""
    data = _post(
        """
        query CurrentCycle($name: String!) {
          teams(filter: { name: { eq: $name } }) {
            nodes {
              id
              activeCycle { id }
            }
          }
        }
        """,
        {"name": _TEAM_NAME},
    )
    nodes = data["teams"]["nodes"]
    if not nodes:
        raise RuntimeError(f"Linear team {_TEAM_NAME!r} not found")
    cycle = nodes[0].get("activeCycle")
    if not cycle:
        raise RuntimeError(f"Linear team {_TEAM_NAME!r} has no active cycle")
    return cycle["id"]


def _sort_pending_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by Linear priority (Urgent→Low→No-priority), tiebroken by ``sortOrder``.

    Linear encodes priority as 0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low.
    The quirk worth pinning down in a test: ``0`` must sort *after* ``4``, not
    before ``1``. We remap 0→5 in the sort key and leave 1..4 in place.
    """
    def key(issue: dict[str, Any]) -> tuple[int, float]:
        p = issue["priority"]
        return (p if p else 5, issue["sortOrder"])
    return sorted(issues, key=key)


def pending_issues(cycle_id: str) -> list[dict[str, Any]]:
    """Return every Todo/Backlog issue in the cycle, sorted for execution.

    No pagination: personal cycles fit comfortably in one page. If a cycle
    ever exceeds 100 pending issues, that's a planning problem, not a tool
    problem (see ``PRODUCT_RULES`` Rule A5 — focus is the multiplier).
    """
    data = _post(
        """
        query CyclePending($cycleId: ID!, $stateTypes: [String!]!) {
          issues(
            filter: {
              cycle: { id: { eq: $cycleId } }
              state: { type: { in: $stateTypes } }
            }
            first: 100
          ) {
            nodes {
              id
              identifier
              title
              description
              priority
              sortOrder
              state { type name }
            }
          }
        }
        """,
        {"cycleId": cycle_id, "stateTypes": _PENDING_STATE_TYPES},
    )
    return _sort_pending_issues(data["issues"]["nodes"])


def get_issue(issue_id: str) -> dict[str, Any]:
    """Re-fetch an issue's current state by id."""
    data = _post(
        """
        query Issue($id: String!) {
          issue(id: $id) {
            id
            identifier
            title
            state { type name }
          }
        }
        """,
        {"id": issue_id},
    )
    issue = data["issue"]
    if issue is None:
        raise RuntimeError(f"Linear issue {issue_id!r} not found")
    return issue


def set_state(issue_id: str, state_name: str) -> None:
    """Transition an issue to the named workflow state.

    Resolves the state ID by name against the configured team and then issues
    an ``issueUpdate`` mutation. No caching — at cycle scale (≤ ~15 issues)
    the extra round-trip is irrelevant and the simpler code is easier to test.
    """
    state_id = _resolve_state_id(state_name)
    data = _post(
        """
        mutation IssueSetState($id: String!, $input: IssueUpdateInput!) {
          issueUpdate(id: $id, input: $input) {
            success
          }
        }
        """,
        {"id": issue_id, "input": {"stateId": state_id}},
    )
    if not data["issueUpdate"]["success"]:
        raise RuntimeError(
            f"Linear issueUpdate failed for {issue_id!r} → {state_name!r}"
        )


def _resolve_state_id(state_name: str) -> str:
    data = _post(
        """
        query WorkflowState($team: String!, $name: String!) {
          workflowStates(
            filter: {
              team: { name: { eq: $team } }
              name: { eq: $name }
            }
            first: 1
          ) {
            nodes { id }
          }
        }
        """,
        {"team": _TEAM_NAME, "name": state_name},
    )
    nodes = data["workflowStates"]["nodes"]
    if not nodes:
        raise RuntimeError(
            f"Linear workflow state {state_name!r} not found for team {_TEAM_NAME!r}"
        )
    return nodes[0]["id"]
