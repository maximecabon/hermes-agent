"""Tests for kanban worker/runs read endpoints.

Covers:
  GET /workers/active
  GET /runs/{run_id}
  GET /runs/{run_id}/inspect
  POST /runs/{run_id}/terminate
"""

from __future__ import annotations

import importlib.util
import json
import secrets
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.activity_tracking import ActivityTrackingMixin
from agent.codex_runtime import make_codex_app_server_event_bridge
from hermes_cli import kanban_db as kb
from agent.session_activity import normalize_agent_activity_snapshot
from tools import kanban_tools


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _load_plugin_router():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return its router."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    mod_name = "hermes_dashboard_plugin_kanban_worker_runs_test"
    # Re-use a cached module if already loaded to avoid duplicate-router issues.
    if mod_name in sys.modules:
        return sys.modules[mod_name].router

    spec = importlib.util.spec_from_file_location(mod_name, plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _insert_run(conn, task_id, *, worker_pid=None, ended_at=None):
    """Insert a task_runs row directly (bypassing claim machinery) and return run_id."""
    lock = secrets.token_hex(8)
    future = int(time.time()) + 3600
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, status, claim_lock, claim_expires, worker_pid, started_at, ended_at) "
        "VALUES (?, 'running', ?, ?, ?, ?, ?)",
        (task_id, lock, future, worker_pid, int(time.time()), ended_at),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# GET /workers/active
# ---------------------------------------------------------------------------

def test_workers_active_empty_board(client):
    """Board with no running tasks returns an empty workers list."""
    r = client.get("/api/plugins/kanban/workers/active")
    assert r.status_code == 200
    body = r.json()
    assert body["workers"] == []
    assert body["count"] == 0
    assert "checked_at" in body


# ---------------------------------------------------------------------------
# GET /runs/{run_id}
# ---------------------------------------------------------------------------

def test_get_run_404_unknown_id(client):
    """Non-existent run_id returns 404."""
    r = client.get("/api/plugins/kanban/runs/999999")
    assert r.status_code == 404
    assert "999999" in r.json()["detail"]


def _activity(*, waiting_for=None, thread_status="active", error=None):
    return normalize_agent_activity_snapshot(
        runtime="codex_app_server",
        waiting_for=waiting_for,
        thread_status=thread_status,
        error=error,
    )


def _enable_activity_api(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"kanban": {"persist_run_activity": True}},
    )
    module = sys.modules["hermes_dashboard_plugin_kanban_worker_runs_test"]
    monkeypatch.setattr(module, "_pid_liveness", lambda *_args, **_kwargs: True, raising=False)
    return module


@pytest.mark.parametrize("waiting_for", ["approval", "input"])
def test_get_run_exposes_each_human_wait_flag_as_waiting_human(client, monkeypatch, waiting_for):
    _enable_activity_api(monkeypatch)
    with kb.connect() as conn:
        _, run_id = _setup_running_task_with_run(conn, title="activity", assignee="worker", worker_pid=12345)
        snapshot = _activity(waiting_for=waiting_for)
        conn.execute("UPDATE task_runs SET activity_json = ? WHERE id = ?", (json.dumps(snapshot), run_id))
        conn.commit()

    body = client.get(f"/api/plugins/kanban/runs/{run_id}").json()["run"]
    assert body["activity"] == snapshot
    assert body["operator_state"] == "WAITING_HUMAN"


@pytest.mark.parametrize("thread_status", ["idle", "notLoaded", "systemError"])
def test_get_run_never_infers_process_gone_from_non_active_codex_status(client, monkeypatch, thread_status):
    _enable_activity_api(monkeypatch)
    with kb.connect() as conn:
        _, run_id = _setup_running_task_with_run(conn, title="activity", assignee="worker", worker_pid=12345)
        conn.execute(
            "UPDATE task_runs SET activity_json = ? WHERE id = ?",
            (json.dumps(_activity(thread_status=thread_status)), run_id),
        )
        conn.commit()

    assert client.get(f"/api/plugins/kanban/runs/{run_id}").json()["run"]["operator_state"] == "UNKNOWN"


def test_get_run_reports_process_gone_only_for_conclusive_dead_pid(client, monkeypatch):
    module = _enable_activity_api(monkeypatch)
    monkeypatch.setattr(module, "_pid_liveness", lambda *_args, **_kwargs: False)
    with kb.connect() as conn:
        _, run_id = _setup_running_task_with_run(conn, title="activity", assignee="worker", worker_pid=12345)
        conn.execute("UPDATE task_runs SET activity_json = ? WHERE id = ?", (json.dumps(_activity()), run_id))
        conn.commit()

    assert client.get(f"/api/plugins/kanban/runs/{run_id}").json()["run"]["operator_state"] == "PROCESS_GONE"


def test_get_run_reports_unknown_when_pid_liveness_is_unavailable(client, monkeypatch):
    module = _enable_activity_api(monkeypatch)
    monkeypatch.setattr(module, "_pid_liveness", lambda *_args, **_kwargs: None)
    with kb.connect() as conn:
        _, run_id = _setup_running_task_with_run(conn, title="activity", assignee="worker", worker_pid=12345)
        conn.execute("UPDATE task_runs SET activity_json = ? WHERE id = ?", (json.dumps(_activity()), run_id))
        conn.commit()

    assert client.get(f"/api/plugins/kanban/runs/{run_id}").json()["run"]["operator_state"] == "UNKNOWN"


def test_get_run_reports_unknown_when_no_pid_was_recorded(client, monkeypatch):
    module = _enable_activity_api(monkeypatch)
    monkeypatch.setattr(module, "_pid_liveness", lambda *_args, **_kwargs: None)
    with kb.connect() as conn:
        _, run_id = _setup_running_task_with_run(conn, title="activity", assignee="worker", worker_pid=None)
        conn.execute("UPDATE task_runs SET activity_json = ? WHERE id = ?", (json.dumps(_activity()), run_id))
        conn.commit()

    assert client.get(f"/api/plugins/kanban/runs/{run_id}").json()["run"]["operator_state"] == "UNKNOWN"


def test_workers_active_exposes_only_redacted_activity_and_derived_state(client, monkeypatch):
    _enable_activity_api(monkeypatch)
    with kb.connect() as conn:
        _, run_id = _setup_running_task_with_run(conn, title="activity", assignee="worker", worker_pid=12345)
        snapshot = _activity(waiting_for="approval")
        snapshot["ignored_payload"] = "Bearer secret-that-must-not-leak"
        conn.execute("UPDATE task_runs SET activity_json = ? WHERE id = ?", (json.dumps(snapshot), run_id))
        conn.commit()

    worker = client.get("/api/plugins/kanban/workers/active").json()["workers"][0]
    assert worker["run_id"] == run_id
    assert worker["operator_state"] == "WAITING_HUMAN"
    assert worker["activity"]["waiting_for"] == "approval"
    assert "ignored_payload" not in worker["activity"]
    assert "secret-that-must-not-leak" not in repr(worker["activity"])


def test_get_run_hides_derived_telemetry_when_the_opt_in_flag_is_off(client):
    with kb.connect() as conn:
        _, run_id = _setup_running_task_with_run(conn, title="activity", assignee="worker", worker_pid=12345)
        conn.execute("UPDATE task_runs SET activity_json = ? WHERE id = ?", (json.dumps(_activity()), run_id))
        conn.commit()

    body = client.get(f"/api/plugins/kanban/runs/{run_id}").json()["run"]
    assert "activity" not in body
    assert "operator_state" not in body


@pytest.mark.parametrize(
    ("status_type", "active_flags", "expected_waiting_for", "expected_operator_state"),
    [
        ("active", ["waitingOnApproval"], "approval", "WAITING_HUMAN"),
        ("active", ["waitingOnUserInput"], "input", "WAITING_HUMAN"),
        ("idle", [], None, "UNKNOWN"),
        ("notLoaded", [], None, "UNKNOWN"),
        ("systemError", [], None, "UNKNOWN"),
    ],
)
def test_codex_thread_status_event_persists_latest_snapshot_and_api_state(
    client,
    monkeypatch,
    status_type,
    active_flags,
    expected_waiting_for,
    expected_operator_state,
):
    """A Codex status event uses the existing activity bridge, writer, and API projection."""
    _enable_activity_api(monkeypatch)
    monkeypatch.setattr(kanban_tools, "heartbeat_current_worker_from_env", lambda: True)
    monkeypatch.setattr(kanban_tools, "inject_new_comments_from_env", lambda _agent: False)
    with kb.connect() as conn:
        task_id, run_id = _setup_running_task_with_run(
            conn, title="codex activity", assignee="worker", worker_pid=12345,
        )
        conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))
        conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    agent = SimpleNamespace(
        api_mode="codex_app_server",
        _current_tool=None,
        _last_activity_error=None,
        session_input_tokens=0,
        session_output_tokens=0,
        session_total_tokens=0,
        _api_call_count=1,
        max_iterations=4,
        context_compressor=None,
        _persist_session_activity_if_due=lambda: None,
    )
    agent._touch_activity = ActivityTrackingMixin._touch_activity.__get__(agent, SimpleNamespace)

    make_codex_app_server_event_bridge(agent)({
        "method": "thread/status/changed",
        "params": {"threadId": "thread-1", "status": {"type": status_type, "activeFlags": active_flags}},
    })

    with kb.connect() as conn:
        row = conn.execute(
            "SELECT activity_json, activity_sequence FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()
    snapshot = json.loads(row["activity_json"])
    assert snapshot["runtime"] == "codex_app_server"
    assert snapshot["thread_status"] == status_type
    assert snapshot["waiting_for"] == expected_waiting_for
    assert row["activity_sequence"] == 1

    body = client.get(f"/api/plugins/kanban/runs/{run_id}").json()["run"]
    assert body["activity"] == snapshot
    assert body["operator_state"] == expected_operator_state


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/inspect
# ---------------------------------------------------------------------------

def test_inspect_run_404(client):
    """Non-existent run_id returns 404."""
    r = client.get("/api/plugins/kanban/runs/888888/inspect")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/terminate
# ---------------------------------------------------------------------------

def _setup_running_task_with_run(conn, *, title, assignee, worker_pid):
    """Create a task in 'running' state with a matching open task_runs row.

    Mirrors what dispatcher_claim does: stamps tasks.status='running',
    tasks.claim_lock, tasks.worker_pid; inserts task_runs row with the
    same claim_lock so reclaim_task's preconditions are satisfied.
    """
    task_id = kb.create_task(conn, title=title, assignee=assignee)
    lock = secrets.token_hex(8)
    future = int(time.time()) + 3600
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock=?, "
        "claim_expires=?, worker_pid=? WHERE id=?",
        (lock, future, worker_pid, task_id),
    )
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, status, claim_lock, claim_expires, worker_pid, started_at) "
        "VALUES (?, 'running', ?, ?, ?, ?)",
        (task_id, lock, future, worker_pid, int(time.time())),
    )
    conn.commit()
    return task_id, cur.lastrowid


def test_terminate_run_404_unknown_id(client):
    """POST to unknown run_id returns 404."""
    r = client.post(
        "/api/plugins/kanban/runs/777777/terminate",
        json={"reason": "test"},
    )
    assert r.status_code == 404
    assert "777777" in r.json()["detail"]


