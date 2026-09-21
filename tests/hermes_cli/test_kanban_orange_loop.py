"""Persistence contract for the bounded orange Kanban repair loop."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
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


def _claimed_replan_source(conn) -> tuple[str, int]:
    source_id = kb.create_task(
        conn,
        title="Repair source",
        assignee="builder",
        repair_depth=1,
        repair_round=2,
        repair_stage="VERIFY",
        root_task_id="t_root",
    )
    claimed = kb.claim_task(conn, source_id, claimer="builder:test")
    assert claimed is not None
    assert claimed.current_run_id is not None
    return source_id, claimed.current_run_id


def test_needs_replan_is_atomic_and_idempotent_for_one_task_round(tmp_path: Path) -> None:
    with kbc.connect_closing(tmp_path / "kanban.db") as conn:
        source_id, run_id = _claimed_replan_source(conn)
        findings = ["TECH-001", "LOOP-002"]

        parent_id = kb.needs_replan(
            conn, source_id, findings=findings, expected_run_id=run_id,
        )
        replay_id = kb.needs_replan(
            conn, source_id, findings=findings, expected_run_id=run_id,
        )

        source = kb.get_task(conn, source_id)
        parent = kb.get_task(conn, parent_id)
        parents = kb.parent_ids(conn, source_id)
        events = [event for event in kb.list_events(conn, source_id) if event.kind == "needs_replan"]
        runs = kb.list_runs(conn, source_id)

    assert parent_id == replay_id
    assert source is not None
    assert source.status == "todo"
    assert source.current_run_id is None
    assert parents == [parent_id]
    assert parent is not None
    assert parent.assignee == "planner"
    assert parent.status == "ready"
    assert parent.repair_stage == "PLANNING_ESCALATION"
    assert parent.repair_round == 2
    assert parent.root_task_id == "t_root"
    assert len(events) == 1
    assert events[0].payload == {
        "findings": findings,
        "parent_id": parent_id,
        "replan_key": f"replan:{source_id}:2",
        "repair_round": 2,
        "root_task_id": "t_root",
    }
    assert len(runs) == 1
    assert runs[0].outcome == "needs_replan"
    assert runs[0].ended_at is not None


def test_needs_replan_rolls_back_every_mutation_when_event_persistence_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    with kbc.connect_closing(tmp_path / "kanban.db") as conn:
        source_id, run_id = _claimed_replan_source(conn)
        original_append = kb._append_event

        def fail_needs_replan_event(conn, task_id, kind, payload=None, *, run_id=None):
            if kind == "needs_replan":
                raise RuntimeError("injected transition failure")
            return original_append(conn, task_id, kind, payload, run_id=run_id)

        monkeypatch.setattr(kb, "_append_event", fail_needs_replan_event)
        try:
            kb.needs_replan(conn, source_id, findings=["TECH-001"], expected_run_id=run_id)
        except RuntimeError as exc:
            assert str(exc) == "injected transition failure"
        else:
            raise AssertionError("the injected persistence failure must escape")

        source = kb.get_task(conn, source_id)
        parents = kb.parent_ids(conn, source_id)
        replan_rows = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ?", (f"replan:{source_id}:2",),
        ).fetchall()
        events = [event for event in kb.list_events(conn, source_id) if event.kind == "needs_replan"]
        runs = kb.list_runs(conn, source_id)

    assert source is not None
    assert source.status == "running"
    assert source.current_run_id == run_id
    assert parents == []
    assert replan_rows == []
    assert events == []
    assert len(runs) == 1
    assert runs[0].ended_at is None


def test_needs_replan_concurrent_replays_create_one_planner_parent(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    with kbc.connect_closing(db_path) as conn:
        source_id, run_id = _claimed_replan_source(conn)

    def transition() -> str:
        with kbc.connect_closing(db_path) as concurrent_conn:
            return kb.needs_replan(
                concurrent_conn, source_id, findings=["TECH-001"], expected_run_id=run_id,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        parent_ids = list(executor.map(lambda _: transition(), range(2)))

    with kbc.connect_closing(db_path) as conn:
        source = kb.get_task(conn, source_id)
        parents = kb.parent_ids(conn, source_id)
        events = [event for event in kb.list_events(conn, source_id) if event.kind == "needs_replan"]
        runs = kb.list_runs(conn, source_id)

    assert len(set(parent_ids)) == 1
    assert parents == [parent_ids[0]]
    assert source is not None
    assert source.status == "todo"
    assert source.current_run_id is None
    assert len(events) == 1
    assert len(runs) == 1
    assert runs[0].ended_at is not None


def test_needs_replan_tool_closes_the_current_worker_run(
    tmp_path: Path, monkeypatch,
) -> None:
    from tools import kanban_tools as tools

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_PROFILE", "builder")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect() as conn:
        source_id, run_id = _claimed_replan_source(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", source_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    result = json.loads(tools._handle_needs_replan({"findings": ["TECH-001"]}))

    assert result["ok"] is True
    assert result["task_id"] == source_id
    assert result["status"] == "todo"
    with kbc.connect() as conn:
        assert kb.parent_ids(conn, source_id) == [result["parent_id"]]


def test_only_the_expected_planner_run_can_create_or_link_orange_subcards(tmp_path: Path) -> None:
    with kbc.connect_closing(tmp_path / "kanban.db") as conn:
        source_id, source_run_id = _claimed_replan_source(conn)
        planner_id = kb.needs_replan(
            conn, source_id, findings=["TECH-001"], expected_run_id=source_run_id,
        )
        planner = kb.claim_task(conn, planner_id, claimer="planner:test")
        specialist_id = kb.create_task(conn, title="Specialist", assignee="builder")
        specialist = kb.claim_task(conn, specialist_id, claimer="builder:test")

        assert planner is not None
        assert planner.current_run_id is not None
        assert specialist is not None
        assert specialist.current_run_id is not None

        try:
            kb.create_task(
                conn, title="Unauthorized orange child", parents=[planner_id],
                actor_task_id=specialist_id, actor_run_id=specialist.current_run_id,
            )
        except PermissionError as exc:
            assert "expected Planner run" in str(exc)
        else:
            raise AssertionError("a specialist run must not create an orange subcard")

        try:
            kb.create_task(
                conn, title="Stale Planner child", parents=[planner_id],
                actor_task_id=planner_id, actor_run_id=planner.current_run_id + 1,
            )
        except PermissionError as exc:
            assert "expected Planner run" in str(exc)
        else:
            raise AssertionError("a stale Planner run must not create an orange subcard")

        first_child = kb.create_task(
            conn, title="Authorized first child", parents=[planner_id],
            finding_ids=["TECH-001"],
            actor_task_id=planner_id, actor_run_id=planner.current_run_id,
        )
        second_child = kb.create_task(
            conn, title="Authorized second child", parents=[planner_id],
            finding_ids=["TECH-001"],
            actor_task_id=planner_id, actor_run_id=planner.current_run_id,
        )
        kb.link_tasks(
            conn, first_child, second_child,
            actor_task_id=planner_id, actor_run_id=planner.current_run_id,
        )

        try:
            kb.link_tasks(
                conn, first_child, planner_id,
                actor_task_id=specialist_id, actor_run_id=specialist.current_run_id,
            )
        except PermissionError as exc:
            assert "expected Planner run" in str(exc)
        else:
            raise AssertionError("a specialist run must not relink an orange subcard")

        ordinary_parent = kb.create_task(conn, title="ordinary parent")
        ordinary_child = kb.create_task(conn, title="ordinary child")
        kb.link_tasks(conn, ordinary_parent, ordinary_child)

        assert kb.get_task(conn, first_child).root_task_id == "t_root"
        assert set(kb.parent_ids(conn, second_child)) == {first_child, planner_id}
        assert kb.parent_ids(conn, ordinary_child) == [ordinary_parent]


def test_orange_findings_are_persisted_covered_and_replay_stable(tmp_path: Path) -> None:
    with kbc.connect_closing(tmp_path / "kanban.db") as conn:
        source_id, source_run_id = _claimed_replan_source(conn)
        planner_id = kb.needs_replan(
            conn, source_id, findings=["TECH-001", "LOOP-002"], expected_run_id=source_run_id,
        )
        planner = kb.claim_task(conn, planner_id, claimer="planner:test")
        assert planner is not None
        assert planner.current_run_id is not None

        try:
            kb.create_task(
                conn, title="Uncovered orange child", parents=[planner_id],
                actor_task_id=planner_id, actor_run_id=planner.current_run_id,
            )
        except ValueError as exc:
            assert "finding_ids" in str(exc)
        else:
            raise AssertionError("an orange child without finding coverage must be rejected")

        tech_child = kb.create_task(
            conn, title="Cover TECH-001", parents=[planner_id], finding_ids=["TECH-001"],
            idempotency_key="cover:TECH-001",
            actor_task_id=planner_id, actor_run_id=planner.current_run_id,
        )
        try:
            kb.require_orange_finding_coverage(conn, planner_id)
        except ValueError as exc:
            assert "LOOP-002" in str(exc)
        else:
            raise AssertionError("an incomplete orange fan-in must be rejected")

        loop_child = kb.create_task(
            conn, title="Cover LOOP-002", parents=[planner_id], finding_ids=["LOOP-002"],
            actor_task_id=planner_id, actor_run_id=planner.current_run_id,
        )
        replay_child = kb.create_task(
            conn, title="Cover TECH-001 replay", parents=[planner_id], finding_ids=["TECH-001"],
            idempotency_key="cover:TECH-001",
            actor_task_id=planner_id, actor_run_id=planner.current_run_id,
        )
        coverage = kb.require_orange_finding_coverage(conn, planner_id)

    assert replay_child == tech_child
    assert coverage == {
        "LOOP-002": [loop_child],
        "TECH-001": [tech_child],
    }
