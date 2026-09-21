"""Shared session activity observation contract: timestamp + bounded description/provenance, observation
only (notification, timeout, kill and retry policy live elsewhere). Provenance is a small closed enum of
*noun* sources; the default agent clock stamps ``unknown`` unless a writer passes ``provenance=``."""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager, suppress
from enum import Enum
from typing import Any, Mapping, Optional, TypedDict

ACTIVITY_DESCRIPTION_MAX = 120
AGENT_ACTIVITY_SCHEMA_VERSION = "agent_activity/v1"
AGENT_ACTIVITY_COUNT_MAX = 2_147_483_647

# A snapshot may name a tool, but never carries its arguments, payload, environment,
# or result. Unknown/dynamic names collapse to ``other`` rather than becoming a
# side-channel for remote server names or user-provided tool labels.
_AGENT_ACTIVITY_TOOL_ALLOWLIST = frozenset({
    "terminal", "read_file", "write_file", "patch", "web_search", "web_extract",
    "browser", "execute_code", "exec_command", "apply_patch", "delegate_task",
})
_AGENT_ACTIVITY_WAITING_FOR_ALLOWLIST = frozenset({"provider", "tool", "approval", "input", "codex"})
_AGENT_ACTIVITY_THREAD_STATUS_ALLOWLIST = frozenset({"active", "idle", "notLoaded", "systemError"})


class AgentActivityTokens(TypedDict):
    input: int
    output: int
    total: int


class AgentActivityLimits(TypedDict):
    iterations_used: int
    iterations_max: int | None
    context_window: int | None


class AgentActivitySnapshotV1(TypedDict):
    schema_version: str
    runtime: str
    tool: str | None
    waiting_for: str | None
    thread_status: str | None
    error: str | None
    tokens: AgentActivityTokens
    limits: AgentActivityLimits

# Durable SessionDB heartbeat cadence. Contract: MUST stay >= 30s — the SessionDB write path is contended and
# this observation-only projection never justifies extra write pressure. A code constant on purpose (no config
# can make it a high-frequency writer); matches the kanban auto-heartbeat. force_persist (terminal stamps) is the only bypass.
SESSION_ACTIVITY_HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0


class ActivityProvenance(str, Enum):
    """Where a durable/in-memory activity stamp came from."""

    UNKNOWN = "unknown"
    # Compression writers: heartbeat, host timeout, cooldown, turn hold.
    # See #72424.
    AGENT_COMPRESSION = "agent.compression"
    AGENT_COMPRESSION_TIMEOUT = "agent.compression_timeout"
    AGENT_COMPRESSION_COOLDOWN = "agent.compression_cooldown"
    AGENT_COMPRESSION_TURNHOLD = "agent.compression_turnhold"


def bound_activity_description(description: Optional[str]) -> str:
    """Clamp free-form activity text to the shared description budget."""
    text = (description or "").strip()
    return text if len(text) <= ACTIVITY_DESCRIPTION_MAX else text[: ACTIVITY_DESCRIPTION_MAX - 1] + "…"


def normalize_activity_provenance(provenance: Optional[ActivityProvenance | str]) -> ActivityProvenance:
    """Return a known provenance, or ``UNKNOWN`` when unset/unrecognized."""
    if isinstance(provenance, ActivityProvenance):
        return provenance
    try:
        return ActivityProvenance((provenance or "").strip())
    except ValueError:
        return ActivityProvenance.UNKNOWN


def _bounded_activity_count(value: Any, *, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    try:
        return min(AGENT_ACTIVITY_COUNT_MAX, max(0, int(value)))
    except (TypeError, ValueError, OverflowError):
        return None if allow_none else 0


def _safe_activity_tool(value: Any) -> str | None:
    if value is None:
        return None
    return str(value) if str(value) in _AGENT_ACTIVITY_TOOL_ALLOWLIST else "other"


def _safe_activity_waiting_for(value: Any) -> str | None:
    if value is None:
        return None
    return str(value) if str(value) in _AGENT_ACTIVITY_WAITING_FOR_ALLOWLIST else None


def _safe_activity_thread_status(value: Any) -> str | None:
    if value is None:
        return None
    return str(value) if str(value) in _AGENT_ACTIVITY_THREAD_STATUS_ALLOWLIST else None


def activity_error_kind(error: Any) -> str | None:
    """Classify an error without returning its text, payload, or exception args."""
    if error is None or error == "":
        return None
    kind = (error if isinstance(error, str) else type(error).__name__).lower()
    if "oauth" in kind:
        return "oauth_error"
    if "auth" in kind or "permission" in kind:
        return "auth_error"
    if "timeout" in kind:
        return "timeout"
    if "rate" in kind or "quota" in kind:
        return "rate_limit"
    if "tool" in kind:
        return "tool_error"
    return "runtime_error"


def infer_activity_waiting_for(description: Any) -> str | None:
    """Map existing activity labels to the closed waiting vocabulary without exporting their text."""
    text = str(description or "").lower()
    if "waiting" in text or "receiving stream" in text or "local model loading" in text:
        return "provider"
    return None


@contextmanager
def activity_waiting_for(agent: Any, waiting_for: str):
    """Set the observation-only wait state and clear it unconditionally at the turn boundary."""
    agent._activity_waiting_for = _safe_activity_waiting_for(waiting_for)
    try:
        yield
    finally:
        agent._activity_waiting_for = None


def normalize_agent_activity_snapshot(
    *, runtime: Any, tool: Any = None, waiting_for: Any = None, thread_status: Any = None, error: Any = None,
    tokens: Mapping[str, Any] | None = None, limits: Mapping[str, Any] | None = None,
    **_ignored_payload: Any,
) -> AgentActivitySnapshotV1:
    """Return the sole, bounded, payload-free ``agent_activity/v1`` projection."""
    token_values = tokens if isinstance(tokens, Mapping) else {}
    limit_values = limits if isinstance(limits, Mapping) else {}
    raw_iterations_max = limit_values.get("iterations_max")
    try:
        iterations_max = None if int(raw_iterations_max) >= sys.maxsize else _bounded_activity_count(
            raw_iterations_max, allow_none=True
        )
    except (TypeError, ValueError, OverflowError):
        iterations_max = _bounded_activity_count(raw_iterations_max, allow_none=True)
    return {
        "schema_version": AGENT_ACTIVITY_SCHEMA_VERSION,
        "runtime": "codex_app_server" if runtime == "codex_app_server" else "hermes",
        "tool": _safe_activity_tool(tool),
        "waiting_for": _safe_activity_waiting_for(waiting_for),
        "thread_status": _safe_activity_thread_status(thread_status),
        "error": activity_error_kind(error),
        "tokens": {
            "input": _bounded_activity_count(token_values.get("input")) or 0,
            "output": _bounded_activity_count(token_values.get("output")) or 0,
            "total": _bounded_activity_count(token_values.get("total")) or 0,
        },
        "limits": {
            "iterations_used": _bounded_activity_count(limit_values.get("iterations_used")) or 0,
            "iterations_max": iterations_max,
            "context_window": _bounded_activity_count(limit_values.get("context_window"), allow_none=True),
        },
    }


def format_iteration_progress(api_call_count: Any, max_iterations: Any) -> str:
    """``iteration N/M`` for user-facing status lines, or ``iteration N`` when the cap is unbounded.

    ``AIAgent.max_iterations`` defaults to ``sys.maxsize`` (unlimited), so printing the pair verbatim
    shows ``iteration 3/9223372036854775807`` in busy acks, heartbeats and timeout diagnostics (#102806).
    """
    try:
        cap = int(max_iterations)
    except (TypeError, ValueError):
        cap = sys.maxsize
    if cap >= sys.maxsize:
        return f"iteration {api_call_count}"
    return f"iteration {api_call_count}/{cap}"


def reset_session_activity_persist_window(agent: Any) -> None:
    """Clear the persist rate-limit so the next stamp writes through (terminal compression labels must not stick on mid-compress text)."""
    with suppress(Exception):
        agent._session_activity_last_persist_mono = 0.0


def build_activity_snapshot(
    *,
    last_activity_at: Optional[float],
    last_activity_description: Optional[str],
    last_activity_provenance: Optional[ActivityProvenance | str] = None,
    now: Optional[float] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build the shared activity snapshot (plus optional caller extras)."""
    when = float(last_activity_at) if last_activity_at is not None else None
    clock = float(now if now is not None else time.time())
    desc = bound_activity_description(last_activity_description)
    prov = normalize_activity_provenance(last_activity_provenance).value
    return {
        "last_activity_at": when,
        "last_activity_description": desc,
        "last_activity_provenance": prov,
        "seconds_since_activity": round(clock - when, 1) if when is not None else None,
        # Short aliases used by existing gateway/delegate readers.
        "last_activity_ts": when, "last_activity_desc": desc, "description": desc, "provenance": prov,
        **(extra or {}),
    }
