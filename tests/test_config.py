from pathlib import Path

from app.config import Settings


def test_settings_normalizes_workspace_and_sqlite_path(tmp_path: Path):
    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "agent.db"),
        workspace_root=str(tmp_path),
        agent_iteration_limit=3,
    )

    assert settings.sqlite_path == tmp_path / "agent.db"
    assert settings.workspace_root == tmp_path.resolve()
    assert settings.agent_iteration_limit == 3
