"""去重预检：入库前先按目标字段 + 时间窗口生成候选组，确认后再落地剔除结果。

设计要点（对应需求边界）：

* 候选组按「目标字段值」分组，窗口为 [锚点-时间窗口, 锚点] 的闭区间，被剔除记录
  会带上命中的窗口起止与距离，保证窗口边界可追溯。
* 冲突时保留规则（最早/最晚）优先，采集时间相同才回退到来源顺序，来源顺序不会
  盖过保留规则。
* 预检可以中断，中断后只从未处理的候选组继续；候选组级别记录处理进度。
* 确认动作加进程内互斥并做状态幂等：并发重复确认只有一次生效，不生成重复待入库项。
* 已启用规则只参与读取：规则配置本身永不被预检改写；已经有结论（已确认/已剔除/
  已入库）的记录在再次确认时只读取、跳过，不重复落地。跨月重算只新增批次，
  不删除任何已入库/已留档记录。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

from app.store import store

RULE_MODULE = "dedup"
RECORD_MODULE = "dedup_record"
PRECHECK_MODULE = "dedup_precheck"
REMOVAL_MODULE = "dedup_removal"
PENDING_MODULE = "dedup_pending"
ARCHIVE_MODULE = "dedup_archive"

# 已经有结论的采集记录只参与读取，确认时不再重复落地。
DECIDED_STATUS = {"已确认", "已剔除", "已入库"}
# 预检单状态机：进行中 -> 已中断 ->（续跑）-> 待确认 -> 已确认
PRECHECK_STATUS = ["进行中", "已中断", "待确认", "已确认"]

# 确认互斥：预检单 id -> 锁；in_flight 记录正在确认的预检，挡住并发重复确认。
_CONFIRM_LOCKS: dict[int, threading.Lock] = {}
_CONFIRM_IN_FLIGHT: set[int] = set()
_GLOBAL_LOCK = threading.Lock()

RULE_SNAPSHOT_FIELDS = [
    "目标字段", "去重粒度", "时间窗口", "去重策略", "保留规则", "落地方式", "是否启用",
]


def _parse_dt(value: str | None) -> datetime | None:
    """兼容 'YYYY-MM-DD HH:MM:SS'、'YYYY-MM-DD HH:MM' 与纯日期。"""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _fmt_dt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _parse_window_days(text: str | None) -> int:
    """把「7天」「30天」「1天」解析成天数；无法解析时按 1 天兜底。"""
    if not text:
        return 1
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    return int(digits) if digits else 1


def _rule_snapshot(rule: dict[str, Any]) -> dict[str, Any]:
    return {field: rule.get(field) for field in RULE_SNAPSHOT_FIELDS}


def _get_rule(rule_code: str) -> dict[str, Any] | None:
    for row in store.rows(RULE_MODULE):
        if row.get("规则编号") == rule_code:
            return row
    return None


def _boundary_text(collected_at: datetime, window_start: datetime, window_end: datetime) -> str:
    """被剔除记录要能看到自己命中的窗口边界。"""
    return (
        f"采集时间 {_fmt_dt(collected_at)} 落入闭窗口 "
        f"[{_fmt_dt(window_start)} ~ {_fmt_dt(window_end)}]，"
        f"距窗口起 {collected_at - window_start}，距窗口止 {window_end - collected_at}"
    )


class DedupPrecheckService:
    # ------------------------------------------------------------------ 采集记录

    def list_records(
        self,
        *,
        rule_code: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(RECORD_MODULE)
        if rule_code:
            rows = [row for row in rows if row.get("规则编号") == rule_code]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    # ------------------------------------------------------------------ 候选组

    def _build_groups(
        self,
        rule: dict[str, Any],
        anchor: datetime,
    ) -> list[dict[str, Any]]:
        """按目标字段值分组，只保留落在时间窗口闭区间内的记录。"""
        rule_code = str(rule["规则编号"])
        days = _parse_window_days(rule.get("时间窗口"))
        window_start = anchor - timedelta(days=days)
        window_end = anchor
        target_field = str(rule.get("目标字段") or "")

        buckets: dict[str, list[dict[str, Any]]] = {}
        for record in store.rows(RECORD_MODULE):
            if record.get("规则编号") != rule_code:
                continue
            collected_at = _parse_dt(record.get("采集时间"))
            if collected_at is None or not (window_start <= collected_at <= window_end):
                continue
            buckets.setdefault(str(record.get("目标字段值") or ""), []).append(record)

        snapshot = _rule_snapshot(rule)
        groups: list[dict[str, Any]] = []
        for index, (field_value, records) in enumerate(sorted(buckets.items()), start=1):
            group_no = f"{rule_code}-G{index:02d}"
            candidates = self._judge_candidates(rule, records, window_start, window_end)
            winner = next((c["记录编号"] for c in candidates if c["预检判定"] == "保留"), None)
            removed = [c["记录编号"] for c in candidates if c["预检判定"] == "待剔除"]
            all_decided = all(c["预检判定"] == "跳过" for c in candidates)
            groups.append({
                "候选组编号": group_no,
                "目标字段": target_field,
                "目标字段值": field_value,
                "去重粒度": snapshot["去重粒度"],
                "时间窗口": snapshot["时间窗口"],
                "去重策略": snapshot["去重策略"],
                "保留规则": snapshot["保留规则"],
                "落地方式": snapshot["落地方式"],
                "是否启用": snapshot["是否启用"],
                "锚点时间": _fmt_dt(anchor),
                "窗口起": _fmt_dt(window_start),
                "窗口止": _fmt_dt(window_end),
                "窗口天数": days,
                "候选记录": candidates,
                "保留记录编号": winner,
                "剔除记录编号": removed,
                "组状态": "已处理" if all_decided else "待处理",
            })
        return groups

    def _judge_candidates(
        self,
        rule: dict[str, Any],
        records: list[dict[str, Any]],
        window_start: datetime,
        window_end: datetime,
    ) -> list[dict[str, Any]]:
        """保留规则优先于来源顺序；已有结论的记录只读取、标记跳过。"""
        keep_earliest = "最晚" not in str(rule.get("保留规则") or "")
        undecided: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []

        for record in records:
            collected_at = _parse_dt(record.get("采集时间"))
            assert collected_at is not None  # 入组前已过滤
            base = {
                "记录编号": record.get("记录编号"),
                "来源顺序": record.get("来源顺序"),
                "采集时间": record.get("采集时间"),
                "来源标识": record.get("来源标识"),
                "当前状态": record.get("状态"),
                "命中窗口边界": _boundary_text(collected_at, window_start, window_end),
            }
            if record.get("状态") in DECIDED_STATUS:
                candidates.append({**base, "预检判定": "跳过",
                                   "判定说明": "记录已有结论，已启用/已确认规则只参与读取"})
            else:
                undecided.append(record)
                candidates.append({**base, "预检判定": "待剔除", "判定说明": ""})

        if undecided:
            # 保留规则决定先后；采集时间相同才用来源顺序兜底，来源顺序不盖过保留规则。
            winner = min(
                undecided,
                key=lambda r: (
                    (_parse_dt(r.get("采集时间")).timestamp()) * (1 if keep_earliest else -1),
                    int(r.get("来源顺序", 0)),
                ),
            )
            reason = (
                "保留规则=保留最早记录，采集时间最早" if keep_earliest
                else "保留规则=保留最晚记录，采集时间最晚"
            )
            for candidate in candidates:
                if candidate["记录编号"] == winner.get("记录编号"):
                    candidate["预检判定"] = "保留"
                    candidate["判定说明"] = f"{reason}；同时间再按来源顺序兜底"
        return candidates

    # ------------------------------------------------------------------ 预检单

    def list_prechecks(self, rule_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(PRECHECK_MODULE)
        if rule_code:
            rows = [row for row in rows if row.get("规则编号") == rule_code]
        return rows

    def get_precheck(self, precheck_id: int) -> dict[str, Any] | None:
        return store.find(PRECHECK_MODULE, precheck_id)

    def start_precheck(
        self,
        rule_code: str,
        *,
        anchor_text: str | None = None,
        max_groups: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        rule = _get_rule(rule_code)
        if rule is None:
            return None, f"去重规则 {rule_code} 不存在或已归档"

        # 同一规则同时只允许一张未收尾的预检单，避免候选组结论互相覆盖。
        for row in store.rows(PRECHECK_MODULE):
            if row.get("规则编号") == rule_code and row.get("状态") != "已确认":
                return None, "该规则存在进行中/待确认的预检单，请先续跑确认或等待其收尾"

        anchor = _parse_dt(anchor_text)
        if anchor is None:
            collected = [
                _parse_dt(r.get("采集时间"))
                for r in store.rows(RECORD_MODULE)
                if r.get("规则编号") == rule_code
            ]
            collected = [item for item in collected if item is not None]
            if not collected:
                return None, f"规则 {rule_code} 暂时没有可预检的采集记录"
            anchor = max(collected)

        groups = self._build_groups(rule, anchor)
        if not groups:
            return None, "当前时间窗口内没有命中任何候选组，请调整锚点时间或窗口"

        precheck_id = store.next_id(PRECHECK_MODULE)
        precheck = {
            "id": precheck_id,
            "预检编号": f"PCK-{precheck_id:04d}",
            "规则编号": rule_code,
            "状态": "进行中",
            "锚点时间": _fmt_dt(anchor),
            "窗口天数": _parse_window_days(rule.get("时间窗口")),
            "规则快照": _rule_snapshot(rule),
            "候选组": groups,
            "已处理组数": 0,
            "候选组总数": len(groups),
            "命中记录数": sum(len(g["候选记录"]) for g in groups),
            "待剔除数": sum(len(g["剔除记录编号"]) for g in groups),
            "拟保留数": sum(1 for g in groups if g["保留记录编号"]),
            "创建时间": _fmt_dt(datetime.now()),
            "确认时间": None,
            "备注": "",
        }
        store.rows(PRECHECK_MODULE).append(precheck)
        return self._advance(precheck, max_groups), "去重预检候选组已生成"

    def resume_precheck(
        self, precheck_id: int, *, max_groups: int | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        precheck = store.find(PRECHECK_MODULE, precheck_id)
        if precheck is None:
            return None, f"预检单 {precheck_id} 不存在"
        if precheck["状态"] != "已中断":
            return None, f"预检单当前状态为「{precheck['状态']}」，只有已中断的预检才能续跑"

        # 续跑时按当前采集记录重算未处理组，已处理组保持原结论，实现从当前候选组继续。
        rule = _get_rule(str(precheck["规则编号"]))
        if rule is None:
            return None, "关联去重规则已不存在，无法续跑"
        anchor = _parse_dt(precheck["锚点时间"])
        fresh = {g["候选组编号"]: g for g in self._build_groups(rule, anchor)}  # type: ignore[arg-type]
        for index, group in enumerate(precheck["候选组"]):
            if index >= precheck["已处理组数"] and group["候选组编号"] in fresh:
                precheck["候选组"][index] = fresh[group["候选组编号"]]
        precheck["状态"] = "进行中"
        precheck["待剔除数"] = sum(len(g["剔除记录编号"]) for g in precheck["候选组"])
        precheck["拟保留数"] = sum(1 for g in precheck["候选组"] if g["保留记录编号"])
        return self._advance(precheck, max_groups), "预检已从当前候选组续跑"

    def interrupt_precheck(self, precheck_id: int) -> tuple[dict[str, Any] | None, str]:
        precheck = store.find(PRECHECK_MODULE, precheck_id)
        if precheck is None:
            return None, f"预检单 {precheck_id} 不存在"
        if precheck["状态"] not in ("进行中", "待确认"):
            return None, f"预检单当前状态为「{precheck['状态']}」，无需中断"
        precheck["状态"] = "已中断"
        precheck["备注"] = "预检中断，续跑时从当前候选组继续"
        return precheck, "预检已中断，进度保留在候选组级别"

    def _advance(self, precheck: dict[str, Any], max_groups: int | None) -> dict[str, Any]:
        """逐组推进；max_groups 限制本次处理的组数，用来模拟中断在组边界。"""
        total = precheck["候选组总数"]
        processed = precheck["已处理组数"]
        limit = total if max_groups is None else min(total, processed + max(0, max_groups))
        for index in range(processed, limit):
            group = precheck["候选组"][index]
            if group["组状态"] != "已处理":
                group["组状态"] = "已核对"
        precheck["已处理组数"] = limit
        if limit < total:
            precheck["状态"] = "已中断"
            precheck["备注"] = f"已核对 {limit}/{total} 个候选组后中断，可续跑"
        else:
            precheck["状态"] = "待确认"
            precheck["备注"] = "候选组全部核对完成，等待确认落地"
        return precheck

    # ------------------------------------------------------------------ 确认

    def confirm_precheck(self, precheck_id: int) -> tuple[dict[str, Any] | None, str]:
        precheck = store.find(PRECHECK_MODULE, precheck_id)
        if precheck is None:
            return None, f"预检单 {precheck_id} 不存在"
        if precheck["状态"] == "已确认":
            return None, "预检单已确认，重复确认不生效"
        if precheck["状态"] == "已中断":
            return None, "预检仍处于中断状态，请先续跑完成全部候选组再确认"
        if precheck["状态"] == "进行中":
            return None, "预检候选组尚未核对完成，不能确认"

        with _GLOBAL_LOCK:
            lock = _CONFIRM_LOCKS.setdefault(precheck_id, threading.Lock())
            if precheck_id in _CONFIRM_IN_FLIGHT:
                return None, "预检正在确认中，并发重复确认只生效一次"
            _CONFIRM_IN_FLIGHT.add(precheck_id)

        try:
            with lock:
                return self._do_confirm(precheck)
        finally:
            with _GLOBAL_LOCK:
                _CONFIRM_IN_FLIGHT.discard(precheck_id)

    def _do_confirm(self, precheck: dict[str, Any]) -> tuple[dict[str, Any], str]:
        rule_code = str(precheck["规则编号"])
        snapshot = precheck["规则快照"]
        now_text = _fmt_dt(datetime.now())
        removal_rows = store.rows(REMOVAL_MODULE)
        pending_rows = store.rows(PENDING_MODULE)
        record_rows = store.rows(RECORD_MODULE)
        record_index = {str(r.get("记录编号")): r for r in record_rows}
        pending_keys = {str(r.get("记录编号")) for r in pending_rows}

        removed_count = 0
        kept_count = 0
        for group in precheck["候选组"]:
            winner_no = group.get("保留记录编号")
            for candidate in group["候选记录"]:
                record_no = str(candidate["记录编号"])
                record = record_index.get(record_no)
                if record is None or record.get("状态") in DECIDED_STATUS:
                    # 已启用规则/跨月重算：已有结论只参与读取，不重复落地。
                    continue
                if candidate["预检判定"] == "跳过":
                    continue
                if candidate["预检判定"] == "待剔除":
                    record["状态"] = "已剔除"
                    removal_id = store.next_id(REMOVAL_MODULE)
                    removal_rows.append({
                        "id": removal_id,
                        "剔除编号": f"RML-{removal_id:04d}",
                        "预检编号": precheck["预检编号"],
                        "候选组编号": group["候选组编号"],
                        "规则编号": rule_code,
                        "记录编号": record_no,
                        "目标字段": group["目标字段"],
                        "目标字段值": group["目标字段值"],
                        "去重粒度": snapshot["去重粒度"],
                        "时间窗口": snapshot["时间窗口"],
                        "去重策略": snapshot["去重策略"],
                        "保留规则": snapshot["保留规则"],
                        "落地方式": snapshot["落地方式"],
                        "是否启用": snapshot["是否启用"],
                        "窗口起": group["窗口起"],
                        "窗口止": group["窗口止"],
                        "命中窗口边界": candidate["命中窗口边界"],
                        "采集时间": candidate["采集时间"],
                        "来源顺序": candidate["来源顺序"],
                        "保留记录编号": winner_no,
                        "剔除时间": now_text,
                    })
                    removed_count += 1
                elif candidate["预检判定"] == "保留" and record_no not in pending_keys:
                    record["状态"] = "已确认"
                    pending_id = store.next_id(PENDING_MODULE)
                    pending_rows.append({
                        "id": pending_id,
                        "待入库编号": f"PEN-{pending_id:04d}",
                        "预检编号": precheck["预检编号"],
                        "候选组编号": group["候选组编号"],
                        "规则编号": rule_code,
                        "记录编号": record_no,
                        "目标字段": group["目标字段"],
                        "目标字段值": group["目标字段值"],
                        "采集时间": candidate["采集时间"],
                        "来源顺序": candidate["来源顺序"],
                        "来源标识": candidate["来源标识"],
                        "生成时间": now_text,
                        "状态": "待入库",
                    })
                    pending_keys.add(record_no)
                    kept_count += 1

        # 历史按当时规则留档；跨月重算只追加新留档，不删除任何旧记录。
        archive_id = store.next_id(ARCHIVE_MODULE)
        archive = {
            "id": archive_id,
            "留档编号": f"ARC-{archive_id:04d}",
            "预检编号": precheck["预检编号"],
            "规则编号": rule_code,
            "规则快照": snapshot,
            "候选组编号": [g["候选组编号"] for g in precheck["候选组"]],
            "候选组数": precheck["候选组总数"],
            "本次剔除数": removed_count,
            "本次待入库数": kept_count,
            "确认时间": now_text,
            "留档说明": "按确认时的去重规则快照留档；跨月重算不删除已入库/已留档记录",
        }
        store.rows(ARCHIVE_MODULE).append(archive)
        precheck["状态"] = "已确认"
        precheck["确认时间"] = now_text
        precheck["备注"] = f"确认完成：剔除 {removed_count} 条，待入库 {kept_count} 条"
        return precheck, f"预检确认完成：剔除 {removed_count} 条，生成待入库 {kept_count} 条，留档 {archive['留档编号']}"

    # ------------------------------------------------------------------ 结果联动

    def list_removals(
        self,
        *,
        rule_code: str | None = None,
        precheck_no: str | None = None,
        group_no: str | None = None,
    ) -> list[dict[str, Any]]:
        """剔除清单：默认按候选组编号排序，前端可直接分三段展示。"""
        rows = store.rows(REMOVAL_MODULE)
        if rule_code:
            rows = [r for r in rows if r.get("规则编号") == rule_code]
        if precheck_no:
            rows = [r for r in rows if r.get("预检编号") == precheck_no]
        if group_no:
            rows = [r for r in rows if r.get("候选组编号") == group_no]
        return sorted(rows, key=lambda r: (str(r.get("候选组编号")), int(r.get("id", 0))))

    def list_pending(self, rule_code: str | None = None) -> list[dict[str, Any]]:
        """任务工作台业务面：确认后立即列出的待入库条目。"""
        rows = store.rows(PENDING_MODULE)
        if rule_code:
            rows = [r for r in rows if r.get("规则编号") == rule_code]
        return rows

    def list_archives(self, rule_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(ARCHIVE_MODULE)
        if rule_code:
            rows = [r for r in rows if r.get("规则编号") == rule_code]
        return rows

    def workbench_summary(self) -> dict[str, Any]:
        pending = store.rows(PENDING_MODULE)
        removals = store.rows(REMOVAL_MODULE)
        archives = store.rows(ARCHIVE_MODULE)
        by_rule: dict[str, dict[str, int]] = {}
        for row in pending:
            bucket = by_rule.setdefault(str(row.get("规则编号")), {"待入库": 0, "已剔除": 0, "留档批次": 0})
            bucket["待入库"] += 1
        for row in removals:
            bucket = by_rule.setdefault(str(row.get("规则编号")), {"待入库": 0, "已剔除": 0, "留档批次": 0})
            bucket["已剔除"] += 1
        for row in archives:
            bucket = by_rule.setdefault(str(row.get("规则编号")), {"待入库": 0, "已剔除": 0, "留档批次": 0})
            bucket["留档批次"] += 1
        return {
            "待入库总数": len(pending),
            "剔除总数": len(removals),
            "留档批次数": len(archives),
            "按规则汇总": by_rule,
            "待入库条目": pending,
        }
