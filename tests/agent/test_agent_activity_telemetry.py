"""Contract tests for the bounded, payload-free agent_activity/v1 snapshot."""

import sys
from types import SimpleNamespace

import pytest

from agent.activity_tracking import build_standard_agent_activity_snapshot
from agent.codex_runtime import build_codex_app_server_activity_snapshot
from agent.session_activity import (
    AGENT_ACTIVITY_COUNT_MAX,
    AGENT_ACTIVITY_SCHEMA_VERSION,
    activity_waiting_for,
    normalize_agent_activity_snapshot,
)


def test_normalizer_emits_bounded_redacted_contract():
    snapshot = normalize_agent_activity_snapshot(
        runtime="hermes",
        tool="mcp.private.secret_tool",
        waiting_for="provider",
        error="RuntimeError: Bearer super-secret-token",
        tokens={"input": -1, "output": AGENT_ACTIVITY_COUNT_MAX + 1, "total": 7},
        limits={"iterations_used": 3, "iterations_max": None, "context_window": "200000"},
        ignored_payload={"authorization": "Bearer super-secret-token"},
    )

    assert snapshot == {
        "schema_version": AGENT_ACTIVITY_SCHEMA_VERSION,
        "runtime": "hermes",
        "tool": "other",
        "waiting_for": "provider",
        "error": "runtime_error",
        "tokens": {"input": 0, "output": AGENT_ACTIVITY_COUNT_MAX, "total": 7},
        "limits": {"iterations_used": 3, "iterations_max": None, "context_window": 200000},
    }
    assert "super-secret-token" not in repr(snapshot)
    assert "authorization" not in snapshot


def test_standard_adapter_reads_existing_activity_without_payloads():
    agent = SimpleNamespace(
        _current_tool="terminal",
        _last_activity_desc="waiting for provider response (streaming)",
        _last_activity_error=None,
        session_input_tokens=11,
        session_output_tokens=7,
        session_total_tokens=18,
        _api_call_count=2,
        max_iterations=5,
        context_compressor=SimpleNamespace(context_length=128000),
    )

    snapshot = build_standard_agent_activity_snapshot(agent)

    assert snapshot == {
        "schema_version": "agent_activity/v1",
        "runtime": "hermes",
        "tool": "terminal",
        "waiting_for": "provider",
        "error": None,
        "tokens": {"input": 11, "output": 7, "total": 18},
        "limits": {"iterations_used": 2, "iterations_max": 5, "context_window": 128000},
    }


def test_normalizer_hides_unbounded_iteration_limit():
    snapshot = normalize_agent_activity_snapshot(
        runtime="hermes",
        limits={"iterations_max": sys.maxsize},
    )

    assert snapshot["limits"]["iterations_max"] is None


def test_standard_adapter_classifies_existing_tool_error_label():
    agent = SimpleNamespace(
        _current_tool=None,
        _last_activity_desc="tool completed: terminal (1.0s) (error)",
        _last_activity_error=None,
        session_input_tokens=0,
        session_output_tokens=0,
        session_total_tokens=0,
        _api_call_count=1,
        max_iterations=2,
        context_compressor=SimpleNamespace(context_length=None),
    )

    assert build_standard_agent_activity_snapshot(agent)["error"] == "tool_error"


def test_codex_adapter_uses_the_same_contract_and_token_usage():
    agent = SimpleNamespace(
        _current_tool=None,
        _last_activity_desc="",
        _last_activity_error=None,
        _activity_waiting_for="codex",
        _api_call_count=1,
        max_iterations=4,
        context_compressor=SimpleNamespace(context_length=200000),
    )
    turn = SimpleNamespace(
        token_usage_last={"inputTokens": 80, "outputTokens": 25, "totalTokens": 130},
        error="OAuthError: token=should-not-leak",
    )

    snapshot = build_codex_app_server_activity_snapshot(agent, turn=turn)

    assert snapshot == {
        "schema_version": "agent_activity/v1",
        "runtime": "codex_app_server",
        "tool": None,
        "waiting_for": "codex",
        "error": "oauth_error",
        "tokens": {"input": 80, "output": 25, "total": 130},
        "limits": {"iterations_used": 1, "iterations_max": 4, "context_window": 200000},
    }
    assert "should-not-leak" not in repr(snapshot)


def test_waiting_for_is_always_cleared_in_finally():
    agent = SimpleNamespace()

    with pytest.raises(RuntimeError, match="boom"):
        with activity_waiting_for(agent, "codex"):
            assert agent._activity_waiting_for == "codex"
            raise RuntimeError("boom")

    assert agent._activity_waiting_for is None
