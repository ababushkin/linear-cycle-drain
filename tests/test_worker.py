"""Unit tests for the streaming worker.

What we substitute and why:

* Spawned ``claude -p``: replaced with a real shell script that ``cat``s a
  canned stream-json file to stdout (the canned file sidesteps shell
  JSON-quoting). This is the only honest way to exercise the line-by-line
  Popen reader, the dedup-by-message-id accumulation, and the process-group
  kill — an in-process mock of ``Popen`` would prove none of those.
* Real OS process groups: the timeout test spawns a script that itself
  backgrounds a grandchild ``sleep``; after the worker times out we assert
  the grandchild pid is dead, which only holds if the *group* (not just the
  direct child) was killed.

The canned ``assistant`` events deliberately repeat one message id across
two events — the real ``stream-json`` emits a turn once per content block
(thinking, text, tool_use), each copy carrying the identical usage — so
the happy-path test pins that a turn is counted exactly once.
"""
from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

from drain_cycle import worker


def _fake_claude_streaming(tmp_path: Path, lines: list[str], *, hang: bool = False) -> Path:
    """A ``claude -p`` stand-in that emits ``lines`` as stdout then exits.

    With ``hang=True`` the script sleeps after emitting (holding stdout
    open and producing no terminal ``result``), so the worker hits its
    timeout with a partially-parsed stream.
    """
    stream_file = tmp_path / "stream.jsonl"
    stream_file.write_text("\n".join(lines) + "\n")
    script = tmp_path / "fake-claude.sh"
    body = f'#!/bin/sh\ncat "{stream_file}"\n'
    if hang:
        body += "sleep 30\n"
    script.write_text(body)
    script.chmod(0o755)
    return script


def _assistant(message_id: str, *, inp: int, out: int, cc: int, cr: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": message_id,
                "usage": {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cache_creation_input_tokens": cc,
                    "cache_read_input_tokens": cr,
                },
            },
        }
    )


def _result(**fields: object) -> str:
    return json.dumps({"type": "result", **fields})


def _wait_dead(pid: int, timeout: float = 3.0) -> bool:
    """Poll until ``pid`` no longer exists (process-group kill reaped it)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


def test_run_issue_accumulates_usage_deduped_by_message_id(tmp_path: Path) -> None:
    """Two turns, the first emitted twice under one id; the duplicate is
    counted once, totals sum across turns, peak_context is the max single
    turn, and the session summary comes from the ``result`` event."""
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"}),
        # Turn one, emitted twice (thinking block then text block) — same id.
        _assistant("msg_a", inp=10, out=4, cc=100, cr=0),
        _assistant("msg_a", inp=10, out=4, cc=100, cr=0),
        "this is not json and must be tolerated",
        # Turn two — distinct id, larger context.
        _assistant("msg_b", inp=5, out=20, cc=0, cr=300),
        _result(
            total_cost_usd=0.42,
            num_turns=2,
            session_id="sess-1",
            is_error=False,
            usage={"input_tokens": 5, "output_tokens": 20},
        ),
    ]
    script = _fake_claude_streaming(tmp_path, lines)
    passthrough = io.StringIO()

    result = worker.run_issue(
        claude_cmd=[str(script)],
        model="claude-sonnet-4-6",
        prompt="ignored by the fake",
        cwd=tmp_path,
        token_limit=None,
        time_limit_seconds=30.0,
        cost_limit_usd=None,
        passthrough=passthrough,
    )

    assert result.breach is None
    assert result.exit_code == 0
    assert result.model == "claude-sonnet-4-6"

    # msg_a counted once: input 10+5=15, output 4+20=24, cc 100+0=100,
    # cr 0+300=300 → cumulative 439.
    assert result.usage == {
        "input_tokens": 15,
        "output_tokens": 24,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 300,
        "cumulative": 439,
        "peak_context": 305,  # turn two: 5 + 300 + 0
    }

    # Session summary from the terminal result event.
    assert result.cost_usd == 0.42
    assert result.num_turns == 2
    assert result.session_id == "sess-1"
    assert result.is_error is False

    assert result.duration_seconds >= 0.0
    # The non-JSON line was echoed to the passthrough, not silently dropped.
    assert "this is not json and must be tolerated" in passthrough.getvalue()


def test_run_issue_times_out_and_kills_whole_process_group(tmp_path: Path) -> None:
    """A worker that overruns its time cap is killed at the process-group
    level, reaping a grandchild that a kill of the direct child alone would
    orphan — the bug ``subprocess.run(timeout=)`` had."""
    pidfile = tmp_path / "grandchild.pid"
    script = tmp_path / "hanging-claude.sh"
    script.write_text(
        "#!/bin/sh\n"
        "sleep 30 &\n"  # grandchild; would survive a direct-child-only kill
        f'echo $! > "{pidfile}"\n'
        "sleep 30\n"  # the worker itself hangs, emitting no output
    )
    script.chmod(0o755)

    result = worker.run_issue(
        claude_cmd=[str(script)],
        model="claude-sonnet-4-6",
        prompt="ignored",
        cwd=tmp_path,
        token_limit=None,
        time_limit_seconds=0.5,
        cost_limit_usd=None,
        passthrough=io.StringIO(),
        poll_interval_seconds=0.05,
    )

    assert result.breach is not None
    assert result.breach.scope == "per-issue"
    assert result.breach.metric == "time"
    assert result.breach.limit == 0.5
    assert result.breach.observed >= 0.5
    # Killed by signal → negative return code.
    assert result.exit_code < 0
    # No stream was emitted, so usage is all-zero and the summary is empty.
    assert result.usage["cumulative"] == 0
    assert result.usage["peak_context"] == 0
    assert result.cost_usd is None
    assert result.num_turns is None
    assert result.session_id is None
    assert result.is_error is None

    grandchild_pid = int(pidfile.read_text().strip())
    assert _wait_dead(grandchild_pid), (
        f"grandchild {grandchild_pid} survived the time cap — process group "
        "was not killed"
    )


def test_run_issue_falls_back_to_accumulated_usage_when_killed_before_result(
    tmp_path: Path,
) -> None:
    """If the session is killed before a ``result`` event, the usage parsed
    from the partial stream is still recorded; the result-only summary
    fields are left ``None``."""
    lines = [
        _assistant("msg_a", inp=10, out=4, cc=100, cr=0),
        _assistant("msg_b", inp=5, out=20, cc=0, cr=300),
    ]
    script = _fake_claude_streaming(tmp_path, lines, hang=True)

    result = worker.run_issue(
        claude_cmd=[str(script)],
        model="claude-opus-4-7",
        prompt="ignored",
        cwd=tmp_path,
        token_limit=None,
        time_limit_seconds=1.0,
        cost_limit_usd=None,
        passthrough=io.StringIO(),
        poll_interval_seconds=0.05,
    )

    assert result.breach is not None
    assert result.breach.metric == "time"
    # Accumulated from the two partial-stream turns.
    assert result.usage["cumulative"] == 439
    assert result.usage["peak_context"] == 305
    assert result.usage["cache_read_input_tokens"] == 300
    # No result event arrived.
    assert result.cost_usd is None
    assert result.num_turns is None
    assert result.session_id is None
    assert result.is_error is None


def test_run_issue_kills_process_group_on_token_breach(tmp_path: Path) -> None:
    """The primary guardrail: a session whose live cumulative tokens cross
    the per-issue cap is killed at the process-group level (grandchild
    reaped), and the breach names the cap and the value at kill time."""
    stream_file = tmp_path / "stream.jsonl"
    # One turn far over the tiny cap, then the script hangs so the monitor's
    # poll catches the breach instead of the process exiting on its own.
    stream_file.write_text(_assistant("msg_big", inp=10_000, out=0, cc=0, cr=0) + "\n")
    pidfile = tmp_path / "grandchild.pid"
    script = tmp_path / "overshoot-claude.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'cat "{stream_file}"\n'  # emit the overshooting usage
        "sleep 30 &\n"  # grandchild; survives a direct-child-only kill
        f'echo $! > "{pidfile}"\n'
        "sleep 30\n"  # hang past the cap
    )
    script.chmod(0o755)

    result = worker.run_issue(
        claude_cmd=[str(script)],
        model="claude-sonnet-4-6",
        prompt="ignored",
        cwd=tmp_path,
        token_limit=100,
        time_limit_seconds=None,
        cost_limit_usd=None,
        passthrough=io.StringIO(),
        poll_interval_seconds=0.05,
    )

    assert result.breach is not None
    assert result.breach.scope == "per-issue"
    assert result.breach.metric == "token"
    assert result.breach.limit == 100
    assert result.breach.observed >= 100
    assert result.exit_code < 0
    # The overshooting turn was recorded before the kill.
    assert result.usage["cumulative"] == 10_000

    grandchild_pid = int(pidfile.read_text().strip())
    assert _wait_dead(grandchild_pid), (
        f"grandchild {grandchild_pid} survived the token cap — process group "
        "was not killed"
    )


def _argv_capturing_claude(tmp_path: Path, argv_file: Path) -> Path:
    """A ``claude -p`` stand-in that records its argv (one token per line)
    to ``argv_file`` then exits, so a test can assert which flags were passed."""
    script = tmp_path / "argv-claude.sh"
    script.write_text(
        "#!/bin/sh\n"
        f': > "{argv_file}"\n'
        f'for a in "$@"; do printf "%s\\n" "$a" >> "{argv_file}"; done\n'
    )
    script.chmod(0o755)
    return script


def test_run_issue_passes_max_budget_usd_flag(tmp_path: Path) -> None:
    """The per-issue cost cap is handed to ``claude`` as ``--max-budget-usd``
    (the native belt), and the prompt stays the trailing positional after the
    value-taking flags."""
    argv_file = tmp_path / "argv.txt"
    script = _argv_capturing_claude(tmp_path, argv_file)

    result = worker.run_issue(
        claude_cmd=[str(script)],
        model="claude-sonnet-4-6",
        prompt="THE-PROMPT",
        cwd=tmp_path,
        token_limit=None,
        time_limit_seconds=None,
        cost_limit_usd=15.0,
        passthrough=io.StringIO(),
    )

    argv = argv_file.read_text().splitlines()
    assert "--max-budget-usd" in argv
    assert argv[argv.index("--max-budget-usd") + 1] == "15"
    assert argv[-1] == "THE-PROMPT"
    assert result.breach is None


def test_run_issue_omits_max_budget_when_cost_limit_none(tmp_path: Path) -> None:
    """With the cost guardrail off, no ``--max-budget-usd`` is passed."""
    argv_file = tmp_path / "argv.txt"
    script = _argv_capturing_claude(tmp_path, argv_file)

    worker.run_issue(
        claude_cmd=[str(script)],
        model="claude-sonnet-4-6",
        prompt="THE-PROMPT",
        cwd=tmp_path,
        token_limit=None,
        time_limit_seconds=None,
        cost_limit_usd=None,
        passthrough=io.StringIO(),
    )

    argv = argv_file.read_text().splitlines()
    assert "--max-budget-usd" not in argv


def test_run_issue_passes_debug_file_flag(tmp_path: Path) -> None:
    """Opt-in debug capture hands the path to ``claude`` as ``--debug-file``,
    and the prompt stays the trailing positional after the value-taking flag."""
    argv_file = tmp_path / "argv.txt"
    script = _argv_capturing_claude(tmp_path, argv_file)
    debug_path = tmp_path / "run-ABA-1.debug.log"

    worker.run_issue(
        claude_cmd=[str(script)],
        model="claude-sonnet-4-6",
        prompt="THE-PROMPT",
        cwd=tmp_path,
        token_limit=None,
        time_limit_seconds=None,
        cost_limit_usd=None,
        passthrough=io.StringIO(),
        debug_file=debug_path,
    )

    argv = argv_file.read_text().splitlines()
    assert "--debug-file" in argv
    assert argv[argv.index("--debug-file") + 1] == str(debug_path)
    assert argv[-1] == "THE-PROMPT"


def test_run_issue_omits_debug_file_when_none(tmp_path: Path) -> None:
    """Debug capture off (the default) passes no ``--debug-file``."""
    argv_file = tmp_path / "argv.txt"
    script = _argv_capturing_claude(tmp_path, argv_file)

    worker.run_issue(
        claude_cmd=[str(script)],
        model="claude-sonnet-4-6",
        prompt="THE-PROMPT",
        cwd=tmp_path,
        token_limit=None,
        time_limit_seconds=None,
        cost_limit_usd=None,
        passthrough=io.StringIO(),
    )

    argv = argv_file.read_text().splitlines()
    assert "--debug-file" not in argv


# ---------------------------------------------------------------------------
# build_argv
# ---------------------------------------------------------------------------

def test_build_argv_orders_flags_with_prompt_trailing() -> None:
    """The shared argv builder keeps the base argv first, the stream flags and
    value-taking options in the middle, and the prompt as the trailing
    positional — the same shape the spawn path asserts via a real script."""
    argv = worker.build_argv(
        ["claude", "-p", "--dangerously-skip-permissions"],
        model="claude-opus-4-8",
        prompt="THE-PROMPT",
        cost_limit_usd=2.5,
        debug_file=Path("/tmp/run.debug.log"),
    )

    assert argv[:3] == ["claude", "-p", "--dangerously-skip-permissions"]
    assert "--verbose" in argv and "stream-json" in argv
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"
    assert argv[argv.index("--max-budget-usd") + 1] == "2.5"
    assert argv[argv.index("--debug-file") + 1] == "/tmp/run.debug.log"
    assert argv[-1] == "THE-PROMPT"


def test_build_argv_omits_optional_flags_when_none() -> None:
    """No cost cap and no debug file → neither flag appears."""
    argv = worker.build_argv(
        ["claude", "-p"],
        model="claude-sonnet-4-6",
        prompt="P",
        cost_limit_usd=None,
        debug_file=None,
    )

    assert "--max-budget-usd" not in argv
    assert "--debug-file" not in argv
    assert argv[-1] == "P"


# ---------------------------------------------------------------------------
# External-stream (watch) path
# ---------------------------------------------------------------------------

def test_external_stream_accumulates_usage_without_spawning(
    tmp_path: Path, monkeypatch
) -> None:
    """With ``external_stream`` set the worker reads the stream directly and
    never spawns a subprocess; usage is parsed with the same fidelity."""
    lines = [
        _assistant("msg_a", inp=10, out=4, cc=100, cr=0),
        _result(total_cost_usd=0.12, num_turns=1, session_id="s-ext", is_error=False),
    ]
    stream = io.StringIO("\n".join(lines) + "\n")

    def _no_popen(*args, **kwargs):
        raise AssertionError("subprocess.Popen must not run on the external path")

    monkeypatch.setattr(worker.subprocess, "Popen", _no_popen)

    result = worker.run_issue(
        claude_cmd=["unused"],
        model="claude-opus-4-8",
        prompt="ignored",
        cwd=tmp_path,
        token_limit=None,
        time_limit_seconds=None,
        cost_limit_usd=None,
        passthrough=io.StringIO(),
        external_stream=stream,
    )

    assert result.breach is None
    # No process → no observable return code; recorded as 0.
    assert result.exit_code == 0
    assert result.usage["cumulative"] == 114  # 10 + 4 + 100 + 0
    assert result.cost_usd == 0.12
    assert result.num_turns == 1
    assert result.session_id == "s-ext"


def test_external_stream_breach_calls_kill_fn(tmp_path: Path) -> None:
    """On the external path a time-cap breach calls ``kill_fn`` (which the
    orchestrator wires to killing the tmux pane) instead of a process-group
    kill — there is no process here to signal."""
    read_fd, write_fd = os.pipe()
    # Emit one turn, then leave the write end open so the reader blocks at the
    # FIFO equivalent and the time cap fires.
    os.write(write_fd, (_assistant("msg_a", inp=1, out=1, cc=0, cr=0) + "\n").encode())
    stream = os.fdopen(read_fd, "r", buffering=1)

    killed: list[bool] = []

    def kill_fn() -> None:
        killed.append(True)
        # Closing the writer is what the real pane kill achieves: the reader
        # then reaches EOF and the worker can finish.
        os.close(write_fd)

    result = worker.run_issue(
        claude_cmd=["unused"],
        model="claude-sonnet-4-6",
        prompt="ignored",
        cwd=tmp_path,
        token_limit=None,
        time_limit_seconds=0.3,
        cost_limit_usd=None,
        passthrough=io.StringIO(),
        external_stream=stream,
        kill_fn=kill_fn,
        poll_interval_seconds=0.05,
    )

    assert result.breach is not None
    assert result.breach.metric == "time"
    assert killed == [True]
    # The partial turn was still accounted before the breach.
    assert result.usage["cumulative"] == 2
    assert result.exit_code == 0
