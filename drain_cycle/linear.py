"""Minimal Linear GraphQL client.

Reads ``LINEAR_API_KEY`` from the environment. Single-team scope: hardcoded
to the ``Personal`` team per README §1 and the US-A spec.

Walking-skeleton scope (Task 1 / ABA-198): resolve current cycle, fetch the
first Todo/Backlog issue, re-fetch issue state by id. Task 2 / ABA-199 adds
``pending_issues`` which returns the full sorted list; the orchestrator
switches to it in Task 3 / ABA-200.
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


def first_pending_issue(cycle_id: str) -> dict[str, Any] | None:
    """Return the first Todo/Backlog issue in the cycle, or None if drained.

    Walking-skeleton ordering only — relies on Linear's default ordering.
    Priority + sortOrder sorting is Task 2 / ABA-199.
    """
    data = _post(
        """
        query CyclePending($cycleId: ID!, $stateTypes: [String!]!) {
          issues(
            filter: {
              cycle: { id: { eq: $cycleId } }
              state: { type: { in: $stateTypes } }
            }
            first: 1
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
    nodes = data["issues"]["nodes"]
    return nodes[0] if nodes else None


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
