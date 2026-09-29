"""Slash transcript reads require the exact adopted cron lane, not a shared chat key."""

import asyncio
from unittest.mock import AsyncMock
from typing import cast

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from plugins.platforms.buzz.adapter import BuzzAdapter
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionSource, SessionStore, build_session_key
from hermes_state import AsyncSessionDB, SessionDB


@pytest.mark.asyncio
@pytest.mark.parametrize("public", [False, True])
async def test_slash_reads_require_cron_owner_even_in_same_channel(tmp_path, public):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = cast(SessionDB, store._db)
    sid = "completed-cron"
    db.create_session(sid, source="cron")
    db.append_message(sid, role="user", content="private prompt")
    db.append_message(sid, role="assistant", content="private answer")
    db.end_session(sid, "cron_complete")
    owner = "a" * 64
    other = "b" * 64
    source = SessionSource(platform=Platform("buzz"), chat_id="shared-channel", chat_type="group",
                           thread_id="post-anchor", user_id=owner)
    assert db.record_cron_delivery(profile_name="default", platform="buzz", scope_id="",
        chat_id="shared-channel", anchor_kind="message", anchor_id="post-anchor",
        cron_session_id=sid, job_id="job", owner_user_id="" if public else owner,
        public_thread=public)
    key = build_session_key(source)
    claimed = db.resolve_cron_delivery_reply(profile_name="default", platform="buzz",
        scope_id="", chat_id="shared-channel", anchor_kind="message", anchor_id="post-anchor",
        user_id=owner, session_key=key, thread_id="post-anchor")
    assert claimed and claimed[0] == sid
    assert db.commit_cron_reply_reservation(sid, claimed[1],
        profile_name="default", platform="buzz", scope_id="", chat_id="shared-channel",
        session_key=key, thread_id="post-anchor", user_id=owner)
    db.release_session_turn_lease(sid, claimed[1])
    await asyncio.to_thread(store.get_or_create_session, source)
    store.switch_session(key, sid)
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner.config = store.config
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = AsyncSessionDB(db)
    adapter = AsyncMock()
    runner._delivery_adapter_for = lambda source: adapter
    owner_entry = store.lookup_by_session_key(key)
    assert await runner._cron_command_route_allowed(source, owner_entry)
    assert await runner._resume_target_allowed(source, sid)

    stranger = SessionSource(platform=Platform("buzz"), chat_id="shared-channel", chat_type="group",
                             thread_id="post-anchor", user_id=other)
    assert build_session_key(stranger) == key
    stranger_entry = await runner.async_session_store.get_or_create_session(stranger)
    assert stranger_entry.session_id == sid
    assert (await runner._cron_command_route_allowed(stranger, stranger_entry)) is public
    assert (await runner._resume_target_allowed(stranger, sid, allow_override=True)) is public
    if public:
        assert (await runner._handle_save_command(MessageEvent(
            text="/save md", source=stranger, message_id="m"))) == "Export complete."
        adapter.send_document.assert_awaited_once()
    else:
        for command, handler in (("/save md", runner._handle_save_command),
                                 ("/branch --here", runner._handle_branch_command),
                                 ("/title", runner._handle_title_command)):
            result = await handler(MessageEvent(text=command, source=stranger, message_id="m"))
            assert "not available" in result
        adapter.send_document.assert_not_awaited()
        assert not await runner._resume_row_visible(stranger, {"id": sid}, allow_all=True)
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "dm"])
async def test_private_buzz_thread_reset_denies_other_pubkey_before_mutation(tmp_path, chat_type):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = cast(SessionDB, store._db)
    sid = "private-cron"
    owner, other = "a" * 64, "b" * 64
    db.create_session(sid, source="cron")
    db.end_session(sid, "cron_complete")
    assert db.record_cron_delivery(
        profile_name="default", platform="buzz", scope_id="", chat_id="shared-chat",
        anchor_kind="message", anchor_id="root", cron_session_id=sid, job_id="job",
        owner_user_id=owner, public_thread=False)
    source = SessionSource(platform=Platform("buzz"), chat_id="shared-chat",
                           chat_type=chat_type, thread_id="root", user_id=owner)
    key = build_session_key(source)
    initial = await asyncio.to_thread(store.get_or_create_session, source)
    adopted = store.adopt_cron_reply(
        key, expected_session_id=initial.session_id,
        profile_name="default", platform="buzz", scope_id="", chat_id="shared-chat",
        anchor_kind="message", anchor_id="root", user_id=owner, thread_id="root")
    assert adopted and adopted[0].session_id == sid
    db.release_session_turn_lease(sid, adopted[1][1])
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner.config = store.config
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_key_for_source = lambda source: build_session_key(source)
    stranger = SessionSource(platform=Platform("buzz"), chat_id="shared-chat",
                             chat_type=chat_type, thread_id="root", user_id=other)
    assert build_session_key(stranger) == key
    assert await runner._cron_command_route_allowed(source, adopted[0])
    assert not await runner._cron_command_route_allowed(stranger, adopted[0])
    runner._interrupt_and_clear_session = AsyncMock()
    denied_event = MessageEvent(text="/new", source=stranger, message_id="other-busy-reset")
    assert "not available" in await runner._busy_new_command(denied_event, key, stranger)
    assert getattr(denied_event, "_reset_route_denied", False)
    runner._interrupt_and_clear_session.assert_not_awaited()
    assert "not available" in await runner._handle_reset_command(
        MessageEvent(text="/new", source=stranger, message_id="other-reset"))
    current = store.lookup_by_session_key(key)
    assert current is not None and current.session_id == sid
    assert db.owns_cron_reply_route(
        sid, session_key=key, profile_name="default", platform="buzz", scope_id="",
        chat_id="shared-chat", thread_id="root", user_id=owner)
    db.close()


@pytest.mark.asyncio
async def test_denied_busy_buzz_reset_preserves_active_guard():
    adapter = BuzzAdapter.__new__(BuzzAdapter)
    guard = asyncio.Event()
    adapter._active_sessions = {"lane": guard}
    adapter._dispatch_inline_reply = AsyncMock(side_effect=lambda event, **_: setattr(event, "_reset_route_denied", True))
    adapter.cancel_session_processing = AsyncMock()
    adapter._drain_pending_after_session_command = AsyncMock()
    adapter._discard_text_debounce = AsyncMock()
    source = SessionSource(platform=Platform("buzz"), chat_id="chat", chat_type="dm", user_id="a" * 64)
    await adapter._dispatch_active_session_command(MessageEvent(text="/new", source=source), "lane", "new")
    assert adapter._active_sessions["lane"] is guard
    adapter.cancel_session_processing.assert_not_awaited()
    adapter._drain_pending_after_session_command.assert_not_awaited()
    adapter._discard_text_debounce.assert_not_awaited()


@pytest.mark.asyncio
async def test_unclaimed_cron_dm_cannot_export_or_resume(tmp_path):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = cast(SessionDB, store._db)
    db.create_session("unclaimed-cron", source="cron")
    db.append_message("unclaimed-cron", role="assistant", content="private result")
    db.end_session("unclaimed-cron", "cron_complete")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="dm", chat_type="dm", user_id="owner")
    entry = await asyncio.to_thread(store.get_or_create_session, source)
    entry.session_id = "unclaimed-cron"
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner.config = store.config
    runner._async_session_store = AsyncSessionStore(store)
    runner._async_session_store.get_or_create_session = AsyncMock(return_value=entry)
    runner._session_db = AsyncSessionDB(db)
    assert not await runner._cron_command_route_allowed(source, entry)
    assert not await runner._resume_target_allowed(source, "unclaimed-cron", allow_override=True)
    assert "not available" in await runner._handle_save_command(
        MessageEvent(text="/save md", source=source, message_id="m"))
    db.close()


@pytest.mark.asyncio
async def test_failed_commit_and_rollback_strands_route_without_exposing_transcript(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = cast(SessionDB, store._db)
    sid = "stranded-cron"
    owner, stranger_id = "a" * 64, "b" * 64
    db.create_session(sid, source="cron")
    db.append_message(sid, role="assistant", content="secret cron result")
    db.end_session(sid, "cron_complete")
    assert db.record_cron_delivery(
        profile_name="default", platform="buzz", scope_id="", chat_id="channel",
        anchor_kind="message", anchor_id="root", cron_session_id=sid, job_id="job",
        owner_user_id=owner)
    source = SessionSource(platform=Platform("buzz"), chat_id="channel", chat_type="group",
                           thread_id="root", user_id=owner)
    key = build_session_key(source)
    initial = store.get_or_create_session(source)
    original_write = db._execute_write
    def fail_rollback(callback, *args, **kwargs):
        if getattr(callback, "__name__", "") == "_restore":
            raise OSError("forced rollback failure")
        return original_write(callback, *args, **kwargs)
    monkeypatch.setattr(db, "commit_cron_reply_reservation", lambda *a, **k: False)
    monkeypatch.setattr(db, "_execute_write", fail_rollback)
    assert store.adopt_cron_reply(
        key, expected_session_id=initial.session_id,
        profile_name="default", platform="buzz", scope_id="", chat_id="channel",
        anchor_kind="message", anchor_id="root", user_id=owner, thread_id="root") is None
    assert store.lookup_by_session_key(key).session_id == sid
    assert db.get_session(sid)["source"] == "buzz"  # source is no longer a reliable guard
    assert db.has_unconsumed_cron_delivery(sid)
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner.config = store.config
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = AsyncSessionDB(db)
    runner._session_key_for_source = lambda src: build_session_key(src)
    adapter = AsyncMock()
    runner._delivery_adapter_for = lambda src: adapter
    runner._intake_adapter_for = lambda src: None
    for who in (owner, stranger_id):
        caller = SessionSource(platform=Platform("buzz"), chat_id="channel", chat_type="group",
                               thread_id="root", user_id=who)
        entry = store.get_or_create_session(caller)
        assert await runner._hmwa_resolve_session(
            MessageEvent(text="intrude", source=caller), caller) is None
        assert not await runner._cron_command_route_allowed(caller, entry)
        assert not await runner._resume_target_allowed(caller, sid, allow_override=True)
        assert not await runner._resume_row_visible(caller, {"id": sid}, allow_all=True)
        assert await runner._resume_access_denied_reply(caller, sid, sid, True, True)
        assert "not available" in await runner._handle_save_command(
            MessageEvent(text="/save md", source=caller, message_id="m"))
        assert "not available" in await runner._handle_title_command(
            MessageEvent(text="/title leaked", source=caller, message_id="m"))
    adapter.send_document.assert_not_awaited()
    db.close()
