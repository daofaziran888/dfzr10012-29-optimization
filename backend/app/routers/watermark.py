"""水位线管理接口：维护水位线配置，覆盖启用水位线、暂停水位线、关闭水位线等动作。

同时作为「另一个业务模块」同步写入指标采集的批次水位数据记录：
消费侧带入版本号，版本过期拒绝写入，保证竞态请求只允许一项生效。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.watermark import WatermarkService

router = APIRouter(prefix="/api/watermark", tags=["水位线管理"])

service = WatermarkService()

LIST_FIELDS = ["配置编号", "流名称", "时间字段", "延迟容忍", "空闲超时", "发射间隔", "对齐策略", "配置状态"]
STATUSES = ["草稿", "已启用", "已暂停", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="草稿、已启用、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按配置编号与状态过滤水位线管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 固定路径必须声明在 /{entry_id} 之前
@router.get("/batch-watermarks", response_model=PageResult[dict])
def list_batch_watermarks() -> PageResult[dict]:
    """读取跨模块共享的批次水位数据记录（与指标采集模块同一份数据）。"""
    items = service.list_batch_watermarks()
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/batch-watermarks/{batch_no}", response_model=ActionResult)
def get_batch_watermark(batch_no: str) -> ActionResult:
    """按批次编号读取单条批次水位数据记录，供同步前获取当前版本号。"""
    record = service.get_batch_watermark(batch_no)
    if record is None:
        return ActionResult(ok=False, message=f"批次 {batch_no} 的水位数据记录不存在")
    return ActionResult(ok=True, message=f"批次 {batch_no} 当前版本号 {record['版本号']}", entry=record)


@router.post("/batch-watermarks/sync", response_model=ActionResult)
def sync_batch_watermark(payload: EntryPayload) -> ActionResult:
    """同步写入另一个模块的批次水位数据记录，必须带入 expected_version。

    入参（values）：
    - batch_no：批次编号；
    - expected_version：消费侧读取到的版本号，与当前不一致即拒绝；
    - 其余字段仅白名单（批次备注）可写，采集结果字段不允许跨模块改写。
    """
    values = payload.values
    batch_no = str(values.get("batch_no") or "").strip()
    expected_version = values.get("expected_version")
    if expected_version is None:
        return ActionResult(ok=False, message="同步写入批次水位必须带入版本号 expected_version")
    record, message = service.sync_batch_watermark(
        batch_no,
        expected_version=int(expected_version),
        values=values,
    )
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=record)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出水位线管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "watermark", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条水位线配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"水位线配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条水位线配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="水位线配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条水位线配置执行启用水位线、暂停水位线、关闭水位线；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
