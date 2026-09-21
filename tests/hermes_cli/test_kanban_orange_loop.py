"""Persistence contract for the bounded orange Kanban repair loop."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.config_defaults import DEFAULT_CONFIG


def _legacy_tasks_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('legacy', 'legacy repair', 'ready', 1)"
    )
    conn.commit()
    conn.close()


def test_orange_repair_state_round_trips_on_fresh_and_migrated_databases(tmp_path: Path) -> None:
    fresh_path = tmp_path / "fresh.db"
    with kbc.connect_closing(fresh_path) as conn:
        task_id = kb.create_task(
            conn,
            title="fresh repair",
            repair_depth=3,
            repair_round=2,
            repair_stage="PLANNING_ESCALATION",
            root_task_id="t_root",
        )
        fresh = kb.get_task(conn, task_id)

    assert fresh is not None
    assert (fresh.repair_depth, fresh.repair_round, fresh.repair_stage, fresh.root_task_id) == (
        3,
        2,
        "PLANNING_ESCALATION",
        "t_root",
    )
    assert DEFAULT_CONFIG["kanban"]["max_depth"] == 3
    assert DEFAULT_CONFIG["kanban"]["max_rewrites"] == 2

    legacy_path = tmp_path / "legacy.db"
    _legacy_tasks_db(legacy_path)
    with kbc.connect_closing(legacy_path) as conn:
        migrated = kb.get_task(conn, "legacy")
        columns_once = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    kbc.init_db(legacy_path)
    with kbc.connect_closing(legacy_path) as conn:
        migrated_twice = kb.get_task(conn, "legacy")
        columns_twice = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}

    assert migrated is not None
    assert migrated_twice is not None
    assert (migrated.repair_depth, migrated.repair_round, migrated.repair_stage, migrated.root_task_id) == (0, 0, None, None)
    assert (migrated_twice.repair_depth, migrated_twice.repair_round, migrated_twice.repair_stage, migrated_twice.root_task_id) == (0, 0, None, None)
    assert {"repair_depth", "repair_round", "repair_stage", "root_task_id"} <= columns_once == columns_twice


def test_claim_recomputes_ready_after_parent_completion(tmp_path: Path) -> None:
    """Admission promotes a dependency-cleared card before it opens a worker run."""
    with kbc.connect_closing(tmp_path / "kanban.db") as conn:
        parent_id = kb.create_task(conn, title="upstream")
        child_id = kb.create_task(conn, title="downstream", parents=[parent_id])
        assert kb.get_task(conn, child_id).status == "todo"

        # Simulate an external durable completion that landed after the last
        # dispatcher pass. Admission, not this setup, must recompute the child.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent_id,))
        assert kb.get_task(conn, child_id).status == "todo"
        claimed = kb.claim_task(conn, child_id, claimer="admission")

    assert claimed is not None
    assert claimed.status == "running"
