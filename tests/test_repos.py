"""Tests for ``drain_cycle.repos`` (ABA-232).

Two distinct exception types must remain distinct: ``RepoConfigError``
is the startup-time gate (CLI exits 1 before any Linear traffic);
``RepoResolutionError`` is the per-issue halt (orchestrator writes a
run-log entry, no Linear revert). These tests pin both surfaces and
every variant of message tail called out in the issue spec.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from drain_cycle import repos


def _write_config(directory: Path, body: str) -> Path:
    path = directory / "repos.yml"
    path.write_text(textwrap.dedent(body).lstrip("\n"))
    return path


def test_load_returns_mapping_for_well_formed_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    config = _write_config(
        tmp_path,
        f"""
        repos:
          target: {target}
        """,
    )
    loaded = repos.load(config)
    assert loaded.mapping == {"target": target}


def test_load_expands_tilde_in_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = _write_config(
        tmp_path,
        """
        repos:
          target: ~/target
        """,
    )
    loaded = repos.load(config)
    assert loaded.mapping["target"] == tmp_path / "target"


def test_load_default_path_uses_home_dot_drain_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".drain-cycle").mkdir()
    cfg = tmp_path / ".drain-cycle" / "repos.yml"
    cfg.write_text(f"repos:\n  alpha: {tmp_path}\n")
    loaded = repos.load()
    assert loaded.mapping == {"alpha": tmp_path}


def test_load_raises_config_error_when_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent.yml"
    with pytest.raises(repos.RepoConfigError, match="not found"):
        repos.load(missing)


def test_missing_file_error_shows_actionable_shape(tmp_path: Path) -> None:
    """ABA-233 bootstrap UX: a fresh user with no repos.yml gets the
    missing path *and* the expected shape, not a bare 'not found'."""
    missing = tmp_path / "nonexistent.yml"
    with pytest.raises(repos.RepoConfigError) as exc:
        repos.load(missing)
    message = str(exc.value)
    assert str(missing) in message
    assert "repos:" in message
    assert "docs/repos.example.yml" in message


def test_load_raises_config_error_on_invalid_yaml(tmp_path: Path) -> None:
    config = tmp_path / "repos.yml"
    config.write_text("repos: [this is not balanced\n")
    with pytest.raises(repos.RepoConfigError, match="not valid YAML"):
        repos.load(config)


def test_load_raises_config_error_on_empty_file(tmp_path: Path) -> None:
    config = tmp_path / "repos.yml"
    config.write_text("")
    with pytest.raises(repos.RepoConfigError, match="empty"):
        repos.load(config)


def test_load_raises_config_error_when_top_level_repos_key_missing(
    tmp_path: Path,
) -> None:
    config = _write_config(
        tmp_path,
        """
        other:
          alpha: /tmp
        """,
    )
    with pytest.raises(repos.RepoConfigError, match="top-level"):
        repos.load(config)


def test_load_raises_config_error_when_repos_block_is_not_mapping(
    tmp_path: Path,
) -> None:
    config = _write_config(
        tmp_path,
        """
        repos:
          - first
          - second
        """,
    )
    with pytest.raises(repos.RepoConfigError, match="non-empty mapping"):
        repos.load(config)


def test_load_raises_config_error_when_repos_block_empty(tmp_path: Path) -> None:
    config = tmp_path / "repos.yml"
    config.write_text("repos: {}\n")
    with pytest.raises(repos.RepoConfigError, match="non-empty mapping"):
        repos.load(config)


def test_load_raises_config_error_when_path_value_not_string(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        """
        repos:
          alpha: 42
        """,
    )
    with pytest.raises(repos.RepoConfigError, match="must be a string"):
        repos.load(config)


def test_resolve_returns_mapped_path_for_single_repo_label(tmp_path: Path) -> None:
    r = repos.Repos(mapping={"alpha": tmp_path})
    issue = {"identifier": "ABA-1", "labels": ["repo:alpha"]}
    assert r.resolve(issue) == tmp_path


def test_resolve_ignores_non_repo_labels(tmp_path: Path) -> None:
    r = repos.Repos(mapping={"alpha": tmp_path})
    issue = {"identifier": "ABA-1", "labels": ["bug", "repo:alpha", "p1"]}
    assert r.resolve(issue) == tmp_path


def test_resolve_raises_when_no_repo_label(tmp_path: Path) -> None:
    r = repos.Repos(mapping={"alpha": tmp_path})
    issue = {"identifier": "ABA-1", "labels": ["bug"]}
    with pytest.raises(
        repos.RepoResolutionError, match="^no repo: label on issue$"
    ):
        r.resolve(issue)


def test_resolve_raises_when_labels_list_empty(tmp_path: Path) -> None:
    r = repos.Repos(mapping={"alpha": tmp_path})
    issue = {"identifier": "ABA-1", "labels": []}
    with pytest.raises(
        repos.RepoResolutionError, match="^no repo: label on issue$"
    ):
        r.resolve(issue)


def test_resolve_raises_when_multiple_repo_labels(tmp_path: Path) -> None:
    r = repos.Repos(mapping={"alpha": tmp_path, "beta": tmp_path})
    issue = {"identifier": "ABA-1", "labels": ["repo:beta", "repo:alpha"]}
    with pytest.raises(
        repos.RepoResolutionError,
        match=r"^multiple repo: labels: alpha, beta$",
    ):
        r.resolve(issue)


def test_resolve_raises_when_repo_name_not_in_config(tmp_path: Path) -> None:
    r = repos.Repos(mapping={"alpha": tmp_path})
    issue = {"identifier": "ABA-1", "labels": ["repo:unknown"]}
    with pytest.raises(
        repos.RepoResolutionError,
        match=r'^repo "unknown" not in ~/\.drain-cycle/repos\.yml$',
    ):
        r.resolve(issue)


def test_resolve_raises_when_resolved_path_does_not_exist(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere"
    r = repos.Repos(mapping={"alpha": missing})
    issue = {"identifier": "ABA-1", "labels": ["repo:alpha"]}
    with pytest.raises(
        repos.RepoResolutionError,
        match=f"^resolved path {missing} does not exist$",
    ):
        r.resolve(issue)
