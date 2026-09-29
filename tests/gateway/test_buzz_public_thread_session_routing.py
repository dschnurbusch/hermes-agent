"""Opted-in Buzz cron threads share only their exact public route."""

from typing import cast
from collections import OrderedDict
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import AsyncSessionStore, SessionSource, SessionStore, build_session_key


def test_public_buzz_thread_route_shared_without_collapsing_private_owner_routes(tmp_path):
    config = GatewayConfig()
    config.group_sessions_per_user = True
    config.thread_sessions_per_user = True
    store = SessionStore(tmp_path / "sessions", config)
    db = cast(SessionDB, store._db)
    owner = "a" * 64
    other = "b" * 64

    def source(user, thread="public-root", chat="channel", profile=None):
        return SessionSource(platform=Platform("buzz"), chat_type="group", chat_id=chat,
                             thread_id=thread, user_id=user, profile=profile)

    try:
        private_a = source(owner, "private-root")
        private_b = source(other, "private-root")
        assert store._generate_session_key(private_a) != store._generate_session_key(private_b)
        assert build_session_key(private_a, thread_sessions_per_user=True) != build_session_key(
            private_b, thread_sessions_per_user=True)
        private_cron = "cron_private_buzz"
        db.create_session(private_cron, source="cron")
        db.end_session(private_cron, "cron_complete")
        assert db.record_cron_delivery(
            profile_name="default", platform="buzz", scope_id="", chat_id="channel",
            anchor_kind="message", anchor_id="private-root", thread_root="private-root",
            cron_session_id=private_cron, job_id="private-job", owner_user_id=owner)
        assert store._generate_session_key(private_a) != store._generate_session_key(private_b)

        cron_id = "cron_public_buzz"
        db.create_session(cron_id, source="cron")
        db.end_session(cron_id, "cron_complete")
        assert db.record_cron_delivery(
            profile_name="default", platform="buzz", scope_id="", chat_id="channel",
            anchor_kind="message", anchor_id="public-root", thread_root="public-root",
            cron_session_id=cron_id, job_id="public-job", owner_user_id="", public_thread=True)

        first = source(owner)
        second = source(other)
        first_key = store._generate_session_key(first)
        assert first_key == store._generate_session_key(second)
        assert first_key == build_session_key(first, thread_sessions_per_user=True)
        assert first_key == build_session_key(second, thread_sessions_per_user=True)
        assert "public_buzz_thread_route" not in first.to_dict()
        assert store.get_or_create_session(first).session_id == store.get_or_create_session(second).session_id
        assert first_key != store._generate_session_key(source(owner, "private-root"))
        assert first_key != store._generate_session_key(source(other, "private-root"))
        assert first_key != store._generate_session_key(source(other, chat="other-channel"))
        assert first_key != build_session_key(second, thread_sessions_per_user=True,
                                              profile="other-profile")
        assert first_key != store._generate_session_key(source("invalid-pubkey"))
    finally:
        db.close()


@pytest.mark.asyncio
async def test_public_buzz_reply_and_later_authorized_participant_share_adopted_thread(tmp_path):
    from gateway.run import GatewayRunner

    config = GatewayConfig()
    config.thread_sessions_per_user = True
    store = SessionStore(tmp_path / "sessions", config)
    db = cast(SessionDB, store._db)
    cron_id = "cron_public_shared"
    db.create_session(cron_id, source="cron")
    db.append_message(cron_id, role="user", content="private cron transcript")
    db.end_session(cron_id, "cron_complete")
    assert db.record_cron_delivery(
        profile_name="default", platform="buzz", scope_id="", chat_id="channel",
        anchor_kind="message", anchor_id="root", thread_root="root",
        cron_session_id=cron_id, job_id="public-job", owner_user_id="", public_thread=True)

    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = db
    runner._session_sources = OrderedDict()
    runner._intake_adapter_for = lambda source: SimpleNamespace(
        inbound_reply_anchor=lambda event: ("message", event.reply_to_message_id)
        if event.reply_to_message_id else None)

    def source(pubkey, thread="root"):
        return SessionSource(platform=Platform("buzz"), chat_type="group", chat_id="channel",
                             thread_id=thread, user_id=pubkey)

    try:
        first = source("a" * 64)
        adopted = await runner._hmwa_resolve_session(
            MessageEvent(text="continue", source=first, reply_to_message_id="root"), first)
        assert adopted and adopted[1].session_id == cron_id
        assert db.is_consumed_cron_delivery(cron_id)
        second = source("b" * 64)
        following = await runner._hmwa_resolve_session(
            MessageEvent(text="follow up", source=second), second)
        assert following and following[1].session_id == cron_id
        assert following[2] == adopted[2]
        assert store.load_transcript(cron_id)[0]["content"] == "private cron transcript"
        assert store._generate_session_key(source("b" * 64, "unrelated")) != adopted[2]
    finally:
        db.close()
