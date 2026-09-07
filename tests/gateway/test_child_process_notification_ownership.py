"""Delegated process notifications cannot acquire the parent's chat route."""
from types import SimpleNamespace
import json
import time

import pytest

from agent.delegation_context import delegated_child_context
from gateway.session_context import clear_session_vars, set_session_vars
from tools.terminal_tool_background import _apply_async_support


@pytest.mark.parametrize("notify,patterns", [(True, None), (False, ["ready"])])
def test_child_process_delivery_does_not_inherit_parent_channel(notify, patterns):
    tokens = set_session_vars(platform="telegram", session_id="parent", chat_id="chat",
                              session_key="route", async_delivery=True)
    try:
        child_process = SimpleNamespace(id="proc-child", watcher_platform="")
        result = {}
        with delegated_child_context("child"):
            accepted = _apply_async_support(child_process, result, notify, patterns)
        assert accepted == (False, None)
        assert child_process.watcher_platform == ""
        assert result["notify_on_complete"] is False
        assert "poll" in result["notify_unsupported"]
        parent_process = SimpleNamespace(id="proc-parent", watcher_platform="")
        assert _apply_async_support(parent_process, {}, notify, patterns) == (notify, patterns)
        assert parent_process.parent_session_id == "parent"
        assert parent_process.watcher_platform == "telegram"
    finally:
        clear_session_vars(tokens)


def test_real_child_process_finishes_without_chat_watcher():
    from tools.terminal_tool import terminal_tool
    from tools.process_registry import process_registry

    tokens = set_session_vars(platform="telegram", session_id="parent", chat_id="chat",
                              session_key="route", async_delivery=True)
    try:
        with delegated_child_context("child"):
            result = json.loads(terminal_tool(command="printf child-finished", background=True,
                                             notify_on_complete=True))
        assert result["notify_on_complete"] is False
        process = process_registry.get(result["session_id"])
        assert process is not None
        assert not process.watcher_platform
        assert not any(w["session_id"] == process.id for w in process_registry.pending_watchers)
        state = {}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = process_registry.poll(process.id)
            if state.get("exit_code") is not None:
                break
            time.sleep(0.05)
        assert state["exit_code"] == 0
    finally:
        clear_session_vars(tokens)
