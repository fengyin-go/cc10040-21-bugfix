"""指标定义领域服务：管理业务指标的计算口径、聚合维度和数据源映射。

作为度量链的起点，指标定义被口径版本、查询构建器、数据服务等多模块引用。
"""
from __future__ import annotations

import time
from typing import Any

from app.store import store
from app.services.base import BaseModuleService
from app.services.recalc_support import (
    find_target_caliber,
    parse_date_range,
    resolve_active_caliber,
)


class MetricDefinitionService(BaseModuleService):
    MODULE = "metric_definition"
    ENTITY = "指标"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["指标名称", "计算口径", "统计粒度", "数据源", "业务域"]
    REQUIRED_FIELDS = ["指标名称", "计算口径", "统计粒度"]
    STATUS_ORDER = ["草稿", "已发布", "已停用"]
    ACTION_RULES = {"发布指标": "已发布", "停用指标": "已停用"}
    NEGATIVE_ACTIONS = ["停用指标"]

    def publish_with_caliber(self, entry_id: int, caliber_desc: str) -> tuple[dict[str, Any] | None, str]:
        """发布指标并自动创建初始口径版本。

        跨模块数据流: metric_definition -> caliber_version
        已发布指标必须有至少一个已生效口径版本，查询构建器才能引用。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        if entry["status"] != "草稿":
            return None, f"{self.ENTITY} {entry_id} 只能从草稿状态发布"

        entry["status"] = "已发布"
        entry["pending"] = True
        entry["abnormal"] = False

        # 自动创建口径版本文档
        version_id = store.next_id("caliber_version")
        timestamp = time.strftime("%Y-%m-%d")
        caliber = {
            "id": version_id,
            "所属指标": entry.get("指标名称", ""),
            "版本号": "v1.0",
            "口径描述": caliber_desc,
            "变更类型": "初始创建",
            "生效时间": timestamp,
            "status": "已生效",
            "pending": True,
            "abnormal": False,
        }
        store.rows("caliber_version").append(caliber)
        return entry, f"指标已发布并创建口径版本 v1.0"

    def get_metric_chain(self, entry_id: int) -> dict[str, Any] | None:
        """获取指标完整数据链：指标 -> 口径 -> 查询模板 -> 数据服务。

        跨模块数据流: metric_definition -> caliber_version -> query_builder -> data_service
        """
        metric_data = store.get_metric_data(entry_id)
        if metric_data is None:
            return None
        metric = metric_data["metric"]
        chain = {
            "metric_id": entry_id,
            "metric_name": metric.get("指标名称", ""),
            "caliber_count": len(metric_data["calibers"]),
            "caliber_versions": [
                {"id": c["id"], "version": c.get("版本号", ""),
                 "status": c.get("status", ""), "desc": c.get("口径描述", "")}
                for c in metric_data["calibers"]
            ],
            "query_templates": len(metric_data["queries"]),
            "service_apis": len(metric_data["services"]),
        }
        return chain

    def recalc_metric_history(self, entry_id: int, new_caliber_version: str,
                              time_range: str) -> tuple[dict[str, Any] | None, str]:
        """登记指标历史数据重算申请（登记到执行链路的入口）。

        跨模块数据流: metric_definition -> caliber_version(资格校验) -> recalc_engine
        登记阶段只做校验并建立申请+待调度任务，不产生任何差异记录；
        目标口径版本尚不存在时直接拒绝，倒序日期不进入调度。
        """
        metric = store.find(self.MODULE, entry_id)
        if metric is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        metric_name = metric.get("指标名称", "")
        target_version = str(new_caliber_version or "").strip()
        if not target_version:
            return None, "新口径版本不能为空"

        # 已校验的日期范围（倒序/缺日期直接拒绝，申请不会进入调度）
        start_date, end_date, date_error = parse_date_range(time_range)
        if date_error:
            return None, date_error

        # 目标版次必须已经登记；未发布则受理申请但首次启动会失败，可待发布后重试
        target_caliber = find_target_caliber(metric_name, target_version)
        if target_caliber is None:
            return None, (
                f"目标口径版本「{target_version}」尚不存在，重算申请不予受理；"
                "请先在口径版本模块登记并发布该版本"
            )

        active_caliber = resolve_active_caliber(metric_name)
        old_version = active_caliber.get("版本号", "unknown") if active_caliber else "unknown"

        # 登记重算申请：承载指标身份、目标版次和已校验日期范围
        application = store.save_recalc_application({
            "metric_id": entry_id,
            "metric": metric_name,
            "target_caliber_id": int(target_caliber.get("id", 0)),
            "target_version": target_version,
            "old_version": old_version,
            "time_range": str(time_range).strip(),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "状态": "待调度",
            "关联任务ID": None,
            "来源申请ID": None,
        })

        # 同一调用链上创建待调度任务，引擎启动时凭申请身份执行
        task_id = store.next_id("recalc_engine")
        task = {
            "id": task_id,
            "申请ID": application["id"],
            "关联指标": metric_name,
            "新口径版本": target_version,
            "历史时间范围": str(time_range).strip(),
            "重算状态": "待调度",
            "产出差异": "",
            "status": "待调度",
            "pending": True,
            "abnormal": False,
        }
        store.rows("recalc_engine").append(task)
        application["关联任务ID"] = task_id

        reason = ""
        if target_caliber.get("status") != "已生效":
            reason = (f"目标口径版本「{target_version}」尚未发布生效，"
                      "任务已受理但首次启动将失败，发布后可从原任务入口重试")
        message = f"已登记重算申请 #{application['id']} 并创建待调度任务 #{task_id}"
        if reason:
            message = f"{message}；{reason}"
        return task, message

    def close_and_reregister_recalc(self, entry_id: int, new_caliber_version: str,
                                    time_range: str,
                                    previous_application_id: int) -> tuple[dict[str, Any] | None, str]:
        """结束原申请并登记关联任务出账（重试路径之二）。

        原申请保留首次失败身份并标记已结束，新申请登记关联关系后独立出账。
        """
        previous = store.find_recalc_application(previous_application_id)
        if previous is None:
            return None, f"原重算申请 {previous_application_id} 不存在"
        previous["状态"] = "已结束"
        previous_task_id = previous.get("关联任务ID")
        if previous_task_id:
            previous_task = store.find("recalc_engine", int(previous_task_id))
            if previous_task is not None and previous_task.get("status") != "已完成":
                previous_task["status"] = "重算失败"
                previous_task["重算状态"] = "重算失败"
                previous_task["pending"] = False
                previous_task["abnormal"] = True

        task, message = self.recalc_metric_history(entry_id, new_caliber_version, time_range)
        if task is None:
            return None, message
        application = store.find_recalc_application(int(task["申请ID"]))
        application["来源申请ID"] = previous_application_id
        return task, f"原申请 #{previous_application_id} 已结束，{message}（关联自原申请）"