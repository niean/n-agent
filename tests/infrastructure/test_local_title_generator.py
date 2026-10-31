import pytest

from app.config import Settings
from app.infrastructure.session.local_title_generator import LocalTitleGenerator


@pytest.mark.asyncio
@pytest.mark.parametrize(("message", "expected"), [
    ("请帮我分析 Docker 启动失败的原因", "分析 Docker 启动失败的原"),
    ("# 标题生成\n后续说明", "标题生成"),
    ("如何配置模型？还有其他问题", "如何配置模型"),
    ("```python\nprint(1)\n```\n解释代码", "解释代码"),
    ("查看 [部署文档](https://example.com)", "查看 部署文档"),
    ("  \n ", ""),
    ("abcdefghijklmnopqrst", "abcdefghijklmnop"),
])
async def test_local_title(message, expected):
    assert await LocalTitleGenerator().generate(message) == expected


def test_llm_title_is_opt_in(monkeypatch):
    monkeypatch.delenv("N_AGENT_SESSION_TITLE_LLM_ENABLED", raising=False)
    assert Settings(_env_file=None).session_title_llm_enabled is False
    monkeypatch.setenv("N_AGENT_SESSION_TITLE_LLM_ENABLED", "true")
    assert Settings(_env_file=None).session_title_llm_enabled is True


@pytest.mark.parametrize("enabled", [False, True])
def test_title_generator_wiring(tmp_path, enabled):
    from app.main import build_application_services
    from app.infrastructure.session.llm_title_generator import LLMTitleGenerator

    settings = Settings(
        _env_file=None, sqlite_path=tmp_path / "sessions.db",
        workspace_root=tmp_path, skills_root=tmp_path / "skills",
        artifacts_enabled=False, session_title_llm_enabled=enabled,
    )
    services = build_application_services(settings)
    expected = LLMTitleGenerator if enabled else LocalTitleGenerator
    assert isinstance(services.session_service.title_generator, expected)
