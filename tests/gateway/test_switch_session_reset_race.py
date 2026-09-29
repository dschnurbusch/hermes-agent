"""A paused /resume cannot reopen a conversation after /new's terminal reset."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from hermes_state import SessionDB


def _fixture(tmp_path):
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    db = SessionDB(db_path=tmp_path / "state.db")
    store._db = db
    store._loaded = True
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm", user_id="user")
    entry = store.get_or_create_session(source)
    db.create_session("older", source="telegram", user_id="user")
    db.end_session("older", "user_exit")
    return store, db, source, entry


def test_paused_switch_cannot_reopen_after_new(tmp_path):
    store, db, source, initial = _fixture(tmp_path)
    entered, release, reset_started = Event(), Event(), Event()
    original_reopen = db.reopen_session

    def paused_reopen(session_id):
        entered.set()
        assert release.wait(5), "test failed to release paused reopen"
        return original_reopen(session_id)

    try:
        with patch.object(db, "reopen_session", side_effect=paused_reopen):
            with ThreadPoolExecutor(max_workers=2) as pool:
                switch = pool.submit(
                    store.switch_session, initial.session_key, "older",
                    expected_session_id=initial.session_id,
                )
                assert entered.wait(5), "switch did not reach reopen"
                def run_reset():
                    reset_started.set()
                    return store.reset_session(initial.session_key)

                reset = pool.submit(run_reset)
                assert reset_started.wait(5)
                try:
                    # A reset must not publish while the earlier switch owns the route lock.
                    # Old code published before this paused reopen and lets /new finish here.
                    with pytest.raises(TimeoutError):
                        reset.result(timeout=2)
                finally:
                    release.set()
                switched = switch.result(timeout=5)
                fresh = reset.result(timeout=5)
        assert switched is not None and switched.session_id == "older"
        assert fresh is not None and fresh.session_id != "older"
        assert store.peek_session_id(initial.session_key) == fresh.session_id
        assert db.get_session(initial.session_id)["end_reason"] == "session_switch"
        assert db.get_session("older")["end_reason"] == "session_reset"
        assert db.get_session(fresh.session_id)["ended_at"] is None
        assert store.get_or_create_session(source).session_id == fresh.session_id
    finally:
        release.set()
        db.close()


def test_reset_winning_during_target_lookup_refuses_stale_switch(tmp_path):
    store, db, source, initial = _fixture(tmp_path)
    entered, release = Event(), Event()
    original_get = db.get_session

    def paused_get(session_id):
        if session_id == "older":
            entered.set()
            assert release.wait(5), "test failed to release target lookup"
        return original_get(session_id)

    try:
        with patch.object(db, "get_session", side_effect=paused_get):
            with ThreadPoolExecutor(max_workers=2) as pool:
                switch = pool.submit(
                    store.switch_session, initial.session_key, "older",
                    expected_session_id=initial.session_id,
                )
                assert entered.wait(5)
                fresh = store.reset_session(initial.session_key)
                release.set()
                assert switch.result(timeout=5) is None
        assert store.peek_session_id(initial.session_key) == fresh.session_id
        assert db.get_session(initial.session_id)["end_reason"] == "session_reset"
        assert db.get_session("older")["end_reason"] == "user_exit"
        assert store.get_or_create_session(source).session_id == fresh.session_id
    finally:
        release.set()
        db.close()


def test_reset_keeps_predecessor_termination_inside_route_lock(tmp_path):
    store, db, source, initial = _fixture(tmp_path)
    entered, release, switch_started = Event(), Event(), Event()
    original_promote = db.promote_to_session_reset

    def paused_promote(session_id, reason):
        if session_id == initial.session_id and reason == "session_reset":
            entered.set()
            assert release.wait(5), "test failed to release reset transition"
        return original_promote(session_id, reason)

    try:
        with patch.object(db, "promote_to_session_reset", side_effect=paused_promote):
            with ThreadPoolExecutor(max_workers=2) as pool:
                reset = pool.submit(store.reset_session, initial.session_key)
                assert entered.wait(5)
                def switch_after_reset_published():
                    switch_started.set()
                    return store.switch_session(initial.session_key, "older")
                switch = pool.submit(switch_after_reset_published)
                assert switch_started.wait(5)
                try:
                    with pytest.raises(TimeoutError):
                        switch.result(timeout=2)
                finally:
                    release.set()
                fresh = reset.result(timeout=5)
                adopted = switch.result(timeout=5)
        assert fresh is not None and adopted is not None
        assert db.get_session(fresh.session_id)["end_reason"] == "session_switch"
        assert db.get_session("older")["ended_at"] is None
        assert store.peek_session_id(initial.session_key) == "older"
    finally:
        release.set()
        db.close()
