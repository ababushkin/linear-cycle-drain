"""OpenTelemetry tracing wired to Honeycomb.

Opt-in by design: :func:`setup` is a no-op unless a Honeycomb ingest key is in
the environment, so a drain with no telemetry configured behaves exactly as it
did before and never blocks on an exporter it cannot reach. The module-level
:data:`tracer` is always safe to call — with no provider installed the OTel API
hands back no-op spans, so the ``start_as_current_span`` calls scattered through
the orchestrator, worker, worktree, and Linear client cost nothing when
telemetry is off.

When a key is present, :func:`setup` installs a ``BatchSpanProcessor`` exporting
OTLP/HTTP to Honeycomb, turns on ``httpx`` auto-instrumentation (the Linear
GraphQL traffic), and registers :func:`shutdown` with ``atexit``. The flush-on-
exit registration is load-bearing: ``drain-cycle`` is a short-lived CLI that
exits via ``sys.exit``, and without a final flush the batch processor's queued
spans die with the interpreter and the last issues of a drain never ship.

Configuration (read from the same environment the CLI loads ``.env`` into):

* ``HONEYCOMB_API_KEY``    — ingest key; its presence is the on/off switch.
* ``HONEYCOMB_API_ENDPOINT`` — ingest host, default ``https://api.honeycomb.io``
  (set the EU host here for an EU team).
* ``OTEL_SERVICE_NAME``    — service name, which is also the Honeycomb dataset;
  defaults to ``drain-cycle``.
"""
from __future__ import annotations

import atexit
import os
from importlib.metadata import PackageNotFoundError, version

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

_SERVICE_NAME = "drain-cycle"
_DEFAULT_ENDPOINT = "https://api.honeycomb.io"

tracer = trace.get_tracer(_SERVICE_NAME)
"""Process-wide tracer. Obtained at import time; the OTel API's proxy provider
delegates to the real provider once :func:`setup` installs one, so binding this
before setup runs (or when it never does) is correct."""

_provider = None
"""The installed ``TracerProvider`` once :func:`setup` succeeds; gates
:func:`shutdown` so a double call (explicit + ``atexit``) flushes once."""


def setup() -> bool:
    """Install the tracer provider + Honeycomb exporter; report whether it ran.

    Returns ``True`` when telemetry is now active, ``False`` when no
    ``HONEYCOMB_API_KEY`` was set (the no-op default tracer stays in place).
    Idempotent: a second call once active is a no-op that returns ``True``.
    """
    global _provider
    if _provider is not None:
        return True
    api_key = os.environ.get("HONEYCOMB_API_KEY")
    if not api_key:
        return False

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    endpoint = os.environ.get("HONEYCOMB_API_ENDPOINT", _DEFAULT_ENDPOINT).rstrip("/")
    service_name = os.environ.get("OTEL_SERVICE_NAME", _SERVICE_NAME)
    resource = Resource.create(
        {"service.name": service_name, "service.version": _package_version()}
    )
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(
        endpoint=f"{endpoint}/v1/traces",
        headers={"x-honeycomb-team": api_key},
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    atexit.register(shutdown)
    _provider = provider
    return True


def shutdown() -> None:
    """Flush queued spans and tear the provider down. Idempotent."""
    global _provider
    if _provider is None:
        return
    provider, _provider = _provider, None
    provider.shutdown()


def mark_error(span: Span, slug: str, reason: str) -> None:
    """Tag ``span`` as a failure site.

    Sets ``error=true`` (a Honeycomb-conventional boolean to filter on), a
    static greppable ``exception.slug`` that ties the trace back to one throw
    site in the code, and the span's status to ERROR with ``reason`` as the
    message. ``slug`` must be a fixed string, not interpolated, so it stays
    low-cardinality and safe to ``GROUP BY``.
    """
    span.set_attribute("error", True)
    span.set_attribute("exception.slug", slug)
    span.set_status(Status(StatusCode.ERROR, reason))


def _package_version() -> str:
    try:
        return version("drain-cycle")
    except PackageNotFoundError:
        return "0.0.0"
