"""Prompt-template assertions.

The prompt is what the orchestrator hands to ``claude -p`` — its four
segments (title, body, execution preamble, tail line) must appear in the
documented order or the spawned agent loses the context it needs to
complete the issue and self-transition Linear. These tests pin the
ordering and the load-bearing substrings so a future refactor cannot
silently reshape the contract.
"""
from __future__ import annotations

from pathlib import Path

from drain_cycle.prompt import _TAIL, build


def _fixture_issue() -> dict:
    return {
        "id": "id-ABA-999",
        "identifier": "ABA-999",
        "title": "Fixture title — drain a trivial issue",
        "description": "Fixture body.\n\nMultiple paragraphs preserved verbatim.",
        "priority": 3,
        "sortOrder": 1.0,
        "state": {"type": "unstarted", "name": "Todo"},
    }


def _positions(text: str, *needles: str) -> list[int]:
    """Return the index of each needle, asserting each one is present."""
    found = []
    for needle in needles:
        idx = text.find(needle)
        assert idx != -1, f"missing segment: {needle!r}\n--- prompt ---\n{text}"
        found.append(idx)
    return found


def test_prompt_contains_four_segments_in_order(tmp_path: Path) -> None:
    issue = _fixture_issue()
    worktree = tmp_path / ".worktrees" / issue["identifier"]
    rendered = build(issue, worktree)

    title_idx, body_idx, preamble_idx, tail_idx = _positions(
        rendered,
        f"# {issue['title']}",
        issue["description"],
        "Execution instructions:",
        _TAIL,
    )
    assert title_idx < body_idx < preamble_idx < tail_idx


def test_preamble_names_worktree_base_branch_and_mcp_call(tmp_path: Path) -> None:
    issue = _fixture_issue()
    worktree = tmp_path / ".worktrees" / issue["identifier"]
    rendered = build(issue, worktree)

    # Each of the three preamble facts the spawned agent needs to act on.
    assert str(worktree) in rendered
    assert "Base branch: main" in rendered
    assert "mcp__claude_ai_Linear__save_issue" in rendered
    assert 'state: "Done"' in rendered
    # The Linear MCP call needs to know which issue — include the identifier.
    assert issue["identifier"] in rendered


def test_tail_line_is_the_last_non_empty_line(tmp_path: Path) -> None:
    issue = _fixture_issue()
    worktree = tmp_path / ".worktrees" / issue["identifier"]
    rendered = build(issue, worktree)

    non_empty = [line for line in rendered.splitlines() if line.strip()]
    assert non_empty[-1] == _TAIL


def test_resumed_prompt_inserts_directive_above_execution_instructions(
    tmp_path: Path,
) -> None:
    """``resumed=True`` adds a resume directive that leads the preamble
    so the agent reads it before the execution procedure, while ``_TAIL``
    stays the last non-empty line (the four-segment ordering is
    load-bearing)."""
    issue = _fixture_issue()
    worktree = tmp_path / ".worktrees" / issue["identifier"]
    rendered = build(issue, worktree, resumed=True)

    title_idx, body_idx, sep_idx, directive_idx, exec_idx, tail_idx = _positions(
        rendered,
        f"# {issue['title']}",
        issue["description"],
        "---",
        "Resuming issue",
        "Execution instructions:",
        _TAIL,
    )
    assert title_idx < body_idx < sep_idx < directive_idx < exec_idx < tail_idx

    # Directive names the issue and the two read-state commands the
    # agent should run before continuing.
    assert issue["identifier"] in rendered[directive_idx:exec_idx]
    assert "git log --oneline main..HEAD" in rendered
    assert "git status" in rendered

    # _TAIL is still the last non-empty line — the prepend must not
    # displace it from the trailing position.
    non_empty = [line for line in rendered.splitlines() if line.strip()]
    assert non_empty[-1] == _TAIL


def test_unresumed_prompt_is_byte_identical_to_no_kwarg_default(
    tmp_path: Path,
) -> None:
    """``resumed=False`` (the default) keeps the prompt unchanged: every
    existing call site renders byte-identical output, and the resume
    directive is absent."""
    issue = _fixture_issue()
    worktree = tmp_path / ".worktrees" / issue["identifier"]

    default = build(issue, worktree)
    explicit_false = build(issue, worktree, resumed=False)

    assert default == explicit_false
    assert "Resuming issue" not in default


def test_empty_description_does_not_break_rendering(tmp_path: Path) -> None:
    issue = _fixture_issue()
    issue["description"] = None  # Linear can return null descriptions
    worktree = tmp_path / ".worktrees" / issue["identifier"]
    rendered = build(issue, worktree)

    # Title and preamble must still render in order even with no body.
    title_idx, preamble_idx, tail_idx = _positions(
        rendered,
        f"# {issue['title']}",
        "Execution instructions:",
        _TAIL,
    )
    assert title_idx < preamble_idx < tail_idx
