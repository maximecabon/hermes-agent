"""Durable origin delivery for correlated Orange `needs_input` questions."""

import asyncio

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


class FailingOriginWakeAdapter:
    """A push adapter that records the precise wake before rejecting it."""

    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)
        raise RuntimeError("simulated origin wake failure")


class RecordingOriginWakeAdapter(FailingOriginWakeAdapter):
    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


def _runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


async def _one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _question(question_id="orange-depth-delivery", prompt="Select a policy for the Orange repair tree."):
    from tools.human_question_contract import HUMAN_QUESTION_SCHEMA, canonical_sha256

    question = {
        "schema_version": HUMAN_QUESTION_SCHEMA,
        "question_id": question_id,
        "audience": "HUMAN",
        "prompt": prompt,
        "answer_kind": "CHOICE",
        "choices": ["Continue", "Stop"],
        "required": True,
        "context": "root_task_id=orange-root",
    }
    question["question_sha256"] = canonical_sha256(question)
    return question


def _origin_task():
    question = _question()
    conn = kbc.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="orange planner needs input",
            assignee="planner",
            session_id="agent:main:telegram:dm:origin-chat",
        )
        kbn.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="origin-chat",
            thread_id="origin-thread",
            user_id="origin-user",
            chat_type="dm",
            delivery_mode="notify+wake",
        )
        assert kb.block_task(conn, task_id, kind="needs_input", reason="policy required", human_question=question)
        event = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return task_id, int(event["id"])
    finally:
        conn.close()


def _rearm_with_new_question(task_id):
    conn = kbc.connect()
    try:
        assert kb.unblock_task(conn, task_id)
        # A new correlation starts a fresh Human Wait cycle; model the
        # controller's re-arm before creating its next typed question.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET block_kind = NULL, block_recurrences = 0 WHERE id = ?", (task_id,))
        assert kb.block_task(
            conn,
            task_id,
            kind="needs_input",
            reason="new policy required",
            human_question=_question("orange-depth-delivery-rearmed", "Choose the follow-up Orange policy."),
        )
        event = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return int(event["id"])
    finally:
        conn.close()


def _origin_state(task_id, event_id):
    conn = kbc.connect()
    try:
        return kbn.origin_wake_state(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="origin-chat",
            thread_id="origin-thread",
            event_id=event_id,
        )
    finally:
        conn.close()


def _unseen_terminal_events(task_id):
    conn = kbc.connect()
    try:
        _, events = kbn.unseen_events_for_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="origin-chat",
            thread_id="origin-thread",
            kinds=["blocked"],
        )
        return events
    finally:
        conn.close()


def test_correlated_needs_input_origin_wake_is_bounded_durable_and_rearmed(tmp_path, monkeypatch):
    """One correlated question keeps its exact origin, backs off 1s/5s, and
    is durably exhausted after attempt three without a fourth send.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "orange-origin.db"))
    kb.init_db()
    task_id, event_id = _origin_task()
    adapter = FailingOriginWakeAdapter()
    clock = {"now": 100}
    monkeypatch.setattr(kbn.time, "time", lambda: clock["now"])

    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.handled) == 1
    first = adapter.handled[0]
    assert first.source.chat_id == "origin-chat"
    assert first.source.thread_id == "origin-thread"
    assert first.source.chat_type == "dm"
    assert "Select a policy for the Orange repair tree." in first.text
    assert "Continue" in first.text
    assert _origin_state(task_id, event_id) == {
        "attempts": 1,
        "retry_at": 101,
        "exhausted": False,
    }
    assert len(_unseen_terminal_events(task_id)) == 1

    # The durable retry deadline blocks a restart/tick before one second.
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.handled) == 1

    clock["now"] = 101
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.handled) == 2
    assert _origin_state(task_id, event_id) == {
        "attempts": 2,
        "retry_at": 106,
        "exhausted": False,
    }

    clock["now"] = 105
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.handled) == 2

    clock["now"] = 106
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.handled) == 3
    assert _origin_state(task_id, event_id) == {
        "attempts": 3,
        "retry_at": 0,
        "exhausted": True,
    }
    assert _unseen_terminal_events(task_id) == []

    clock["now"] = 999
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.handled) == 3, "an exhausted origin event must never send a fourth wake"

    # A distinct persisted question is a new correlation: it re-arms delivery
    # without resurrecting the exhausted event's retry budget.
    rearmed_event_id = _rearm_with_new_question(task_id)
    accepted = RecordingOriginWakeAdapter()
    asyncio.run(_one_tick(monkeypatch, _runner(accepted)))
    assert len(accepted.handled) == 1
    assert "Choose the follow-up Orange policy." in accepted.handled[0].text
    asyncio.run(_one_tick(monkeypatch, _runner(accepted)))
    assert len(accepted.handled) == 1, "a successful correlated wake is acknowledged exactly once"
    assert _origin_state(task_id, rearmed_event_id) == {
        "attempts": 0,
        "retry_at": 0,
        "exhausted": False,
    }
