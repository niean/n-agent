# app/application/evaluation_io.py
"""评估数据集 / 校准集 manifest 加载共享 IO 原语（T12 / T8 共用）。

两个 manifest 家族（T12 DatasetManifest、T8 校准集）共享同一校验纪律：
- canonical JSON 内容 hash（去掉 hash 字段后重算比对）；
- 64 位小写十六进制 SHA-256 字段校验；
- 文件存在 + UTF-8 JSON 解码封装（fail-fast，错误类型由调用方注入）。

本模块只提供原语，不定义 manifest schema；字段语义由各自模块维护。
hash 计算沿用 domain canonical_json_bytes（allow_nan=False）：含 NaN/
Infinity 等非有限字面量的 manifest 在内容 hash 处即被拒绝（json.loads
默认接受 NaN 字面量的洞由此封堵），EvaluationValidationError 原样上抛，
由调用方按各自错误类型包装。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Type

from app.domain.evaluation.models import canonical_json_bytes

# SHA-256 十六进制摘要长度与小写十六进制字符集（T8/T12 共用单一常量）
SHA256_HEX_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")


def require_sha256_hex(
    value: Any, label: str, *, error: Type[Exception]
) -> str:
    """校验 64 位小写十六进制 SHA-256 摘要字段；非法时抛 ``error``。"""
    if (
        not isinstance(value, str)
        or len(value) != SHA256_HEX_LENGTH
        or any(c not in _HEX_DIGITS for c in value)
    ):
        raise error(
            f"{label} 必须是 {SHA256_HEX_LENGTH} 位小写十六进制 SHA-256 摘要: "
            f"{value!r}"
        )
    return value


def read_json_object(path: str | Path, *, error: Type[Exception]) -> Any:
    """读取 UTF-8 JSON 文件（fail-fast）：文件不存在 / 解码失败 / JSON
    非法均抛 ``error``，不返回部分结果。"""
    resolved = Path(path)
    if not resolved.is_file():
        raise error(f"manifest 文件不存在: {resolved}")
    try:
        return json.loads(resolved.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise error(f"manifest 不是合法 UTF-8 JSON: {resolved}: {exc}") from exc


def compute_content_hash(
    content: Mapping[str, Any], *, exclude_field: str
) -> str:
    """manifest 内容 hash：canonical JSON（去掉 ``exclude_field`` 字段）
    的 SHA-256。作者侧生成与加载侧校验必须使用同一函数。

    canonical_json_bytes 禁止非有限数值（allow_nan=False）：含 NaN/
    Infinity 字面量的内容抛 EvaluationValidationError（含 json.loads
    默认接受 NaN 字面量的封堵）。"""
    payload = {
        k: v for k, v in content.items() if k != exclude_field
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
