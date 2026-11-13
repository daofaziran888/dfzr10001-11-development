"""去重预检领域规则测试：验收序列 K、跨日/跨月边界、中断续跑、并发幂等与留档。"""
from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.services.dedup_precheck import DedupPrecheckService
from app.store import store


class PrecheckTestBase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.svc = DedupPrecheckService()

    def confirm_all(self, *prechecks: dict) -> None:
        for precheck in prechecks:
            entry, message = self.svc.confirm_precheck(precheck["id"])
            self.assertIsNotNone(entry, message)


class AcceptanceSequenceKTest(PrecheckTestBase):
    """验收序列 K：命中 2/3/5，确认后剔除 1/2/4，不产生重复待入库。"""

    def test_candidate_group_sizes(self) -> None:
        p1, _ = self.svc.start_precheck("DEDU-0001")
        p2, _ = self.svc.start_precheck("DEDU-0002")
        p3, _ = self.svc.start_precheck("DEDU-0003")
        self.assertEqual(p1["命中记录数"], 2)
        self.assertEqual(p2["命中记录数"], 3)
        self.assertEqual(p2["窗口天数"], 7)
        self.assertEqual(p3["命中记录数"], 5)
        self.assertEqual(p3["窗口天数"], 30)
        self.assertEqual(p1["候选组总数"], 1)
        self.assertEqual(p2["候选组总数"], 1)
        self.assertEqual(p3["候选组总数"], 1)

    def test_retention_rules_keep_different_winners(self) -> None:
        p2, _ = self.svc.start_precheck("DEDU-0002")
        p3, _ = self.svc.start_precheck("DEDU-0003")
        # DEDU-0002 保留最早，DEDU-0003 保留最晚，两者不能共用胜出记录
        self.assertEqual(p2["候选组"][0]["保留记录编号"], "R-D02-01")
        self.assertEqual(p3["候选组"][0]["保留记录编号"], "R-D03-05")
        self.assertNotEqual(
            p2["候选组"][0]["保留记录编号"], p3["候选组"][0]["保留记录编号"]
        )

    def test_window_boundary_visible_on_candidates(self) -> None:
        p2, _ = self.svc.start_precheck("DEDU-0002")
        boundary = p2["候选组"][0]["候选记录"][0]["命中窗口边界"]
        self.assertIn("闭窗口", boundary)
        self.assertIn("距窗口起", boundary)
        self.assertIn("距窗口止", boundary)

    def test_removal_counts_and_segmented_list(self) -> None:
        p1, _ = self.svc.start_precheck("DEDU-0001")
        p2, _ = self.svc.start_precheck("DEDU-0002")
        p3, _ = self.svc.start_precheck("DEDU-0003")
        self.confirm_all(p1, p2, p3)
        removals = self.svc.list_removals()
        self.assertEqual(
            [len([r for r in removals if r["规则编号"] == code])
             for code in ("DEDU-0001", "DEDU-0002", "DEDU-0003")],
            [1, 2, 4],
        )
        self.assertEqual(
            sorted({r["候选组编号"] for r in removals}),
            ["DEDU-0001-G01", "DEDU-0002-G01", "DEDU-0003-G01"],
        )

    def test_pending_entries_unique_and_grouped_one_each(self) -> None:
        p1, _ = self.svc.start_precheck("DEDU-0001")
        p2, _ = self.svc.start_precheck("DEDU-0002")
        p3, _ = self.svc.start_precheck("DEDU-0003")
        self.confirm_all(p1, p2, p3)
        pending = self.svc.list_pending()
        self.assertEqual(len(pending), 3)
        keys = [r["记录编号"] for r in pending]
        self.assertEqual(len(keys), len(set(keys)))

    def test_archive_keeps_rule_snapshot(self) -> None:
        p2, _ = self.svc.start_precheck("DEDU-0002")
        self.confirm_all(p2)
        archive = self.svc.list_archives("DEDU-0002")[0]
        for field in ("目标字段", "去重粒度", "时间窗口", "去重策略",
                      "保留规则", "落地方式", "是否启用"):
            self.assertIn(field, archive["规则快照"])
        self.assertEqual(archive["规则快照"]["保留规则"], "保留最早记录")


class RetentionPrecedenceTest(PrecheckTestBase):
    """保留规则优先于来源顺序。"""

    def test_latest_rule_beats_source_order(self) -> None:
        # DEDU-0003 来源顺序 1 的记录采集时间最早，但保留规则=最晚，应保留顺序 5
        p3, _ = self.svc.start_precheck("DEDU-0003")
        group = p3["候选组"][0]
        self.assertEqual(group["保留记录编号"], "R-D03-05")
        self.assertEqual(
            group["剔除记录编号"],
            ["R-D03-01", "R-D03-02", "R-D03-03", "R-D03-04"],
        )

    def test_earliest_rule_beats_source_order(self) -> None:
        p2, _ = self.svc.start_precheck("DEDU-0002")
        self.assertEqual(p2["候选组"][0]["保留记录编号"], "R-D02-01")

    def test_same_timestamp_falls_back_to_source_order(self) -> None:
        store.rows("dedup_record").append({
            "id": 20, "记录编号": "R-D01-04", "规则编号": "DEDU-0001", "目标字段": "手机号",
            "目标字段值": "13800000009", "来源顺序": 2, "采集时间": "2026-11-11 08:00:00",
            "来源标识": "源端Z", "状态": "待入库"})
        store.rows("dedup_record").append({
            "id": 21, "记录编号": "R-D01-05", "规则编号": "DEDU-0001", "目标字段": "手机号",
            "目标字段值": "13800000009", "来源顺序": 1, "采集时间": "2026-11-11 08:00:00",
            "来源标识": "源端A", "状态": "待入库"})
        p1, _ = self.svc.start_precheck("DEDU-0001")
        group = next(g for g in p1["候选组"] if g["目标字段值"] == "13800000009")
        self.assertEqual(group["保留记录编号"], "R-D01-05")  # 同时间按来源顺序 1 兜底


class CrossDayBoundaryTest(PrecheckTestBase):
    """跨日边界：00:00 / 00:01 / 23:59 锚点的纳入口径。"""

    def test_midnight_anchors(self) -> None:
        a1, _ = self.svc.start_precheck("DEDU-0001", anchor_text="2026-11-12 00:00:00")
        a2, _ = self.svc.start_precheck("DEDU-0002", anchor_text="2026-11-13 00:01:00")
        a3, _ = self.svc.start_precheck("DEDU-0003", anchor_text="2026-11-12 23:59:00")
        self.assertEqual(a1["命中记录数"], 2)
        self.assertEqual(a2["命中记录数"], 3)
        self.assertEqual(a3["命中记录数"], 1)
        self.assertTrue(a2["候选组"][0]["窗口起"].startswith("2026-11-06"))
        self.assertTrue(a3["候选组"][0]["窗口起"].startswith("2026-10-13"))

    def test_window_is_closed_interval(self) -> None:
        # 锚点恰好等于最晚采集时间时，该记录仍应纳入闭窗口
        a3, _ = self.svc.start_precheck("DEDU-0003", anchor_text="2026-10-31 23:59:00")
        self.assertEqual(a3["命中记录数"], 1)
        self.assertEqual(a3["候选组"][0]["候选记录"][0]["记录编号"], "R-D03-01")


class CrossMonthGroupingTest(PrecheckTestBase):
    """跨月窗口相交时按目标字段分别成组。"""

    def test_groups_separated_by_target_field(self) -> None:
        m3, _ = self.svc.start_precheck("DEDU-0003")
        self.assertTrue(m3["候选组"][0]["窗口起"].startswith("2026-10-14"))
        self.assertTrue(m3["候选组"][0]["窗口止"].startswith("2026-11-13"))
        self.assertEqual(m3["候选组"][0]["目标字段"], "证件号")
        self.assertEqual(m3["候选组"][0]["候选记录"][0]["记录编号"], "R-D03-01")

        m2, _ = self.svc.start_precheck("DEDU-0002")
        self.assertEqual(m2["候选组"][0]["目标字段"], "订单号")
        # 两个规则的窗口相交，但目标字段不同，候选组互不合并
        self.assertEqual(len(m2["候选组"]), 1)
        self.assertEqual(len(m3["候选组"]), 1)

    def test_multiple_field_values_form_multiple_groups(self) -> None:
        store.rows("dedup_record").append({
            "id": 11, "记录编号": "R-D03-06", "规则编号": "DEDU-0003", "目标字段": "证件号",
            "目标字段值": "ID-3301-0002", "来源顺序": 6, "采集时间": "2026-11-13 12:00:00",
            "来源标识": "源端D", "状态": "待入库"})
        p3, _ = self.svc.start_precheck("DEDU-0003")
        self.assertEqual(p3["候选组总数"], 2)
        self.assertEqual({g["目标字段值"] for g in p3["候选组"]},
                         {"ID-3301-0001", "ID-3301-0002"})


class InterruptResumeTest(PrecheckTestBase):
    """预检中断后从当前候选组继续。"""

    def _two_group_precheck(self) -> dict:
        store.rows("dedup_record").append({
            "id": 11, "记录编号": "R-D03-06", "规则编号": "DEDU-0003", "目标字段": "证件号",
            "目标字段值": "ID-3301-0002", "来源顺序": 6, "采集时间": "2026-11-13 12:00:00",
            "来源标识": "源端D", "状态": "待入库"})
        partial, _ = self.svc.start_precheck("DEDU-0003", max_groups=1)
        return partial

    def test_interrupt_at_group_boundary(self) -> None:
        partial = self._two_group_precheck()
        self.assertEqual(partial["状态"], "已中断")
        self.assertEqual(partial["已处理组数"], 1)
        self.assertEqual(partial["候选组总数"], 2)

    def test_confirm_rejected_while_interrupted(self) -> None:
        partial = self._two_group_precheck()
        entry, message = self.svc.confirm_precheck(partial["id"])
        self.assertIsNone(entry)
        self.assertIn("中断", message)

    def test_resume_continues_from_current_group(self) -> None:
        partial = self._two_group_precheck()
        resumed, message = self.svc.resume_precheck(partial["id"])
        self.assertIsNotNone(resumed, message)
        self.assertEqual(resumed["状态"], "待确认")
        self.assertEqual(resumed["已处理组数"], 2)
        entry, _ = self.svc.confirm_precheck(partial["id"])
        self.assertEqual(entry["状态"], "已确认")
        # 5 条组剔除 4，单条组保留不剔除
        self.assertEqual(len(self.svc.list_removals(rule_code="DEDU-0003")), 4)

    def test_resume_rejected_when_not_interrupted(self) -> None:
        p1, _ = self.svc.start_precheck("DEDU-0001")
        entry, message = self.svc.resume_precheck(p1["id"])
        self.assertIsNone(entry)
        self.assertIn("只有已中断", message)

    def test_manual_interrupt_and_resume(self) -> None:
        p1, _ = self.svc.start_precheck("DEDU-0001")
        interrupted, _ = self.svc.interrupt_precheck(p1["id"])
        self.assertEqual(interrupted["状态"], "已中断")
        resumed, _ = self.svc.resume_precheck(p1["id"])
        self.assertEqual(resumed["状态"], "待确认")


class ConcurrentConfirmTest(PrecheckTestBase):
    """并发重复确认只生效一次。"""

    def test_parallel_confirm_single_effect(self) -> None:
        precheck, _ = self.svc.start_precheck("DEDU-0002")
        outcomes: list[bool] = []
        lock = threading.Lock()

        def confirm() -> None:
            entry, _ = self.svc.confirm_precheck(precheck["id"])
            with lock:
                outcomes.append(entry is not None)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: confirm(), range(8)))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(len(self.svc.list_pending()), 1)
        self.assertEqual(len(self.svc.list_removals()), 2)

    def test_sequential_duplicate_confirm_blocked(self) -> None:
        precheck, _ = self.svc.start_precheck("DEDU-0002")
        first, _ = self.svc.confirm_precheck(precheck["id"])
        self.assertIsNotNone(first)
        second, message = self.svc.confirm_precheck(precheck["id"])
        self.assertIsNone(second)
        self.assertIn("重复确认不生效", message)
        self.assertEqual(len(self.svc.list_pending()), 1)


class ReadOnlyAndRecomputeTest(PrecheckTestBase):
    """已启用规则只参与读取；跨月重算不删除已入库/已留档记录。"""

    def test_enabled_rule_config_never_mutated(self) -> None:
        before = next(r for r in store.rows("dedup") if r["规则编号"] == "DEDU-0001")
        snapshot_before = dict(before)
        precheck, _ = self.svc.start_precheck("DEDU-0001")
        self.confirm_all(precheck)
        after = next(r for r in store.rows("dedup") if r["规则编号"] == "DEDU-0001")
        self.assertEqual(after, snapshot_before)
        self.assertEqual(after["status"], "已启用")

    def test_recompute_does_not_delete_decided_records(self) -> None:
        first, _ = self.svc.start_precheck("DEDU-0001")
        self.confirm_all(first)
        removals_before = len(self.svc.list_removals())
        archives_before = len(self.svc.list_archives())
        for record in store.rows("dedup_record"):
            if record["规则编号"] == "DEDU-0001" and record["状态"] == "已确认":
                record["状态"] = "已入库"

        second, _ = self.svc.start_precheck("DEDU-0001")
        self.confirm_all(second)
        self.assertEqual(len(self.svc.list_removals()), removals_before)
        self.assertEqual(len(self.svc.list_archives()), archives_before + 1)
        self.assertTrue(
            any(r["状态"] == "已入库" for r in store.rows("dedup_record"))
        )

    def test_decided_records_marked_skip_in_new_precheck(self) -> None:
        first, _ = self.svc.start_precheck("DEDU-0001")
        self.confirm_all(first)
        second, _ = self.svc.start_precheck("DEDU-0001")
        verdicts = {
            c["记录编号"]: c["预检判定"]
            for g in second["候选组"] for c in g["候选记录"]
        }
        self.assertTrue(all(v == "跳过" for v in verdicts.values()))


class MultiRuleBoundaryTest(PrecheckTestBase):
    """多规则边界：各规则按目标字段分组，每组只留 1 条。"""

    def test_each_rule_removal_totals(self) -> None:
        prechecks = [
            self.svc.start_precheck(code)[0]
            for code in ("DEDU-0001", "DEDU-0002", "DEDU-0003")
        ]
        self.confirm_all(*prechecks)
        expected = {"DEDU-0001": 1, "DEDU-0002": 2, "DEDU-0003": 4}
        for code, count in expected.items():
            self.assertEqual(
                len(self.svc.list_removals(rule_code=code)), count, code
            )

    def test_second_open_precheck_rejected_for_same_rule(self) -> None:
        self.svc.start_precheck("DEDU-0002")
        entry, message = self.svc.start_precheck("DEDU-0002")
        self.assertIsNone(entry)
        self.assertIn("进行中/待确认", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
