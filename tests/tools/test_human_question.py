"""Typed Human Wait question persistence for the Kanban needs_input boundary."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools.human_question_contract import (
    HUMAN_QUESTION_SCHEMA,
    HumanQuestionContractError,
    canonical_sha256,
    validate_human_question,
)


def _question(question_id: str = "q-scope") -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": HUMAN_QUESTION_SCHEMA,
        "question_id": question_id,
        "audience": "HUMAN",
        "prompt": "Quel périmètre doit être retenu ?",
        "answer_kind": "CHOICE",
        "choices": ["Option-A", "Option-B"],
        "required": True,
        "context": "Décision nécessaire avant la reprise.",
    }
    return {**body, "question_sha256": canonical_sha256(body)}


def _running_task(conn) -> str:
    task_id = kb.create_task(conn, title="needs input", assignee="builder")
    assert kb.claim_task(conn, task_id, claimer="builder") is not None
    return task_id


def test_needs_input_question_round_trips_with_task_and_root_correlation(tmp_path) -> None:
    with kbc.connect_closing(tmp_path / "kanban.db") as conn:
        task_id = _running_task(conn)
        question = _question()

        assert kb.block_task(conn, task_id, reason="human decision", kind="needs_input", human_question=question)
        stored = kb.get_human_question(conn, task_id, "q-scope")
        event = [event for event in kb.list_events(conn, task_id) if event.kind == "blocked"][-1]

    assert validate_human_question(question) == question
    assert stored == {
        "task_id": task_id,
        "root_task_id": task_id,
        "question": question,
    }
    assert event.payload["human_question"] == {
        "question_id": "q-scope",
        "question_sha256": question["question_sha256"],
        "root_task_id": task_id,
    }
    assert question["prompt"] not in json.dumps(event.payload)


def test_invalid_question_rejects_without_task_event_or_sqlite_mutation(tmp_path) -> None:
    with kbc.connect_closing(tmp_path / "kanban.db") as conn:
        task_id = _running_task(conn)
        invalid = _question()
        invalid["question_sha256"] = "0" * 64
        events_before = len(kb.list_events(conn, task_id))

        with pytest.raises(HumanQuestionContractError):
            kb.block_task(conn, task_id, reason="human decision", kind="needs_input", human_question=invalid)

        assert kb.get_task(conn, task_id).status == "running"
        assert kb.get_human_question(conn, task_id, "q-scope") is None
        assert len(kb.list_events(conn, task_id)) == events_before


def test_kanban_block_tool_requires_and_persists_the_typed_question(tmp_path, monkeypatch) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "builder")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        task_id = _running_task(conn)
        run_id = kb.get_task(conn, task_id).current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    from tools import kanban_tools as kt

    missing = json.loads(kt._handle_block({"reason": "decision", "kind": "needs_input"}))
    assert "question is required" in missing["error"]
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, task_id).status == "running"

    question = _question("q-tool")
    result = json.loads(kt._handle_block({
        "reason": "decision", "kind": "needs_input", "question": question,
    }))
    assert result["ok"] is True
    assert result["question_id"] == "q-tool"
    with kbc.connect_closing() as conn:
        assert kb.get_human_question(conn, task_id, "q-tool") == {
            "task_id": task_id,
            "root_task_id": task_id,
            "question": question,
        }
