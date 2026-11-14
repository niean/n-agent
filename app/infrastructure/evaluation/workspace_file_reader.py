"""T10：场景工作区文件受控读取端口。

安全读取任务工作区文件并返回内容与 sha256（资源账目登记与释放复核
的依据）。路径纪律与 E2E 编排器一致：绝对路径、``..`` 穿越、空路径
拒绝；任一级（含悬空）符号链接拒绝；仅普通文件可读（resolve 后越出
根拒绝）。仅真正不存在返回 None；其余违规抛 WorkspaceFileError——
调用方（适配器）将异常如实记录为证据 error，不当作文件缺失。
"""
from __future__ import annotations

import hashlib
from pathlib import Path


class WorkspaceFileError(ValueError):
    """工作区文件读取违规（穿越/symlink/非普通文件/根缺失）。"""


def read_workspace_file(workspace_root: str | Path, rel_path: str) -> dict | None:
    """安全读取工作区文件文本；仅真正不存在返回 None。

    返回 {filename, content, sha256}；违规抛 WorkspaceFileError
    （分支与 E2E read_workspace_file 语义对齐，错误信息直接进证据）。"""
    if not workspace_root:
        raise WorkspaceFileError("workspace root unavailable")
    root = Path(workspace_root).resolve()
    if not root.is_dir():
        raise WorkspaceFileError("workspace root missing")
    relative = Path(rel_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise WorkspaceFileError("workspace path invalid")
    target = root / relative
    for component in [target, *target.parents]:
        if component == root:
            break
        if component.is_symlink():
            raise WorkspaceFileError("workspace symlink forbidden")
    if not target.exists():
        return None
    if root not in target.resolve().parents or not target.is_file():
        raise WorkspaceFileError("workspace requires regular file")
    data = target.read_bytes()
    return {
        "filename": str(relative),
        "content": data.decode("utf-8"),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
