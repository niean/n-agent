from __future__ import annotations

import re


class LocalTitleGenerator:
    """Generate a short extractive title without a model request."""

    def __init__(self, max_chars: int = 16):
        self.max_chars = max_chars

    async def generate(self, user_message: str) -> str:
        text = (user_message or "").strip()
        text = re.sub(r"```[^\n]*\n.*?(?:```|$)", "", text, flags=re.S)
        text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"https?://\S+", "", text)
        for line in text.splitlines():
            line = re.sub(r"^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)", "", line)
            line = re.sub(r"[`*_~]", "", line)
            line = re.sub(r"\s+", " ", line).strip()
            line = re.sub(r"^(?:请帮我|请帮助我|帮我|麻烦帮我)\s*", "", line)
            title = re.split(r"[。！？!?；;]", line, maxsplit=1)[0].strip()
            if title:
                return title[:self.max_chars].rstrip(" ，,。.")
        return ""
