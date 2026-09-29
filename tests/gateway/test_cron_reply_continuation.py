"""Behavior contract for resuming the real cron transcript from an exact delivered-post reply."""

from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import AsyncSessionStore, SessionSource, SessionStore


def _completed_cron(db, session_id="cron_daily_20260928_120000"):
    db.create_session(session_id, source="cron")
    db.append_message(session_id, role="user", content="collect the full report")
    db.append_message(session_id, role="assistant", content="full cron result with prior reasoning")
    db.end_session(session_id, "cron_complete")
    return session_id


def test_provenance_is_exact_and_refuses_active_or_ambiguous_targets(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    cron_id = _completed_cron(db)
    assert db.record_cron_delivery(
        profile_name="default", platform="buzz", scope_id="", chat_id="channel-a",
        anchor_kind="message", anchor_id="event-1", cron_session_id=cron_id,
        job_id="daily", execution_id="exec-1", owner_user_id="user-a",
    )
    exact = dict(
        profile_name="default", platform="buzz", scope_id="", chat_id="channel-a",
        anchor_kind="message", anchor_id="event-1", user_id="user-a",
    )
    assert db.resolve_cron_delivery_reply(**{**exact, "chat_id": "channel-b"}) is None
    assert db.resolve_cron_delivery_reply(**{**exact, "profile_name": "other"}) is None

    assert db.try_acquire_session_turn_lease(cron_id, "pid=999999;test", ttl_seconds=60)
    assert db.resolve_cron_delivery_reply(**exact) is None
    db.release_session_turn_lease(cron_id, "pid=999999;test")

    assert db.resolve_cron_delivery_reply(**{**exact, "user_id": "user-b"}) is None
    claimed = db.resolve_cron_delivery_reply(**exact)
    assert claimed and claimed[0] == cron_id
    assert not db.try_acquire_session_turn_lease(cron_id, "pid=999999;other", ttl_seconds=60)
    assert db.commit_cron_reply_reservation(cron_id, claimed[1],
        profile_name="default", platform="buzz", scope_id="", chat_id="channel-a",
        session_key="agent:main:buzz:dm:channel-a", thread_id="", user_id="user-a")
    assert db.transfer_cron_reply_reservation(cron_id, claimed[1], "pid=999999;turn")
    db.release_session_turn_lease(cron_id, "pid=999999;turn")
    assert db.resolve_cron_delivery_reply(**exact) is None

    other = _completed_cron(db, "cron_daily_20260928_130000")
    assert db.record_cron_delivery(
        profile_name="default", platform="buzz", scope_id="", chat_id="channel-a",
        anchor_kind="message", anchor_id="ambiguous", cron_session_id=cron_id,
        job_id="daily", execution_id="exec-1", owner_user_id="user-a",
    )
    assert db.record_cron_delivery(
        profile_name="default", platform="buzz", scope_id="", chat_id="channel-a",
        anchor_kind="message", anchor_id="ambiguous", cron_session_id=other,
        job_id="daily", execution_id="exec-2", owner_user_id="user-a",
    )
    # A late duplicate receipt cannot resurrect a spent anchor.
    assert db.resolve_cron_delivery_reply(**exact) is None
    import sqlite3
    with sqlite3.connect(tmp_path / "state.db") as conn:
        rows = conn.execute(
            "SELECT cron_session_id FROM cron_delivery_provenance WHERE anchor_id = ?",
            ("ambiguous",),
        ).fetchall()
    assert rows == [(other,)]
    db.close()


@pytest.mark.asyncio
async def test_buzz_restart_and_failed_cas_releases_reservation(tmp_path):
    from hermes_state import SessionDB
    from gateway.run import GatewayRunner

    path = tmp_path / "state.db"
    db = SessionDB(path)
    cron_id = _completed_cron(db)
    assert db.record_cron_delivery(
        profile_name="default", platform="buzz", scope_id="", chat_id="channel-a",
        anchor_kind="message", anchor_id="post", cron_session_id=cron_id,
        job_id="daily", owner_user_id="user-a",
    )
    db.close()
    reopened = SessionDB(path)
    exact = dict(profile_name="default", platform="buzz", scope_id="", chat_id="channel-a",
                 anchor_kind="message", anchor_id="post", user_id="user-a")
    claimed = reopened.resolve_cron_delivery_reply(**exact)
    assert claimed and claimed[0] == cron_id
    assert reopened.resolve_cron_delivery_reply(**exact) is None
    assert not reopened.try_acquire_session_turn_lease(cron_id, "pid=999999;other")
    runner = object.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace(_db_for_key=lambda _key: reopened)
    event = SimpleNamespace(_cron_reply_reservation=claimed)
    async def fail_switch(*_args):
        return None
    runner._handle_message_with_agent_inner = fail_switch
    await runner._handle_message_with_agent(event, None, "buzz:key", 1)
    assert reopened.try_acquire_session_turn_lease(cron_id, "pid=999999;other")
    reopened.release_session_turn_lease(cron_id, "pid=999999;other")
    reopened.close()

@pytest.mark.asyncio
async def test_failed_adoption_cas_releases_reservation_and_keeps_ordinary_route(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = store._db
    sid = _completed_cron(db)
    assert db.record_cron_delivery(profile_name="default", platform="buzz", scope_id="",
        chat_id="channel-a", anchor_kind="message", anchor_id="post", cron_session_id=sid,
        job_id="daily", owner_user_id="user-a")
    source = SessionSource(platform=Platform("buzz"), chat_id="channel-a", chat_type="dm",
                           user_id="user-a")
    adapter = SimpleNamespace(inbound_reply_anchor=lambda _event: ("message", "post"))
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = db
    runner._intake_adapter_for = lambda _source: adapter
    runner._session_sources = {}
    original_switch = store.switch_session
    def reset_before_cas(key, target, **kwargs):
        store.reset_session(key)
        return original_switch(key, target, **kwargs)
    monkeypatch.setattr(store, "switch_session", reset_before_cas)
    event = MessageEvent(text="continue", source=source, message_id="reply",
                         reply_to_message_id="post")
    resolved = await runner._hmwa_resolve_session(event, source)
    assert resolved is not None and resolved[1].session_id != sid
    assert getattr(event, "_cron_reply_reservation", None) is None
    assert db.try_acquire_session_turn_lease(sid, "pid=999999;probe", ttl_seconds=60)
    db.release_session_turn_lease(sid, "pid=999999;probe")
    assert not db.is_consumed_cron_delivery(sid)
    monkeypatch.setattr(store, "switch_session", original_switch)
    retry = await runner._hmwa_resolve_session(
        MessageEvent(text="retry", source=source, message_id="reply-again",
                     reply_to_message_id="post"), source)
    assert retry and retry[1].session_id == sid
    db.close()


@pytest.mark.parametrize("failure", ["refused", "raised"])
def test_commit_failure_restores_route_lifecycle_and_anchor(tmp_path, monkeypatch, failure):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = store._db
    parent = _completed_cron(db, "cron_parent")
    assert db.record_cron_delivery(profile_name="default", platform="buzz", scope_id="",
        chat_id="channel-a", anchor_kind="message", anchor_id="post", cron_session_id=parent,
        job_id="daily", owner_user_id="user-a")
    descendant = "cron_descendant"
    db.create_session(descendant, source="cron", parent_session_id=parent)
    db.append_message(descendant, role="assistant", content="compressed continuation")
    db.end_session(descendant, "cron_complete")
    db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET end_reason='compression' WHERE id=?", (parent,)))
    source = SessionSource(platform=Platform("buzz"), chat_id="channel-a", chat_type="dm",
                           user_id="user-a")
    entry = store.get_or_create_session(source)
    previous = {sid: db.get_session(sid) for sid in (entry.session_id, descendant)}
    original = db.commit_cron_reply_reservation
    def fail_commit(*args, **kwargs):
        if failure == "raised":
            raise RuntimeError("mocked commit failure")
        return False
    monkeypatch.setattr(db, "commit_cron_reply_reservation", fail_commit)
    kwargs = dict(expected_session_id=entry.session_id, profile_name="default", platform="buzz",
        scope_id="", chat_id="channel-a", anchor_kind="message", anchor_id="post", user_id="user-a")
    assert store.adopt_cron_reply(entry.session_key, **kwargs) is None
    assert store.peek_session_id(entry.session_key) == entry.session_id
    for sid, row in previous.items():
        restored = db.get_session(sid)
        assert (restored["ended_at"], restored["end_reason"]) == (row["ended_at"], row["end_reason"])
    assert db.get_session(parent)["end_reason"] == "compression"
    assert not db.is_consumed_cron_delivery(parent)
    monkeypatch.setattr(db, "commit_cron_reply_reservation", original)
    adopted = store.adopt_cron_reply(entry.session_key, **kwargs)
    assert adopted and adopted[0].session_id == descendant
    assert db.is_consumed_cron_delivery(parent)
    db.close()


@pytest.mark.asyncio
async def test_failed_commit_and_rollback_stranded_route_refused_at_ingress(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    from collections import OrderedDict

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = store._db
    sid = _completed_cron(db, "cron_stranded")
    owner = "a" * 64
    assert db.record_cron_delivery(profile_name="default", platform="buzz", scope_id="",
        chat_id="channel-a", anchor_kind="message", anchor_id="post", cron_session_id=sid,
        job_id="daily", owner_user_id="", public_thread=True, thread_root="post")
    source = SessionSource(platform=Platform("buzz"), chat_id="channel-a", chat_type="group",
                           user_id=owner, thread_id="post")
    entry = store.get_or_create_session(source)
    original_write = db._execute_write
    monkeypatch.setattr(db, "commit_cron_reply_reservation", lambda *a, **kw: False)
    def fail_compensation(fn, *a, **kw):
        if getattr(fn, "__name__", "") == "_restore":
            raise RuntimeError("mocked rollback outage")
        return original_write(fn, *a, **kw)
    monkeypatch.setattr(db, "_execute_write", fail_compensation)
    assert store.adopt_cron_reply(entry.session_key, expected_session_id=entry.session_id,
        profile_name="default", platform="buzz", scope_id="", chat_id="channel-a",
        anchor_kind="message", anchor_id="post", thread_id="post", user_id=owner) is None
    assert store.peek_session_id(entry.session_key) == sid
    assert db.has_unconsumed_cron_delivery(sid)
    assert not db.is_consumed_cron_delivery(sid)

    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = db
    runner._session_sources = OrderedDict()
    runner._intake_adapter_for = lambda _source: SimpleNamespace(inbound_reply_anchor=lambda _event: None)
    for user_id in (owner, "b" * 64):
        incoming = SessionSource(platform=Platform("buzz"), chat_id="channel-a",
            chat_type="group", user_id=user_id, thread_id="post")
        event = MessageEvent(text="unanchored", source=incoming, message_id="later")
        assert await runner._hmwa_resolve_session(event, incoming) is None
    db.close()


def test_commit_receipt_lost_after_durable_spend_keeps_adopted_route(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = store._db
    sid = _completed_cron(db, "cron_receipt_lost")
    assert db.record_cron_delivery(profile_name="default", platform="buzz", scope_id="",
        chat_id="channel-a", anchor_kind="message", anchor_id="post", cron_session_id=sid,
        job_id="daily", owner_user_id="user-a")
    source = SessionSource(platform=Platform("buzz"), chat_id="channel-a", chat_type="dm",
                           user_id="user-a")
    entry = store.get_or_create_session(source)
    original = db.commit_cron_reply_reservation
    def lose_receipt(*args, **kwargs):
        assert original(*args, **kwargs)
        return False
    monkeypatch.setattr(db, "commit_cron_reply_reservation", lose_receipt)
    adopted = store.adopt_cron_reply(entry.session_key,
        expected_session_id=entry.session_id, profile_name="default", platform="buzz",
        scope_id="", chat_id="channel-a", anchor_kind="message", anchor_id="post", user_id="user-a")
    assert adopted and adopted[0].session_id == sid
    assert db.is_consumed_cron_delivery(sid)
    assert store.peek_session_id(entry.session_key) == sid
    db.close()


def test_failed_commit_finishes_rollback_before_reset_wins(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = store._db
    sid = _completed_cron(db, "cron_failed_commit_reset")
    assert db.record_cron_delivery(profile_name="default", platform="buzz", scope_id="",
        chat_id="channel-a", anchor_kind="message", anchor_id="post", cron_session_id=sid,
        job_id="daily", owner_user_id="user-a")
    source = SessionSource(platform=Platform("buzz"), chat_id="channel-a", chat_type="dm",
                           user_id="user-a")
    entry = store.get_or_create_session(source)
    arrived, release = Event(), Event()
    def fail_commit(*_args, **_kwargs):
        arrived.set()
        assert release.wait(5)
        return False
    monkeypatch.setattr(db, "commit_cron_reply_reservation", fail_commit)
    kwargs = dict(expected_session_id=entry.session_id, profile_name="default", platform="buzz",
        scope_id="", chat_id="channel-a", anchor_kind="message", anchor_id="post", user_id="user-a")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            adoption = pool.submit(store.adopt_cron_reply, entry.session_key, **kwargs)
            assert arrived.wait(5)
            reset = pool.submit(store.reset_session, entry.session_key)
            with pytest.raises(TimeoutError):
                reset.result(timeout=0.2)
            release.set()
            assert adoption.result(timeout=5) is None
            fresh = reset.result(timeout=5)
        assert fresh.session_id != sid
        assert store.peek_session_id(entry.session_key) == fresh.session_id
        assert db.get_session(sid)["end_reason"] == "cron_complete"
        assert not db.is_consumed_cron_delivery(sid)
        assert db.resolve_cron_delivery_reply(profile_name="default", platform="buzz", scope_id="",
            chat_id="channel-a", anchor_kind="message", anchor_id="post", user_id="user-a")
    finally:
        release.set()
        db.close()


def test_cron_adoption_route_lock_and_failed_cas_retry(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = store._db
    sid = _completed_cron(db, "cron_concurrent")
    assert db.record_cron_delivery(profile_name="default", platform="buzz", scope_id="",
        chat_id="channel-a", anchor_kind="message", anchor_id="post", cron_session_id=sid,
        job_id="daily", owner_user_id="user-a")
    source = SessionSource(platform=Platform("buzz"), chat_id="channel-a", chat_type="dm",
                           user_id="user-a")
    entry = store.get_or_create_session(source)
    arrived, release = Event(), Event()
    original = store.switch_session
    def paused_switch(*args, **kwargs):
        arrived.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "switch_session", paused_switch)
    kwargs = dict(expected_session_id=entry.session_id, profile_name="default",
        platform="buzz", scope_id="", chat_id="channel-a", anchor_kind="message",
        anchor_id="post", user_id="user-a")
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(store.adopt_cron_reply, entry.session_key, **kwargs)
            assert arrived.wait(5)
            second = pool.submit(store.adopt_cron_reply, entry.session_key, **kwargs)
            reset = pool.submit(store.reset_session, entry.session_key)
            with pytest.raises(TimeoutError):
                reset.result(timeout=0.2)
            release.set()
            adopted = first.result(timeout=5)
            assert second.result(timeout=5) is None
            fresh = reset.result(timeout=5)
        assert adopted and adopted[0].session_id == sid
        assert fresh.session_id != sid
        assert store.peek_session_id(entry.session_key) == fresh.session_id
        assert db.is_consumed_cron_delivery(sid)
        assert store.adopt_cron_reply(entry.session_key, **kwargs) is None
    finally:
        release.set()
        db.close()


@pytest.mark.asyncio
async def test_gateway_dm_direct_reply_switches_to_real_cron_transcript(tmp_path):
    from gateway.run import GatewayRunner

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = store._db
    cron_id = _completed_cron(db)
    assert db.record_cron_delivery(
        profile_name="default", platform="buzz", scope_id="", chat_id="channel-a",
        anchor_kind="message", anchor_id="cron-post", cron_session_id=cron_id,
        job_id="daily", execution_id="exec-1", owner_user_id="user-a",
    )
    source = SessionSource(
        platform=Platform("buzz"), chat_id="channel-a", chat_type="dm",
        user_id="user-a", message_id="reply-1",
    )
    event = MessageEvent(
        text="go deeper", source=source, message_id="reply-1",
        reply_to_message_id="cron-post",
    )
    adapter = SimpleNamespace(
        inbound_reply_anchor=lambda ev: ("message", ev.reply_to_message_id)
        if ev.reply_to_message_id else None)
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = db
    runner._intake_adapter_for = lambda _source: adapter
    runner._session_sources = {}

    resolved = await runner._hmwa_resolve_session(event, source)

    assert resolved is not None
    resolved_source, entry, session_key = resolved
    assert resolved_source is source
    assert entry.session_id == cron_id
    assert store.load_transcript(entry.session_id)[-1]["content"] == "full cron result with prior reasoning"
    assert store.lookup_by_session_key(session_key).session_id == cron_id
    db.close()


@pytest.mark.asyncio
async def test_gateway_flat_message_without_anchor_keeps_normal_route(tmp_path):
    from gateway.run import GatewayRunner

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(
        platform=Platform("buzz"), chat_id="channel-a", chat_type="group", user_id="user-a")
    event = MessageEvent(text="unrelated flat message", source=source, message_id="flat-1")
    adapter = SimpleNamespace(inbound_reply_anchor=lambda _event: None)
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = store._db
    runner._intake_adapter_for = lambda _source: adapter
    runner._session_sources = {}

    resolved = await runner._hmwa_resolve_session(event, source)

    assert resolved is not None
    assert not resolved[1].session_id.startswith("cron_")
    assert store.lookup_by_session_key(resolved[2]).session_id == resolved[1].session_id
    store._db.close()


def test_platform_anchor_contracts_are_direct_and_fail_closed():
    from gateway.platforms.base import SendResult
    from plugins.platforms.buzz.adapter import BuzzAdapter
    from plugins.platforms.google_chat.adapter import GoogleChatAdapter
    from plugins.platforms.slack.adapter import SlackAdapter

    sent = SendResult(success=True, message_id="171.002")
    assert BuzzAdapter.outbound_reply_anchor(None, sent) == ("message", "171.002")
    assert SlackAdapter.outbound_reply_anchor(None, sent) is None
    assert SlackAdapter.outbound_reply_anchor(
        None, sent, requested_thread_id="old-root") is None

    slack_event = MessageEvent(
        text="reply", source=SessionSource(platform=Platform.SLACK, chat_id="C1"),
        message_id="171.003", raw_message={"ts": "171.003", "thread_ts": "171.002"},
    )
    assert SlackAdapter.inbound_reply_anchor(None, slack_event) == ("thread", "171.002")
    slack_event.raw_message = {"ts": "171.004"}
    assert SlackAdapter.inbound_reply_anchor(None, slack_event) is None

    chat_result = SendResult(
        success=True, message_id="spaces/S/messages/M",
        raw_response={"thread": {"name": "spaces/S/threads/T"},
                      "_cron_reply_thread": "spaces/S/threads/T"},
    )
    assert GoogleChatAdapter.outbound_reply_anchor(None, chat_result) == (
        "thread", "spaces/S/threads/T")
    assert GoogleChatAdapter.outbound_reply_anchor(
        None, chat_result, requested_thread_id="spaces/S/threads/OLD") is None
