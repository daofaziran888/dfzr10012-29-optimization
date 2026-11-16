"""流作业指标采集业务规则：状态流转、字段校验与筛选口径都收在这里。

批量暂停在单条暂停之外新增「批次水位数据记录」链路：

- 整组处理多条采集器，一次提交只写一条批次结果（见 batch_watermark 服务）；
- 同一批次并发暂停只允许一个任务，重复请求直接拒绝，不产生重复写入；
- 已完成采集器只做并集去重，恢复/重试时不再写入、不被覆盖；
- 失败批次从最后确认水位（续做位置=已完成数量，即批次末尾）继续，
  且沿用首次固化的聚合方式与保留天数，保留原聚合口径；
- 批次水位与单个采集器暂停冲突时，以当前生效批次水位为准，
  采集器单暂停会被拦下，避免一边暂停批次一边继续触发单条状态变更。
"""
from __future__ import annotations

from typing import Any

from app.services.batch_watermark import (
    BatchWatermarkConflict,
    batch_watermark_service,
)
from app.store import store

MODULE = "metric"
REQUIRED_FIELDS = ["采集器编号", "作业名称", "指标名称"]
STATUS_ORDER = ["草稿", "采集中", "已暂停", "已关闭"]
ACTION_RULES = {"启动采集": "草稿", "暂停采集": "采集中", "关闭采集": "已暂停"}
NEGATIVE_ACTIONS = []

#: 采集器允许的聚合方式与保留天数；批量暂停时按此口径校验并固化
ALLOWED_AGGREGATIONS = {"avg", "sum"}
ALLOWED_RETENTION_DAYS = {7, 14}


class MetricService:
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
            rows = [row for row in rows if keyword in str(row.get("采集器编号", ""))]
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
        with store.lock:
            entry = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标采集器 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于流作业指标采集可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        # 批次水位与单采集器暂停冲突：生效批次期间只认批次水位，不允许单条暂停
        if action == "暂停采集":
            active = batch_watermark_service.active_record_for_collector(str(entry.get("采集器编号", "")))
            if active is not None:
                return None, (
                    f"采集器 {entry.get('采集器编号')} 已被批次 {active['批次编号']} "
                    f"（版本号 {active['版本号']}，批次状态 {active['批次状态']}）的水位记录接管，"
                    "批次暂停期间不允许单独暂停，请在批次链路中处理"
                )
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"指标采集器已{action}"

    # ---- 批量暂停 -------------------------------------------------------
    def list_batch_watermarks(self) -> list[dict[str, Any]]:
        return batch_watermark_service.list_records()

    def get_batch_watermark(self, batch_no: str) -> dict[str, Any] | None:
        return batch_watermark_service.get_record(batch_no)

    def pause_batch(
        self,
        batch_no: str,
        collector_nos: list[str],
        *,
        expected_version: int | None = None,
        simulate_failed: list[str] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """整组暂停一批采集器，并把结果写入批次水位数据记录。

        参数：
        - batch_no：批次编号，如 batch-29，同一批次全链路只对应一条记录；
        - collector_nos：本次提交要处理的采集器编号（整组多条）；
        - expected_version：消费侧带入的版本号，续做时校验，过期则拒绝；
        - simulate_failed：模拟本次写入失败的采集器编号，落入失败项并保留续做位置。
        """
        batch_no = str(batch_no or "").strip()
        if not batch_no:
            return None, "批次编号不能为空"
        collector_nos = [str(no).strip() for no in collector_nos if str(no).strip()]
        if not collector_nos:
            return None, "批次至少要包含一个指标采集器"
        simulate_failed = set(simulate_failed or [])

        with store.lock:
            # 1) 并发互斥：同一批次同时只允许一个暂停任务
            try:
                record = batch_watermark_service.create_processing(batch_no, collector_nos)
            except BatchWatermarkConflict as exc:
                return None, str(exc)

            # 2) 续做版本校验：消费侧带入版本号，与最后确认水位不一致即拒绝
            if expected_version is not None and int(expected_version) != int(record.get("版本号", 0)):
                return None, (
                    f"批次 {batch_no} 版本号已过期：带入 {expected_version}，"
                    f"当前 {record.get('版本号')}，请按最后确认水位续做"
                )

            # 3) 失败恢复：已完成采集器不再写入，未确认项从续做位置（批次末尾）继续
            done = list(record.get("已完成采集器", []))
            pending_nos = [no for no in collector_nos if no not in done]

            # 幂等：本次整组都已确认完成时不再写入，版本号与批次结果都不变
            if record.get("批次状态") != "处理中" and not pending_nos:
                if not record.get("失败项"):
                    return record, f"批次 {batch_no} 全部采集器已完成，重复请求不重复写入"

            completed: list[str] = []
            failed: list[dict[str, str]] = []
            aggregation: dict[str, dict[str, Any]] = {}

            for no in pending_nos:
                entry = store.find_by_field(MODULE, "采集器编号", no)
                if entry is None:
                    failed.append({"采集器编号": no, "失败原因": "采集器不存在或已归档"})
                    continue
                agg = str(entry.get("聚合方式", "")).strip()
                retention_raw = entry.get("保留天数")
                try:
                    retention = int(retention_raw)
                except (TypeError, ValueError):
                    retention = None
                # 口径校验：只接受 avg/sum 与 7/14 天保留期，不满足计入失败项，不动采集器状态
                if agg not in ALLOWED_AGGREGATIONS:
                    failed.append({"采集器编号": no, "失败原因": f"聚合方式 {agg or '空'} 不在 avg/sum 允许范围"})
                    continue
                if retention not in ALLOWED_RETENTION_DAYS:
                    failed.append({"采集器编号": no, "失败原因": f"保留天数 {retention_raw} 不在 7/14 天允许范围"})
                    continue
                if no in simulate_failed:
                    failed.append({"采集器编号": no, "失败原因": "批次写入失败，等待按最后确认水位续做"})
                    continue

                # 固化聚合口径：首次写入后不再变化，续做保留原聚合方式与保留天数
                aggregation[no] = {"聚合方式": agg, "保留天数": retention}
                entry["status"] = "已暂停"
                entry["pending"] = False
                completed.append(no)

            # 4) 一次提交整组落一条批次结果；已完成项并集去重，失败项保留并推进续做位置
            saved = batch_watermark_service.commit(
                batch_no,
                collector_nos=collector_nos,
                completed=completed,
                failed=failed,
                aggregation=aggregation,
                result_detail={
                    "跳过已完成": [no for no in collector_nos if no in done],
                    "续做说明": "失败项从最后确认水位（续做位置，批次末尾）继续，沿用原聚合口径",
                },
            )

        if failed:
            message = (
                f"批次 {batch_no} 部分失败：已完成 {len(saved['已完成采集器'])} 个，"
                f"失败 {len(saved['失败项'])} 个，续做位置 {saved['续做位置']}，"
                "失败项将从批次末尾按原聚合口径续做"
            )
        else:
            message = (
                f"批次 {batch_no} 暂停完成：共 {len(saved['已完成采集器'])} 个采集器，"
                f"版本号 {saved['版本号']}"
            )
        return saved, message
