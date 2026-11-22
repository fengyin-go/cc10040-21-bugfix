"""报告模板领域服务：管理定期分析报告的模板、指标组合和自动分发。

跨模块数据流: pivot_table -> report_template
             metric_definition -> query_builder -> report_template（包含指标查询结果）
"""
from __future__ import annotations

import time
from typing import Any

from app.store import store
from app.services.base import BaseModuleService


class ReportTemplateService(BaseModuleService):
    MODULE = "report_template"
    ENTITY = "报告模板"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["模板名称", "包含指标集", "报告周期", "输出格式", "分发渠道"]
    REQUIRED_FIELDS = ["模板名称", "包含指标集", "报告周期"]
    STATUS_ORDER = ["草稿", "已发布", "已停用"]
    ACTION_RULES = {"发布模板": "已发布", "停用模板": "已停用"}
    NEGATIVE_ACTIONS = ["停用模板"]

    def generate_report(self, entry_id: int) -> dict[str, Any] | None:
        """生成报告：收集关联指标的数据并汇总。

        跨模块数据流: query_builder -> data_service -> report_template
        """
        template = store.find(self.MODULE, entry_id)
        if template is None:
            return {"error": f"报告模板 {entry_id} 不存在"}
        if template["status"] != "已发布":
            return {"error": f"报告模板 {entry_id} 未发布"}

        metric_set = template.get("包含指标集", "")
        metric_names = [m.strip() for m in metric_set.split(",") if m.strip()]

        sections = []
        for metric_name in metric_names:
            # 获取查询结果
            queries = store.find_by_field("query_builder", "关联指标", metric_name)
            query_results = []
            for q in queries:
                if q.get("status") == "已发布":
                    from app.services.query_builder import QueryBuilderService
                    builder = QueryBuilderService()
                    result = builder.execute_query(int(q.get("id", 0)))
                    if "rows" in result and result["rows"]:
                        # 将 rows 转为可序列化的 dict list
                        serialized_rows = [dict(r) if not isinstance(r, dict) else r for r in result["rows"]]
                        query_results.append({
                            "template": q.get("模板名称"),
                            "source": result.get("source", "computed"),
                            "row_count": len(result["rows"]),
                            "sample_rows": serialized_rows[:3],
                        })

            # 获取口径版本信息
            calibers = store.find_by_field("caliber_version", "所属指标", metric_name)
            active_caliber = next((c for c in calibers if c.get("status") == "已生效"), None)

            # 报告只消费获准且实际完成的窗口差异（窗内、指标与记录日期一致）
            from app.services.recalc_engine import RecalcEngineService
            recalc_payload = RecalcEngineService().get_available_diffs(metric_name)
            recalc_diffs = recalc_payload.get("items", [])

            sections.append({
                "metric": metric_name,
                "caliber_version": active_caliber.get("版本号") if active_caliber else "未知",
                "caliber_desc": active_caliber.get("口径描述") if active_caliber else "未知",
                "query_results_count": len(query_results),
                "query_results": query_results,
                "recalc_diff_count": len(recalc_diffs),
                "recalc_diff_total": recalc_payload.get("diff_total", 0),
                "recalc_diffs": recalc_diffs,
                "recalc_diff_reason": recalc_payload.get("reason", ""),
            })

        return {
            "report_template": template.get("模板名称"),
            "period": template.get("报告周期"),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "format": template.get("输出格式", "HTML"),
            "sections": sections,
            "total_metrics": len(sections),
        }