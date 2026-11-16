"""批次水位数据记录的共享读写口径。

这张表（store.BATCH_WATERMARK_TABLE）被三个模块共用：

1. 指标采集（metric）：批量暂停采集器时整组写入一条批次水位，保留
   已完成采集器、失败项与续做位置，一次提交只记录一次批次结果；
2. 水位线管理（watermark）：作为「另一个业务模块」按版本号同步写入同一条记录；
3. 流告警规则（alarm）：只消费当前生效（已提交且未关闭）的批次水位。

记录沿用仓库既有字段风格（中文业务字段 + id/status/pending/abnormal），新增：

- 批次编号：批次主键，例如 batch-29，同一批次只有一条记录；
- 版本号：每次提交自增，跨模块同步写入时做乐观锁校验，竞态只允许一项生效；
- 已完成采集器：已暂停成功的采集器编号列表，恢复时不再重复写入、不被覆盖；
- 失败项：本次/历次提交失败的采集器编号及原因，失败批次从最后确认水位续做；
- 续做位置：最后确认水位，即已完成采集器数量，恢复时从批次末尾（该位置之后）继续；
- 聚合口径：按采集器编号固化聚合方式与保留天数，续做时保留原聚合口径；
- 批次结果：一次提交一条结果（成功/失败项/续做位置），整组处理多条数据。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.store import BATCH_WATERMARK_ACTIVE_STATUSES, BATCH_WATERMARK_TABLE, store

#: 一次批次提交的结果状态
RESULT_SUCCESS = "成功"
RESULT_PARTIAL = "部分失败"
#: 批次水位本身的状态：处理中（占坑，防并发）/已暂停/部分失败/已关闭
STATUS_PROCESSING = "处理中"
STATUS_PAUSED = "已暂停"
STATUS_PARTIAL = "部分失败"
STATUS_CLOSED = "已关闭"

#: 跨模块同步写入允许覆盖的字段；其余字段（已完成采集器、失败项、续做位置等）
#: 只能由批次处理链路按规则推进，外部模块不得改写。
SYNC_WRITABLE_FIELDS = ("批次备注",)


class BatchWatermarkConflict(Exception):
    """版本号不匹配：消费侧带入的版本已过期，本次写入不生效。"""


class BatchWatermarkService:
    """批次水位数据记录的唯一写入口，所有方法均在 store 锁内执行。"""

    # ---- 查询 -----------------------------------------------------------
    def get_record(self, batch_no: str) -> dict[str, Any] | None:
        with store.lock:
            record = store.find_by_field(BATCH_WATERMARK_TABLE, "批次编号", batch_no)
            return deepcopy(record) if record is not None else None

    def list_records(self) -> list[dict[str, Any]]:
        with store.lock:
            return [deepcopy(row) for row in store.rows(BATCH_WATERMARK_TABLE)]

    def active_record_for_collector(self, collector_no: str) -> dict[str, Any] | None:
        """返回仍约束某个采集器的生效批次水位（已关闭的批次不再生效）。

        一个采集器理论上只可能落在一个生效批次里；若历史数据出现多条，
        取版本号最大（最近一次提交）的一条，保证消费侧只用当前生效记录。
        """
        with store.lock:
            matched = [
                row for row in store.rows(BATCH_WATERMARK_TABLE)
                if row.get("批次状态") in BATCH_WATERMARK_ACTIVE_STATUSES
                and collector_no in (row.get("批次采集器") or [])
            ]
            if not matched:
                return None
            return deepcopy(max(matched, key=lambda row: int(row.get("版本号", 0))))

    def active_record_for_metric(self, metric_name: str) -> dict[str, Any] | None:
        """告警规则按监控指标（采集器编号）匹配生效批次水位。"""
        return self.active_record_for_collector(metric_name)

    # ---- 写入 -----------------------------------------------------------
    def create_processing(self, batch_no: str, collector_nos: list[str]) -> dict[str, Any]:
        """为一次批次暂停占坑。已存在处理中记录时拒绝，保证同批次并发只跑一个任务。"""
        with store.lock:
            existing = store.find_by_field(BATCH_WATERMARK_TABLE, "批次编号", batch_no)
            if existing is not None and existing.get("批次状态") == STATUS_PROCESSING:
                raise BatchWatermarkConflict(f"批次 {batch_no} 已有暂停任务在处理，并发请求仅允许一项生效")
            if existing is None:
                existing = {
                    "id": store.next_id(BATCH_WATERMARK_TABLE),
                    "status": STATUS_PROCESSING,
                    "pending": True,
                    "abnormal": False,
                    "批次编号": batch_no,
                    "版本号": 0,
                    "批次状态": STATUS_PROCESSING,
                    "批次采集器": list(collector_nos),
                    "已完成采集器": [],
                    "失败项": [],
                    "续做位置": 0,
                    "聚合口径": {},
                    "批次结果": [],
                    "提交次数": 0,
                    "批次备注": "",
                }
                store.rows(BATCH_WATERMARK_TABLE).append(existing)
            return deepcopy(existing)

    def commit(
        self,
        batch_no: str,
        *,
        collector_nos: list[str],
        completed: list[str],
        failed: list[dict[str, str]],
        aggregation: dict[str, dict[str, Any]],
        result_detail: dict[str, Any],
    ) -> dict[str, Any]:
        """一次批次暂停的整组提交：同一条记录只写一次批次结果，版本号自增。

        - 已完成采集器做并集去重，已完成项不被覆盖、不再重复写入；
        - 失败项按采集器编号去重保留最新原因，续做位置 = 最后确认水位
          （已完成采集器数量），恢复时从批次末尾该位置之后继续；
        - 聚合口径只在首次出现时固化，续做保留原聚合方式与保留天数；
        - 批次结果追加一条提交结果，并把最近一次结果同步到顶层状态字段。
        """
        with store.lock:
            record = store.find_by_field(BATCH_WATERMARK_TABLE, "批次编号", batch_no)
            if record is None:
                # 兜底：正常链路一定先 create_processing
                record = self.create_processing(batch_no, collector_nos)
                record = store.find_by_field(BATCH_WATERMARK_TABLE, "批次编号", batch_no)

            # 整组范围合并：新增采集器并入批次，不剔除历史项
            merged_collectors = list(dict.fromkeys([*record.get("批次采集器", []), *collector_nos]))
            record["批次采集器"] = merged_collectors

            # 已完成采集器：并集去重，已完成的不覆盖
            completed_set = [*record.get("已完成采集器", [])]
            for no in completed:
                if no not in completed_set:
                    completed_set.append(no)
            record["已完成采集器"] = completed_set

            # 失败项：按采集器编号去重；本次已成功的项从失败项移除，仍失败的保留最新原因
            failed_map = {item["采集器编号"]: item for item in record.get("失败项", [])}
            for no in completed:
                failed_map.pop(no, None)
            for item in failed:
                failed_map[item["采集器编号"]] = dict(item)
            record["失败项"] = list(failed_map.values())

            # 最后确认水位：已完成数量即续做位置，失败批次从该位置（批次末尾）继续
            record["续做位置"] = len(completed_set)

            # 聚合口径：首次固化，后续续做沿用原口径，不允许改写或重复写入
            caliber = dict(record.get("聚合口径", {}))
            for no, spec in aggregation.items():
                if no not in caliber:
                    caliber[no] = dict(spec)
            record["聚合口径"] = caliber

            has_failure = bool(record["失败项"])
            record["版本号"] = int(record.get("版本号", 0)) + 1
            record["提交次数"] = int(record.get("提交次数", 0)) + 1
            record["批次状态"] = STATUS_PARTIAL if has_failure else STATUS_PAUSED
            record["status"] = record["批次状态"]
            record["abnormal"] = has_failure
            record["pending"] = has_failure

            result = {
                "版本号": record["版本号"],
                "提交结果": RESULT_PARTIAL if has_failure else RESULT_SUCCESS,
                "本次采集器": list(collector_nos),
                "本次完成": list(completed),
                "本次失败": list(failed),
                "已完成采集器": list(completed_set),
                "失败项": list(record["失败项"]),
                "续做位置": record["续做位置"],
            }
            result.update(result_detail)
            record.setdefault("批次结果", []).append(result)
            return deepcopy(record)

    def mark_closed(self, batch_no: str) -> dict[str, Any] | None:
        """关闭批次水位：关闭后不再约束采集器单暂停与告警触发。"""
        with store.lock:
            record = store.find_by_field(BATCH_WATERMARK_TABLE, "批次编号", batch_no)
            if record is None:
                return None
            record["批次状态"] = STATUS_CLOSED
            record["status"] = STATUS_CLOSED
            record["pending"] = False
            record["版本号"] = int(record.get("版本号", 0)) + 1
            return deepcopy(record)

    def sync_write(self, batch_no: str, *, expected_version: int | None, values: dict[str, Any]) -> dict[str, Any]:
        """另一个业务模块（水位线管理）同步写入同一条批次水位记录。

        消费侧必须带入版本号：版本号与当前记录不一致即拒绝（乐观锁），
        保证竞态请求只允许一项生效；只允许写白名单字段，采集结果类字段不动。
        """
        with store.lock:
            record = store.find_by_field(BATCH_WATERMARK_TABLE, "批次编号", batch_no)
            if record is None:
                raise BatchWatermarkConflict(f"批次 {batch_no} 的水位数据记录不存在，无法同步写入")
            if expected_version is not None and int(expected_version) != int(record.get("版本号", 0)):
                raise BatchWatermarkConflict(
                    f"批次 {batch_no} 版本号已过期：带入 {expected_version}，当前 {record.get('版本号')}，本次写入不生效"
                )
            for field in SYNC_WRITABLE_FIELDS:
                if field in values:
                    record[field] = values[field]
            record["版本号"] = int(record.get("版本号", 0)) + 1
            record.setdefault("批次结果", []).append({
                "版本号": record["版本号"],
                "提交结果": "跨模块同步",
                "同步字段": [field for field in SYNC_WRITABLE_FIELDS if field in values],
            })
            return deepcopy(record)


batch_watermark_service = BatchWatermarkService()
