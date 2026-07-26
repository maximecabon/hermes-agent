"""Behavioral tests for the durable human-approval core tool."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor



def _create(store, *, profile="default", session_id="session-1", key="gate-1", now=1000.0):
    return store.create_request(
        summary="Approve the bounded deployment",
        requested_action="Deploy artifact build-42 to staging",
        reason="The workflow requires an explicit human sign-off",
        evidence=["report.json"],
        expires_in=60,
        idempotency_key=key,
        requester_profile=profile,
        requester_session_id=session_id,
        requester_session_key=f"key:{profile}:{session_id}",
        requester_turn_id="turn-1",
        tool_call_id="call-1",
        origin_platform="discord",
        origin_chat_id="origin-chat",
        origin_thread_id="origin-thread",
        now=now,
    )


def _deliver(store, request_id, *, owner="worker-1", target_chat="4242", target_thread="7", now=1001.0):
    claimed = store.claim_next_delivery(
        owner=owner,
        telegram_chat_id=target_chat,
        telegram_thread_id=target_thread,
        lease_seconds=30,
        now=now,
        request_id=request_id,
    )
    assert claimed is not None
    assert claimed.request_id == request_id
    assert store.complete_delivery(
        request_id,
        owner=owner,
        origin_delivery="delivered",
        telegram_delivery="delivered",
        origin_message_id="origin-message",
        telegram_message_id="telegram-message",
        now=now,
    )


def test_create_is_idempotent_and_correlated(tmp_path):
    from tools.human_approval import HumanApprovalStore

    store = HumanApprovalStore(tmp_path / "approval.db")
    first = _create(store)
    second = _create(store, now=1002.0)

    assert first.request_id == second.request_id
    assert len(first.request_id) == 32  # 128 random bits, hex encoded
    assert first.requester_profile == "default"
    assert first.requester_session_id == "session-1"
    assert first.requester_turn_id == "turn-1"
    assert first.tool_call_id == "call-1"
    assert first.state == "pending"


def test_idempotency_key_cannot_approve_a_different_action(tmp_path):
    from tools.human_approval import HumanApprovalStore, IdempotencyConflict

    store = HumanApprovalStore(tmp_path / "approval.db")
    _create(store)

    try:
        store.create_request(
            summary="Different request",
            requested_action="Delete production data",
            reason="Different scope",
            evidence=[],
            expires_in=60,
            idempotency_key="gate-1",
            requester_profile="default",
            requester_session_id="session-1",
            requester_session_key="key:default:session-1",
            requester_turn_id="turn-2",
            tool_call_id="call-2",
            origin_platform="discord",
            origin_chat_id="origin-chat",
            origin_thread_id="origin-thread",
            now=1003.0,
        )
    except IdempotencyConflict:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("reusing an idempotency key for another action must fail closed")


def test_resolution_is_atomic_chat_scoped_and_fail_closed(tmp_path):
    from tools.human_approval import HumanApprovalStore

    store = HumanApprovalStore(tmp_path / "approval.db")
    request = _create(store)
    _deliver(store, request.request_id)

    wrong_chat = store.resolve_request(
        request.request_id,
        decision="approved",
        decided_by="authorized-user",
        telegram_chat_id="9999",
        telegram_thread_id="7",
        now=1002.0,
    )
    assert wrong_chat.changed is False
    assert wrong_chat.reason == "unauthorized_chat"
    assert store.get_request(request.request_id, now=1002.0).state == "pending"

    approved = store.resolve_request(
        request.request_id,
        decision="approved",
        decided_by="authorized-user",
        telegram_chat_id="4242",
        telegram_thread_id="7",
        now=1003.0,
    )
    duplicate = store.resolve_request(
        request.request_id,
        decision="refused",
        decided_by="authorized-user",
        telegram_chat_id="4242",
        telegram_thread_id="7",
        now=1004.0,
    )

    assert approved.changed is True
    assert approved.state == "approved"
    assert duplicate.changed is False
    assert duplicate.state == "approved"
    assert store.get_request(request.request_id, now=1004.0).state == "approved"


def test_timeout_and_delivery_failure_never_approve(tmp_path):
    from tools.human_approval import HumanApprovalStore

    store = HumanApprovalStore(tmp_path / "approval.db")
    expired = _create(store, key="expires", now=2000.0)
    assert store.expire_due(now=2061.0) == 1
    assert store.get_request(expired.request_id, now=2061.0).state == "expired"

    failed = _create(store, key="delivery", session_id="session-2", now=3000.0)
    claimed = store.claim_next_delivery(
        owner="worker-fail",
        telegram_chat_id="4242",
        telegram_thread_id=None,
        lease_seconds=30,
        now=3001.0,
    )
    assert claimed is not None and claimed.request_id == failed.request_id
    assert store.complete_delivery(
        failed.request_id,
        owner="worker-fail",
        origin_delivery="delivered",
        telegram_delivery="failed",
        failure_state="delivery_failed",
        now=3001.0,
    )
    assert store.get_request(failed.request_id, now=3002.0).state == "delivery_failed"


def test_two_profiles_share_store_without_cross_resolution(tmp_path):
    from tools.human_approval import HumanApprovalStore

    db_path = tmp_path / "approval.db"

    def create(profile):
        return _create(
            HumanApprovalStore(db_path),
            profile=profile,
            session_id=f"session-{profile}",
            key="same-caller-key",
            now=4000.0,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        alpha, beta = list(pool.map(create, ("alpha", "beta")))

    assert alpha.request_id != beta.request_id
    store = HumanApprovalStore(db_path)
    _deliver(store, alpha.request_id, owner="worker-alpha", now=4001.0)
    _deliver(store, beta.request_id, owner="worker-beta", now=4002.0)

    first = store.resolve_request(
        alpha.request_id,
        decision="approved",
        decided_by="owner",
        telegram_chat_id="4242",
        telegram_thread_id="7",
        now=4003.0,
    )
    second = store.resolve_request(
        beta.request_id,
        decision="refused",
        decided_by="owner",
        telegram_chat_id="4242",
        telegram_thread_id="7",
        now=4003.0,
    )

    assert first.state == "approved"
    assert second.state == "refused"
    assert store.get_request(alpha.request_id, now=4003.0).requester_profile == "alpha"
    assert store.get_request(beta.request_id, now=4003.0).requester_profile == "beta"


def test_callback_data_contains_only_opaque_request_id_and_decision():
    from tools.human_approval import parse_callback_data, telegram_button_specs

    request_id = "a" * 32
    specs = telegram_button_specs(request_id)
    assert specs == [
        ("Approuver", f"ha:{request_id}:a"),
        ("Refuser", f"ha:{request_id}:r"),
    ]
    assert parse_callback_data(f"ha:{request_id}:a") == (request_id, "approved")
    assert parse_callback_data(f"ha:{request_id}:r") == (request_id, "refused")
    assert parse_callback_data("ha:not-an-id:a") is None


def test_tool_is_registered_and_in_core_toolsets():
    import model_tools
    from toolsets import resolve_toolset

    assert model_tools.registry.get_entry("request_human_approval") is not None
    for toolset in ("hermes-cli", "hermes-telegram", "hermes-cron", "coding"):
        assert "request_human_approval" in resolve_toolset(toolset)


def test_tool_result_for_refusal_blocks_requested_action(tmp_path, monkeypatch):
    from tools import human_approval as module

    store = module.HumanApprovalStore(tmp_path / "approval.db")

    def resolve_after_create(request_id, **_kwargs):
        store.mark_local_origin_delivered(request_id, now=5000.0)
        claimed = store.claim_next_delivery(
            owner="mock-gateway",
            telegram_chat_id="4242",
            telegram_thread_id=None,
            lease_seconds=30,
            now=5000.0,
        )
        assert claimed is not None
        store.complete_delivery(
            request_id,
            owner="mock-gateway",
            origin_delivery="delivered",
            telegram_delivery="delivered",
            telegram_message_id="m1",
            now=5000.0,
        )
        store.resolve_request(
            request_id,
            decision="refused",
            decided_by="owner",
            telegram_chat_id="4242",
            telegram_thread_id=None,
            now=5000.0,
        )
        return store.get_request(request_id, now=5000.0)

    monkeypatch.setattr(module, "get_shared_store", lambda: store)
    monkeypatch.setattr(module, "wait_for_terminal", resolve_after_create)
    monkeypatch.setattr(module, "_current_context", lambda **_kw: {
        "requester_profile": "default",
        "requester_session_id": "session-tool",
        "requester_session_key": "key-tool",
        "requester_turn_id": "turn-tool",
        "tool_call_id": "call-tool",
        "origin_platform": "local",
        "origin_chat_id": "",
        "origin_thread_id": "",
    })

    result = json.loads(module.request_human_approval_tool(
        summary="Approve staging deployment",
        requested_action="Deploy build-42",
        reason="Explicit human gate",
        evidence=["report.json"],
        expires_in=60,
        idempotency_key="tool-refusal",
        session_id="session-tool",
    ))

    assert result["status"] == "refused"
    assert result["next_action"] == "stop"
    assert result["requested_action"] == "Deploy build-42"
