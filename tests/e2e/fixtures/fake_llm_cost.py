#!/usr/bin/env python
"""E2E 成本探测确定性 stub Provider（T8 专用，仅隔离 E2E 实例使用）。

向量 B 用量（prompt=150 / cached=50 / completion=20 -> Ti=100/To=20/Tic=50，
测试单价 2/10/0.2 USD/M -> 总额 0.00041）；工具结果已存在时的最终回答使用
全零用量（显式零 => estimated/0，验证零价保留）。

决策规则（按请求内容确定性分派）：
- 消息历史存在 tool 角色：最终回答，全零用量；calculator 工具结果 => 回答
  "323"（business() 断言），其余工具（search_knowledge）=> 回答 "kb answer"。
- 用户消息包含 "知识库" 且带工具：调用 search_knowledge 工具，kb_id 取自工具
  描述 "Enabled knowledge bases: <id> (...)" 首个启用库；query 为固定 200
  token 检索串（bge-m3 实测 prompt_eval_count=200，见 cost-probe-query.txt）。
  用量向量 B。
- 用户消息包含 "calculator" 或 "17*19" 且带工具：调用 calculator
  工具（expression="17*19"），用量向量 B；工具结果回填后走最终回答分支
  （全零用量），与 business() 对 execute_tool Span 的断言兼容。
- 其余：回答 "323"，用量向量 B（缺省安全路径）。

启动：python fake_llm_cost.py <port> [query-file]
"""
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
QUERY_PATH = sys.argv[2] if len(sys.argv) > 2 else "/tests/fixtures/cost-probe-query.txt"

KB_TOOL = "search_knowledge"
CALC_TOOL = "calculator"
FINAL_TEXT = "kb answer"
CALC_ANSWER = "323"
# 向量 B：prompt=150、cached=50、completion=20（OpenAI 无 cache_write 概念）。
USAGE_VECTOR_B = {
    "prompt_tokens": 150,
    "completion_tokens": 20,
    "total_tokens": 170,
    "prompt_tokens_details": {"cached_tokens": 50},
}
# 显式全零：零价/零 Token 亦为已知，estimated 可以得到 total=0。
USAGE_ZERO = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "prompt_tokens_details": {"cached_tokens": 0},
}


def probe_query():
    with open(QUERY_PATH, "rb") as handle:
        return handle.read().decode("utf-8").strip()


def kb_id_from_tools(tools):
    for tool in tools or []:
        function = (tool or {}).get("function") or {}
        if function.get("name") != KB_TOOL:
            continue
        match = re.search(r"Enabled knowledge bases: (\S+?) \(", function.get("description") or "")
        if match:
            return match.group(1)
    return None


def wants_kb(messages):
    return any(
        isinstance(m.get("content"), str) and "知识库" in m.get("content")
        for m in messages
    )


def tool_names(tools):
    return {((tool or {}).get("function") or {}).get("name") for tool in tools or []}


def wants_calc(messages, tools):
    if CALC_TOOL not in tool_names(tools):
        return False
    return any(
        isinstance(m.get("content"), str)
        and ("calculator" in m.get("content") or "17*19" in m.get("content"))
        for m in messages
    )


def has_tool_result(messages):
    return any(m.get("role") == "tool" for m in messages)


def final_answer_text(messages):
    """工具结果回填后的最终回答：calculator => 323，其余（kb 检索）=> kb answer。"""
    for message in messages:
        if message.get("role") != "tool":
            continue
        if message.get("name") == CALC_TOOL:
            return CALC_ANSWER
        if CALC_ANSWER in str(message.get("content") or ""):
            return CALC_ANSWER
    return FINAL_TEXT


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            body = b'{"status": "ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def _base(self, model, message, finish_reason, usage):
        return {
            "id": "chatcmpl-cost-probe",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": dict(usage),
        }

    def _tool_completion(self, model, name, arguments):
        return self._base(
            model,
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call-cost-probe", "type": "function",
                             "function": {"name": name, "arguments": arguments}}]},
            "tool_calls",
            USAGE_VECTOR_B,
        )

    def _kb_tool_completion(self, model):
        kb_id = kb_id_from_tools(self.tools)
        arguments = json.dumps({"kb_id": kb_id or "legacy-n-kb",
                                "query": probe_query()}, ensure_ascii=False)
        return self._tool_completion(model, KB_TOOL, arguments)

    def _completion(self, model):
        messages = self.messages
        if has_tool_result(messages):
            return self._base(model, {"role": "assistant",
                                      "content": final_answer_text(messages)},
                              "stop", USAGE_ZERO)
        if wants_kb(messages) and self.tools:
            return self._kb_tool_completion(model)
        if wants_calc(messages, self.tools):
            return self._tool_completion(model, CALC_TOOL,
                                         json.dumps({"expression": "17*19"}))
        return self._base(model, {"role": "assistant", "content": CALC_ANSWER},
                          "stop", USAGE_VECTOR_B)

    def _sse(self, chunks):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in chunks:
            data = ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode()
            self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")
            self.wfile.flush()
        done = b"data: [DONE]\n\n"
        self.wfile.write(("%x\r\n" % len(done)).encode() + done + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_error(400)
            return
        self.messages = payload.get("messages", [])
        self.tools = payload.get("tools") or []
        model = payload.get("model", "unknown")
        completion = self._completion(model)
        if not payload.get("stream"):
            body = json.dumps(completion, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        choice = completion["choices"][0]
        usage_chunk = {"id": "chatcmpl-cost-probe", "object": "chat.completion.chunk",
                       "model": model, "choices": [], "usage": dict(completion["usage"])}

        def chunk(delta, finish=None):
            return {"id": "chatcmpl-cost-probe", "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        message = choice["message"]
        if message.get("tool_calls"):
            call = message["tool_calls"][0]
            chunks = [
                chunk({"role": "assistant", "content": None}),
                chunk({"tool_calls": [{"index": 0, "id": call["id"], "type": "function",
                                       "function": call["function"]}]}),
                chunk({}, finish="tool_calls"),
                usage_chunk,
            ]
        else:
            chunks = [
                chunk({"role": "assistant", "content": None}),
                chunk({"content": message["content"]}),
                chunk({}, finish="stop"),
                usage_chunk,
            ]
        self._sse(chunks)


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
