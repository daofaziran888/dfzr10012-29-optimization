"""流作业指标采集接口：维护指标采集器，覆盖启动采集、暂停采集、关闭采集等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.metric import MetricService

router = APIRouter(prefix="/api/metric", tags=["流作业指标采集"])

service = MetricService()

LIST_FIELDS = ["采集器编号", "作业名称", "指标名称", "采集间隔", "聚合方式", "保留天数", "最近采集", "采集器状态"]
STATUSES = ["草稿", "采集中", "已暂停", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按采集器编号检索"),
    status: str | None = Query(default=None, description="草稿、采集中、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按采集器编号与状态过滤流作业指标采集列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 固定路径必须声明在 /{entry_id} 之前，否则会被动态路径吞掉
@router.get("/batch-watermarks", response_model=PageResult[dict])
def list_batch_watermarks() -> PageResult[dict]:
    """查看批次水位数据记录：可核对已完成采集器、失败项与续做位置。"""
    items = service.list_batch_watermarks()
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.post("/batch-pause", response_model=ActionResult)
def pause_batch(payload: EntryPayload) -> ActionResult:
    """整组暂停一批指标采集器并写入批次水位数据记录。

    入参（values）：
    - batch_no：批次编号，如 batch-29，同批次全链路只有一条记录；
    - collector_nos：本次整组处理的采集器编号列表；
    - expected_version：可选，续做时带入消费侧版本号做乐观锁校验；
    - simulate_failed：可选，模拟写入失败的采集器编号，用于失败恢复核对。
    """
    values = payload.values
    batch_no = str(values.get("batch_no") or "").strip()
    collector_nos = values.get("collector_nos") or []
    if not isinstance(collector_nos, list):
        return ActionResult(ok=False, message="collector_nos 必须是采集器编号数组，整组一次提交")
    expected_version = values.get("expected_version")
    simulate_failed = values.get("simulate_failed") or []
    if not isinstance(simulate_failed, list):
        return ActionResult(ok=False, message="simulate_failed 必须是采集器编号数组")
    record, message = service.pause_batch(
        batch_no,
        [str(no) for no in collector_nos],
        expected_version=int(expected_version) if expected_version is not None else None,
        simulate_failed=[str(no) for no in simulate_failed],
    )
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=record)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出流作业指标采集清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "metric", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条指标采集器明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"指标采集器 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条指标采集器，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="指标采集器已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条指标采集器执行启动采集、暂停采集、关闭采集；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
