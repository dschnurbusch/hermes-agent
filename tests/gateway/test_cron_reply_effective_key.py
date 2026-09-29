"""Cron reply lane checks must accept the effective SessionStore key layout."""

from collections import OrderedDict
from types import SimpleNamespace
from typing import cast

import pytest

from hermes_state import SessionDB

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from gateway.session_cron_reply import private_cron_reply_route


@pytest.mark.parametrize("group_per_user,thread_per_user", [
    (True, False), (True, True), (False, False), (False, True),
])
def test_buzz_reply_uses_effective_isolation_key(
    tmp_path, group_per_user, thread_per_user,
):
    config = GatewayConfig()
    config.group_sessions_per_user = group_per_user
    config.thread_sessions_per_user = thread_per_user
    store = SessionStore(tmp_path / "sessions", config)
    try:
        owner = SessionSource(platform=Platform("buzz"), chat_type="group",
                              chat_id="room", thread_id="cron-root", user_id="a" * 64)
        stranger = SessionSource(platform=Platform("buzz"), chat_type="group",
                                 chat_id="room", thread_id="cron-root", user_id="b" * 64)
        entry = store.get_or_create_session(owner)
        key = entry.session_key
        assert key == store._generate_session_key(owner)
        assert private_cron_reply_route(owner, key, "default", config=config)
        if group_per_user and thread_per_user:
            assert store._generate_session_key(stranger) != key
            assert not private_cron_reply_route(stranger, key, "default", config=config)
        else:
            assert store._generate_session_key(stranger) == key
        assert not private_cron_reply_route(owner, key, "other-profile", config=config)
        assert not private_cron_reply_route(owner, key + ":wrong", "default", config=config)
    finally:
        getattr(store._db, "close")()


@pytest.mark.asyncio
async def test_enabled_thread_isolation_adopts_only_owner_cron_reply(tmp_path):
    from gateway.run import GatewayRunner

    config = GatewayConfig()
    config.thread_sessions_per_user = True
    store = SessionStore(tmp_path / "sessions", config)
    db = cast(SessionDB, store._db)
    sid = "cron_private_isolated_thread"
    db.create_session(sid, source="cron")
    db.append_message(sid, role="user", content="private input")
    db.end_session(sid, "cron_complete")
    assert db.record_cron_delivery(
        profile_name="default", platform="buzz", scope_id="", chat_id="room",
        anchor_kind="message", anchor_id="cron-root", cron_session_id=sid,
        job_id="private", owner_user_id="a" * 64)

    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = db
    runner._session_sources = OrderedDict()
    runner._intake_adapter_for = lambda source: SimpleNamespace(
        inbound_reply_anchor=lambda event: ("message", "cron-root"))
    try:
        owner = SessionSource(platform=Platform("buzz"), chat_type="group",
                              chat_id="room", thread_id="cron-root", user_id="a" * 64)
        stranger = SessionSource(platform=Platform("buzz"), chat_type="group",
                                 chat_id="room", thread_id="cron-root", user_id="b" * 64)
        accepted = await runner._hmwa_resolve_session(
            MessageEvent(text="continue", source=owner), owner)
        assert accepted and accepted[1].session_id == sid
        assert accepted[2] == store._generate_session_key(owner)
        refused = await runner._hmwa_resolve_session(
            MessageEvent(text="intrude", source=stranger), stranger)
        assert refused is None or refused[1].session_id != sid
        assert store._generate_session_key(stranger) != accepted[2]
    finally:
        getattr(db, "close")()
