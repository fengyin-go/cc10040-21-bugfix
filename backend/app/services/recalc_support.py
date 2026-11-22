"""口径重算共享校验：登记与启动沿同一调用链复用，避免两处口径漂移。

只依赖 store，不反向依赖具体领域服务，杜绝循环导入。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.store import store

_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")


def parse_date_range(time_range: str) -> tuple[date | None, date | None, str]:
    """解析历史时间范围，要求恰好两个日期且起不晚于止；倒序日期直接拒绝。"""
    text = str(time_range or "").strip()
    matches = _DATE_RE.findall(text)
    if len(matches) != 2:
        return None, None, "历史时间范围必须包含开始和结束两个日期（示例：2026-11-01 到 2026-11-30）"
    try:
        start = date(int(matches[0][0]), int(matches[0][1]), int(matches[0][2]))
        end = date(int(matches[1][0]), int(matches[1][1]), int(matches[1][2]))
    except ValueError as exc:
        return None, None, f"历史时间范围含非法日期：{exc}"
    if start > end:
        return None, None, "历史时间范围开始日期不能晚于结束日期（倒序日期不予调度）"
    return start, end, ""


def find_target_caliber(metric_name: str, target_version: str) -> dict[str, Any] | None:
    """定位申请所指的目标口径版次（不限状态，状态由调用方判定）。"""
    for caliber in store.find_by_field("caliber_version", "所属指标", metric_name):
        if str(caliber.get("版本号", "")).strip() == str(target_version or "").strip():
            return caliber
    return None


def resolve_active_caliber(metric_name: str) -> dict[str, Any] | None:
    calibers = store.find_by_field("caliber_version", "所属指标", metric_name)
    active = [c for c in calibers if c.get("status") == "已生效"]
    return active[0] if active else None


def evaluate_target(metric_name: str, target_version: str) -> tuple[dict[str, Any] | None, str]:
    """目标版次资格判定，返回 (目标版次或 None, 不获准原因)。"""
    if not store.find_by_field("metric_definition", "指标名称", metric_name):
        return None, f"指标「{metric_name}」不存在，无法发起口径重算"
    caliber = find_target_caliber(metric_name, target_version)
    if caliber is None:
        return None, (
            f"目标口径版本「{target_version}」尚未登记/发布，重算申请不予受理；"
            "待新版本发布后可从原任务入口重试"
        )
    if caliber.get("status") != "已生效":
        return caliber, (
            f"目标口径版本「{target_version}」当前为{caliber.get('status')}，"
            "尚未发布生效，重算暂不具备启动资格"
        )
    return caliber, ""
