"""Behavioral faults at the thin Hermes delivery boundary."""

import json
import time

import pytest

from tests.hermes_hook_support import load_plugin, make_context


def run(ctx):
    plugin = load_plugin()
    plugin.register(ctx)
    return ctx.hooks["pre_llm_call"](turn_id="fixture-turn")["context"]


def test_fetch_failure_is_explicit_and_stderr_is_not_returned(tmp_path):
    ctx, _ = make_context(
        tmp_path,
        "import sys\nsys.stderr.write('private-canary')\nraise SystemExit(1)\n",
    )
    context = run(ctx)
    assert "STATUS=missing" in context
    assert "delivery_nonzero" in context
    assert "private-canary" not in context


@pytest.mark.parametrize(
    "body,error",
    [
        ("import time\ntime.sleep(2)\n", "timeout"),
        ("print('x'*200000)\n", "output_limit"),
    ],
)
def test_child_wall_clock_and_output_are_bounded(tmp_path, body, error):
    ctx, _ = make_context(tmp_path, body)
    ctx.settings["timeout_ms"] = 100 if error == "timeout" else 1000
    if error == "output_limit":
        # Exercise pipe overflow independently of Python fixture initialization.
        # The real wall-clock test above retains its 100 ms deadline.
        from pathlib import Path

        Path(ctx.settings["executable"]).write_text(
            "#!/bin/sh\nprintf '%s' '" + "x" * 200000 + "'\n"
        )
    start = time.monotonic()
    context = run(ctx)
    assert "STATUS=missing" in context
    assert error in context
    assert time.monotonic() - start < 1.5


def test_nonzero_cannot_inject_even_a_prepared_frame(tmp_path):
    frame = {
        "schema": "universal-docs.context/v1",
        "status": "prepared",
        "context": "must-not-inject",
        "error": None,
    }
    ctx, _ = make_context(
        tmp_path, f"print({json.dumps(frame)!r})\nraise SystemExit(1)\n"
    )
    context = run(ctx)
    assert "STATUS=missing" in context
    assert "must-not-inject" not in context


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "[]",
        '{"schema":"universal-docs.context/v1","schema":"other"}',
        "[" * 2000 + "0" + "]" * 2000,
        json.dumps(
            {
                "schema": "universal-docs.context/v1",
                "status": "prepared",
                "context": "x" * 9000,
                "error": None,
            }
        ),
        json.dumps(
            {
                "schema": "universal-docs.context/v1",
                "status": "prepared",
                "context": "",
                "error": None,
            }
        ),
    ],
)
def test_invalid_frames_never_claim_preparation(raw):
    plugin = load_plugin()
    context = plugin._frame_context(raw.encode())["context"]
    assert "STATUS=missing" in context
    assert len(context.encode()) <= plugin.MAX_RETURN_BYTES


def test_prepared_stale_is_visible_not_relabelled_fresh():
    plugin = load_plugin()
    raw = json.dumps(
        {
            "schema": "universal-docs.context/v1",
            "status": "prepared_stale",
            "context": "fixture stale data",
            "error": None,
        }
    )
    context = plugin._frame_context(raw.encode())["context"]
    assert "STATUS=prepared_stale" in context
    assert "fixture stale data" in context


def test_second_turn_runs_again_and_missing_identity_does_not(tmp_path):
    plugin = load_plugin()
    ctx, log = make_context(tmp_path)
    plugin.register(ctx)
    callback = ctx.hooks["pre_llm_call"]
    assert "missing_turn_identity" in callback()["context"]
    callback(turn_id="first")
    callback(turn_id="second")
    assert len(log.read_text().splitlines()) == 2


def test_unverifiable_cleanup_reports_unknown_instead_of_throwing(
    tmp_path, monkeypatch
):
    plugin = load_plugin()
    ctx, _ = make_context(tmp_path)
    plugin.register(ctx)

    def denied(*args):
        raise PermissionError("fixture permission denial")

    monkeypatch.setattr(plugin.os, "killpg", denied)
    context = ctx.hooks["pre_llm_call"](turn_id="cleanup-fixture")["context"]
    assert "STATUS=missing" in context
    assert "cleanup_unverified" in context
    assert "fixture permission denial" not in context
