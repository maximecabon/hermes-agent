"""Telegram rendering and callback tests for explicit human approvals."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.config import HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter
from tools.human_approval import HumanApprovalStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _delivered_request(store, *, now=None):
    now = time.time() if now is None else now
    request = store.create_request(
        summary="Approve deployment",
        requested_action="Deploy build-42 to staging",
        reason="Explicit sign-off required",
        evidence=["report.json"],
        expires_in=60,
        idempotency_key="telegram-gate",
        requester_profile="planner",
        requester_session_id="session-1",
        requester_session_key="key-1",
        requester_turn_id="turn-1",
        tool_call_id="call-1",
        origin_platform="discord",
        origin_chat_id="origin-chat",
        origin_thread_id="origin-thread",
        now=now,
    )
    claimed = store.claim_next_delivery(
        owner="telegram-gateway",
        telegram_chat_id="4242",
        telegram_thread_id=None,
        lease_seconds=30,
        now=now,
    )
    assert claimed is not None
    store.complete_delivery(
        request.request_id,
        owner="telegram-gateway",
        origin_delivery="delivered",
        telegram_delivery="delivered",
        origin_message_id="o1",
        telegram_message_id="t1",
        now=now,
    )
    return store.get_request(request.request_id, now=now)


@pytest.mark.anyio
async def test_send_gate_renders_exactly_approuver_and_refuser(tmp_path, monkeypatch):
    adapter = _make_adapter()
    record = _delivered_request(HumanApprovalStore(tmp_path / "approval.db"))
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=77))
    buttons = []
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardButton",
        lambda text, callback_data: buttons.append((text, callback_data)) or (text, callback_data),
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardMarkup", lambda rows: rows
    )

    result = await adapter.send_human_approval_gate(
        record,
        chat_id="4242",
        thread_id=None,
    )

    assert result.success is True
    assert buttons == [
        ("Approuver", f"ha:{record.request_id}:a"),
        ("Refuser", f"ha:{record.request_id}:r"),
    ]
    kwargs = adapter._bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == 4242
    assert "Deploy build-42 to staging" in kwargs["text"]
    assert "secret" not in " ".join(callback for _, callback in buttons).lower()


@pytest.mark.anyio
async def test_authorized_callback_resolves_only_correlated_request(tmp_path):
    store = HumanApprovalStore(tmp_path / "approval.db")
    record = _delivered_request(store)
    other = store.create_request(
        summary="Other gate",
        requested_action="Publish another artifact",
        reason="Separate decision",
        evidence=[],
        expires_in=60,
        idempotency_key="other-gate",
        requester_profile="builder",
        requester_session_id="session-2",
        requester_session_key="key-2",
        requester_turn_id="turn-2",
        tool_call_id="call-2",
        origin_platform="slack",
        origin_chat_id="other-origin",
        origin_thread_id=None,
        now=time.time(),
    )

    adapter = _make_adapter()
    adapter._is_callback_user_authorized = MagicMock(return_value=True)
    query = AsyncMock()
    query.data = f"ha:{record.request_id}:a"
    query.message = MagicMock(chat_id=4242, message_thread_id=None, text="Gate")
    query.message.chat.type = "private"
    query.from_user = MagicMock(id="owner", first_name="Owner")
    update = MagicMock(callback_query=query)

    with patch("tools.human_approval.get_shared_store", return_value=store):
        await adapter._handle_callback_query(update, MagicMock())

    assert store.get_request(record.request_id).state == "approved"
    assert store.get_request(other.request_id).state == "pending"
    query.edit_message_text.assert_called_once()
    assert query.edit_message_text.call_args.kwargs["reply_markup"] is None


@pytest.mark.anyio
async def test_unauthorized_wrong_chat_and_double_click_have_no_effect(tmp_path):
    store = HumanApprovalStore(tmp_path / "approval.db")
    record = _delivered_request(store)
    adapter = _make_adapter()

    def make_query(chat_id=4242):
        query = AsyncMock()
        query.data = f"ha:{record.request_id}:r"
        query.message = MagicMock(chat_id=chat_id, message_thread_id=None, text="Gate")
        query.message.chat.type = "private"
        query.from_user = MagicMock(id="owner", first_name="Owner")
        return query

    unauthorized = make_query()
    adapter._is_callback_user_authorized = MagicMock(return_value=False)
    with patch("tools.human_approval.get_shared_store", return_value=store):
        await adapter._handle_callback_query(MagicMock(callback_query=unauthorized), MagicMock())
    assert store.get_request(record.request_id, now=1001.0).state == "pending"

    wrong_chat = make_query(chat_id=9999)
    adapter._is_callback_user_authorized = MagicMock(return_value=True)
    with patch("tools.human_approval.get_shared_store", return_value=store):
        await adapter._handle_callback_query(MagicMock(callback_query=wrong_chat), MagicMock())
    assert store.get_request(record.request_id, now=1001.0).state == "pending"

    first = make_query()
    second = make_query()
    with patch("tools.human_approval.get_shared_store", return_value=store):
        await adapter._handle_callback_query(MagicMock(callback_query=first), MagicMock())
        await adapter._handle_callback_query(MagicMock(callback_query=second), MagicMock())

    assert store.get_request(record.request_id, now=1001.0).state == "refused"
    assert "déjà" in second.answer.call_args.kwargs["text"].lower() or "already" in second.answer.call_args.kwargs["text"].lower()


@pytest.mark.anyio
async def test_expired_callback_is_noop_and_removes_buttons(tmp_path):
    store = HumanApprovalStore(tmp_path / "approval.db")
    record = _delivered_request(store, now=2000.0)
    store.expire_due(now=2061.0)

    adapter = _make_adapter()
    adapter._is_callback_user_authorized = MagicMock(return_value=True)
    query = AsyncMock()
    query.data = f"ha:{record.request_id}:a"
    query.message = MagicMock(chat_id=4242, message_thread_id=None, text="Gate")
    query.message.chat.type = "private"
    query.from_user = MagicMock(id="owner", first_name="Owner")

    with patch("tools.human_approval.get_shared_store", return_value=store), patch(
        "tools.human_approval.time.time", return_value=2061.0
    ):
        await adapter._handle_callback_query(MagicMock(callback_query=query), MagicMock())

    assert store.get_request(record.request_id, now=2061.0).state == "expired"
    query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)


@pytest.mark.anyio
async def test_delivery_mirrors_origin_and_telegram(tmp_path):
    store = HumanApprovalStore(tmp_path / "approval.db")
    now = time.time()
    record = store.create_request(
        summary="Cross-channel gate",
        requested_action="Publish build-42",
        reason="Human sign-off required",
        evidence=[],
        expires_in=60,
        idempotency_key="cross-channel",
        requester_profile="builder",
        requester_session_id="session-cross",
        requester_session_key="key-cross",
        requester_turn_id="turn-cross",
        tool_call_id="call-cross",
        origin_platform="discord",
        origin_chat_id="origin-chat",
        origin_thread_id="origin-thread",
        now=now,
    )
    claimed = store.claim_next_delivery(
        owner="worker",
        telegram_chat_id="4242",
        telegram_thread_id=None,
        lease_seconds=30,
        now=now,
        request_id=record.request_id,
    )
    assert claimed is not None

    origin_adapter = MagicMock()
    origin_adapter.send = AsyncMock(
        return_value=SendResult(success=True, message_id="origin-message")
    )
    adapter = _make_adapter()
    adapter.gateway_runner = MagicMock()
    adapter.gateway_runner._adapter_for_source.return_value = origin_adapter
    adapter._bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=88)
    )

    await adapter._deliver_human_approval(
        store,
        claimed,
        owner="worker",
        target_chat="4242",
        target_thread=None,
    )

    terminal = store.get_request(record.request_id)
    assert terminal.state == "pending"
    assert terminal.origin_delivery == "delivered"
    assert terminal.telegram_delivery == "delivered"
    origin_adapter.send.assert_awaited_once()
    adapter._bot.send_message.assert_awaited_once()


@pytest.mark.anyio
async def test_origin_equal_to_telegram_target_is_deduplicated(tmp_path):
    store = HumanApprovalStore(tmp_path / "approval.db")
    now = time.time()
    record = store.create_request(
        summary="Same-channel gate",
        requested_action="Release build-43",
        reason="Human sign-off required",
        evidence=[],
        expires_in=60,
        idempotency_key="same-channel",
        requester_profile="builder",
        requester_session_id="session-same",
        requester_session_key="key-same",
        requester_turn_id="turn-same",
        tool_call_id="call-same",
        origin_platform="telegram",
        origin_chat_id="4242",
        origin_thread_id="7",
        now=now,
    )
    claimed = store.claim_next_delivery(
        owner="worker",
        telegram_chat_id="4242",
        telegram_thread_id="7",
        lease_seconds=30,
        now=now,
        request_id=record.request_id,
    )
    assert claimed is not None

    adapter = _make_adapter()
    adapter._send_human_approval_origin = AsyncMock()
    adapter._bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=89)
    )
    await adapter._deliver_human_approval(
        store,
        claimed,
        owner="worker",
        target_chat="4242",
        target_thread="7",
    )

    updated = store.get_request(record.request_id)
    assert updated.origin_delivery == "same_as_telegram"
    assert updated.telegram_delivery == "delivered"
    adapter._send_human_approval_origin.assert_not_awaited()
    adapter._bot.send_message.assert_awaited_once()


def test_target_falls_back_to_official_telegram_home_channel():
    home = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="4242",
        name="Approvals",
        thread_id="7",
    )
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", home_channel=home)
    )
    with patch("hermes_cli.config.cfg_get", return_value={}):
        assert adapter._human_approval_target() == ("4242", "7")
