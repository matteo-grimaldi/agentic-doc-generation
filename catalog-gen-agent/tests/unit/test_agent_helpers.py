from types import SimpleNamespace
from unittest.mock import Mock

import pytest




@pytest.mark.parametrize("obj,expected", [
    (None, {}), ({"status": "ok"}, {"status": "ok"}),
    (SimpleNamespace(model_dump=lambda: {"error": "x"}), {"error": "x"}),
    (SimpleNamespace(dict=lambda: {"status": "old"}), {"status": "old"}),
    (SimpleNamespace(model_dump=Mock(side_effect=ValueError()), dict=lambda: {"status": "fallback"}), {"status": "fallback"}),
    (SimpleNamespace(model_dump=Mock(side_effect=ValueError()), dict=Mock(side_effect=ValueError()), status="failed"), {"status": "failed"}),
    (object(), {}),
])
def test_response_normalization(agent_modules, obj, expected):
    assert agent_modules[0]._response_as_dict(obj) == expected


@pytest.mark.parametrize("payload,expected", [
    (None, None), ({}, None), ({"error": None}, None),
    ({"error": "bad"}, ("server_error", "bad")),
    ({"error": {}}, ("server_error", "Unknown backend error")),
    ({"error": {"type": "bad_request", "detail": "invalid"}}, ("bad_request", "invalid")),
    ({"error": {"code": "denied", "message": "no"}}, ("denied", "no")),
])
def test_error_normalization(agent_modules, payload, expected):
    assert agent_modules[0]._extract_ogx_error(payload) == expected


def test_long_errors_truncated(agent_modules):
    agent = agent_modules[0]
    assert len(agent._extract_ogx_error({"error": "x" * 900})[1]) == 500
    message = agent._extract_ogx_error({"error": {"message": "x" * 900}})[1]
    assert len(message) == 500 and message.endswith("...")


@pytest.mark.parametrize("code,message,hint", [
    ("server_error", "secret backend detail", True),
    ("api_connection_error", "lost", True),
    ("connection_error", "lost", True),
    ("other", "server disconnected", True),
    ("bad_request", "invalid model", False),
])
def test_failure_message(agent_modules, code, message, hint):
    result = agent_modules[0]._format_ogx_failure_message(code, message, 7)
    assert f"`{code}`" in result
    assert ("7 tool steps" in result) == hint
    if not hint:
        assert message in result






@pytest.mark.parametrize("state", ["no-url", "missing", "empty", "whitespace", "unreadable"])
def test_gateway_fails_closed(agent_modules, monkeypatch, tmp_path, state):
    agent = agent_modules[0]
    path = tmp_path / "token"
    monkeypatch.setattr(agent, "MCP_GATEWAY_URL", "" if state == "no-url" else "https://gateway.invalid")
    monkeypatch.setattr(agent, "MCP_GATEWAY_TOKEN_PATH", str(path))
    if state in ("empty", "whitespace"):
        path.write_text("" if state == "empty" else " \n ")
    if state == "unreadable":
        monkeypatch.setattr("builtins.open", Mock(side_effect=PermissionError("denied")))
    with pytest.raises(RuntimeError):
        agent._mcp_gateway_tool()


def test_gateway_rotation(agent_modules, monkeypatch, tmp_path):
    agent = agent_modules[0]
    path = tmp_path / "token"
    monkeypatch.setattr(agent, "MCP_GATEWAY_TOKEN_PATH", str(path))
    monkeypatch.setattr(agent, "MCP_GATEWAY_URL", "https://gateway.invalid/mcp")
    for token in ("first", "rotated"):
        path.write_text(f" {token}\n")
        tool = agent._mcp_gateway_tool()
        assert tool["authorization"] == token
        assert tool["server_url"] == "https://gateway.invalid/mcp"
        assert tool["headers"] == {"X-Mcp-Virtualserver": agent.MCP_VIRTUAL_SERVER}
        assert tool["require_approval"] == "never"
