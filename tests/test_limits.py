"""Unit tests for the guardrail thresholds and their loader.

These exercise pure logic — no subprocess, no Linear. The breach *kills*
are exercised against a real process group in ``test_worker.py`` (per-issue)
and through the orchestrator in ``test_orchestrator_cycle_limit.py``
(cycle-wide). Here we pin: the defaults, the ``limits.yml`` precedence
(absent → default, ``null`` → off, number → override), the malformed-config
rejections, the ``check_cycle`` ordering, and the ``Breach`` wording.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from drain_cycle import limits


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "limits.yml"
    path.write_text(body)
    return path


def test_load_returns_defaults_when_file_absent(tmp_path: Path) -> None:
    loaded = limits.load(tmp_path / "nonexistent.yml")
    assert loaded == limits.Limits()
    # The documented defaults, pinned so a careless edit to the dataclass
    # surfaces here.
    assert loaded.per_issue_tokens == 8_000_000
    assert loaded.per_issue_seconds == 20 * 60
    assert loaded.per_issue_cost_usd == 15.0
    assert loaded.cycle_tokens == 30_000_000
    assert loaded.cycle_seconds == 90 * 60
    assert loaded.cycle_cost_usd == 60.0


def test_load_returns_defaults_when_file_empty(tmp_path: Path) -> None:
    assert limits.load(_write(tmp_path, "")) == limits.Limits()


def test_load_overrides_only_named_keys_keeping_other_defaults(tmp_path: Path) -> None:
    loaded = limits.load(_write(tmp_path, "per_issue_tokens: 1000000\ncycle_cost_usd: 25\n"))
    assert loaded.per_issue_tokens == 1_000_000
    assert loaded.cycle_cost_usd == 25
    # Untouched keys keep their baked-in defaults.
    assert loaded.per_issue_seconds == limits.Limits().per_issue_seconds
    assert loaded.cycle_tokens == limits.Limits().cycle_tokens


def test_load_null_value_turns_a_guardrail_off(tmp_path: Path) -> None:
    loaded = limits.load(_write(tmp_path, "per_issue_tokens: null\nper_issue_cost_usd: null\n"))
    assert loaded.per_issue_tokens is None
    assert loaded.per_issue_cost_usd is None
    # A sibling guardrail stays at its default — "off" is per-key.
    assert loaded.per_issue_seconds == limits.Limits().per_issue_seconds


def test_load_rejects_non_mapping(tmp_path: Path) -> None:
    with pytest.raises(limits.LimitsConfigError, match="mapping of limit names"):
        limits.load(_write(tmp_path, "- just\n- a\n- list\n"))


def test_load_rejects_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(limits.LimitsConfigError, match="unknown keys: per_issue_dollars"):
        limits.load(_write(tmp_path, "per_issue_dollars: 5\n"))


@pytest.mark.parametrize(
    "value",
    ["0", "-1", "abc", "true"],
)
def test_load_rejects_non_positive_or_non_numeric(tmp_path: Path, value: str) -> None:
    with pytest.raises(limits.LimitsConfigError, match="positive number or null"):
        limits.load(_write(tmp_path, f"per_issue_tokens: {value}\n"))


def test_load_rejects_invalid_yaml(tmp_path: Path) -> None:
    with pytest.raises(limits.LimitsConfigError, match="not valid YAML"):
        limits.load(_write(tmp_path, "per_issue_tokens: [unclosed\n"))


def test_check_cycle_no_breach_under_all_caps() -> None:
    lim = limits.Limits()
    assert limits.check_cycle(lim, tokens=1, cost_usd=0.5, seconds=10.0) is None


def test_check_cycle_tokens_take_precedence_as_primary_guardrail() -> None:
    # Both tokens and cost are over their caps; tokens is reported first.
    lim = limits.Limits(cycle_tokens=100, cycle_cost_usd=10.0)
    breach = limits.check_cycle(lim, tokens=200, cost_usd=50.0, seconds=0.0)
    assert breach is not None
    assert breach.scope == "cycle"
    assert breach.metric == "token"
    assert breach.limit == 100
    assert breach.observed == 200


def test_check_cycle_reports_cost_then_time() -> None:
    lim = limits.Limits(cycle_tokens=None, cycle_cost_usd=10.0, cycle_seconds=5.0)
    cost_breach = limits.check_cycle(lim, tokens=999, cost_usd=12.0, seconds=99.0)
    assert cost_breach is not None and cost_breach.metric == "cost"

    lim_time_only = limits.Limits(cycle_tokens=None, cycle_cost_usd=None, cycle_seconds=5.0)
    time_breach = limits.check_cycle(lim_time_only, tokens=999, cost_usd=999.0, seconds=6.0)
    assert time_breach is not None and time_breach.metric == "time"


def test_check_cycle_skips_disabled_guardrails() -> None:
    lim = limits.Limits(cycle_tokens=None, cycle_cost_usd=None, cycle_seconds=None)
    assert limits.check_cycle(lim, tokens=10**12, cost_usd=10**6, seconds=10**6) is None


def test_breach_describe_wording() -> None:
    assert (
        limits.Breach("per-issue", "token", 8_000_000, 9_000_000).describe()
        == "per-issue token cap exceeded: 9,000,000 ≥ 8,000,000 tokens"
    )
    assert (
        limits.Breach("cycle", "cost", 60.0, 62.5).describe()
        == "cycle cost cap exceeded: $62.50 ≥ $60.00"
    )
    assert (
        limits.Breach("per-issue", "time", 1200, 1201).describe()
        == "per-issue time cap exceeded: 1,201s ≥ 1,200s"
    )
