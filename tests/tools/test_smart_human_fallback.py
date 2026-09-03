"""Smart Approval can fail closed without prompting an interactive owner."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools import approval as A


@pytest.fixture
def smart_gateway(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(A.approval_context, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(A, "_get_denial_breaker_threshold", lambda: 0)
    monkeypatch.setattr(
        A,
        "detect_dangerous_command",
        lambda command: (True, "fallback-test-danger", f"risk:{command}"),
    )
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
        raising=False,
    )

    session_key = "smart-human-fallback-test"
    token = A.approval_context.set_current_session_key(session_key)
    with A._lock:
        A._permanent_approved.discard("fallback-test-danger")
        A._permanent_approved.discard("execute_code")
        A._session_approved.get(session_key, set()).discard("fallback-test-danger")
        A._session_approved.get(session_key, set()).discard("execute_code")
        A._gateway_queues.pop(session_key, None)
        A._gateway_notify_cbs.pop(session_key, None)
    try:
        yield session_key
    finally:
        A.approval_context.reset_current_session_key(token)
        A._reset_denials(session_key)
        with A._lock:
            A._gateway_queues.pop(session_key, None)
            A._gateway_notify_cbs.pop(session_key, None)


@pytest.mark.parametrize("verdict", ["deny", "escalate"])
def test_terminal_nonapproval_denies_without_prompt(
    smart_gateway, monkeypatch, verdict
):
    prompted = []
    monkeypatch.setattr(A, "_smart_verdict", lambda *_args: verdict)
    monkeypatch.setattr(A, "_get_smart_human_fallback", lambda: "deny")
    A.register_gateway_notify(
        smart_gateway, lambda request: prompted.append(request)
    )

    result = A.check_all_command_guards("dangerous command", "local")

    assert result["approved"] is False
    assert result["smart_verdict"] == verdict
    assert result["outcome"] == "denied"
    assert prompted == []


@pytest.mark.parametrize("verdict", ["deny", "escalate"])
def test_execute_code_nonapproval_denies_without_prompt(
    smart_gateway, monkeypatch, verdict
):
    prompted = []
    monkeypatch.setattr(A, "_smart_verdict", lambda *_args: verdict)
    monkeypatch.setattr(A, "_get_smart_human_fallback", lambda: "deny")
    A.register_gateway_notify(
        smart_gateway, lambda request: prompted.append(request)
    )

    result = A.check_execute_code_guard("print('x')", "local")

    assert result["approved"] is False
    assert result["smart_verdict"] == verdict
    assert result["outcome"] == "denied"
    assert prompted == []


def test_default_prompt_behavior_is_preserved(smart_gateway, monkeypatch):
    prompted = []
    monkeypatch.setattr(A, "_smart_verdict", lambda *_args: "escalate")
    monkeypatch.setattr(A, "_get_smart_human_fallback", lambda: "prompt")

    def resolve(request):
        prompted.append(request)
        A.resolve_gateway_approval(smart_gateway, "deny")

    A.register_gateway_notify(smart_gateway, resolve)
    result = A.check_all_command_guards("dangerous command", "local")

    assert result["approved"] is False
    assert len(prompted) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("deny", "deny"), ("block", "deny"), ("prompt", "prompt"), ("bogus", "prompt")],
)
def test_smart_human_fallback_config_parsing(monkeypatch, raw, expected):
    monkeypatch.setattr(
        A.approval_context,
        "_get_approval_config",
        lambda: {"smart_human_fallback": raw},
    )
    assert A._get_smart_human_fallback() == expected


def test_fail_closed_breaker_never_requests_technical_approval(
    smart_gateway, monkeypatch
):
    monkeypatch.setattr(A, "_smart_verdict", lambda *_args: "deny")
    monkeypatch.setattr(A, "_get_smart_human_fallback", lambda: "deny")
    monkeypatch.setattr(A, "_get_denial_breaker_threshold", lambda: 1)

    result = A.check_all_command_guards("dangerous command", "local")

    assert "CIRCUIT BREAKER:" in result["message"]
    assert "do not request technical approval" in result["message"]
    assert "/approve" not in result["message"]


def test_fail_closed_suppresses_interactive_cli_callback(
    smart_gateway, monkeypatch
):
    callback_calls = []
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setattr(A, "_smart_verdict", lambda *_args: "deny")
    monkeypatch.setattr(A, "_get_smart_human_fallback", lambda: "deny")

    result = A.check_all_command_guards(
        "dangerous command",
        "local",
        approval_callback=lambda *args, **kwargs: callback_calls.append(
            (args, kwargs)
        ),
    )

    assert result["approved"] is False
    assert result["smart_verdict"] == "deny"
    assert callback_calls == []


@pytest.mark.parametrize("failure", ["malformed", "provider_error"])
def test_reviewer_failure_fails_closed_without_prompt(
    smart_gateway, monkeypatch, failure
):
    prompted = []
    monkeypatch.setattr(A, "_get_smart_human_fallback", lambda: "deny")
    monkeypatch.setattr(
        "agent.auxiliary_client._get_task_timeout", lambda _task: 1
    )

    if failure == "malformed":
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="maybe"))]
        )
        monkeypatch.setattr(
            "agent.auxiliary_client.call_llm", lambda **_kwargs: response
        )
    else:
        def fail_provider(**_kwargs):
            raise RuntimeError("reviewer unavailable")

        monkeypatch.setattr("agent.auxiliary_client.call_llm", fail_provider)

    A.register_gateway_notify(
        smart_gateway, lambda request: prompted.append(request)
    )
    result = A.check_all_command_guards("dangerous command", "local")

    assert result["approved"] is False
    assert result["smart_verdict"] == "escalate"
    assert result["outcome"] == "denied"
    assert prompted == []