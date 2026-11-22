"""口径重算引擎领域服务：根据新口径版本重算历史数据。

跨模块数据流: metric_definition（登记申请）-> caliber_version（资格校验）
             -> recalc_engine（调度执行）-> query_builder/cache_layer（缓存失效）
身份链路: 申请 -> 任务 -> 尝试 -> 重算结果明细（窗口差异），明细按任务与尝试回写历史列表。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.store import store
from app.services.base import BaseModuleService
from app.services.recalc_support import (
    evaluate_target,
    parse_date_range,
    resolve_active_caliber,
)


class BusinessRejection(Exception):
    """业务校验未通过：路由层据此返回 ok=False 而不是 500。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RecalcEngineService(BaseModuleService):
    MODULE = "recalc_engine"
    ENTITY = "重算任务"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["关联指标", "新口径版本", "历史时间范围", "重算状态", "产出差异"]
    REQUIRED_FIELDS = ["关联指标", "新口径版本", "历史时间范围"]
    STATUS_ORDER = ["待调度", "重算中", "已完成", "重算失败"]
    ACTION_RULES = {"启动重算": "重算中", "取消重算": "重算失败"}
    NEGATIVE_ACTIONS = ["取消重算"]

    # ── 登记衔接：通用登记入口同样走申请校验，杜绝绕过指标定义的虚假任务 ──

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        metric_rows = store.find_by_field(
            "metric_definition", "指标名称", str(values.get("关联指标", "")).strip())
        if not metric_rows:
            raise BusinessRejection(
                f"指标「{values.get('关联指标')}」不存在，无法登记重算任务")
        from app.services.metric_definition import MetricDefinitionService
        task, message = MetricDefinitionService().recalc_metric_history(
            int(metric_rows[0]["id"]),
            str(values.get("新口径版本", "")).strip(),
            str(values.get("历史时间范围", "")).strip(),
        )
        if task is None:
            raise BusinessRejection(message)
        return task, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """重算动作必须走真实执行链路，不能由通用骨架直接翻状态。"""
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "启动重算":
            result = self.start_recalc(entry_id)
            if result is None or "error" in result:
                return None, (result or {}).get("error", "重算启动失败")
            return task, result.get("message", "重算已完成")
        if action == "取消重算":
            return self.cancel_recalc(entry_id)
        return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

    # ── 资格判定（原任务入口据此说明可启动/不可启动原因） ──

    def check_eligibility(self, entry_id: int) -> dict[str, Any]:
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return {
                "task_id": entry_id,
                "eligible": False,
                "reason": f"重算任务 {entry_id} 不存在，没有可用差异",
                "available_diff_count": 0,
            }
        application = self._application_of(task)
        if application is None:
            return {
                "task_id": entry_id,
                "eligible": False,
                "reason": "任务缺少关联的重算申请，登记链路不完整，没有可用差异",
                "available_diff_count": 0,
            }

        metric_name = str(task.get("关联指标", ""))
        target_version = str(task.get("新口径版本", ""))
        target, reason = evaluate_target(metric_name, target_version)

        # 获准目标还须与记录日期、指标一致：目标版次必须属于申请登记的同一指标
        if target is not None and target.get("所属指标") != metric_name:
            reason = (f"目标口径版本「{target_version}」属于其他指标"
                      f"「{target.get('所属指标')}」，与本任务指标「{metric_name}」不一致")
        if (target is not None and application.get("metric")
                and target.get("所属指标") != application.get("metric")):
            reason = "目标口径版本与申请登记的指标身份不一致，没有可用差异"

        attempts = store.recalc_attempts(entry_id)
        completed = store.successful_attempts(entry_id)
        available = store.available_recalc_diffs(metric_name=metric_name)
        available = [d for d in available if int(d.get("task_id", 0)) == entry_id]

        payload = {
            "task_id": entry_id,
            "application_id": application.get("id"),
            "metric": metric_name,
            "target_caliber_id": target.get("id") if target else None,
            "target_version": target_version,
            "time_range": task.get("历史时间范围"),
            "application_status": application.get("状态"),
            "task_status": task.get("status"),
            "attempt_count": len(attempts),
            "completed_attempt_count": len(completed),
            "eligible": not reason,
            "reason": reason,
            "available_diff_count": len(available),
        }
        if not reason and not completed:
            payload["reason"] = "资格已具备，可从原任务入口启动重算"
        elif not reason:
            payload["reason"] = "重算已实际完成，重复启动不会重复签发差异"
        return payload

    # ── 启动执行 ──

    def start_recalc(self, entry_id: int) -> dict[str, Any] | None:
        """启动重算：从登记申请取得指标身份、目标版次与已校验日期范围。"""
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return {"error": f"重算任务 {entry_id} 不存在"}

        application = self._application_of(task)
        if application is None:
            return {
                "error": "任务缺少关联的重算申请，无法启动（登记到执行链路不完整）",
                "task_id": entry_id,
                "status": "重算失败",
                "diff_count": 0,
                "details": [],
            }

        metric_name = str(task.get("关联指标", ""))
        target_version = str(task.get("新口径版本", ""))
        time_range = str(task.get("历史时间范围", ""))

        # 幂等：同一任务一次成功后，重复启动只签发一次差异，原失败尝试仍可核查
        prior_success = store.successful_attempts(entry_id)
        if prior_success or task.get("status") == "已完成":
            existing = store.recalc_details(entry_id)
            in_window = [d for d in existing if d.get("在窗内")]
            return {
                "task_id": entry_id,
                "application_id": application.get("id"),
                "metric": metric_name,
                "status": "已完成",
                "idempotent": True,
                "message": "该任务已成功重算，重复启动不重复签发差异",
                "diff_count": len(in_window),
                "details": in_window,
            }

        start_date, end_date, date_error = parse_date_range(time_range)
        if date_error:
            return self._fail_attempt(task, application, entry_id, metric_name,
                                      target_version, time_range, date_error)

        target, reason = evaluate_target(metric_name, target_version)
        if target is not None and target.get("所属指标") != metric_name:
            reason = (f"目标口径版本「{target_version}」属于其他指标"
                      f"「{target.get('所属指标')}」，与任务指标「{metric_name}」不一致")
        if reason:
            return self._fail_attempt(task, application, entry_id, metric_name,
                                      target_version, time_range, reason)

        # 资格齐备：进入重算中并执行窗口出账
        task["status"] = "重算中"
        task["重算状态"] = "重算中"
        task["pending"] = True
        task["abnormal"] = False

        active_caliber = resolve_active_caliber(metric_name)
        old_version = (application.get("old_version")
                       or (active_caliber.get("版本号") if active_caliber else "unknown"))
        attempt_no = len(store.recalc_attempts(entry_id)) + 1
        attempt = store.add_recalc_attempt({
            "task_id": entry_id,
            "application_id": application["id"],
            "attempt_no": attempt_no,
            "metric": metric_name,
            "old_version": old_version,
            "new_version": target_version,
            "target_caliber_id": target.get("id"),
            "time_range": time_range,
            "status": "重算中",
            "diff_count": 0,
            "reason": "",
        })

        details = self._produce_window_details(
            task=task, application=application, attempt=attempt,
            start_date=start_date, end_date=end_date,
            metric_name=metric_name, old_version=old_version,
            target_version=target_version,
        )
        in_window = [d for d in details if d.get("在窗内")]
        diff_total = sum(float(d.get("差额", 0)) for d in in_window)

        attempt["status"] = "已完成"
        attempt["diff_count"] = len(in_window)
        attempt["diff_total"] = diff_total
        attempt["diff_summary"] = (
            f"口径从 {old_version} 变更为 {target_version}，窗口 {time_range} "
            f"内差异 {len(in_window)} 笔，合计差额 {diff_total:g}"
        )

        task["status"] = "已完成"
        task["重算状态"] = "已完成"
        task["pending"] = False
        task["abnormal"] = False
        task["产出差异"] = attempt["diff_summary"]
        application["状态"] = "已完成"

        # 重算完成后失效该指标查询缓存
        from app.services.cache_layer import CacheLayerService
        invalidation = CacheLayerService().invalidate_by_metric(metric_name)

        return {
            "task_id": entry_id,
            "application_id": application["id"],
            "attempt_id": attempt["id"],
            "attempt_no": attempt_no,
            "metric": metric_name,
            "old_caliber": old_version,
            "new_caliber": target_version,
            "time_range": time_range,
            "status": "已完成",
            "message": attempt["diff_summary"],
            "diff_count": len(in_window),
            "diff_total": diff_total,
            "details": details,
            "cache_invalidated": invalidation.get("invalidated_entries", 0),
        }

    def cancel_recalc(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if task.get("status") == "已完成":
            return None, "已完成的重算任务不能取消"
        task["status"] = "重算失败"
        task["重算状态"] = "重算失败"
        task["pending"] = False
        task["abnormal"] = True
        application = self._application_of(task)
        if application is not None and application.get("状态") != "已完成":
            application["状态"] = "重算失败"
        return task, "重算任务已取消并标记为重算失败"

    # ── 历史与差异消费 ──

    def get_recalc_history(self, metric_name: str | None = None) -> list[dict[str, Any]]:
        """历史列表：按任务与尝试回写，保留首次失败与实际出账身份。"""
        history = store.recalc_history()
        if metric_name:
            history = [h for h in history if h.get("关联指标") == metric_name]
        return history

    def get_available_diffs(self, metric_name: str | None = None,
                            record_date: str | None = None) -> dict[str, Any]:
        """报告消费面：仅返回获准且实际完成的窗口差异，空态/其他指标说明原因。"""
        diffs = store.available_recalc_diffs(metric_name=metric_name,
                                             record_date=record_date)
        if not diffs:
            if metric_name and not store.find_by_field(
                    "metric_definition", "指标名称", metric_name):
                reason = f"指标「{metric_name}」不存在，没有可用差异"
            elif metric_name:
                reason = (f"指标「{metric_name}」当前没有获准且实际完成的窗口差异"
                          "（可能尚未重算、目标属于其他指标或变更落在窗口外）")
            else:
                reason = "暂无已完成的重算窗口差异"
            return {"items": [], "total": 0, "reason": reason}
        return {
            "items": diffs,
            "total": len(diffs),
            "diff_total": sum(float(d.get("差额", 0)) for d in diffs),
            "reason": "",
        }

    def estimate_recalc_impact(self, entry_id: int) -> dict[str, Any] | None:
        """预估算重算影响范围。

        跨模块数据流: recalc_engine -> query_builder（预估缓存失效范围）
                     recalc_engine -> data_service（预估接口影响）
        """
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return {"error": f"重算任务 {entry_id} 不存在"}

        metric_name = task.get("关联指标", "")

        # 查找关联的查询模板和服务接口
        queries = store.find_by_field("query_builder", "关联指标", metric_name)
        services = [row for row in store.rows("data_service")
                    if metric_name in str(row.get("关联指标或模型", ""))]
        subscriptions = store.find_by_field("data_subscription", "关联指标", metric_name)

        return {
            "metric": metric_name,
            "affected_query_templates": len(queries),
            "affected_service_apis": len(services),
            "affected_subscriptions": len(subscriptions),
            "estimated_cache_invalidation": len([q for q in queries if q.get("缓存策略")]),
            "recommended_action": "confirm_and_proceed",
        }

    # ── 内部辅助 ──

    def _application_of(self, task: dict[str, Any]) -> dict[str, Any] | None:
        application_id = task.get("申请ID")
        if application_id is None:
            return None
        return store.find_recalc_application(int(application_id))

    def _fail_attempt(self, task: dict[str, Any], application: dict[str, Any],
                      entry_id: int, metric_name: str, target_version: str,
                      time_range: str, reason: str) -> dict[str, Any]:
        """首次/重试启动失败：任务留失败态，尝试单独留痕，申请保留可重试身份。"""
        task["status"] = "重算失败"
        task["重算状态"] = "重算失败"
        task["pending"] = False
        task["abnormal"] = True
        task["产出差异"] = ""
        if application.get("状态") != "已结束":
            application["状态"] = "重算失败"

        attempt_no = len(store.recalc_attempts(entry_id)) + 1
        attempt = store.add_recalc_attempt({
            "task_id": entry_id,
            "application_id": application["id"],
            "attempt_no": attempt_no,
            "metric": metric_name,
            "old_version": application.get("old_version", "unknown"),
            "new_version": target_version,
            "target_caliber_id": application.get("target_caliber_id"),
            "time_range": time_range,
            "status": "重算失败",
            "diff_count": 0,
            "diff_total": 0,
            "reason": reason,
        })
        return {
            "error": reason,
            "task_id": entry_id,
            "application_id": application["id"],
            "attempt_id": attempt["id"],
            "attempt_no": attempt_no,
            "status": "重算失败",
            "diff_count": 0,
            "details": [],
        }

    def _produce_window_details(self, *, task: dict[str, Any],
                                application: dict[str, Any], attempt: dict[str, Any],
                                start_date: date, end_date: date,
                                metric_name: str, old_version: str,
                                target_version: str) -> list[dict[str, Any]]:
        """为真实出账窗口生成重算结果明细，并按任务与尝试回写。"""
        details: list[dict[str, Any]] = []
        for change in store.recalc_change_ledger():
            if change.get("指标") != metric_name:
                # 仅属于另一指标的有效版次/变更不参与本任务出账
                continue
            try:
                record_date = date.fromisoformat(str(change.get("记录日期")))
            except ValueError:
                continue
            in_window = start_date <= record_date <= end_date
            old_value = float(change.get("旧值", 0))
            new_value = float(change.get("新值", 0))
            detail = store.add_recalc_detail({
                "task_id": task["id"],
                "attempt_id": attempt["id"],
                "attempt_no": attempt["attempt_no"],
                "application_id": application["id"],
                "指标": metric_name,
                "记录日期": record_date.isoformat(),
                "旧值": old_value,
                "新值": new_value,
                "差额": new_value - old_value,
                "旧口径": old_version,
                "新口径": target_version,
                "目标口径版本ID": application.get("target_caliber_id"),
                "在窗内": in_window,
                "已完成": True,
                "时间范围": task.get("历史时间范围"),
            })
            details.append(detail)
        return details
