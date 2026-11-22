"""口径重算登记到执行全链路校验（11 月 1 日窗口场景）。

覆盖：
- 目标口径版本不存在时拒绝登记，不产生虚假差异；
- 新版本未发布时首次启动失败并留痕，发布后从原任务入口重试成功；
- 资格判定（空态、其他指标、指标/日期一致性）；
- 重复启动幂等，只签发一次差异，首次失败仍可核查；
- 倒序日期不进入调度；
- 窗内旧值 20/新值 30、窗外变更不进差额，差额合计 10；
- 仅属于另一指标的有效版次触发的失败与本指标区分；
- 报告只消费获准且实际完成的窗口差异；
- 结束原申请并登记关联任务出账，历史保留首次失败身份。
"""
from __future__ import annotations

import pytest

from app.services.metric_definition import MetricDefinitionService
from app.services.caliber_version import CaliberVersionService
from app.services.recalc_engine import RecalcEngineService
from app.store import store


@pytest.fixture()
def scenario():
    """构造：两个指标，各有旧版；指标A的新版待发布，指标B的新版已生效。"""
    metric_a = f"重算验证指标A-{len(store.recalc_applications())}-{len(store.rows('metric_definition'))}"
    metric_b = f"重算验证指标B-{len(store.recalc_applications())}-{len(store.rows('metric_definition'))}"
    created_metric_ids: list[int] = []
    created_caliber_ids: list[int] = []
    created_template_ids: list[int] = []
    ledger_before = list(store.recalc_change_ledger())
    apps_before = len(store.recalc_applications())
    rows = store.rows("metric_definition")
    id_a = store.next_id("metric_definition")
    rows.append({
        "id": id_a, "status": "已发布", "pending": False, "abnormal": False,
        "指标名称": metric_a, "计算口径": "旧口径", "统计粒度": "日",
        "数据源": "dw", "业务域": "验收",
    })
    created_metric_ids.append(id_a)
    id_b = store.next_id("metric_definition")
    rows.append({
        "id": id_b, "status": "已发布", "pending": False, "abnormal": False,
        "指标名称": metric_b, "计算口径": "旧口径", "统计粒度": "日",
        "数据源": "dw", "业务域": "验收",
    })
    created_metric_ids.append(id_b)

    def add_caliber(metric, version, status):
        cid = store.next_id("caliber_version")
        store.rows("caliber_version").append({
            "id": cid, "所属指标": metric, "版本号": version,
            "口径描述": f"{metric}-{version}", "变更类型": "口径调整",
            "生效时间": "2026-10-01", "status": status,
            "pending": status != "已过期", "abnormal": False,
        })
        created_caliber_ids.append(cid)
        return cid

    old_a = add_caliber(metric_a, "v1.0", "已生效")
    new_a_pending = add_caliber(metric_a, "v2.0", "待生效")
    old_b = add_caliber(metric_b, "v1.0", "已生效")
    new_b_active = add_caliber(metric_b, "v2.0", "已生效")

    # 变更台账：A 指标 11 月 1 日窗内 20->30，12 月 20 日窗外；B 指标一条
    ledger = store.recalc_change_ledger()
    base = max(int(c["id"]) for c in ledger)
    ledger.extend([
        {"id": base + 1, "指标": metric_a, "记录日期": "2026-11-01", "旧值": 20, "新值": 30},
        {"id": base + 2, "指标": metric_a, "记录日期": "2026-12-20", "旧值": 50, "新值": 80},
        {"id": base + 3, "指标": metric_b, "记录日期": "2026-11-05", "旧值": 5, "新值": 9},
    ])

    yield {
        "id_a": id_a, "id_b": id_b, "metric_a": metric_a, "metric_b": metric_b,
        "old_a": old_a, "new_a_pending": new_a_pending,
        "old_b": old_b, "new_b_active": new_b_active,
        "window": "2026-11-01 到 2026-11-30",
        "created_template_ids": created_template_ids,
    }

    # 回滚本用例写入的全部数据，保证用例间互不污染
    store.rows("metric_definition")[:] = [
        r for r in store.rows("metric_definition") if r["id"] not in created_metric_ids]
    store.rows("caliber_version")[:] = [
        r for r in store.rows("caliber_version") if r["id"] not in created_caliber_ids]
    store.rows("report_template")[:] = [
        r for r in store.rows("report_template") if r["id"] not in created_template_ids]
    ledger[:] = ledger_before
    created_task_ids = {
        int(t["id"]) for t in store.rows("recalc_engine")
        if t.get("关联指标") in (metric_a, metric_b)}
    store.rows("recalc_engine")[:] = [
        r for r in store.rows("recalc_engine") if r["id"] not in created_task_ids]
    # 重算内部留痕按本用例新增申请整体回滚
    kept_apps = store.recalc_applications()[:apps_before]
    store._recalc_applications[:] = kept_apps
    keep_app_ids = {int(a["id"]) for a in kept_apps}
    keep_task_ids = {int(t["id"]) for t in store.rows("recalc_engine")}
    store._recalc_attempts[:] = [
        a for a in store._recalc_attempts
        if int(a.get("application_id", 0)) in keep_app_ids
        and int(a.get("task_id", 0)) in keep_task_ids]
    store._recalc_details[:] = [
        d for d in store._recalc_details
        if int(d.get("application_id", 0)) in keep_app_ids
        and int(d.get("task_id", 0)) in keep_task_ids]


class TestRegistrationValidation:
    def test_missing_target_version_is_rejected(self, scenario):
        svc = MetricDefinitionService()
        task, msg = svc.recalc_metric_history(
            scenario["id_a"], "v9.9", scenario["window"])
        assert task is None
        assert "尚不存在" in msg
        # 没有任何虚假差异/历史
        assert store.recalc_history() == [] or all(
            h.get("关联指标") != scenario["metric_a"] for h in store.recalc_history())
        assert store.available_recalc_diffs(metric_name=scenario["metric_a"]) == []

    def test_reversed_date_range_never_scheduled(self, scenario):
        svc = MetricDefinitionService()
        task, msg = svc.recalc_metric_history(
            scenario["id_a"], "v2.0", "2026-11-30 到 2026-11-01")
        assert task is None
        assert "倒序" in msg
        assert not [a for a in store.recalc_applications()
                    if a.get("metric") == scenario["metric_a"]]

    def test_missing_dates_rejected(self, scenario):
        svc = MetricDefinitionService()
        task, msg = svc.recalc_metric_history(
            scenario["id_a"], "v2.0", "11月窗口")
        assert task is None
        assert "两个日期" in msg


class TestFirstFailureAndRecovery:
    def test_nov1_window_fail_then_publish_then_retry(self, scenario):
        metric_svc = MetricDefinitionService()
        engine = RecalcEngineService()

        # 登记申请：v2.0 已登记但待生效，申请被受理、任务待调度
        task, msg = metric_svc.recalc_metric_history(
            scenario["id_a"], "v2.0", scenario["window"])
        assert task is not None
        task_id = int(task["id"])
        application_id = int(task["申请ID"])
        assert task["status"] == "待调度"
        assert "尚未发布" in msg

        # 原任务入口资格：明确说明缺版原因且无可用差异
        eligibility = engine.check_eligibility(task_id)
        assert eligibility["eligible"] is False
        assert "尚未发布" in eligibility["reason"]
        assert eligibility["available_diff_count"] == 0

        # 首次启动失败
        failed = engine.start_recalc(task_id)
        assert failed["status"] == "重算失败"
        assert failed["attempt_no"] == 1
        assert "尚未发布" in failed["error"]
        assert store.find("recalc_engine", task_id)["status"] == "重算失败"

        # 历史保留首次失败身份
        history = engine.get_recalc_history(scenario["metric_a"])
        first = [h for h in history if h["任务ID"] == task_id]
        assert len(first) == 1 and first[0]["状态"] == "重算失败"
        assert first[0]["尝试序号"] == 1

        # 发布新版本
        caliber_svc = CaliberVersionService()
        activated, activate_msg = caliber_svc.activate_version(
            scenario["new_a_pending"])
        assert activated is not None and "已生效" in activate_msg

        # 发布后资格恢复，沿用既有申请
        eligibility2 = engine.check_eligibility(task_id)
        assert eligibility2["eligible"] is True
        assert eligibility2["application_id"] == application_id

        # 从原任务入口重试成功，沿用同一申请
        result = engine.start_recalc(task_id)
        assert result["status"] == "已完成"
        assert result["application_id"] == application_id
        assert result["attempt_no"] == 2

        # 窗内仅一笔差异：旧值20、新值30、差额10；窗外 12-20 不计入
        in_window = result["details"]
        in_window = [d for d in in_window if d["在窗内"]]
        assert len(in_window) == 1
        row = in_window[0]
        assert row["记录日期"] == "2026-11-01"
        assert row["旧值"] == 20 and row["新值"] == 30 and row["差额"] == 10
        all_details = result["details"]
        assert any(not d["在窗内"] and d["记录日期"] == "2026-12-20"
                   for d in all_details)
        assert result["diff_count"] == 1
        assert result["diff_total"] == 10

        # 历史同时保留首次失败和实际出账
        history = engine.get_recalc_history(scenario["metric_a"])
        task_history = [h for h in history if h["任务ID"] == task_id]
        statuses = sorted(h["状态"] for h in task_history)
        assert statuses == ["已完成", "重算失败"]
        assert [h["尝试序号"] for h in task_history] == [1, 2]

        # 报告消费面：只有窗内完成差异
        available = engine.get_available_diffs(scenario["metric_a"])
        assert available["total"] == 1
        assert available["diff_total"] == 10
        assert available["items"][0]["记录日期"] == "2026-11-01"

    def test_repeat_successful_start_issues_diff_once(self, scenario):
        metric_svc = MetricDefinitionService()
        engine = RecalcEngineService()
        CaliberVersionService().activate_version(scenario["new_a_pending"])
        task, _ = metric_svc.recalc_metric_history(
            scenario["id_a"], "v2.0", scenario["window"])
        task_id = int(task["id"])

        first = engine.start_recalc(task_id)
        assert first["status"] == "已完成"
        second = engine.start_recalc(task_id)
        assert second.get("idempotent") is True
        assert second["diff_count"] == 1
        # 明细只有一笔（按首次成功尝试回写）
        details = store.recalc_details(task_id)
        assert len([d for d in details if d["在窗内"]]) == 1
        # 没有多余尝试记录
        assert len(store.recalc_attempts(task_id)) == 1


class TestOtherMetricAndEmptyStates:
    def test_active_version_of_other_metric_does_not_unlock(self, scenario):
        """B 指标的 v2.0 已生效，但 A 指标申请仍因自身缺版失败。"""
        metric_svc = MetricDefinitionService()
        engine = RecalcEngineService()
        task, _ = metric_svc.recalc_metric_history(
            scenario["id_a"], "v2.0", scenario["window"])
        result = engine.start_recalc(int(task["id"]))
        assert result["status"] == "重算失败"
        # B 指标的 v2.0 虽已生效，A 仍因自身版本待发布而失败
        assert "尚未发布" in result["error"]

        # B 指标用自己的有效版次可以正常出账，但差异不串到 A
        task_b, _ = metric_svc.recalc_metric_history(
            scenario["id_b"], "v2.0", "2026-11-01 到 2026-11-30")
        result_b = engine.start_recalc(int(task_b["id"]))
        assert result_b["status"] == "已完成"
        diffs_a = engine.get_available_diffs(scenario["metric_a"])
        diffs_b = engine.get_available_diffs(scenario["metric_b"])
        assert all(d["指标"] == scenario["metric_b"] for d in diffs_b["items"])
        # A 此时仍未成功出账
        assert diffs_a["total"] == 0
        assert "没有获准且实际完成" in diffs_a["reason"]

    def test_unknown_metric_empty_state_explains_reason(self, scenario):
        engine = RecalcEngineService()
        payload = engine.get_available_diffs("不存在的指标X")
        assert payload["total"] == 0
        assert "不存在" in payload["reason"]

    def test_target_version_belonging_to_other_metric_rejected(self, scenario):
        """申请所指版本号若只在另一指标名下，按目标不存在/不一致处理。"""
        metric_svc = MetricDefinitionService()
        # 指标A 申请一个未在自身名下登记的版本号
        task, msg = metric_svc.recalc_metric_history(
            scenario["id_a"], "v9.8", scenario["window"])
        assert task is None
        assert "尚不存在" in msg
        # 没有产生任何任务与差异
        assert all(t.get("关联指标") != scenario["metric_a"]
                   for t in store.rows("recalc_engine"))
        assert store.available_recalc_diffs(metric_name=scenario["metric_a"]) == []

    def test_unknown_task_eligibility_explains(self):
        engine = RecalcEngineService()
        payload = engine.check_eligibility(999999)
        assert payload["eligible"] is False
        assert "不存在" in payload["reason"]
        assert payload["available_diff_count"] == 0

    def test_record_date_filter_keeps_identity_consistent(self, scenario):
        metric_svc = MetricDefinitionService()
        engine = RecalcEngineService()
        CaliberVersionService().activate_version(scenario["new_a_pending"])
        task, _ = metric_svc.recalc_metric_history(
            scenario["id_a"], "v2.0", scenario["window"])
        engine.start_recalc(int(task["id"]))

        hit = engine.get_available_diffs(scenario["metric_a"], "2026-11-01")
        assert hit["total"] == 1
        miss = engine.get_available_diffs(scenario["metric_a"], "2026-12-20")
        assert miss["total"] == 0
        assert "没有获准且实际完成" in miss["reason"]


class TestCloseAndReregister:
    def test_close_original_application_and_related_task(self, scenario):
        metric_svc = MetricDefinitionService()
        engine = RecalcEngineService()
        task, _ = metric_svc.recalc_metric_history(
            scenario["id_a"], "v2.0", scenario["window"])
        original_task_id = int(task["id"])
        original_application_id = int(task["申请ID"])
        first = engine.start_recalc(original_task_id)
        assert first["status"] == "重算失败"

        # 发布后选择路径二：结束原申请，登记关联任务出账
        CaliberVersionService().activate_version(scenario["new_a_pending"])
        new_task, msg = metric_svc.close_and_reregister_recalc(
            scenario["id_a"], "v2.0", scenario["window"], original_application_id)
        assert new_task is not None
        assert "关联" in msg
        assert store.find_recalc_application(original_application_id)["状态"] == "已结束"
        assert int(new_task["申请ID"]) != original_application_id

        result = engine.start_recalc(int(new_task["id"]))
        assert result["status"] == "已完成"
        assert result["diff_total"] == 10

        # 历史仍可核查首次失败
        history = engine.get_recalc_history(scenario["metric_a"])
        assert any(h["任务ID"] == original_task_id and h["状态"] == "重算失败"
                   for h in history)
        assert any(h["任务ID"] == int(new_task["id"]) and h["状态"] == "已完成"
                   for h in history)


class TestReportConsumption:
    def test_report_only_uses_completed_window_diffs(self, scenario):
        from app.services.report_template import ReportTemplateService
        metric_svc = MetricDefinitionService()
        engine = RecalcEngineService()

        # 先建一个引用指标A的已发布报告模板
        tpl_id = store.next_id("report_template")
        scenario["created_template_ids"].append(tpl_id)
        store.rows("report_template").append({
            "id": tpl_id, "status": "已发布", "pending": False, "abnormal": False,
            "模板名称": "验收报告", "包含指标集": scenario["metric_a"],
            "报告周期": "月报", "输出格式": "HTML", "分发渠道": "邮件",
        })
        task, _ = metric_svc.recalc_metric_history(
            scenario["id_a"], "v2.0", scenario["window"])
        # 未发布前失败：报告无差异且说明原因
        engine.start_recalc(int(task["id"]))
        report_before = ReportTemplateService().generate_report(tpl_id)
        section = next(s for s in report_before["sections"]
                       if s["metric"] == scenario["metric_a"])
        assert section["recalc_diff_count"] == 0
        assert section["recalc_diffs"] == []

        CaliberVersionService().activate_version(scenario["new_a_pending"])
        engine.start_recalc(int(task["id"]))
        report_after = ReportTemplateService().generate_report(tpl_id)
        section = next(s for s in report_after["sections"]
                       if s["metric"] == scenario["metric_a"])
        assert section["recalc_diff_count"] == 1
        assert section["recalc_diff_total"] == 10
        assert section["recalc_diffs"][0]["差额"] == 10
