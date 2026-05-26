"""Unit tests for the telemetry module's load-bearing invariants.

These pin the two behaviours the rest of the suite relies on but can't observe
(it runs with no ``HONEYCOMB_API_KEY``, so the no-op tracer is transparent):
``setup`` does nothing without a key, and ``shutdown`` flushes exactly once.
``setup`` is never called *with* a key here — that would install a global
provider and instrument ``httpx`` process-wide, leaking into other tests.
"""
from __future__ import annotations

import pytest
from opentelemetry.trace import StatusCode

from drain_cycle import telemetry


def test_setup_is_noop_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HONEYCOMB_API_KEY", raising=False)
    monkeypatch.setattr(telemetry, "_provider", None)

    assert telemetry.setup() is False
    assert telemetry._provider is None


def test_shutdown_flushes_once_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProvider:
        def __init__(self) -> None:
            self.shutdowns = 0

        def shutdown(self) -> None:
            self.shutdowns += 1

    fake = FakeProvider()
    monkeypatch.setattr(telemetry, "_provider", fake)

    telemetry.shutdown()
    telemetry.shutdown()

    assert fake.shutdowns == 1
    assert telemetry._provider is None


def test_shutdown_without_setup_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry, "_provider", None)
    telemetry.shutdown()  # must not raise


def test_mark_error_sets_flag_slug_and_error_status() -> None:
    class FakeSpan:
        def __init__(self) -> None:
            self.attrs: dict[str, object] = {}
            self.status = None

        def set_attribute(self, key: str, value: object) -> None:
            self.attrs[key] = value

        def set_status(self, status: object) -> None:
            self.status = status

    span = FakeSpan()
    telemetry.mark_error(span, "err-test-slug", "boom")

    assert span.attrs["error"] is True
    assert span.attrs["exception.slug"] == "err-test-slug"
    assert span.status is not None
    assert span.status.status_code == StatusCode.ERROR
