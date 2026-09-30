"""地球物理接口：维护物探测线，覆盖施测、采集、处理、归档与撤回改版。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.geophysics import ConcurrentUpdate, service

router = APIRouter(prefix="/api/geophysics", tags=["地球物理"])

LIST_FIELDS = ["测线编号", "勘探区", "物探方法", "测线长度", "点距", "施测日期", "数据质量"]
STATUSES = ["待施测", "施测中", "已采集", "数据处理", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按测线编号检索"),
    status: str | None = Query(default=None, description="待施测、施测中、已采集、数据处理、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按测线编号与状态过滤地球物理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/ledgers")
def get_ledgers() -> dict[str, Any]:
    """读取三处台账：勘探区采集台账、成果图清单、待处理列表，按同一修订号摆放。"""
    return service.ledgers()


@router.post("/migrate")
def migrate_legacy() -> ActionResult:
    """手动触发老测线迁移：缺失状态的按施测先后补齐；已迁移则幂等返回。"""
    result = service.migrate_legacy_rows()
    return ActionResult(ok=True, message=f"老测线迁移完成，补齐 {result['migrated']} 条")


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出地球物理清单：返回当前过滤条件下的全量数据。"""
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
    """登记一条物探测线，缺字段时说明原因而不是静默丢弃。"""
    entry, errors = service.create_entry(payload.values)
    if errors:
        return ActionResult(ok=False, message=f"登记未通过：{'、'.join(errors)}")
    return ActionResult(ok=True, message="物探测线已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条测线单向推进：只允许走到下一格，跳级、回退、并发重复提交一律拦下。

    可选传入 seq（操作序号）与 revision（本地修订号）：同序号重复提交按断点续跑
    返回原结果，旧序号/断号/修订号过期返回 409。
    """
    action = str(payload.values.get("action") or "").strip()
    seq = _as_int(payload.values.get("seq"))
    client_revision = _as_int(payload.values.get("revision"))
    line_no = _line_no_by_id(entry_id)
    try:
        entry, message, resumed, status_code = service.run_action(
            entry_id, action, seq=seq, client_revision=client_revision
        )
    except ConcurrentUpdate as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "ok": False,
                "message": str(exc),
                "entry": None,
                "resumed": False,
                "seq_head": service.seq_head(line_no),
            },
        )
    if entry is None:
        if status_code == 404:
            raise HTTPException(status_code=404, detail=message)
        return ActionResult(ok=False, message=message, seq_head=service.seq_head(line_no))
    return ActionResult(
        ok=True,
        message=message,
        entry=entry,
        resumed=resumed,
        seq_head=service.seq_head(line_no),
    )


@router.post("/{entry_id}/withdraw", response_model=ActionResult)
def withdraw_archived(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """撤回已归档测线：视为新修订版本从待施测重走，历史按原始上报口径封存。"""
    values = payload.values if payload is not None else {}
    seq = _as_int(values.get("seq"))
    client_revision = _as_int(values.get("revision"))
    line_no = _line_no_by_id(entry_id)
    try:
        entry, message, resumed, status_code = service.withdraw_archived(
            entry_id, seq=seq, client_revision=client_revision
        )
    except ConcurrentUpdate as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "ok": False,
                "message": str(exc),
                "entry": None,
                "resumed": False,
                "seq_head": service.seq_head(line_no),
            },
        )
    if entry is None:
        if status_code == 404:
            raise HTTPException(status_code=404, detail=message)
        return ActionResult(ok=False, message=message, seq_head=service.seq_head(line_no))
    return ActionResult(
        ok=True,
        message=message,
        entry=entry,
        resumed=resumed,
        seq_head=service.seq_head(line_no),
    )


def _line_no_by_id(entry_id: int) -> str:
    entry = service.get_entry(entry_id)
    return str(entry.get("测线编号", "")) if entry else ""


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"操作序号/修订号必须是整数，收到：{value!r}")
