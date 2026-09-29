"""Status projections may read a cron transcript only from its claimed route."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionSource, SessionStore, build_session_key
from hermes_state import AsyncSessionDB


def _runner(store):
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner.config = store.config
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = AsyncSessionDB(store._db)
    runner._session_key_for_source = lambda source: build_session_key(source)
    runner._running_agents = {}
    runner.adapters = {Platform("buzz"): SimpleNamespace(_pending_messages={})}
    runner._queue_depth = lambda *args, **kwargs: 0
    runner._cached_agent_for = lambda key: None
    runner._resident_agent_for = lambda key: None
    runner._rehydrate_session_model_override = lambda key: None
    runner._session_model_override = lambda key: None
    runner._resolve_route_context = AsyncMock(return_value=None)
    runner._run_in_executor_with_context = AsyncMock(return_value=False)
    return runner


def _source(user):
    return SessionSource(platform=Platform("buzz"), chat_id="shared", chat_type="group",
                         thread_id="anchor", user_id=user)


@pytest.mark.asyncio
@pytest.mark.parametrize("public,stranded", [(False, False), (True, False), (False, True)])
async def test_status_projections_bind_to_cron_delivery_owner(tmp_path, public, stranded):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = store._db
    sid = "cron-secret"
    owner, other = "a" * 64, "b" * 64
    db.create_session(sid, source="cron", model="sensitive-model")
    db.append_message(sid, role="user", content="sensitive transcript")
    db.append_message(sid, role="assistant", content="private result")
    db.end_session(sid, "cron_complete")
    assert db.record_cron_delivery(
        profile_name="default", platform="buzz", scope_id="", chat_id="shared",
        anchor_kind="message", anchor_id="anchor", cron_session_id=sid,
        job_id="job", owner_user_id="" if public else owner, public_thread=public)
    initial = store.get_or_create_session(_source(owner))
    key = build_session_key(_source(owner))
    if stranded:
        # A failed reservation rollback can leave a routed key without committed
        # ownership. Neither sender may read that route.
        store.switch_session(key, sid)
    else:
        claimed = store.adopt_cron_reply(
            key, expected_session_id=initial.session_id, profile_name="default",
            platform="buzz", scope_id="", chat_id="shared", anchor_kind="message",
            anchor_id="anchor", user_id=owner, thread_id="anchor")
        assert claimed and claimed[0].session_id == sid
        db.release_session_turn_lease(sid, claimed[1][1])
    runner = _runner(store)
    try:
        for user in (owner, other):
            allowed = not stranded and (user == owner or public)
            for command, handler in (
                ("/status", runner._handle_status_command),
                ("/context all", runner._handle_context_command),
                ("/usage", runner._handle_usage_command),
            ):
                event = MessageEvent(text=command, source=_source(user), message_id="m")
                if command in {"/status", "/context all"}:
                    # Busy slash dispatch must enforce the same guard mid-turn.
                    name = command.split()[0][1:]
                    handler = lambda ev: runner._dispatch_busy_slash_command(
                        ev, SimpleNamespace(name=name, busy_policy="dispatch", busy_handler=None),
                        key, ev.source)
                if allowed:
                    with patch("gateway.slash_commands_status.fetch_account_usage", return_value=None), \
                         patch("agent.account_usage.nous_credits_lines", return_value=[]):
                        result = await handler(event)
                    assert "not available" not in result
                    assert result
                else:
                    # Denial happens before model-route, transcript and account probes.
                    with patch.object(runner.async_session_store, "load_transcript", side_effect=AssertionError("read")), \
                         patch.object(runner, "_status_session_db_facts", side_effect=AssertionError("metadata")):
                        result = await handler(event)
                    assert "not available" in result
                    assert sid not in result and "sensitive" not in result
    finally:
        db.close()
