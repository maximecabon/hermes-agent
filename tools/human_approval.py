#!/usr/bin/env python3
"""Durable, cross-profile human approval gate.

The tool is deliberately separate from Hermes' command/edit approvals and from
``clarify``.  Requests live in one SQLite database under the shared Hermes root,
so a Telegram gateway and agents running in different profile processes observe
the same state.  Every non-approved terminal state is fail-closed.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_constants import get_default_hermes_root


TERMINAL_STATES = frozenset(
    {
        "approved",
        "refused",
        "expired",
        "delivery_failed",
        "telegram_unavailable",
        "cancelled",
    }
)
_DELIVERED_STATES = frozenset({"delivered", "same_as_telegram", "deferred_to_result"})
_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CALLBACK_RE = re.compile(r"^ha:([0-9a-f]{32}):([ar])$")
_MAX_TEXT = {
    "summary": 1000,
    "requested_action": 3000,
    "reason": 2000,
    "evidence": 1000,
}
_MAX_EVIDENCE = 12


class IdempotencyConflict(RuntimeError):
    """The same caller key was reused for a different requested action."""


@dataclass(frozen=True)
class HumanApprovalRecord:
    request_id: str
    requester_profile: str
    requester_session_id: str
    requester_session_key: str
    requester_turn_id: str
    tool_call_id: str
    origin_platform: str
    origin_chat_id: str
    origin_thread_id: Optional[str]
    summary: str
    requested_action: str
    reason: str
    evidence: tuple[str, ...]
    state: str
    origin_delivery: str
    telegram_delivery: str
    telegram_chat_id: Optional[str]
    telegram_thread_id: Optional[str]
    origin_message_id: Optional[str]
    telegram_message_id: Optional[str]
    created_at: float
    expires_at: float
    decided_at: Optional[float]
    decision_channel: Optional[str]
    decided_by: Optional[str]
    failure_code: Optional[str]


@dataclass(frozen=True)
class ResolutionResult:
    changed: bool
    state: str
    reason: str


def _optional(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _clean_text(value: Any, *, field: str) -> str:
    text = "" if value is None else str(value).strip()
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(text, force=True)
    except Exception as exc:
        raise ValueError("approval payload redaction failed") from exc
    return text[: _MAX_TEXT[field]]


def _clean_evidence(values: Optional[Iterable[Any]]) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        values = [values]
    cleaned = []
    for value in list(values)[:_MAX_EVIDENCE]:
        item = _clean_text(value, field="evidence")
        if item:
            cleaned.append(item)
    return tuple(cleaned)


def _payload_hash(
    summary: str,
    requested_action: str,
    reason: str,
    evidence: tuple[str, ...],
) -> str:
    payload = json.dumps(
        {
            "summary": summary,
            "requested_action": requested_action,
            "reason": reason,
            "evidence": evidence,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_record(row: sqlite3.Row) -> HumanApprovalRecord:
    try:
        evidence = tuple(json.loads(row["evidence_json"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        evidence = ()
    return HumanApprovalRecord(
        request_id=row["request_id"],
        requester_profile=row["requester_profile"],
        requester_session_id=row["requester_session_id"],
        requester_session_key=row["requester_session_key"],
        requester_turn_id=row["requester_turn_id"],
        tool_call_id=row["tool_call_id"],
        origin_platform=row["origin_platform"],
        origin_chat_id=row["origin_chat_id"],
        origin_thread_id=_optional(row["origin_thread_id"]),
        summary=row["summary"],
        requested_action=row["requested_action"],
        reason=row["reason"],
        evidence=evidence,
        state=row["state"],
        origin_delivery=row["origin_delivery"],
        telegram_delivery=row["telegram_delivery"],
        telegram_chat_id=_optional(row["telegram_chat_id"]),
        telegram_thread_id=_optional(row["telegram_thread_id"]),
        origin_message_id=_optional(row["origin_message_id"]),
        telegram_message_id=_optional(row["telegram_message_id"]),
        created_at=float(row["created_at"]),
        expires_at=float(row["expires_at"]),
        decided_at=float(row["decided_at"]) if row["decided_at"] is not None else None,
        decision_channel=_optional(row["decision_channel"]),
        decided_by=_optional(row["decided_by"]),
        failure_code=_optional(row["failure_code"]),
    )


class HumanApprovalStore:
    """Small SQLite state machine shared by agent and gateway processes."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _initialize(self) -> None:
        last_error: Optional[sqlite3.OperationalError] = None
        for attempt in range(5):
            try:
                self._initialize_once()
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                last_error = exc
                time.sleep(0.05 * (attempt + 1))
        if last_error is not None:
            raise last_error

    def _initialize_once(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS human_approval_requests (
                    request_id TEXT PRIMARY KEY,
                    idempotency_hash TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    requester_profile TEXT NOT NULL,
                    requester_session_id TEXT NOT NULL,
                    requester_session_key TEXT NOT NULL,
                    requester_turn_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    origin_platform TEXT NOT NULL,
                    origin_chat_id TEXT NOT NULL,
                    origin_thread_id TEXT,
                    summary TEXT NOT NULL,
                    requested_action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    origin_delivery TEXT NOT NULL DEFAULT 'pending',
                    telegram_delivery TEXT NOT NULL DEFAULT 'pending',
                    telegram_chat_id TEXT,
                    telegram_thread_id TEXT,
                    origin_message_id TEXT,
                    telegram_message_id TEXT,
                    delivery_owner TEXT,
                    delivery_claimed_at REAL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    decided_at REAL,
                    decision_channel TEXT,
                    decided_by TEXT,
                    failure_code TEXT,
                    UNIQUE (
                        requester_profile,
                        requester_session_id,
                        idempotency_hash
                    )
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_human_approval_delivery
                ON human_approval_requests (
                    state, telegram_delivery, delivery_claimed_at, created_at
                )
                """
            )

    @staticmethod
    def _expire_due_conn(conn: sqlite3.Connection, now: float) -> int:
        cursor = conn.execute(
            """
            UPDATE human_approval_requests
               SET state = 'expired',
                   decided_at = ?,
                   failure_code = 'timeout',
                   delivery_owner = NULL,
                   delivery_claimed_at = NULL
             WHERE state = 'pending' AND expires_at <= ?
            """,
            (now, now),
        )
        return int(cursor.rowcount)

    def create_request(
        self,
        *,
        summary: str,
        requested_action: str,
        reason: str,
        evidence: Optional[Iterable[Any]],
        expires_in: int,
        idempotency_key: Optional[str],
        requester_profile: str,
        requester_session_id: str,
        requester_session_key: str,
        requester_turn_id: str,
        tool_call_id: str,
        origin_platform: str,
        origin_chat_id: str,
        origin_thread_id: Optional[str],
        now: Optional[float] = None,
    ) -> HumanApprovalRecord:
        current = time.time() if now is None else float(now)
        timeout = int(expires_in)
        if timeout <= 0:
            raise ValueError("expires_in must be positive")

        clean_summary = _clean_text(summary, field="summary")
        clean_action = _clean_text(requested_action, field="requested_action")
        clean_reason = _clean_text(reason, field="reason")
        clean_evidence = _clean_evidence(evidence)
        if not clean_summary or not clean_action or not clean_reason:
            raise ValueError("summary, requested_action, and reason are required")

        profile = str(requester_profile or "default").strip() or "default"
        session_id = str(requester_session_id or "").strip()
        if not session_id:
            raise ValueError("requester session correlation is required")
        caller_key = str(idempotency_key or secrets.token_hex(16)).strip()
        if not caller_key:
            caller_key = secrets.token_hex(16)
        idempotency_hash = _key_hash(caller_key)
        payload_hash = _payload_hash(
            clean_summary, clean_action, clean_reason, clean_evidence
        )
        request_id = secrets.token_hex(16)

        values = (
            request_id,
            idempotency_hash,
            payload_hash,
            profile[:100],
            session_id[:500],
            str(requester_session_key or "")[:1000],
            str(requester_turn_id or "")[:200],
            str(tool_call_id or "")[:200],
            str(origin_platform or "local").strip().lower()[:100],
            str(origin_chat_id or "")[:500],
            _optional(origin_thread_id),
            clean_summary,
            clean_action,
            clean_reason,
            json.dumps(clean_evidence, ensure_ascii=False),
            current,
            current + timeout,
        )

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR IGNORE INTO human_approval_requests (
                    request_id, idempotency_hash, payload_hash,
                    requester_profile, requester_session_id,
                    requester_session_key, requester_turn_id, tool_call_id,
                    origin_platform, origin_chat_id, origin_thread_id,
                    summary, requested_action, reason, evidence_json,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = conn.execute(
                """
                SELECT * FROM human_approval_requests
                 WHERE requester_profile = ?
                   AND requester_session_id = ?
                   AND idempotency_hash = ?
                """,
                (profile[:100], session_id[:500], idempotency_hash),
            ).fetchone()
            if row is None:  # pragma: no cover - SQLite invariant
                raise RuntimeError("approval request was not persisted")
            if row["payload_hash"] != payload_hash:
                raise IdempotencyConflict(
                    "idempotency key already belongs to another requested action"
                )
            return _as_record(row)

    def get_request(
        self, request_id: str, *, now: Optional[float] = None
    ) -> Optional[HumanApprovalRecord]:
        current = time.time() if now is None else float(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_due_conn(conn, current)
            row = conn.execute(
                "SELECT * FROM human_approval_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return _as_record(row) if row is not None else None

    def expire_due(self, *, now: Optional[float] = None) -> int:
        current = time.time() if now is None else float(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._expire_due_conn(conn, current)

    def claim_next_delivery(
        self,
        *,
        owner: str,
        telegram_chat_id: str,
        telegram_thread_id: Optional[str],
        lease_seconds: float,
        now: Optional[float] = None,
        request_id: Optional[str] = None,
    ) -> Optional[HumanApprovalRecord]:
        current = time.time() if now is None else float(now)
        lease_before = current - max(float(lease_seconds), 1.0)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_due_conn(conn, current)
            params: list[Any] = [lease_before, current]
            request_clause = ""
            if request_id is not None:
                request_clause = " AND request_id = ?"
                params.append(request_id)
            row = conn.execute(
                f"""
                SELECT * FROM human_approval_requests
                 WHERE state = 'pending'
                   AND telegram_delivery = 'pending'
                   AND (delivery_owner IS NULL OR delivery_claimed_at <= ?)
                   AND expires_at > ?
                   {request_clause}
                 ORDER BY created_at, request_id
                 LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                """
                UPDATE human_approval_requests
                   SET delivery_owner = ?,
                       delivery_claimed_at = ?,
                       telegram_chat_id = ?,
                       telegram_thread_id = ?
                 WHERE request_id = ?
                   AND state = 'pending'
                   AND telegram_delivery = 'pending'
                """,
                (
                    str(owner)[:200],
                    current,
                    str(telegram_chat_id or "")[:500],
                    _optional(telegram_thread_id),
                    row["request_id"],
                ),
            )
            if cursor.rowcount != 1:
                return None
            claimed = conn.execute(
                "SELECT * FROM human_approval_requests WHERE request_id = ?",
                (row["request_id"],),
            ).fetchone()
            return _as_record(claimed)

    def complete_delivery(
        self,
        request_id: str,
        *,
        owner: str,
        origin_delivery: str,
        telegram_delivery: str,
        origin_message_id: Optional[str] = None,
        telegram_message_id: Optional[str] = None,
        failure_state: Optional[str] = None,
        failure_code: Optional[str] = None,
        now: Optional[float] = None,
    ) -> bool:
        if failure_state not in {None, "delivery_failed", "telegram_unavailable"}:
            raise ValueError("invalid delivery failure state")
        current = time.time() if now is None else float(now)
        next_state = failure_state or "pending"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_due_conn(conn, current)
            cursor = conn.execute(
                """
                UPDATE human_approval_requests
                   SET state = ?,
                       origin_delivery = ?,
                       telegram_delivery = ?,
                       origin_message_id = ?,
                       telegram_message_id = ?,
                       decided_at = CASE WHEN ? = 'pending' THEN decided_at ELSE ? END,
                       failure_code = ?,
                       delivery_owner = NULL,
                       delivery_claimed_at = NULL
                 WHERE request_id = ?
                   AND state = 'pending'
                   AND delivery_owner = ?
                """,
                (
                    next_state,
                    str(origin_delivery)[:100],
                    str(telegram_delivery)[:100],
                    _optional(origin_message_id),
                    _optional(telegram_message_id),
                    next_state,
                    current,
                    _optional(failure_code) or (failure_state if failure_state else None),
                    request_id,
                    str(owner)[:200],
                ),
            )
            return cursor.rowcount == 1

    def mark_local_origin_delivered(
        self, request_id: str, *, now: Optional[float] = None
    ) -> bool:
        current = time.time() if now is None else float(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_due_conn(conn, current)
            cursor = conn.execute(
                """
                UPDATE human_approval_requests
                   SET origin_delivery = 'delivered'
                 WHERE request_id = ?
                   AND state = 'pending'
                   AND origin_delivery = 'pending'
                """,
                (request_id,),
            )
            return cursor.rowcount == 1

    def cancel_request(
        self, request_id: str, *, failure_code: str = "interrupted"
    ) -> bool:
        current = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE human_approval_requests
                   SET state = 'cancelled', decided_at = ?, failure_code = ?,
                       delivery_owner = NULL, delivery_claimed_at = NULL
                 WHERE request_id = ? AND state = 'pending'
                """,
                (current, str(failure_code)[:100], request_id),
            )
            return cursor.rowcount == 1

    def resolve_request(
        self,
        request_id: str,
        *,
        decision: str,
        decided_by: str,
        telegram_chat_id: str,
        telegram_thread_id: Optional[str],
        now: Optional[float] = None,
    ) -> ResolutionResult:
        current = time.time() if now is None else float(now)
        normalized_decision = str(decision).strip().lower()
        if normalized_decision not in {"approved", "refused"}:
            return ResolutionResult(False, "unknown", "invalid_decision")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_due_conn(conn, current)
            row = conn.execute(
                "SELECT * FROM human_approval_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                return ResolutionResult(False, "unknown", "unknown_request")
            if row["state"] != "pending":
                return ResolutionResult(False, row["state"], "already_terminal")

            expected_chat = str(row["telegram_chat_id"] or "")
            actual_chat = str(telegram_chat_id or "")
            expected_thread = str(row["telegram_thread_id"] or "")
            actual_thread = str(telegram_thread_id or "")
            if expected_chat != actual_chat or expected_thread != actual_thread:
                return ResolutionResult(False, "pending", "unauthorized_chat")
            if (
                row["origin_delivery"] not in _DELIVERED_STATES
                or row["telegram_delivery"] not in _DELIVERED_STATES
            ):
                return ResolutionResult(False, "pending", "not_delivered")

            cursor = conn.execute(
                """
                UPDATE human_approval_requests
                   SET state = ?,
                       decided_at = ?,
                       decision_channel = 'telegram',
                       decided_by = ?,
                       delivery_owner = NULL,
                       delivery_claimed_at = NULL
                 WHERE request_id = ? AND state = 'pending'
                """,
                (
                    normalized_decision,
                    current,
                    _clean_text(decided_by, field="summary")[:200],
                    request_id,
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - transaction serializes writers
                latest = conn.execute(
                    "SELECT state FROM human_approval_requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                state = latest["state"] if latest else "unknown"
                return ResolutionResult(False, state, "already_terminal")
            return ResolutionResult(True, normalized_decision, "resolved")


def get_shared_store() -> HumanApprovalStore:
    """Return a store rooted above profile homes for cross-process visibility."""
    return HumanApprovalStore(
        get_default_hermes_root() / "approvals" / "human_approvals.db"
    )


def telegram_button_specs(request_id: str) -> list[tuple[str, str]]:
    if not _REQUEST_ID_RE.fullmatch(str(request_id)):
        raise ValueError("invalid approval request id")
    return [
        ("Approuver", f"ha:{request_id}:a"),
        ("Refuser", f"ha:{request_id}:r"),
    ]


def parse_callback_data(value: str) -> Optional[tuple[str, str]]:
    match = _CALLBACK_RE.fullmatch(str(value or ""))
    if match is None:
        return None
    return match.group(1), "approved" if match.group(2) == "a" else "refused"


def format_telegram_gate(record: HumanApprovalRecord) -> str:
    evidence = ""
    if record.evidence:
        evidence = "\n".join(f"• {html.escape(item)}" for item in record.evidence)
        evidence = f"\n\n<b>Preuves :</b>\n{evidence}"
    expiry = datetime.fromtimestamp(record.expires_at, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    return (
        "🛑 <b>Approbation humaine requise</b>\n\n"
        f"<b>Résumé :</b> {html.escape(record.summary)}\n\n"
        f"<b>Action demandée :</b> {html.escape(record.requested_action)}\n\n"
        f"<b>Motif :</b> {html.escape(record.reason)}"
        f"{evidence}\n\n"
        f"<b>Expire :</b> {expiry}\n"
        f"<code>{record.request_id}</code>"
    )


def format_origin_notice(record: HumanApprovalRecord) -> str:
    return (
        "Human approval requested; the correlated gate was also sent to Telegram.\n"
        f"Request: {record.request_id}\n"
        f"Summary: {record.summary}\n"
        f"Requested action: {record.requested_action}\n"
        "Only an explicit Telegram approval authorizes that exact action."
    )


def _current_context(*, session_id: Optional[str] = None) -> dict[str, str]:
    from gateway.session_context import get_session_env

    profile = get_session_env("HERMES_SESSION_PROFILE", "").strip()
    if not profile:
        profile = os.getenv("HERMES_PROFILE", "").strip()
    if not profile:
        home = Path(os.getenv("HERMES_HOME", ""))
        if home.parent.name == "profiles":
            profile = home.name
    profile = profile or "default"

    try:
        from tools.approval import (
            get_current_observability_context,
            get_current_session_key,
        )

        turn_id, tool_call_id = get_current_observability_context()
        session_key = get_current_session_key("")
    except Exception:
        turn_id, tool_call_id, session_key = "", "", ""

    resolved_session = (
        str(session_id or "").strip()
        or get_session_env("HERMES_SESSION_ID", "").strip()
        or session_key
        or f"local-process-{os.getpid()}"
    )
    return {
        "requester_profile": profile,
        "requester_session_id": resolved_session,
        "requester_session_key": session_key,
        "requester_turn_id": turn_id,
        "tool_call_id": tool_call_id,
        "origin_platform": (
            get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
            or "local"
        ),
        "origin_chat_id": get_session_env("HERMES_SESSION_CHAT_ID", "").strip(),
        "origin_thread_id": get_session_env(
            "HERMES_SESSION_THREAD_ID", ""
        ).strip(),
    }


def _approval_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import cfg_get

        value = cfg_get("gateway.human_approval", {})
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def wait_for_terminal(
    request_id: str,
    *,
    store: Optional[HumanApprovalStore] = None,
    poll_interval: Optional[float] = None,
) -> HumanApprovalRecord:
    active_store = store or get_shared_store()
    config = _approval_config()
    interval = poll_interval
    if interval is None:
        try:
            interval = float(config.get("poll_interval_seconds", 0.25))
        except (TypeError, ValueError):
            interval = 0.25
    interval = min(max(float(interval), 0.05), 2.0)

    from tools.interrupt import is_interrupted

    while True:
        record = active_store.get_request(request_id)
        if record is None:
            raise RuntimeError("approval request disappeared")
        if record.state in TERMINAL_STATES:
            return record
        if is_interrupted():
            active_store.cancel_request(request_id)
        time.sleep(interval)


def _result_payload(record: HumanApprovalRecord) -> dict[str, Any]:
    if record.state == "approved":
        next_action = "proceed_with_requested_action_only"
    elif record.state == "refused":
        next_action = "stop"
    else:
        next_action = "stop_and_retry_gate"
    decided_at = None
    if record.decided_at is not None:
        decided_at = datetime.fromtimestamp(
            record.decided_at, tz=timezone.utc
        ).isoformat()
    return {
        "status": record.state,
        "request_id": record.request_id,
        "requested_action": record.requested_action,
        "requester_profile": record.requester_profile,
        "requester_session_id": record.requester_session_id,
        "origin_delivery": record.origin_delivery,
        "telegram_delivery": record.telegram_delivery,
        "decision_channel": record.decision_channel,
        "decided_at": decided_at,
        "failure_code": record.failure_code,
        "next_action": next_action,
        "scope": "Approval applies only to requested_action exactly as shown.",
    }


def request_human_approval_tool(
    *,
    summary: str,
    requested_action: str,
    reason: str,
    evidence: Optional[list[str]] = None,
    expires_in: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    session_id: Optional[str] = None,
    **_: Any,
) -> str:
    """Persist, deliver, and synchronously await an explicit human decision."""
    config = _approval_config()
    try:
        max_timeout = int(config.get("max_timeout_seconds", 86400))
    except (TypeError, ValueError):
        max_timeout = 86400
    try:
        configured_default = int(config.get("default_timeout_seconds", 3600))
    except (TypeError, ValueError):
        configured_default = 3600
    try:
        timeout = int(configured_default if expires_in is None else expires_in)
    except (TypeError, ValueError):
        timeout = 0
    if timeout <= 0 or timeout > max(max_timeout, 1):
        return json.dumps(
            {
                "status": "delivery_failed",
                "error": "expires_in is outside the configured safe range",
                "next_action": "stop_and_retry_gate",
            },
            ensure_ascii=False,
        )

    try:
        context = _current_context(session_id=session_id)
        store = get_shared_store()
        record = store.create_request(
            summary=summary,
            requested_action=requested_action,
            reason=reason,
            evidence=evidence,
            expires_in=timeout,
            idempotency_key=idempotency_key,
            **context,
        )

        if record.origin_platform in {"local", "cli", "tui", "desktop"}:
            if record.state == "pending" and record.origin_delivery == "pending":
                print(format_origin_notice(record), flush=True)
                store.mark_local_origin_delivered(record.request_id)

        terminal = wait_for_terminal(record.request_id, store=store)
        return json.dumps(_result_payload(terminal), ensure_ascii=False)
    except IdempotencyConflict:
        return json.dumps(
            {
                "status": "delivery_failed",
                "error": "idempotency key is already bound to another action",
                "next_action": "stop_and_retry_gate",
            },
            ensure_ascii=False,
        )
    except (KeyboardInterrupt, SystemExit):
        try:
            store.cancel_request(record.request_id)  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return json.dumps(
            {
                "status": "cancelled",
                "next_action": "stop_and_retry_gate",
            },
            ensure_ascii=False,
        )
    except Exception:
        return json.dumps(
            {
                "status": "delivery_failed",
                "error": "human approval gate failed closed",
                "next_action": "stop_and_retry_gate",
            },
            ensure_ascii=False,
        )


HUMAN_APPROVAL_SCHEMA = {
    "name": "request_human_approval",
    "description": (
        "Block until an explicitly authorized human approves or refuses one "
        "precisely described action. The request is mirrored to the origin "
        "channel and to the configured Telegram approval chat. Only status "
        "'approved' authorizes requested_action; refusal, timeout, silence, "
        "restart, callback mismatch, or delivery failure are fail-closed. "
        "Reuse idempotency_key when retrying the same request. Do not use this "
        "for ordinary clarification or technical verification."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Short, non-sensitive summary for the approver.",
            },
            "requested_action": {
                "type": "string",
                "description": (
                    "Exact bounded action that approval would authorize; approval "
                    "does not cover any broader or different action."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Why explicit human authorization is required.",
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": _MAX_EVIDENCE,
                "description": "Optional non-sensitive evidence references.",
            },
            "expires_in": {
                "type": "integer",
                "minimum": 1,
                "maximum": 86400,
                "default": 3600,
                "description": "Seconds before the request expires fail-closed.",
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "Stable caller-generated key for retries of this exact action."
                ),
            },
        },
        "required": ["summary", "requested_action", "reason"],
    },
}


from tools.registry import registry

registry.register(
    name="request_human_approval",
    toolset="human_approval",
    schema=HUMAN_APPROVAL_SCHEMA,
    handler=lambda args, **kw: request_human_approval_tool(**args, **kw),
    emoji="🛑",
)
