from __future__ import annotations

from app.domain.provider import LLMProvider, LLMResult


_PROMPT = (
    "你是一个会话标题生成助手。基于用户的首条消息，输出一个简洁的中文标题，"
    "不超过 16 个字符，只输出标题本身，不加引号、不加标点结尾、不加前缀。"
)


class LLMTitleGenerator:
    def __init__(self, provider: LLMProvider, model: str, max_chars: int = 16):
        self.provider = provider
        self.model = model
        self.max_chars = max_chars

    async def generate(self, user_message: str) -> str:
        text = (user_message or "").strip()
        if not text:
            return ""
        result = await self.provider.chat(
            [
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": text[:500]},
            ],
            [],
            False,
            self.model,
            {},
        )
        if not isinstance(result, LLMResult):
            return ""
        content = result.message.get("content") or ""
        title = str(content).strip().splitlines()[0].strip().strip('"').strip("'").strip("。.")
        return title[: self.max_chars]
