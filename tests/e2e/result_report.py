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
        {"category": "OTel", "id": key, "name": name, "result": "SKIP", "trace_id": None,
         "trace_url": None, "reason": "未执行"}
        for key, name in (("good", "成功调用与三信号关联"),
                          ("fail", "Provider 500 错误记录"),
                          ("cancel", "客户端断连后 Span 收尾"))
    ]}


def new_checks_report(run_id, keys):
    names = {"artifacts": "Artifact 管理检查"}
    return {"run_id": run_id, "suite": "management-checks", "cases": [
        {"category": "制品", "id": key, "name": names[key], "result": "SKIP", "trace_id": None,
         "trace_url": None, "reason": "未执行；管理接口与文件/配置检查不产生对话 Trace"}
        for key in keys]}


def finish(report, base_url, exit_code):
    report["exit_code"] = exit_code
    for case in report["cases"]:
        if case["result"] == "RUNNING":
            case.update(result="FAIL", reason="用例执行中断或断言失败，详见终端诊断")
        case["trace_url"] = trace_url(base_url, case["trace_id"])


def render_table(cases):
    def cell(value):
        return str(value or "").replace("|", "\\|").replace("\n", " ")
    lines = ["| 分类 | 用例 | 结果 | TraceID | 链接 |", "|---|---|---|---|---|"]
    for case in cases:
        link = f'[查看Trace]({case["trace_url"]})' if case["trace_url"] else (
            case["reason"] or ("Trace 页面地址未配置" if case["trace_id"] else "未取得 Trace"))
        lines.append("| " + " | ".join(cell(value) for value in (
            case["category"], case["name"], case["result"], case["trace_id"] or "—", link)) + " |")
    return "\n".join(lines) + "\n"


def render_summary(cases):
    counts = {result: sum(case["result"] == result for case in cases)
              for result in ("PASS", "FAIL", "SKIP")}
    return "Summary（结果行）: " + " | ".join(f"{key} {value}" for key, value in counts.items())


def aggregate_report(run_id, directories, exit_code):
    import importlib.util
    spec = importlib.util.spec_from_file_location("e2e_trace_report", Path(__file__).with_name("trace_report.py"))
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    cases = []
    sources = []
    for directory in directories:
        path = Path(directory) / "report.json"
        source = json.loads(path.read_text())
        sources.append(str(path))
        cases.extend(source["cases"] if source.get("suite") else support.report_cases(source))
    return {"run_id": run_id, "suite": "all", "exit_code": exit_code,
            "sources": sources, "cases": cases}


def write_report(directory, report):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    markdown = (f'E2E：{report["suite"]}；运行：{report["run_id"]}；退出码：{report.get("exit_code", "执行中")}\n\n' +
                render_table(report["cases"]))
    if report["suite"] == "otel-smoke":
        markdown += "\n说明：Provider 500 用例的 PASS 表示预期错误已正确记录。\n"
    (directory / "result.md").write_text(markdown)
    (directory / "report.md").write_text(markdown)


def main():
    directory, action, *args = sys.argv[1:]
    path = Path(directory) / "report.json"
    if action == "init":
        report = new_report(args[0])
    elif action in ("missing", "suite-failure"):
        report = {"run_id": args[0], "suite": "missing-report", "exit_code": int(args[3]),
                  "cases": [{"category": args[1], "id": args[2], "name": args[2],
                             "result": "FAIL", "trace_id": None, "trace_url": None,
                             "reason": ("子测试未生成结果报告，详见运行日志" if action == "missing" else
                                        "套件执行或清理失败，详见运行日志；退出码 " + args[3])}]}
    elif action == "aggregate":
        report = aggregate_report(args[0], args[2:], int(args[1]))
    elif action == "init-checks":
        report = new_checks_report(args[0], args[1:])
    else:
        report = json.loads(path.read_text())
        if action == "check":
            case = next(c for c in report["cases"] if c["id"] == args[0])
            case.update(result=args[1], reason="管理接口与文件/配置检查不产生对话 Trace")
        elif action == "disabled":
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
    if action in ("finish", "aggregate"):
        print(render_summary(report["cases"]))
        print(f"RESULT_FILE {Path(directory) / 'result.md'}")
        print(f"REPORT_DIR {directory}")


if __name__ == "__main__":
    main()
