"""Telegram mirror delivery for correlated Orange ``needs_input`` questions."""

import asyncio
import contextlib
from types import SimpleNamespace

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


class RecordingOriginAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


class FailingTelegramAdapter:
    """Telegram's real send contract: failure is returned, not raised."""

    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})
        return SendResult(success=False, error="simulated Telegram outage")


async def _one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _question():
    from tools.human_question_contract import HUMAN_QUESTION_SCHEMA, canonical_sha256

    question = {
        "schema_version": HUMAN_QUESTION_SCHEMA,
        "question_id": "telegram-mirror-question",
        "audience": "HUMAN",
        "prompt": "Choose the Orange repair policy.",
        "answer_kind": "CHOICE",
        "choices": ["Continue", "Stop"],
        "required": True,
        "context": "root_task_id=orange-root",
    }
    question["question_sha256"] = canonical_sha256(question)
    return question


def _create_mirrored_question():
    conn = kbc.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="orange planner needs mirrored input",
            assignee="planner",
            session_id="agent:main:discord:dm:origin-discord",
        )
        kbn.add_notify_sub(
            conn,
            task_id=task_id,
            platform="discord",
            chat_id="origin-discord",
            thread_id="origin-thread",
            user_id="origin-user",
            chat_type="dm",
            notifier_profile="default",
            delivery_mode="notify+wake",
        )
        kbn.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="4242",
            thread_id="17",
            user_id="telegram-user",
            chat_type="dm",
            notifier_profile="telegram-target",
            delivery_mode="notify+wake",
            delivery_metadata={
                "chat_type": "dm",
                "direct_messages_topic_id": "17",
                "telegram_dm_topic_reply_fallback": True,
                "telegram_reply_to_message_id": "462",
            },
        )
        assert kb.block_task(conn, task_id, kind="needs_input", reason="policy required", human_question=_question())
        event = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return task_id, int(event["id"])
    finally:
        conn.close()


def _telegram_state(task_id, event_id):
    conn = kbc.connect()
    try:
        return kbn.telegram_wake_state(
            conn,
            task_id=task_id,
            chat_id="4242",
            thread_id="17",
            event_id=event_id,
        )
    finally:
        conn.close()


def _unseen(task_id, platform, chat_id, thread_id):
    conn = kbc.connect()
    try:
        _, events = kbn.unseen_events_for_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            kinds=["blocked"],
        )
        return events
    finally:
        conn.close()


def _event_kinds(task_id):
    conn = kbc.connect()
    try:
        return [
            row["kind"]
            for row in conn.execute("SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,))
        ]
    finally:
        conn.close()


def _runner(origin, telegram):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: origin}
    runner._profile_adapters = {"telegram-target": {Platform.TELEGRAM: telegram}}
    runner._primary_profile_name = "default"
    runner._active_profile_name = lambda: "default"
    runner.config = SimpleNamespace(multiplex_profiles=True, profile_routes=[])
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def test_non_telegram_origin_mirrors_once_to_configured_telegram_target_with_durable_failure_state(tmp_path, monkeypatch):
    """Telegram is its own acknowledged leg: a failed target never replays the
    Discord origin, preserves DM-topic/profile routing, and stops after three.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "telegram-mirror.db"))
    kb.init_db()
    task_id, event_id = _create_mirrored_question()
    origin, telegram = RecordingOriginAdapter(), FailingTelegramAdapter()
    clock = {"now": 100}
    monkeypatch.setattr(kbn.time, "time", lambda: clock["now"])
    from gateway.kanban_watchers_notifier import _KanbanNotification
    monkeypatch.setattr(_KanbanNotification, "_owner_scope", lambda self: contextlib.nullcontext())

    asyncio.run(_one_tick(monkeypatch, _runner(origin, telegram)))
    assert len(origin.sent) == len(origin.handled) == 1
    assert origin.handled[0].source.platform is Platform.DISCORD
    assert origin.handled[0].source.chat_id == "origin-discord"
    assert origin.handled[0].source.thread_id == "origin-thread"
    assert len(telegram.sent) == 1
    assert telegram.sent[0]["chat_id"] == "4242"
    assert telegram.sent[0]["metadata"] == {
        "chat_type": "dm",
        "direct_messages_topic_id": "17",
        "telegram_dm_topic_reply_fallback": True,
        "telegram_reply_to_message_id": "462",
        "thread_id": "17",
    }
    assert "Choose the Orange repair policy." in telegram.sent[0]["text"]
    assert _telegram_state(task_id, event_id) == {"attempts": 1, "retry_at": 101, "exhausted": False}
    assert _unseen(task_id, "discord", "origin-discord", "origin-thread") == []
    assert len(_unseen(task_id, "telegram", "4242", "17")) == 1

    asyncio.run(_one_tick(monkeypatch, _runner(origin, telegram)))
    assert len(origin.sent) == 1 and len(telegram.sent) == 1

    clock["now"] = 101
    asyncio.run(_one_tick(monkeypatch, _runner(origin, telegram)))
    assert len(origin.sent) == 1 and len(telegram.sent) == 2
    assert _telegram_state(task_id, event_id) == {"attempts": 2, "retry_at": 106, "exhausted": False}

    clock["now"] = 106
    asyncio.run(_one_tick(monkeypatch, _runner(origin, telegram)))
    assert len(origin.sent) == 1 and len(telegram.sent) == 3
    assert _telegram_state(task_id, event_id) == {"attempts": 3, "retry_at": 0, "exhausted": True}
    assert _event_kinds(task_id).count("telegram_exhausted") == 1
    assert _unseen(task_id, "telegram", "4242", "17") == []

    clock["now"] = 999
    asyncio.run(_one_tick(monkeypatch, _runner(origin, telegram)))
    assert len(origin.sent) == 1
    assert len(telegram.sent) == 3, "telegram_exhausted must prevent a fourth send"
