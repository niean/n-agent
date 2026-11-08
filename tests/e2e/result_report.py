"""宿主 stdlib 报告工具：记录用例结果，生成 Grafana Tempo 直达链接。"""
from __future__ import annotations

import importlib.util
import json
import os
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
                          ("cancel", "客户端断连后 Span 收尾"),
                          ("langfuse-comparison", "OTel 与 Langfuse 数值一致性"))
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
        link = f'[查看对比结果]({case["comparison_path"]})' if case.get("comparison_path") else (
            f'[查看Trace]({case["trace_url"]})' if case["trace_url"] else (
            case["reason"] or ("Trace 页面地址未配置" if case["trace_id"] else "未取得 Trace"))
        )
        trace = (f'{len(case["trace_ids"])} 条，见对比结果' if case.get("comparison_path") else
                 case["trace_id"] or "—")
        lines.append("| " + " | ".join(cell(value) for value in (
            case["category"], case["name"], case["result"], trace, link)) + " |")
    return "\n".join(lines) + "\n"


def group_cases(cases):
    """Group explicit case identities; message/Trace rows are evidence, not cases."""
    groups = {}
    for case in cases:
        key = (case["category"], case.get("case_id", case["id"]))
        groups.setdefault(key, []).append(case)
    return groups


def split_attempts(cases):
    groups = group_cases(cases)
    latest = {key: max(row.get("attempt", 1) for row in rows) for key, rows in groups.items()}
    final, retries = [], []
    for row in cases:
        key = (row["category"], row.get("case_id", row["id"]))
        target = final if row.get("attempt", 1) == latest[key] else retries
        target.append(row)
    return final, retries


def render_sections(cases):
    final, retries = split_attempts(cases)
    return ("## 最终结果\n\n" + render_table(final) +
            "\n## 重试记录\n\n" + render_table(retries) +
            ("\n无重试记录。\n" if not retries else ""))


def render_summary(cases):
    final, _ = split_attempts(cases)
    counts = {result: 0 for result in ("PASS", "FAIL", "SKIP")}
    for rows in group_cases(final).values():
        results = {row.get("attempt_result", row["result"]) for row in rows}
        results.update(row["result"] for row in rows)
        result = ("FAIL" if results - {"PASS", "SKIP"} else
                  "SKIP" if "SKIP" in results else "PASS")
        counts[result] += 1
    total = sum(counts.values())
    retried = sum(max(row.get("attempt", 1) for row in rows) > 1
                  for rows in group_cases(cases).values())
    if not total:
        outcome = "无测试结果"
    elif counts["PASS"] == total:
        outcome = f"全部通过（{total}/{total}）"
    else:
        outcome = f'通过{counts["PASS"]}/{total}（失败{counts["FAIL"]}，跳过{counts["SKIP"]}）'
    return f"最终结果：{outcome}；其中{retried}/{total}个用例进行了重试。"


def aggregate_report(run_id, directories, exit_code):
    spec = importlib.util.spec_from_file_location("e2e_trace_report", Path(__file__).with_name("trace_report.py"))
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    cases = []
    sources = []
    for directory in directories:
        path = Path(directory) / "report.json"
        source = json.loads(path.read_text())
        sources.append(str(path))
        rows = source["cases"] if source.get("suite") else support.report_cases(source)
        for case in rows:
            if case.get("comparison_path"):
                case["comparison_path"] = str((Path(directory) / case["comparison_path"]).resolve())
        cases.extend(rows)
    return {"run_id": run_id, "suite": "all", "exit_code": exit_code,
            "sources": sources, "cases": cases}


def write_report(directory, report):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for case in report["cases"]:
        if case.get("comparison_path") and Path(case["comparison_path"]).is_absolute():
            case["comparison_path"] = os.path.relpath(case["comparison_path"], directory)
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    markdown = (f'E2E：{report["suite"]}；运行：{report["run_id"]}；退出码：{report.get("exit_code", "执行中")}\n\n' +
                render_sections(report["cases"]))
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
        elif action == "comparison":
            data = json.loads((Path(directory) / args[0]).read_text())
            if data["result"] not in ("PASS", "FAIL", "SKIP"):
                raise ValueError("comparison has no final result")
            case = next(c for c in report["cases"] if c["id"] == "langfuse-comparison")
            case.update(result=data["result"], reason=data.get("reason", ""),
                        comparison_path=str(Path(args[0]).with_suffix(".md")),
                        trace_ids=[c["trace_id"] for c in data["calls"] if c.get("trace_url")],
                        evidence=data)
        elif action in ("comparison-error", "comparison-skip"):
            module_spec = importlib.util.spec_from_file_location("otel_langfuse_comparison", Path(__file__).with_name("otel_langfuse_comparison.py"))
            module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(module)
            artifact = Path(directory) / "comparison" / "comparison.json"
            data = json.loads(artifact.read_text()) if artifact.exists() else {"checks": [], "calls": []}
            result = "FAIL" if action == "comparison-error" else "SKIP"
            data.update(result=result, reason=args[0])
            module.write_comparison(artifact.parent, data)
            case = next(c for c in report["cases"] if c["id"] == "langfuse-comparison")
            case.update(result=result, reason=args[0], comparison_path="comparison/comparison.md",
                        trace_ids=[c["trace_id"] for c in data["calls"] if c.get("trace_url")], evidence=data)
        elif action == "skip":
            case = next(c for c in report["cases"] if c["id"] == args[0])
            case.update(result="SKIP", reason=args[1])
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
