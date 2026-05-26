"""Spawn a streaming ``claude -p`` worker for one issue and record its usage.

The orchestrator owns the cycle loop and the Linear lifecycle; this module
owns one spawned session: how it is launched, how its token usage is
parsed off the wire, and how it is force-terminated when it overruns the
per-issue time cap.

**Why stream-json.** A bare ``claude -p`` prints only the final assistant
text; the run log then records exit code and wall-clock and nothing about
spend. ``--output-format stream-json`` (requires ``--verbose``) emits one
JSON event per line — per-turn ``assistant`` messages plus a terminal
``result`` — from which we reconstruct exactly what the session cost.

**Token accounting.** Each ``assistant`` event carries ``message.usage``,
but the same assistant message is emitted *once per content block*
(thinking, text, tool_use) — every copy repeats the identical usage under
the identical ``message.id``. Summing per event therefore double- (or
triple-) counts a turn; we key by ``message.id`` so each turn's usage is
counted exactly once. ``cumulative`` is the sum across turns of all four
token components — the real billed-token figure, dominated in long
tool-use loops by cache reads re-paid every turn. ``peak_context`` is the
largest single-turn context (input + cache_read + cache_create), i.e. how
close the session came to the context-window ceiling.

The terminal ``result`` event is authoritative for ``cost_usd``
(``total_cost_usd``), ``num_turns``, ``session_id`` and ``is_error``. Note
``result.usage`` is only the *final* turn's snapshot, not the session
total, so it is deliberately not used for the token figures — those come
from the accumulated per-turn stream, which is also what survives a kill
before any ``result`` arrives.

**Guardrails — belt and suspenders.** ``per_issue_cost_usd`` is handed to
``claude`` as ``--max-budget-usd`` so the session self-terminates on cost
(the native belt). The token and time caps are the orchestrator's
suspenders: a poll loop watches the live cumulative-token tally and the
elapsed wall-clock, and SIGKILLs the session the moment either is crossed.
Any cap passed as ``None`` is not enforced; with both token and time caps
off the worker simply waits for the session to exit on its own. A crossed
cap is reported back as a ``Breach`` so the orchestrator can name it in the
halt entry. See ``limits.py`` for the thresholds and their precedence.

**Process-group kill.** The worker is launched with
``start_new_session=True`` so it leads its own process group. On a breach
the whole group is SIGKILLed via ``os.killpg``, reaping grandchildren —
MCP servers, sub-agents — that a kill of the direct child alone would
orphan to keep burning tokens. SIGKILL (not a SIGTERM grace) is used
deliberately: a session being force-terminated past a guardrail has no
clean-shutdown work worth waiting for, and SIGKILL is the only signal a
misbehaving grandchild cannot ignore. Killing the whole group also closes
the stdout pipe those grandchildren inherited, which is what lets the
reader thread reach EOF instead of blocking forever.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

from opentelemetry.trace import Span

from . import telemetry
from .limits import Breach

_STREAM_FLAGS = ["--verbose", "--output-format", "stream-json"]
"""Flags that switch ``claude -p`` into line-delimited JSON event mode.
``--verbose`` is required: without it ``stream-json`` emits only the
terminal ``result`` event, dropping the per-turn ``assistant`` usage we
accumulate."""

_READER_JOIN_TIMEOUT_SECONDS = 5.0
"""Bound on waiting for the stdout-reader thread to drain to EOF after the
process exits. The group SIGKILL closes every inherited write-end of the
pipe, so EOF normally arrives at once; the bound only guards the
pathological case of a group member that somehow escaped the kill, so the
worker returns with the usage it has rather than hanging."""

_POLL_INTERVAL_SECONDS = 1.0
"""How often the breach monitor wakes to compare the live token tally and
elapsed wall-clock against the per-issue caps. A worry-free overshoot of at
most one interval before the kill lands; one second is responsive enough for
a session measured in minutes and cheap enough to poll."""

_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


@dataclass
class WorkerResult:
    """Outcome of one spawned session, recorded into the run-log entry.

    ``exit_code`` is the process return code (negative when killed by a
    signal, e.g. ``-9`` on a guardrail SIGKILL). ``breach`` is the branch
    selector the orchestrator reads: ``None`` when the session ran to its
    own exit, otherwise the per-issue token or time cap that was crossed,
    carrying the cap and the value observed at kill time. ``usage`` carries
    the four token components plus ``cumulative`` and ``peak_context``.
    ``cost_usd`` / ``num_turns`` / ``session_id`` / ``is_error`` come from
    the terminal ``result`` event and are ``None`` when the session was
    killed before one arrived.
    """

    exit_code: int
    breach: Breach | None
    duration_seconds: float
    model: str
    usage: dict[str, int]
    cost_usd: float | None
    num_turns: int | None
    session_id: str | None
    is_error: bool | None


def run_issue(
    *,
    claude_cmd: list[str],
    model: str,
    prompt: str,
    cwd: Path,
    token_limit: int | None,
    time_limit_seconds: float | None,
    cost_limit_usd: float | None,
    passthrough: TextIO | None = None,
    debug_file: Path | None = None,
    poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
    on_progress: Callable[[int, int, int, float | None, float], None] | None = None,
) -> WorkerResult:
    """Spawn one streaming ``claude -p`` session and return its usage.

    ``claude_cmd`` is the base argv (``["claude", "-p",
    "--dangerously-skip-permissions"]``); the streaming flags, ``--model``,
    ``--max-budget-usd`` (when ``cost_limit_usd`` is set), and the prompt are
    appended here. The session runs in its own process group; if its
    cumulative tokens cross ``token_limit`` or its wall-clock crosses
    ``time_limit_seconds`` the whole group is killed and the returned
    ``WorkerResult.breach`` names the cap. A limit passed as ``None`` is not
    enforced. ``cost_limit_usd`` is enforced by ``claude`` itself, not here.

    Non-JSON lines on the merged stdout/stderr stream (claude warnings,
    hook stderr) are written to ``passthrough`` (default ``sys.stderr``) so
    the operator still sees diagnostics; recognised JSON events are
    consumed by the usage parser.

    ``debug_file``, when set, is passed to ``claude`` as ``--debug-file`` so
    the session writes its startup diagnostics — which settings sources,
    plugins, MCP servers, and hooks initialised — to that path. Debug logs
    go to the file, not stderr, so the merged stream the usage parser reads
    is unaffected. Opt-in only; ``None`` (the default) passes no flag and the
    session behaves exactly as before. See ``docs/design-decisions.md`` §10.

    ``on_progress``, when set, is called from the reader thread after each
    new assistant turn with ``(turns, cumulative_tokens, peak_context_tokens,
    cost_usd, elapsed_seconds)``. Callers use this for live progress display
    and the active-run marker update.
    """
    with telemetry.tracer.start_as_current_span("drain.worker.session") as span:
        span.set_attribute("worker.model", model)
        # The prompt is the trailing positional; it must follow the value-taking
        # ``--model`` / ``--max-budget-usd`` / ``--debug-file`` options, not sit
        # between an option and its value.
        argv = [*claude_cmd, *_STREAM_FLAGS, "--model", model]
        if cost_limit_usd is not None:
            argv += ["--max-budget-usd", f"{cost_limit_usd:g}"]
        if debug_file is not None:
            argv += ["--debug-file", str(debug_file)]
        argv.append(prompt)
        sink = passthrough if passthrough is not None else sys.stderr
        accumulator = _UsageAccumulator()

        started = time.monotonic()
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )

        # Wrap on_progress to inject elapsed_seconds computed from the start time.
        _progress_cb: Callable[[int, int, int, float | None], None] | None = None
        if on_progress is not None:
            def _progress_cb(turns: int, cumulative: int, peak: int, cost_usd: float | None) -> None:
                elapsed = time.monotonic() - started
                on_progress(turns, cumulative, peak, cost_usd, elapsed)

        reader = threading.Thread(
            target=_drain_stream,
            args=(proc.stdout, accumulator, sink, _progress_cb),
            daemon=True,
        )
        reader.start()

        breach = _monitor(
            proc,
            accumulator,
            started=started,
            token_limit=token_limit,
            time_limit_seconds=time_limit_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        if breach is not None:
            _kill_process_group(proc)
            proc.wait()
        reader.join(timeout=_READER_JOIN_TIMEOUT_SECONDS)
        duration = time.monotonic() - started

        cost_usd, num_turns, session_id, is_error = accumulator.summary()
        result = WorkerResult(
            exit_code=proc.returncode,
            breach=breach,
            duration_seconds=duration,
            model=model,
            usage=accumulator.usage(),
            cost_usd=cost_usd,
            num_turns=num_turns,
            session_id=session_id,
            is_error=is_error,
        )
        _annotate_worker_span(span, result)
        return result


def _annotate_worker_span(span: Span, result: WorkerResult) -> None:
    """Record a finished session's usage onto its span.

    Token totals, cost, turn count, and the session id become attributes so a
    drain's spend is queryable in Honeycomb (HEATMAP cost, GROUP BY model). A
    crossed cap is marked as an error with the breach's scope/metric/limit/
    observed broken out and a static slug — so a guardrail kill is filterable
    and ties back to the worker's monitor loop. ``is_error`` is the claude
    session's own terminal flag, distinct from a breach kill.
    """
    usage = result.usage
    span.set_attribute("worker.exit_code", result.exit_code)
    span.set_attribute("worker.duration_seconds", result.duration_seconds)
    span.set_attribute("worker.tokens.cumulative", usage.get("cumulative", 0))
    span.set_attribute("worker.tokens.peak_context", usage.get("peak_context", 0))
    if result.num_turns is not None:
        span.set_attribute("worker.num_turns", result.num_turns)
    if result.cost_usd is not None:
        span.set_attribute("worker.cost_usd", result.cost_usd)
    if result.session_id is not None:
        span.set_attribute("worker.session_id", result.session_id)
    if result.is_error is not None:
        span.set_attribute("worker.is_error", result.is_error)
    if result.breach is not None:
        breach = result.breach
        span.set_attribute("worker.breach.scope", breach.scope)
        span.set_attribute("worker.breach.metric", breach.metric)
        span.set_attribute("worker.breach.limit", breach.limit)
        span.set_attribute("worker.breach.observed", breach.observed)
        telemetry.mark_error(span, "err-worker-breach", breach.describe())


def _monitor(
    proc: subprocess.Popen[str],
    accumulator: _UsageAccumulator,
    *,
    started: float,
    token_limit: int | None,
    time_limit_seconds: float | None,
    poll_interval_seconds: float,
) -> Breach | None:
    """Wait for the session, returning the per-issue cap it breaches (if any).

    With both caps off there is nothing to watch for, so we block on the
    process directly. Otherwise we wake every ``poll_interval_seconds`` to
    compare the live cumulative-token tally and elapsed wall-clock against
    the caps; the first crossed cap is returned for the caller to kill on.
    Returns ``None`` when the session exits on its own first.
    """
    if token_limit is None and time_limit_seconds is None:
        proc.wait()
        return None
    while True:
        try:
            proc.wait(timeout=poll_interval_seconds)
            return None
        except subprocess.TimeoutExpired:
            pass
        if token_limit is not None:
            observed = accumulator.cumulative()
            if observed >= token_limit:
                return Breach("per-issue", "token", token_limit, observed)
        if time_limit_seconds is not None:
            elapsed = time.monotonic() - started
            if elapsed >= time_limit_seconds:
                return Breach("per-issue", "time", time_limit_seconds, elapsed)


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the worker's whole process group, reaping grandchildren.

    No-ops if the leader has already exited (its pid — and thus the group
    id — is gone). SIGKILL is sent before the caller reaps the leader, so
    the group still exists and its id cannot have been reused.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _drain_stream(
    stream: TextIO | None,
    accumulator: _UsageAccumulator,
    sink: TextIO,
    on_progress: Callable[[int, int, int, float | None], None] | None = None,
) -> None:
    """Read the worker's merged output line by line until EOF.

    Recognised JSON object events feed the usage accumulator; everything
    else (non-JSON diagnostics, JSON that isn't an object) is echoed to
    ``sink`` so the operator keeps the visibility a captured pipe would
    otherwise hide. ``on_progress`` is called once per new turn (deduplicated
    by message id) with a live usage snapshot.
    """
    if stream is None:
        return
    last_message_id: str | None = None
    try:
        for line in stream:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                _echo(sink, line)
                continue
            if isinstance(event, dict):
                accumulator.feed(event)
                if on_progress is not None and event.get("type") == "assistant":
                    mid = (event.get("message") or {}).get("id")
                    if mid != last_message_id:
                        last_message_id = mid
                        snap = accumulator.live_snapshot()
                        on_progress(*snap)
            else:
                _echo(sink, line)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _echo(sink: TextIO, line: str) -> None:
    try:
        print(line, file=sink)
    except (OSError, ValueError):
        pass


def _coerce_int(value: Any) -> int:
    return value if isinstance(value, int) else 0


class _UsageAccumulator:
    """Reconstruct session usage from the streamed events.

    Per-turn usage is keyed by ``message.id`` so a turn emitted once per
    content block is counted once; the last copy wins (later events carry
    the more complete snapshot). The ``result`` event overwrites the
    session-summary fields each time it is seen.

    All mutation happens on the reader thread and all reads happen on the
    main thread after it is joined — but the join is bounded, so a group
    member that escaped the kill (e.g. via ``setsid``) could keep the
    reader alive past the join. The lock makes the concurrent case safe
    rather than a ``dictionary changed size during iteration`` crash.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turns: dict[str, dict[str, int]] = {}
        self._anon = 0
        self.cost_usd: float | None = None
        self.num_turns: int | None = None
        self.session_id: str | None = None
        self.is_error: bool | None = None

    def feed(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "assistant":
            self._feed_assistant(event)
        elif event_type == "result":
            self._feed_result(event)

    def _feed_assistant(self, event: dict[str, Any]) -> None:
        message = event.get("message")
        if not isinstance(message, dict):
            return
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return
        message_id = message.get("id")
        turn = {field: _coerce_int(usage.get(field)) for field in _TOKEN_FIELDS}
        with self._lock:
            if isinstance(message_id, str):
                key = message_id
            else:
                key = f"_anon-{self._anon}"
                self._anon += 1
            self._turns[key] = turn

    def _feed_result(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.cost_usd = event.get("total_cost_usd")
            self.num_turns = event.get("num_turns")
            self.session_id = event.get("session_id")
            self.is_error = event.get("is_error")

    def cumulative(self) -> int:
        """Live billed-token total across turns — the per-issue token cap is
        compared against this on every poll, so it reads under the lock and
        stays cheap (no peak-context / per-component breakdown)."""
        with self._lock:
            turns = list(self._turns.values())
        return sum(turn[field] for turn in turns for field in _TOKEN_FIELDS)

    def live_snapshot(self) -> tuple[int, int, int, float | None]:
        """Current ``(turns, cumulative_tokens, peak_context_tokens, cost_usd)``.

        Called from the reader thread after each new assistant turn. Reads
        under the lock so the main-thread monitor cannot see a torn state.
        """
        with self._lock:
            turn_count = len(self._turns)
            turns_list = list(self._turns.values())
            cost = self.cost_usd
        cumulative = sum(t[f] for t in turns_list for f in _TOKEN_FIELDS)
        peak = max(
            (
                t["input_tokens"]
                + t["cache_read_input_tokens"]
                + t["cache_creation_input_tokens"]
                for t in turns_list
            ),
            default=0,
        )
        return turn_count, cumulative, peak, cost

    def usage(self) -> dict[str, int]:
        totals = {field: 0 for field in _TOKEN_FIELDS}
        peak_context = 0
        with self._lock:
            turns = list(self._turns.values())
        for turn in turns:
            for field in _TOKEN_FIELDS:
                totals[field] += turn[field]
            context = (
                turn["input_tokens"]
                + turn["cache_read_input_tokens"]
                + turn["cache_creation_input_tokens"]
            )
            peak_context = max(peak_context, context)
        return {
            **totals,
            "cumulative": sum(totals.values()),
            "peak_context": peak_context,
        }

    def summary(self) -> tuple[float | None, int | None, str | None, bool | None]:
        """Read the result-event fields under the lock, as a snapshot."""
        with self._lock:
            return self.cost_usd, self.num_turns, self.session_id, self.is_error
