"""宿主 stdlib 报告工具：记录用例结果，生成 Grafana Tempo 直达链接。"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def trace_url(base_url, trace_id):
    if not re.fullmatch(r"[0-9a-f]{32}", trace_id or "") or trace_id == "0" * 32:
        return None
    base = urlsplit(base_url)
    if base.scheme not in ("http", "https") or not base.netloc or base.username or base.password:
        return None
    datasource = {"type": "tempo", "uid": "nagent-tempo"}
    panes = {"trace": {
        "datasource": datasource["uid"],
        "queries": [{"refId": "A", "queryType": "traceql", "query": trace_id,
                     "datasource": datasource}],
        "range": {"from": "now-1h", "to": "now"},
    }}
    params = [(k, v) for k, v in parse_qsl(base.query)
              if k not in ("panes", "left", "right", "schemaVersion")]
    params.extend([("schemaVersion", "1"), ("panes", json.dumps(panes, separators=(",", ":")))])
    return urlunsplit((base.scheme, base.netloc, base.path, urlencode(params), ""))


def new_report(run_id):
    return {"run_id": run_id, "suite": "otel-smoke", "cases": [
        {"id": key, "name": name, "result": "SKIP", "trace_id": None,
         "trace_url": None, "reason": "未执行"}
        for key, name in (("good", "成功调用与三信号关联"),
                          ("fail", "Provider 500 错误记录"),
                          ("cancel", "客户端断连后 Span 收尾"))
    ]}


def finish(report, base_url, exit_code):
    report["exit_code"] = exit_code
    for case in report["cases"]:
        if case["result"] == "RUNNING":
            case.update(result="FAIL", reason="用例执行中断或断言失败，详见终端诊断")
        case["trace_url"] = trace_url(base_url, case["trace_id"])


def write_report(directory, report):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = [f'E2E：{report["suite"]}；运行：{report["run_id"]}；退出码：{report.get("exit_code", "执行中")}',
             "", "| 用例 | 结果 | Trace ID | 现场 |", "|---|---|---|---|"]
    for case in report["cases"]:
        link = f'[查看Trace]({case["trace_url"]})' if case["trace_url"] else (
            case["reason"] or ("Trace 页面地址未配置" if case["trace_id"] else "未取得 Trace"))
        lines.append(f'| {case["name"]} | {case["result"]} | {case["trace_id"] or "—"} | {link} |')
    lines.extend(["", "说明：Provider 500 用例的 PASS 表示预期错误已正确记录。", ""])
    (directory / "report.md").write_text("\n".join(lines))


def main():
    directory, action, *args = sys.argv[1:]
    path = Path(directory) / "report.json"
    if action == "init":
        report = new_report(args[0])
    else:
        report = json.loads(path.read_text())
        if action == "disabled":
            for case in report["cases"]:
                case["reason"] = "OTel 未启用"
        elif action == "finish":
            finish(report, args[0], int(args[1]))
        else:
            case = next(c for c in report["cases"] if c["id"] == args[0])
            if action == "begin":
                case.update(result="RUNNING", reason="")
            elif action == "trace":
                case["trace_id"] = json.loads(Path(args[1]).read_text())["traceId"]
            elif action == "pass":
                case.update(result="PASS", reason="")
            else:
                raise ValueError(f"unknown action: {action}")
    write_report(directory, report)
    if action == "finish":
        print((Path(directory) / "report.md").read_text())
        print(f"REPORT_DIR {directory}")


if __name__ == "__main__":
    main()
