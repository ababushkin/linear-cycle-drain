"""Tests for ``drain_cycle.model``.

Worker-model resolution is lenient by contract: it always returns a usable
model and never raises, so an unattended cycle never halts on a mis-labelled
issue. These tests pin the default, the alias mapping, verbatim pass-through
of unknown values, and the ambiguous-label fallback.
"""
from __future__ import annotations

from drain_cycle import model


def test_no_model_label_defaults_to_sonnet() -> None:
    issue = {"identifier": "ABA-1", "labels": ["repo:alpha"]}
    assert model.resolve(issue) == "claude-sonnet-4-6"


def test_missing_labels_key_defaults_to_sonnet() -> None:
    assert model.resolve({"identifier": "ABA-1"}) == "claude-sonnet-4-6"


def test_model_sonnet_alias() -> None:
    issue = {"identifier": "ABA-1", "labels": ["model:sonnet"]}
    assert model.resolve(issue) == "claude-sonnet-4-6"


def test_model_opus_alias() -> None:
    issue = {"identifier": "ABA-1", "labels": ["repo:alpha", "model:opus"]}
    assert model.resolve(issue) == "claude-opus-4-7"


def test_model_haiku_alias() -> None:
    issue = {"identifier": "ABA-1", "labels": ["model:haiku"]}
    assert model.resolve(issue) == "claude-haiku-4-5-20251001"


def test_unknown_alias_passes_through_verbatim() -> None:
    issue = {"identifier": "ABA-1", "labels": ["model:claude-opus-4-7"]}
    assert model.resolve(issue) == "claude-opus-4-7"


def test_multiple_model_labels_fall_back_to_default() -> None:
    issue = {"identifier": "ABA-1", "labels": ["model:opus", "model:sonnet"]}
    assert model.resolve(issue) == "claude-sonnet-4-6"
