"""Latest-only task-run agent activity persistence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agent.session_activity import AGENT_ACTIVITY_COUNT_MAX, normalize_agent_activity_snapshot
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.config_defaults import DEFAULT_CONFIG


def _snapshot(*, tool: str = "terminal") -> dict:
    return normalize_agent_activity_snapshot(
        runtime="hermes",
        tool=tool,
        tokens={"input": AGENT_ACTIVITY_COUNT_MAX + 10, "output": 2, "total": 3},
        limits={"iterations_used": 4, "iterations_max": None, "context_window": 5},
    )


def _task_with_active_run(conn) -> tuple[str, int]:
    task_id = kb.create_task(conn, title="activity", assignee="worker")
    assert kb.claim_task(conn, task_id) is not None
    run_id = kb.get_task(conn, task_id).current_run_id
    assert run_id is not None
    return task_id, run_id


def _column_names(conn, table: str) -> list[tuple[str, str, int, object, int]]:
    return [
        (row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"])
        for row in conn.execute(f"PRAGMA table_info({table})")
    ]


def test_fresh_and_legacy_task_runs_have_same_activity_schema(tmp_path):
    fresh_path = tmp_path / "fresh.db"
    with kbc.connect(fresh_path) as fresh:
        fresh_schema = _column_names(fresh, "task_runs")

    legacy_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(legacy_path)
    legacy.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT, "
        "status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_by TEXT, created_at INTEGER NOT NULL, "
        "started_at INTEGER, completed_at INTEGER, workspace_kind TEXT NOT NULL DEFAULT 'scratch', "
        "workspace_path TEXT, claim_lock TEXT, claim_expires INTEGER)"
    )
    legacy.execute(
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
        "kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL)"
    )
    legacy.execute(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
        "profile TEXT, step_key TEXT, status TEXT NOT NULL, claim_lock TEXT, claim_expires INTEGER, "
        "worker_pid INTEGER, max_runtime_seconds INTEGER, last_heartbeat_at INTEGER, "
        "started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT, summary TEXT, metadata TEXT, error TEXT)"
    )
    legacy.commit()
    legacy.close()

    with kbc.connect(legacy_path) as migrated:
        migrated_schema = _column_names(migrated, "task_runs")
    # Replay is a no-op and preserves the exact canonical shape.
    with kbc.connect(legacy_path) as replayed:
        assert _column_names(replayed, "task_runs") == migrated_schema == fresh_schema


def test_snapshot_is_latest_only_idempotent_and_sequence_fenced(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir()
    with kbc.connect() as conn:
        task_id, run_id = _task_with_active_run(conn)
        first = _snapshot(tool="terminal")
        assert kbd.persist_run_activity_snapshot(conn, task_id, first, sequence=10, expected_run_id=run_id)
        first_row = conn.execute(
            "SELECT activity_json, activity_updated_at, activity_sequence FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert json.loads(first_row["activity_json"]) == first
        assert first_row["activity_sequence"] == 10
        assert json.loads(first_row["activity_json"])["tokens"]["input"] == AGENT_ACTIVITY_COUNT_MAX

        # Identical replay and an old callback are no-ops; no timestamp rewrite.
        assert not kbd.persist_run_activity_snapshot(conn, task_id, first, sequence=10, expected_run_id=run_id)
        assert not kbd.persist_run_activity_snapshot(conn, task_id, _snapshot(tool="web_search"), sequence=9, expected_run_id=run_id)
        unchanged = conn.execute(
            "SELECT activity_json, activity_updated_at, activity_sequence FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert tuple(unchanged) == tuple(first_row)

        newest = _snapshot(tool="web_search")
        assert kbd.persist_run_activity_snapshot(conn, task_id, newest, sequence=11, expected_run_id=run_id)
        latest = kb.get_run(conn, run_id)
        assert latest is not None
        assert latest.activity_json == newest
        assert latest.activity_sequence == 11


def test_absent_activity_stays_null_and_writer_does_not_change_transitions(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir()
    with kbc.connect() as conn:
        task_id, run_id = _task_with_active_run(conn)
        before_events = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)).fetchone()[0]
        empty = conn.execute(
            "SELECT activity_json, activity_updated_at, activity_sequence FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert tuple(empty) == (None, None, None)

        assert kbd.persist_run_activity_snapshot(conn, task_id, _snapshot(), sequence=1, expected_run_id=run_id)
        assert kb.get_task(conn, task_id).status == "running"
        assert kb.get_task(conn, task_id).current_run_id == run_id
        after_events = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)).fetchone()[0]
        assert after_events == before_events

        assert kb.complete_task(conn, task_id, result="done")
        assert not kbd.persist_run_activity_snapshot(conn, task_id, _snapshot(), sequence=2, expected_run_id=run_id)


def test_opt_in_flag_defaults_off_and_bridge_skips_write(tmp_path, monkeypatch):
    assert DEFAULT_CONFIG["kanban"]["persist_run_activity"] is False
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    with kbc.connect() as conn:
        task_id, run_id = _task_with_active_run(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"kanban": {"persist_run_activity": False}})

    assert not kbd.persist_current_worker_activity_snapshot(_snapshot(), sequence=1)
    with kbc.connect() as conn:
        row = conn.execute("SELECT activity_json FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        assert row["activity_json"] is None

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"kanban": {"persist_run_activity": True}})
    assert kbd.persist_current_worker_activity_snapshot(_snapshot(), sequence=1)
    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT activity_json, activity_sequence FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert json.loads(row["activity_json"])["schema_version"] == "agent_activity/v1"
        assert row["activity_sequence"] == 1


def test_activity_tracking_reuses_existing_auto_heartbeat_cadence(monkeypatch):
    from types import SimpleNamespace

    from agent.activity_tracking import ActivityTrackingMixin
    from tools import kanban_tools

    persisted = []
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_activity")
    monkeypatch.setattr(kanban_tools, "heartbeat_current_worker_from_env", lambda: True)
    monkeypatch.setattr(kanban_tools, "inject_new_comments_from_env", lambda _agent: False)
    monkeypatch.setattr(
        kbd,
        "persist_current_worker_activity_snapshot",
        lambda snapshot, *, sequence: persisted.append((snapshot, sequence)) or True,
    )

    agent = SimpleNamespace(
        _current_tool="terminal",
        session_input_tokens=1,
        session_output_tokens=2,
        session_total_tokens=3,
        _api_call_count=4,
        max_iterations=5,
        context_compressor=None,
        _persist_session_activity_if_due=lambda: None,
    )
    ActivityTrackingMixin._touch_activity(agent, "tool running")

    assert len(persisted) == 1
    snapshot, sequence = persisted[0]
    assert snapshot["schema_version"] == "agent_activity/v1"
    assert snapshot["tool"] == "terminal"
    assert sequence == agent._turn_liveness_activity_generation
