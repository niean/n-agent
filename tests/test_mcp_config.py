from app.config import Settings


def test_mcp_settings_can_be_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("N_AGENT_MCP_CONNECT_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("N_AGENT_MCP_MAX_TOOLS", "7")
    monkeypatch.setenv("N_AGENT_MCP_MAX_SCHEMA_BYTES", "4096")
    monkeypatch.setenv("N_AGENT_MCP_MAX_RESULT_BYTES", "8192")
    monkeypatch.setenv("N_AGENT_MCP_ALLOW_PRIVATE_HOSTS", "true")

    settings = Settings()

    assert settings.mcp_connect_timeout_seconds == 3
    assert settings.mcp_max_tools == 7
    assert settings.mcp_max_schema_bytes == 4096
    assert settings.mcp_max_result_bytes == 8192
    assert settings.mcp_allow_private_hosts is True
