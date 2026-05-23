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

**Process-group kill.** The worker is launched with
``start_new_session=True`` so it leads its own process group. On timeout
the whole group is SIGKILLed via ``os.killpg``, reaping grandchildren —
MCP servers, sub-agents — that a kill of the direct child alone would
orphan to keep burning tokens. SIGKILL (not a SIGTERM grace) is used
deliberately: a session being force-terminated past its deadline has no
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
from typing import Any, TextIO

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
    signal, e.g. ``-9`` on the timeout SIGKILL). ``timed_out`` is the
    branch selector the orchestrator reads to take its timeout-halt path.
    ``usage`` carries the four token components plus ``cumulative`` and
    ``peak_context``. ``cost_usd`` / ``num_turns`` / ``session_id`` /
    ``is_error`` come from the terminal ``result`` event and are ``None``
    when the session was killed before one arrived.
    """

    exit_code: int
    timed_out: bool
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
    timeout_seconds: float,
    passthrough: TextIO | None = None,
) -> WorkerResult:
    """Spawn one streaming ``claude -p`` session and return its usage.

    ``claude_cmd`` is the base argv (``["claude", "-p",
    "--dangerously-skip-permissions"]``); the streaming flags, ``--model``,
    and the prompt are appended here. The session runs in its own process
    group; on exceeding ``timeout_seconds`` the whole group is killed.

    Non-JSON lines on the merged stdout/stderr stream (claude warnings,
    hook stderr) are written to ``passthrough`` (default ``sys.stderr``) so
    the operator still sees diagnostics; recognised JSON events are
    consumed by the usage parser.
    """
    # The prompt is the trailing positional; it must follow the value-taking
    # ``--model`` option, not sit between an option and its value.
    argv = [*claude_cmd, *_STREAM_FLAGS, "--model", model, prompt]
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
    reader = threading.Thread(
        target=_drain_stream,
        args=(proc.stdout, accumulator, sink),
        daemon=True,
    )
    reader.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(proc)
        proc.wait()
    reader.join(timeout=_READER_JOIN_TIMEOUT_SECONDS)
    duration = time.monotonic() - started

    cost_usd, num_turns, session_id, is_error = accumulator.summary()
    return WorkerResult(
        exit_code=proc.returncode,
        timed_out=timed_out,
        duration_seconds=duration,
        model=model,
        usage=accumulator.usage(),
        cost_usd=cost_usd,
        num_turns=num_turns,
        session_id=session_id,
        is_error=is_error,
    )


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
    stream: TextIO | None, accumulator: _UsageAccumulator, sink: TextIO
) -> None:
    """Read the worker's merged output line by line until EOF.

    Recognised JSON object events feed the usage accumulator; everything
    else (non-JSON diagnostics, JSON that isn't an object) is echoed to
    ``sink`` so the operator keeps the visibility a captured pipe would
    otherwise hide.
    """
    if stream is None:
        return
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
