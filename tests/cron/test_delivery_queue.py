"""Durable at-most-once delivery handoff for restart-safe cron workers."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from unittest.mock import Mock

import pytest


def test_pending_delivery_is_claimed_and_sent_once(tmp_path, monkeypatch):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-1", {"id": "job-1"}, "brief")
    send = Mock(return_value=None)

    assert queue.drain(send) == 1
    assert queue.drain(send) == 0
    send.assert_called_once_with({"id": "job-1"}, "brief", False)
    status = queue.get_status("exec-1")
    assert status["status"] == "delivered"
    assert status["job_json"] == "{}"
    assert status["content"] == ""


def test_terminal_delivery_retention_is_bounded(tmp_path, monkeypatch):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    monkeypatch.setattr(queue, "MAX_TERMINAL_DELIVERIES", 2, raising=False)
    for index in range(4):
        execution_id = f"exec-{index}"
        queue.enqueue(execution_id, {"id": f"job-{index}"}, f"brief-{index}")
        assert queue.claim_next()["execution_id"] == execution_id
        assert queue._finish(execution_id, error=None)

    # Pruning may discard verbose outcome rows, but never the durable
    # idempotency tombstone for an execution that could be replayed later.
    pruned = queue.get_status("exec-0")
    assert pruned is not None
    assert pruned["status"] == "delivered"
    assert queue.get_status("exec-1")["status"] == "delivered"
    assert queue.get_status("exec-2")["status"] == "delivered"
    assert queue.get_status("exec-3")["status"] == "delivered"

    # Pruning may discard verbose outcome rows, but never the durable
    # idempotency tombstone for an execution that could be replayed later.
    queue.enqueue("exec-0", {"id": "job-replayed"}, "duplicate brief")
    send = Mock(return_value=None)
    assert queue.drain(send) == 0
    send.assert_not_called()


def test_failure_delivery_lane_survives_durable_handoff(tmp_path, monkeypatch):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue(
        "exec-failure",
        {"id": "job-failure", "failure_deliver": "local"},
        "failed",
        for_failure=True,
    )
    send = Mock(return_value=None)

    assert queue.drain(send) == 1
    send.assert_called_once_with(
        {"id": "job-failure", "failure_deliver": "local"},
        "failed",
        True,
    )


def test_confirmed_reply_provenance_returns_to_external_worker(tmp_path, monkeypatch):
    import json
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-provenance", {"id": "job-1"}, "brief")
    candidate = {
        "platform": "buzz", "chat_id": "channel-a", "anchor_kind": "message",
        "anchor_id": "event-1", "cron_session_id": "cron-1", "job_id": "job-1",
    }

    def send(job, _content, _for_failure):
        job["_cron_delivery_provenance"] = candidate
        return None

    assert queue.drain(send) == 1
    status = queue.get_status("exec-provenance")
    assert status["status"] == "delivered"
    assert json.loads(status["provenance_json"]) == candidate


def test_late_receipt_persists_only_after_completed_execution_in_own_profile(tmp_path, monkeypatch):
    from cron import delivery_queue as queue, executions, reply_provenance
    from cron.scheduler_provider import _profile_cron_scope
    from hermes_constants import get_hermes_home

    monkeypatch.setattr(queue, "DELIVERY_DB", None)
    a, b = tmp_path / "profiles" / "a", tmp_path / "profiles" / "b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    records = []
    completed = set()
    available = set()
    monkeypatch.setattr(executions, "get_execution", lambda eid: {
        "status": "completed" if eid in completed else "running", "job_id": "job-1",
    })
    def record(candidate):
        if candidate["execution_id"] not in available:
            return False
        records.append((get_hermes_home(), candidate))
        return True
    monkeypatch.setattr(reply_provenance, "record_confirmed_delivery", record)

    def create(home, execution_id):
        candidate = {
            "execution_id": execution_id, "job_id": "job-1", "platform": "slack",
            "chat_id": "channel", "anchor_kind": "message", "anchor_id": execution_id,
            "cron_session_id": "cron-session", "owner_user_id": "owner",
        }
        with _profile_cron_scope(home):
            queue.enqueue(execution_id, {"id": "job-1", "_cron_run_session_id": "cron-session"}, "brief")
            def send(job, _content, _for_failure):
                job["_cron_delivery_provenance"] = candidate
            assert queue.drain(send) == 1
            assert queue.get_status(execution_id)["provenance_json"]
        return candidate

    candidate_a = create(a, "exec-a")
    candidate_b = create(b, "exec-b")
    assert records == []  # The send may precede worker fire-claim completion.
    completed.update(("exec-a", "exec-b"))
    with _profile_cron_scope(a):
        assert queue.drain(lambda *_: pytest.fail("duplicate send")) == 0
        assert queue.get_status("exec-a")["provenance_json"]  # State write failed: retryable.
    available.update(("exec-a", "exec-b"))
    with _profile_cron_scope(a):
        assert queue.drain(lambda *_: pytest.fail("duplicate send")) == 0
        assert queue.get_status("exec-a")["provenance_json"] is None
    with _profile_cron_scope(b):
        assert queue.drain(lambda *_: pytest.fail("duplicate send")) == 0
        assert queue.get_status("exec-b")["provenance_json"] is None
    with _profile_cron_scope(a):
        assert queue.drain(lambda *_: pytest.fail("duplicate persistence")) == 0
    assert records == [(a, candidate_a), (b, candidate_b)]


def test_unpersisted_provenance_survives_aging_and_terminal_pruning_but_is_bounded(tmp_path, monkeypatch):
    from cron import delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    monkeypatch.setattr(queue, "MAX_TERMINAL_DELIVERIES", 0)
    monkeypatch.setattr(queue, "MAX_UNPERSISTED_PROVENANCE", 2)
    for index in range(3):
        execution_id = f"terminal-{index}"
        candidate = {"execution_id": execution_id, "job_id": "job-1", "owner_user_id": "secret"}
        queue.enqueue(execution_id, {"id": "job-1"}, "brief")
        assert queue.claim_next()["execution_id"] == execution_id
        assert queue._finish(execution_id, error=None, provenance=candidate)
        with queue._transaction() as conn:
            conn.execute("UPDATE deliveries SET finished_at='2000-01-01T00:00:00+00:00' "
                         "WHERE execution_id=?", (execution_id,))
            queue._prune_terminal_unlocked(conn)
    assert queue.get_status("terminal-0").get("provenance_json") is None
    for index in (1, 2):
        assert queue.get_status(f"terminal-{index}")["provenance_json"] is not None
    queue.enqueue("pending", {"id": "job-2", "private": "recoverable"}, "queued brief")
    pending = queue.get_status("pending")
    assert pending["status"] == "pending"
    assert pending["content"] == "queued brief"
    assert '"private": "recoverable"' in pending["job_json"]


def test_aged_receipt_survives_restart_pruning_and_is_persisted(tmp_path, monkeypatch):
    import json
    from cron import delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    candidate = {"execution_id": "old-run", "job_id": "job-1", "platform": "slack",
                 "chat_id": "channel", "anchor_kind": "message", "anchor_id": "anchor",
                 "cron_session_id": "cron-session", "owner_user_id": "owner"}
    queue.enqueue("old-run", {"id": "job-1"}, "brief")
    assert queue.claim_next()["execution_id"] == "old-run"
    assert queue._finish("old-run", error=None, provenance=candidate)
    with queue._transaction() as conn:
        conn.execute("UPDATE deliveries SET finished_at='2000-01-01T00:00:00+00:00' "
                     "WHERE execution_id='old-run'")

    # A fresh interpreter models the gateway restarting after the worker has
    # finished. recover_abandoned prunes before drain retries provenance.
    result = subprocess.run([sys.executable, "-c", """
import json, sys
from cron import delivery_queue as queue, executions, reply_provenance
queue.DELIVERY_DB = __import__('pathlib').Path(sys.argv[1])
queue.MAX_TERMINAL_DELIVERIES = 0
executions.get_execution = lambda eid: {'status': 'completed', 'job_id': 'job-1'}
reply_provenance.record_confirmed_delivery = lambda candidate: (
    __import__('pathlib').Path(sys.argv[2]).write_text(json.dumps(candidate)) or True)
assert queue.drain(lambda *_: (_ for _ in ()).throw(AssertionError('duplicate send'))) == 0
assert queue.get_status('old-run')['status'] == 'delivered'
""", str(tmp_path / "deliveries.db"), str(tmp_path / "recorded.json")],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "recorded.json").read_text()) == candidate
    assert queue.get_status("old-run").get("provenance_json") is None


def test_legacy_queue_schema_adds_failure_lane_before_enqueue(tmp_path, monkeypatch):
    import cron.delivery_queue as queue

    db = tmp_path / "deliveries.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE deliveries (
                 execution_id TEXT PRIMARY KEY,
                 job_json TEXT NOT NULL,
                 content TEXT NOT NULL,
                 status TEXT NOT NULL,
                 owner_process_id TEXT,
                 owner_pid INTEGER,
                 owner_started_at INTEGER,
                 created_at TEXT NOT NULL,
                 finished_at TEXT,
                 error TEXT
               )"""
        )
    monkeypatch.setattr(queue, "DELIVERY_DB", db)

    queue.enqueue(
        "exec-migrated",
        {"id": "job-migrated"},
        "failed",
        for_failure=True,
    )

    assert queue.get_status("exec-migrated")["for_failure"] == 1


def test_wait_timeout_marks_inflight_delivery_unknown_without_retry(
    tmp_path, monkeypatch
):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-inflight", {"id": "job-inflight"}, "result")
    assert queue.claim_next() is not None

    error = queue.enqueue_and_wait(
        "exec-inflight", {"id": "job-inflight"}, "result", timeout=0
    )

    assert error is not None
    assert "outcome is unknown" in error
    status = queue.get_status("exec-inflight")
    assert status is not None
    assert status["status"] == "unknown"
    send = Mock(return_value=None)
    assert queue.drain(send) == 0
    send.assert_not_called()


def test_dead_delivery_owner_becomes_unknown_and_is_not_retried(
    tmp_path, monkeypatch
):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-1", {"id": "job-1"}, "brief")
    assert queue.claim_next() is not None
    monkeypatch.setattr(queue, "_PROCESS_ID", "replacement-gateway")
    monkeypatch.setattr(queue, "_owner_is_live", lambda _pid, _started: False)

    assert queue.recover_abandoned() == 1
    send = Mock()
    assert queue.drain(send) == 0
    send.assert_not_called()
    assert queue.get_status("exec-1")["status"] == "unknown"


def test_delivery_failure_is_terminal_not_retried_and_redacted(
    tmp_path, monkeypatch
):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-1", {"id": "job-1"}, "brief")
    send = Mock(return_value="request failed: https://example.test/?token=TOKEN123")

    assert queue.drain(send) == 1
    assert queue.drain(send) == 0
    assert send.call_count == 1
    status = queue.get_status("exec-1")
    assert status is not None
    assert status["status"] == "failed"
    assert "TOKEN123" not in status["error"]
    assert "token=***" in status["error"]


def test_wait_timeout_leaves_unclaimed_delivery_queued_for_next_gateway(
    tmp_path, monkeypatch
):
    """A row nobody claimed was never attempted: it is not uncertain, so a
    gateway outage longer than the worker's wait budget must not lose it."""
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    job = {"id": "job-3", "deliver": "origin"}

    error = queue.enqueue_and_wait("exec-3", job, "result", timeout=0)

    # Deferred, not failed: the worker must not record delivery_failed.
    assert error is None
    status = queue.get_status("exec-3")
    assert status is not None
    assert status["status"] == "pending"
    send = Mock(return_value=None)
    assert queue.drain(send) == 1
    send.assert_called_once_with(job, "result", False)
    assert queue.get_status("exec-3")["status"] == "delivered"


def test_same_gateway_recovers_terminalization_failure_without_resending(
    tmp_path, monkeypatch
):
    import cron.delivery_queue as queue

    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-4", {"id": "job-4"}, "result")
    send = Mock(return_value=None)
    original_finish = queue._finish
    monkeypatch.setattr(
        queue,
        "_finish",
        Mock(side_effect=OSError("database temporarily unavailable")),
    )

    with pytest.raises(OSError, match="temporarily unavailable"):
        queue.drain(send)

    monkeypatch.setattr(queue, "_finish", original_finish)
    assert queue.drain(send) == 0
    send.assert_called_once()
    status = queue.get_status("exec-4")
    assert status["status"] == "unknown"
    assert "not retried" in status["error"]
