"""宿主单测：conversations_runner 纯函数。不触网、不触 Docker。"""
from __future__ import annotations

import importlib.util
import sys
import json
import traceback
from pathlib import Path

import pytest

_RUNNER_PATH = Path(__file__).with_name("conversations_runner.py")
_spec = importlib.util.spec_from_file_location("conversations_runner", _RUNNER_PATH)
cr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cr
_spec.loader.exec_module(cr)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _case(**over):
    base = {
        "schema_version": 1,
        "id": "c1",
        "title": "t",
        "source_session_id": "dashboard-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "channel": "dashboard",
        "requires": [],
        "options": {},
        "messages": [{"id": "m1", "content": "hi", "expect": {"rubric": "r", "tools": []}}],
        "side_effects": [],
        "cleanup": ["session"],
    }
    base.update(over)
    return base


def _write_case(tmp_path, name="a.json", **over):
    (tmp_path / name).write_text(json.dumps(_case(**over)), encoding="utf-8")


class TestDatasetValidation:
    def test_valid_case_loads(self):
        case = cr.validate_case(_case())
        assert case.id == "c1"
        assert case.messages[0].replay_via == "channel"
        assert case.messages[0].expect.forbidden_tools == []

    def test_unknown_channel_rejected(self):
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(channel="feishu"))

    def test_task_api_requires_action_params(self):
        msg = {"id": "m1", "content": "", "replay_via": "task_api",
               "expect": {"rubric": "r", "tools": []}}
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(messages=[msg]))

    def test_action_params_forbidden_on_channel_message(self):
        msg = {"id": "m1", "content": "hi", "action_params": {"title": "x"},
               "expect": {"rubric": "r", "tools": []}}
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(messages=[msg]))

    def test_runner_action_only_browser_type_password(self):
        msg = {"id": "m1", "content": "x", "runner_action_after": "rm_rf",
               "expect": {"rubric": "r", "tools": []}}
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(messages=[msg]))

    def test_duplicate_case_id_rejected(self, tmp_path):
        (tmp_path / "a.json").write_text(json.dumps(_case()), encoding="utf-8")
        (tmp_path / "b.json").write_text(json.dumps(_case(title="t2")), encoding="utf-8")
        with pytest.raises(cr.DatasetError):
            cr.load_dataset(tmp_path, only=None)

    def test_only_unknown_id_rejected(self, tmp_path):
        (tmp_path / "a.json").write_text(json.dumps(_case()), encoding="utf-8")
        with pytest.raises(cr.DatasetError):
            cr.load_dataset(tmp_path, only=["nope"])

    def test_only_duplicate_id_rejected(self, tmp_path):
        (tmp_path / "a.json").write_text(json.dumps(_case()), encoding="utf-8")
        with pytest.raises(cr.DatasetError):
            cr.load_dataset(tmp_path, only=["c1", "c1"])

    def test_empty_dataset_dir_rejected(self, tmp_path):
        with pytest.raises(cr.DatasetError):
            cr.load_dataset(tmp_path, only=None)

    def test_options_whitelist(self):
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(options={"evil_key": 1}))

    def test_side_effect_task_terminal_requires_status_timeout(self):
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(side_effects=[{"type": "task_terminal"}]))

    def test_image_asset_path_escape_rejected(self):
        msg = {"id": "m1", "content": "x", "image_asset": "../outside.png",
               "expect": {"rubric": "r", "tools": []}}
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(messages=[msg]))


class TestStrictTypes:
    def test_case_not_dict_rejected_without_type_error(self):
        with pytest.raises(cr.DatasetError):
            cr.validate_case([1, 2, 3])

    def test_schema_version_must_be_int_1(self):
        for bad in (True, "1", 2, 1.0, None):
            with pytest.raises(cr.DatasetError):
                cr.validate_case(_case(schema_version=bad))

    def test_id_title_source_must_be_nonempty_str(self):
        for key in ("id", "title", "source_session_id"):
            for bad in (123, "", None, ["x"]):
                with pytest.raises(cr.DatasetError):
                    cr.validate_case(_case(**{key: bad}))

    def test_messages_must_be_nonempty_list(self):
        for bad in ([], None, "x", {}):
            with pytest.raises(cr.DatasetError):
                cr.validate_case(_case(messages=bad))

    def test_message_id_unique_nonempty(self):
        m1 = {"id": "m1", "content": "a", "expect": {"rubric": "r", "tools": []}}
        m2 = {"id": "m1", "content": "b", "expect": {"rubric": "r", "tools": []}}
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(messages=[m1, m2]))
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(messages=[{"id": "", "content": "a",
                                             "expect": {"rubric": "r", "tools": []}}]))

    def test_channel_message_content_nonempty_str(self):
        for bad in ("", None, 123):
            msg = {"id": "m1", "content": bad, "expect": {"rubric": "r", "tools": []}}
            with pytest.raises(cr.DatasetError):
                cr.validate_case(_case(messages=[msg]))

    def test_rubric_must_be_nonempty_str(self):
        for bad in ("", None, 42):
            msg = {"id": "m1", "content": "hi", "expect": {"rubric": bad, "tools": []}}
            with pytest.raises(cr.DatasetError):
                cr.validate_case(_case(messages=[msg]))

    def test_expect_required(self):
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(messages=[{"id": "m1", "content": "hi"}]))

    def test_tools_must_be_str_list(self):
        for bad in ("web_fetch", ["web_fetch", 1], [None], [True]):
            msg = {"id": "m1", "content": "hi", "expect": {"rubric": "r", "tools": bad}}
            with pytest.raises(cr.DatasetError):
                cr.validate_case(_case(messages=[msg]))

    def test_forbidden_tools_must_be_str_list(self):
        msg = {"id": "m1", "content": "hi",
               "expect": {"rubric": "r", "tools": [], "forbidden_tools": [1]}}
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(messages=[msg]))

    def test_requires_enum_and_type(self):
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(requires=["kubernetes"]))
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(requires="sandbox"))
        case = cr.validate_case(_case(requires=["sandbox", "web"]))
        assert case.requires == ["sandbox", "web"]

    def test_cleanup_enum_and_type(self):
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(cleanup=["everything"]))
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(cleanup="session"))

    def test_replay_via_enum(self):
        msg = {"id": "m1", "content": "hi", "replay_via": "pigeon",
               "expect": {"rubric": "r", "tools": []}}
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(messages=[msg]))

    def test_action_params_null_forbidden_on_channel(self):
        msg = {"id": "m1", "content": "hi", "action_params": None,
               "expect": {"rubric": "r", "tools": []}}
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(messages=[msg]))

    def test_external_memory_enabled_must_be_str_list(self):
        for bad in ("file_1", ["file_1", 2], [None]):
            with pytest.raises(cr.DatasetError):
                cr.validate_case(_case(options={"external_memory_enabled": bad}))
        case = cr.validate_case(_case(options={"external_memory_enabled": ["file_1"]}))
        assert case.options == {"external_memory_enabled": ["file_1"]}


class TestTaskApiActionParams:
    def _msg(self, **ap_over):
        ap = {"title": "任务：产出制品", "body": "做点什么", "goal_mode": True, "priority": 2}
        ap.update(ap_over)
        return {"id": "m1", "content": "", "replay_via": "task_api",
                "action_params": ap, "expect": {"rubric": "r", "tools": []}}

    def test_valid_task_api_message_loads(self):
        case = cr.validate_case(_case(messages=[self._msg()]))
        msg = case.messages[0]
        assert msg.replay_via == "task_api"
        assert msg.action_params["goal_mode"] is True
        assert msg.action_params["priority"] == 2

    def test_title_body_nonempty_str(self):
        for key in ("title", "body"):
            for bad in ("", None, 5):
                with pytest.raises(cr.DatasetError):
                    cr.validate_case(_case(messages=[self._msg(**{key: bad})]))

    def test_goal_mode_must_be_bool(self):
        for bad in ("true", 1, None):
            with pytest.raises(cr.DatasetError):
                cr.validate_case(_case(messages=[self._msg(goal_mode=bad)]))

    def test_priority_must_be_int_not_bool(self):
        for bad in ("2", 2.5, True, None):
            with pytest.raises(cr.DatasetError):
                cr.validate_case(_case(messages=[self._msg(priority=bad)]))

    def test_unknown_action_params_key_rejected(self):
        msg = self._msg()
        msg["action_params"]["evil"] = 1
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(messages=[msg]))

    def test_action_params_must_be_dict(self):
        msg = {"id": "m1", "content": "", "replay_via": "task_api",
               "action_params": "x", "expect": {"rubric": "r", "tools": []}}
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(messages=[msg]))


class TestRunnerAction:
    def _msg(self, mid="m2", action="browser_type_password"):
        return {"id": mid, "content": "x", "runner_action_after": action,
                "expect": {"rubric": "r", "tools": []}}

    def test_browser_login_pod_m2_accepted(self):
        case = cr.validate_case(_case(id="browser-login-pod", messages=[self._msg()]))
        assert case.messages[0].runner_action_after == "browser_type_password"

    def test_rejected_on_other_case(self):
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(id="other-case", messages=[self._msg()]))

    def test_rejected_on_other_message(self):
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(id="browser-login-pod", messages=[self._msg(mid="m3")]))


class TestSideEffects:
    def test_task_terminal_status_enum(self):
        for bad in ("failed", "running", None, 1):
            with pytest.raises(cr.DatasetError):
                cr.validate_case(_case(side_effects=[
                    {"type": "task_terminal", "status": bad, "timeout": 600}]))

    def test_task_terminal_timeout_positive_number(self):
        for bad in (0, -1, "600", True, None):
            with pytest.raises(cr.DatasetError):
                cr.validate_case(_case(side_effects=[
                    {"type": "task_terminal", "status": "succeeded", "timeout": bad}]))

    def test_workspace_file_requires_path_and_expected_content(self):
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(side_effects=[{"type": "workspace_file", "path": "a.txt"}]))
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(side_effects=[
                {"type": "workspace_file", "expected_content": "x"}]))

    def test_artifact_registered_requires_count_and_task_ref(self):
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(side_effects=[
                {"type": "artifact_registered", "source_context_ref": "task_id"}]))
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(side_effects=[
                {"type": "artifact_registered", "count": 2,
                 "source_context_ref": "other-case-task"}]))

    def test_artifact_registered_count_int(self):
        for bad in ("2", 2.0, True, 0, -1):
            with pytest.raises(cr.DatasetError):
                cr.validate_case(_case(side_effects=[
                    {"type": "artifact_registered", "count": bad,
                     "source_context_ref": "task_id"}]))

    def test_unknown_side_effect_type_rejected(self):
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(side_effects=[{"type": "drop_database"}]))

    def test_valid_side_effects_load(self):
        case = cr.validate_case(_case(side_effects=[
            {"type": "task_terminal", "status": "succeeded", "timeout": 600},
            {"type": "workspace_file", "path": "a.txt", "expected_content": "artifact a"},
            {"type": "artifact_registered", "count": 1,
             "source_context_ref": "task_id", "kind": "markdown"},
        ]))
        assert len(case.side_effects) == 3


class TestLoadDataset:
    def test_only_empty_selection_rejected(self, tmp_path):
        _write_case(tmp_path)
        with pytest.raises(cr.DatasetError):
            cr.load_dataset(tmp_path, only=[])

    def test_only_blank_id_rejected(self, tmp_path):
        _write_case(tmp_path)
        with pytest.raises(cr.DatasetError):
            cr.load_dataset(tmp_path, only=["  "])

    def test_only_selects_subset(self, tmp_path):
        _write_case(tmp_path, "a.json")
        _write_case(tmp_path, "b.json", id="c2")
        cases = cr.load_dataset(tmp_path, only=["c2"])
        assert [c.id for c in cases] == ["c2"]

    def test_invalid_json_rejected(self, tmp_path):
        (tmp_path / "a.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(cr.DatasetError):
            cr.load_dataset(tmp_path, only=None)

    def test_missing_dir_rejected(self, tmp_path):
        with pytest.raises(cr.DatasetError):
            cr.load_dataset(tmp_path / "nope", only=None)

    def test_image_asset_absolute_path_rejected(self):
        msg = {"id": "m1", "content": "x", "image_asset": "/etc/passwd",
               "expect": {"rubric": "r", "tools": []}}
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(messages=[msg]))

    def test_image_asset_must_live_under_assets(self):
        msg = {"id": "m1", "content": "x", "image_asset": "elsewhere/x.png",
               "expect": {"rubric": "r", "tools": []}}
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(messages=[msg]))

    def test_load_dataset_verifies_png_magic(self, tmp_path):
        assets = tmp_path / "assets"
        assets.mkdir()
        (assets / "fake.png").write_bytes(b"this is not a png")
        msg = {"id": "m1", "content": "x", "image_asset": "assets/fake.png",
               "expect": {"rubric": "r", "tools": []}}
        _write_case(tmp_path, messages=[msg])
        with pytest.raises(cr.DatasetError):
            cr.load_dataset(tmp_path, only=None)

    def test_load_dataset_accepts_valid_png(self, tmp_path):
        assets = tmp_path / "assets"
        assets.mkdir()
        (assets / "ok.png").write_bytes(_PNG_MAGIC + b"\x00" * 32)
        msg = {"id": "m1", "content": "x", "image_asset": "assets/ok.png",
               "expect": {"rubric": "r", "tools": []}}
        _write_case(tmp_path, messages=[msg])
        cases = cr.load_dataset(tmp_path, only=None)
        assert cases[0].messages[0].image_asset == "assets/ok.png"

    def test_image_asset_symlink_escape_rejected(self, tmp_path):
        assets = tmp_path / "assets"
        assets.mkdir()
        outside = tmp_path / "outside.png"
        outside.write_bytes(_PNG_MAGIC + b"\x00" * 32)
        (assets / "evil.png").symlink_to(outside)
        msg = {"id": "m1", "content": "x", "image_asset": "assets/evil.png",
               "expect": {"rubric": "r", "tools": []}}
        _write_case(tmp_path, messages=[msg])
        with pytest.raises(cr.DatasetError):
            cr.load_dataset(tmp_path, only=None)

    def test_missing_image_asset_file_rejected(self, tmp_path):
        msg = {"id": "m1", "content": "x", "image_asset": "assets/ghost.png",
               "expect": {"rubric": "r", "tools": []}}
        _write_case(tmp_path, messages=[msg])
        with pytest.raises(cr.DatasetError):
            cr.load_dataset(tmp_path, only=None)

    def test_real_dataset_loads(self):
        dataset_dir = Path(__file__).with_name("conversations")
        cases = cr.load_dataset(dataset_dir, only=None)
        assert len(cases) == 12
        ids = {c.id for c in cases}
        assert {"browser-login-pod", "task-artifact-cmd", "multimodal-vision-chat"} <= ids
        subset = cr.load_dataset(dataset_dir, only=["task-artifact-cmd"])
        assert [c.id for c in subset] == ["task-artifact-cmd"]
        assert subset[0].messages[0].replay_via == "task_api"


class TestRunId:
    def test_unique_same_second(self):
        import datetime as dt
        now = dt.datetime(2026, 9, 13, 12, 0, 0)
        assert cr.make_run_id(now) != cr.make_run_id(now)

    def test_format(self):
        rid = cr.make_run_id()
        assert len(rid.rsplit("-", 1)[1]) == 6


class TestToolEvidence:
    def test_cursor_filters_seen(self):
        before = {"tc1", "tc2"}
        after = [{"id": "tc1"}, {"id": "tc3"}, {"id": "tc2"}]
        assert [t["id"] for t in cr.new_tool_calls(before, after)] == ["tc3"]

    def test_new_tool_calls_preserves_order_and_multiple(self):
        after = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        assert [t["id"] for t in cr.new_tool_calls({"b"}, after)] == ["a", "c"]

    def test_normalize_tool_call_real_shape(self):
        raw = {
            "id": "tc1",
            "session_id": "s1",
            "tool_name": "web_fetch",
            "arguments": {"url": "https://example.com"},
            "result": "weather data",
            "status": "success",
            "duration_ms": 123,
        }
        rec = cr.normalize_tool_call(raw)
        assert rec["id"] == "tc1"
        assert rec["name"] == "web_fetch"
        assert rec["arguments"] == {"url": "https://example.com"}
        assert rec["result"] == "weather data"
        assert rec["status"] == "success"
        assert rec["session_id"] == "s1"

    def test_real_shape_flows_into_deterministic_checks(self):
        raw_list = [{
            "id": "tc1", "session_id": "s1", "tool_name": "web_fetch",
            "arguments": {}, "result": "weather data", "status": "success",
            "duration_ms": 10,
        }]
        recs = [cr.normalize_tool_call(r) for r in raw_list]
        expect = cr.Expect(rubric="r", tools=["web_fetch"], forbidden_tools=[])
        assert cr.deterministic_failures(expect, recs) == []


class TestDeterministicChecks:
    def _expect(self, tools=(), forbidden=()):
        return cr.Expect(rubric="r", tools=list(tools), forbidden_tools=list(forbidden))

    def test_expected_tool_needs_success(self):
        fails = cr.deterministic_failures(
            self._expect(tools=["web_fetch"]),
            [{"name": "web_fetch", "status": "error"}],
        )
        assert fails and "web_fetch" in fails[0]

    def test_expected_tool_success_passes(self):
        assert cr.deterministic_failures(
            self._expect(tools=["web_fetch"]),
            [{"name": "web_fetch", "status": "success", "result": "weather data"}],
        ) == []

    def test_forbidden_tool_hit_fails(self):
        fails = cr.deterministic_failures(
            self._expect(forbidden=["web_fetch"]),
            [{"name": "web_fetch", "status": "success", "result": "weather data"}],
        )
        assert fails

    def test_missing_expected_tool_fails(self):
        assert cr.deterministic_failures(self._expect(tools=["execute_code"]), [])

    def test_success_status_with_empty_result_fails(self):
        for empty in ("", None, [], {}):
            fails = cr.deterministic_failures(
                self._expect(tools=["web_fetch"]),
                [{"name": "web_fetch", "status": "success", "result": empty}],
            )
            assert fails, f"empty result {empty!r} must not count as success"

    def test_forbidden_tool_fails_even_when_error_status(self):
        fails = cr.deterministic_failures(
            self._expect(forbidden=["web_fetch"]),
            [{"name": "web_fetch", "status": "error", "result": None}],
        )
        assert fails

    def test_one_successful_call_among_errors_passes(self):
        assert cr.deterministic_failures(
            self._expect(tools=["web_fetch"]),
            [{"name": "web_fetch", "status": "error", "result": "boom"},
             {"name": "web_fetch", "status": "success", "result": "data"}],
        ) == []


class TestJudgeParse:
    def test_valid(self):
        v = cr.parse_judge_output('{"pass": true, "reason": "ok"}')
        assert v.passed is True and v.reason == "ok"

    def test_fenced_json_accepted(self):
        v = cr.parse_judge_output('```json\n{"pass": false, "reason": "bad"}\n```')
        assert v.passed is False

    def test_non_bool_pass_rejected(self):
        with pytest.raises(cr.JudgeParseError):
            cr.parse_judge_output('{"pass": "true", "reason": "x"}')

    def test_empty_reason_rejected(self):
        with pytest.raises(cr.JudgeParseError):
            cr.parse_judge_output('{"pass": true, "reason": ""}')

    def test_garbage_rejected(self):
        with pytest.raises(cr.JudgeParseError):
            cr.parse_judge_output("I think it passes")

    def test_non_dict_json_rejected(self):
        with pytest.raises(cr.JudgeParseError):
            cr.parse_judge_output('[{"pass": true, "reason": "x"}]')

    def test_missing_keys_rejected(self):
        with pytest.raises(cr.JudgeParseError):
            cr.parse_judge_output('{"pass": true}')
        with pytest.raises(cr.JudgeParseError):
            cr.parse_judge_output('{"reason": "x"}')

    def test_json_embedded_in_prose_accepted(self):
        v = cr.parse_judge_output('Here is my verdict: {"pass": true, "reason": "fine"} done')
        assert v.passed is True


class TestFileContent:
    def test_normalize_removes_exactly_one_trailing_newline(self):
        assert cr.normalize_file_content("abc\n") == "abc"
        assert cr.normalize_file_content("abc\n\n") == "abc\n"
        assert cr.normalize_file_content("abc") == "abc"
        assert cr.normalize_file_content("") == ""

    def test_two_trailing_newlines_mismatch(self):
        assert cr.file_content_matches("artifact a", "artifact a\n")
        assert cr.file_content_matches("artifact a", "artifact a")
        assert not cr.file_content_matches("artifact a", "artifact a\n\n")

    def test_sha256_file(self, tmp_path):
        import hashlib
        p = tmp_path / "x.bin"
        p.write_bytes(b"hello")
        assert cr.sha256_file(p) == hashlib.sha256(b"hello").hexdigest()

    def test_atomic_write_json_roundtrip(self, tmp_path):
        p = tmp_path / "m.json"
        cr.atomic_write_json(p, {"a": 1, "b": "中文"})
        assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "b": "中文"}
        assert not (tmp_path / "m.json.tmp").exists()


class TestCleanupPlan:
    def test_session_deleted_last(self):
        resources = [
            {"type": "session", "id": "s1", "case_id": "c"},
            {"type": "task", "id": "t1", "case_id": "c"},
            {"type": "browser_session", "session_id": "s1", "case_id": "c"},
            {"type": "artifact", "id": "a1", "case_id": "c"},
            {"type": "workspace_file", "path": "x.txt", "sha256": "h", "case_id": "c"},
        ]
        plan = cr.build_cleanup_plan(resources)
        assert [r["type"] for r in plan] == [
            "browser_session", "artifact", "task", "workspace_file", "session",
        ]

    def test_full_order_with_sandbox_history(self):
        resources = [{"type": t} for t in (
            "session", "workspace_file", "task", "artifact",
            "sandbox_history", "browser_session",
        )]
        plan = cr.build_cleanup_plan(resources)
        assert [r["type"] for r in plan] == [
            "browser_session", "sandbox_history", "artifact",
            "task", "workspace_file", "session",
        ]

    def test_unknown_resource_type_rejected(self):
        # T7 起清单语义错误由 ManifestError 承载（DatasetError 仅用于数据集文件）。
        with pytest.raises(cr.ManifestError):
            cr.build_cleanup_plan([{"type": "nuke"}])

    def test_workspace_guard_hash_mismatch_refuses(self):
        r = {"type": "workspace_file", "path": "x.txt", "sha256": "aaa"}
        assert cr.workspace_file_guard(r, current_sha256="bbb") == "refuse_hash_mismatch"

    def test_workspace_guard_absent_is_noop(self):
        r = {"type": "workspace_file", "path": "x.txt", "sha256": "aaa"}
        assert cr.workspace_file_guard(r, current_sha256=None) == "already_absent"

    def test_workspace_guard_match_allows(self):
        r = {"type": "workspace_file", "path": "x.txt", "sha256": "aaa"}
        assert cr.workspace_file_guard(r, current_sha256="aaa") == "delete"


class TestExitCode:
    def test_priority_3_over_2_over_1(self):
        assert cr.compute_exit_code(any_fail=True, runner_error=True, cleanup_failed=True) == 3
        assert cr.compute_exit_code(any_fail=True, runner_error=True, cleanup_failed=False) == 2
        assert cr.compute_exit_code(any_fail=True, runner_error=False, cleanup_failed=False) == 1
        assert cr.compute_exit_code(any_fail=False, runner_error=False, cleanup_failed=False) == 0


# =========================================================================
# TestChannel*: 通道驱动协议 mock 测试（宿主，无真实服务、无第三方依赖）
# =========================================================================


def _sse_chunk(content=None, finish=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    return "data: " + json.dumps({
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }, ensure_ascii=False)


def _sse_approval():
    return "data: " + json.dumps({
        "object": "n-agent.tool_approval",
        "approval": {"confirmation_id": "c1", "tool": "execute_code"},
    }, ensure_ascii=False)


class _FakeStreamResponse:
    def __init__(self, status_code=200, lines=(), body=b""):
        self.status_code = status_code
        self._lines = list(lines)
        self._body = body

    def read(self):
        return self._body

    def iter_lines(self):
        return iter(self._lines)


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *args):
        return False


class _FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"http {self.status}")

    def json(self):
        return self._payload


class _FakeHttpClient:
    """轻量 fake，模拟 httpx.Client 的 stream/get/post/delete。"""

    def __init__(self, stream_lines=(), stream_status=200, stream_body=b"",
                 get_payload=None):
        self._stream_lines = stream_lines
        self._stream_status = stream_status
        self._stream_body = stream_body
        self._get_payload = get_payload
        self.stream_calls = []
        self.get_calls = []
        self.post_calls = []
        self.delete_calls = []

    def stream(self, method, url, json=None, headers=None):
        self.stream_calls.append(
            {"method": method, "url": url, "json": json, "headers": headers})
        return _FakeStreamCtx(
            _FakeStreamResponse(self._stream_status, self._stream_lines, self._stream_body))

    def get(self, url):
        self.get_calls.append(url)
        return _FakeResponse(self._get_payload)

    def post(self, url, params=None):
        self.post_calls.append({"url": url, "params": params})
        return _FakeResponse({})

    def delete(self, url):
        self.delete_calls.append(url)
        return _FakeResponse(None, status=204)


def _dashboard_driver(fake_client):
    # 不经 __init__（宿主无 httpx 保证），直接注入 fake client。
    driver = object.__new__(cr.DashboardDriver)
    driver._client = fake_client
    return driver


class TestChannelDashboard:
    def test_send_concatenates_content_deltas(self):
        client = _FakeHttpClient(stream_lines=[
            "", _sse_chunk("hello "), _sse_chunk("world"),
            _sse_chunk(finish="stop"), "data: [DONE]",
        ])
        driver = _dashboard_driver(client)
        out = driver.send("s1", "hi", None, {})
        assert out == "hello world"

    def test_send_http_error_raises(self):
        client = _FakeHttpClient(stream_status=500, stream_body=b"server boom")
        driver = _dashboard_driver(client)
        with pytest.raises(cr.ReplayError, match="http 500"):
            driver.send("s1", "hi", None, {})

    def test_send_tool_approval_envelope_raises(self):
        client = _FakeHttpClient(stream_lines=[
            _sse_chunk("working"), _sse_approval(),
            _sse_chunk(finish="stop"), "data: [DONE]",
        ])
        driver = _dashboard_driver(client)
        with pytest.raises(cr.ReplayError, match="tool approval"):
            driver.send("s1", "hi", None, {})

    def test_send_incomplete_stream_raises(self):
        # 断流：没有任何 finish_reason 就到 [DONE]。
        client = _FakeHttpClient(stream_lines=[_sse_chunk("partial"), "data: [DONE]"])
        driver = _dashboard_driver(client)
        with pytest.raises(cr.ReplayError, match="incomplete"):
            driver.send("s1", "hi", None, {})

    def test_send_error_finish_raises(self):
        client = _FakeHttpClient(stream_lines=[
            _sse_chunk(content="provider down", finish="error"), "data: [DONE]",
        ])
        driver = _dashboard_driver(client)
        with pytest.raises(cr.ReplayError, match="error"):
            driver.send("s1", "hi", None, {})

    def test_send_malformed_sse_json_raises(self):
        # 非法 data: 载荷必须收敛为 ReplayError 并携带 payload 上下文，
        # 不允许裸 JSONDecodeError 逃逸。
        client = _FakeHttpClient(stream_lines=[
            _sse_chunk("partial"), "data: {oops",
            _sse_chunk(finish="stop"), "data: [DONE]",
        ])
        driver = _dashboard_driver(client)
        with pytest.raises(cr.ReplayError, match="dashboard SSE 非法 JSON"):
            driver.send("s1", "hi", None, {})

    def test_send_plain_text_body_and_headers(self):
        client = _FakeHttpClient(stream_lines=[_sse_chunk(finish="stop"), "data: [DONE]"])
        driver = _dashboard_driver(client)
        options = {"external_memory_enabled": ["file_1"]}
        driver.send("s1", "hi", None, options)
        call = client.stream_calls[0]
        assert call["method"] == "POST"
        assert call["url"] == "/chat/completions"
        assert call["headers"] == {"X-Session-ID": "s1"}
        body = call["json"]
        assert body["stream"] is True
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        assert body["options"] == {"external_memory_enabled": ["file_1"]}
        # options 必须拷贝，调用方后续改动不污染已发请求语义
        assert body["options"] is not options

    def test_send_multimodal_parts_body(self):
        client = _FakeHttpClient(stream_lines=[_sse_chunk(finish="stop"), "data: [DONE]"])
        driver = _dashboard_driver(client)
        driver.send("s1", "看图说话", "data:image/png;base64,AAA", {})
        content = client.stream_calls[0]["json"]["messages"][0]["content"]
        assert content == [
            {"type": "text", "text": "看图说话"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]

    def test_create_session_posts_with_query_param(self):
        client = _FakeHttpClient()
        driver = _dashboard_driver(client)
        driver.create_session("s1")
        assert client.post_calls == [
            {"url": "/chat/sessions", "params": {"session_id": "s1"}}]

    def test_tool_calls_gets_and_normalizes_real_shape(self):
        payload = [{
            "id": "tc1", "session_id": "s1", "tool_name": "web_fetch",
            "arguments": {"url": "https://example.com"}, "result": "data",
            "status": "success", "duration_ms": 5,
        }]
        client = _FakeHttpClient(get_payload=payload)
        driver = _dashboard_driver(client)
        raw = driver.tool_calls("s1")
        assert client.get_calls == ["/chat/sessions/s1/tool-calls"]
        recs = [cr.normalize_tool_call(r) for r in raw]
        assert recs[0]["name"] == "web_fetch"
        expect = cr.Expect(rubric="r", tools=["web_fetch"], forbidden_tools=[])
        assert cr.deterministic_failures(expect, recs) == []

    def test_session_detail_and_delete(self):
        client = _FakeHttpClient(get_payload={"session": {"id": "s1"}, "messages": []})
        driver = _dashboard_driver(client)
        assert driver.session_detail("s1")["session"]["id"] == "s1"
        assert client.get_calls == ["/chat/sessions/s1"]
        driver.delete_session("s1")
        assert client.delete_calls == ["/chat/sessions/s1"]


def _fake_proc(rc=0, out="", err=""):
    import subprocess as sp
    return sp.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


class TestChannelCli:
    def _browse_rows(self, *session_ids):
        return json.dumps([
            {"session_id": sid, "conversation_id": "conv1",
             "display_name": "conv1", "updated_at": "2026-09-14"}
            for sid in session_ids
        ])

    def test_prepare_resolves_unique_session(self, monkeypatch):
        calls = []

        def fake_run(argv, capture_output, text, timeout):
            calls.append(argv)
            if "--browse" in argv:
                return _fake_proc(out=self._browse_rows("cli-abc"))
            return _fake_proc(out="[]")

        monkeypatch.setattr(cr.subprocess, "run", fake_run)
        driver = cr.CliDriver()
        assert driver.prepare("conv1") == "cli-abc"
        # 第一次：准备命令触发 Gateway 建会话（无 --browse）
        assert calls[0][:2] == ["n-agent", "sessions"]
        assert "--browse" not in calls[0]
        assert "--conversation-id" in calls[0] and "conv1" in calls[0]
        # 第二次：--browse 非交互回读
        assert "--browse" in calls[1]
        assert "--no-interactive" in calls[1] and "--json" in calls[1]

    def test_prepare_ambiguous_multi_sessions_rejected(self, monkeypatch):
        monkeypatch.setattr(
            cr.subprocess, "run",
            lambda argv, capture_output, text, timeout: _fake_proc(
                out=self._browse_rows("s1", "s2")))
        with pytest.raises(cr.ReplayError, match="ambiguous"):
            cr.CliDriver().prepare("conv1")

    def test_prepare_zero_sessions_rejected(self, monkeypatch):
        monkeypatch.setattr(
            cr.subprocess, "run",
            lambda argv, capture_output, text, timeout: _fake_proc(out="[]"))
        with pytest.raises(cr.ReplayError, match="ambiguous"):
            cr.CliDriver().prepare("conv1")

    def test_prepare_non_json_browse_output_rejected(self, monkeypatch):
        monkeypatch.setattr(
            cr.subprocess, "run",
            lambda argv, capture_output, text, timeout: _fake_proc(out="not json"))
        with pytest.raises(cr.ReplayError, match="JSON"):
            cr.CliDriver().prepare("conv1")

    def test_prepare_create_command_rc_nonzero_raises(self, monkeypatch):
        monkeypatch.setattr(
            cr.subprocess, "run",
            lambda argv, capture_output, text, timeout: _fake_proc(rc=2, err="boom"))
        with pytest.raises(cr.ReplayError, match="rc=2"):
            cr.CliDriver().prepare("conv1")

    def test_send_passes_message_and_returns_stdout(self, monkeypatch):
        calls = []

        def fake_run(argv, capture_output, text, timeout):
            calls.append(argv)
            return _fake_proc(out="agent answer\n")

        monkeypatch.setattr(cr.subprocess, "run", fake_run)
        out = cr.CliDriver().send("conv1", "hello")
        assert out == "agent answer\n"
        argv = calls[0]
        assert argv[:2] == ["n-agent", "chat"]
        assert "hello" in argv
        assert "--conversation-id" in argv and "conv1" in argv
        assert "--no-stream" in argv

    def test_send_rc_nonzero_raises(self, monkeypatch):
        monkeypatch.setattr(
            cr.subprocess, "run",
            lambda argv, capture_output, text, timeout: _fake_proc(rc=1, err="denied"))
        with pytest.raises(cr.ReplayError, match="rc=1"):
            cr.CliDriver().send("conv1", "hi")

    def test_timeout_wrapped_as_replay_error(self, monkeypatch):
        import subprocess as sp

        def fake_run(argv, capture_output, text, timeout):
            raise sp.TimeoutExpired(cmd=argv, timeout=timeout)

        monkeypatch.setattr(cr.subprocess, "run", fake_run)
        with pytest.raises(cr.ReplayError, match="timeout"):
            cr.CliDriver().send("conv1", "hi")


class TestChannelAcp:
    def test_driver_initial_state(self):
        driver = cr.AcpDriver(cwd="/tmp/x")
        assert driver.session_id is None
        assert driver.client.chunks == []
        assert driver.client.denied_permissions == []

    def test_send_before_start_rejected(self):
        with pytest.raises(cr.ReplayError, match="not started"):
            cr.AcpDriver(cwd="/tmp/x").send("hi")

    def test_close_without_start_is_noop(self):
        cr.AcpDriver(cwd="/tmp/x").close()

    def test_recording_client_collects_agent_message_chunks(self):
        import asyncio
        import types

        client = cr.AcpRecordingClient()
        chunk = types.SimpleNamespace(
            session_update="agent_message_chunk",
            content=types.SimpleNamespace(text="hello"))
        other = types.SimpleNamespace(
            session_update="user_message_chunk",
            content=types.SimpleNamespace(text="ignored"))
        empty = types.SimpleNamespace(
            session_update="agent_message_chunk",
            content=types.SimpleNamespace(text=""))
        tool = types.SimpleNamespace(session_update="tool_call", tool_call_id="t1")
        for upd in (chunk, other, empty, tool):
            asyncio.run(client.session_update("s1", upd))
        assert client.chunks == ["hello"]

    def test_begin_turn_clears_chunks(self):
        client = cr.AcpRecordingClient()
        client.chunks = ["old"]
        client.begin_turn()
        assert client.chunks == []

    def test_request_permission_denies_and_records(self):
        import asyncio
        acp_schema = pytest.importorskip("acp.schema")

        client = cr.AcpRecordingClient()
        resp = asyncio.run(client.request_permission(
            tool_call={"id": "t1"}, options=[{"id": "o1"}]))
        assert isinstance(resp.outcome, acp_schema.DeniedOutcome)
        assert client.denied_permissions == [
            {"tool_call": {"id": "t1"}, "options": [{"id": "o1"}]}]

    def test_unsupported_file_terminal_requests_fail_and_record(self):
        import asyncio

        client = cr.AcpRecordingClient()
        names = (
            "read_text_file", "write_text_file", "create_terminal",
            "terminal_output", "release_terminal", "wait_for_terminal_exit",
            "kill_terminal",
        )
        for name in names:
            with pytest.raises(cr.ReplayError, match=name):
                asyncio.run(getattr(client, name)(session_id="s1"))
        assert client.unsupported_requests == list(names)

    def test_acp_subprocess_env_filters_n_agent_and_excludes_e2e(self):
        environ = {
            "N_AGENT_WORKSPACE_ROOT": "/workspace",
            "N_AGENT_ACP_CONTAINER_WORKSPACE_ROOT": "/workspace-code",
            "N_AGENT_ACP_HOST_WORKSPACE_ROOT": "/host/code",
            "N_AGENT_SQLITE_PATH": "/app/locals/sessions.db",
            "N_AGENT_E2E_JUDGE_API_KEY": "judge-secret",
            "N_AGENT_E2E_BROWSER_PASSWORD": "browser-secret",
            "PATH": "/usr/bin",
            "HOME": "/root",
        }
        env = cr._acp_subprocess_env(environ)
        assert env == {
            "N_AGENT_WORKSPACE_ROOT": "/workspace",
            "N_AGENT_ACP_CONTAINER_WORKSPACE_ROOT": "/workspace-code",
            "N_AGENT_ACP_HOST_WORKSPACE_ROOT": "/host/code",
            "N_AGENT_SQLITE_PATH": "/app/locals/sessions.db",
        }

    def test_start_passes_n_agent_env_to_spawn(self, monkeypatch):
        import sys
        import types

        recorded = {}

        class _FakeCM:
            async def __aenter__(self):
                return (object(), object(), None)

            async def __aexit__(self, *a):
                return False

        def fake_spawn(command, *args, **kwargs):
            recorded["spawn"] = (command, args, kwargs)
            return _FakeCM()

        class _FakeConn:
            async def initialize(self, protocol_version=None):
                return types.SimpleNamespace(
                    auth_methods=[types.SimpleNamespace(id="m1")])

            async def authenticate(self, method_id=None):
                return None

            async def new_session(self, cwd=None):
                recorded["cwd"] = cwd
                return types.SimpleNamespace(session_id="acp-x")

            async def close(self):
                return None

        core = types.ModuleType("acp.core")
        core.connect_to_agent = lambda client, **kw: _FakeConn()
        meta = types.ModuleType("acp.meta")
        meta.PROTOCOL_VERSION = "1"
        transports = types.ModuleType("acp.transports")
        transports.spawn_stdio_transport = fake_spawn
        monkeypatch.setitem(sys.modules, "acp.core", core)
        monkeypatch.setitem(sys.modules, "acp.meta", meta)
        monkeypatch.setitem(sys.modules, "acp.transports", transports)
        monkeypatch.setenv("N_AGENT_ACP_CONTAINER_WORKSPACE_ROOT", "/workspace-code")
        monkeypatch.setenv("N_AGENT_E2E_JUDGE_API_KEY", "judge-secret")

        driver = cr.AcpDriver(cwd="/workspace-code/repo")
        try:
            assert driver.start() == "acp-x"
        finally:
            driver.close()
        command, args, kwargs = recorded["spawn"]
        assert command == "n-agent" and args == ("acp",)
        env = kwargs["env"]
        assert env["N_AGENT_ACP_CONTAINER_WORKSPACE_ROOT"] == "/workspace-code"
        assert "N_AGENT_E2E_JUDGE_API_KEY" not in env
        assert recorded["cwd"] == "/workspace-code/repo"


class TestToolEvidenceItem:
    def test_result_bound_preserves_typical_fetch_payload(self):
        # wttr.in 当前天气 payload 约 2.3KB，temp_C 在 ~491 字符处；
        # 结果证据界限须覆盖典型抓取结果（Run 4 skill-weather-chat 截断教训）。
        rec = {"name": "web_fetch", "status": "success",
               "arguments": "x" * 400, "result": "r" * 5000}
        item = cr._tool_evidence_item(rec)
        assert item["result"] == "r" * 4000 + "...(truncated)"
        assert item["arguments"] == "x" * 300 + "...(truncated)"

    def test_short_result_unchanged(self):
        rec = {"name": "t", "status": "success",
               "arguments": "a", "result": "short"}
        item = cr._tool_evidence_item(rec)
        assert item["result"] == "short"
        assert item["arguments"] == "a"


class TestChannelEvidence:
    def test_acp_channel_includes_protocol_handshake_facts(self):
        case = cr.validate_case(_case(channel="acp"))
        ev = cr._channel_evidence(case, "acp-123")
        assert ev["channel"] == "acp"
        assert ev["session_id"] == "acp-123"
        assert ev["session_established"] is True
        assert "initialize/authenticate/session/new" in ev["acp_protocol"]
        assert "session/prompt" in ev["acp_protocol"]

    def test_non_acp_channel_has_no_protocol_field(self):
        case = cr.validate_case(_case(channel="dashboard"))
        ev = cr._channel_evidence(case, "e2e-x")
        assert ev == {"channel": "dashboard", "session_id": "e2e-x",
                      "session_established": True}


# =========================================================================
# TestJudge*: Judge 客户端与重试决策（宿主，fake openai 注入）
# =========================================================================


class TestJudgeRetry:
    def test_retry_once_then_judge_error(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            raise cr.JudgeParseError("bad")

        v = cr.judge_with_retry(flaky)
        assert calls["n"] == 2
        assert v.passed is False and v.reason.startswith("judge_error")

    def test_first_success_no_retry(self):
        calls = {"n": 0}

        def ok():
            calls["n"] += 1
            return cr.JudgeVerdict(passed=True, reason="fine")

        v = cr.judge_with_retry(ok)
        assert calls["n"] == 1 and v.passed is True

    def test_retry_once_on_network_error_then_success(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise cr.JudgeNetworkError("conn reset")
            return cr.JudgeVerdict(passed=True, reason="ok")

        v = cr.judge_with_retry(flaky)
        assert calls["n"] == 2 and v.passed is True

    def test_non_retryable_exception_propagates(self):
        calls = {"n": 0}

        def bad():
            calls["n"] += 1
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            cr.judge_with_retry(bad)
        assert calls["n"] == 1

    def test_judge_error_reason_truncated_to_500(self):
        def flaky():
            raise cr.JudgeNetworkError("x" * 1000)

        v = cr.judge_with_retry(flaky)
        assert v.passed is False
        assert v.reason.startswith("judge_error") and len(v.reason) <= 500

    def test_mixed_parse_then_network_error_reason_uses_last(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise cr.JudgeParseError("bad json")
            raise cr.JudgeNetworkError("conn reset")

        v = cr.judge_with_retry(flaky)
        assert calls["n"] == 2
        assert v.passed is False
        assert v.reason.startswith("judge_error") and "conn reset" in v.reason


def _fake_openai(monkeypatch, content='{"pass": true, "reason": "ok"}',
                 create_exc=None, empty_choices=False):
    """注入 fake openai 模块（Judge 延迟 import 时命中 sys.modules）。"""
    import types

    calls = {"init": [], "create": []}

    class _Completions:
        def create(self, **kwargs):
            calls["create"].append(kwargs)
            if create_exc is not None:
                raise create_exc
            if empty_choices:
                return types.SimpleNamespace(choices=[])
            message = types.SimpleNamespace(content=content)
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=message)])

    class _OpenAI:
        def __init__(self, **kwargs):
            calls["init"].append(kwargs)
            self.chat = types.SimpleNamespace(completions=_Completions())

    module = types.ModuleType("openai")
    module.OpenAI = _OpenAI
    monkeypatch.setitem(sys.modules, "openai", module)
    return calls


class TestJudgeEvidence:
    def _judge_env(self, monkeypatch):
        for key in ("N_AGENT_E2E_JUDGE_BASE_URL", "N_AGENT_E2E_JUDGE_API_KEY",
                    "N_AGENT_E2E_JUDGE_MODEL"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("N_AGENT_PROVIDER_BASE_URL", "https://provider.example")
        monkeypatch.setenv("N_AGENT_PROVIDER_API_KEY", "provider-key")
        monkeypatch.setenv("N_AGENT_PROVIDER_MODEL", "provider-model")

    def test_prompt_construction_and_verdict(self, monkeypatch):
        self._judge_env(monkeypatch)
        calls = _fake_openai(monkeypatch)
        judge = cr.Judge()
        verdict = judge.judge(
            rubric="必须调用 execute_code",
            user_message="u1", context="c1", actual_response="a1",
            tool_evidence=[{"name": "execute_code"}], side_effects=[])
        assert verdict.passed is True and verdict.reason == "ok"

        init = calls["init"][0]
        assert init["base_url"] == "https://provider.example"
        assert init["api_key"] == "provider-key"
        assert init["timeout"] == 60.0
        assert init["max_retries"] == 0

        create = calls["create"][0]
        assert create["model"] == "provider-model"
        assert create["temperature"] == 0
        system, user = create["messages"]
        assert system["role"] == "system"
        assert "必须调用 execute_code" in system["content"]
        assert "缺失证据" in system["content"]
        assert "只输出 JSON" in system["content"]
        assert user["role"] == "user"
        payload = json.loads(user["content"])
        assert payload == {
            "user_message": "u1", "context": "c1", "actual_response": "a1",
            "tool_evidence": [{"name": "execute_code"}], "side_effects": []}

    def test_e2e_env_overrides_provider_env(self, monkeypatch):
        self._judge_env(monkeypatch)
        monkeypatch.setenv("N_AGENT_E2E_JUDGE_BASE_URL", "https://judge.example")
        monkeypatch.setenv("N_AGENT_E2E_JUDGE_API_KEY", "judge-key")
        monkeypatch.setenv("N_AGENT_E2E_JUDGE_MODEL", "judge-model")
        calls = _fake_openai(monkeypatch)
        judge = cr.Judge()
        judge.judge(rubric="r", user_message="u")
        init = calls["init"][0]
        assert init["base_url"] == "https://judge.example"
        assert init["api_key"] == "judge-key"
        assert calls["create"][0]["model"] == "judge-model"

    def test_sdk_exception_mapped_to_network_error(self, monkeypatch):
        self._judge_env(monkeypatch)
        _fake_openai(monkeypatch, create_exc=RuntimeError("conn reset"))
        judge = cr.Judge()
        with pytest.raises(cr.JudgeNetworkError, match="conn reset"):
            judge.judge(rubric="r", user_message="u")

    def test_parse_error_not_mapped_to_network_error(self, monkeypatch):
        self._judge_env(monkeypatch)
        _fake_openai(monkeypatch, content="not json at all")
        judge = cr.Judge()
        with pytest.raises(cr.JudgeParseError):
            judge.judge(rubric="r", user_message="u")

    def test_non_serializable_kwarg_type_error_not_mapped(self, monkeypatch):
        # 消息构造在 SDK 调用之前：json.dumps 的 TypeError 是数据错误，
        # 原样透传，不得被归类为可重试的 JudgeNetworkError。
        self._judge_env(monkeypatch)
        _fake_openai(monkeypatch)
        judge = cr.Judge()
        with pytest.raises(TypeError):
            judge.judge(rubric="r", user_message=object())

    def test_empty_choices_mapped_to_network_error(self, monkeypatch):
        self._judge_env(monkeypatch)
        _fake_openai(monkeypatch, empty_choices=True)
        judge = cr.Judge()
        with pytest.raises(cr.JudgeNetworkError, match="空 choices"):
            judge.judge(rubric="r", user_message="u")


# =========================================================================
# TestPreflight: 环境探测纯逻辑（fake ctx + 预置 JSON）
# =========================================================================


class _FakePreflightDriver:
    """按 path 返回预置 JSON 的 driver fake；exc 非空时统一抛错。"""

    def __init__(self, responses=None, exc=None):
        self._responses = dict(responses or {})
        self._exc = exc
        self.get_calls = []

    def get(self, path, params=None, timeout=None):
        self.get_calls.append({"path": path, "params": params, "timeout": timeout})
        if self._exc is not None:
            raise self._exc
        return self._responses[path]


class _FakePreflightCtx:
    """编排上下文 fake：暴露 driver 与本用例会话 ID。"""

    def __init__(self, driver, session_id="sess-1"):
        self.driver = driver
        self.session_id = session_id


def _good_preflight_responses():
    return {
        "/chat/tools": [{"name": "execute_code"}, {"name": "web_fetch"}],
        "/chat/plugins": {"items": [{"name": "hello", "enabled": True}]},
        "/chat/external-memory/providers": {
            "providers": [{"provider_type": "mem0", "enabled": True}]},
        "/chat/knowledge/bases": [{"base_type": "n_kb", "enabled": True}],
        "/chat/providers": [{"is_active": True, "supports_vision": True}],
        "/chat/tasks/board": {"columns": []},
        "/chat/browser/sessions": {"sessions": []},
    }


def _preflight_case(*requires):
    return cr.Case(
        id="c1", title="t", source_session_id="s", channel="dashboard",
        requires=list(requires), options={}, messages=[], side_effects=[],
        cleanup=[])


class TestPreflight:
    def _ctx(self, responses=None, exc=None, session_id="sess-1"):
        driver = _FakePreflightDriver(responses, exc)
        return _FakePreflightCtx(driver, session_id), driver

    def test_all_requires_pass_on_good_fixtures(self, monkeypatch):
        monkeypatch.setenv("N_AGENT_E2E_BROWSER_PASSWORD", "pw")
        ctx, _ = self._ctx(_good_preflight_responses())
        case = _preflight_case(*sorted(cr.ALLOWED_REQUIRES))
        assert cr.run_preflight(case, ctx) == []

    def test_memory_file_is_noop(self):
        ctx, _ = self._ctx({})
        assert cr.run_preflight(_preflight_case("memory-file"), ctx) == []

    @pytest.mark.parametrize("require,responses,expected", [
        ("sandbox",
         {"/chat/tools": [{"name": "web_fetch"}]},
         "sandbox: execute_code 不可用"),
        ("plugin-hello",
         {"/chat/plugins": {"items": [{"name": "hello", "enabled": False}]}},
         "plugin-hello: hello 插件不可用或未启用"),
        ("mem0",
         {"/chat/external-memory/providers": {"providers": [
             {"provider_type": "mem0", "enabled": False}]}},
         "mem0: 无启用的 mem0 provider"),
        ("nkb",
         {"/chat/knowledge/bases": [{"base_type": "n_kb", "enabled": False}]},
         "nkb: 无启用的 N-KB 知识库"),
        ("vision",
         {"/chat/providers": [{"is_active": True, "supports_vision": False}]},
         "vision: active provider 不支持 vision"),
        ("task",
         {"/chat/tasks/board": {"oops": []}},
         "task: tasks/board 不可用"),
        ("browser",
         {"/chat/browser/sessions": {"sessions": {}}},
         "browser: sessions 字段不是数组"),
        ("web",
         {"/chat/tools": [{"name": "execute_code"}]},
         "web: web_fetch 不可用"),
    ])
    def test_checker_failure_strings(self, require, responses, expected):
        ctx, _ = self._ctx(responses)
        assert cr.run_preflight(_preflight_case(require), ctx) == [expected]

    def test_browser_credentials_missing_env(self, monkeypatch):
        monkeypatch.delenv("N_AGENT_E2E_BROWSER_PASSWORD", raising=False)
        ctx, _ = self._ctx({})
        assert cr.run_preflight(_preflight_case("browser-credentials"), ctx) == [
            "browser-credentials: N_AGENT_E2E_BROWSER_PASSWORD 未设置"]

    def test_browser_checker_uses_case_session_id(self):
        ctx, driver = self._ctx({"/chat/browser/sessions": {"sessions": []}},
                                session_id="sess-42")
        assert cr.run_preflight(_preflight_case("browser"), ctx) == []
        call = driver.get_calls[0]
        assert call["path"] == "/chat/browser/sessions"
        assert call["params"] == {"n_agent_session_id": "sess-42"}

    def test_http_checkers_use_15s_timeout(self):
        ctx, driver = self._ctx(_good_preflight_responses())
        case = _preflight_case("sandbox", "task")
        assert cr.run_preflight(case, ctx) == []
        assert driver.get_calls
        assert all(c["timeout"] == 15.0 for c in driver.get_calls)

    def test_unknown_require_skipped_silently(self):
        ctx, _ = self._ctx({})
        assert cr.run_preflight(_preflight_case("unknown-req"), ctx) == []

    def test_checker_exception_wrapped(self):
        ctx, _ = self._ctx(exc=RuntimeError("boom"))
        assert cr.run_preflight(_preflight_case("sandbox"), ctx) == [
            "sandbox: preflight error RuntimeError: boom"]

    def test_multiple_failures_aggregated_in_order(self):
        ctx, _ = self._ctx({"/chat/tools": []})
        assert cr.run_preflight(_preflight_case("sandbox", "web"), ctx) == [
            "sandbox: execute_code 不可用", "web: web_fetch 不可用"]

    @pytest.mark.parametrize("require,path,expected", [
        ("sandbox", "/chat/tools", "sandbox: execute_code 不可用"),
        ("nkb", "/chat/knowledge/bases", "nkb: 无启用的 N-KB 知识库"),
        ("vision", "/chat/providers", "vision: active provider 不支持 vision"),
        ("web", "/chat/tools", "web: web_fetch 不可用"),
    ])
    @pytest.mark.parametrize("bad_top", [None, "oops", {"oops": []}])
    def test_wrong_type_top_level_degrades_to_fail_string(
            self, require, path, expected, bad_top):
        # 顶层响应为 None/str/dict 时降级为 checker 的 FAIL 字符串，
        # 不得崩溃为 "preflight error"。
        ctx, _ = self._ctx({path: bad_top})
        failures = cr.run_preflight(_preflight_case(require), ctx)
        assert failures == [expected]
        assert "preflight error" not in failures[0]


# =========================================================================
# TestWaitTaskTerminal: 任务终态轮询（subprocess/monotonic/sleep 桩）
# =========================================================================


class TestWaitTaskTerminal:
    def _proc(self, rc=0, stdout=""):
        import subprocess as sp
        return sp.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")

    def test_terminal_returns_task(self, monkeypatch):
        calls = {"n": 0}
        def fake_run(argv, **kw):
            calls["n"] += 1
            return self._proc(stdout=json.dumps({"id": "t_x", "status": "succeeded"}))
        monkeypatch.setattr(cr.subprocess, "run", fake_run)
        task = cr.wait_task_terminal("t_x", deadline=cr.time.monotonic() + 60, interval=0)
        assert task["status"] == "succeeded" and calls["n"] == 1

    def test_polls_until_terminal(self, monkeypatch):
        statuses = iter(["running", "running", "succeeded"])
        monkeypatch.setattr(cr.subprocess, "run",
            lambda argv, **kw: self._proc(stdout=json.dumps({"status": next(statuses)})))
        monkeypatch.setattr(cr.time, "sleep", lambda s: None)
        task = cr.wait_task_terminal("t_x", deadline=cr.time.monotonic() + 60, interval=0)
        assert task["status"] == "succeeded"

    def test_timeout_raises_single_budget(self, monkeypatch):
        calls = {"n": 0}
        def fake_run(argv, **kw):
            calls["n"] += 1
            return self._proc(stdout=json.dumps({"status": "running"}))
        monkeypatch.setattr(cr.subprocess, "run", fake_run)
        monkeypatch.setattr(cr.time, "sleep", lambda s: None)
        # deadline 用 tick 0.0 算出（=60）；首轮检查 tick 10.0 通过并轮询一次，
        # sleep 参数求值消费 tick 100.0，次轮检查 tick 200.0 越限抛出。
        ticks = iter([0.0, 10.0, 100.0, 200.0])
        monkeypatch.setattr(cr.time, "monotonic", lambda: next(ticks))
        with pytest.raises(cr.ReplayError, match="deadline"):
            cr.wait_task_terminal("t_x", deadline=cr.time.monotonic() + 60, interval=0)
        assert calls["n"] >= 1  # 至少完成一次真实轮询后才判定超时

    def test_nonzero_rc_raises(self, monkeypatch):
        monkeypatch.setattr(cr.subprocess, "run",
            lambda argv, **kw: self._proc(rc=3, stdout=""))
        with pytest.raises(cr.ReplayError, match="task show failed"):
            cr.wait_task_terminal("t_x", deadline=cr.time.monotonic() + 60, interval=0)

    def test_task_show_timeout_raises(self, monkeypatch):
        import subprocess as sp
        def fake_run(argv, **kw):
            raise sp.TimeoutExpired(cmd=argv, timeout=1)
        monkeypatch.setattr(cr.subprocess, "run", fake_run)
        with pytest.raises(cr.ReplayError, match="task show timeout"):
            cr.wait_task_terminal("t_x", deadline=cr.time.monotonic() + 60, interval=0)

    def test_invalid_json_output_raises(self, monkeypatch):
        monkeypatch.setattr(cr.subprocess, "run",
            lambda argv, **kw: self._proc(stdout="not-json"))
        with pytest.raises(cr.ReplayError, match="task show output invalid"):
            cr.wait_task_terminal("t_x", deadline=cr.time.monotonic() + 60, interval=0)

    def test_status_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(cr.subprocess, "run",
            lambda argv, **kw: self._proc(stdout=json.dumps({"status": "Succeeded"})))
        task = cr.wait_task_terminal("t_x", deadline=cr.time.monotonic() + 60, interval=0)
        assert task["status"] == "Succeeded"

    def test_deadline_checked_before_poll(self, monkeypatch):
        calls = {"n": 0}
        def fake_run(argv, **kw):
            calls["n"] += 1
            return self._proc(stdout=json.dumps({"status": "succeeded"}))
        monkeypatch.setattr(cr.subprocess, "run", fake_run)
        with pytest.raises(cr.ReplayError, match="deadline"):
            cr.wait_task_terminal("t_x", deadline=cr.time.monotonic() - 1, interval=0)
        assert calls["n"] == 0

    def test_argv_and_timeout_cap(self, monkeypatch):
        seen = {}
        def fake_run(argv, **kw):
            seen["argv"] = argv
            seen["timeout"] = kw.get("timeout")
            return self._proc(stdout=json.dumps({"status": "succeeded"}))
        monkeypatch.setattr(cr.subprocess, "run", fake_run)
        cr.wait_task_terminal("t_x", deadline=cr.time.monotonic() + 600, interval=0)
        assert seen["argv"] == ["n-agent", "task", "show", "t_x", "--json"]
        assert seen["timeout"] <= 30


# =========================================================================
# TestSideEffectWorkspaceFile: 工作区文件安全读取
# =========================================================================


class TestSideEffectWorkspaceFile:
    def test_valid_file_content(self, tmp_path):
        (tmp_path / "out.txt").write_text("artifact a", encoding="utf-8")
        assert cr.read_workspace_file(tmp_path, "out.txt") == "artifact a"

    def test_missing_returns_none(self, tmp_path):
        assert cr.read_workspace_file(tmp_path, "nope.txt") is None

    def test_dotdot_rejected(self, tmp_path):
        with pytest.raises(cr.ReplayError, match="workspace path invalid"):
            cr.read_workspace_file(tmp_path, "../evil.txt")

    def test_absolute_rejected(self, tmp_path):
        with pytest.raises(cr.ReplayError, match="workspace path invalid"):
            cr.read_workspace_file(tmp_path, "/etc/passwd")

    def test_empty_rejected(self, tmp_path):
        with pytest.raises(cr.ReplayError, match="workspace path invalid"):
            cr.read_workspace_file(tmp_path, "")

    def test_symlink_component_rejected(self, tmp_path):
        real = tmp_path / "real.txt"
        real.write_text("x", encoding="utf-8")
        (tmp_path / "link.txt").symlink_to(real)
        with pytest.raises(cr.ReplayError, match="workspace symlink forbidden"):
            cr.read_workspace_file(tmp_path, "link.txt")

    def test_dangling_symlink_rejected(self, tmp_path):
        (tmp_path / "dangling.txt").symlink_to(tmp_path / "nonexistent-target")
        with pytest.raises(cr.ReplayError, match="workspace symlink forbidden"):
            cr.read_workspace_file(tmp_path, "dangling.txt")

    def test_directory_target_rejected(self, tmp_path):
        (tmp_path / "subdir").mkdir()
        with pytest.raises(cr.ReplayError, match="workspace requires regular file"):
            cr.read_workspace_file(tmp_path, "subdir")

    def test_symlinked_parent_escape_rejected(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("s", encoding="utf-8")
        root = tmp_path / "ws"
        root.mkdir()
        (root / "esc").symlink_to(outside, target_is_directory=True)
        with pytest.raises(cr.ReplayError, match="workspace symlink forbidden"):
            cr.read_workspace_file(root, "esc/secret.txt")


# =========================================================================
# TestSideEffectListArtifacts: 制品分页累加（DashboardDriver.get 协议 fake）
# =========================================================================


class _FakeListDriver:
    """按调用序返回预置分页 JSON 的 driver fake。"""

    def __init__(self, pages):
        self._pages = list(pages)
        self.get_calls = []

    def get(self, path, params=None, timeout=None):
        self.get_calls.append({"path": path, "params": params})
        return self._pages.pop(0)


class TestSideEffectListArtifacts:
    def test_single_page(self):
        driver = _FakeListDriver([
            {"items": [{"id": "a1"}, {"id": "a2"}], "next_cursor": None},
        ])
        items = cr.list_artifacts(driver, "task-1")
        assert [i["id"] for i in items] == ["a1", "a2"]
        assert driver.get_calls[0]["params"] == {
            "source_context_ref": "task-1", "limit": 50}

    def test_multi_page_cursor_accumulation(self):
        cursor = {"updated_at": "2026-09-13T12:00:00+00:00", "artifact_id": "a2"}
        driver = _FakeListDriver([
            {"items": [{"id": "a1"}, {"id": "a2"}], "next_cursor": cursor},
            {"items": [{"id": "a3"}], "next_cursor": None},
        ])
        items = cr.list_artifacts(driver, "task-1")
        assert [i["id"] for i in items] == ["a1", "a2", "a3"]
        assert len(driver.get_calls) == 2
        # next_cursor 是 JSON 对象，回传 cursor 参数必须 JSON 编码（_parse_cursor 契约）。
        assert driver.get_calls[1]["params"]["cursor"] == json.dumps(cursor)

    def test_empty(self):
        driver = _FakeListDriver([{"items": [], "next_cursor": None}])
        assert cr.list_artifacts(driver, "task-1") == []

    def test_invalid_response_shape_raises(self):
        driver = _FakeListDriver([{"items": "not-a-list"}])
        with pytest.raises(cr.ReplayError, match="artifacts list invalid response"):
            cr.list_artifacts(driver, "task-1")

    def test_repeated_cursor_raises(self):
        # 病态服务端持续回传相同非空 next_cursor：检测后报错而非无限翻页。
        cursor = {"updated_at": "2026-09-13T12:00:00+00:00", "artifact_id": "a2"}
        driver = _FakeListDriver([
            {"items": [{"id": "a1"}], "next_cursor": cursor},
            {"items": [{"id": "a1"}], "next_cursor": cursor},
        ])
        with pytest.raises(cr.ReplayError, match="cursor not advancing"):
            cr.list_artifacts(driver, "task-1")


# =========================================================================
# TestSideEffectVerify: verify_side_effects 分发与判定
# =========================================================================


class _FakeVerifyDriver:
    """制品列表 + content 文本端点 fake。"""

    def __init__(self, pages=None, contents=None):
        self._pages = list(pages or [])
        self._contents = dict(contents or {})
        self.get_calls = []

    def get(self, path, params=None, timeout=None):
        self.get_calls.append({"path": path, "params": params})
        return self._pages.pop(0)

    def get_text(self, path, params=None, timeout=None):
        return self._contents[path]


class _FakeSideEffectCtx:
    """编排上下文 fake：driver / workspace_root / task_id / task_deadline。"""

    def __init__(self, driver=None, workspace_root=None, task_id="task-1",
                 deadline=None):
        self.driver = driver
        self.workspace_root = workspace_root
        self.task_id = task_id
        self.task_deadline = deadline


class TestSideEffectVerify:
    def _proc(self, rc=0, stdout=""):
        import subprocess as sp
        return sp.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")

    def _ctx(self, **kw):
        kw.setdefault("deadline", cr.time.monotonic() + 600)
        return _FakeSideEffectCtx(**kw)

    def _case_with(self, *side_effects):
        return cr.validate_case(_case(side_effects=list(side_effects)))

    def _stub_task(self, monkeypatch, status):
        monkeypatch.setattr(cr.subprocess, "run",
            lambda argv, **kw: self._proc(stdout=json.dumps({"status": status})))

    def test_task_terminal_pass(self, monkeypatch):
        self._stub_task(monkeypatch, "succeeded")
        case = self._case_with({"type": "task_terminal", "status": "succeeded",
                                "timeout": 600})
        results = cr.verify_side_effects(case, self._ctx())
        assert results == [{"type": "task_terminal", "verdict": "PASS",
                            "detail": "task status succeeded"}]

    def test_task_terminal_fail_status(self, monkeypatch):
        self._stub_task(monkeypatch, "failed")
        case = self._case_with({"type": "task_terminal", "status": "succeeded",
                                "timeout": 600})
        results = cr.verify_side_effects(case, self._ctx())
        assert results[0]["verdict"] == "FAIL"
        assert "failed" in results[0]["detail"]

    def test_task_terminal_unresolved_task_id(self, monkeypatch):
        self._stub_task(monkeypatch, "succeeded")
        case = self._case_with({"type": "task_terminal", "status": "succeeded",
                                "timeout": 600})
        results = cr.verify_side_effects(case, self._ctx(task_id=None))
        assert results[0]["verdict"] == "FAIL"

    def test_workspace_file_match(self, tmp_path):
        (tmp_path / "task-output-a.txt").write_text("artifact a", encoding="utf-8")
        case = self._case_with({"type": "workspace_file",
                                "path": "task-output-a.txt",
                                "expected_content": "artifact a"})
        results = cr.verify_side_effects(
            case, self._ctx(workspace_root=tmp_path))
        assert results[0]["verdict"] == "PASS"
        assert results[0]["sha256"] == cr.sha256_file(tmp_path / "task-output-a.txt")

    def test_workspace_file_mismatch(self, tmp_path):
        (tmp_path / "out.txt").write_text("wrong", encoding="utf-8")
        case = self._case_with({"type": "workspace_file", "path": "out.txt",
                                "expected_content": "artifact a"})
        results = cr.verify_side_effects(case, self._ctx(workspace_root=tmp_path))
        assert results[0]["verdict"] == "FAIL"
        assert results[0]["sha256"] == cr.sha256_file(tmp_path / "out.txt")

    def test_workspace_file_pass_includes_path_for_manifest(self, tmp_path):
        (tmp_path / "out.txt").write_text("artifact a", encoding="utf-8")
        case = self._case_with({"type": "workspace_file", "path": "out.txt",
                                "expected_content": "artifact a"})
        results = cr.verify_side_effects(case, self._ctx(workspace_root=tmp_path))
        assert results[0]["verdict"] == "PASS"
        assert results[0]["path"] == "out.txt"

    def test_workspace_file_mismatch_includes_path_for_manifest(self, tmp_path):
        (tmp_path / "out.txt").write_text("wrong", encoding="utf-8")
        case = self._case_with({"type": "workspace_file", "path": "out.txt",
                                "expected_content": "artifact a"})
        results = cr.verify_side_effects(case, self._ctx(workspace_root=tmp_path))
        assert results[0]["verdict"] == "FAIL"
        assert results[0]["path"] == "out.txt"

    def test_workspace_file_missing(self, tmp_path):
        case = self._case_with({"type": "workspace_file", "path": "out.txt",
                                "expected_content": "artifact a"})
        results = cr.verify_side_effects(case, self._ctx(workspace_root=tmp_path))
        assert results[0]["verdict"] == "FAIL"

    def test_artifact_count_exact(self):
        driver = _FakeVerifyDriver(pages=[
            {"items": [{"id": "a1", "name": "x", "kind": "markdown"},
                       {"id": "a2", "name": "y", "kind": "markdown"}],
             "next_cursor": None},
        ])
        case = self._case_with({"type": "artifact_registered", "count": 2,
                                "source_context_ref": "task_id",
                                "storage_ref_prefix": "workspace:"},
                               {"type": "workspace_file", "path": "x",
                                "expected_content": "1"},
                               {"type": "workspace_file", "path": "y",
                                "expected_content": "2"})
        results = cr.verify_side_effects(case, self._ctx(driver=driver))
        artifact_result = results[0]
        assert artifact_result["verdict"] == "PASS"
        call = driver.get_calls[0]
        assert call["path"] == "/chat/artifacts"
        assert call["params"]["source_context_ref"] == "task-1"

    def test_artifact_count_mismatch(self):
        driver = _FakeVerifyDriver(pages=[
            {"items": [{"id": "a1", "name": "x"}], "next_cursor": None},
        ])
        case = self._case_with({"type": "artifact_registered", "count": 2,
                                "source_context_ref": "task_id"})
        results = cr.verify_side_effects(case, self._ctx(driver=driver))
        assert results[0]["verdict"] == "FAIL"
        assert "'x'" in results[0]["detail"]  # detail 含实际制品名

    def test_artifact_unresolved_task_id(self):
        case = self._case_with({"type": "artifact_registered", "count": 1,
                                "source_context_ref": "task_id"})
        results = cr.verify_side_effects(case, self._ctx(task_id=None))
        assert results[0]["verdict"] == "FAIL"
        assert "task id unresolved" in results[0]["detail"]

    def test_artifact_driver_unavailable(self):
        case = self._case_with({"type": "artifact_registered", "count": 1,
                                "source_context_ref": "task_id"})
        results = cr.verify_side_effects(case, self._ctx(driver=None))
        assert results[0]["verdict"] == "FAIL"
        assert "driver unavailable" in results[0]["detail"]

    def test_artifact_storage_ref_expectations_unresolvable(self):
        # storage_ref_prefix 校验依赖 workspace_file 声明；缺失时优雅 FAIL。
        driver = _FakeVerifyDriver(pages=[
            {"items": [{"id": "a1", "name": "x"}], "next_cursor": None},
        ])
        case = self._case_with({"type": "artifact_registered", "count": 1,
                                "source_context_ref": "task_id",
                                "storage_ref_prefix": "workspace:"})
        results = cr.verify_side_effects(case, self._ctx(driver=driver))
        assert results[0]["verdict"] == "FAIL"
        assert "storage_ref expectations unresolvable" in results[0]["detail"]

    def test_artifact_storage_ref_exact_names(self, tmp_path):
        (tmp_path / "task-output-a.txt").write_text("artifact a", encoding="utf-8")
        (tmp_path / "task-output-b.md").write_text("# artifact b", encoding="utf-8")
        driver = _FakeVerifyDriver(pages=[
            {"items": [{"id": "a1", "name": "task-output-a.txt", "kind": "text"},
                       {"id": "a2", "name": "task-output-b.md", "kind": "markdown"}],
             "next_cursor": None},
        ])
        case = self._case_with(
            {"type": "workspace_file", "path": "task-output-a.txt",
             "expected_content": "artifact a"},
            {"type": "workspace_file", "path": "task-output-b.md",
             "expected_content": "# artifact b"},
            {"type": "artifact_registered", "count": 2,
             "source_context_ref": "task_id", "storage_ref_prefix": "workspace:"})
        results = cr.verify_side_effects(
            case, self._ctx(driver=driver, workspace_root=tmp_path))
        assert [r["verdict"] for r in results] == ["PASS", "PASS", "PASS"]

    def test_artifact_wrong_name_fails(self, tmp_path):
        (tmp_path / "task-output-a.txt").write_text("artifact a", encoding="utf-8")
        driver = _FakeVerifyDriver(pages=[
            {"items": [{"id": "a1", "name": "renamed.txt"}], "next_cursor": None},
        ])
        case = self._case_with(
            {"type": "workspace_file", "path": "task-output-a.txt",
             "expected_content": "artifact a"},
            {"type": "artifact_registered", "count": 1,
             "source_context_ref": "task_id", "storage_ref_prefix": "workspace:"})
        results = cr.verify_side_effects(
            case, self._ctx(driver=driver, workspace_root=tmp_path))
        assert results[1]["verdict"] == "FAIL"
        assert "renamed.txt" in results[1]["detail"]

    def test_artifact_inline_content_evidence(self):
        driver = _FakeVerifyDriver(
            pages=[{"items": [{"id": "art-1", "name": "report.md",
                               "kind": "markdown"}],
                    "next_cursor": None}],
            contents={"/chat/artifacts/art-1/content": "  博客 报告\n内容  "})
        case = self._case_with({"type": "artifact_registered", "count": 1,
                                "source_context_ref": "task_id",
                                "kind": "markdown"})
        results = cr.verify_side_effects(case, self._ctx(driver=driver))
        assert results[0]["verdict"] == "PASS"
        evidence = results[0]["artifacts"][0]
        # 去空白 Unicode 计数："博客报告内容" = 6。
        assert evidence["char_count"] == 6
        assert "博客" in evidence["content"]

    def test_artifact_kind_mismatch_fails(self):
        driver = _FakeVerifyDriver(
            pages=[{"items": [{"id": "art-1", "name": "report.txt",
                               "kind": "text"}],
                    "next_cursor": None}],
            contents={"/chat/artifacts/art-1/content": "x"})
        case = self._case_with({"type": "artifact_registered", "count": 1,
                                "source_context_ref": "task_id",
                                "kind": "markdown"})
        results = cr.verify_side_effects(case, self._ctx(driver=driver))
        assert results[0]["verdict"] == "FAIL"

    def test_unknown_type_fail_closed(self):
        case = self._case_with()
        case.side_effects.append({"type": "rm_rf"})
        results = cr.verify_side_effects(case, self._ctx())
        assert results[0]["verdict"] == "FAIL"
        assert "unknown side effect type" in results[0]["detail"]

    def test_verify_error_becomes_fail_verdict(self, tmp_path, monkeypatch):
        # 验证器内部非 ReplayError 异常收敛为 FAIL（不崩溃编排）。
        def boom(root, rel_path):
            raise ValueError("disk gone")
        monkeypatch.setattr(cr, "read_workspace_file", boom)
        case = self._case_with({"type": "workspace_file", "path": "out.txt",
                                "expected_content": "x"})
        results = cr.verify_side_effects(
            case, self._ctx(workspace_root=tmp_path))
        assert results[0]["verdict"] == "FAIL"
        assert results[0]["detail"].startswith("verify error ValueError")

    def test_replay_error_propagates(self, monkeypatch):
        # 基础设施错误（task show 失败）原样透传，由编排按运行错误处理。
        monkeypatch.setattr(cr.subprocess, "run",
            lambda argv, **kw: self._proc(rc=1, stdout=""))
        case = self._case_with({"type": "task_terminal", "status": "succeeded",
                                "timeout": 600})
        with pytest.raises(cr.ReplayError):
            cr.verify_side_effects(case, self._ctx())


# =============================================================================
# 第十四节: 浏览器接管（纯逻辑 + fake driver 流程）
# =============================================================================

_BROWSER_SID = "e2e-20260914-000000-a1b2c3-browser-login-pod"
_BROWSER_ROW = {
    "id": "bsess-1",
    "n_agent_session_id": _BROWSER_SID,
    "backend_type": "container",
    "status": "active",
    "profile_ref": "bp-container-abcdef012345",
}


def _browser_case():
    import types
    return types.SimpleNamespace(messages=[
        types.SimpleNamespace(
            id="m1",
            content="调用 browser_navigate 打开页面 https://a6thn73a795g.meoo.info/#/login。本会话禁用web_fetch工具"),
    ])


class TestSelectUniqueBrowserSession:
    def test_unique_match(self):
        resp = {"sessions": [
            {"id": "bsess-1", "n_agent_session_id": _BROWSER_SID},
            {"id": "bsess-2", "n_agent_session_id": "other"},
        ]}
        assert cr.select_unique_browser_session_id(resp, _BROWSER_SID) == "bsess-1"

    def test_zero_match_fails(self):
        with pytest.raises(cr._BrowserCaseFail):
            cr.select_unique_browser_session_id({"sessions": []}, _BROWSER_SID)

    def test_multiple_match_fails(self):
        resp = {"sessions": [
            {"id": "bsess-1", "n_agent_session_id": _BROWSER_SID},
            {"id": "bsess-2", "n_agent_session_id": _BROWSER_SID},
        ]}
        with pytest.raises(cr._BrowserCaseFail):
            cr.select_unique_browser_session_id(resp, _BROWSER_SID)

    def test_invalid_shape_is_replay_error(self):
        with pytest.raises(cr.ReplayError):
            cr.select_unique_browser_session_id({"sessions": "nope"}, _BROWSER_SID)
        with pytest.raises(cr.ReplayError):
            cr.select_unique_browser_session_id([], _BROWSER_SID)


class TestExtractWriteChallenge:
    def test_token_returned(self):
        detail = {"status": "active", "write_challenges": {"takeover": "tok-1"}}
        assert cr.extract_write_challenge(detail, "takeover") == "tok-1"

    def test_missing_op_fails(self):
        detail = {"status": "takeover", "write_challenges": {"release": "tok-r"}}
        with pytest.raises(cr._BrowserCaseFail):
            cr.extract_write_challenge(detail, "takeover")

    def test_empty_token_fails(self):
        detail = {"status": "active", "write_challenges": {"takeover": ""}}
        with pytest.raises(cr._BrowserCaseFail):
            cr.extract_write_challenge(detail, "takeover")

    def test_invalid_shape_is_replay_error(self):
        with pytest.raises(cr.ReplayError):
            cr.extract_write_challenge({"status": "active"}, "takeover")


class TestLookupSessionProfileRef:
    def test_valid_row(self):
        assert cr.lookup_session_profile_ref(
            [_BROWSER_ROW], "bsess-1", _BROWSER_SID) == "bp-container-abcdef012345"

    def test_missing_row_fails(self):
        with pytest.raises(cr._BrowserCaseFail):
            cr.lookup_session_profile_ref([], "bsess-1", _BROWSER_SID)

    def test_duplicate_rows_fail(self):
        with pytest.raises(cr._BrowserCaseFail):
            cr.lookup_session_profile_ref(
                [_BROWSER_ROW, dict(_BROWSER_ROW)], "bsess-1", _BROWSER_SID)

    def test_wrong_n_agent_session_ignored(self):
        row = dict(_BROWSER_ROW, n_agent_session_id="other")
        with pytest.raises(cr._BrowserCaseFail):
            cr.lookup_session_profile_ref([row], "bsess-1", _BROWSER_SID)

    def test_non_container_backend_fails(self):
        row = dict(_BROWSER_ROW, backend_type="host_cdp")
        with pytest.raises(cr._BrowserCaseFail):
            cr.lookup_session_profile_ref([row], "bsess-1", _BROWSER_SID)

    def test_non_takeoverable_status_fails(self):
        row = dict(_BROWSER_ROW, status="closed")
        with pytest.raises(cr._BrowserCaseFail):
            cr.lookup_session_profile_ref([row], "bsess-1", _BROWSER_SID)

    def test_short_hash_profile_ref_rejected(self):
        # Dashboard 详情只暴露短哈希，禁止当原始 profile_ref 使用。
        row = dict(_BROWSER_ROW, profile_ref="bp-conta...")
        with pytest.raises(cr._BrowserCaseFail):
            cr.lookup_session_profile_ref([row], "bsess-1", _BROWSER_SID)

    def test_malformed_profile_ref_rejected(self):
        row = dict(_BROWSER_ROW, profile_ref="bp-container-XYZ")
        with pytest.raises(cr._BrowserCaseFail):
            cr.lookup_session_profile_ref([row], "bsess-1", _BROWSER_SID)


class TestBuildCdpEndpoint:
    _payload = {"cdp_port": 20222, "runtime_id": "r1"}

    def test_valid(self):
        ep = cr.build_cdp_endpoint(
            "http://browser:9223", self._payload, resolve=lambda h: "172.19.0.10")
        assert ep == "http://172.19.0.10:20222"

    def test_port_out_of_range(self):
        with pytest.raises(cr._BrowserCaseFail):
            cr.build_cdp_endpoint(
                "http://browser:9223", {"cdp_port": 80, "runtime_id": "r1"},
                resolve=lambda h: "172.19.0.10")

    def test_port_bool_rejected(self):
        with pytest.raises(cr._BrowserCaseFail):
            cr.build_cdp_endpoint(
                "http://browser:9223", {"cdp_port": True, "runtime_id": "r1"},
                resolve=lambda h: "172.19.0.10")

    def test_runtime_id_empty(self):
        with pytest.raises(cr._BrowserCaseFail):
            cr.build_cdp_endpoint(
                "http://browser:9223", {"cdp_port": 20222, "runtime_id": ""},
                resolve=lambda h: "172.19.0.10")

    def test_bad_scheme(self):
        with pytest.raises(cr._BrowserCaseFail):
            cr.build_cdp_endpoint(
                "ftp://browser:9223", self._payload,
                resolve=lambda h: "172.19.0.10")

    def test_dns_failure_is_replay_error(self):
        def boom(host):
            raise OSError("no resolve")
        with pytest.raises(cr.ReplayError):
            cr.build_cdp_endpoint("http://browser:9223", self._payload,
                                  resolve=boom)

    def test_ipv6_bracketed(self):
        ep = cr.build_cdp_endpoint(
            "http://browser:9223", self._payload, resolve=lambda h: "fd00::1")
        assert ep == "http://[fd00::1]:20222"


class TestExtractExpectedHost:
    def test_host_from_m1(self):
        host = cr.extract_expected_host(_browser_case().messages)
        assert host == "a6thn73a795g.meoo.info"

    def test_m1_missing_fails(self):
        import types
        with pytest.raises(cr._BrowserCaseFail):
            cr.extract_expected_host(
                [types.SimpleNamespace(id="m2", content="https://a.example/")])

    def test_multiple_hosts_fail(self):
        import types
        msgs = [types.SimpleNamespace(
            id="m1", content="见 https://a.example/ 和 https://b.example/")]
        with pytest.raises(cr._BrowserCaseFail):
            cr.extract_expected_host(msgs)

    def test_no_url_fails(self):
        import types
        msgs = [types.SimpleNamespace(id="m1", content="没有链接")]
        with pytest.raises(cr._BrowserCaseFail):
            cr.extract_expected_host(msgs)


class TestSelectTargetPage:
    _host = "a6thn73a795g.meoo.info"

    def test_unique_match(self):
        pages = [
            {"url": "about:blank", "type": "page"},
            {"url": f"https://{self._host}/#/login", "type": "page"},
        ]
        assert cr.select_target_page_index(pages, self._host) == 1

    def test_zero_match_is_precondition_failed(self):
        pages = [{"url": "https://other.example/", "type": "page"}]
        with pytest.raises(cr._BrowserPreconditionFailed):
            cr.select_target_page_index(pages, self._host)

    def test_multiple_match_fails(self):
        pages = [
            {"url": f"https://{self._host}/a", "type": "page"},
            {"url": f"https://{self._host}/b", "type": "page"},
        ]
        with pytest.raises(cr._BrowserCaseFail):
            cr.select_target_page_index(pages, self._host)

    def test_host_case_insensitive(self):
        pages = [{"url": f"https://{self._host.upper()}/x", "type": "page"}]
        assert cr.select_target_page_index(pages, self._host) == 0

    def test_non_page_type_ignored(self):
        pages = [
            {"url": f"https://{self._host}/x", "type": "background_page"},
            {"url": f"https://{self._host}/y", "type": "page"},
        ]
        assert cr.select_target_page_index(pages, self._host) == 1


class TestCheckLoginForm:
    def test_one_visible_ok(self):
        cr.check_login_form(1)

    def test_zero_is_precondition_failed(self):
        with pytest.raises(cr._BrowserPreconditionFailed):
            cr.check_login_form(0)

    def test_multiple_is_case_fail_not_precondition(self):
        with pytest.raises(cr._BrowserCaseFail) as exc_info:
            cr.check_login_form(2)
        assert not isinstance(exc_info.value, cr._BrowserPreconditionFailed)


class TestClassifyBrowserCommandResult:
    def test_ok(self):
        assert cr.classify_browser_command_result(
            "takeover", 200, {"ok": True}) is None

    def test_200_without_ok_fails(self):
        with pytest.raises(cr._BrowserCaseFail):
            cr.classify_browser_command_result("takeover", 200, {"ok": False})

    def test_403_is_case_fail_with_code(self):
        with pytest.raises(cr._BrowserCaseFail) as exc_info:
            cr.classify_browser_command_result(
                "takeover", 403, {"error": {"code": "invalid_challenge"}})
        assert "invalid_challenge" in str(exc_info.value)

    def test_409_is_case_fail(self):
        with pytest.raises(cr._BrowserCaseFail):
            cr.classify_browser_command_result(
                "takeover", 409, {"error": {"code": "invalid_state_transition"}})

    def test_5xx_is_replay_error(self):
        with pytest.raises(cr.ReplayError):
            cr.classify_browser_command_result("takeover", 500, None)


class _FakeBrowserDriver:
    """DashboardDriver 协议假实现：list/detail 按序返回，post 记录证据。"""

    def __init__(self, list_response, details, post_results=None):
        self._list = list_response
        self._details = list(details)
        self._post_results = list(post_results or [])
        self.gets = []
        self.posts = []

    @property
    def origin(self):
        return "http://127.0.0.1:8201"

    def get(self, path, params=None, headers=None, timeout=None):
        self.gets.append({"path": path, "params": params, "headers": headers})
        if path == "/chat/browser/sessions":
            return self._list
        return self._details.pop(0)

    def post(self, path, params=None, headers=None, json_body=None, timeout=None):
        self.posts.append({"path": path, "params": params, "headers": headers})
        if self._post_results:
            return self._post_results.pop(0)
        return (200, {"ok": True})


class _StubTakeoverHelper(cr.BrowserTakeoverHelper):
    """打薄 CDP/DB 边界的测试替身（不触 playwright/sqlite/控制面）。"""

    def __init__(self, driver, fill_error=None, password="pw-fake-sentinel"):
        env = {"N_AGENT_E2E_BROWSER_PASSWORD": password}
        super().__init__(driver, env=env)
        self._fill_error = fill_error
        self.fill_calls = []
        self.ensure_calls = []

    def _query_registry_rows(self):
        return [dict(_BROWSER_ROW)]

    def _ensure_profile_endpoint(self, profile_ref):
        self.ensure_calls.append(profile_ref)
        return "http://172.19.0.10:20222"

    def _fill_password_via_cdp(self, cdp_endpoint, expected_host, password):
        if self._fill_error is not None:
            raise self._fill_error
        self.fill_calls.append((cdp_endpoint, expected_host, password))


def _browser_flow_fixture(post_results=None, fill_error=None):
    list_response = {"sessions": [
        {"id": "bsess-1", "n_agent_session_id": _BROWSER_SID}]}
    details = [
        {"status": "active",
         "write_challenges": {"takeover": "tok-t1", "close": "tok-c1"}},
        {"status": "takeover",
         "write_challenges": {"release": "tok-r1", "close": "tok-c2"}},
    ]
    driver = _FakeBrowserDriver(list_response, details,
                                post_results=post_results)
    helper = _StubTakeoverHelper(driver, fill_error=fill_error)
    return driver, helper


class TestBrowserTakeoverFlow:
    def test_success_challenge_freshness_and_release(self):
        driver, helper = _browser_flow_fixture()
        result = helper.type_password(_browser_case(), _BROWSER_SID)
        assert result["verdict"] == "PASS"
        assert result["action"] == "browser_type_password"
        assert result["browser_session_id"] == "bsess-1"
        # takeover -> release 顺序，各自携带对应 GET 的新 token（不重用）。
        assert [p["path"] for p in driver.posts] == [
            "/chat/browser/sessions/bsess-1/takeover",
            "/chat/browser/sessions/bsess-1/release",
        ]
        assert driver.posts[0]["headers"]["X-Browser-Challenge"] == "tok-t1"
        assert driver.posts[1]["headers"]["X-Browser-Challenge"] == "tok-r1"
        # 每次请求带合法 actor、同源 Origin 与 query n_agent_session_id。
        for req in driver.gets + driver.posts:
            assert req["headers"]["X-Dashboard-Actor"] == "e2e-browser-takeover"
            assert req["headers"]["Origin"] == "http://127.0.0.1:8201"
            assert req["params"]["n_agent_session_id"] == _BROWSER_SID
        # CDP 输密发生在 takeover 之后，使用证明过的端点与目标 host。
        assert helper.fill_calls == [
            ("http://172.19.0.10:20222", "a6thn73a795g.meoo.info",
             "pw-fake-sentinel")]
        assert helper.ensure_calls == ["bp-container-abcdef012345"]

    def test_takeover_rejected_no_release_attempted(self):
        driver, helper = _browser_flow_fixture(
            post_results=[(403, {"error": {"code": "invalid_challenge"}})])
        result = helper.type_password(_browser_case(), _BROWSER_SID)
        assert result["verdict"] == "FAIL"
        assert "invalid_challenge" in result["detail"]
        # takeover 未成功，不尝试 release。
        assert len(driver.posts) == 1
        assert helper.fill_calls == []

    def test_fill_failure_still_releases(self):
        driver, helper = _browser_flow_fixture(
            fill_error=cr._BrowserCaseFail("密码输入框不可填充"))
        result = helper.type_password(_browser_case(), _BROWSER_SID)
        assert result["verdict"] == "FAIL"
        assert "不可填充" in result["detail"]
        assert [p["path"] for p in driver.posts] == [
            "/chat/browser/sessions/bsess-1/takeover",
            "/chat/browser/sessions/bsess-1/release",
        ]

    def test_precondition_failed_marked(self):
        driver, helper = _browser_flow_fixture(
            fill_error=cr._BrowserPreconditionFailed("页面无可见密码输入框"))
        result = helper.type_password(_browser_case(), _BROWSER_SID)
        assert result["verdict"] == "FAIL"
        assert result["detail"].startswith("precondition_failed:")

    def test_release_failure_recorded_in_pass_detail(self):
        driver, helper = _browser_flow_fixture(
            post_results=[(200, {"ok": True}),
                          (403, {"error": {"code": "invalid_challenge"}})])
        result = helper.type_password(_browser_case(), _BROWSER_SID)
        assert result["verdict"] == "PASS"
        assert "release 失败" in result["detail"]

    def test_release_failure_recorded_in_fail_detail(self):
        # FAIL 路径同样附 release_note（与 PASS 路径一致）。
        driver, helper = _browser_flow_fixture(
            post_results=[(200, {"ok": True}),
                          (403, {"error": {"code": "invalid_challenge"}})],
            fill_error=cr._BrowserCaseFail("fill boom"))
        result = helper.type_password(_browser_case(), _BROWSER_SID)
        assert result["verdict"] == "FAIL"
        assert "fill boom" in result["detail"]
        assert "release 失败" in result["detail"]

    def test_replay_error_fill_still_releases(self):
        # 基础设施错误（ReplayError）原样透传，finally 仍尝试 release。
        driver, helper = _browser_flow_fixture(
            fill_error=cr.ReplayError("cdp 操作失败: boom"))
        with pytest.raises(cr.ReplayError):
            helper.type_password(_browser_case(), _BROWSER_SID)
        assert [p["path"] for p in driver.posts] == [
            "/chat/browser/sessions/bsess-1/takeover",
            "/chat/browser/sessions/bsess-1/release",
        ]

    def test_missing_password_env_fails_before_http(self):
        driver, helper = _browser_flow_fixture()
        helper._env = {}
        result = helper.type_password(_browser_case(), _BROWSER_SID)
        assert result["verdict"] == "FAIL"
        assert driver.gets == [] and driver.posts == []
        assert "pw-fake-sentinel" not in result["detail"]

    def test_replay_error_propagates(self):
        class BoomDriver(_FakeBrowserDriver):
            def get(self, path, params=None, headers=None, timeout=None):
                raise cr.ReplayError("dashboard get 网络错误: boom")
        driver = BoomDriver({"sessions": []}, [])
        helper = _StubTakeoverHelper(driver)
        with pytest.raises(cr.ReplayError):
            helper.type_password(_browser_case(), _BROWSER_SID)

    def test_password_never_in_result(self):
        driver, helper = _browser_flow_fixture(
            fill_error=cr._BrowserCaseFail("fill boom"))
        result = helper.type_password(_browser_case(), _BROWSER_SID)
        assert "pw-fake-sentinel" not in json.dumps(result, ensure_ascii=False)


class TestRedact:
    def test_password_scrubbed(self):
        out = cr.BrowserTakeoverHelper._redact(
            "locator.fill: value 'pw-fake-sentinel' cannot be filled",
            "pw-fake-sentinel")
        assert "pw-fake-sentinel" not in out
        assert "***" in out

    def test_no_match_unchanged(self):
        msg = "plain error"
        assert cr.BrowserTakeoverHelper._redact(msg, "pw-fake-sentinel") == msg

    def test_empty_or_none_password_unchanged_no_crash(self):
        msg = "message mentioning pw-fake-sentinel"
        assert cr.BrowserTakeoverHelper._redact(msg, "") == msg
        assert cr.BrowserTakeoverHelper._redact(msg, None) == msg


# ---------------------------------------------------------------------------
# 假 playwright.sync_api：驱动真实 _fill_password_via_cdp（宿主无 playwright）
# ---------------------------------------------------------------------------


class _FakePlaywrightError(Exception):
    pass


class _FakeCdpLocator:
    def __init__(self, page):
        self._page = page

    @property
    def first(self):
        return self

    def count(self):
        return self._page.visible_password_count

    def fill(self, value, timeout=None):
        self._page.fill_calls.append(value)
        if self._page.fill_error is not None:
            raise self._page.fill_error


class _FakeCdpPage:
    def __init__(self, url, *, visible_password_count=1, fill_error=None,
                 url_after_select=None):
        self._url = url
        self._url_after_select = url_after_select
        self._url_reads = 0
        self.visible_password_count = visible_password_count
        self.fill_error = fill_error
        self.fill_calls = []

    @property
    def url(self):
        # 页面选择（快照读取）之后可模拟跳转：第二次及以后读取返回变更 URL。
        self._url_reads += 1
        if self._url_after_select is not None and self._url_reads > 1:
            return self._url_after_select
        return self._url

    def wait_for_selector(self, selector, state=None, timeout=None):
        return None

    def locator(self, selector):
        return _FakeCdpLocator(self)


class _FakeCdpContext:
    def __init__(self, pages):
        self.pages = pages


class _FakeCdpBrowser:
    def __init__(self, pages):
        self.contexts = [_FakeCdpContext(pages)]


class _FakeChromium:
    def __init__(self, browser, connect_error=None):
        self._browser = browser
        self._connect_error = connect_error

    def connect_over_cdp(self, endpoint, timeout=None):
        if self._connect_error is not None:
            raise self._connect_error
        return self._browser


class _FakePwRuntime:
    def __init__(self, chromium):
        self.chromium = chromium
        self.stopped = False

    def stop(self):
        self.stopped = True


def _install_fake_playwright(monkeypatch, *, pages=None, connect_error=None):
    """注入假 playwright.sync_api，返回 runtime（可断言 stop 已调用）。"""
    import types
    browser = _FakeCdpBrowser(list(pages or []))
    runtime = _FakePwRuntime(_FakeChromium(browser, connect_error))
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.Error = _FakePlaywrightError
    sync_api.sync_playwright = lambda: types.SimpleNamespace(
        start=lambda: runtime)
    playwright_mod = types.ModuleType("playwright")
    playwright_mod.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", playwright_mod)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    return runtime


class _RealFillHelper(cr.BrowserTakeoverHelper):
    """保留真实 _fill_password_via_cdp，仅打薄 DB/控制面边界。"""

    def __init__(self, driver, password="pw-fake-sentinel"):
        super().__init__(
            driver, env={"N_AGENT_E2E_BROWSER_PASSWORD": password})

    def _query_registry_rows(self):
        return [dict(_BROWSER_ROW)]

    def _ensure_profile_endpoint(self, profile_ref):
        return "http://172.19.0.10:20222"


class TestFillPasswordViaCdp:
    _host = "a6thn73a795g.meoo.info"
    _url = f"https://{_host}/#/login"
    _sentinel = "pw-fake-sentinel"

    def _helper(self):
        driver = _FakeBrowserDriver({"sessions": []}, [])
        return _RealFillHelper(driver)

    def test_fill_error_redacted_and_chain_cut(self, monkeypatch):
        # fill 异常消息内嵌密码：消息脱敏且 from None 切断异常链。
        page = _FakeCdpPage(
            self._url,
            fill_error=_FakePlaywrightError(
                f"locator.fill: value '{self._sentinel}' cannot be filled"))
        runtime = _install_fake_playwright(monkeypatch, pages=[page])
        with pytest.raises(cr._BrowserCaseFail) as exc_info:
            self._helper()._fill_password_via_cdp(
                "http://172.19.0.10:20222", self._host, self._sentinel)
        exc = exc_info.value
        assert self._sentinel not in str(exc)
        assert exc.__cause__ is None
        assert exc.__suppress_context__
        assert self._sentinel not in "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__))
        assert runtime.stopped  # finally 断开本地 CDP 传输

    def test_connect_error_redacted_and_chain_cut(self, monkeypatch):
        # CDP 连接异常（基础设施）内嵌密码：ReplayError 同样脱敏断链。
        _install_fake_playwright(
            monkeypatch,
            connect_error=RuntimeError(
                f"cdp handshake failed: {self._sentinel}"))
        with pytest.raises(cr.ReplayError) as exc_info:
            self._helper()._fill_password_via_cdp(
                "http://172.19.0.10:20222", self._host, self._sentinel)
        exc = exc_info.value
        assert self._sentinel not in str(exc)
        assert exc.__cause__ is None
        assert exc.__suppress_context__
        assert self._sentinel not in "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__))

    def test_page_url_changed_before_fill_refused(self, monkeypatch):
        # 页面选择到填充之间 host 变更 -> 拒绝输密。
        page = _FakeCdpPage(
            self._url, url_after_select="https://other.example/login")
        _install_fake_playwright(monkeypatch, pages=[page])
        with pytest.raises(cr._BrowserCaseFail) as exc_info:
            self._helper()._fill_password_via_cdp(
                "http://172.19.0.10:20222", self._host, self._sentinel)
        assert "已变更" in str(exc_info.value)
        assert page.fill_calls == []

    def test_flow_verdict_contains_no_sentinel(self, monkeypatch):
        # 完整 type_password 流程：内嵌密码的 fill 异常不进入 verdict。
        page = _FakeCdpPage(
            self._url,
            fill_error=_FakePlaywrightError(
                f"locator.fill: value '{self._sentinel}' cannot be filled"))
        _install_fake_playwright(monkeypatch, pages=[page])
        list_response = {"sessions": [
            {"id": "bsess-1", "n_agent_session_id": _BROWSER_SID}]}
        details = [
            {"status": "active",
             "write_challenges": {"takeover": "tok-t1", "close": "tok-c1"}},
            {"status": "takeover",
             "write_challenges": {"release": "tok-r1", "close": "tok-c2"}},
        ]
        driver = _FakeBrowserDriver(list_response, details)
        helper = _RealFillHelper(driver)
        result = helper.type_password(_browser_case(), _BROWSER_SID)
        assert result["verdict"] == "FAIL"
        assert self._sentinel not in json.dumps(result, ensure_ascii=False)


class TestRegistryAndEnsureEdges:
    def test_relative_sqlite_path_is_replay_error(self):
        driver = _FakeBrowserDriver({"sessions": []}, [])
        helper = _StubTakeoverHelper(driver)
        helper._env["N_AGENT_SQLITE_PATH"] = "relative/path.db"
        # 调用真实实现（stub 覆盖了 _query_registry_rows）。
        with pytest.raises(cr.ReplayError):
            cr.BrowserTakeoverHelper._query_registry_rows(helper)

    def test_non_utf8_ensure_response_is_replay_error(self, monkeypatch):
        import urllib.request

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"\xff\xfe\xff"

        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
        driver = _FakeBrowserDriver({"sessions": []}, [])
        helper = _StubTakeoverHelper(driver)
        with pytest.raises(cr.ReplayError):
            cr.BrowserTakeoverHelper._ensure_profile_endpoint(
                helper, "bp-container-abcdef012345")


# =============================================================================
# T2 质量评审小修：未知键严格性 / ManifestError 语义 / _verify_png I/O
# =============================================================================


class TestUnknownKeyStrictness:
    def test_case_level_unknown_key_rejected(self):
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(image_assets=["assets/x.png"]))

    def test_message_level_unknown_key_rejected(self):
        msg = {"id": "m1", "content": "hi", "image_assets": ["assets/x.png"],
               "expect": {"rubric": "r", "tools": []}}
        with pytest.raises(cr.DatasetError):
            cr.validate_case(_case(messages=[msg]))

    def test_side_effect_unknown_keys_rejected(self):
        for se in (
            {"type": "task_terminal", "status": "succeeded", "timeout": 600,
             "cmd": "rm -rf /"},
            {"type": "workspace_file", "path": "a.txt", "expected_content": "x",
             "extra": 1},
            {"type": "artifact_registered", "count": 1,
             "source_context_ref": "task_id", "evil": True},
        ):
            with pytest.raises(cr.DatasetError):
                cr.validate_case(_case(side_effects=[se]))

    def test_all_known_keys_still_load(self):
        case = cr.validate_case(_case(side_effects=[
            {"type": "task_terminal", "status": "succeeded", "timeout": 600},
            {"type": "workspace_file", "path": "a.txt", "expected_content": "x"},
            {"type": "artifact_registered", "count": 1,
             "source_context_ref": "task_id", "kind": "markdown",
             "storage_ref_prefix": "workspace:"},
        ]))
        assert len(case.side_effects) == 3


class TestVerifyPngIo:
    def test_directory_asset_reports_clearly(self, tmp_path):
        asset = tmp_path / "dir.png"
        asset.mkdir()
        with pytest.raises(cr.DatasetError, match="目录"):
            cr._verify_png(asset, "c1", "m1")
        # 不再误报为“不存在”。
        with pytest.raises(cr.DatasetError) as exc_info:
            cr._verify_png(asset, "c1", "m1")
        assert "不存在" not in str(exc_info.value)

    def test_oserror_wrapped_as_dataset_error(self, tmp_path, monkeypatch):
        asset = tmp_path / "x.png"
        asset.write_bytes(_PNG_MAGIC + b"\x00" * 8)
        real_open = open

        def boom(path, *args, **kwargs):
            if str(path) == str(asset):
                raise OSError("disk gone")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", boom)
        with pytest.raises(cr.DatasetError, match="读取失败"):
            cr._verify_png(asset, "c1", "m1")


# =============================================================================
# 第十五节: 清单（manifest 校验 + ManifestRecorder）
# =============================================================================


class TestManifestValidation:
    def _manifest(self, **over):
        base = {"schema_version": 1, "project": "default",
                "run_id": "20260913-120000-abc123",
                "base_url": "http://127.0.0.1:8201",
                "environment": "fixture-service-default",
                "started_at": "2026-09-13T12:00:00",
                "resources": [], "exemptions": []}
        base.update(over)
        return base

    def test_valid(self):
        cr.validate_manifest(self._manifest())

    def test_valid_full_resources(self):
        m = self._manifest(
            resources=[
                {"type": "session", "id": "s1", "case_id": "c"},
                {"type": "task", "id": "t1", "case_id": "c",
                 "owner_session_id": "s1", "status": "active",
                 "verify": "get_404", "run_id": 3},
                {"type": "artifact", "id": "a1", "case_id": "c", "task_id": "t1"},
                {"type": "workspace_file", "path": "out.txt", "case_id": "c",
                 "sha256": "a" * 64},
                {"type": "browser_session", "id": "b1", "case_id": "c",
                 "owner_session_id": "s1"},
                {"type": "sandbox_history", "id": "tc1", "case_id": "c",
                 "owner_session_id": "s1"},
            ],
            exemptions=[{"type": "external", "detail": "mem0 外部记忆写入"},
                        {"type": "telemetry", "detail": "usage 计数无删除能力"}])
        cr.validate_manifest(m)

    def test_env_mismatch_rejected(self):
        with pytest.raises(cr.ManifestError):
            cr.validate_manifest(self._manifest(),
                                 expect_base_url="http://other:1")
        with pytest.raises(cr.ManifestError):
            cr.validate_manifest(self._manifest(),
                                 expect_environment="other-env")

    def test_matching_env_accepted(self):
        cr.validate_manifest(
            self._manifest(), expect_base_url="http://127.0.0.1:8201",
            expect_environment="fixture-service-default")

    def test_bad_version_rejected(self):
        for bad in (2, "1", 1.0, None, True):
            with pytest.raises(cr.ManifestError):
                cr.validate_manifest(self._manifest(schema_version=bad))

    def test_project_must_be_default(self):
        with pytest.raises(cr.ManifestError):
            cr.validate_manifest(self._manifest(project="other"))

    def test_top_level_unknown_key_rejected(self):
        with pytest.raises(cr.ManifestError):
            cr.validate_manifest(self._manifest(cleanup_command="rm -rf /"))

    def test_required_top_level_fields(self):
        for key in ("run_id", "base_url", "environment", "started_at"):
            with pytest.raises(cr.ManifestError):
                cr.validate_manifest(self._manifest(**{key: ""}))
            with pytest.raises(cr.ManifestError):
                cr.validate_manifest(self._manifest(**{key: None}))

    def test_base_url_must_be_http_url(self):
        for bad in ("not-a-url", "ftp://x", "/local/path"):
            with pytest.raises(cr.ManifestError):
                cr.validate_manifest(self._manifest(base_url=bad))

    def test_resources_must_be_list(self):
        for bad in (None, "x", {}):
            with pytest.raises(cr.ManifestError):
                cr.validate_manifest(self._manifest(resources=bad))

    def test_unknown_resource_type_rejected(self):
        m = self._manifest(resources=[{"type": "nuke", "id": "x", "case_id": "c"}])
        with pytest.raises(cr.ManifestError):
            cr.validate_manifest(m)

    def test_command_fields_rejected(self):
        for key in ("command", "cmd", "shell", "exec"):
            m = self._manifest(resources=[
                {"type": "session", "id": "s1", "case_id": "c", key: "rm -rf /"}])
            with pytest.raises(cr.ManifestError):
                cr.validate_manifest(m)

    def test_missing_ownership_fields_rejected(self):
        bad_resources = [
            {"type": "session", "id": "s1"},  # 缺 case_id
            {"type": "task", "id": "t1", "case_id": "c"},  # 缺 owner_session_id
            {"type": "artifact", "id": "a1", "case_id": "c"},  # 缺 task/session 归属
            {"type": "browser_session", "id": "b1", "case_id": "c"},
            {"type": "sandbox_history", "id": "tc1", "case_id": "c"},
            {"type": "workspace_file", "path": "a.txt"},  # 缺 case_id
        ]
        for res in bad_resources:
            with pytest.raises(cr.ManifestError):
                cr.validate_manifest(self._manifest(resources=[res]))

    def test_bad_status_rejected(self):
        m = self._manifest(resources=[
            {"type": "session", "id": "s1", "case_id": "c", "status": "gone"}])
        with pytest.raises(cr.ManifestError):
            cr.validate_manifest(m)

    def test_all_valid_statuses_accepted(self):
        for status in ("intent", "active", "deleted", "failed", "unresolved"):
            m = self._manifest(resources=[
                {"type": "session", "id": "s1", "case_id": "c", "status": status}])
            cr.validate_manifest(m)

    def test_verify_method_must_match_type(self):
        m = self._manifest(resources=[
            {"type": "session", "id": "s1", "case_id": "c", "verify": "get_404"}])
        with pytest.raises(cr.ManifestError):
            cr.validate_manifest(m)
        m = self._manifest(resources=[
            {"type": "session", "id": "s1", "case_id": "c",
             "verify": "detail_session_null"}])
        cr.validate_manifest(m)

    def test_workspace_file_outside_root_rejected(self):
        m = self._manifest(resources=[{"type": "workspace_file",
                                       "path": "../evil.txt", "case_id": "c"}])
        with pytest.raises(cr.ManifestError):
            cr.validate_manifest(m)

    def test_workspace_file_absolute_path_rejected(self):
        m = self._manifest(resources=[{"type": "workspace_file",
                                       "path": "/etc/passwd", "case_id": "c"}])
        with pytest.raises(cr.ManifestError):
            cr.validate_manifest(m)

    def test_workspace_file_bad_sha256_rejected(self):
        for bad in ("xyz", "a" * 63, 123):
            m = self._manifest(resources=[
                {"type": "workspace_file", "path": "a.txt", "case_id": "c",
                 "sha256": bad}])
            with pytest.raises(cr.ManifestError):
                cr.validate_manifest(m)

    def test_workspace_file_symlink_rejected_with_root(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        real = tmp_path / "real.txt"
        real.write_text("x", encoding="utf-8")
        (ws / "link.txt").symlink_to(real)
        m = self._manifest(resources=[{"type": "workspace_file",
                                       "path": "link.txt", "case_id": "c"}])
        with pytest.raises(cr.ManifestError):
            cr.validate_manifest(m, workspace_root=ws)

    def test_workspace_file_inside_root_accepted(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "ok.txt").write_text("x", encoding="utf-8")
        m = self._manifest(resources=[{"type": "workspace_file",
                                       "path": "ok.txt", "case_id": "c"}])
        cr.validate_manifest(m, workspace_root=ws)

    def test_exemptions_type_checked(self):
        for bad in (None, "x", {}):
            with pytest.raises(cr.ManifestError):
                cr.validate_manifest(self._manifest(exemptions=bad))
        for bad_entry in (
            {"type": "unknown", "detail": "x"},
            {"type": "external"},  # 缺 detail
            {"detail": "x"},  # 缺 type
            {"type": "external", "detail": "x", "command": "rm"},
            "not-a-dict",
        ):
            with pytest.raises(cr.ManifestError):
                cr.validate_manifest(self._manifest(exemptions=[bad_entry]))

    def test_intent_status_idless_with_intent_key_accepted(self):
        # 崩溃中断时 intent()/unresolved 持久化的条目只有 intent_key 无 id。
        m = self._manifest(resources=[
            {"type": "session", "case_id": "c", "status": "intent",
             "intent_key": "intent-0001"},
            {"type": "task", "case_id": "c", "owner_session_id": "s1",
             "status": "unresolved", "intent_key": "intent-0002"},
        ])
        cr.validate_manifest(m)

    def test_intent_status_idless_without_intent_key_rejected(self):
        for status in ("intent", "unresolved"):
            m = self._manifest(resources=[
                {"type": "session", "case_id": "c", "status": status}])
            with pytest.raises(cr.ManifestError):
                cr.validate_manifest(m)
        # 其他状态下无 id 依然拒绝。
        m = self._manifest(resources=[
            {"type": "session", "case_id": "c", "status": "active"}])
        with pytest.raises(cr.ManifestError):
            cr.validate_manifest(m)

    @pytest.mark.parametrize("bad_id", (
        "../sessions/victim", "a/b", "a\\b", "a..b", "..",
        "a b", "a\tb", "a\nb", "a\x00b"))
    def test_id_path_traversal_chars_rejected(self, bad_id):
        m = self._manifest(resources=[
            {"type": "session", "id": bad_id, "case_id": "c"}])
        with pytest.raises(cr.ManifestError):
            cr.validate_manifest(m)

    def test_ownership_and_intent_key_traversal_rejected(self):
        bad_resources = [
            {"type": "task", "id": "t1", "case_id": "c",
             "owner_session_id": "../victim"},
            {"type": "artifact", "id": "a1", "case_id": "c", "task_id": "a/b"},
            {"type": "browser_session", "id": "b1", "case_id": "c",
             "owner_session_id": "s1", "session_id": "a b"},
            {"type": "session", "case_id": "c", "status": "intent",
             "intent_key": "../evil"},
        ]
        for res in bad_resources:
            with pytest.raises(cr.ManifestError):
                cr.validate_manifest(self._manifest(resources=[res]))


class TestManifestRecorder:
    def _recorder(self, tmp_path, **kw):
        kw.setdefault("run_id", "20260913-120000-abc123")
        kw.setdefault("base_url", "http://127.0.0.1:8201")
        kw.setdefault("environment", "fixture-service-default")
        return cr.ManifestRecorder(tmp_path / "manifest.json", **kw)

    def _load(self, tmp_path):
        return json.loads(
            (tmp_path / "manifest.json").read_text(encoding="utf-8"))

    def test_initial_manifest_persisted(self, tmp_path):
        self._recorder(tmp_path)
        m = self._load(tmp_path)
        assert m["schema_version"] == 1
        assert m["project"] == "default"
        assert m["run_id"] == "20260913-120000-abc123"
        assert m["environment"] == "fixture-service-default"
        assert m["resources"] == [] and m["exemptions"] == []
        cr.validate_manifest(m)

    def test_intent_assigns_stable_key_and_persists(self, tmp_path):
        rec = self._recorder(tmp_path)
        stored = rec.intent({"type": "task", "case_id": "c1",
                             "owner_session_id": "s1"})
        assert stored["intent_key"]
        assert stored["status"] == "intent"
        m = self._load(tmp_path)
        assert len(m["resources"]) == 1
        assert m["resources"][0]["intent_key"] == stored["intent_key"]

    def test_upsert_merges_by_type_and_id_no_duplicates(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.upsert({"type": "artifact", "id": "a1", "case_id": "c1",
                    "task_id": "t1"})
        rec.upsert({"type": "artifact", "id": "a1", "case_id": "c1",
                    "task_id": "t1", "status": "active"})
        m = self._load(tmp_path)
        assert len(m["resources"]) == 1
        assert m["resources"][0]["status"] == "active"

    def test_workspace_file_merges_by_path(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.upsert({"type": "workspace_file", "path": "out.txt", "case_id": "c1"})
        rec.upsert({"type": "workspace_file", "path": "out.txt", "case_id": "c1",
                    "sha256": "b" * 64})
        m = self._load(tmp_path)
        assert len(m["resources"]) == 1
        assert m["resources"][0]["sha256"] == "b" * 64

    def test_id_acquisition_rewrite_preserves_ownership(self, tmp_path):
        rec = self._recorder(tmp_path)
        stored = rec.intent({"type": "task", "case_id": "c1",
                             "owner_session_id": "s1", "verify": "get_404",
                             "run_id": 7})
        rec.upsert({"type": "task", "id": "t_1",
                    "intent_key": stored["intent_key"]})
        m = self._load(tmp_path)
        assert len(m["resources"]) == 1
        r = m["resources"][0]
        assert r["id"] == "t_1"
        assert r["case_id"] == "c1"
        assert r["owner_session_id"] == "s1"
        assert r["verify"] == "get_404"
        assert r["run_id"] == 7

    def test_upsert_does_not_clobber_ownership_with_none(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.upsert({"type": "task", "id": "t1", "case_id": "c1",
                    "owner_session_id": "s1"})
        rec.upsert({"type": "task", "id": "t1", "status": "deleted"})
        m = self._load(tmp_path)
        r = m["resources"][0]
        assert r["owner_session_id"] == "s1" and r["case_id"] == "c1"
        assert r["status"] == "deleted"

    def test_mark_status_transitions_persisted(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.upsert({"type": "session", "id": "s1", "case_id": "c1"})
        rec.mark_status({"type": "session", "id": "s1"}, "deleted")
        m = self._load(tmp_path)
        assert m["resources"][0]["status"] == "deleted"

    def test_mark_status_rejects_bad_status(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.upsert({"type": "session", "id": "s1", "case_id": "c1"})
        with pytest.raises(cr.ManifestError):
            rec.mark_status({"type": "session", "id": "s1"}, "gone")

    def test_mark_status_unknown_resource_rejected(self, tmp_path):
        rec = self._recorder(tmp_path)
        with pytest.raises(cr.ManifestError):
            rec.mark_status({"type": "session", "id": "ghost"}, "deleted")

    def test_mark_unresolved_with_detail(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.upsert({"type": "artifact", "id": "a1", "case_id": "c1",
                    "task_id": "t1"})
        rec.mark_status({"type": "artifact", "id": "a1"}, "unresolved",
                        detail="关联任务归属不唯一")
        m = self._load(tmp_path)
        r = m["resources"][0]
        assert r["status"] == "unresolved"
        assert "归属不唯一" in r["status_detail"]
        cr.validate_manifest(m)

    def test_exempt_dedup_and_persisted(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.exempt("external", "mem0 外部记忆写入")
        rec.exempt("external", "mem0 外部记忆写入")
        rec.exempt("telemetry", "usage 计数")
        m = self._load(tmp_path)
        assert m["exemptions"] == [
            {"type": "external", "detail": "mem0 外部记忆写入"},
            {"type": "telemetry", "detail": "usage 计数"}]

    def test_exempt_unknown_type_rejected(self, tmp_path):
        rec = self._recorder(tmp_path)
        with pytest.raises(cr.ManifestError):
            rec.exempt("filesystem", "x")

    def test_secrets_redacted_before_persist(self, tmp_path):
        rec = self._recorder(tmp_path, secrets=["s3cr3t-value"])
        rec.upsert({"type": "session", "id": "s1", "case_id": "c1",
                    "status_detail": "seen s3cr3t-value here"})
        raw = (tmp_path / "manifest.json").read_text(encoding="utf-8")
        assert "s3cr3t-value" not in raw

    def test_snapshot_is_deep_copy_and_valid(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.upsert({"type": "session", "id": "s1", "case_id": "c1"})
        snap = rec.snapshot()
        cr.validate_manifest(snap)
        snap["resources"].append({"type": "session", "id": "s2", "case_id": "c"})
        assert len(self._load(tmp_path)["resources"]) == 1

    def test_secrets_scrubbed_in_nested_dict_keys(self, tmp_path):
        rec = self._recorder(tmp_path, secrets=["s3cr3t-key"])
        rec.upsert({"type": "session", "id": "s1", "case_id": "c1",
                    "status_detail": {"outer": {"s3cr3t-key": "value"}}})
        raw = (tmp_path / "manifest.json").read_text(encoding="utf-8")
        assert "s3cr3t-key" not in raw

    def test_existing_nonempty_manifest_refused_without_resume(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.upsert({"type": "session", "id": "s1", "case_id": "c1"})
        with pytest.raises(cr.ManifestError):
            self._recorder(tmp_path)
        m = self._load(tmp_path)
        assert len(m["resources"]) == 1  # 崩溃恢复状态未被截断

    def test_resume_loads_and_validates_existing(self, tmp_path):
        rec = self._recorder(tmp_path)
        stored = rec.intent({"type": "task", "case_id": "c1",
                             "owner_session_id": "s1"})
        rec2 = self._recorder(tmp_path, resume=True)
        snap = rec2.snapshot()
        assert len(snap["resources"]) == 1
        assert snap["resources"][0]["intent_key"] == stored["intent_key"]
        # 接管后继续追加不丢失既有资源，intent_key 序号不回退。
        stored2 = rec2.intent({"type": "task", "case_id": "c1",
                               "owner_session_id": "s1"})
        assert stored2["intent_key"] != stored["intent_key"]
        m = self._load(tmp_path)
        assert len(m["resources"]) == 2

    def test_resume_environment_mismatch_rejected(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.upsert({"type": "session", "id": "s1", "case_id": "c1"})
        with pytest.raises(cr.ManifestError):
            self._recorder(tmp_path, resume=True, environment="other-env")

    def test_empty_existing_manifest_overwrite_ok(self, tmp_path):
        self._recorder(tmp_path)  # 空清单（无 resources/exemptions）
        rec = self._recorder(tmp_path)  # 非崩溃恢复状态，允许重建
        assert rec.snapshot()["resources"] == []


# =============================================================================
# 第十六节: 清理（Cleaner）
# =============================================================================


class _FakeCleanupDriver:
    """DashboardDriver 协议 fake：get/get_raw/post/delete_raw 按 path 预置响应。"""

    def __init__(self):
        self.calls = []
        self.get_map = {}
        self.get_raw_map = {}
        self.post_map = {}
        self.delete_map = {}

    def _next(self, mapping, path, default):
        value = mapping.get(path, default)
        if isinstance(value, list):
            if not value:
                # 预置空列表：该端点恒返回空列表响应（如历史回读为空）。
                return [] if path in mapping else default
            popped = value.pop(0)
            if not value:
                value.append(popped)  # 粘性：队列耗尽后重复最后一个响应
            return popped
        return value

    def get(self, path, params=None, headers=None, timeout=None):
        self.calls.append(("GET", path, params))
        value = self._next(self.get_map, path, None)
        if isinstance(value, Exception):
            raise value
        return value

    def get_raw(self, path, params=None, headers=None, timeout=None):
        self.calls.append(("GET", path, params))
        value = self._next(self.get_raw_map, path, (404, None))
        if isinstance(value, Exception):
            raise value
        return value

    def post(self, path, params=None, headers=None, json_body=None,
             timeout=None):
        self.calls.append(("POST", path, params))
        value = self._next(self.post_map, path, (200, {"ok": True}))
        if isinstance(value, Exception):
            raise value
        return value

    def delete_raw(self, path, params=None, headers=None, timeout=None):
        self.calls.append(("DELETE", path, params))
        value = self._next(self.delete_map, path, (204, None))
        if isinstance(value, Exception):
            raise value
        return value

    def paths(self, method):
        return [p for m, p, _ in self.calls if m == method]


class _FakeCleanupCtx:
    def __init__(self, driver, workspace_root=None, browser_helper=None,
                 recorder=None):
        self.driver = driver
        self.workspace_root = workspace_root
        self.browser_helper = browser_helper
        self.recorder = recorder


class _FakeBrowserCloser:
    """browser_helper 协议 fake：close 记录调用，status 按序返回。"""

    def __init__(self, statuses=(), close_error=None):
        self.closed = []
        self._statuses = list(statuses)
        self._close_error = close_error

    def close_browser_session(self, browser_session_id, n_agent_session_id):
        self.closed.append((browser_session_id, n_agent_session_id))
        if self._close_error is not None:
            raise self._close_error

    def get_browser_session_status(self, browser_session_id,
                                   n_agent_session_id):
        if self._statuses:
            return self._statuses.pop(0)
        return "closed"


def _cleaner(**kw):
    kw.setdefault("sleep", lambda s: None)
    return cr.Cleaner(**kw)


class TestCleanup:
    def test_artifact_delete_verified_by_404(self):
        driver = _FakeCleanupDriver()
        driver.get_raw_map["/chat/artifacts/a1"] = (404, None)
        res = [{"type": "artifact", "id": "a1", "case_id": "c", "task_id": "t1"}]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver))
        assert [r["id"] for r in out["deleted"]] == ["a1"]
        assert out["failed"] == []
        assert driver.paths("DELETE") == ["/chat/artifacts/a1"]

    def test_already_deleted_reclean_is_success(self):
        driver = _FakeCleanupDriver()
        driver.delete_map["/chat/artifacts/a1"] = (404, None)
        driver.get_raw_map["/chat/artifacts/a1"] = (404, None)
        res = [{"type": "artifact", "id": "a1", "case_id": "c", "task_id": "t1"}]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver))
        assert [r["id"] for r in out["deleted"]] == ["a1"]
        assert out["failed"] == []

    @pytest.mark.parametrize("status_code", (403, 500, 503))
    def test_delete_error_status_is_failed_not_deleted(self, status_code):
        driver = _FakeCleanupDriver()
        driver.delete_map["/chat/artifacts/a1"] = (status_code, None)
        driver.get_raw_map["/chat/artifacts/a1"] = (200, {"id": "a1"})
        res = [{"type": "artifact", "id": "a1", "case_id": "c", "task_id": "t1"}]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver))
        assert out["deleted"] == []
        assert len(out["failed"]) == 1
        assert out["failed"][0]["resource"]["id"] == "a1"

    def test_replay_error_marks_failed_and_continues(self):
        driver = _FakeCleanupDriver()
        driver.delete_map["/chat/artifacts/a1"] = cr.ReplayError("timeout")
        res = [{"type": "artifact", "id": "a1", "case_id": "c", "task_id": "t1"},
               {"type": "artifact", "id": "a2", "case_id": "c", "task_id": "t1"}]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver))
        assert [r["id"] for r in out["deleted"]] == ["a2"]
        assert [f["resource"]["id"] for f in out["failed"]] == ["a1"]

    def test_verify_still_present_is_failed(self):
        driver = _FakeCleanupDriver()
        driver.get_raw_map["/chat/artifacts/a1"] = (200, {"id": "a1"})
        res = [{"type": "artifact", "id": "a1", "case_id": "c", "task_id": "t1"}]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver))
        assert out["deleted"] == []
        assert len(out["failed"]) == 1

    def test_session_delete_verifies_session_null(self):
        driver = _FakeCleanupDriver()
        driver.get_map["/chat/sandbox/execute-code-history"] = []
        driver.get_raw_map["/chat/sessions/s1"] = (200, {"session": None})
        res = [{"type": "session", "id": "s1", "case_id": "c"}]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver))
        assert [r["id"] for r in out["deleted"]] == ["s1"]
        assert driver.paths("DELETE") == ["/chat/sessions/s1"]

    def test_session_still_present_is_failed(self):
        driver = _FakeCleanupDriver()
        driver.get_map["/chat/sandbox/execute-code-history"] = []
        driver.get_raw_map["/chat/sessions/s1"] = (200, {"session": {"id": "s1"}})
        res = [{"type": "session", "id": "s1", "case_id": "c"}]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver))
        assert out["deleted"] == []
        assert len(out["failed"]) == 1

    def test_unresolved_resource_failed_without_calls(self):
        driver = _FakeCleanupDriver()
        res = [{"type": "artifact", "id": "a1", "case_id": "c",
                "task_id": "t1", "status": "unresolved"}]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver))
        assert out["deleted"] == []
        assert len(out["failed"]) == 1
        assert driver.calls == []

    def test_task_cancel_before_any_delete_ordering(self):
        driver = _FakeCleanupDriver()
        # 回读顺序：backfill 详情(running) -> cancel 前轮询(running)
        # -> cancel 后静止(cancelled) -> 删除后验证(404)。
        driver.get_raw_map["/chat/tasks/t1"] = [
            (200, {"task": {"status": "running"}}),
            (200, {"task": {"status": "running"}}),
            (200, {"task": {"status": "cancelled"}}),
            (404, None),
        ]
        driver.get_map["/chat/artifacts"] = {
            "items": [{"id": "a1"}], "next_cursor": None}
        driver.get_raw_map["/chat/artifacts/a1"] = (404, None)
        res = [{"type": "task", "id": "t1", "case_id": "c",
                "owner_session_id": "s1"},
               {"type": "artifact", "id": "a1", "case_id": "c", "task_id": "t1"}]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver))
        assert out["failed"] == []
        assert {r.get("id") for r in out["deleted"]} == {"t1", "a1"}
        cancel_idx = driver.calls.index(("POST", "/chat/tasks/t1/cancel", None))
        artifact_del_idx = driver.calls.index(
            ("DELETE", "/chat/artifacts/a1", None))
        task_del_idx = driver.calls.index(("DELETE", "/chat/tasks/t1", None))
        # 任务取消必须早于任何删除；制品删除早于任务删除（CLEANUP_ORDER）。
        assert cancel_idx < artifact_del_idx < task_del_idx

    def test_worker_not_quiescent_keeps_owned_resources(self):
        driver = _FakeCleanupDriver()
        driver.get_raw_map["/chat/tasks/t1"] = (200, {"task": {"status": "running"}})
        driver.get_map["/chat/artifacts"] = {
            "items": [], "next_cursor": None}
        driver.get_map["/chat/sandbox/execute-code-history"] = []
        driver.get_raw_map["/chat/sessions/s1"] = (200, {"session": None})
        res = [
            {"type": "task", "id": "t1", "case_id": "c", "owner_session_id": "s1"},
            {"type": "artifact", "id": "a1", "case_id": "c", "task_id": "t1"},
            {"type": "session", "id": "s1", "case_id": "c"},
        ]
        out = _cleaner(quiesce_timeout=0).clean(res, _FakeCleanupCtx(driver))
        # 不静止：任务与其制品保留并 failed；会话仍被删除。
        failed_ids = {f["resource"].get("id") for f in out["failed"]}
        assert failed_ids == {"t1", "a1"}
        assert [r["id"] for r in out["deleted"]] == ["s1"]
        assert "/chat/tasks/t1" not in driver.paths("DELETE")
        assert "/chat/artifacts/a1" not in driver.paths("DELETE")

    def test_terminal_task_not_recancelled(self):
        driver = _FakeCleanupDriver()
        driver.get_raw_map["/chat/tasks/t1"] = [
            (200, {"task": {"status": "succeeded"}}),
            (404, None),
        ]
        driver.get_map["/chat/artifacts"] = {"items": [], "next_cursor": None}
        res = [{"type": "task", "id": "t1", "case_id": "c",
                "owner_session_id": "s1"}]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver))
        assert out["failed"] == []
        assert ("POST", "/chat/tasks/t1/cancel", None) not in driver.calls
        assert driver.paths("DELETE") == ["/chat/tasks/t1"]

    def test_expired_task_treated_terminal_not_recancelled(self):
        # 服务 stale 回收终态为 expired（7 态机），清理不得再取消或误判不静止。
        driver = _FakeCleanupDriver()
        driver.get_raw_map["/chat/tasks/t1"] = [
            (200, {"task": {"status": "expired"}}),
            (404, None),
        ]
        driver.get_map["/chat/artifacts"] = {"items": [], "next_cursor": None}
        res = [{"type": "task", "id": "t1", "case_id": "c",
                "owner_session_id": "s1"}]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver))
        assert out["failed"] == []
        assert ("POST", "/chat/tasks/t1/cancel", None) not in driver.calls
        assert driver.paths("DELETE") == ["/chat/tasks/t1"]

    def test_workspace_file_sha_mismatch_failed_and_untouched(self, tmp_path):
        target = tmp_path / "out.txt"
        target.write_text("tampered", encoding="utf-8")
        res = [{"type": "workspace_file", "path": "out.txt", "case_id": "c",
                "sha256": "0" * 64}]
        driver = _FakeCleanupDriver()
        out = _cleaner().clean(res, _FakeCleanupCtx(driver, workspace_root=tmp_path))
        assert out["deleted"] == []
        assert len(out["failed"]) == 1
        assert target.read_text(encoding="utf-8") == "tampered"

    def test_workspace_file_delete_success(self, tmp_path):
        target = tmp_path / "out.txt"
        target.write_text("artifact a", encoding="utf-8")
        res = [{"type": "workspace_file", "path": "out.txt", "case_id": "c",
                "sha256": cr.sha256_file(target)}]
        out = _cleaner().clean(
            res, _FakeCleanupCtx(_FakeCleanupDriver(), workspace_root=tmp_path))
        assert [r["path"] for r in out["deleted"]] == ["out.txt"]
        assert not target.exists()

    def test_workspace_file_already_absent_success(self, tmp_path):
        res = [{"type": "workspace_file", "path": "gone.txt", "case_id": "c",
                "sha256": "0" * 64}]
        out = _cleaner().clean(
            res, _FakeCleanupCtx(_FakeCleanupDriver(), workspace_root=tmp_path))
        assert [r["path"] for r in out["deleted"]] == ["gone.txt"]

    def test_workspace_file_symlink_refused(self, tmp_path):
        real = tmp_path / "real.txt"
        real.write_text("x", encoding="utf-8")
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        res = [{"type": "workspace_file", "path": "link.txt", "case_id": "c",
                "sha256": cr.sha256_file(real)}]
        out = _cleaner().clean(
            res, _FakeCleanupCtx(_FakeCleanupDriver(), workspace_root=tmp_path))
        assert len(out["failed"]) == 1
        assert link.exists() and real.exists()

    def test_browser_close_then_verify_closed(self):
        driver = _FakeCleanupDriver()
        helper = _FakeBrowserCloser(statuses=["closed"])
        res = [{"type": "browser_session", "id": "b1", "case_id": "c",
                "owner_session_id": "s1"}]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver, browser_helper=helper))
        assert helper.closed == [("b1", "s1")]
        assert [r["id"] for r in out["deleted"]] == ["b1"]

    def test_browser_already_gone_is_success(self):
        helper = _FakeBrowserCloser(statuses=[None])
        res = [{"type": "browser_session", "id": "b1", "case_id": "c",
                "owner_session_id": "s1"}]
        out = _cleaner().clean(
            res, _FakeCleanupCtx(_FakeCleanupDriver(), browser_helper=helper))
        assert [r["id"] for r in out["deleted"]] == ["b1"]

    def test_browser_still_active_is_failed(self):
        helper = _FakeBrowserCloser(statuses=["active"])
        res = [{"type": "browser_session", "id": "b1", "case_id": "c",
                "owner_session_id": "s1"}]
        out = _cleaner().clean(
            res, _FakeCleanupCtx(_FakeCleanupDriver(), browser_helper=helper))
        assert out["deleted"] == []
        assert len(out["failed"]) == 1

    def test_browser_close_error_still_verified(self):
        # close 被拒（如已 closed 的 invalid_state_transition）后回读 closed 仍算成功。
        helper = _FakeBrowserCloser(
            statuses=["closed"], close_error=cr._BrowserCaseFail("invalid_state"))
        res = [{"type": "browser_session", "id": "b1", "case_id": "c",
                "owner_session_id": "s1"}]
        out = _cleaner().clean(
            res, _FakeCleanupCtx(_FakeCleanupDriver(), browser_helper=helper))
        assert [r["id"] for r in out["deleted"]] == ["b1"]

    def test_sandbox_history_release_backfill_delete_confirm(self):
        driver = _FakeCleanupDriver()
        # release 后回读发现新行 tc2（释放产生），逐条删除后再确认列表为空。
        driver.get_map["/chat/sandbox/execute-code-history"] = [
            [{"id": "tc1"}, {"id": "tc2"}],  # release 后补齐
            [],  # 删除后确认
        ]
        res = [{"type": "sandbox_history", "id": "tc1", "case_id": "c",
                "owner_session_id": "s1"}]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver))
        assert {r["id"] for r in out["deleted"]} == {"tc1", "tc2"}
        release_idx = driver.calls.index(
            ("POST", "/chat/sandbox/active/s1/release", None))
        del1 = driver.calls.index(
            ("DELETE", "/chat/sandbox/execute-code-history/tc1", None))
        del2 = driver.calls.index(
            ("DELETE", "/chat/sandbox/execute-code-history/tc2", None))
        assert release_idx < del1 and release_idx < del2

    def test_sandbox_history_still_listed_is_failed(self):
        driver = _FakeCleanupDriver()
        driver.get_map["/chat/sandbox/execute-code-history"] = [
            [{"id": "tc1"}], [{"id": "tc1"}]]
        res = [{"type": "sandbox_history", "id": "tc1", "case_id": "c",
                "owner_session_id": "s1"}]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver))
        assert out["deleted"] == []
        assert [f["resource"]["id"] for f in out["failed"]] == ["tc1"]

    def test_cleanup_order_across_types(self):
        driver = _FakeCleanupDriver()
        driver.get_map["/chat/sandbox/execute-code-history"] = []
        driver.get_raw_map["/chat/artifacts/a1"] = (404, None)
        driver.get_raw_map["/chat/sessions/s1"] = (200, {"session": None})
        helper = _FakeBrowserCloser(statuses=["closed"])
        res = [
            {"type": "session", "id": "s1", "case_id": "c"},
            {"type": "artifact", "id": "a1", "case_id": "c", "task_id": "t1"},
            {"type": "browser_session", "id": "b1", "case_id": "c",
             "owner_session_id": "s1"},
        ]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver, browser_helper=helper))
        assert out["failed"] == []
        deletes = [p for p in driver.paths("DELETE")]
        assert deletes == ["/chat/artifacts/a1", "/chat/sessions/s1"]

    def test_recorder_progress_persisted(self, tmp_path):
        rec = cr.ManifestRecorder(
            tmp_path / "manifest.json", run_id="r1",
            base_url="http://127.0.0.1:8201", environment="env")
        rec.upsert({"type": "artifact", "id": "a1", "case_id": "c",
                    "task_id": "t1"})
        rec.upsert({"type": "artifact", "id": "a2", "case_id": "c",
                    "task_id": "t1"})
        driver = _FakeCleanupDriver()
        driver.get_raw_map["/chat/artifacts/a1"] = (404, None)
        driver.delete_map["/chat/artifacts/a2"] = (500, None)
        driver.get_raw_map["/chat/artifacts/a2"] = (200, {"id": "a2"})
        res = [dict(r) for r in rec.snapshot()["resources"]]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver, recorder=rec))
        assert [r["id"] for r in out["deleted"]] == ["a1"]
        m = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        by_id = {r["id"]: r for r in m["resources"]}
        assert by_id["a1"]["status"] == "deleted"
        assert by_id["a2"]["status"] == "failed"

    def test_intent_only_resources_failed_without_http(self):
        # cleanup-only：崩溃中断清单中无 id 的 intent 资源干净失败
        # （不发起 HTTP），其余资源正常删除。
        driver = _FakeCleanupDriver()
        m = {"schema_version": 1, "project": "default", "run_id": "r1",
             "base_url": "http://127.0.0.1:8201", "environment": "env",
             "started_at": "2026-09-13T12:00:00",
             "resources": [
                 {"type": "session", "case_id": "c", "status": "intent",
                  "intent_key": "intent-0001"},
                 {"type": "artifact", "id": "a1", "case_id": "c",
                  "task_id": "t1"},
             ],
             "exemptions": []}
        validated = cr.validate_manifest(m)
        driver.get_raw_map["/chat/artifacts/a1"] = (404, None)
        out = _cleaner().clean(validated["resources"], _FakeCleanupCtx(driver))
        assert [r["id"] for r in out["deleted"]] == ["a1"]
        assert len(out["failed"]) == 1
        assert out["failed"][0]["resource"]["intent_key"] == "intent-0001"
        assert driver.paths("DELETE") == ["/chat/artifacts/a1"]
        assert all("sessions" not in p for _, p, _ in driver.calls)

    def test_sandbox_history_backfill_error_blocks_session_branch(self):
        # 历史回读 ReplayError：会话及其历史行整体保留，零 DELETE。
        driver = _FakeCleanupDriver()
        driver.get_map["/chat/sandbox/execute-code-history"] = \
            cr.ReplayError("boom")
        res = [
            {"type": "session", "id": "s1", "case_id": "c"},
            {"type": "sandbox_history", "id": "tc1", "case_id": "c",
             "owner_session_id": "s1"},
        ]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver))
        assert out["deleted"] == []
        failed_ids = {f["resource"].get("id") for f in out["failed"]}
        assert failed_ids == {"s1", "tc1"}
        assert driver.paths("DELETE") == []

    def test_worker_not_quiescent_keeps_same_case_workspace_file(self, tmp_path):
        # worker 不静止：同用例 workspace 文件随任务分支整体保留。
        target = tmp_path / "out.txt"
        target.write_text("artifact a", encoding="utf-8")
        driver = _FakeCleanupDriver()
        driver.get_raw_map["/chat/tasks/t1"] = (200, {"task": {"status": "running"}})
        driver.get_map["/chat/artifacts"] = {"items": [], "next_cursor": None}
        res = [
            {"type": "task", "id": "t1", "case_id": "c", "owner_session_id": "s1"},
            {"type": "workspace_file", "path": "out.txt", "case_id": "c",
             "sha256": cr.sha256_file(target)},
        ]
        out = _cleaner(quiesce_timeout=0).clean(
            res, _FakeCleanupCtx(driver, workspace_root=tmp_path))
        failed = {(f["resource"]["type"],
                   f["resource"].get("id") or f["resource"].get("path"))
                  for f in out["failed"]}
        assert failed == {("task", "t1"), ("workspace_file", "out.txt")}
        assert out["deleted"] == []
        assert target.exists()

    def test_task_artifact_late_write_rescanned_after_quiesce(self):
        # 静止确认后二次回读：首次 backfill 与静止窗口之间晚写入的制品
        # 仍被纳入清单并删除。
        driver = _FakeCleanupDriver()
        # backfill 详情(running) -> cancel 前轮询(running) -> 静止(cancelled)
        # -> rescan 详情(cancelled) -> 删除后回读(404)。
        driver.get_raw_map["/chat/tasks/t1"] = [
            (200, {"task": {"status": "running"}}),
            (200, {"task": {"status": "running"}}),
            (200, {"task": {"status": "cancelled"}}),
            (200, {"task": {"status": "cancelled"}}),
            (404, None),
        ]
        driver.get_map["/chat/artifacts"] = [
            {"items": [{"id": "a1"}], "next_cursor": None},  # 首次 backfill
            {"items": [{"id": "a1"}, {"id": "a2"}],  # 静止后二次扫描发现 a2
             "next_cursor": None},
        ]
        driver.get_raw_map["/chat/artifacts/a1"] = (404, None)
        driver.get_raw_map["/chat/artifacts/a2"] = (404, None)
        res = [{"type": "task", "id": "t1", "case_id": "c",
                "owner_session_id": "s1"}]
        out = _cleaner().clean(res, _FakeCleanupCtx(driver))
        assert out["failed"] == []
        assert {r.get("id") for r in out["deleted"]} == {"t1", "a1", "a2"}
        assert "/chat/artifacts/a2" in driver.paths("DELETE")


# =========================================================================
# TestOrchestration: 编排入口（fake driver/judge/cleaner 全路径覆盖）
# =========================================================================


class _FakeOrchDriver:
    """编排 fake dashboard driver：脚本化 send/tool_calls/post + 调用日志。"""

    def __init__(self, *, send_outcomes=None, tool_batches=None,
                 post_responses=None, session_detail=None):
        self.calls = []
        self._send_outcomes = list(send_outcomes or [])
        self._tool_batches = list(tool_batches or [])
        self._post_responses = dict(post_responses or {})
        self._session_detail = session_detail or {"session": {}}

    def create_session(self, session_id):
        self.calls.append(("create_session", session_id))

    def send(self, session_id, content, image_data_url=None, options=None):
        self.calls.append(("send", session_id, content))
        outcome = self._send_outcomes.pop(0) if self._send_outcomes else "resp"
        # BaseException：KeyboardInterrupt 等非 Exception 中断同样抛出。
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def tool_calls(self, session_id):
        self.calls.append(("tool_calls", session_id))
        if self._tool_batches:
            return self._tool_batches.pop(0)
        return []

    def session_detail(self, session_id):
        self.calls.append(("session_detail", session_id))
        return self._session_detail

    def post(self, path, params=None, headers=None, json_body=None, timeout=None):
        self.calls.append(("post", path, json_body))
        return self._post_responses.get(path, (200, {"id": "t_1", "status": "queued"}))

    def get(self, path, params=None, timeout=None, headers=None):
        self.calls.append(("get", path))
        return {"sessions": []}

    def get_raw(self, path, params=None, headers=None, timeout=None):
        self.calls.append(("get_raw", path))
        return (200, {"task": {"status": "succeeded",
                               "execution_session_id": None,
                               "origin_session_id": "s1"}})

    def paths(self, method):
        return [c[1] for c in self.calls if c[0] == method]


class _FakeJudge:
    def __init__(self, order=None, passed=True, reason="ok"):
        self.calls = []
        self._order = order
        self._passed = passed
        self._reason = reason

    def judge(self, **kwargs):
        self.calls.append(kwargs)
        if self._order is not None:
            self._order.append("judge")
        return cr.JudgeVerdict(self._passed, self._reason)


class _FakeCleaner:
    def __init__(self, result=None):
        self.calls = []
        self._result = result if result is not None else {"deleted": [], "failed": []}

    def clean(self, resources, ctx):
        self.calls.append({"resources": resources, "ctx": ctx})
        return self._result


class _FakeCli:
    def __init__(self, session_id="cli-real-1"):
        self.calls = []
        self._session_id = session_id

    def prepare(self, conversation_id):
        self.calls.append(("prepare", conversation_id))
        return self._session_id

    def send(self, conversation_id, content):
        self.calls.append(("send", conversation_id, content))
        return "cli response"


class _FakeAcp:
    """编排 fake ACP driver：脚本化 start/send + close 计数（泄漏检测）。"""

    def __init__(self, session_id="acp-real-1"):
        self.session_id = session_id
        self.calls = []
        self.close_count = 0

    def start(self):
        self.calls.append(("start",))
        return self.session_id

    def send(self, content):
        self.calls.append(("send", content))
        return "acp response"

    def close(self):
        self.close_count += 1


class _FakeBrowserHelper:
    def __init__(self):
        self.calls = []


def _orch_deps(tmp_path, *, driver, judge=None, cleaner=None, cli=None,
               acp=None, side_effects_fn=None, environ=None):
    return cr.RunnerDeps(
        driver_factory=lambda base_url: driver,
        cli_driver_factory=lambda: cli if cli is not None else _FakeCli(),
        acp_driver_factory=lambda cwd: acp,
        browser_helper_factory=lambda d, env=None: _FakeBrowserHelper(),
        judge_factory=lambda: judge if judge is not None else _FakeJudge(),
        cleaner_factory=lambda: cleaner if cleaner is not None else _FakeCleaner(),
        preflight_fn=lambda case, ctx: [],
        side_effects_fn=side_effects_fn or (lambda case, ctx: []),
        run_id_fn=lambda: "testrun1",
        environ={} if environ is None else environ,
        workspace_root=str(tmp_path / "ws"),
        environment="test-env",
        lock_path=str(tmp_path / "run.lock"),
        acp_cwd=str(tmp_path),
    )


def _replay_args(dataset_dir, report_dir, *extra):
    return cr.parse_cli_args(["--dataset-dir", str(dataset_dir),
                              "--report-dir", str(report_dir), *extra])


def _run_report(tmp_path, report_dir):
    return json.loads((Path(report_dir) / "testrun1" / "report.json")
                      .read_text(encoding="utf-8"))


def _run_manifest(tmp_path, report_dir):
    return json.loads((Path(report_dir) / "testrun1" / "manifest.json")
                      .read_text(encoding="utf-8"))


class TestOrchestrationArgs:
    def test_cleanup_only_mutex_only(self):
        with pytest.raises(SystemExit) as ei:
            cr.parse_cli_args(["--cleanup-only", "m.json", "--only", "a"])
        assert ei.value.code == 2

    def test_cleanup_only_mutex_keep(self):
        with pytest.raises(SystemExit) as ei:
            cr.parse_cli_args(["--cleanup-only", "m.json", "--keep"])
        assert ei.value.code == 2

    def test_cleanup_only_mutex_dataset_dir(self):
        with pytest.raises(SystemExit) as ei:
            cr.parse_cli_args(["--cleanup-only", "m.json", "--dataset-dir", "d"])
        assert ei.value.code == 2

    def test_replay_requires_dataset_dir(self):
        with pytest.raises(SystemExit) as ei:
            cr.parse_cli_args(["--report-dir", "r"])
        assert ei.value.code == 2

    def test_replay_requires_report_dir(self):
        with pytest.raises(SystemExit) as ei:
            cr.parse_cli_args(["--dataset-dir", "d"])
        assert ei.value.code == 2

    def test_help_exits_zero(self):
        with pytest.raises(SystemExit) as ei:
            cr.parse_cli_args(["--help"])
        assert ei.value.code == 0

    def test_cleanup_only_minimal_ok(self):
        args = cr.parse_cli_args(["--cleanup-only", "m.json"])
        assert args.cleanup_only == "m.json"
        assert args.base_url == "http://127.0.0.1:8201"


class TestOrchestrationReplay:
    def test_all_pass_exit_zero_and_report(self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        _write_case(ds)
        driver = _FakeOrchDriver()
        cleaner = _FakeCleaner()
        judge = _FakeJudge()
        deps = _orch_deps(tmp_path, driver=driver, judge=judge, cleaner=cleaner)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 0
        out = capsys.readouterr().out
        assert f"REPORT_DIR {tmp_path / 'reports' / 'testrun1'}" in out
        report = _run_report(tmp_path, tmp_path / "reports")
        assert report["run_id"] == "testrun1"
        assert report["summary"] == {"total": 1, "passed": 1, "failed": 0}
        case = report["cases"][0]
        assert case["verdict"] == "PASS"
        assert case["messages"][0]["verdict"] == "PASS"
        assert report["cleanup"]["failed"] == []
        # 会话资源进 manifest 并交给 Cleaner；遥测豁免登记。
        manifest = _run_manifest(tmp_path, tmp_path / "reports")
        sids = [r["id"] for r in manifest["resources"] if r["type"] == "session"]
        assert sids == ["e2e-testrun1-c1"]
        assert cleaner.calls[0]["resources"][0]["id"] == "e2e-testrun1-c1"
        assert any(e["type"] == "telemetry" for e in manifest["exemptions"])
        assert report["cleanup"]["exemptions"] == manifest["exemptions"]

    def test_judge_receives_session_state_when_options_present(self, tmp_path):
        ds = tmp_path / "ds"
        ds.mkdir()
        _write_case(ds, options={"external_memory_enabled": ["file_1"]})
        driver = _FakeOrchDriver(
            session_detail={"session": {"external_memory_enabled": ["file_1"]}})
        judge = _FakeJudge()
        deps = _orch_deps(tmp_path, driver=driver, judge=judge)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 0
        state = judge.calls[0]["session_state"]
        assert state["requested_options"] == {"external_memory_enabled": ["file_1"]}
        assert state["session_external_memory_enabled"] == ["file_1"]

    def test_judge_session_state_none_without_options(self, tmp_path):
        ds = tmp_path / "ds"
        ds.mkdir()
        _write_case(ds)
        driver = _FakeOrchDriver()
        judge = _FakeJudge()
        deps = _orch_deps(tmp_path, driver=driver, judge=judge)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 0
        assert judge.calls[0]["session_state"] is None

    def test_session_state_readback_happens_after_send(self, tmp_path):
        # external_memory_enabled 在首条消息发送时才锁定到会话，回读必须在
        # send 之后，否则读到 null（Run 4 memory-file-chat 误判教训）。
        ds = tmp_path / "ds"
        ds.mkdir()
        _write_case(ds, options={"external_memory_enabled": ["file_1"]})
        driver = _FakeOrchDriver(
            session_detail={"session": {"external_memory_enabled": ["file_1"]}})
        judge = _FakeJudge()
        deps = _orch_deps(tmp_path, driver=driver, judge=judge)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 0
        kinds = [c[0] for c in driver.calls]
        assert kinds.index("session_detail") > kinds.index("send")

    def test_case_fail_exit_one(self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        _write_case(ds)
        driver = _FakeOrchDriver()
        judge = _FakeJudge(passed=False, reason="rubric 不满足")
        deps = _orch_deps(tmp_path, driver=driver, judge=judge)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 1
        report = _run_report(tmp_path, tmp_path / "reports")
        assert report["cases"][0]["verdict"] == "FAIL"
        assert report["cases"][0]["messages"][0]["reason"] == "rubric 不满足"

    def test_cleanup_failure_exit_three(self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        _write_case(ds)
        driver = _FakeOrchDriver()
        cleaner = _FakeCleaner(result={
            "deleted": [],
            "failed": [{"resource": {"type": "session", "id": "e2e-testrun1-c1"},
                        "reason": "DELETE http 500"}]})
        deps = _orch_deps(tmp_path, driver=driver, cleaner=cleaner)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 3
        report = _run_report(tmp_path, tmp_path / "reports")
        assert report["cleanup"]["failed"][0]["reason"] == "DELETE http 500"

    def test_keep_skips_cleanup_but_writes_report(self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        _write_case(ds)
        driver = _FakeOrchDriver()
        cleaner = _FakeCleaner()
        deps = _orch_deps(tmp_path, driver=driver, cleaner=cleaner)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports"), "--keep"], deps=deps)
        assert code == 0
        assert cleaner.calls == []
        report = _run_report(tmp_path, tmp_path / "reports")
        assert report["cleanup"]["kept"] is True
        assert report["cleanup"]["failed"] == []
        assert f"REPORT_DIR" in capsys.readouterr().out

    def test_only_unknown_id_exit_two(self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        _write_case(ds)
        driver = _FakeOrchDriver()
        deps = _orch_deps(tmp_path, driver=driver)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports"), "--only", "nope"], deps=deps)
        assert code == 2


class TestOrchestrationWorkspacePrecheck:
    def _task_case(self, **over):
        msg = {"id": "m1", "content": "", "replay_via": "task_api",
               "action_params": {"title": "任务", "body": "b",
                                 "goal_mode": True, "priority": 2},
               "expect": {"rubric": "r", "tools": []}}
        base = {"requires": [], "messages": [msg],
                "side_effects": [
                    {"type": "task_terminal", "status": "succeeded",
                     "timeout": 600},
                    {"type": "workspace_file", "path": "out.txt",
                     "expected_content": "x"}],
                "cleanup": ["session", "task", "workspace_file"]}
        base.update(over)
        return base

    def test_preexisting_file_fails_before_task_creation(self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        (ds / "a.json").write_text(json.dumps(_case(**self._task_case())),
                                   encoding="utf-8")
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "out.txt").write_text("existing", encoding="utf-8")
        driver = _FakeOrchDriver()
        deps = _orch_deps(tmp_path, driver=driver)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 1
        # 未创建任何任务（无 POST /chat/tasks）。
        assert "/chat/tasks" not in driver.paths("post")
        report = _run_report(tmp_path, tmp_path / "reports")
        case = report["cases"][0]
        assert case["verdict"] == "FAIL"
        assert case["reason"].startswith("workspace_file_preexists: out.txt")
        assert case["messages"][0]["reason"] == "blocked_by_previous_failure"

    def test_absent_file_allows_task_creation(self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        (ds / "a.json").write_text(json.dumps(_case(**self._task_case())),
                                   encoding="utf-8")
        driver = _FakeOrchDriver()
        order = []
        judge = _FakeJudge(order=order)

        def _se(case, ctx):
            order.append("side_effects")
            return [{"type": "task_terminal", "verdict": "PASS", "detail": "ok"},
                    {"type": "workspace_file", "verdict": "PASS",
                     "detail": "ok", "sha256": "0" * 64}]

        deps = _orch_deps(tmp_path, driver=driver, judge=judge,
                          side_effects_fn=_se)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 0
        assert "/chat/tasks" in driver.paths("post")

    def test_workspace_file_recorded_in_manifest_for_cleanup(self, tmp_path):
        ds = tmp_path / "ds"
        ds.mkdir()
        (ds / "a.json").write_text(json.dumps(_case(**self._task_case())),
                                   encoding="utf-8")
        driver = _FakeOrchDriver()

        def _se(case, ctx):
            return [{"type": "task_terminal", "verdict": "PASS", "detail": "ok"},
                    {"type": "workspace_file", "verdict": "PASS",
                     "detail": "ok", "path": "out.txt", "sha256": "a" * 64}]

        cleaner = _FakeCleaner()
        deps = _orch_deps(tmp_path, driver=driver, cleaner=cleaner,
                          side_effects_fn=_se)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 0
        manifest = _run_manifest(tmp_path, tmp_path / "reports")
        wf = [r for r in manifest["resources"] if r["type"] == "workspace_file"]
        assert len(wf) == 1
        assert wf[0]["path"] == "out.txt"
        assert wf[0]["sha256"] == "a" * 64
        assert wf[0]["verify"] == "fs_absent"
        assert wf[0]["case_id"] == "c1"
        # workspace_file 资源交给 Cleaner（不漏删）。
        assert any(r.get("type") == "workspace_file"
                   for r in cleaner.calls[0]["resources"])


class TestOrchestrationBlocked:
    def test_mid_case_failure_blocks_remaining_and_continues(self, tmp_path,
                                                             capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        msgs = [{"id": f"m{i}", "content": "hi",
                 "expect": {"rubric": "r", "tools": []}} for i in (1, 2, 3)]
        _write_case(ds, "a.json", messages=msgs)
        _write_case(ds, "b.json", id="c2", title="t2",
                    source_session_id="dashboard-bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee")
        driver = _FakeOrchDriver(
            send_outcomes=["r1", cr.ReplayError("boom"), "r3"])
        deps = _orch_deps(tmp_path, driver=driver)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 1
        report = _run_report(tmp_path, tmp_path / "reports")
        assert report["summary"] == {"total": 2, "passed": 1, "failed": 1}
        c1, c2 = report["cases"]
        assert [m["verdict"] for m in c1["messages"]] == ["PASS", "FAIL", "FAIL"]
        assert c1["messages"][1]["reason"].startswith("replay_error: boom")
        assert c1["messages"][2]["reason"] == "blocked_by_previous_failure"
        assert c2["verdict"] == "PASS"


class TestOrchestrationCliOrdering:
    def test_prepare_before_first_send_and_manifest(self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        _write_case(ds, channel="cli",
                    source_session_id="cli-6d7f1ab0-e0f7-404e-9448-1941b26a239e")
        driver = _FakeOrchDriver()
        cli = _FakeCli(session_id="cli-real-1")
        deps = _orch_deps(tmp_path, driver=driver, cli=cli)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 0
        kinds = [c[0] for c in cli.calls]
        assert kinds == ["prepare", "send"]  # prepare 恰一次且在首个 send 前
        manifest = _run_manifest(tmp_path, tmp_path / "reports")
        sessions = [r for r in manifest["resources"] if r["type"] == "session"]
        assert [s["id"] for s in sessions] == ["cli-real-1"]
        assert any(e["type"] == "external"
                   and "cli gateway 入口键 e2e-testrun1-c1" in e["detail"]
                   for e in manifest["exemptions"])


class TestOrchestrationTaskJudgeDelay:
    def test_task_api_judge_after_side_effect_verification(self, tmp_path,
                                                           capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        msg = {"id": "m1", "content": "", "replay_via": "task_api",
               "action_params": {"title": "任务：产出制品", "body": "b",
                                 "goal_mode": True, "priority": 2},
               "expect": {"rubric": "r", "tools": []}}
        (ds / "a.json").write_text(json.dumps(_case(
            messages=[msg],
            side_effects=[{"type": "task_terminal", "status": "succeeded",
                           "timeout": 600}],
            cleanup=["session", "task"])), encoding="utf-8")
        driver = _FakeOrchDriver()
        order = []
        judge = _FakeJudge(order=order)

        def _se(case, ctx):
            order.append("side_effects")
            assert ctx.task_id == "t_1"
            return [{"type": "task_terminal", "verdict": "PASS", "detail": "ok"}]

        deps = _orch_deps(tmp_path, driver=driver, judge=judge,
                          side_effects_fn=_se)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 0
        # Judge 延迟：任务等待与副作用验证先于该消息 Judge。
        assert order == ["side_effects", "judge"]
        # task_api 不经聊天通道发送。
        assert "send" not in [c[0] for c in driver.calls]
        # 创建参数：e2e- 前缀标题 + 稳定 idempotency_key + origin 会话。
        post = next(c for c in driver.calls if c[0] == "post")
        body = post[2]
        assert body["title"].startswith("e2e-testrun1-c1-任务：产出制品")
        assert body["origin_session_id"] == "e2e-testrun1-c1"
        import hashlib as _hl
        assert body["idempotency_key"] == _hl.sha256(
            b"testrun1/c1/m1").hexdigest()
        # 任务真实 ID 记录 manifest。
        manifest = _run_manifest(tmp_path, tmp_path / "reports")
        tasks = [r for r in manifest["resources"] if r["type"] == "task"]
        assert tasks[0]["id"] == "t_1"
        assert tasks[0]["owner_session_id"] == "e2e-testrun1-c1"
        report = _run_report(tmp_path, tmp_path / "reports")
        assert report["cases"][0]["side_effects"][0]["verdict"] == "PASS"


class TestOrchestrationCleanupOnly:
    def _write_manifest(self, tmp_path, **over):
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        data = {"schema_version": 1, "project": "default", "run_id": "r1",
                "base_url": "http://127.0.0.1:8201", "environment": "test-env",
                "started_at": "2026-09-13T22:30:00+08:00",
                "resources": [{"type": "session", "id": "e2e-r1-c1",
                               "case_id": "c1", "status": "active",
                               "verify": "detail_session_null"}],
                "exemptions": []}
        data.update(over)
        path = run_dir / "manifest.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def _cleanup_deps(self, tmp_path, cleaner, driver=None):
        def _no_judge():
            raise AssertionError("cleanup-only 不得初始化 Judge")
        return cr.RunnerDeps(
            driver_factory=lambda base_url: driver or _FakeOrchDriver(),
            cli_driver_factory=lambda: _FakeCli(),
            acp_driver_factory=lambda cwd: None,
            browser_helper_factory=lambda d, env=None: _FakeBrowserHelper(),
            judge_factory=_no_judge,
            cleaner_factory=lambda: cleaner,
            preflight_fn=lambda case, ctx: [],
            side_effects_fn=lambda case, ctx: [],
            run_id_fn=lambda: "testrun1",
            environ={},
            workspace_root=str(tmp_path / "ws"),
            environment="test-env",
            lock_path=str(tmp_path / "run.lock"),
            acp_cwd=str(tmp_path),
        )

    def test_cleanup_only_success_updates_original(self, tmp_path, capsys):
        manifest = self._write_manifest(tmp_path)

        class _MarkingCleaner:
            def clean(self, resources, ctx):
                for r in resources:
                    ctx.recorder.mark_status(r, "deleted")
                return {"deleted": resources, "failed": []}

        deps = self._cleanup_deps(tmp_path, _MarkingCleaner())
        code = cr.main(["--cleanup-only", str(manifest)], deps=deps)
        assert code == 0
        # 原 manifest 进度更新（不新建 run）。
        updated = json.loads(manifest.read_text(encoding="utf-8"))
        assert updated["resources"][0]["status"] == "deleted"
        assert updated["run_id"] == "r1"
        cleanup_report = json.loads(
            (manifest.parent / "cleanup-report.json").read_text(encoding="utf-8"))
        assert cleanup_report["cleanup_only"] is True
        assert cleanup_report["cleanup"]["failed"] == []
        assert f"REPORT_DIR {manifest.parent}" in capsys.readouterr().out

    def test_cleanup_only_base_url_mismatch_exit_two(self, tmp_path, capsys):
        manifest = self._write_manifest(tmp_path)
        deps = self._cleanup_deps(tmp_path, _FakeCleaner())
        code = cr.main(["--cleanup-only", str(manifest),
                        "--base-url", "http://other:9999"], deps=deps)
        assert code == 2

    def test_cleanup_only_invalid_manifest_exit_two(self, tmp_path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        deps = self._cleanup_deps(tmp_path, _FakeCleaner())
        assert cr.main(["--cleanup-only", str(bad)], deps=deps) == 2

    def test_cleanup_only_cleanup_failure_exit_three(self, tmp_path, capsys):
        manifest = self._write_manifest(tmp_path)
        cleaner = _FakeCleaner(result={
            "deleted": [],
            "failed": [{"resource": {"type": "session", "id": "e2e-r1-c1"},
                        "reason": "DELETE http 403"}]})
        deps = self._cleanup_deps(tmp_path, cleaner)
        code = cr.main(["--cleanup-only", str(manifest)], deps=deps)
        assert code == 3


class TestOrchestrationLock:
    def test_second_acquire_fails_fast_and_release(self, tmp_path):
        path = tmp_path / "run.lock"
        first = cr.RunLock(path).acquire()
        try:
            with pytest.raises(cr.RunLockError) as ei:
                cr.RunLock(path).acquire()
            assert "运行锁被占用" in str(ei.value)
        finally:
            first.release()
        # 释放后可重新获取（fd 已关闭）。
        second = cr.RunLock(path).acquire()
        second.release()

    def test_replay_refused_when_lock_held(self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        _write_case(ds)
        driver = _FakeOrchDriver()
        deps = _orch_deps(tmp_path, driver=driver)
        held = cr.RunLock(deps.lock_path).acquire()
        try:
            code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                            str(tmp_path / "reports")], deps=deps)
        finally:
            held.release()
        assert code == 2
        assert "运行锁被占用" in capsys.readouterr().err

    def test_lock_released_after_run(self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        _write_case(ds)
        driver = _FakeOrchDriver()
        deps = _orch_deps(tmp_path, driver=driver)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 0
        # 运行结束后锁已释放，可立即重新获取。
        lock = cr.RunLock(deps.lock_path).acquire()
        lock.release()


class TestOrchestrationInterrupt:
    def test_keyboard_interrupt_persists_manifest_and_cleans_once(
            self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        _write_case(ds, "a.json")
        _write_case(ds, "b.json", id="c2", title="t2",
                    source_session_id="dashboard-bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee")
        # 用例 2 首条消息发送时中断（KeyboardInterrupt 非 Exception，不经
        # 用例级兜底，直接进入编排中断路径）。
        driver = _FakeOrchDriver(
            send_outcomes=["r1", KeyboardInterrupt("ctrl-c")])
        cleaner = _FakeCleaner()
        deps = _orch_deps(tmp_path, driver=driver, cleaner=cleaner)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 2
        # 中断后清理仍尝试恰好一次。
        assert len(cleaner.calls) == 1
        # 中断前已建会话的 manifest 条目已持久化（--cleanup-only 可恢复）。
        manifest = _run_manifest(tmp_path, tmp_path / "reports")
        sids = [r["id"] for r in manifest["resources"]
                if r["type"] == "session"]
        assert sids == ["e2e-testrun1-c1", "e2e-testrun1-c2"]
        # 部分结果保留：用例 1 完整 PASS，用例 2 记为中断未执行。
        report = _run_report(tmp_path, tmp_path / "reports")
        assert report["summary"] == {"total": 2, "passed": 1, "failed": 1}
        assert report["cases"][0]["verdict"] == "PASS"
        assert report["cases"][1]["verdict"] == "FAIL"
        assert "运行中断" in report["cases"][1]["reason"]

    def test_run_case_exception_runner_error_remaining_cases_run(
            self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        _write_case(ds, "a.json")
        _write_case(ds, "b.json", id="c2", title="t2",
                    source_session_id="dashboard-bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee")
        # 用例 1 发送前快照抛非 ReplayError（编排自身缺陷），记 runner_error
        # 并继续执行用例 2。
        class _BoomDriver(_FakeOrchDriver):
            def __init__(self):
                super().__init__()
                self._boomed = False

            def tool_calls(self, session_id):
                if not self._boomed:
                    self._boomed = True
                    raise RuntimeError("snapshot boom")
                return super().tool_calls(session_id)

        driver = _BoomDriver()
        deps = _orch_deps(tmp_path, driver=driver)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 2
        report = _run_report(tmp_path, tmp_path / "reports")
        c1, c2 = report["cases"]
        assert c1["verdict"] == "FAIL"
        assert c1["reason"].startswith(
            "runner_error: RuntimeError: snapshot boom")
        assert c2["verdict"] == "PASS"
        # 用例 2 的会话确实建立并发送（未被 runner_error 阻断）。
        assert ("create_session", "e2e-testrun1-c2") in driver.calls
        assert ("send", "e2e-testrun1-c2", "hi") in driver.calls


class TestOrchestrationReportDir:
    def test_existing_report_dir_exit_two(self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        _write_case(ds)
        # run_id 固定为 testrun1：预建同目录触发拒绝覆盖。
        (tmp_path / "reports" / "testrun1").mkdir(parents=True)
        deps = _orch_deps(tmp_path, driver=_FakeOrchDriver())
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 2
        assert "报告目录已存在" in capsys.readouterr().err

    def test_report_dir_under_file_unexpected_error_exit_two(
            self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        _write_case(ds)
        # report-dir 父路径是文件：mkdir 抛 OSError（用例循环之外的未预期
        # 异常），必须按 runner 错误（2）而非 traceback/退出码 1 处理。
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        deps = _orch_deps(tmp_path, driver=_FakeOrchDriver())
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(blocker / "reports")], deps=deps)
        assert code == 2
        assert "ERROR 编排异常" in capsys.readouterr().err


class TestOrchestrationAcp:
    def _acp_case(self, ds, **over):
        _write_case(ds, channel="acp",
                    source_session_id="acp-6d7f1ab0-e0f7-404e-9448-1941b26a239e",
                    **over)

    def test_session_new_real_id_recorded_in_manifest(self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        self._acp_case(ds)
        acp = _FakeAcp(session_id="acp-real-1")
        deps = _orch_deps(tmp_path, driver=_FakeOrchDriver(), acp=acp)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 0
        # session/new 返回的真实 ID 记录 manifest。
        manifest = _run_manifest(tmp_path, tmp_path / "reports")
        sessions = [r for r in manifest["resources"] if r["type"] == "session"]
        assert [s["id"] for s in sessions] == ["acp-real-1"]
        assert acp.calls == [("start",), ("send", "hi")]
        # 正常路径 close 恰好一次。
        assert acp.close_count == 1

    def test_acp_closed_on_browser_preflight_failure(self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        self._acp_case(ds, requires=["browser"])
        acp = _FakeAcp()
        deps = _orch_deps(tmp_path, driver=_FakeOrchDriver(), acp=acp)
        deps.preflight_fn = lambda case, ctx: ["browser 守护不可用"]
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 1
        report = _run_report(tmp_path, tmp_path / "reports")
        assert report["cases"][0]["reason"].startswith(
            "preflight_failed: browser 守护不可用")
        # 预检失败提前 return 仍关闭 acp 子进程（不泄漏）。
        assert acp.close_count == 1

    def test_acp_closed_on_workspace_precheck_failure(self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        self._acp_case(
            ds,
            side_effects=[{"type": "workspace_file", "path": "out.txt",
                           "expected_content": "x"}],
            cleanup=["session", "workspace_file"])
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "out.txt").write_text("existing", encoding="utf-8")
        acp = _FakeAcp()
        deps = _orch_deps(tmp_path, driver=_FakeOrchDriver(), acp=acp)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 1
        report = _run_report(tmp_path, tmp_path / "reports")
        assert report["cases"][0]["reason"].startswith(
            "workspace_file_preexists: out.txt")
        # 前置检查失败提前 return 仍关闭 acp 子进程（不泄漏）。
        assert acp.close_count == 1


class TestOrchestrationMessageExemption:
    _MEM0_DETAIL = "mem0 外部记忆写入（memory-mem0-chat m5）"

    def _mem0_case(self, ds, expect):
        _write_case(
            ds, id="memory-mem0-chat",
            source_session_id="dashboard-cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee",
            messages=[{"id": "m5", "content": "记住我喜欢咖啡",
                       "expect": expect}])

    def test_deterministic_failure_still_records_mem0_exemption(
            self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        # 期望工具未调用 -> 确定性失败，不经 Judge；但消息已发送、mem0 外部
        # 写入可能已发生，豁免必须仍登记（与"消息已发送"因果绑定）。
        self._mem0_case(ds, {"rubric": "r", "tools": ["memory_store"]})
        judge = _FakeJudge()
        deps = _orch_deps(tmp_path, driver=_FakeOrchDriver(), judge=judge)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 1
        assert judge.calls == []  # 确定性失败短路，未调用 Judge
        manifest = _run_manifest(tmp_path, tmp_path / "reports")
        assert {"type": "external", "detail": self._MEM0_DETAIL} \
            in manifest["exemptions"]

    def test_judge_path_records_mem0_exemption_once(self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        self._mem0_case(ds, {"rubric": "r", "tools": []})
        judge = _FakeJudge()
        deps = _orch_deps(tmp_path, driver=_FakeOrchDriver(), judge=judge)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        assert code == 0
        assert len(judge.calls) == 1
        manifest = _run_manifest(tmp_path, tmp_path / "reports")
        mem0 = [e for e in manifest["exemptions"]
                if e["detail"] == self._MEM0_DETAIL]
        assert mem0 == [{"type": "external", "detail": self._MEM0_DETAIL}]


class TestOrchestrationRunnerError:
    def test_side_effects_replay_error_marks_runner_error_exit_two(
            self, tmp_path, capsys):
        ds = tmp_path / "ds"
        ds.mkdir()
        _write_case(ds,
                    side_effects=[{"type": "task_terminal",
                                   "status": "succeeded", "timeout": 600}],
                    cleanup=["session", "task"])

        def _se(case, ctx):
            raise cr.ReplayError("side effect infra down")

        deps = _orch_deps(tmp_path, driver=_FakeOrchDriver(),
                          side_effects_fn=_se)
        code = cr.main(["--dataset-dir", str(ds), "--report-dir",
                        str(tmp_path / "reports")], deps=deps)
        # 副作用验证的 ReplayError 属基础设施错误 -> runner_error -> 2。
        assert code == 2
        report = _run_report(tmp_path, tmp_path / "reports")
        case = report["cases"][0]
        assert case["verdict"] == "FAIL"
        assert case["messages"][0]["verdict"] == "PASS"
        assert case["side_effects"][0]["type"] == "task_terminal"
        assert case["side_effects"][0]["verdict"] == "FAIL"
        assert case["side_effects"][0]["detail"].startswith("infra_error")


class TestExtractTaskIdFromTools:
    """create_task 工具结果的真实存储形状（sqlite_store result_json 探针证实）：
    API result 字段是信封 dict {"tool_call_id","name","status","content"}，
    真正负载是 content 里的 JSON 字符串 {"success": true, "task": {"id": ...}}。"""

    def _record(self, result):
        return {"name": "create_task", "status": "success", "result": result}

    def test_real_envelope_shape_extracts_id(self):
        payload = json.dumps(
            {"success": True,
             "task": {"id": "t_a85d05c4581848e8", "title": "分析Python语言特点",
                      "status": "queued", "goal_mode": False}},
            ensure_ascii=False)
        envelope = {"tool_call_id": "call_x", "name": "create_task",
                    "status": "success", "content": payload, "duration_ms": 11}
        assert cr._extract_task_id_from_tools(
            [self._record(envelope)]) == "t_a85d05c4581848e8"

    def test_envelope_as_json_string_extracts_id(self):
        payload = json.dumps({"success": True, "task": {"id": "t_abc123"}})
        envelope = json.dumps({"tool_call_id": "call_x", "name": "create_task",
                               "status": "success", "content": payload})
        assert cr._extract_task_id_from_tools(
            [self._record(envelope)]) == "t_abc123"

    def test_direct_payload_shape_still_works(self):
        # 兼容未套信封的直连形状（task_api 路径或未来存储变化）。
        assert cr._extract_task_id_from_tools(
            [self._record({"success": True, "task": {"id": "t_direct"}})]
        ) == "t_direct"

    def test_content_not_json_returns_none(self):
        envelope = {"tool_call_id": "call_x", "name": "create_task",
                    "status": "success", "content": "not json"}
        assert cr._extract_task_id_from_tools([self._record(envelope)]) is None

    def test_error_payload_returns_none(self):
        payload = json.dumps({"success": False, "error": "task_invalid"})
        envelope = {"tool_call_id": "call_x", "name": "create_task",
                    "status": "error", "content": payload}
        assert cr._extract_task_id_from_tools([self._record(envelope)]) is None

    def test_non_create_task_tools_ignored(self):
        assert cr._extract_task_id_from_tools(
            [{"name": "web_fetch", "status": "success",
              "result": {"content": "{}"}}]) is None
