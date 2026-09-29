"""Buzz cron public replies require adapter-verified group destinations."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cron import reply_provenance, scheduler_delivery as delivery

OWNER = "a" * 64
OTHER = "b" * 64
ORIGIN = {"platform": "buzz", "chat_id": "channel-a", "user_id": OWNER, "chat_type": "group"}
TARGET = {"platform": "buzz", "chat_id": "channel-a", "thread_id": None, "_resolved_from": "explicit"}

@pytest.mark.parametrize("deliver,expected", [
    ("buzz:channel-a", True), ("local,buzz:channel-a", True),
    ("origin", False), ("local", False), ("buzz", False), ("all", False),
    ("origin,buzz:channel-a", False), ("local,origin,buzz:channel-a", False),
    ("buzz:channel-a,buzz:channel-a", False),
    ("local,buzz:channel-a,slack:other", False),
    ("buzz:other-channel", False),
])
def test_public_thread_requires_exact_opted_in_single_route(deliver, expected):
    job = {"deliver": deliver, "public_thread_continuation": True, "origin": ORIGIN}
    assert delivery._public_buzz_thread_opted_in(job, TARGET, adapter=SimpleNamespace(chat_type_for_chat=lambda _: "group")) is expected
    assert not delivery._public_buzz_thread_opted_in(job, TARGET, for_failure=True)
    assert not delivery._public_buzz_thread_opted_in({"deliver": deliver}, TARGET)

@pytest.mark.parametrize("origin", [
    ORIGIN, {**ORIGIN, "chat_type": "dm"},
    {**ORIGIN, "chat_id": "another-channel"},
    {**ORIGIN, "platform": "telegram"},
    {**ORIGIN, "platform": "discord"}, None,
])
@pytest.mark.parametrize("chat_type,allowed", [("group", True), ("channel", True), ("dm", False), (None, False)])
def test_public_buzz_destination_requires_adapter_group(monkeypatch, origin, chat_type, allowed):
    adapter = SimpleNamespace(supports_inchannel_continuable=True,
                              chat_type_for_chat=lambda chat_id: chat_type if chat_id == "channel-a" else None)
    monkeypatch.setattr(delivery, "_resolve_target_transport", lambda *args: (
        (SimpleNamespace(is_relay=False), SimpleNamespace(extra={"cron_continuable_surface": "in_channel"}), adapter, {}), None))
    job = {"id": "test", "deliver": "local,buzz:channel-a", "origin": origin,
           "public_thread_continuation": True, "attach_to_session": True}
    assert delivery._public_buzz_thread_opted_in(job, TARGET, adapter=adapter) is allowed
    prepared = delivery._prepare_target_delivery(
        job, TARGET, adapters={}, loop=None, config=None, notify_delivery=True,
        mirror_enabled=True, mirror_text="private brief", delivery_errors=[])
    assert prepared is not None
    assert prepared.explicit_buzz_reply_unsafe is not allowed
    assert prepared.mirror_this_target is allowed
    if allowed:
        assert not prepared.in_channel_surface  # Public thread never seeds a flat channel.
    else:
        assert not prepared.inchannel_continuable


def test_public_thread_job_field_persists_only_by_explicit_opt_in():
    from cron.jobs import create_job, get_job, update_job

    job = create_job(schedule="every 7h", prompt="synthetic report", deliver="local,buzz:channel-a")
    assert get_job(job["id"])["deliver"] == "local,buzz:channel-a"
    assert not get_job(job["id"]).get("public_thread_continuation")
    updated = update_job(job["id"], {"public_thread_continuation": True})
    assert updated["public_thread_continuation"] is True
    assert get_job(job["id"])["public_thread_continuation"] is True
    with pytest.raises(ValueError):
        update_job(job["id"], {"public_thread_continuation": "true"})


@pytest.mark.parametrize("deliver,origin,allowed", [
    ("origin", ORIGIN, True),
    ("buzz:channel-a", ORIGIN, False),
    ("origin,buzz:channel-a", ORIGIN, False),
    ("buzz:channel-a,origin", ORIGIN, False),
    ("buzz:channel-a", {}, False),
    ("buzz", ORIGIN, False),
    ("all", ORIGIN, False),
    ("origin", {**ORIGIN, "user_id": "not-a-pubkey"}, False),
    ("origin", {**ORIGIN, "chat_id": "other"}, False),
])
def test_only_captured_origin_binds_buzz_owner(deliver, origin, allowed):
    job = {"deliver": deliver, "origin": origin}
    assert delivery._buzz_origin_owner_bound(job, TARGET, origin) is allowed


@pytest.mark.parametrize("deliver", ["buzz:channel-a", "origin,buzz:channel-a", "buzz:channel-a,origin", "buzz", "all"])
def test_unsafe_buzz_targets_send_without_mirror_seed_or_owner_claim(monkeypatch, deliver):
    adapter = SimpleNamespace(supports_inchannel_continuable=True)
    monkeypatch.setattr(delivery, "_resolve_target_transport", lambda *args: (
        (SimpleNamespace(is_relay=False), SimpleNamespace(extra={"cron_continuable_surface": "in_channel"}), adapter, {}), None))
    job = {"id": "test", "deliver": deliver, "origin": ORIGIN,
           "attach_to_session": True, "_cron_run_session_id": "cron-session"}
    prepared = delivery._prepare_target_delivery(
        job, TARGET, adapters={}, loop=None, config=None, notify_delivery=True,
        mirror_enabled=True, mirror_text="private brief", delivery_errors=[])
    assert prepared is not None
    assert prepared.explicit_buzz_reply_unsafe
    assert not prepared.mirror_this_target
    assert not prepared.inchannel_continuable
    seed = Mock()
    mirror = Mock()
    monkeypatch.setattr(delivery, "_seed_cron_channel_session", seed)
    monkeypatch.setattr(delivery, "_maybe_mirror_cron_delivery", mirror)
    delivery._seed_live_delivery_sessions(prepared, "event-1")
    seed.assert_not_called()
    assert mirror.call_args.kwargs["enabled"] is False


def test_captured_origin_can_seed_but_explicit_failure_lane_cannot(monkeypatch):
    adapter = SimpleNamespace(supports_inchannel_continuable=True)
    monkeypatch.setattr(delivery, "_resolve_target_transport", lambda *args: (
        (SimpleNamespace(is_relay=False), SimpleNamespace(extra={"cron_continuable_surface": "in_channel"}), adapter, {}), None))
    job = {"id": "test", "deliver": "origin", "failure_deliver": "buzz:channel-a",
           "origin": ORIGIN, "_cron_run_session_id": "cron-session"}
    origin_target = {**TARGET, "_resolved_from": "origin"}
    def prepare(for_failure=False):
        return delivery._prepare_target_delivery(
            job, origin_target, adapters={}, loop=None, config=None,
            notify_delivery=True, mirror_enabled=False, mirror_text="brief",
            delivery_errors=[], for_failure=for_failure)

    normal = prepare()
    failure = prepare(True)
    assert normal is not None and failure is not None
    assert normal.inchannel_continuable
    assert not normal.explicit_buzz_reply_unsafe
    assert failure.explicit_buzz_reply_unsafe
    assert not failure.inchannel_continuable


@pytest.mark.parametrize("deliver,expect_owner", [
    ("origin", True), ("buzz:channel-a", False), ("origin,buzz:channel-a", False),
])
def test_confirmed_live_send_only_claims_origin_owner(monkeypatch, deliver, expect_owner):
    adapter = SimpleNamespace(supports_inchannel_continuable=False)
    monkeypatch.setattr(delivery, "_resolve_target_transport", lambda *args: (
        (SimpleNamespace(is_relay=False), SimpleNamespace(extra={}), adapter, {}), None))
    monkeypatch.setattr(delivery, "_live_route_metadata", lambda _: (None, {}, {}))
    monkeypatch.setattr(delivery, "_live_send_text", lambda *args, **kwargs: (
        True, False, "event-1", ("message", "event-1")))
    monkeypatch.setattr(delivery, "_seed_live_delivery_sessions", lambda *args: None)
    job = {"id": "test", "deliver": deliver, "origin": ORIGIN,
           "_cron_run_session_id": "cron-session", "_cron_provenance_allowed": True}
    target = {**TARGET, "_resolved_from": "origin"}
    t = delivery._prepare_target_delivery(
        job, target, adapters={}, loop=None, config=None, notify_delivery=True,
        mirror_enabled=True, mirror_text="brief", delivery_errors=[])
    assert t is not None
    assert delivery._deliver_via_live_adapter(
        t, "brief", [], target_errors=[], delivery_errors=[], unverified_targets=[])
    candidate = job.get("_cron_delivery_provenance")
    assert bool(candidate) is expect_owner
    if candidate:
        assert candidate["owner_user_id"] == OWNER
        assert candidate["owner_binding"] == "captured_origin"


def test_confirmed_group_send_records_public_anchor_without_origin_or_mirror(monkeypatch):
    adapter = SimpleNamespace(supports_inchannel_continuable=False,
                              chat_type_for_chat=lambda _: "group",
                              scope_id_for_chat=lambda _: "relay-scope")
    monkeypatch.setattr(delivery, "_resolve_target_transport", lambda *args: (
        (SimpleNamespace(is_relay=False), SimpleNamespace(extra={}), adapter, {}), None))
    monkeypatch.setattr(delivery, "_live_route_metadata", lambda _: (None, {}, {}))
    monkeypatch.setattr(delivery, "_live_send_text", lambda *args, **kwargs: (
        True, False, "event-1", ("message", "event-1")))
    monkeypatch.setattr(delivery, "_seed_live_delivery_sessions", lambda *args: None)
    job = {"id": "test", "deliver": "local,buzz:channel-a", "origin": None,
           "public_thread_continuation": True, "_cron_run_session_id": "cron-session",
           "_cron_provenance_allowed": True}
    t = delivery._prepare_target_delivery(
        job, TARGET, adapters={}, loop=None, config=None, notify_delivery=True,
        mirror_enabled=False, mirror_text="brief", delivery_errors=[])
    assert t is not None and not t.explicit_buzz_reply_unsafe and not t.mirror_this_target
    assert delivery._deliver_via_live_adapter(
        t, "brief", [], target_errors=[], delivery_errors=[], unverified_targets=[])
    assert job["_cron_delivery_provenance"]["access_policy"] == "public_buzz_thread"
    assert job["_cron_delivery_provenance"]["thread_root"] == "event-1"
    assert job["_cron_delivery_provenance"]["owner_user_id"] == ""
    adapter.chat_type_for_chat = lambda _: "dm"
    job.pop("_cron_delivery_provenance")
    assert delivery._deliver_via_live_adapter(
        t, "brief", [], target_errors=[], delivery_errors=[], unverified_targets=[])
    assert "_cron_delivery_provenance" not in job


def test_buzz_provenance_sink_rejects_unbound_and_invalid_owner(monkeypatch):
    candidate = {"platform": "buzz", "chat_id": "channel-a", "anchor_kind": "message",
                 "anchor_id": "event-1", "cron_session_id": "cron-1", "job_id": "job-1",
                 "owner_user_id": OWNER}
    db = Mock()
    db.record_cron_delivery.return_value = True
    monkeypatch.setattr("hermes_state_registry.acquire", lambda: db)
    monkeypatch.setattr("hermes_state_registry.release", lambda _: None)
    assert reply_provenance.record_confirmed_delivery(candidate) is False
    assert reply_provenance.record_confirmed_delivery({**candidate, "owner_binding": "captured_origin", "owner_user_id": "owner"}) is False
    db.record_cron_delivery.assert_not_called()
    assert reply_provenance.record_confirmed_delivery({**candidate, "owner_binding": "captured_origin"}) is True
    assert db.record_cron_delivery.call_args.kwargs["owner_user_id"] == OWNER


def test_non_buzz_provenance_does_not_require_buzz_binding(monkeypatch):
    db = Mock()
    db.record_cron_delivery.return_value = True
    monkeypatch.setattr("hermes_state_registry.acquire", lambda: db)
    monkeypatch.setattr("hermes_state_registry.release", lambda _: None)
    candidate = {"platform": "slack", "chat_id": "C1", "anchor_kind": "message",
                 "anchor_id": "123", "cron_session_id": "cron-1", "job_id": "job-1", "owner_user_id": "U1"}
    assert reply_provenance.record_confirmed_delivery(candidate) is True
