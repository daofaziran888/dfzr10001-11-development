"""采集数据存储接口：维护存储配置，覆盖提交校验、启用配置、停用配置等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dedup_precheck import DedupPrecheckService
from app.services.storage import StorageService

router = APIRouter(prefix="/api/storage", tags=["采集数据存储"])

service = StorageService()
precheck_service = DedupPrecheckService()

LIST_FIELDS = ["存储编号", "存储类型", "连接串", "压缩方式", "分区策略", "保留天数", "备份策略", "是否启用"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按存储编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按存储编号与状态过滤采集数据存储列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集数据存储清单：返回当前过滤条件下的全量数据。

    必须声明在 ``/{entry_id}`` 之前，否则单段路径会被 int 型入口参数先匹配。
    """
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "storage", "total": total, "items": items}


@router.get("/dedup-removals")
def dedup_removals(
    rule_code: str | None = Query(default=None, description="按去重规则编号过滤"),
) -> dict[str, Any]:
    """采集数据存储清单中的去重剔除追溯记录：预检确认后生成，带命中窗口边界。

    数据与「采集去重 - 剔除追溯清单」同源，存储侧只做只读联动展示。
    """
    items = precheck_service.list_removals(rule_code=rule_code)
    return {
        "total": len(items),
        "items": items,
        "说明": "去重预检确认后的剔除追溯记录，按候选组编号分段，含命中窗口边界",
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条存储配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"存储配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条存储配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="存储配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条存储配置执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
