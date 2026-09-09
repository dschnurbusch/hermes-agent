"""Shared MCP transport adoption must also adopt profile-local Skills guards."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def shared(tmp_path, monkeypatch):
    from agent.secret_scope import is_multiplex_active, set_multiplex_active
    from hermes_constants import hermes_home_key, set_hermes_home_override, reset_hermes_home_override
    from tools import mcp_tool as core, mcp_skills_registry as skills
    from tools.mcp_skills_protocol import SkillEntry, SkillsGetResult, SkillsListResult
    from tools.registry import registry

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    token = set_hermes_home_override(a)
    multiplex = is_multiplex_active()
    set_multiplex_active(True)
    body = "---\nname: demo\ndescription: Demo guidance\n---\nRead carefully.\n"
    uri = "skill://fixture/demo/SKILL.md"
    entry = SkillEntry.model_validate({"uri": uri, "frontmatter": {"name": "demo", "description": "Demo guidance"},
        "resources": [{"uri": uri, "digest": "sha256:" + hashlib.sha256(body.encode()).hexdigest(), "size": len(body)}]})
    class Session:
        async def send_request(self, request, adapter):
            if request.method == "skills/list":
                return SkillsListResult(skills=[entry])
            assert request.method == "skills/get"
            return SkillsGetResult(skill=entry)
        async def read_resource(self, requested):
            return SimpleNamespace(contents=[SimpleNamespace(uri=requested, text=body, mimeType="text/markdown")])
    server = core.MCPServerTask("fixture")
    server._config = {"url": "https://fixture.invalid/mcp", "skills": {"enabled": True}}
    server.initialize_result = SimpleNamespace(capabilities=SimpleNamespace(
        extensions={"io.modelcontextprotocol/skills": {}}, resources=SimpleNamespace(), prompts=None))
    server.session = Session()
    monkeypatch.setattr(core, "_servers", {"fixture": server})
    monkeypatch.setattr(core, "_server_scope_keys", {"fixture": hermes_home_key(a)})
    monkeypatch.setattr(core, "_server_tool_scopes", {"fixture": {hermes_home_key(a)}})
    from tools import mcp_tool_loop
    monkeypatch.setattr(mcp_tool_loop, "_run_on_mcp_loop", lambda factory, **kw: asyncio.run(factory()))
    skills.clear_runtime_state()
    asyncio.run(server._discover_skills())
    reset_hermes_home_override(token)
    token = set_hermes_home_override(b)
    try:
        yield a, b, server, entry, body
    finally:
        for home in (a, b):
            for name in server._registered_tool_names:
                registry.deregister(name, scope=hermes_home_key(home))
        skills.clear_runtime_state()
        set_multiplex_active(multiplex)
        reset_hermes_home_override(token)


def test_shared_adoption_guards_generic_reads_and_retains_native_source(shared):
    from tools import mcp_tool_registration as registration, mcp_skills_cache as cache
    from tools import mcp_skills_registry as skills
    from tools.registry import registry
    a, b, server, entry, body = shared
    registration.register_connected_into_current_scope({"fixture": dict(server._config)})
    # Invoke the registered generated resource tool, including its real render guard.
    registered = registry.get_entry("mcp__fixture__read_resource")
    assert registered is not None
    handler = registered.handler
    good = json.loads(handler({"uri": entry.uri}, session_id="b-session"))
    assert good["result"] == body
    record = skills.pin_session(b, "b-session")[0]
    raw, *_ = cache.get_verified_resource(record, record["resources"][0], b, "b-session")
    assert raw == body.encode()
    server.session.read_resource = lambda uri: _result(SimpleNamespace(contents=[
        SimpleNamespace(uri=uri, text="changed content", mimeType="text/markdown")]))
    bad = json.loads(handler({"uri": entry.uri}, session_id="b-session"))
    assert "error" in bad, "shared profile resource read bypassed manifest verification"
    record = skills.pin_session(b, "b-session")[0]
    cache._capture_source("fixture", server, b, record["config_fingerprint"])
    cache._capture_source("fixture", server, a)
    server._revoke_skills_readiness()
    assert skills.pin_session(b, "after-revoke") == ()
    with pytest.raises(RuntimeError, match="extension-ready"):
        cache._capture_source("fixture", server, b)


async def _result(value):
    return value


def test_shared_native_source_uses_adopting_config_and_revokes_on_removal(shared):
    from tools import mcp_tool_registration as registration, mcp_skills_cache as cache
    from tools import mcp_skills_registry as skills
    a, b, server, entry, body = shared
    config = {**server._config, "tool_timeout": 42}
    registration.register_connected_into_current_scope({"fixture": config})
    token = cache._capture_source("fixture", server, b)
    assert token.config_fingerprint == skills.server_config_fingerprint(config)
    record = skills.pin_session(b, "b-session")[0]
    assert record["config_fingerprint"] == token.config_fingerprint
    raw, *_ = cache.get_verified_resource(record, record["resources"][0], b, "b-session")
    assert raw == body.encode()
    registration.register_connected_into_current_scope({
        "fixture": {**config, "skills": {"enabled": False}}})
    assert skills.pin_session(b, "opted-out") == ()
    with pytest.raises(RuntimeError, match="extension-ready"):
        cache._require_source_token(token)
    registration.register_connected_into_current_scope({"fixture": config})
    cache._capture_source("fixture", server, b)
    registration.register_connected_into_current_scope({})
    assert skills.pin_session(b, "after-removal") == ()
    with pytest.raises(RuntimeError, match="extension-ready"):
        cache._capture_source("fixture", server, b)
    cache._capture_source("fixture", server, a)
