"""采集去重接口：维护去重规则，并提供入库前去重预检、确认与追溯。

规则维护覆盖提交校验、启用配置、停用配置；预检接口覆盖候选组生成、中断续跑、
确认落地（剔除追溯、待入库条目、历史留档）与任务工作台联动。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dedup import DedupService
from app.services.dedup_precheck import DedupPrecheckService

router = APIRouter(prefix="/api/dedup", tags=["采集去重"])

service = DedupService()
precheck_service = DedupPrecheckService()

LIST_FIELDS = ["规则编号", "目标字段", "去重粒度", "时间窗口", "去重策略", "保留规则", "落地方式", "是否启用"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤采集去重列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集去重清单：返回当前过滤条件下的全量数据。

    注意：本路由必须声明在 ``/{entry_id}`` 之前，否则单段路径 export 会被
    int 型入口参数先匹配并返回 422。
    """
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dedup", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条去重规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"去重规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条去重规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="去重规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条去重规则执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


# ---------------------------------------------------------------- 去重预检

@router.get("/records/list", response_model=PageResult[dict])
def list_records(
    rule_code: str | None = Query(default=None, description="按规则编号过滤采集记录"),
    status: str | None = Query(default=None, description="待入库、已确认、已剔除、已入库"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """读取预检输入：待入库的采集记录清单。"""
    items, total = precheck_service.list_records(
        rule_code=rule_code, status=status, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/prechecks", response_model=ActionResult)
def create_precheck(payload: EntryPayload) -> ActionResult:
    """按目标字段与时间窗口生成候选组；anchor 缺省取该规则采集记录的最大采集时间。"""
    values = payload.values
    rule_code = str(values.get("规则编号") or "").strip()
    if not rule_code:
        return ActionResult(ok=False, message="缺少必填字段：规则编号")
    anchor = values.get("anchor") or values.get("锚点时间")
    max_groups = values.get("max_groups")
    precheck, message = precheck_service.start_precheck(
        rule_code,
        anchor_text=str(anchor) if anchor is not None else None,
        max_groups=int(max_groups) if max_groups not in (None, "") else None,
    )
    if precheck is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=precheck)


@router.get("/prechecks/list")
def list_prechecks(
    rule_code: str | None = Query(default=None, description="按规则编号过滤预检单"),
) -> dict[str, Any]:
    """预检单列表：包含状态、候选组核对进度与拟剔除数量。"""
    items = precheck_service.list_prechecks(rule_code)
    return {"total": len(items), "items": items}


@router.get("/prechecks/{precheck_id}")
def get_precheck(precheck_id: int) -> dict[str, Any]:
    """预检单明细：候选组、窗口边界、保留/剔除结论与处理进度。"""
    precheck = precheck_service.get_precheck(precheck_id)
    if precheck is None:
        raise HTTPException(status_code=404, detail=f"预检单 {precheck_id} 不存在")
    return precheck


@router.post("/prechecks/{precheck_id}/resume", response_model=ActionResult)
def resume_precheck(precheck_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """中断后从当前候选组续跑；max_groups 可限制本次续跑的组数。"""
    values = payload.values if payload else {}
    max_groups = values.get("max_groups")
    precheck, message = precheck_service.resume_precheck(
        precheck_id,
        max_groups=int(max_groups) if max_groups not in (None, "") else None,
    )
    if precheck is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=precheck)


@router.post("/prechecks/{precheck_id}/interrupt", response_model=ActionResult)
def interrupt_precheck(precheck_id: int) -> ActionResult:
    """中断预检，已核对候选组结论保留，续跑时从下一组继续。"""
    precheck, message = precheck_service.interrupt_precheck(precheck_id)
    if precheck is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=precheck)


@router.post("/prechecks/{precheck_id}/confirm", response_model=ActionResult)
def confirm_precheck(precheck_id: int) -> ActionResult:
    """确认预检：剔除结果写入存储追溯清单，胜出记录立即进入待入库，并按当时规则留档。

    并发重复确认只生效一次，不生成重复待入库项。
    """
    precheck, message = precheck_service.confirm_precheck(precheck_id)
    if precheck is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=precheck)


@router.get("/removals/list")
def list_removals(
    rule_code: str | None = Query(default=None),
    precheck_no: str | None = Query(default=None),
    group_no: str | None = Query(default=None),
) -> dict[str, Any]:
    """剔除追溯清单：每条带命中的窗口边界，按候选组编号分段。"""
    items = precheck_service.list_removals(
        rule_code=rule_code, precheck_no=precheck_no, group_no=group_no
    )
    return {"total": len(items), "items": items}


@router.get("/pending/list")
def list_pending(
    rule_code: str | None = Query(default=None),
) -> dict[str, Any]:
    """确认后立即生成的待入库条目，供任务工作台业务面读取。"""
    items = precheck_service.list_pending(rule_code)
    return {"total": len(items), "items": items}


@router.get("/archives/list")
def list_archives(
    rule_code: str | None = Query(default=None),
) -> dict[str, Any]:
    """历史留档：按确认时的去重规则快照保存，跨月重算只追加不删除。"""
    items = precheck_service.list_archives(rule_code)
    return {"total": len(items), "items": items}


@router.get("/workbench/summary")
def workbench_summary() -> dict[str, Any]:
    """任务工作台业务面：按规则汇总待入库、剔除与留档批次。"""
    return precheck_service.workbench_summary()
