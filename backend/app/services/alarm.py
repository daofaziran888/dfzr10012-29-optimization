"""流告警规则业务规则：状态流转、字段校验与筛选口径都收在这里。

批次水位与单个采集器暂停状态冲突时，告警侧只能使用当前生效的批次水位数据记录：
监控指标（采集器编号）落在生效暂停批次里的规则，既不能启用、也不能触发，
避免一边暂停批次一边继续触发规则。
"""
from __future__ import annotations

from typing import Any

from app.services.batch_watermark import batch_watermark_service
from app.store import store

MODULE = "alarm"
REQUIRED_FIELDS = ["规则编号", "监控指标", "告警条件"]
STATUS_ORDER = ["草稿", "已启用", "已暂停", "已关闭"]
ACTION_RULES = {"启用告警": "草稿", "暂停告警": "已启用", "关闭告警": "已暂停"}
NEGATIVE_ACTIONS = []


class AlarmService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"告警规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于流告警规则可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "启用告警":
            blocked = self.evaluate_rule(str(entry.get("监控指标", "")))
            if blocked is not None:
                return None, (
                    f"监控指标 {entry.get('监控指标')} 已被生效批次水位 {blocked['批次编号']} "
                    f"（版本号 {blocked['版本号']}）暂停，规则不得启用或触发"
                )
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"告警规则已{action}"

    def evaluate_rule(self, metric_name: str) -> dict[str, Any] | None:
        """告警触发前的批次水位校验：返回生效批次水位则必须拦截，返回 None 才允许触发。

        只采用当前生效（已提交、未关闭）的批次水位数据记录；已关闭批次不再拦截。
        """
        return batch_watermark_service.active_record_for_metric(metric_name)

    def trigger_check(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """模拟一次告警规则触发处理，校验是否与生效批次水位冲突。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"告警规则 {entry_id} 不存在或已归档"
        blocked = self.evaluate_rule(str(entry.get("监控指标", "")))
        if blocked is not None:
            return blocked, (
                f"规则 {entry.get('规则编号')} 未触发：监控指标 {entry.get('监控指标')} "
                f"处于批次 {blocked['批次编号']} 的生效暂停水位（版本号 {blocked['版本号']}），"
                "批次暂停期间不得继续触发规则"
            )
        return None, f"规则 {entry.get('规则编号')} 当前无生效批次水位冲突，可正常触发"
