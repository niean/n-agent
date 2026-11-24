from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from app.application.runtime_provider import ActiveProviderHolder
from app.domain.gateway import GatewayLLMSelection, GatewaySessionLink, GatewaySessionRegistry
from app.domain.provider import ProviderConfig, ProviderRegistry, resolve_model


_CONFIG_LOAD_ERROR = "暂时无法加载 Provider 配置，请稍后重试。"

FEISHU_LLM_COMMANDS = frozenset({"/providers", "/provider", "/models", "/model"})


class FeishuLLMSelectionError(Exception):
    """A selection error with a public message for the Feishu user."""


@dataclass(frozen=True)
class FeishuLLMRoute:
    model: str
    provider: ActiveProviderHolder


class FeishuSessionLLM:
    def __init__(self, providers: ProviderRegistry, holder: ActiveProviderHolder):
        self._providers = providers
        self._holder = holder

    async def _effective_config(self, selection: GatewayLLMSelection | None) -> ProviderConfig | None:
        if selection is None:
            return self._holder.current_config
        try:
            config = await self._providers.get_provider(selection.provider_id)
        except Exception as exc:
            raise FeishuLLMSelectionError(_CONFIG_LOAD_ERROR) from exc
        if config is None:
            raise FeishuLLMSelectionError("原选 Provider 已不可用，请用 /providers 查看并用 /provider 重新选择。")
        return config

    async def _bind(self, config: ProviderConfig) -> ActiveProviderHolder:
        active = self._holder.snapshot()
        if active is not None and active.current_config == config:
            return active
        try:
            secret = await self._providers.get_secret(config.id)
            return await self._holder.bind(config, secret or "")
        except Exception as exc:
            raise FeishuLLMSelectionError(_CONFIG_LOAD_ERROR) from exc

    async def resolve(self, link: GatewaySessionLink) -> FeishuLLMRoute | None:
        selection = link.llm_selection
        config = await self._effective_config(selection)
        if config is None:
            return None
        model = (selection.model_id if selection else None) or config.model
        # A literal catalog ID may also be a legacy alias in other entrypoints.
        if resolve_model(model, config.model) != model:
            config = replace(config, model=model)
        return FeishuLLMRoute(model, await self._bind(config))

    async def command(
        self,
        command: str,
        argument: str,
        link: GatewaySessionLink | None,
        create_link: Callable[[], Awaitable[GatewaySessionLink]],
        registry: GatewaySessionRegistry,
    ) -> tuple[str, str]:
        session_id = link.session_id if link else ""
        selection = link.llm_selection if link else None
        if command in {"/provider", "/model"} and not argument:
            return session_id, f"用法: {command} <{command[1:]}>"
        if command in {"/providers", "/provider"}:
            try:
                configs = await self._providers.list_providers()
            except Exception as exc:
                raise FeishuLLMSelectionError(_CONFIG_LOAD_ERROR) from exc
            configs = sorted(configs, key=lambda cfg: (cfg.name.casefold(), cfg.id))
            if command == "/providers":
                active = self._holder.current_config
                current = selection.provider_id if selection else (active.id if active else None)
                rows = [
                    f"{cfg.name} ({cfg.id}) · 默认模型 {cfg.model}" + ("（当前）" if cfg.id == current else "")
                    for cfg in configs
                ]
                if current and all(cfg.id != current for cfg in configs):
                    rows.insert(0, "原选 Provider 已不可用，请用 /provider 重新选择。")
                return session_id, "\n".join(rows) or "尚未配置 Provider。"
            if not configs:
                return session_id, "尚未配置 Provider。"
            config = next((cfg for cfg in configs if cfg.id == argument), None)
            if config is None:
                matches = [cfg for cfg in configs if cfg.name == argument]
                if len(matches) > 1:
                    return session_id, "Provider 名称有多个匹配项，请使用 /providers 中的 ID。"
                if not matches:
                    return session_id, "未找到 Provider，请用 /providers 查看可选项。"
                config = matches[0]
            model_override = selection.model_id if selection and selection.provider_id == config.id else None
            await self._bind(config)
        else:
            config = await self._effective_config(selection)
            if config is None:
                return session_id, "尚未配置 Provider。"
            bound = await self._bind(config)
            try:
                models = await bound.list_models_strict()
                catalog_unavailable = False
            except Exception:
                models = []
                catalog_unavailable = True
            model_ids = dict.fromkeys([config.model, *(item.id for item in models)])
            current_model = (selection.model_id if selection else None) or config.model
            if command == "/models":
                rows = [f"Provider {config.name} ({config.id})"]
                if catalog_unavailable:
                    rows.append("模型目录不可用，仍可选择配置的默认模型。")
                rows.extend(model + ("（当前）" if model == current_model else "") for model in model_ids)
                if current_model not in model_ids:
                    rows.append(f"{current_model}（当前，未出现在目录中）")
                return session_id, "\n".join(rows)
            if argument not in model_ids:
                if catalog_unavailable:
                    return session_id, "模型目录不可用，目前只能选择配置的默认模型。"
                return session_id, "未找到模型，请用 /models 查看当前 Provider 的可选模型。"
            model_override = argument

        if link is None:
            link = await create_link()
        try:
            await registry.set_llm_selection(
                link.id, GatewayLLMSelection(config.id, model_override), expected=selection,
            )
        except Exception as exc:
            raise FeishuLLMSelectionError("未能保存当前会话的模型选择，请重试。") from exc
        return link.session_id, f"当前 Provider {config.name} ({config.id})，模型 {model_override or config.model}。"
