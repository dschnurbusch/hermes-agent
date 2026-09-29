"""Synthetic Buzz cron delivery through the durable receipt and reply boundary.

Only the transport is fake; the queue, receipt, SQLite ledger, route and turn lease are real.
"""
import asyncio
import json
from collections import OrderedDict
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cron import delivery_queue, scheduler_delivery
from cron.reply_provenance import record_confirmed_delivery
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import AsyncSessionStore, SessionSource, SessionStore, build_session_key
from gateway.session_cron_reply import private_cron_reply_route
from hermes_state_cron_delivery import cron_reply_reservation
from plugins.platforms.buzz.adapter import BuzzAdapter

OWNER = "a" * 64
STRANGER = "b" * 64
CHANNEL = "test-channel"


def _completed(db, sid):
    db.create_session(sid, source="cron")
    db.append_message(sid, role="user", content="private task input")
    db.append_message(sid, role="assistant", content="full private report")
    db.end_session(sid, "cron_complete")


async def _busy_runner_event(runner, event, monkeypatch, calls):
    """Enter the real runner busy fork, keeping transport and agent execution inert."""
    monkeypatch.setattr(runner, "_hm_admit_event", AsyncMock(return_value=(event, event.source, False)))
    monkeypatch.setattr(runner, "_hm_estop_gate", lambda *args: None)
    monkeypatch.setattr(runner, "_hm_pending_reply_intercepts", AsyncMock(return_value=None))
    monkeypatch.setattr(runner, "_session_key_for_source", lambda source: build_session_key(
        source, thread_sessions_per_user=runner.session_store.config.thread_sessions_per_user))
    monkeypatch.setattr(runner, "_hm_evict_idle_stale_agent", lambda *args: None)
    monkeypatch.setattr(runner, "_hm_evict_reaped_agent", lambda *args: None)
    monkeypatch.setattr(runner, "_is_session_running", lambda *args: True)
    monkeypatch.setattr(runner, "_check_slash_access", lambda *args: None)
    monkeypatch.setattr(runner, "_interrupt_and_clear_session", AsyncMock(
        side_effect=lambda *args, **kwargs: calls.append(("stop", event.source.user_id))))
    monkeypatch.setattr(runner, "_effective_busy_input_mode", lambda *args: "queue")
    monkeypatch.setattr(runner, "_hm_busy_telegram_grace_queue", lambda *args: False)
    monkeypatch.setattr(runner, "_queue_or_replace_pending_event", lambda *args: calls.append(
        ("queue", event.source.user_id)))
    return await runner._handle_message(event)


def _deliver(monkeypatch, job, *, queued=False, chat_type_for_chat=None):
    """Real scheduler target/receipt path, with only network transport replaced."""
    from gateway import delivery as router_module
    from gateway import config as config_module
    from cron import scheduler as scheduler_module

    sent = []
    adapter = SimpleNamespace(
        supports_inchannel_continuable=False,
        outbound_reply_anchor=lambda result, **kw: ("message", result.message_id),
        scope_id_for_chat=lambda chat: "",
        chat_type_for_chat=chat_type_for_chat or (lambda chat: "group" if chat == CHANNEL else None),
    )
    config = GatewayConfig()
    config.platforms[Platform("buzz")] = PlatformConfig(enabled=True)
    monkeypatch.setattr(config_module, "load_gateway_config", lambda: config)
    monkeypatch.setattr(scheduler_module, "load_config", lambda: {"cron": {"wrap_response": False}})

    class Router:
        async def _deliver_to_platform(self, target, text, metadata, transport=None):
            sent.append((target, text, metadata))
            return SendResult(success=True, message_id="signed-post")

    monkeypatch.setattr(router_module, "DeliveryRouter", lambda *args, **kwargs: Router())
    def thread_safe(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as exc:
            future.set_exception(exc)
        return future
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", thread_safe)
    loop = SimpleNamespace(is_running=lambda: True)
    def send(active_job, text, failure=False):
        return scheduler_delivery._deliver_result(
            active_job, text, adapters={Platform("buzz"): adapter}, loop=loop,
            for_failure=failure)
    if queued:
        assert delivery_queue.drain(send) == 1
    else:
        assert send(job, "full private report") is None
    assert len(sent) == 1
    return sent


async def _authenticated_buzz_reply(monkeypatch, pubkey, *, root=None, parent="signed-post"):
    """Exercise Buzz's signed-event ingress gates without a network relay."""
    adapter = BuzzAdapter(PlatformConfig(enabled=True, extra={"relay_url": "https://relay.test"}))
    adapter._self_pubkey = "c" * 64
    adapter._display_name = "Test bot"
    adapter._channel_state[CHANNEL] = adapter._new_channel_state("group")
    adapter.set_authorization_check(lambda *args: pubkey in {OWNER, STRANGER})
    adapter._allowed_pubkeys = {OWNER, STRANGER}
    adapter._resolve_user_name = AsyncMock(return_value="Owner")
    captured = []
    async def capture(**kwargs):
        captured.append(kwargs)
    adapter._dispatch_message = capture
    state = adapter._channel_state[CHANNEL]
    await adapter._handle_event(CHANNEL, state, {
        "id": "signed-post", "pubkey": adapter._self_pubkey, "content": "full private report",
        "created_at": 1, "kind": 9, "tags": [["h", CHANNEL]] + ([["e", root, "", "root"]] if root else []),
    })
    await adapter._handle_event(CHANNEL, state, {
        "id": "owner-reply", "pubkey": pubkey, "content": "continue",
        "created_at": 2, "kind": 9,
        "tags": [["h", CHANNEL]] + ([["p", adapter._self_pubkey]] if parent != "signed-post" else []) +
                ([["e", root, "", "root"]] if root else []) + [["e", parent, "", "reply"]],
    })
    assert len(captured) == (1 if pubkey in {OWNER, STRANGER} else 0)
    if not captured:
        return None, adapter
    inbound = captured[0]
    source = SessionSource(platform=Platform("buzz"), chat_id=inbound["chat_id"],
                           chat_type=inbound["chat_type"], user_id=inbound["user_id"],
                           thread_id=inbound["thread_id"], message_id=inbound["message_id"])
    event = MessageEvent(text=inbound["text"], source=source, message_id=inbound["message_id"],
                         raw_message=inbound["raw_message"],
                         reply_to_message_id=inbound["reply_to_message_id"])
    return event, adapter


@pytest.mark.asyncio
async def test_captured_origin_scheduler_receipt_persistence_owner_thread(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = store._db
    sid = "cron_private_20260928_120000"
    _completed(db, sid)
    monkeypatch.setattr(delivery_queue, "DELIVERY_DB", tmp_path / "receipts.db")
    job = {"id": "test-origin", "deliver": "origin", "execution_id": "exec-origin",
           "_cron_run_session_id": sid,
           "origin": {"platform": "buzz", "chat_id": CHANNEL, "user_id": OWNER,
                      "chat_type": "group"}}
    delivery_queue.enqueue("exec-origin", job, "full private report")
    sent = await asyncio.to_thread(_deliver, monkeypatch, job, queued=True)
    receipt = delivery_queue.get_status("exec-origin")
    assert receipt["status"] == "delivered"
    candidate = json.loads(receipt["provenance_json"])
    assert candidate and candidate["owner_user_id"] == OWNER
    assert sent[0][1] == "full private report"
    assert record_confirmed_delivery(candidate)
    assert db.resolve_cron_delivery_reply(profile_name="default", platform="buzz", scope_id="",
        chat_id=CHANNEL, anchor_kind="message", anchor_id="signed-post", user_id=STRANGER) is None

    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = db
    runner._session_sources = OrderedDict()
    event, adapter = await _authenticated_buzz_reply(monkeypatch, OWNER)
    assert event is not None
    runner._intake_adapter_for = lambda source: adapter
    source = event.source
    resolved = await runner._hmwa_resolve_session(event, source)
    assert resolved and resolved[1].session_id == sid
    assert store.lookup_by_session_key(resolved[2]).session_id == sid
    assert getattr(event, "_cron_reply_reservation", None)
    assert [row["content"] for row in store.load_transcript(sid)] == [
        "private task input", "full private report"]
    calls = []
    stranger_source = SessionSource(platform=Platform("buzz"), chat_id=CHANNEL,
        chat_type="group", user_id=STRANGER, thread_id="signed-post")
    for text in ("/stop", "plain queued message"):
        denied = await _busy_runner_event(runner, MessageEvent(text=text, source=stranger_source),
                                          monkeypatch, calls)
        assert "not available" in denied
    assert calls == []
    assert [row["content"] for row in store.load_transcript(sid)] == [
        "private task input", "full private report"]
    await _busy_runner_event(runner, MessageEvent(text="/stop", source=source), monkeypatch, calls)
    assert calls == [("stop", OWNER)]
    from gateway.platforms.base import _thread_metadata_for_source
    outbound = []
    async def fake_send(args, content, mentions):
        outbound.append((args, content))
        return 0, "", ""
    adapter._run_message_send = fake_send
    adapter._mention_pubkeys_for = AsyncMock(return_value=[])
    adapter._send_result = lambda *args: SendResult(success=True, message_id="answer")
    assert (await adapter.send(CHANNEL, "continued answer", metadata=
        _thread_metadata_for_source(resolved[0]))).success
    assert outbound[0][0][-2:] == ["--reply-to", "signed-post"]
    db.release_session_turn_lease(sid, event._cron_reply_reservation[1])
    stranger = SessionSource(platform=Platform("buzz"), chat_id=CHANNEL,
                              chat_type="group", user_id=STRANGER, thread_id="signed-post")
    assert build_session_key(stranger) == resolved[2]
    for other in (stranger, SessionSource(platform=Platform("buzz"), chat_id=CHANNEL,
                    chat_type="group", user_id=STRANGER, thread_id=None)):
        for parent in ("signed-post", None):
            ev = MessageEvent(text="intrude", source=other, message_id="stranger",
                              reply_to_message_id=parent)
            result = await runner._hmwa_resolve_session(ev, other)
            assert result is None or result[1].session_id != sid
    for parent in ("owner-reply", None):
        ev = MessageEvent(text="more", source=source, message_id="owner-next",
                          reply_to_message_id=parent)
        assert (await runner._hmwa_resolve_session(ev, source))[1].session_id == sid
    unrelated = SessionSource(platform=Platform("buzz"), chat_id=CHANNEL,
                              chat_type="group", user_id=OWNER)
    assert (await runner._hmwa_resolve_session(MessageEvent(text="flat", source=unrelated),
                                                unrelated))[1].session_id != sid
    db.close()
    restored = SessionStore(tmp_path / "sessions", GatewayConfig())
    runner.session_store = restored
    runner._async_session_store = AsyncSessionStore(restored)
    assert (await runner._hmwa_resolve_session(MessageEvent(text="again", source=source),
                                                source))[1].session_id == sid
    assert await runner._resume_target_allowed(source, sid)
    assert not await runner._resume_target_allowed(stranger, sid, allow_override=True)
    assert not await runner._resume_target_allowed(unrelated, sid, allow_override=True)
    unclaimed = "cron_unclaimed_20260928"
    _completed(restored._db, unclaimed)
    assert not await runner._resume_target_allowed(source, unclaimed, allow_override=True)
    assert await runner._cron_command_route_allowed(source,
        restored.lookup_by_session_key(resolved[2]))
    # /new severs the route; a spent anchor cannot revive the cron transcript.
    current = restored.lookup_by_session_key(resolved[2])
    fresh = restored.reset_session(resolved[2])
    assert fresh.session_id != current.session_id
    assert (await runner._hmwa_resolve_session(MessageEvent(text="new", source=source,
        reply_to_message_id="signed-post"), source))[1].session_id != sid
    restored._db.close()


@pytest.mark.asyncio
async def test_public_explicit_buzz_delivery_two_signed_participants(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    config = GatewayConfig(thread_sessions_per_user=True)
    store = SessionStore(tmp_path / "sessions", config)
    db = store._db
    sid = "cron_public_20260928"
    _completed(db, sid)
    monkeypatch.setattr(delivery_queue, "DELIVERY_DB", tmp_path / "receipts.db")
    job = {"id": "public", "deliver": f"local,buzz:{CHANNEL}", "public_thread_continuation": True,
           "origin": None,
           "execution_id": "exec-public", "_cron_run_session_id": sid}
    delivery_queue.enqueue("exec-public", job, "full report")
    await asyncio.to_thread(_deliver, monkeypatch, job, queued=True)
    candidate = json.loads(delivery_queue.get_status("exec-public")["provenance_json"])
    assert candidate["access_policy"] == "public_buzz_thread"
    assert candidate["owner_user_id"] == ""
    assert record_confirmed_delivery(candidate)
    unauthorized, _ = await _authenticated_buzz_reply(monkeypatch, "d" * 64)
    assert unauthorized is None  # Buzz admission is required, not merely a matching thread ID.
    assert db.resolve_cron_delivery_reply(profile_name="other-profile", platform="buzz",
        scope_id="", chat_id=CHANNEL, anchor_kind="message", anchor_id="signed-post",
        user_id=OWNER, session_key="agent:main:buzz:group:test-channel:signed-post",
        thread_id="signed-post") is None

    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = db
    runner._session_sources = OrderedDict()
    first, adapter = await _authenticated_buzz_reply(monkeypatch, OWNER)
    runner._intake_adapter_for = lambda source: adapter
    resolved = await runner._hmwa_resolve_session(first, first.source)
    assert resolved and resolved[1].session_id == sid
    first_key = resolved[2]
    calls = []
    second_busy, _ = await _authenticated_buzz_reply(monkeypatch, STRANGER)
    assert second_busy is not None
    await _busy_runner_event(runner, MessageEvent(text="join", source=second_busy.source),
                             monkeypatch, calls)
    await _busy_runner_event(runner, MessageEvent(text="/stop", source=second_busy.source),
                             monkeypatch, calls)
    assert calls == [("queue", STRANGER), ("stop", STRANGER)]
    db.release_session_turn_lease(sid, first._cron_reply_reservation[1])
    db.append_message(sid, role="user", content="first participant")
    db.append_message(sid, role="assistant", content="first answer")

    second, adapter = await _authenticated_buzz_reply(monkeypatch, STRANGER)
    runner._intake_adapter_for = lambda source: adapter
    resumed = await runner._hmwa_resolve_session(second, second.source)
    assert resumed and resumed[1].session_id == sid and resumed[2] == first_key
    assert [m["content"] for m in store.load_transcript(sid)][-2:] == [
        "first participant", "first answer"]
    db.append_message(sid, role="user", content="second participant")
    db.append_message(sid, role="assistant", content="second answer")
    for other in (
        SessionSource(platform=Platform("buzz"), chat_id=CHANNEL, chat_type="group",
                      user_id="d" * 64, thread_id="different-root"),
        SessionSource(platform=Platform("buzz"), chat_id=CHANNEL, chat_type="group",
                      user_id="d" * 64),
        SessionSource(platform=Platform("buzz"), chat_id="other-channel", chat_type="group",
                      user_id="d" * 64, thread_id="signed-post"),
    ):
        result = await runner._hmwa_resolve_session(
            MessageEvent(text="other", source=other, message_id="other",
                         reply_to_message_id="signed-post"), other)
        assert result is None or result[1].session_id != sid
    db.close()
    restored = SessionStore(tmp_path / "sessions", config)
    runner.session_store = restored
    runner._async_session_store = AsyncSessionStore(restored)
    assert (await runner._hmwa_resolve_session(
        MessageEvent(text="after restart", source=second.source), second.source))[1].session_id == sid
    assert [m["content"] for m in restored.load_transcript(sid)][-2:] == [
        "second participant", "second answer"]
    duplicates = ("cron_duplicate_a", "cron_duplicate_b")
    for candidate_id in duplicates:
        _completed(restored._db, candidate_id)
        assert restored._db.record_cron_delivery(profile_name="default", platform="buzz",
            scope_id="", chat_id=CHANNEL, anchor_kind="message", anchor_id="duplicate-post",
            cron_session_id=candidate_id, job_id="public", public_thread=True)
    assert restored._db.resolve_cron_delivery_reply(profile_name="default", platform="buzz",
        scope_id="", chat_id=CHANNEL, anchor_kind="message", anchor_id="duplicate-post",
        user_id=OWNER, session_key="agent:main:buzz:group:test-channel:duplicate-post",
        thread_id="duplicate-post") is None
    fresh = restored.reset_session(first_key)
    assert fresh.session_id != sid
    assert (await runner._hmwa_resolve_session(MessageEvent(text="reset", source=second.source,
        reply_to_message_id="signed-post"), second.source))[1].session_id != sid
    restored._db.close()


@pytest.mark.asyncio
async def test_busy_pending_cron_route_denies_both_and_ordinary_approval_survives(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform("buzz"), chat_id=CHANNEL, chat_type="group",
                           user_id=OWNER, thread_id="pending-root")
    key = build_session_key(source)
    ordinary = store.get_or_create_session(source)
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    calls = []
    event = MessageEvent(text="/approve", source=source)
    await _busy_runner_event(runner, event, monkeypatch, calls)
    runner._hm_pending_reply_intercepts = AsyncMock(return_value="approved")
    assert await runner._handle_message(event) == "approved"

    sid = "cron_pending_busy"
    _completed(store._db, sid)
    assert store._db.record_cron_delivery(profile_name="default", platform="buzz", scope_id="",
        chat_id=CHANNEL, anchor_kind="message", anchor_id="pending-root", cron_session_id=sid,
        job_id="pending", owner_user_id=OWNER)
    assert store.switch_session(key, sid, expected_session_id=ordinary.session_id)
    for participant in (OWNER, STRANGER):
        current_source = SessionSource(platform=Platform("buzz"), chat_id=CHANNEL,
            chat_type="group", user_id=participant, thread_id="pending-root")
        denied = await _busy_runner_event(runner, MessageEvent(text="/stop", source=current_source),
                                          monkeypatch, calls)
        assert "not available" in denied
    public_sid = "cron_public_pending_busy"
    _completed(store._db, public_sid)
    assert store._db.record_cron_delivery(profile_name="default", platform="buzz", scope_id="",
        chat_id=CHANNEL, anchor_kind="message", anchor_id="pending-public", cron_session_id=public_sid,
        job_id="pending-public", public_thread=True)
    public_source = SessionSource(platform=Platform("buzz"), chat_id=CHANNEL, chat_type="group",
                                  user_id=STRANGER, thread_id="pending-public")
    public_key = build_session_key(public_source)
    public_ordinary = store.get_or_create_session(public_source)
    assert store.switch_session(public_key, public_sid, expected_session_id=public_ordinary.session_id)
    for participant in (OWNER, STRANGER):
        current_source = SessionSource(platform=Platform("buzz"), chat_id=CHANNEL,
            chat_type="group", user_id=participant, thread_id="pending-public")
        denied = await _busy_runner_event(runner, MessageEvent(text="/stop", source=current_source),
                                          monkeypatch, calls)
        assert "not available" in denied
    assert calls == []
    store._db.close()


@pytest.mark.asyncio
async def test_queued_public_delivery_after_adapter_state_restore(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    sid = "cron_queued_restored"
    _completed(store._db, sid)
    monkeypatch.setattr(delivery_queue, "DELIVERY_DB", tmp_path / "receipts.db")
    cursor = tmp_path / "buzz-cursors.json"
    original = BuzzAdapter(PlatformConfig(enabled=True, extra={"relay_url": "https://relay.test"}))
    original._self_pubkey = "c" * 64
    original._channel_state[CHANNEL] = original._new_channel_state("group")
    original._channel_meta[CHANNEL] = {"channel_id": CHANNEL, "name": "Reports"}
    monkeypatch.setattr(original, "_cursor_path", lambda: cursor)
    original._save_cursors()
    restored = BuzzAdapter(PlatformConfig(enabled=True, extra={"relay_url": "https://relay.test"}))
    restored._self_pubkey = original._self_pubkey
    monkeypatch.setattr(restored, "_cursor_path", lambda: cursor)
    restored._load_cursors()
    assert restored._restore_channel_state(CHANNEL, "group")
    restored._channel_meta[CHANNEL] = {"channel_id": CHANNEL, "name": "Reports"}
    restored._mark_connected()
    assert restored.chat_type_for_chat(CHANNEL) is None  # restored cursors alone are not proof
    async def inventory(args, *, input_text=None):
        if args == ["dms", "list"]:
            return 0, "[]", ""
        if args == ["channels", "list"]:
            return 0, json.dumps([{"channel_id": CHANNEL, "name": "Reports"}]), ""
        raise AssertionError(args)
    monkeypatch.setattr(restored, "_run_cli", inventory)
    await restored._discover_dms(seed=True)
    assert restored.chat_type_for_chat(CHANNEL) == "group"
    job = {"id": "queued", "deliver": f"local,buzz:{CHANNEL}", "origin": None,
           "public_thread_continuation": True, "execution_id": "exec-queued",
           "_cron_run_session_id": sid}
    delivery_queue.enqueue("exec-queued", job, "report")
    await asyncio.to_thread(_deliver, monkeypatch, job, queued=True,
                            chat_type_for_chat=restored.chat_type_for_chat)
    candidate = json.loads(delivery_queue.get_status("exec-queued")["provenance_json"])
    assert candidate["access_policy"] == "public_buzz_thread"
    assert candidate["thread_root"] == "signed-post"
    store._db.close()


@pytest.mark.asyncio
async def test_existing_buzz_nip10_root_requires_direct_post_then_keeps_root(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = store._db
    sid = "cron_existing_root"
    _completed(db, sid)
    job = {"id": "existing", "deliver": f"local,buzz:{CHANNEL}",
           "public_thread_continuation": True,
           "origin": {"platform": "buzz", "chat_id": CHANNEL, "chat_type": "group",
                      "thread_id": "original-root", "user_id": OWNER},
           "execution_id": "exec-existing", "_cron_run_session_id": sid}
    await asyncio.to_thread(_deliver, monkeypatch, job)
    candidate = job["_cron_delivery_provenance"]
    assert (candidate["anchor_id"], candidate["thread_root"]) == ("signed-post", "original-root")
    assert record_confirmed_delivery(candidate)
    first, adapter = await _authenticated_buzz_reply(monkeypatch, OWNER, root="original-root")
    assert first.source.thread_id == "original-root"
    assert first.reply_to_message_id == "signed-post"
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = db
    runner._session_sources = OrderedDict()
    runner._intake_adapter_for = lambda _source: adapter
    wrong_root = SessionSource(platform=Platform("buzz"), chat_id=CHANNEL,
        chat_type="group", user_id=OWNER, thread_id="sibling-root")
    assert db.resolve_cron_delivery_reply(profile_name="default", platform="buzz",
        scope_id="", chat_id=CHANNEL, anchor_kind="message", anchor_id="signed-post",
        user_id=OWNER, session_key=build_session_key(wrong_root), thread_id="sibling-root") is None
    sibling, _ = await _authenticated_buzz_reply(monkeypatch, OWNER,
        root="original-root", parent="unrelated-sibling")
    assert sibling is not None and sibling.reply_to_message_id == "unrelated-sibling"
    assert (await runner._hmwa_resolve_session(sibling, sibling.source))[1].session_id != sid
    adopted = await runner._hmwa_resolve_session(first, first.source)
    assert adopted and adopted[1].session_id == sid
    assert adopted[2].endswith(":original-root")
    db.release_session_turn_lease(sid, first._cron_reply_reservation[1])
    subsequent = SessionSource(platform=Platform("buzz"), chat_id=CHANNEL,
        chat_type="group", user_id=STRANGER, thread_id="original-root")
    assert (await runner._hmwa_resolve_session(
        MessageEvent(text="later", source=subsequent, message_id="later",
                     reply_to_message_id="unrelated-sibling"), subsequent))[1].session_id == sid
    outbound = []
    async def fake_send(args, content, mentions):
        outbound.append(args)
        return 0, "", ""
    adapter._run_message_send = fake_send
    adapter._mention_pubkeys_for = AsyncMock(return_value=[])
    adapter._send_result = lambda *args: SendResult(success=True, message_id="answer")
    from gateway.platforms.base import _thread_metadata_for_source
    assert (await adapter.send(CHANNEL, "continued answer", metadata=
        _thread_metadata_for_source(first.source))).success
    assert outbound[0][-2:] == ["--reply-to", "original-root"]
    db.close()


def test_explicit_buzz_target_sends_but_never_claims_owner(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    sid = "cron_private_20260928_130000"
    _completed(store._db, sid)
    job = {"id": "test-explicit", "deliver": f"buzz:{CHANNEL}", "execution_id": "exec-explicit",
           "_cron_run_session_id": sid}
    sent = _deliver(monkeypatch, job)
    assert sent and "_cron_delivery_provenance" not in job
    assert store._db.resolve_cron_delivery_reply(profile_name="default", platform="buzz",
        scope_id="", chat_id=CHANNEL, anchor_kind="message", anchor_id="signed-post",
        user_id=OWNER) is None
    store._db.close()


@pytest.mark.parametrize("platform", ["buzz", "slack", "google_chat"])
def test_shared_thread_never_adopts_even_with_direct_anchor(platform):
    owner = SessionSource(platform=Platform(platform), chat_id="shared", chat_type="group",
                          user_id=OWNER, thread_id="cron-thread")
    stranger = SessionSource(platform=Platform(platform), chat_id="shared", chat_type="group",
                             user_id=STRANGER, thread_id="cron-thread")
    shared_key = build_session_key(owner, profile="default")
    shared_config = GatewayConfig()
    isolated_config = GatewayConfig()
    isolated_config.thread_sessions_per_user = True
    if platform == "buzz":
        assert shared_key == build_session_key(stranger, profile="default")
        assert private_cron_reply_route(owner, shared_key, "default", config=shared_config)
    else:
        assert shared_key == build_session_key(stranger, profile="default")
        assert not private_cron_reply_route(owner, shared_key, "default", config=shared_config)
    isolated_key = build_session_key(owner, thread_sessions_per_user=True, profile="default")
    assert (isolated_key != build_session_key(stranger, thread_sessions_per_user=True,
                                              profile="default"))
    assert private_cron_reply_route(owner, isolated_key, "default", config=isolated_config) == (platform == "buzz")
    # Even an owner-suffixed key alone cannot authorize fallback routes after restart.
    if platform != "buzz":
        assert not private_cron_reply_route(stranger, isolated_key, "default", config=isolated_config)


@pytest.mark.asyncio
async def test_legacy_adopted_shared_thread_fails_closed_after_restart(tmp_path):
    from gateway.run import GatewayRunner

    sessions = tmp_path / "sessions"
    store = SessionStore(sessions, GatewayConfig())
    sid = "cron_private_legacy_20260928"
    _completed(store._db, sid)
    owner = SessionSource(platform=Platform("buzz"), chat_id=CHANNEL,
                          chat_type="group", user_id=OWNER, thread_id="signed-post")
    key = build_session_key(owner)
    current = store.get_or_create_session(owner)
    assert store._db.record_cron_delivery(
        profile_name="default", platform="buzz", scope_id="", chat_id=CHANNEL,
        anchor_kind="message", anchor_id="signed-post", cron_session_id=sid,
        job_id="legacy", owner_user_id=OWNER)
    claimed = store._db.resolve_cron_delivery_reply(
        profile_name="default", platform="buzz", scope_id="", chat_id=CHANNEL,
        anchor_kind="message", anchor_id="signed-post", user_id=OWNER)
    assert claimed
    # Simulate the old consumed marker with no owner route binding.
    store._db._execute_write(lambda conn: (
        conn.execute("INSERT INTO cron_delivery_consumed (cron_session_id, consumed_at) VALUES (?, ?)",
                     (sid, 1.0)),
        conn.execute("DELETE FROM cron_delivery_provenance WHERE cron_session_id=?", (sid,)),
    ))
    store._db.release_session_turn_lease(sid, claimed[1])
    assert store.switch_session(key, sid, expected_session_id=current.session_id)
    store._db.close()

    restored = SessionStore(sessions, GatewayConfig())
    runner = object.__new__(GatewayRunner)
    runner.session_store = restored
    runner._async_session_store = AsyncSessionStore(restored)
    runner._session_db = restored._db
    runner._session_sources = OrderedDict()
    runner._intake_adapter_for = lambda _source: SimpleNamespace(
        inbound_reply_anchor=lambda event: ("message", "signed-post")
        if event.reply_to_message_id else None)
    for source in (owner, SessionSource(platform=Platform("buzz"), chat_id=CHANNEL,
                           chat_type="group", user_id=STRANGER, thread_id="signed-post")):
        for anchor in ("signed-post", None):
            event = MessageEvent(text="more", source=source, message_id="next",
                                 reply_to_message_id=anchor)
            result = await runner._hmwa_resolve_session(event, source)
            assert result is None or result[1].session_id != sid
    restored._db.close()


@pytest.mark.asyncio
async def test_queued_second_turn_does_not_inherit_spent_reservation(tmp_path):
    from gateway.run import GatewayRunner

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = store._db
    sid = "cron_private_20260928_140000"
    _completed(db, sid)
    assert db.record_cron_delivery(
        profile_name="default", platform="buzz", scope_id="", chat_id=CHANNEL,
        anchor_kind="message", anchor_id="post", cron_session_id=sid,
        job_id="test", owner_user_id=OWNER)
    source = SessionSource(platform=Platform("buzz"), chat_id=CHANNEL,
                           chat_type="group", user_id=OWNER, thread_id="post")
    key = build_session_key(source)
    reserved = db.resolve_cron_delivery_reply(
        profile_name="default", platform="buzz", scope_id="", chat_id=CHANNEL,
        anchor_kind="message", anchor_id="post", user_id=OWNER,
        session_key=key, thread_id="post")
    assert reserved
    assert db.commit_cron_reply_reservation(sid, reserved[1],
        profile_name="default", platform="buzz", scope_id="", chat_id=CHANNEL,
        session_key=key, thread_id="post", user_id=OWNER)
    first_holder = "pid=999999:turn=first:platform=buzz"
    assert db.transfer_cron_reply_reservation(sid, reserved[1], first_holder)
    db.release_session_turn_lease(sid, first_holder)
    # The first turn's context is still live when the in-band queued turn recurses.
    token = cron_reply_reservation.set(reserved)
    try:
        context = SimpleNamespace(
            source=source, session_id=sid, session_key=key, run_generation=1,
            _interrupt_depth=0, history=[], _status_thread_metadata=None,
            context_prompt="", result_holder=[None])
        runner = object.__new__(GatewayRunner)
        runner.session_store = store
        runner._MAX_INTERRUPT_DEPTH = 8
        runner._delivery_adapter_for = Mock(return_value=None)
        runner._is_goal_continuation_event = Mock(return_value=False)
        runner._session_key_for_source = Mock(return_value=key)
        runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="second")
        runner._refresh_agent_cache_message_count = AsyncMock()
        runner._intake_adapter_for = Mock(return_value=None)

        async def second_turn(**kwargs):
            assert cron_reply_reservation.get() is None
            second_holder = "pid=999999:turn=second:platform=buzz"
            assert db.acquire_session_turn_lease(sid, second_holder, wait_seconds=0)
            assert not db.transfer_cron_reply_reservation(sid, reserved[1], "stale")
            db.append_message(sid, role="user", content="second")
            db.append_message(sid, role="assistant", content="second answer")
            db.release_session_turn_lease(sid, second_holder)
            return {"final_response": "second answer", "messages": []}

        runner._run_agent = second_turn
        followup = SimpleNamespace(source=source, message_id="second-id", metadata={},
                                   internal=False, channel_prompt=None, message_type=None)
        result = await GatewayRunner._run_agent_queued_followup(
            runner, context, None, "second", followup, "first answer",
            {"interrupted": True, "messages": []}, None)
        assert result["final_response"] == "second answer"
        assert cron_reply_reservation.get() == reserved
        assert [row["content"] for row in store.load_transcript(sid)][-2:] == [
            "second", "second answer"]
        stranger = SessionSource(platform=Platform("buzz"), chat_id=CHANNEL,
                                  chat_type="group", user_id=STRANGER, thread_id="post")
        denied = await GatewayRunner._run_agent_queued_followup(
            runner, context, None, "intrude", SimpleNamespace(source=stranger, message_id="intruder"),
            "first answer", {"interrupted": True, "messages": []}, None)
        assert denied.get("interrupted") is True
        assert [row["content"] for row in store.load_transcript(sid)][-2:] == [
            "second", "second answer"]
    finally:
        cron_reply_reservation.reset(token)
        db.close()
