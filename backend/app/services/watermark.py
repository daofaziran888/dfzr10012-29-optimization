"""水位线管理业务规则：状态流转、字段校验与筛选口径都收在这里。

水位线模块同时作为「另一个业务模块」同步写入指标采集的批次水位数据记录：
必须带入消费侧版本号做乐观锁校验，版本过期即拒绝，竞态请求只允许一项生效，
且只允许更新白名单字段，已完成采集器、失败项、续做位置等结果字段不被改写。
"""
from __future__ import annotations

from typing import Any

from app.services.batch_watermark import BatchWatermarkConflict, batch_watermark_service
from app.store import store

MODULE = "watermark"
REQUIRED_FIELDS = ["配置编号", "流名称", "时间字段"]
STATUS_ORDER = ["草稿", "已启用", "已暂停", "已关闭"]
ACTION_RULES = {"启用水位线": "草稿", "暂停水位线": "已启用", "关闭水位线": "已暂停"}
NEGATIVE_ACTIONS = []


class WatermarkService:
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
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
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
            return None, f"水位线配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于水位线管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"水位线配置已{action}"

    # ---- 批次水位数据记录：跨模块同步写入 -------------------------------
    def list_batch_watermarks(self) -> list[dict[str, Any]]:
        return batch_watermark_service.list_records()

    def get_batch_watermark(self, batch_no: str) -> dict[str, Any] | None:
        return batch_watermark_service.get_record(batch_no)

    def sync_batch_watermark(
        self,
        batch_no: str,
        *,
        expected_version: int | None,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """按版本号同步写入另一个模块产生的同一条批次水位数据记录。"""
        try:
            record = batch_watermark_service.sync_write(
                str(batch_no or "").strip(),
                expected_version=expected_version,
                values=values or {},
            )
        except BatchWatermarkConflict as exc:
            return None, str(exc)
        return record, f"批次 {record['批次编号']} 已同步写入，当前版本号 {record['版本号']}"
