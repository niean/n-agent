"""场景子进程执行原语（plan T11，spec 5.1 CLI/ACP 通道）。

Infrastructure 固定适配器共用的受控子进程纪律：
- 类型化 argv 列表执行，禁止 shell=True（manifest 不是命令）；
- 独立进程组（start_new_session）+ 有界限时，超时杀整组；
- 环境白名单：仅透传 N_AGENT_* 配置（剔除 N_AGENT_E2E_* 密钥），
  附加递归防护（禁用评估 worker）与最小基础环境；
- stderr 经脱敏器清洗且有界截断（不把 secret/长负载写进证据）；
- 记录进程身份（pid=<n>:start=<ts>，与资源账目契约同形）供审计。

只提供纯原语，不做会话解析/证据组装（那是 profile 适配器职责）。
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Mapping


# 子进程环境白名单：N_AGENT_* 服务配置显式透传（acp spawn 最小白名单
# 不继承它们），N_AGENT_E2E_*（Judge/API 密钥、浏览器密码）不属于服务
# 配置，绝不下发；禁用评估 worker 防止子进程递归领取同一实验。
_RECURSION_GUARD_ENV = {"N_AGENT_EVALUATION_ENABLED": "false"}
_BASE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


def subprocess_env(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """构建子进程环境：最小基础环境 + N_AGENT_*（非 E2E 密钥）。"""
    source = os.environ if environ is None else environ
    env = {key: source[key] for key in _BASE_ENV_KEYS if source.get(key)}
    for key, value in source.items():
        if key.startswith("N_AGENT_") and not key.startswith("N_AGENT_E2E_"):
            env[key] = value
    env.update(_RECURSION_GUARD_ENV)
    return env


def redact_bounded(text: str | None, *, limit: int, redactor) -> str:
    """stderr/stdout 证据清洗：先脱敏后有界截断。"""
    raw = text or ""
    cleaned = redactor(raw) if redactor else raw
    if len(cleaned) > limit:
        return cleaned[:limit] + f"...[truncated {len(cleaned) - limit}b]"
    return cleaned


@dataclass(frozen=True)
class ProcessResult:
    """单次子进程执行结果（证据只携带有界摘要，不携带全文）。"""

    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    process_identity: str | None
    timed_out: bool = False
    spawn_error: str | None = None


def run_process(
    argv: list[str],
    *,
    timeout_seconds: float,
    redactor: Callable[[str], str] | None = None,
    output_limit: int = 4000,
    clock: Callable[[], float] = time.monotonic,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    popen_factory: Callable[..., subprocess.Popen] | None = None,
) -> ProcessResult:
    """受控执行：类型化 argv、独立进程组、限时杀组、白名单环境。

    spawn 本身失败（可执行文件缺失等）返回 spawn_error，不抛出——
    调用方按证据纪律如实记录（读取失败不当不存在）。"""
    env = subprocess_env()
    identity: str | None = None
    proc = None
    started = clock()
    popen = popen_factory or subprocess.Popen

    def _identity_from(popen_obj: subprocess.Popen) -> str | None:
        pid = popen_obj.pid
        if pid is None:
            return None
        start_ts = ""
        try:
            stat = os.stat(f"/proc/{pid}")
            start_ts = str(stat.st_mtime)
        except OSError:
            start_ts = str(int(started * 1000))
        return f"pid={pid}:start={start_ts}"

    try:
        proc = popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            shell=False,
            start_new_session=True,
        )
        identity = _identity_from(proc)
    except Exception as exc:  # noqa: BLE001 - spawn 失败如实记录
        return ProcessResult(
            argv=tuple(argv), exit_code=None, stdout="", stderr="",
            duration_seconds=clock() - started, process_identity=None,
            spawn_error=f"{type(exc).__name__}: {exc}",
        )
    try:
        out, err = proc.communicate(timeout=max(0.1, timeout_seconds))
        exit_code = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        # 独立进程组：杀整组（负 pid），不留散落子进程
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001 - 杀组后仍收不到输出
            out, err = "", ""
        exit_code = proc.returncode
    duration = clock() - started
    return ProcessResult(
        argv=tuple(argv),
        exit_code=exit_code,
        stdout=redact_bounded(out, limit=output_limit, redactor=redactor),
        stderr=redact_bounded(err, limit=output_limit, redactor=redactor),
        duration_seconds=duration,
        process_identity=identity,
        timed_out=timed_out,
    )


# ---------------------------------------------------------------------------
# ACP 常驻子进程原语（plan T12）：异步 stdio 交互 + 进程组所有权
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AcpProcessHandle:
    """ACP agent 子进程句柄（stdio 流 + 进程身份 + 终止权）。"""

    argv: tuple[str, ...]
    process: object                      # asyncio subprocess（stdio 管道）
    process_identity: str
    stderr_text: Callable[[], str]       # 当前 stderr 尾部（未脱敏原文）
    _drain_task: object = None

    async def kill(self) -> None:
        """整组 SIGKILL（独立进程组）；已退出为幂等 no-op。"""
        import asyncio as _asyncio

        proc = self.process
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await _asyncio.wait_for(proc.wait(), timeout=5)
        except _asyncio.TimeoutError:
            pass
        task = self._drain_task
        if task is not None:
            try:
                await _asyncio.wait_for(task, timeout=2)
            except Exception:  # noqa: BLE001 - 排空任务收尾失败不干扰
                pass


def _process_identity(pid: int, started_monotonic: float) -> str:
    start_ts = ""
    try:
        stat = os.stat(f"/proc/{pid}")
        start_ts = str(stat.st_mtime)
    except OSError:
        start_ts = str(int(started_monotonic * 1000))
    return f"pid={pid}:start={start_ts}"


async def spawn_process(
    argv: list[str],
    *,
    create: Callable[..., object] | None = None,
    env: Mapping[str, str] | None = None,
    stderr_sink: Callable[[str], None] | None = None,
    stderr_limit: int = 4000,
    clock: Callable[[], float] = time.monotonic,
) -> AcpProcessHandle:
    """spawn 常驻 ACP agent 子进程（类型化 argv、独立进程组、白名单 env）。

    stderr 后台排空并保留原文尾部（证据侧由适配器脱敏有界）；协议
    通道只走 stdout，stderr 诊断信息不污染也不丢失。spawn 失败直接
    抛出，由适配器按纪律如实记录（transport_error acp_spawn_failed）。"""
    import asyncio as _asyncio

    process_env = subprocess_env(env)
    create_fn = create or _asyncio.create_subprocess_exec
    started = clock()
    proc = await create_fn(
        *argv,
        stdin=_asyncio.subprocess.PIPE,
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.PIPE,
        env=process_env,
        start_new_session=True,
    )
    chunks: list[str] = []

    async def _drain_stderr():
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                if stderr_sink is not None:
                    stderr_sink(text)
                chunks.append(text)
                if sum(len(c) for c in chunks) > stderr_limit * 4:
                    del chunks[:-8]
        except Exception:  # noqa: BLE001 - stderr 排空失败不干扰协议通道
            pass

    drain_task = _asyncio.create_task(_drain_stderr())
    return AcpProcessHandle(
        argv=tuple(argv),
        process=proc,
        process_identity=_process_identity(proc.pid, started),
        stderr_text=lambda: "".join(chunks),
        _drain_task=drain_task,
    )


async def kill_process_identity(
    process_identity: str,
    *,
    alive: Callable[[int], bool] | None = None,
    start_time: Callable[[int], float] | None = None,
    killpg: Callable[[int, int], None] | None = None,
) -> str:
    """按登记的进程身份停止进程组（settle/stopper 安全网）。

    PID 复用防护：进程存在但启动时刻与登记不符 -> 拒绝并抛出
    （manual_attention，防误杀无关进程）。进程已不存在 -> 幂等
    "stopped"。默认 alive/start_time/killpg 由平台事实提供。"""
    def _default_alive(pid: int) -> bool:
        return os.path.exists(f"/proc/{pid}")

    def _default_start_time(pid: int) -> float:
        return float(os.stat(f"/proc/{pid}").st_mtime)

    alive_fn = alive or _default_alive
    start_fn = start_time or _default_start_time
    killpg_fn = killpg or os.killpg

    match = re.match(r"^pid=(\d+):start=(\d+(?:\.\d+)?)$", str(process_identity))
    if match is None:
        raise RuntimeError(
            f"acp 进程身份非法（manual_attention）: {process_identity!r}"
        )
    pid = int(match.group(1))
    registered_start = float(match.group(2))
    if not alive_fn(pid):
        return "stopped"
    try:
        actual_start = start_fn(pid)
    except OSError:
        actual_start = None
    if actual_start is not None and abs(actual_start - registered_start) > 1.0:
        raise RuntimeError(
            f"acp 进程身份不匹配（pid={pid} 疑似复用，manual_attention）: "
            f"登记 {registered_start} 实际 {actual_start}"
        )
    try:
        killpg_fn(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return "stopped"
    return "stopped"
