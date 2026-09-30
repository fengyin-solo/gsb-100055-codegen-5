"""地球物理接口：维护物探测线的单向流转，并暴露三处同步口径与修订历史。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.geophysics import (
    LEDGER_TABLE,
    MAPS_TABLE,
    PENDING_TABLE,
    REVISIONS_TABLE,
    GeophysicsService,
)

router = APIRouter(prefix="/api/geophysics", tags=["地球物理"])

service = GeophysicsService()

LIST_FIELDS = ["测线编号", "勘探区", "物探方法", "测线长度", "点距", "施测日期", "数据质量", "测线状态"]
STATUSES = ["待施测", "施测中", "已采集", "数据处理", "已归档"]

# 内存仓库单例在导入时已就绪：这里幂等补齐修订号并迁移老测线状态
service.bootstrap()


class SubmitPayload(BaseModel):
    """流程提交：动作 + 客户端操作序号 + 提交时所持修订号（并发核对）。"""

    action: str
    seq: int | None = None
    expected_revision: int | None = None
    remark: str | None = None


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按测线编号检索"),
    status: str | None = Query(default=None, description="待施测、施测中、已采集、数据处理、已归档"),
    area: str | None = Query(default=None, description="按勘探区检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按测线编号、勘探区与状态过滤物探测线列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, area=area, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/status-summary")
def status_summary() -> dict[str, Any]:
    """各流程站位测线数量，供前端统计卡片使用。"""
    return {"counts": service.status_counts()}


@router.get("/ledger")
def ledger(keyword: str | None = None) -> dict[str, Any]:
    """勘探区采集台账：每次推进一行，按修订号排序。"""
    items = service.outlet(LEDGER_TABLE, keyword)
    return {"name": "勘探区采集台账", "total": len(items), "items": items}


@router.get("/maps")
def maps(keyword: str | None = None) -> dict[str, Any]:
    """成果图清单：当前版本一测线一行，挂最近修订号。"""
    items = service.outlet(MAPS_TABLE, keyword)
    return {"name": "成果图清单", "total": len(items), "items": items}


@router.get("/pending")
def pending(keyword: str | None = None) -> dict[str, Any]:
    """待处理列表：流程中的测线及下一步动作，归档后自动撤下。"""
    items = service.outlet(PENDING_TABLE, keyword)
    return {"name": "待处理列表", "total": len(items), "items": items}


@router.get("/revisions")
def revisions(keyword: str | None = None) -> dict[str, Any]:
    """修订历史：含撤回留档，历史长度与数据质量按原始上报口径留存。"""
    items = service.outlet(REVISIONS_TABLE, keyword)
    return {"name": "修订历史", "total": len(items), "items": items}


@router.post("/migrate-legacy", response_model=ActionResult)
def migrate_legacy() -> ActionResult:
    """老测线缺失状态迁移：按施测先后补齐（幂等，可重复执行）。"""
    count = service.migrate_legacy_statuses()
    if not count:
        return ActionResult(ok=True, message="没有需要迁移的老测线，状态已全部合规")
    return ActionResult(ok=True, message=f"已按施测先后为 {count} 条老测线补齐状态")


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出地球物理清单：返回全量物探测线数据。

    注意：必须声明在 `/{entry_id}` 之前，否则会被当成测线 id 解析。
    """
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "geophysics", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条物探测线明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"物探测线 {entry_id} 不存在")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条物探测线，缺字段或编号重复时说明原因而不是静默丢弃。"""
    entry, problems = service.create_entry(payload.values)
    if problems:
        return ActionResult(ok=False, message="；".join(problems) if len(problems) > 1 else f"缺少必填字段：{problems[0]}")
    return ActionResult(ok=True, message="物探测线已登记，进入「待施测」", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: SubmitPayload) -> ActionResult:
    """对单条测线提交流程动作。

    - 跳级、回退、非归档撤回都会被拦下并说明原因；
    - 带 seq 的重复提交按操作序号回放已落地结果，不重复推进；
    - expected_revision 与服务端不一致时返回 409，表示并发核对失败。
    """
    action = str(payload.action or "").strip()
    entry, message, conflict = service.submit(
        entry_id, action, seq=payload.seq, expected_revision=payload.expected_revision
    )
    if conflict:
        raise HTTPException(status_code=409, detail=message)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
