# app/application/evaluation_e2e_conversion.py
"""E2E 会话样例到 schema 2 数据集的离线无损转换（plan T2）。

输入是 tests/e2e/conversations/ 下受版本管理的源 JSON（黑盒 E2E 回放
用例），输出是"待审核的 schema 2 manifest/内联导入包 + 覆盖报告"。

边界纪律（spec-261114-expand-e2e-experiments 4.2）：
- 仅数据变换：本模块是纯函数（stdlib + application 层数据合同），不
  启动任何业务服务、不发模型/网络请求、不触碰 evaluations.db，不自动
  生成金标（expected_output 只逐字保留源 rubric）；
- 保留原文：每轮 rubric、expect.tools/forbidden_tools、通道、task
  action_params 原样进入场景契约；无法无损转换的条件进入 coverage 的
  unsupported 清单并使该 case 整体标记 unsupported（不静默丢条件）；
- 变换必须显式声明：原样保留、逻辑别名化、隔离路径参数化三类转换记入
  source.transformations，审核方必须知道发生了什么变换；
- 不导入身份：source_session_id 与历史 run/task/tool-call ID 不进入
  产物，溯源只保留 repo_path + 源 case id + 源文件 SHA-256 + 规则版本。

断言生成规则由 CASE_RULES 逐项声明（spec 3 覆盖矩阵的结构化转换规则），
未在规则表登记的构造（未知 channel / side_effect 类型 / 消息字段）整体
unsupported，不猜测语义。
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.application.evaluation_dataset import compute_dataset_hash

# 转换规则版本（进入每个 case 的 source.rules_version，审核可追溯）
CONVERSION_RULES_VERSION = "e2e-conversion-v2"

# 新集合 dataset_id（spec 3：版本 1/2/3/4 分别覆盖 4/7/10/12 个源样例）
DATASET_ID = "agent-e2e-scenarios"

# 源样例目录（仓库相对路径）
SOURCE_DIR_RELPATH = "tests/e2e/conversations"

# 源文档允许的顶层键（未知键 -> unsupported，防源格式漂移被静默容忍）
_SOURCE_KEYS = frozenset({
    "schema_version", "id", "title", "source_session_id", "channel",
    "requires", "options", "messages", "side_effects", "cleanup",
})

# 源消息允许的键
_MESSAGE_KEYS = frozenset({
    "id", "content", "expect", "image_asset", "action_params", "replay_via",
})

# expect 允许的键
_EXPECT_KEYS = frozenset({"rubric", "tools", "forbidden_tools"})

# 已知 side_effect 类型（未知类型 -> unsupported）
_SIDE_EFFECT_TYPES = frozenset({
    "task_terminal", "workspace_file", "artifact_registered",
})

# 通道 -> execution.profile（未知通道 -> unsupported）
_CHANNEL_PROFILES = {
    "dashboard": "dashboard",
    "cli": "cli",
    "acp": "acp",
}

# comparison_scope 映射（spec 3：固定素材 / 实时冒烟 / 通道冒烟 分组，
# 不同证据总体不混成同一可比总体）
_COMPARISON_SCOPES = {
    "multimodal-vision-chat": "fixed_fixture",
    "plugin-hello-chat": "fixed_fixture",
    "sandbox-execute-chat": "fixed_fixture",
    "memory-file-chat": "fixed_fixture",
    "memory-mem0-chat": "fixed_fixture",
    "task-artifact-chat": "live_smoke",
    "task-artifact-cmd": "fixed_fixture",
    "skill-weather-chat": "live_smoke",
    "rag-nkb-chat": "live_smoke",
    "browser-login-pod": "live_smoke",
    "cli-weather-chat": "channel_smoke",
    "acp-weather-chat": "channel_smoke",
}

# 生命周期规则 ID（注册语义，不是清理脚本）
_RELEASE_RULE = "release_active_resources"
_RETENTION_RULE = "retain_evidence_default"

# 逐项转换规则（spec 3 矩阵的机器可读形态）：
# - extra_result_contains: 源 rubric 中明确的确定性输出包含检查；
# - options_alias / binding: 记忆类逻辑别名与不可变材料摘要；
# - extra_transformations: 必须显式声明的数据变换。
_CASE_RULES: Mapping[str, Mapping[str, Any]] = {
    "multimodal-vision-chat": {
        "result_contains": "有什么我能帮你的吗",
        "fixture_asset": "assets/chat-multimodal-1.png",
        "extra_transformations": [],
    },
    "plugin-hello-chat": {
        "extra_transformations": ["原样保留"],
    },
    "sandbox-execute-chat": {
        "result_contains": "Hello Sandbox",
        "extra_transformations": ["原样保留"],
    },
    "memory-file-chat": {
        "result_contains": "文件记忆1",
        "options_alias": "file_1",
        "binding_material": "memory-file-project:e2e-file-1",
        "binding_fact": "文件记忆1",
        "extra_transformations": ["file_1 逻辑别名映射到专用隔离文件记忆项目（首轮前绑定）"],
    },
    "memory-mem0-chat": {
        "result_contains": "樱桃",
        "options_alias": "mem0-e2e-test",
        "binding_material": "mem0-namespace:e2e-synthetic-facts",
        "binding_fact": "喜欢樱桃",
        "extra_transformations": [
            "mem0 原个人事实改为同文案的合成测试事实（专用测试命名空间，不读取用户个人记忆）",
        ],
    },
    "rag-nkb-chat": {
        # plan T8：N-KB 检索题的确定性引用纪律（原 rubric 两条路径：
        # 原文引用，或标题+链接并说明无流程正文；语义部分由
        # agent.scenario_quality 承接）
        "kb_assertion": {"min_results": 1, "require_quote": False},
        "extra_transformations": ["原样保留"],
    },
    "browser-login-pod": {
        # plan T9：浏览器终态断言（closed 或确认不存在均规范化；读取
        # 失败不当不存在）
        "browser_state_closed": True,
        "extra_transformations": ["原样保留"],
    },
    "task-artifact-chat": {
        "extra_transformations": ["原样保留"],
    },
    "task-artifact-cmd": {
        "extra_transformations": [
            "文件基名与内容不变，目录改为服务端生成的执行专属隔离目录（路径参数化）",
            "任务经 task API action 创建（replay_via=task_api），不经空聊天发送",
        ],
    },
    "skill-weather-chat": {
        "extra_transformations": ["原样保留"],
    },
    "cli-weather-chat": {
        "extra_transformations": ["原样保留"],
    },
    "acp-weather-chat": {
        "extra_transformations": ["原样保留"],
    },
}

# ACP 通道协议握手序列断言（spec 3 R4：真实 initialize→…→prompt）
_ACP_PROTOCOL_STEPS = ("initialize", "authenticate", "session/new", "session/prompt")


@dataclass(frozen=True)
class ConversionResult:
    """转换产物：待审核 manifest 载荷 + 内联素材 + 覆盖报告。"""

    manifest: dict
    materials: tuple[tuple[str, bytes], ...]
    coverage: dict


def compute_manifest_hash(content: Mapping[str, Any]) -> str:
    """manifest 内容 hash（与 T12 权威加载器同一算法）。"""
    return compute_dataset_hash(content)


def convert_e2e_conversations(
    source_dir: str | Path,
    *,
    dataset_id: str = DATASET_ID,
    dataset_version: str = "1",
    case_ids: tuple[str, ...] | None = None,
) -> ConversionResult:
    """转换源目录下全部（或指定）E2E 会话 JSON 为 schema 2 审核包。

    任一 case 含无法识别的构造时该 case 标记 unsupported 并从 manifest
    剔除（进入 coverage 报告），不静默丢条件、不猜测语义。
    """
    directory = Path(source_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"源样例目录不存在: {directory}")
    paths = sorted(directory.glob("*.json"))
    if case_ids is not None:
        wanted = set(case_ids)
        paths = [path for path in paths if path.stem in wanted]

    cases: list[dict[str, Any]] = []
    materials: list[tuple[str, bytes]] = []
    coverage_cases: list[dict[str, Any]] = []
    for path in paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        converted = _convert_case(path, raw)
        coverage_cases.append(converted["coverage_entry"])
        if converted["case"] is not None:
            cases.append(converted["case"])
        materials.extend(converted["materials"])

    coverage = {
        "rules_version": CONVERSION_RULES_VERSION,
        "source_dir": SOURCE_DIR_RELPATH,
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "cases": coverage_cases,
        "summary": {
            "converted": sum(
                1 for entry in coverage_cases if entry["status"] == "converted"
            ),
            "unsupported": sum(
                1 for entry in coverage_cases if entry["status"] == "unsupported"
            ),
        },
    }
    manifest = {
        "schema_version": "2",
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "target_kind": "agent_run",
        "cases": cases,
    }
    return ConversionResult(
        manifest=manifest,
        materials=tuple(materials),
        coverage=coverage,
    )


# ---------------------------------------------------------------------------
# 单 case 转换
# ---------------------------------------------------------------------------


def _convert_case(path: Path, raw: Mapping[str, Any]) -> dict[str, Any]:
    case_id = path.stem
    unsupported: list[str] = []
    unknown_top = sorted(set(raw) - _SOURCE_KEYS)
    if unknown_top:
        unsupported.append(f"未知顶层字段: {unknown_top}")
    # replay_via=task_api 的源样例经 task API action 创建任务（非聊天通道）
    replay_via = raw.get("messages", [{}])[0].get("replay_via") if isinstance(
        raw.get("messages"), list
    ) and raw.get("messages") else None
    channel = raw.get("channel")
    profile = _CHANNEL_PROFILES.get(channel)
    if replay_via == "task_api":
        profile = "task_api"
    if profile is None:
        unsupported.append(f"未知 channel: {channel!r}")
    messages = raw.get("messages")
    if not isinstance(messages, list) or not messages:
        unsupported.append("messages 必须是非空数组")
        messages = []
    for position, message in enumerate(messages):
        if not isinstance(message, Mapping):
            unsupported.append(f"messages[{position}] 不是 JSON 对象")
            continue
        unknown_message = sorted(set(message) - _MESSAGE_KEYS)
        if unknown_message:
            unsupported.append(f"messages[{position}] 未知字段: {unknown_message}")
        expect = message.get("expect")
        if isinstance(expect, Mapping):
            unknown_expect = sorted(set(expect) - _EXPECT_KEYS)
            if unknown_expect:
                unsupported.append(
                    f"messages[{position}].expect 未知字段: {unknown_expect}"
                )
        else:
            unsupported.append(f"messages[{position}] 缺少 expect.rubric")

    steps: list[dict[str, Any]] = []
    assertions: list[dict[str, Any]] = []
    expected_steps: dict[str, dict[str, str]] = {}
    coverages: dict[str, str] = {}
    image_step_assets: list[str] = []
    for position, message in enumerate(messages):
        if not isinstance(message, Mapping):
            continue
        expect = message.get("expect") or {}
        rubric = expect.get("rubric") if isinstance(expect, Mapping) else None
        if not isinstance(rubric, str) or not rubric:
            continue
        step_id = f"turn-{position + 1}"
        action_params = message.get("action_params")
        if action_params is not None:
            if not isinstance(action_params, Mapping) or sorted(
                set(action_params) - {"title", "body", "goal_mode", "priority"}
            ):
                unsupported.append(
                    f"messages[{position}].action_params 含非白名单字段"
                )
                continue
            steps.append({
                "step_id": step_id,
                "action": "task_create",
                "payload": dict(action_params),
            })
        else:
            payload: dict[str, Any] = {"message": message.get("content")}
            image_asset = message.get("image_asset")
            if image_asset is not None:
                payload["image_asset"] = image_asset
                image_step_assets.append(str(image_asset))
            steps.append({
                "step_id": step_id,
                "action": "chat",
                "payload": payload,
            })
        expected_steps[step_id] = {"rubric": rubric}
        for tool in expect.get("tools", []) or []:
            assertions.append({
                "assertion_type": "tool_succeeded",
                "step_id": step_id,
                "params": {"tool": str(tool)},
            })
        for tool in expect.get("forbidden_tools", []) or []:
            assertions.append({
                "assertion_type": "tool_absent",
                "step_id": step_id,
                "params": {"tool": str(tool)},
            })
        # rubric 语义覆盖：默认由语义评分承接（未机械化不得只留硬断言）
        coverages[rubric] = "agent.scenario_quality"

    # side_effects -> 结构化断言（未知类型已进入 unsupported）
    for effect in raw.get("side_effects") or []:
        if not isinstance(effect, Mapping):
            unsupported.append(f"side_effects 条目不是 JSON 对象: {effect!r}")
            continue
        effect_type = effect.get("type")
        if effect_type not in _SIDE_EFFECT_TYPES:
            unsupported.append(f"未知 side_effect 类型: {effect_type!r}")
            continue
        if effect_type == "task_terminal":
            assertions.append({
                "assertion_type": "task_terminal",
                "step_id": None,
                "params": {
                    "status": str(effect.get("status", "succeeded")),
                    "within_seconds": int(effect.get("timeout", 600)),
                },
            })
        elif effect_type == "workspace_file":
            assertions.append({
                "assertion_type": "file_content",
                "step_id": None,
                "params": {
                    "filename": Path(str(effect.get("path", ""))).name,
                    "contains": str(effect.get("expected_content", "")),
                },
            })
        elif effect_type == "artifact_registered":
            params: dict[str, Any] = {"min_count": int(effect.get("count", 1))}
            if effect.get("kind"):
                params["artifact_kind"] = str(effect["kind"])
            if effect.get("storage_ref_prefix"):
                # T10：storage_ref 实际核对目标（受控内部端口回读，
                # 基名集合来自本样例 workspace_file 声明）
                params["storage_ref_prefix"] = str(effect["storage_ref_prefix"])
                params["workspace_files"] = sorted({
                    str(w.get("path", ""))
                    for w in raw.get("side_effects") or []
                    if isinstance(w, Mapping)
                    and w.get("type") == "workspace_file" and w.get("path")
                })
            assertions.append({
                "assertion_type": "artifact_set",
                "step_id": None,
                "params": params,
            })

    rules = _CASE_RULES.get(case_id, {})
    if not rules and not unsupported:
        # 未登记规则的 case：转换规则表必须逐项明确（spec 4.2），
        # 不允许未审核的自动断言生成
        unsupported.append("case 未登记转换规则（CASE_RULES）")
    if profile == "acp":
        assertions.append({
            "assertion_type": "protocol_sequence",
            "step_id": None,
            "params": {"entries": list(_ACP_PROTOCOL_STEPS)},
        })
    if profile == "cli":
        # plan T11：CLI 通道真实入口核验（来源/会话绑定/步骤退出与输出）
        assertions.append({
            "assertion_type": "cli_channel",
            "step_id": None,
            "params": {},
        })
    contains_text = rules.get("result_contains")
    if contains_text:
        assertions.append({
            "assertion_type": "result_contains",
            "step_id": steps[0]["step_id"] if steps else None,
            "params": {"text": contains_text},
        })
    kb_rule = rules.get("kb_assertion")
    if kb_rule:
        # plan T8：N-KB 检索引用纪律（整场景级断言，step_id None）
        assertions.append({
            "assertion_type": "kb_source_and_quote",
            "step_id": None,
            "params": {
                "min_results": int(kb_rule.get("min_results", 1)),
                "require_quote": bool(kb_rule.get("require_quote", False)),
            },
        })
    if rules.get("browser_state_closed"):
        # plan T9：浏览器终态（turn-2 关闭后回读 closed/确认不存在）
        assertions.append({
            "assertion_type": "browser_state",
            "step_id": None,
            "params": {"state": "closed"},
        })
    if assertions:
        coverages["工具调用/禁用工具/副作用与输出包含的确定性核验"] = (
            "agent.scenario_assertions"
        )

    options: dict[str, Any] = {}
    bindings: dict[str, dict[str, str]] = {}
    source_options = raw.get("options") or {}
    known_option_keys = {"external_memory_enabled"}
    unknown_option = sorted(set(source_options) - known_option_keys)
    if unknown_option:
        unsupported.append(f"未知 options 字段: {unknown_option}")
    alias = rules.get("options_alias")
    if "external_memory_enabled" in source_options and alias:
        options["external_memory_enabled"] = alias
        material = rules.get("binding_material")
        fact = rules.get("binding_fact", "")
        if material:
            bindings[alias] = {
                "material": material,
                "sha256": hashlib.sha256(fact.encode("utf-8")).hexdigest(),
            }

    transformations = list(rules.get("extra_transformations", []))
    if raw.get("source_session_id"):
        transformations.append(
            "剥离源会话身份等原始身份标识，仅保留仓库路径与源文件 hash 溯源"
        )

    materials: list[tuple[str, bytes]] = []
    fixture_fields: dict[str, str] = {}
    inline_images: dict[int, str] = {}
    asset = rules.get("fixture_asset")
    if asset:
        asset_path = path.parent / asset
        content = asset_path.read_bytes()
        fixture_rel = f"fixtures/{Path(asset).name}"
        materials.append((fixture_rel, content))
        fixture_fields = {
            "fixture": fixture_rel,
            "fixture_sha256": hashlib.sha256(content).hexdigest(),
        }
        # 图片随输入内联为 data URL（与 v1 相同形态）：执行层只透传消息，
        # 不解析路径引用；内联字节与 fixture 同源（三路字节纪律）
        suffix = Path(asset).suffix.lstrip(".").lower() or "png"
        mime = f"image/{'jpeg' if suffix == 'jpg' else suffix}"
        for position, message in enumerate(messages):
            if isinstance(message, Mapping) and message.get("image_asset") is not None:
                inline_images[position] = (
                    f"data:{mime};base64,"
                    + base64.b64encode(content).decode("ascii")
                )
                transformations.append(
                    "图片素材原样保留为数据集 fixture，并以内联 data URL 随输入进入消息"
                    "（内联字节与 fixture 同源）"
                )
    for image_asset in image_step_assets:
        if not rules.get("fixture_asset"):
            unsupported.append(f"消息引用图片素材 {image_asset!r} 但规则未登记 fixture")

    coverage_entry = {
        "case_id": case_id,
        "status": "unsupported" if unsupported else "converted",
        "unsupported": unsupported,
        "profile": profile,
        "steps": [step["step_id"] for step in steps],
        "assertions": [
            item["assertion_type"] for item in assertions
        ],
        "transformations": transformations,
        "coverage": coverages,
    }
    if unsupported or not steps:
        return {
            "case": None,
            "materials": [],
            "coverage_entry": coverage_entry,
        }

    comparison_scope = _COMPARISON_SCOPES.get(case_id, "fixed_fixture")
    source: dict[str, Any] = {
        "repo_path": f"{SOURCE_DIR_RELPATH}/{path.name}",
        "source_case_id": str(raw.get("id", case_id)),
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rules_version": CONVERSION_RULES_VERSION,
        "transformations": transformations,
    }
    if comparison_scope == "live_smoke":
        # live 来源标采集时间（plan T6）：live_smoke 样例依赖运行期外部
        # 服务，采集时间标识其时效边界
        source["source_captured_at"] = datetime.now(
            timezone.utc
        ).isoformat()
    scenario = {
        "execution": {
            "profile": profile,
            "steps": steps,
            "timeout_seconds": 600 if profile in ("task_api", "agent") else 300,
            "step_timeouts": (
                {"turn-1": 600} if profile == "task_api" else None
            ),
        },
        "requirements": _requirements(raw, assertions),
        "options": options,
        "assertions": assertions,
        "expected_output": {"steps": expected_steps},
        "bindings": bindings,
        "lifecycle": {
            "release_rule": _RELEASE_RULE,
            "retention_rule": _RETENTION_RULE,
        },
        "required_metrics": coverages,
        "source": source,
        "comparison_scope": comparison_scope,
    }
    case = {
        "case_id": case_id,
        "input": _case_input(raw, messages, steps, inline_images),
        "target_kind": "agent_run",
        "expected_output": {
            "steps": expected_steps,
        },
        "scenario": scenario,
        **fixture_fields,
    }
    return {
        "case": case,
        "materials": materials,
        "coverage_entry": coverage_entry,
    }


def _requirements(raw: Mapping[str, Any], assertions: list[dict[str, Any]]) -> dict[str, Any]:
    """requires 能力 -> evidence_capabilities；expect.tools 并集 -> required_tools。"""
    capabilities: list[str] = []
    for entry in raw.get("requires") or []:
        name = str(entry)
        if name not in capabilities:
            capabilities.append(name)
    tools: list[str] = []
    for assertion in assertions:
        if assertion["assertion_type"] == "tool_succeeded":
            tool = assertion["params"]["tool"]
            if tool not in tools:
                tools.append(tool)
    return {
        "required_tools": tools,
        "evidence_capabilities": capabilities,
    }


def _case_input(
    raw: Mapping[str, Any],
    messages: list[Mapping[str, Any]],
    steps: list[dict[str, Any]],
    inline_images: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    """case input：忠实保留源输入（turns 按步骤对齐），不携带身份。

    声明图片素材的轮次内联 data URL messages（与 v1 相同形态）：
    执行层（_turn_messages）只透传显式 messages 或 message 文本，不解析
    路径引用，因此图片必须随输入进入消息。"""
    inline_images = inline_images or {}
    turns: list[dict[str, Any]] = []
    for position, (step, message) in enumerate(zip(steps, messages)):
        turn: dict[str, Any] = {"step_id": step["step_id"]}
        if step["action"] == "task_create":
            turn["action"] = "task_create"
            turn["task"] = dict(step["payload"])
        else:
            turn["message"] = message.get("content")
            if message.get("image_asset") is not None:
                turn["image_asset"] = message["image_asset"]
                data_url = inline_images.get(position)
                if data_url:
                    turn["messages"] = [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": turn["message"]},
                            {
                                "type": "image_url",
                                "image_url": {"url": data_url},
                            },
                        ],
                    }]
        turns.append(turn)
    return {
        "title": raw.get("title", ""),
        "turns": turns,
    }
