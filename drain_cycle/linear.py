"""Minimal Linear GraphQL client.

Reads ``LINEAR_API_KEY`` from the environment. The CLI entrypoint
(``cli.main``) loads ``.env`` from the drain-cycle repo root before this
module is exercised; a shell-exported value still takes precedence.
Single-team scope: hardcoded to the ``Personal`` team per README §1.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

_ENDPOINT = "https://api.linear.app/graphql"
_TEAM_NAME = "Personal"
_PENDING_STATE_TYPES = ["backlog", "unstarted"]
_RESOLVED_STATE_TYPES = {"completed", "canceled"}


@dataclass(frozen=True)
class ExecutionPlan:
    """Result of ``_plan``: runnable issues in topo order + deferred issues."""
    order: list  # list[dict] — issues to spawn, in execution order
    deferred: list  # list[dict] — each {"issue", "blocker_identifier", "blocker_state_type"}


class DependencyCycleError(RuntimeError):
    """Raised when the blocks graph among pending issues contains a cycle."""
    def __init__(self, identifiers: list[str]) -> None:
        self.identifiers = list(identifiers)
        super().__init__(f"Dependency cycle among: {', '.join(identifiers)}")


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


def _plan(issues: list[dict[str, Any]]) -> ExecutionPlan:
    """Topological sort over ``issues`` respecting blocks/blocked-by.

    Returns an ``ExecutionPlan`` with ``order`` (runnable, topo-sorted by
    ``(sortOrder, id)`` tiebreak) and ``deferred`` (issues skipped because
    an external unresolved blocker exists, or because a blocking issue in
    the drain was itself deferred — cascade).

    Raises ``DependencyCycleError`` when the intra-set blocks graph contains
    a cycle (self-loops included).
    """
    P: set[str] = {i["id"] for i in issues}
    by_id: dict[str, dict[str, Any]] = {i["id"]: i for i in issues}

    # Build de-duped intra-P edges: blocker_id → set of blocked_ids
    children: dict[str, set[str]] = {i["id"]: set() for i in issues}
    # Per-issue unique set of intra-P blocker ids (for cascade check + in-degree)
    intra_blockers: dict[str, set[str]] = {i["id"]: set() for i in issues}
    indegree: dict[str, int] = {i["id"]: 0 for i in issues}

    for issue in issues:
        for blocker in issue.get("blockers", []):
            bid = blocker["id"]
            if bid in P:
                if bid not in intra_blockers[issue["id"]]:
                    intra_blockers[issue["id"]].add(bid)
                    indegree[issue["id"]] += 1
                    children[bid].add(issue["id"])

    # Kahn's algorithm: ready = in-degree-0 issues, sorted by (sortOrder, id)
    ready: list[dict[str, Any]] = sorted(
        [i for i in issues if indegree[i["id"]] == 0],
        key=lambda i: (i["sortOrder"], i["id"]),
    )

    order: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    deferred_ids: set[str] = set()

    while ready:
        issue = ready.pop(0)
        iid = issue["id"]

        # Cascade: any intra-P blocker already deferred → defer this too
        cascade_blocker = next(
            (by_id[bid] for bid in intra_blockers[iid] if bid in deferred_ids),
            None,
        )
        if cascade_blocker is not None:
            deferred.append({
                "issue": issue,
                "blocker_identifier": cascade_blocker["identifier"],
                "blocker_state_type": cascade_blocker["state"]["type"],
            })
            deferred_ids.add(iid)
        else:
            # External unresolved blocker → defer
            ext_blocker = next(
                (
                    b for b in issue.get("blockers", [])
                    if b["id"] not in P and b["state_type"] not in _RESOLVED_STATE_TYPES
                ),
                None,
            )
            if ext_blocker is not None:
                deferred.append({
                    "issue": issue,
                    "blocker_identifier": ext_blocker["identifier"],
                    "blocker_state_type": ext_blocker["state_type"],
                })
                deferred_ids.add(iid)
            else:
                order.append(issue)

        # Decrement children regardless (deferred nodes still unblock their children
        # in the graph so cycle detection remains accurate)
        for child_id in children[iid]:
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                child = by_id[child_id]
                # Insert maintaining (sortOrder, id) order
                idx = 0
                key = (child["sortOrder"], child["id"])
                while idx < len(ready) and (ready[idx]["sortOrder"], ready[idx]["id"]) <= key:
                    idx += 1
                ready.insert(idx, child)

    cycle_nodes = [i for i in issues if indegree[i["id"]] > 0]
    if cycle_nodes:
        raise DependencyCycleError([i["identifier"] for i in cycle_nodes])

    return ExecutionPlan(order=order, deferred=deferred)


def pending_issues(cycle_id: str) -> list[dict[str, Any]]:
    """Return every Todo/Backlog issue in the cycle, sorted for execution.

    No pagination: personal cycles fit comfortably in one page. If a cycle
    ever exceeds 100 pending issues, that's a planning problem, not a tool
    problem (see ``PRODUCT_RULES`` Rule A5 — focus is the multiplier).

    ``labels`` is flattened from the GraphQL ``{nodes: [{name}]}`` shape to
    a plain ``list[str]`` so downstream code (``repos.Repos.resolve``)
    doesn't need to know the wire shape.
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
              labels { nodes { name } }
            }
          }
        }
        """,
        {"cycleId": cycle_id, "stateTypes": _PENDING_STATE_TYPES},
    )
    issues = data["issues"]["nodes"]
    for issue in issues:
        issue["labels"] = [node["name"] for node in issue["labels"]["nodes"]]
    return _sort_pending_issues(issues)


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
