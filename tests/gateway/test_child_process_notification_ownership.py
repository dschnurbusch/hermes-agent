"""Delegated process notifications cannot acquire the parent's chat route."""
from types import SimpleNamespace
import json
import time

import pytest

from agent.delegation_context import delegated_child_context
from gateway.session_context import clear_session_vars, set_session_vars
from tools.terminal_tool_background import _apply_async_support


@pytest.mark.parametrize("notify,patterns", [(True, None), (False, ["ready"])])
@pytest.mark.parametrize("child_scope", ["context", "subprocess"])
def test_child_process_delivery_does_not_inherit_parent_channel(notify, patterns, child_scope, monkeypatch):
    from contextlib import nullcontext
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

    tokens = set_session_vars(platform="telegram", session_id="parent", chat_id="chat",
                              session_key="route", async_delivery=True)
    try:
        child_process = SimpleNamespace(id="proc-child", watcher_platform="")
        result = {}
        with monkeypatch.context() as child_env:
            if child_scope == "subprocess":
                child_env.setenv(DELEGATED_CHILD_ENV_MARKER, "1")
            scope = delegated_child_context("child") if child_scope == "context" else nullcontext()
            with scope:
                accepted = _apply_async_support(child_process, result, notify, patterns)
        assert accepted == (notify if child_scope == "context" else False, None)
        assert child_process.watcher_platform == ""
        assert result["notify_on_complete"] is False
        if child_scope == "context":
            assert "handoff" in result["subagent_note"]
        else:
            assert "poll" in result["notify_unsupported"]
        parent_process = SimpleNamespace(id="proc-parent", watcher_platform="")
        assert _apply_async_support(parent_process, {}, notify, patterns) == (notify, patterns)
        assert parent_process.parent_session_id == "parent"
        assert parent_process.watcher_platform == "telegram"
    finally:
        clear_session_vars(tokens)


def test_real_child_process_handoff_notifies_parent_without_child_chat_watcher(tmp_path, monkeypatch):
    import shlex
    import sys
    import weakref

    from tools.delegate_tool import _register_subagent, _unregister_subagent
    from tools.terminal_tool_background import spawn_background_process
    from tools.process_registry import process_registry, _handle_process
    import tools.process_registry as registry_module

    monkeypatch.setattr(registry_module, "_SYSTEMD_SCOPE_AVAILABLE", False)
    parent = type("Parent", (), {"session_id": "parent", "_current_task_id": "parent-handoff-turn"})()
    child = SimpleNamespace(_delegate_parent_ref=weakref.ref(parent))
    child_id = "sa-0-route-handoff"
    _register_subagent({"subagent_id": child_id, "parent_id": None, "depth": 0,
                       "goal": "handoff", "model": "test", "started_at": time.time(),
                       "status": "running", "tool_count": 0, "agent": child})
    gate = tmp_path / "finish"
    script = ("import pathlib,time; p=pathlib.Path(" + repr(str(gate)) + "); "
              "exec('while not p.exists(): time.sleep(0.02)'); print('child-finished')")
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    tokens = set_session_vars(platform="telegram", session_id="parent", chat_id="chat",
                              session_key="route", async_delivery=True)
    process = None
    try:
        with delegated_child_context("child"):
            result = json.loads(spawn_background_process(
                command=command, env=SimpleNamespace(env={}), env_type="local",
                effective_task_id=child_id, task_id=child_id, session_key=child_id,
                workdir=str(tmp_path), cwd=str(tmp_path), effective_pty=False,
                notify_on_complete=True, watch_patterns=None, approval_note=None,
                pty_disabled_reason=None))
        assert result["notify_on_complete"] is False
        process = process_registry.get(result["session_id"])
        assert process is not None
        assert not process.watcher_platform
        assert not any(w["session_id"] == process.id for w in process_registry.pending_watchers)
        handoff = json.loads(_handle_process(
            {"action": "handoff", "session_id": process.id, "data": "finish the child check"},
            task_id=child_id))
        assert handoff["status"] == "handed_off"
        assert process.notify_on_complete
        gate.touch()
        deadline = time.monotonic() + 10
        events = []
        while time.monotonic() < deadline and not events:
            events = process_registry.drain_notifications(
                owns_event=lambda event: event.get("session_id") == process.id)
            if not events:
                time.sleep(0.05)
        assert len(events) == 1
        event, text = events[0]
        assert event["owner_task_id"] == "parent-handoff-turn"
        assert event["exit_code"] == 0
        assert "child-finished" in text
    finally:
        gate.touch()
        if process is not None and not process.exited:
            process_registry.kill_process(process.id, source="test")
        _unregister_subagent(child_id)
        clear_session_vars(tokens)
