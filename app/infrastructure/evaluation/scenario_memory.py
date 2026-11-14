"""场景记忆隔离绑定（plan T7）。

逻辑别名（file_1 / mem0-e2e-test）-> 每绑定独立 provider 实例与专用
存储空间：file 别名映射独立测试项目目录；mem0 别名经工厂构造独立
Provider 实例并使用专用 user/agent namespace。

纪律（plan T7 S3）：
- 归属保护：绑定按 session_id 存取，resolve 只对绑定的会话生效；
- 不修改全局 ExternalMemoryManager 注册表、不替换全局 Provider 实例；
  解析按 session_id 参数进行（跨 HTTP 请求安全，不依赖父进程
  ContextVar）；
- mem0 后端无隔离 provision/delete 能力（工厂未接线或 provision
  失败）-> blocked（ScenarioMemoryError），不降级到共享空间；
- 绝不访问/覆盖真实个人记忆：专用 namespace 由 session_id 派生，
  与生产 user_id 空间不相交；
- prepare 失败回收已建资源；delete 精确删除绑定资源。
"""
from __future__ import annotations

import hashlib
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from app.infrastructure.memory.builtin_project import BuiltinProjectMemory


class ScenarioMemoryError(Exception):
    """场景记忆准备失败/能力缺失（blocked 语义，不降级）。"""


def _fact_sha256(fact: str) -> str:
    return hashlib.sha256(fact.encode("utf-8")).hexdigest()


class ScenarioMemoryBinder:
    """会话记忆隔离绑定注册表（session_id + 逻辑别名 -> 独立 provider）。"""

    def __init__(
        self,
        *,
        file_root: str | Path,
        mem0_factory: Callable[[dict[str, Any]], Any] | None = None,
        mem0_base_config: Mapping[str, Any] | None = None,
        mem0_poll_seconds: float = 120.0,
        mem0_poll_interval: float = 5.0,
    ) -> None:
        self._file_root = Path(file_root)
        self._mem0_factory = mem0_factory
        self._mem0_base_config = dict(mem0_base_config or {})
        # mem0 add 后异步索引：prepare 轮询到预置事实可检索才返回绑定，
        # 避免"预置成功但首轮 prefetch 拿不到"的半截绑定（T8 S4 实测：
        # mem0 索引延迟可达分钟级，30s 窗口不足）
        self._mem0_poll_seconds = float(mem0_poll_seconds)
        self._mem0_poll_interval = float(mem0_poll_interval)
        self._bindings: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 准备 / 解析 / 删除
    # ------------------------------------------------------------------

    def prepare(self, session_id: str, alias: str, *, fact: str) -> dict[str, Any]:
        """为会话绑定逻辑别名的独立记忆空间并预置合成事实。

        返回 prepare receipt（进入资源 intent 与实验快照）。"""
        if not session_id or not alias or not fact:
            raise ScenarioMemoryError("session_id/alias/fact 不得为空")
        kind = self._kind_of(alias)
        key = (session_id, alias)
        with self._lock:
            if key in self._bindings:
                # 幂等：同一会话重复 prepare 返回既有 receipt（重试安全）
                return dict(self._bindings[key]["receipt"])
            if kind == "file":
                binding = self._prepare_file(session_id, alias, fact)
            else:
                binding = self._prepare_mem0(session_id, alias, fact)
            self._bindings[key] = binding
            return dict(binding["receipt"])

    def resolve(self, session_id: str, alias: str) -> Any | None:
        """归属保护解析：仅绑定的 (session_id, alias) 返回 provider。"""
        with self._lock:
            binding = self._bindings.get((session_id, alias))
        return binding["provider"] if binding else None

    def delete(self, session_id: str, alias: str) -> None:
        """删除精确测试绑定（专用目录/专用命名空间）。

        进程重启后 _bindings 为空：空间定位由 (session_id, alias)
        确定性派生（file 目录/mem0 命名空间同 prepare 规则），此处
        重建定位精确删除，保证 TTL 清理跨重启仍生效（T9 S4 实测
        缺口）。"""
        with self._lock:
            binding = self._bindings.pop((session_id, alias), None)
        if not binding:
            if not session_id or not alias:
                return
            kind = self._kind_of(alias)
            safe = hashlib.sha256(
                f"{session_id}:{alias}".encode()
            ).hexdigest()[:16]
            if kind == "file":
                shutil.rmtree(
                    self._file_root / f"{alias}-{safe}", ignore_errors=True
                )
                return
            if self._mem0_factory is None:
                raise ScenarioMemoryError(
                    "mem0 隔离删除能力未接线（无专用空间工厂），拒绝共享空间"
                )
            config = dict(self._mem0_base_config)
            config["user_id"] = f"eval-{safe}"
            config["agent_id"] = f"eval-agent-{safe}"
            provider = self._mem0_factory(config)
            try:
                provider.delete_all()
            except Exception:
                raise
            return
        if binding["kind"] == "file":
            shutil.rmtree(binding["project_dir"], ignore_errors=True)
        else:
            try:
                binding["provider"].delete_all()
            except Exception:
                # 删除失败不阻塞账目登记：资源 intent 记录 cleanup 状态
                binding["provider"] = None
                raise

    def global_provider_names(self) -> tuple[str, ...]:
        """binder 不持有任何全局 Provider（供测试核对未被替换）。"""
        return ()

    def receipt(self, session_id: str, alias: str) -> dict[str, Any] | None:
        with self._lock:
            binding = self._bindings.get((session_id, alias))
        return dict(binding["receipt"]) if binding else None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _kind_of(alias: str) -> str:
        if alias.startswith("mem0"):
            return "mem0"
        if alias.startswith("file"):
            return "file"
        raise ScenarioMemoryError(f"未知记忆逻辑别名: {alias!r}")

    def _prepare_file(self, session_id: str, alias: str, fact: str) -> dict[str, Any]:
        safe = hashlib.sha256(f"{session_id}:{alias}".encode()).hexdigest()[:16]
        project_dir = self._file_root / f"{alias}-{safe}"
        project_dir.mkdir(parents=True, exist_ok=True)
        try:
            provider = BuiltinProjectMemory(
                project_root=project_dir, memory_path="./memory"
            )
            memory_file = project_dir / "memory" / "memory.md"
            memory_file.parent.mkdir(parents=True, exist_ok=True)
            memory_file.write_text(f"- {fact}\n", encoding="utf-8")
            provider.initialize(session_id)
        except Exception as exc:
            shutil.rmtree(project_dir, ignore_errors=True)
            raise ScenarioMemoryError(f"file 记忆准备失败: {exc}") from exc
        return {
            "kind": "file",
            "provider": provider,
            "project_dir": str(project_dir),
            "receipt": {
                "kind": "file",
                "alias": alias,
                "session_id": session_id,
                "project_dir": str(project_dir),
                "fact_sha256": _fact_sha256(fact),
            },
        }

    def _prepare_mem0(self, session_id: str, alias: str, fact: str) -> dict[str, Any]:
        if self._mem0_factory is None:
            raise ScenarioMemoryError(
                "mem0 隔离 provision 能力未接线（无专用空间工厂），拒绝共享空间"
            )
        safe = hashlib.sha256(f"{session_id}:{alias}".encode()).hexdigest()[:16]
        config = dict(self._mem0_base_config)
        # 专用命名空间：与生产 user_id 空间不相交，绝不访问个人记忆
        config["user_id"] = f"eval-{safe}"
        config["agent_id"] = f"eval-agent-{safe}"
        provider = None
        try:
            provider = self._mem0_factory(config)
            provider.provision(fact)
            # 索引就绪轮询：预置事实必须可检索才视为绑定成功；超时回收
            # 专用命名空间并 blocked（不留下检索不到的半截绑定）
            deadline = time.monotonic() + self._mem0_poll_seconds
            while fact not in str(provider.prefetch(fact, session_id=session_id) or ""):
                if time.monotonic() >= deadline:
                    try:
                        provider.delete_all()
                    except Exception:
                        pass
                    raise ScenarioMemoryError(
                        f"mem0 预置事实在 {self._mem0_poll_seconds:g}s 内不可检索"
                        "（后端索引未就绪），绑定 blocked"
                    )
                time.sleep(self._mem0_poll_interval)
        except ScenarioMemoryError:
            raise
        except Exception as exc:
            # provision/prefetch 中途失败：尽力回收已建命名空间，不残留
            if provider is not None:
                try:
                    provider.delete_all()
                except Exception:
                    pass
            raise ScenarioMemoryError(f"mem0 专用空间准备失败: {exc}") from exc
        return {
            "kind": "mem0",
            "provider": provider,
            "receipt": {
                "kind": "mem0",
                "alias": alias,
                "session_id": session_id,
                "user_id": config["user_id"],
                "agent_id": config["agent_id"],
                "fact_sha256": _fact_sha256(fact),
            },
        }
