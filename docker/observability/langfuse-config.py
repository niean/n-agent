"""Standard-library generation of the opt-in, private Langfuse Collector fork."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

DEFAULT_ENDPOINT = 'http://n-langfuse-web:3000/api/public/otel/v1/traces'
IMAGE = 'n-agent-langfuse-collector:0.137.0'
CONTENT = ('input_messages', 'output_messages', 'user_message', 'assistant_message', 'tool_arguments', 'tool_result', 'system_prompt', 'tool_definitions', 'error', 'retrieval_query', 'retrieval_results', 'judge_verdict', 'judge_reason')
SUFFIXES = ('original_bytes', 'truncated', 'incomplete', 'observed_bytes')
OBJECTS = ('agent', 'model', 'tool', 'context', 'task', 'delegation', 'schedule', 'memory', 'skill', 'curator', 'gateway', 'request', 'http', 'budget', 'policy', 'observation', 'knowledge')
IDENTITY = ('gen_ai.agent.name', 'nagent.agent.run_id', 'nagent.agent.source', 'nagent.agent.iteration', 'nagent.request.id', 'nagent.request.channel', 'nagent.task.id', 'nagent.task.run_id', 'nagent.task.state', 'nagent.delegation.id', 'nagent.delegation.member_id', 'nagent.delegation.role', 'nagent.schedule.task_id', 'nagent.schedule.claim_id', 'nagent.schedule.delivery_status')
FACTS = ('error.type', 'nagent.tool.executed', 'nagent.tool.policy_outcome', 'nagent.tool.risk_level', 'gen_ai.tool.name', 'gen_ai.tool.call.id', 'gen_ai.tool.type', 'gen_ai.provider.name', 'gen_ai.response.finish_reasons', 'gen_ai.request.stream', 'nagent.model.usage_known', 'nagent.model.message_count', 'nagent.model.duration_ms', 'nagent.model.cost_reason', 'nagent.model.cost_currency', 'nagent.model.cost_decimal', 'nagent.model.price_id', 'nagent.model.billing_provider', 'nagent.model.billing_model', 'nagent.agent.end_reason', 'nagent.agent.message_count', 'nagent.agent.inference_calls', 'nagent.agent.tool_calls', 'nagent.agent.usage_known', 'nagent.context.before_tokens', 'nagent.context.after_tokens', 'nagent.memory.provider', 'nagent.task.achieved', 'nagent.task.judge_attempt', 'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens', 'nagent.model.cache_read_tokens', 'nagent.model.cache_write_tokens', 'nagent.model.cost_status', 'nagent.model.pricing_version', 'gen_ai.request.model', 'gen_ai.response.model', *(f'nagent.{obj}.{field}' for obj in OBJECTS for field in ('operation', 'outcome')), 'nagent.evaluation.operation')
PARAMETERS = ('temperature', 'top_p', 'top_k', 'max_tokens', 'max_completion_tokens', 'max_output_tokens', 'frequency_penalty', 'presence_penalty')
MODEL = ('gen_ai.request.model', 'gen_ai.response.model', 'nagent.model.completion_start_time', *(f'nagent.model.parameter.{key}' for key in PARAMETERS))
# 成本金额键（cost_usd 与四个分项桶）；decimal 是受控十进制字符串，单独守卫。
COST_AMOUNT_KEYS = ('nagent.model.cost_usd', 'nagent.model.cost_input_usd', 'nagent.model.cost_output_usd', 'nagent.model.cost_cache_read_usd', 'nagent.model.cost_cache_write_usd')
# 分项桶（总桶 cost_usd 之外）：input 两个操作都适用，其余分项仅 chat 适用。
COST_COMPONENT_KEYS = COST_AMOUNT_KEYS[1:]
# 双管线共享的 double 容差降级模板：|sum - total| <= max(1e-12, total*1e-9)。
# OTTL 0.137.0 无 Abs，等价改写为四次比较的析取（total 经守卫保证非负）。
# N-Agent 与 KB 两条管线必须保持同步，各自以 sum/total 占位符填充。
COST_TOLERANCE_STATEMENT = ('set(cache["cost"]["ok"], false) where cache["cost"]["ok"] == true and not '
                            '(({sum} <= {total} + 0.000000000001 and {sum} >= {total} - 0.000000000001) or '
                            '({sum} <= {total} + {total} * 0.000000001 and {sum} >= {total} - {total} * 0.000000001))')
# T11 评估投影边界（spec 11.2/12.1）：受信本地编排注入的实验身份经封闭白名单
# 映射为 langfuse.experiment.*；evaluation.run 评估 Trace 独立映射 evaluator。
# 客户端 baggage/header 永不直接投影：映射只认 nagent.evaluation.experiment.*
# 源键，purge 守卫先删非法形态，再精确映射；源键不入 metadata（仅顶层投影）。
EXPERIMENT_ATTRIBUTE_MAP = (
    ('nagent.evaluation.experiment.id', 'langfuse.experiment.id'),
    ('nagent.evaluation.experiment.name', 'langfuse.experiment.name'),
    ('nagent.evaluation.experiment.dataset_id', 'langfuse.experiment.dataset_id'),
    ('nagent.evaluation.experiment.item_id', 'langfuse.experiment.item_id'),
    ('nagent.evaluation.experiment.root_observation_id', 'langfuse.experiment.root_observation_id'),
)
SPAN_KEYS = tuple(dict.fromkeys(('gen_ai.operation.name', 'gen_ai.conversation.id', *IDENTITY, *FACTS, *MODEL, *COST_AMOUNT_KEYS, *(source for source, _ in EXPERIMENT_ATTRIBUTE_MAP), *(f'nagent.content.{field}{suffix}' for field in CONTENT for suffix in ('', *(f'_{x}' for x in SUFFIXES))))))
BOOL_KEYS = ('nagent.tool.executed', 'gen_ai.request.stream', 'nagent.model.usage_known', 'nagent.agent.usage_known', 'nagent.task.achieved')
COUNT_KEYS = ('nagent.agent.iteration', 'nagent.agent.message_count', 'nagent.agent.inference_calls', 'nagent.agent.tool_calls', 'nagent.model.message_count', 'nagent.context.before_tokens', 'nagent.context.after_tokens', 'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens', 'nagent.model.cache_read_tokens', 'nagent.model.cache_write_tokens')
CUSTOM_TYPES = {'task.run': 'chain', 'delegation.run_member': 'chain', 'schedule.run': 'chain', 'gateway.handle_message': 'chain', 'skill.review': 'chain', 'curator.run': 'chain', 'knowledge.search': 'retriever', 'memory.prefetch': 'retriever', 'memory.search': 'retriever', 'task.evaluate': 'evaluator'}
# This is a fixed diagnostic enum, never a pass-through of arbitrary error text.
ERROR_TYPES = ('TimeoutError', 'ConnectionError', 'CancelledError', 'ValueError', 'ValidationError', 'RateLimitError', 'BadRequestError', 'AuthenticationError', 'PermissionDeniedError', 'NotFoundError', 'PolicyDenied', 'APIError', 'APIConnectionError', 'APITimeoutError', 'APIStatusError', 'APIResponseValidationError', 'InternalServerError', 'ConflictError', 'UnprocessableEntityError', 'OverloadedError', 'RequestTooLargeError', 'tool_error', 'denied', 'cancelled', 'other')
# N-KB projection vocabulary mirrors app/application/telemetry_contract.py.
KB_ERROR_TYPES = ('unsupported_file_type', 'file_too_large', 'embedding_failed', 'vector_index_failed', 'sqlite_failed', 'timeout', 'other')
# 八类不可估原因，与 app/domain/cost.py UnknownReason 词汇一致；estimated 无 reason。
COST_REASONS = ('missing_usage', 'invalid_usage', 'price_not_found', 'price_ambiguous', 'unsupported_pricing', 'identity_unresolved', 'pricing_unavailable', 'amount_unrepresentable')
# 成本金额键只走 cost_details 分项投影，绝不进入 metadata；decimal 是受控
# 十进制字符串，随诊断键进 metadata。
KB_COST_AMOUNT_KEYS = ('nkb.model.cost_usd', 'nkb.model.cost_input_usd')
# 成组守卫的全部成本相关键（金额/分项/decimal/状态/原因/币种/版本/价格项/计费身份）。
# 任一键"存在但非法"必须在字段清理（purge）之前登记整组非法：清理会删除
# 非法属性，后行的成组校验不得让整组伪装成"合法缺席"逃逸。
COST_GROUP_GUARD_KEYS = (*COST_AMOUNT_KEYS, 'nagent.model.cost_decimal', 'nagent.model.cost_status', 'nagent.model.cost_currency', 'nagent.model.cost_reason', 'nagent.model.pricing_version', 'nagent.model.price_id', 'nagent.model.billing_provider', 'nagent.model.billing_model')
KB_COST_GROUP_GUARD_KEYS = (*KB_COST_AMOUNT_KEYS, 'nkb.model.cost_decimal', 'nkb.model.cost_status', 'nkb.model.cost_reason', 'nkb.model.cost_currency', 'nkb.model.pricing_version', 'nkb.model.price_id', 'nkb.model.billing_provider', 'nkb.model.billing_model')


def illegal_group_statements(group_keys, guard):
    """生成"存在但非法登记"语句：字段缺席合法（unknown 组按设计无金额），
    存在但未通过守卫即登记整组非法，与 purge 的删除判定完全一致。"""
    return [f'set(cache["cost"]["illegal"], true) where attributes["{key}"] != nil and not ({guard(key)})'
            for key in group_keys]
KB_METADATA_KEYS = ('nkb.index.operation', 'nkb.index.outcome', 'nkb.index.duration_ms', 'nkb.retrieval.operation', 'nkb.retrieval.outcome', 'nkb.retrieval.duration_ms', 'nkb.index.document_id', 'nkb.index.job_id', 'nkb.index.chunk_count', 'nkb.retrieval.result_count', 'nkb.model.usage_known', 'nkb.model.cost_status', 'nkb.model.cost_reason', 'nkb.model.cost_currency', 'nkb.model.cost_decimal', 'nkb.model.pricing_version', 'nkb.model.price_id', 'nkb.model.billing_provider', 'nkb.model.billing_model', 'gen_ai.request.model', 'gen_ai.provider.name', 'error.type')
KB_CONTENT = ('index_input', 'index_result', 'retrieval_input', 'retrieval_output', 'embedding_input')
KB_SPAN_KEYS = tuple(dict.fromkeys(('gen_ai.operation.name', 'gen_ai.provider.name', 'gen_ai.request.model', 'gen_ai.usage.input_tokens', 'error.type', *KB_METADATA_KEYS, *KB_COST_AMOUNT_KEYS, *(f'nkb.content.{field}{suffix}' for field in KB_CONTENT for suffix in ('', *(f'_{x}' for x in SUFFIXES))))))
KB_BOOL_KEYS = ('nkb.model.usage_known',)
KB_OUTCOMES = ('success', 'failure', 'denied', 'cancelled', 'timed_out', 'skipped', 'deferred', 'unknown')
KB_OPERATIONS = {'nkb.index.operation': ('run', 'parse', 'split', 'embed', 'write'), 'nkb.retrieval.operation': ('search',)}
KB_EVENTS = ('nkb.document.completed', 'nkb.index.completed', 'nkb.retrieval.completed', 'nkb.model.completed', 'nkb.mcp.completed', 'nkb.http.completed')
KB_EVENT_KEYS = ('error.type', 'nkb.model.usage_known', *(f'nkb.{obj}.outcome' for obj in ('document', 'index', 'retrieval', 'model', 'mcp', 'request', 'http')))


def value_guard(key, error_types=ERROR_TYPES):
    """OTTL's typed guards reject malformed source attributes before projection."""
    value = f'attributes["{key}"]'
    if key in BOOL_KEYS or key in KB_BOOL_KEYS or key.endswith(('_truncated', '_incomplete')):
        return f'IsBool({value})'
    if key == 'error.type':
        return 'IsString(' + value + ') and (' + ' or '.join(f'{value} == {json.dumps(error)}' for error in error_types) + ')'
    if key == 'nagent.task.judge_attempt':
        return f'IsInt({value}) and {value} > 0'
    if key in COUNT_KEYS or key.endswith(('_original_bytes', '_observed_bytes')):
        return f'IsInt({value}) and {value} >= 0'
    if key in ('nagent.model.cost_status', 'nagent.model.cost_currency', 'nagent.model.cost_reason'):
        # 成本判定词汇受控：status/currency 固定枚举，reason 用八类原因白名单。
        allowed = {'nagent.model.cost_status': ('estimated', 'unknown'),
                   'nagent.model.cost_currency': ('USD',),
                   'nagent.model.cost_reason': COST_REASONS}[key]
        return 'IsString(' + value + ') and (' + ' or '.join(f'{value} == {json.dumps(item)}' for item in allowed) + ')'
    if key == 'nagent.model.cost_decimal':
        # 规范十进制字符串（无指数、无符号、无冗余尾零），与 app/main.py
        # _canonical_decimal_string 的产出逐字节对齐；有界长度拒绝超长伪造。
        pattern = r'^\d{1,340}([.]\d{1,340})?$'
        return f'IsString({value}) and IsMatch({value}, {json.dumps(pattern)})'
    if key in ('nagent.model.price_id', 'nagent.model.pricing_version'):
        # 有界计价标识：与 KB 管线同 pattern（KB pricing_version 上限 129）。
        pattern = r'^[\w.:/@+-]{1,128}$' if key == 'nagent.model.price_id' else r'^[\w.:/@+-]{1,129}$'
        return f'IsString({value}) and IsMatch({value}, {json.dumps(pattern)})'
    if key in ('nagent.model.billing_provider', 'nagent.model.billing_model'):
        # 受信计费身份：有界开放字符串，拒绝控制字符与超长值（不白名单化）。
        pattern = r'^[^\x00-\x1f\x7f]{1,64}$'
        return f'IsString({value}) and IsMatch({value}, {json.dumps(pattern)})'
    if key.startswith('nagent.model.parameter.') or (key.startswith('nagent.model.cost_') and key.endswith('_usd')):
        # A finite double equals itself and lies inside IEEE's finite range.
        finite = f'((IsInt({value})) or (IsDouble({value}) and {value} == {value} and {value} <= 1.7976931348623157e308 and {value} >= -1.7976931348623157e308))'
        return finite + (f' and {value} >= 0' if key.startswith('nagent.model.cost_') else '')
    if key == 'gen_ai.response.finish_reasons':
        # OTel attributes are homogeneous slices; the JSON representation also
        # excludes numeric, boolean and nested list values at this boundary.
        pattern = r'^\[("([^"\\]|\\.)*"(, ?"([^"\\]|\\.)*")*)?\]$'
        return f'IsList({value}) and IsMatch(String({value}), {json.dumps(pattern)})'
    if key == 'nagent.evaluation.operation':
        # evaluation.run 评估 Trace 判别键：固定单一值（producer 常量，
        # app/application/evaluation_telemetry.EVALUATION_SPAN_NAME）。
        return f'IsString({value}) and {value} == "evaluation.run"'
    if key.startswith('nagent.evaluation.experiment.'):
        # 受信实验身份：有界开放标识（ID 模式）；name 拒绝控制字符。
        pattern = r'^[^\x00-\x1f\x7f]{1,64}$' if key.endswith('.name') else r'^[\w.:/-]{1,128}$'
        return f'IsString({value}) and IsMatch({value}, {json.dumps(pattern)})'
    if key.endswith('.duration_ms'):
        # Contract "number": non-negative finite int or double; a finite double
        # equals itself and lies inside IEEE's finite range. Bools never pass.
        # 与 kb_value_guard 的 .duration_ms 分支同语义（attempt-10：IsString
        # 默认分支把 double 形态的 nagent.model.duration_ms 删除，metadata
        # 投影恒空，同口径耗时核对退化为 SKIP）。
        finite = f'((IsInt({value})) or (IsDouble({value}) and {value} == {value} and {value} <= 1.7976931348623157e308 and {value} >= -1.7976931348623157e308))'
        return f'{finite} and {value} >= 0'
    return f'IsString({value})'


def kb_value_guard(key):
    """KB guards mirror N-KB telemetry_contract.valid_attribute exactly."""
    value = f'attributes["{key}"]'
    if key in KB_OPERATIONS:
        return 'IsString(' + value + ') and (' + ' or '.join(f'{value} == {json.dumps(action)}' for action in KB_OPERATIONS[key]) + ')'
    if key == 'nkb.model.cost_status':
        return f'IsString({value}) and ({value} == "estimated" or {value} == "unknown")'
    if key == 'nkb.model.cost_reason':
        # 八类不可估原因加 none（estimated 判定固定为 none），镜像 KB 契约。
        return 'IsString(' + value + ') and (' + ' or '.join(f'{value} == {json.dumps(item)}' for item in (*COST_REASONS, 'none')) + ')'
    if key == 'nkb.model.cost_currency':
        return f'IsString({value}) and {value} == "USD"'
    if key == 'nkb.model.billing_provider':
        return f'IsString({value}) and ({value} == "ollama" or {value} == "other")'
    if key == 'nkb.model.cost_decimal':
        # 规范非负十进制字符串（无符号、无指数），逐侧 340 位，镜像 N-KB
        # COST_DECIMAL_PATTERN 与 Domain 投影上限（超界金额在 Domain 按
        # amount_unrepresentable 收口，与 nagent.model.cost_decimal 同界）。
        pattern = r'^\d{1,340}([.]\d{1,340})?$'
        return f'IsString({value}) and IsMatch({value}, {json.dumps(pattern)})'
    if key == 'nkb.model.pricing_version':
        pattern = r'^[\w.:/@+-]{1,129}$'
        return f'IsString({value}) and IsMatch({value}, {json.dumps(pattern)})'
    if key == 'nkb.model.price_id':
        pattern = r'^[\w.:/@+-]{1,128}$'
        return f'IsString({value}) and IsMatch({value}, {json.dumps(pattern)})'
    if key == 'nkb.model.billing_model':
        # 受信计费模型名：有界开放集合，拒绝控制字符（不白名单化）。
        pattern = r'^[^\x00-\x1f\x7f]{1,64}$'
        return f'IsString({value}) and IsMatch({value}, {json.dumps(pattern)})'
    if key in ('nkb.model.cost_usd', 'nkb.model.cost_input_usd'):
        # Contract "number": non-negative finite int or double; a finite double
        # equals itself and lies inside IEEE's finite range. Bools never pass.
        finite = f'((IsInt({value})) or (IsDouble({value}) and {value} == {value} and {value} <= 1.7976931348623157e308 and {value} >= -1.7976931348623157e308))'
        return f'{finite} and {value} >= 0'
    if key.endswith('.outcome'):
        return 'IsString(' + value + ') and (' + ' or '.join(f'{value} == {json.dumps(outcome)}' for outcome in KB_OUTCOMES) + ')'
    if key == 'gen_ai.provider.name':
        return f'IsString({value}) and {value} == "ollama"'
    if key == 'gen_ai.operation.name':
        return f'IsString({value}) and {value} == "embeddings"'
    if key in ('nkb.index.document_id', 'nkb.index.job_id'):
        # Raw regex literals cannot appear inside f-string expressions on
        # Python < 3.12 (PEP 701); precompute to stay 3.11-compatible.
        id_pattern = r'^[\w.:/-]{1,128}$'
        return f'IsString({value}) and IsMatch({value}, {json.dumps(id_pattern)})'
    if key == 'gen_ai.request.model':
        model_pattern = r'^[\w.:/@_-]{1,64}$'
        return f'IsString({value}) and IsMatch({value}, {json.dumps(model_pattern)})'
    if key.endswith('.duration_ms'):
        # Contract "number": non-negative finite int or double; a finite double
        # equals itself and lies inside IEEE's finite range. Bools never pass.
        finite = f'((IsInt({value})) or (IsDouble({value}) and {value} == {value} and {value} <= 1.7976931348623157e308 and {value} >= -1.7976931348623157e308))'
        return f'{finite} and {value} >= 0'
    if key in ('nkb.index.chunk_count', 'nkb.retrieval.result_count'):
        return f'IsInt({value}) and {value} >= 0'
    return value_guard(key, KB_ERROR_TYPES)


def validate_endpoint(endpoint):
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
        valid = parsed.scheme in ('http', 'https') and parsed.hostname and parsed.username is None and parsed.password is None and '?' not in endpoint and '#' not in endpoint and not any(c.isspace() for c in endpoint)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Langfuse endpoint must be an http(s) URL without credentials, query or fragment')
    return endpoint


def settings(directory, root):
    """Read only our literal settings from shared dotenv; leave base Compose semantics alone."""
    keys = ('N_OBSERVABILITY_LANGFUSE_ENABLED', 'LANGFUSE_ENV_FILE', 'LANGFUSE_OTEL_ENDPOINT', 'LANGFUSE_KB_OTEL_ENDPOINT')
    values = {}
    path = Path(directory) / '.env'
    if path.is_file():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition('=')
            if sep and key in keys:
                if key in values:
                    raise ValueError('Duplicate Langfuse setting')
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key] = value
    values.update({key: os.environ[key] for key in keys if key in os.environ})
    enabled = values.get(keys[0], 'false').lower()
    if enabled not in ('true', 'false', '1', '0'):
        raise ValueError('N_OBSERVABILITY_LANGFUSE_ENABLED must be true or false')
    path = Path(values.get(keys[1], 'docker/langfuse/.env'))
    path = path if path.is_absolute() else Path(root) / path
    endpoint = values.get(keys[2], DEFAULT_ENDPOINT)
    kb_endpoint = values.get(keys[3], DEFAULT_ENDPOINT)
    if enabled in ('true', '1'):
        validate_endpoint(endpoint)
        validate_endpoint(kb_endpoint)
    return enabled in ('true', '1'), path, endpoint, kb_endpoint


def span_statements():
    agent = 'resource.attributes["service.name"] == "n-agent"'
    chat = f'{agent} and attributes["gen_ai.operation.name"] == "chat"'
    statements = [f'keep_keys(attributes, {json.dumps(SPAN_KEYS)})', 'set(status.message, "")']
    # 成本相关字段"存在但非法"必须在字段清理之前登记整组非法状态：
    # purge 会删除非法属性，后行的成组校验不得让整组伪装成"合法缺席"逃逸
    # （estimated 带非法 reason 被清理后伪装成 reason 缺席、非法可选
    # cache_write 分项被清理后伪装成缺席）。字段缺席本身合法（unknown 组
    # 按设计无金额），只有存在但未通过守卫才登记——判定与 purge 完全一致。
    statements += ['set(cache["cost"], {})', 'set(cache["cost"]["illegal"], false)']
    statements += illegal_group_statements(COST_GROUP_GUARD_KEYS, value_guard)
    # unknown 组显式校验（评审修复第三轮；spec：unknown 必须有受控 reason 且
    # 不得携带任何金额）：缺 reason 或携带任一金额/分项/decimal 即登记整组
    # 非法。语句先于 purge——非法金额会被守卫删除、合法形态金额会存活到
    # ok==false 门禁，登记必须发生在这两者之前，缺 reason 的伪装形态（清理
    # 后缺席）也不得逃逸。unknown 组拒绝语义与非法组一致：ok 恒 false 阻断
    # cost_details 与 model.name，金额经 ok==false 删除，status 保留进
    # metadata 供拒绝审计。
    nagent_unknown = 'attributes["nagent.model.cost_status"] == "unknown"'
    nagent_unknown_amount = ' or '.join(
        f'attributes["{key}"] != nil' for key in (*COST_AMOUNT_KEYS, 'nagent.model.cost_decimal'))
    statements.append(
        f'set(cache["cost"]["illegal"], true) where {nagent_unknown} and '
        f'(attributes["nagent.model.cost_reason"] == nil or {nagent_unknown_amount})')
    # The purge is first. Guards run before comparisons or downstream copying.
    for key in SPAN_KEYS:
        statements.append(f'delete_key(attributes, "{key}") where attributes["{key}"] != nil and not ({value_guard(key)})')
    statements += ['set(cache["known_agent_root"], false)', 'set(cache["known_agent_root"], true) where name == "invoke_agent n-agent"', 'set(name, "span")', 'set(attributes["langfuse.observation.type"], "span")', 'set(cache["entry"], false)']
    for operation, kind in [('invoke_agent', 'agent'), ('chat', 'generation'), ('execute_tool', 'tool')]:
        condition = f'{agent} and attributes["gen_ai.operation.name"] == "{operation}"'
        statements += [f'set(name, "{operation}") where {condition}', f'set(attributes["langfuse.observation.type"], "{kind}") where {condition}']
    statements.append(f'set(name, "invoke_agent n-agent") where {agent} and attributes["gen_ai.operation.name"] == "invoke_agent" and cache["known_agent_root"] == true')
    # Custom operations have their own canonical attribute, never gen_ai.operation.name.
    custom_names = dict(CUSTOM_TYPES)
    for operation in ('context.compress', 'task.submit', 'request.handle', 'http.handle', 'memory.prepare_prompt', 'memory.pre_compress', 'memory.sync', 'memory.execute_tool', 'memory.probe'):
        custom_names[operation] = 'span'
    for operation, kind in custom_names.items():
        obj, action = operation.split('.')
        condition = f'{agent} and attributes["nagent.{obj}.operation"] == "{action}"'
        statements += [f'set(name, "{operation}") where {condition}', f'set(attributes["langfuse.observation.type"], "{kind}") where {condition}']
        if kind == 'chain':
            statements += [f'set(cache["entry"], true) where {condition}']
    # T11 评估投影边界：evaluation.run 评估 Trace（异步评分独立 Trace，Link
    # 指向真实业务目标）独立映射 evaluator；实验身份仅来自受信本地编排注入
    # 的 nagent.evaluation.experiment.*（purge 守卫已先删非法形态），精确
    # 映射为 langfuse.experiment.*，客户端 baggage/header 永不直接投影。
    eval_run = f'{agent} and attributes["nagent.evaluation.operation"] == "evaluation.run"'
    statements += [f'set(name, "evaluation.run") where {eval_run}', f'set(attributes["langfuse.observation.type"], "evaluator") where {eval_run}']
    for source, target in EXPERIMENT_ATTRIBUTE_MAP:
        statements.append(f'set(attributes["{target}"], attributes["{source}"]) where {agent} and attributes["{source}"] != nil')
    statements += ['set(attributes["langfuse.trace.name"], name) where parent_span_id == SpanID(0x0000000000000000)', 'set(attributes["langfuse.session.id"], attributes["gen_ai.conversation.id"]) where IsString(attributes["gen_ai.conversation.id"])']
    for source, target in [('service.version', 'langfuse.release'), ('deployment.environment.name', 'langfuse.environment')]:
        value = f'resource.attributes["{source}"]'
        pattern = r'^[a-zA-Z0-9][a-zA-Z0-9._+/-]{0,199}$' if source == 'service.version' else r'^[a-z0-9][a-z0-9_-]{0,39}$'
        condition = f'{agent} and IsString({value}) and IsMatch({value}, {json.dumps(pattern)})'
        if source == 'deployment.environment.name':
            condition += f' and not IsMatch({value}, "^langfuse")'
        statements.append(f'set(attributes["{target}"], {value}) where {condition}')
    # 成本成组守卫（成组校验，不部分成功）：estimated 组必须整体合法才允许
    # 计价投影；unknown 组必须无金额。任一不满足即整组拒绝——不投影
    # cost_details、丢弃残留金额键、model.name 不投影（阻断自动推价）；
    # 独立合法 Token 不受影响。OTTL 无 Decimal 与 Abs，仅做 double 容差校验
    # （spec 允许）：|sum - total| <= max(1e-12, total*1e-9) 等价改写为四次
    # 比较的析取（total 经守卫保证非负，|total| 即 total）。
    statements += ['set(cache["cost"]["ok"], false)', 'set(cache["cost"]["sum"], 0.0)', 'set(cache["cost_details"], {})']
    estimated = 'attributes["nagent.model.cost_status"] == "estimated"'
    amount = 'attributes["nagent.model.cost_usd"]'
    decimal_key = 'nagent.model.cost_decimal'
    currency = 'attributes["nagent.model.cost_currency"]'
    reason = 'attributes["nagent.model.cost_reason"]'
    identity = ' and '.join(f'IsString(attributes["nagent.model.{key}"]) and attributes["nagent.model.{key}"] != ""' for key in ('pricing_version', 'price_id', 'billing_provider', 'billing_model'))

    def group_gate(operation, buckets):
        presence = ''.join(f' and attributes["nagent.model.cost_{bucket}_usd"] != nil' for bucket in buckets)
        return f'{operation} and {estimated} and {currency} == "USD" and {reason} == nil and {amount} != nil and attributes["{decimal_key}"] != nil and {identity}{presence} and cache["cost"]["illegal"] == false'

    embeddings = f'{agent} and attributes["gen_ai.operation.name"] == "embeddings"'
    # chat 适用分项为 input/output/cache_read（write 可缺席）；embeddings 仅 input。
    statements.append(f'set(cache["cost"]["ok"], true) where {group_gate(chat, ("input", "output", "cache_read"))}')
    statements.append(f'set(cache["cost"]["ok"], true) where {group_gate(embeddings, ("input",))}')
    for source in COST_COMPONENT_KEYS:
        # input 桶 chat/embeddings 通用；其余分项仅 chat 适用（embeddings 无该分项）。
        gate = 'cache["cost"]["ok"] == true' + ('' if source.endswith('input_usd') else f' and {chat}')
        statements.append(f'set(cache["cost"]["sum"], cache["cost"]["sum"] + attributes["{source}"]) where {gate} and attributes["{source}"] != nil')
    sum_value = 'cache["cost"]["sum"]'
    statements.append(COST_TOLERANCE_STATEMENT.format(sum=sum_value, total=amount))
    statements.append(f'set(cache["cost_details"], {{"total": {amount}}}) where cache["cost"]["ok"] == true')
    statements.append(f'set(cache["cost_details"]["input"], attributes["nagent.model.cost_input_usd"]) where cache["cost"]["ok"] == true and attributes["nagent.model.cost_input_usd"] != nil')
    for bucket in ('output', 'cache_read', 'cache_write'):
        source = f'nagent.model.cost_{bucket}_usd'
        statements.append(f'set(cache["cost_details"]["{bucket}"], attributes["{source}"]) where cache["cost"]["ok"] == true and {chat} and attributes["{source}"] != nil')
    statements.append(f'set(attributes["langfuse.observation.cost_details"], String(cache["cost_details"])) where cache["cost"]["ok"] == true')
    # 整组拒绝（含 unknown 组残留金额）时丢弃金额与 decimal；estimated 组携带
    # reason 属生产者矛盾，一并丢弃。诊断性字符串键（status/reason/currency/
    # 版本/price_id/billing_*）保留进 metadata 供拒绝审计（金额绝不投影）：
    # 存活值均已过 purge 守卫，非法字段自身已被清理删除。整组拒绝经
    # ok==false 门禁阻断 cost_details 与 model.name 自动推价，不做部分成功。
    for key in (*COST_AMOUNT_KEYS, decimal_key):
        statements.append(f'delete_key(attributes, "{key}") where cache["cost"]["ok"] == false and attributes["{key}"] != nil')
    statements.append(f'delete_key(attributes, "nagent.model.cost_reason") where {estimated} and cache["cost"]["ok"] == false and {reason} != nil')
    statements += ['set(cache["metadata"], {})']
    metadata_keys = tuple(dict.fromkeys((*IDENTITY, *FACTS)))
    for key in metadata_keys:
        statements.append(f'set(cache["metadata"]["{key}"], attributes["{key}"]) where {agent} and attributes["{key}"] != nil')
    # Each replacement assigns a fresh envelope, so completeness cannot leak
    # from a lower-priority source. Metadata bodies are independent envelopes.
    def envelope(field, target, condition):
        source = f'nagent.content.{field}'
        condition = f'{agent} and {condition} and IsString(attributes["{source}"])'
        result = [f'set({target}, {{"source": "{field}"}}) where {condition}']
        for suffix in SUFFIXES:
            key = f'{source}_{suffix}'
            result.append(f'set({target}["{suffix}"], attributes["{key}"]) where {condition} and attributes["{key}"] != nil')
        return condition, result

    for direction, fields in [('input', ('user_message', 'input_messages', 'tool_arguments', 'retrieval_query')), ('output', ('assistant_message', 'output_messages', 'tool_result', 'retrieval_results', 'judge_verdict'))]:
        for field in fields:
            condition = 'true'
            if field.startswith('tool_'):
                condition = 'attributes["gen_ai.operation.name"] == "execute_tool"'
            elif field.startswith('retrieval_'):
                condition = 'attributes["nagent.knowledge.operation"] == "search"'
            elif field.startswith('judge_'):
                condition = 'attributes["nagent.task.operation"] == "evaluate"'
            target = f'cache["metadata"]["{direction}"]'
            condition, body = envelope(field, target, condition)
            statements += [f'set(attributes["langfuse.observation.{direction}"], attributes["nagent.content.{field}"]) where {condition}', *body]
    for field in ('system_prompt', 'tool_definitions', 'error', 'judge_reason'):
        target = f'cache["metadata"]["{field}"]'
        guard = 'attributes["nagent.task.operation"] == "evaluate"' if field == 'judge_reason' else 'true'
        condition, body = envelope(field, target, guard)
        statements += [*body, f'set({target}["content"], attributes["nagent.content.{field}"]) where {condition}']
    for direction in ('input', 'output'):
        statements.append(f'set(attributes["langfuse.trace.{direction}"], attributes["langfuse.observation.{direction}"]) where {agent} and cache["entry"] == true and parent_span_id == SpanID(0x0000000000000000) and attributes["langfuse.observation.{direction}"] != nil')
    statements += ['set(cache["usage"], {})', 'set(cache["parameters"], {})']
    for bucket, source in [('input', 'gen_ai.usage.input_tokens'), ('output', 'gen_ai.usage.output_tokens'), ('cache_read', 'nagent.model.cache_read_tokens'), ('cache_write', 'nagent.model.cache_write_tokens')]:
        statements.append(f'set(cache["usage"]["{bucket}"], attributes["{source}"]) where {chat} and IsInt(attributes["{source}"]) and attributes["{source}"] >= 0')
    statements += [f'set(attributes["langfuse.observation.usage_details"], String(cache["usage"])) where {chat} and Len(cache["usage"]) > 0']
    for source in ('gen_ai.response.model', 'gen_ai.request.model'):
        # 仅合法 estimated 组投影 model.name；unknown 与残缺 estimated 均阻断
        # Langfuse 自动推价。已识别的请求模型优先，避免响应别名归一为 other
        # 覆盖配置展示名；请求未知时仍可回退响应。两者原始投影均保留在 metadata。
        known_request = f' and attributes["{source}"] != "other" and attributes["{source}"] != ""' if source == 'gen_ai.request.model' else ''
        statements.append(f'set(attributes["langfuse.observation.model.name"], attributes["{source}"]) where {chat} and {estimated} and cache["cost"]["ok"] == true and IsString(attributes["{source}"]){known_request}')
    for parameter in PARAMETERS:
        source = f'nagent.model.parameter.{parameter}'
        statements.append(f'set(cache["parameters"]["{parameter}"], attributes["{source}"]) where {chat} and attributes["{source}"] != nil')
    statements.append(f'set(attributes["langfuse.observation.model.parameters"], String(cache["parameters"])) where {chat} and Len(cache["parameters"]) > 0')
    # SDK-produced UTC RFC3339; validate its structure before publishing.
    timestamp = 'attributes["nagent.model.completion_start_time"]'
    leap_year = r'([0-9]{2}(0[48]|[2468][048]|[13579][26])|(0[48]|[2468][048]|[13579][26])00)'
    date = rf'([0-9]{{4}}-((01|03|05|07|08|10|12)-(0[1-9]|[12][0-9]|3[01])|(04|06|09|11)-(0[1-9]|[12][0-9]|30)|02-(0[1-9]|1[0-9]|2[0-8]))|{leap_year}-02-29)'
    pattern = rf'^{date}T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]([.][0-9]+)?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$'
    statements.append(f'set(attributes["langfuse.observation.completion_start_time"], {timestamp}) where {chat} and IsString({timestamp}) and IsMatch({timestamp}, {json.dumps(pattern)})')
    statements.append('set(attributes["langfuse.observation.level"], "ERROR") where status.code == STATUS_CODE_ERROR')
    # Fallback only for execution failure/timeouts; outcome alone never sets level.
    for obj in OBJECTS:
        for outcome in ('failure', 'timed_out'):
            statements.append(f'set(attributes["langfuse.observation.status_message"], "{outcome}") where status.code == STATUS_CODE_ERROR and attributes["nagent.{obj}.outcome"] == "{outcome}"')
    for error in ERROR_TYPES:
        statements.append(f'set(attributes["langfuse.observation.status_message"], "{error}") where status.code == STATUS_CODE_ERROR and attributes["error.type"] == "{error}"')
    envelope_keys = ('input', 'output', 'system_prompt', 'tool_definitions', 'error', 'judge_reason')
    for key in (*metadata_keys, *envelope_keys):
        source = f'cache["metadata"]["{key}"]'
        value = f'String({source})' if key in envelope_keys else source
        statements.append(f'set(attributes["langfuse.observation.metadata.{key}"], {value}) where {source} != nil')
    final = ('langfuse.observation.type', 'langfuse.trace.name', 'langfuse.session.id', 'langfuse.release', 'langfuse.environment', 'langfuse.trace.input', 'langfuse.trace.output', 'langfuse.observation.input', 'langfuse.observation.output', *(f'langfuse.observation.metadata.{key}' for key in (*metadata_keys, *envelope_keys)), 'langfuse.observation.usage_details', 'langfuse.observation.model.name', 'langfuse.observation.model.parameters', 'langfuse.observation.completion_start_time', 'langfuse.observation.cost_details', 'langfuse.observation.level', 'langfuse.observation.status_message', *(target for _, target in EXPERIMENT_ATTRIBUTE_MAP))
    statements += [f'keep_keys(attributes, {json.dumps(final)})']
    return statements


def span_statements_kb():
    """KB projection: an independent allowlist mirroring N-KB's telemetry contract."""
    kb = 'resource.attributes["service.name"] == "n-kb"'
    embed = f'{kb} and attributes["gen_ai.operation.name"] == "embeddings"'
    retrieval = f'{kb} and attributes["nkb.retrieval.operation"] == "search"'
    index_run = f'{kb} and attributes["nkb.index.operation"] == "run"'
    statements = [f'keep_keys(attributes, {json.dumps(KB_SPAN_KEYS)})', 'set(status.message, "")']
    # 成本相关字段"存在但非法"必须在字段清理之前登记整组非法状态（与
    # N-Agent 管线同语义）：purge 删除非法属性，成组校验不得让整组伪装成
    # "合法缺席"逃逸；字段缺席合法，存在但未通过守卫才登记。
    statements += ['set(cache["cost"], {})', 'set(cache["cost"]["illegal"], false)']
    statements += illegal_group_statements(KB_COST_GROUP_GUARD_KEYS, kb_value_guard)
    # unknown 组显式校验（与 N-Agent 管线同语义，评审修复第三轮）：缺 reason
    # 或携带任一金额/分项/decimal 即登记整组非法；KB 契约的 estimated 判定
    # 固定携带 cost_reason=none，unknown 上的 none 属生产者矛盾，一并登记。
    kb_unknown = 'attributes["nkb.model.cost_status"] == "unknown"'
    kb_unknown_amount = ' or '.join(
        f'attributes["{key}"] != nil' for key in (*KB_COST_AMOUNT_KEYS, 'nkb.model.cost_decimal'))
    statements.append(
        f'set(cache["cost"]["illegal"], true) where {kb_unknown} and '
        f'(attributes["nkb.model.cost_reason"] == nil or '
        f'attributes["nkb.model.cost_reason"] == "none" or {kb_unknown_amount})')
    # The purge is first. Guards run before comparisons or downstream copying.
    for key in KB_SPAN_KEYS:
        statements.append(f'delete_key(attributes, "{key}") where attributes["{key}"] != nil and not ({kb_value_guard(key)})')
    statements += ['set(cache["entry"], false)', 'set(name, "span")', 'set(attributes["langfuse.observation.type"], "span")']
    for condition, operation, kind in ((retrieval, 'retrieval.search', 'retriever'), (index_run, 'index.run', 'chain')):
        statements += [f'set(name, "{operation}") where {condition}', f'set(attributes["langfuse.observation.type"], "{kind}") where {condition}']
    statements.append(f'set(cache["entry"], true) where {index_run}')
    statements += [f'set(name, "embeddings") where {embed}', f'set(attributes["langfuse.observation.type"], "embedding") where {embed}']
    # Any two discriminators at once is a producer bug: fall back to a plain
    # span and never let the conflicting entry flag or body survive.
    retrieval_op = 'attributes["nkb.retrieval.operation"] == "search"'
    index_op = 'attributes["nkb.index.operation"] == "run"'
    embeddings_op = 'attributes["gen_ai.operation.name"] == "embeddings"'
    conflict = f'{kb} and (({retrieval_op} and {index_op}) or ({retrieval_op} and {embeddings_op}) or ({index_op} and {embeddings_op}))'
    statements += [f'set(name, "span") where {conflict}', f'set(attributes["langfuse.observation.type"], "span") where {conflict}', f'set(cache["entry"], false) where {conflict}']
    # Trace bodies only ever come from the global root span.
    statements.append('set(attributes["langfuse.trace.name"], name) where parent_span_id == SpanID(0x0000000000000000)')
    for source, target in [('service.version', 'langfuse.release'), ('deployment.environment.name', 'langfuse.environment')]:
        value = f'resource.attributes["{source}"]'
        pattern = r'^[a-zA-Z0-9][a-zA-Z0-9._+/-]{0,199}$' if source == 'service.version' else r'^[a-z0-9][a-z0-9_-]{0,39}$'
        condition = f'{kb} and IsString({value}) and IsMatch({value}, {json.dumps(pattern)})'
        if source == 'deployment.environment.name':
            condition += f' and not IsMatch({value}, "^langfuse")'
        statements.append(f'set(attributes["{target}"], {value}) where {condition}')
    # KB 成本成组守卫：embeddings 单 input 分项加 total；estimated 组必须整体
    # 合法才投影 cost_details，unknown 组必须无金额（整组拒绝时统一丢弃）。
    # 容差同 N-Agent 管线：double 四次比较析取，OTTL 无 Decimal 与 Abs。
    projected_embedding = f'{kb} and attributes["langfuse.observation.type"] == "embedding"'
    statements += ['set(cache["cost"]["ok"], false)', 'set(cache["cost"]["sum"], 0.0)', 'set(cache["cost_details"], {})']
    amount = 'attributes["nkb.model.cost_usd"]'
    decimal_key = 'nkb.model.cost_decimal'
    source_input = 'attributes["nkb.model.cost_input_usd"]'
    identity = ' and '.join(f'IsString(attributes["nkb.model.{key}"]) and attributes["nkb.model.{key}"] != ""' for key in ('pricing_version', 'price_id', 'billing_provider', 'billing_model'))
    statements.append(f'set(cache["cost"]["ok"], true) where {projected_embedding} and attributes["nkb.model.cost_status"] == "estimated" and attributes["nkb.model.cost_currency"] == "USD" and attributes["nkb.model.cost_reason"] == nil and {amount} != nil and attributes["{decimal_key}"] != nil and {source_input} != nil and {identity} and cache["cost"]["illegal"] == false')
    statements.append(f'set(cache["cost"]["sum"], cache["cost"]["sum"] + {source_input}) where cache["cost"]["ok"] == true and {source_input} != nil')
    sum_value = 'cache["cost"]["sum"]'
    # 容差模板与 N-Agent 管线共享（见 COST_TOLERANCE_STATEMENT），两处保持同步。
    statements.append(COST_TOLERANCE_STATEMENT.format(sum=sum_value, total=amount))
    statements.append(f'set(cache["cost_details"], {{"total": {amount}}}) where {projected_embedding} and cache["cost"]["ok"] == true')
    statements.append(f'set(cache["cost_details"]["input"], {source_input}) where {projected_embedding} and cache["cost"]["ok"] == true and {source_input} != nil')
    statements.append(f'set(attributes["langfuse.observation.cost_details"], String(cache["cost_details"])) where {projected_embedding} and cache["cost"]["ok"] == true')
    for key in ('nkb.model.cost_usd', 'nkb.model.cost_input_usd', decimal_key):
        statements.append(f'delete_key(attributes, "{key}") where cache["cost"]["ok"] == false and attributes["{key}"] != nil')
    statements.append(f'delete_key(attributes, "nkb.model.cost_reason") where attributes["nkb.model.cost_status"] == "estimated" and cache["cost"]["ok"] == false and attributes["nkb.model.cost_reason"] != nil')
    # 非法整组拒绝（cache["cost"]["illegal"]）经 ok==false 门禁阻断 cost_details
    # 与 model.name 自动推价，不做部分成功；诊断性字符串键保留进 metadata 供
    # 拒绝审计（存活值均已过 purge 守卫，非法字段自身已被清理删除）。
    statements += ['set(cache["metadata"], {})']
    for key in KB_METADATA_KEYS:
        statements.append(f'set(cache["metadata"]["{key}"], attributes["{key}"]) where {kb} and attributes["{key}"] != nil')

    def envelope(field, target, kind):
        # Each replacement assigns a fresh envelope keyed on the projected type,
        # so conflict fallbacks and missing suffixes cannot leak between bodies.
        source = f'nkb.content.{field}'
        condition = f'{kb} and attributes["langfuse.observation.type"] == "{kind}" and IsString(attributes["{source}"])'
        result = [f'set({target}, {{"source": "{field}"}}) where {condition}']
        for suffix in SUFFIXES:
            key = f'{source}_{suffix}'
            result.append(f'set({target}["{suffix}"], attributes["{key}"]) where {condition} and attributes["{key}"] != nil')
        return condition, result

    # Missing directions and missing suffixes stay absent; output is only ever
    # copied from a real attribute, never synthesized for a failed run.
    for field, direction, kind in [('index_input', 'input', 'chain'), ('index_result', 'output', 'chain'), ('retrieval_input', 'input', 'retriever'), ('retrieval_output', 'output', 'retriever'), ('embedding_input', 'input', 'embedding')]:
        target = f'cache["metadata"]["{direction}"]'
        condition, body = envelope(field, target, kind)
        statements += [f'set(attributes["langfuse.observation.{direction}"], attributes["nkb.content.{field}"]) where {condition}', *body]
    for direction in ('input', 'output'):
        statements.append(f'set(attributes["langfuse.trace.{direction}"], attributes["langfuse.observation.{direction}"]) where {kb} and cache["entry"] == true and parent_span_id == SpanID(0x0000000000000000) and attributes["langfuse.observation.{direction}"] != nil')
    statements += ['set(cache["usage"], {})']
    statements.append(f'set(cache["usage"]["input"], attributes["gen_ai.usage.input_tokens"]) where {projected_embedding} and attributes["nkb.model.usage_known"] == true and IsInt(attributes["gen_ai.usage.input_tokens"]) and attributes["gen_ai.usage.input_tokens"] >= 0')
    statements.append(f'set(attributes["langfuse.observation.usage_details"], String(cache["usage"])) where {projected_embedding} and Len(cache["usage"]) > 0')
    model = 'attributes["gen_ai.request.model"]'
    model_pattern = r'^[\w.:/@_-]{1,64}$'
    # 仅合法 estimated 组投影 model.name；unknown 阻断自动推价，原始模型键
    # 只保留在 KB metadata。
    statements.append(f'set(attributes["langfuse.observation.model.name"], {model}) where {projected_embedding} and attributes["nkb.model.cost_status"] == "estimated" and cache["cost"]["ok"] == true and IsString({model}) and IsMatch({model}, {json.dumps(model_pattern)})')
    statements.append('set(attributes["langfuse.observation.level"], "ERROR") where status.code == STATUS_CODE_ERROR')
    # Fallback only for execution failure/timeouts; outcome alone never sets
    # level. Only index/retrieval outcomes survive the span allowlist.
    for obj in ('index', 'retrieval'):
        for outcome in ('failure', 'timed_out'):
            statements.append(f'set(attributes["langfuse.observation.status_message"], "{outcome}") where status.code == STATUS_CODE_ERROR and attributes["nkb.{obj}.outcome"] == "{outcome}"')
    for error in KB_ERROR_TYPES:
        statements.append(f'set(attributes["langfuse.observation.status_message"], "{error}") where status.code == STATUS_CODE_ERROR and attributes["error.type"] == "{error}"')
    envelope_keys = ('input', 'output')
    for key in (*KB_METADATA_KEYS, *envelope_keys):
        source = f'cache["metadata"]["{key}"]'
        value = f'String({source})' if key in envelope_keys else source
        statements.append(f'set(attributes["langfuse.observation.metadata.{key}"], {value}) where {source} != nil')
    final = ('langfuse.observation.type', 'langfuse.trace.name', 'langfuse.release', 'langfuse.environment', 'langfuse.trace.input', 'langfuse.trace.output', 'langfuse.observation.input', 'langfuse.observation.output', *(f'langfuse.observation.metadata.{key}' for key in (*KB_METADATA_KEYS, *envelope_keys)), 'langfuse.observation.usage_details', 'langfuse.observation.model.name', 'langfuse.observation.cost_details', 'langfuse.observation.level', 'langfuse.observation.status_message')
    statements += [f'keep_keys(attributes, {json.dumps(final)})']
    return statements


def build_config(public_key, secret_key, endpoint=DEFAULT_ENDPOINT, kb_endpoint=DEFAULT_ENDPOINT):
    # The KB pipeline targets the shared n-agent Langfuse project: one Basic
    # credential pair for both exporters, so spans from n-agent and n-kb that
    # share a W3C trace id merge into a single tree. kb_endpoint stays
    # independently configurable (defaults to the shared endpoint).
    validate_endpoint(endpoint)
    validate_endpoint(kb_endpoint)
    auth = base64.b64encode(f'{public_key}:{secret_key}'.encode()).decode()
    event_keys = ('error.type', 'nagent.tool.executed', 'nagent.tool.policy_outcome', *('nagent.' + obj + '.outcome' for obj in OBJECTS))
    safe_events = [f'nagent.{obj}.completed' for obj in OBJECTS]
    # Event facts re-run the KB guards, so a secret wearing a legal key is
    # dropped before it can reach the KB project; unknown events are renamed.
    kb_spanevent = [f'keep_keys(attributes, {json.dumps(KB_EVENT_KEYS)})']
    kb_spanevent += [f'delete_key(attributes, "{key}") where attributes["{key}"] != nil and not ({kb_value_guard(key)})' for key in KB_EVENT_KEYS]
    kb_spanevent.append('set(name, "event") where not IsMatch(name, "^(' + '|'.join(x.replace('.', '[.]') for x in KB_EVENTS) + ')$")')

    def exporter(auth_value, target):
        return {'traces_endpoint': target, 'headers': {'Authorization': 'Basic ' + auth_value, 'x-langfuse-ingestion-version': '4'}, 'timeout': '10s',
            'sending_queue': dict(enabled=True, sizer='bytes', queue_size=16777216, block_on_overflow=False, wait_for_result=False, num_consumers=2),
            'retry_on_failure': dict(initial_interval='1s', max_interval='5s', max_elapsed_time='60s')}

    return {'processors': {
        'filter/langfuse': {'error_mode': 'propagate', 'traces': {'span': ['resource.attributes["service.name"] != "n-agent"']}},
        'transform/langfuse': {'error_mode': 'propagate', 'trace_statements': [
            {'context': 'scope', 'statements': ['set(name, "")', 'set(version, "")', 'keep_keys(attributes, [])']},
            {'context': 'span', 'statements': span_statements()},
            {'context': 'resource', 'statements': ['keep_keys(attributes, ["service.name"])']},
            {'context': 'spanevent', 'statements': [f'keep_keys(attributes, {json.dumps(event_keys)})', 'set(name, "event") where not IsMatch(name, "^(' + '|'.join(x.replace('.', '[.]') for x in safe_events) + ')$")']},
        ]},
        'linksanitize/langfuse': {},
        'batch/langfuse': {'timeout': '1s', 'send_batch_size': 1, 'send_batch_max_size': 1},
        'filter/langfuse-kb': {'error_mode': 'propagate', 'traces': {'span': ['resource.attributes["service.name"] != "n-kb"']}},
        'transform/langfuse-kb': {'error_mode': 'propagate', 'trace_statements': [
            {'context': 'scope', 'statements': ['set(name, "")', 'set(version, "")', 'keep_keys(attributes, [])']},
            {'context': 'span', 'statements': span_statements_kb()},
            {'context': 'resource', 'statements': ['keep_keys(attributes, ["service.name"])']},
            {'context': 'spanevent', 'statements': kb_spanevent},
        ]},
        'linksanitize/langfuse-kb': {},
        'batch/langfuse-kb': {'timeout': '1s', 'send_batch_size': 1, 'send_batch_max_size': 1},
    }, 'exporters': {'otlphttp/langfuse': exporter(auth, endpoint), 'otlphttp/langfuse-kb': exporter(auth, kb_endpoint)}, 'service': {'pipelines': {
        'traces/langfuse': {'receivers': ['otlp'], 'processors': ['memory_limiter', 'filter/langfuse', 'transform/langfuse', 'linksanitize/langfuse', 'batch/langfuse'], 'exporters': ['otlphttp/langfuse']},
        'traces/langfuse-kb': {'receivers': ['otlp'], 'processors': ['memory_limiter', 'filter/langfuse-kb', 'transform/langfuse-kb', 'linksanitize/langfuse-kb', 'batch/langfuse-kb'], 'exporters': ['otlphttp/langfuse-kb']},
    }}}


def publish_json(directory, value):
    """Atomic content-addressed publication; never replace a mounted inode."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    data = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    path = directory / (hashlib.sha256(data).hexdigest() + '.json')
    if path.exists():
        if path.read_bytes() != data or path.stat().st_mode & 0o777 != 0o600:
            raise ValueError('Invalid existing generated configuration')
        return path
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
        temporary = Path(output.name)
        os.chmod(temporary, 0o600)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    try:
        os.link(temporary, path)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError('Generated configuration hash collision')
    finally:
        temporary.unlink()
    return path


def load_credentials(root, path):
    spec = importlib.util.spec_from_file_location('langfuse_stack_credentials', Path(root) / 'docker/langfuse/manage-stack.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    values = module.read_env(path)
    module.validate_env(values)
    if Path(path).stat().st_mode & 0o777 != 0o600:
        raise ValueError('Langfuse credentials must have permissions 0600')
    return module, values


def prepare(directory, root):
    enabled, env_file, endpoint, kb_endpoint = settings(directory, root)
    if not enabled:
        return None
    module, values = load_credentials(root, env_file)
    folder = Path(root) / 'locals/harness_tmp/langfuse-collector'
    config = publish_json(folder, build_config(
        values['LANGFUSE_INIT_PROJECT_PUBLIC_KEY'], values['LANGFUSE_INIT_PROJECT_SECRET_KEY'], endpoint,
        kb_endpoint=kb_endpoint))
    command = ['--config=/etc/otelcol-contrib/config.yaml', '--config=/etc/otelcol-contrib/langfuse.json']
    override = publish_json(folder, {'services': {'otel-collector': {
        # The private 0600 bind is owned by the invoking host account.
        'user': f'{os.getuid()}:{os.getgid()}',
        'image': IMAGE, 'build': {'context': str(Path(directory) / 'langfuse-collector')},
        'command': command,
        'volumes': [{'type': 'bind', 'source': str(config), 'target': '/etc/otelcol-contrib/langfuse.json', 'read_only': True}],
        'healthcheck': {'test': ['CMD', '/otelcol-contrib', 'validate', *command]},
    }}})
    return {'override': override, 'config': config, 'module': module, 'credentials': values}
