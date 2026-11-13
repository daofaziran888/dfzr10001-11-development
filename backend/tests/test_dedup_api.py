"""去重预检 HTTP 接口测试：路由穿透、完整流程、并发确认幂等与工作台联动。"""
from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import app
from app.store import store


class DedupApiTestBase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.client = TestClient(app)

    def post(self, path: str, body: dict | None = None):
        return self.client.post(path, json=body or {"values": {}})


class RoutingTest(DedupApiTestBase):
    def test_static_paths_win_over_int_entry_route(self) -> None:
        # /export 必须声明在 /{entry_id} 之前，否则单段路径会被 int 解析成 422
        self.assertEqual(self.client.get("/api/dedup/export").status_code, 200)
        self.assertEqual(self.client.get("/api/dedup/records/list").status_code, 200)
        self.assertEqual(self.client.get("/api/dedup/prechecks/list").status_code, 200)
        self.assertEqual(self.client.get("/api/dedup/workbench/summary").status_code, 200)

    def test_missing_rule_returns_404(self) -> None:
        self.assertEqual(self.client.get("/api/dedup/999").status_code, 404)

    def test_rule_status_seed(self) -> None:
        items = self.client.get("/api/dedup").json()["items"]
        self.assertEqual(
            {r["规则编号"]: r["status"] for r in items},
            {"DEDU-0001": "已启用", "DEDU-0002": "待校验", "DEDU-0003": "待校验"},
        )


class PrecheckFlowTest(DedupApiTestBase):
    def test_full_flow_for_dedu_0002(self) -> None:
        resp = self.post("/api/dedup/prechecks", {"values": {"规则编号": "DEDU-0002"}})
        payload = resp.json()
        self.assertTrue(payload["ok"])
        precheck = payload["entry"]
        self.assertEqual(precheck["状态"], "待确认")
        self.assertEqual(precheck["命中记录数"], 3)
        group = precheck["候选组"][0]
        for field in ("目标字段", "去重粒度", "时间窗口", "去重策略",
                      "保留规则", "落地方式", "是否启用"):
            self.assertIn(field, group)
        self.assertEqual(group["保留记录编号"], "R-D02-01")
        self.assertEqual(group["剔除记录编号"], ["R-D02-02", "R-D02-03"])
        self.assertIn("闭窗口", group["候选记录"][1]["命中窗口边界"])

    def test_confirm_writes_removal_pending_archive(self) -> None:
        precheck = self.post(
            "/api/dedup/prechecks", {"values": {"规则编号": "DEDU-0002"}}
        ).json()["entry"]
        result = self.post(f"/api/dedup/prechecks/{precheck['id']}/confirm").json()
        self.assertTrue(result["ok"])

        removals = self.client.get(
            "/api/dedup/removals/list", params={"rule_code": "DEDU-0002"}
        ).json()
        self.assertEqual(removals["total"], 2)
        self.assertTrue(all(r["候选组编号"] == "DEDU-0002-G01" for r in removals["items"]))

        pending = self.client.get(
            "/api/dedup/pending/list", params={"rule_code": "DEDU-0002"}
        ).json()
        self.assertEqual(pending["total"], 1)
        self.assertEqual(pending["items"][0]["记录编号"], "R-D02-01")

        archives = self.client.get(
            "/api/dedup/archives/list", params={"rule_code": "DEDU-0002"}
        ).json()["items"]
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0]["规则快照"]["时间窗口"], "7天")

    def test_concurrent_confirm_only_one_takes_effect(self) -> None:
        precheck = self.post(
            "/api/dedup/prechecks", {"values": {"规则编号": "DEDU-0002"}}
        ).json()["entry"]
        outcomes: list[bool] = []
        lock = threading.Lock()

        def confirm() -> None:
            ok = self.post(
                f"/api/dedup/prechecks/{precheck['id']}/confirm"
            ).json()["ok"]
            with lock:
                outcomes.append(ok)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: confirm(), range(8)))
        self.assertEqual(sum(outcomes), 1)
        # 无重复待入库项
        self.assertEqual(self.client.get("/api/dedup/pending/list").json()["total"], 1)
        # 再次确认仍被幂等拦截
        again = self.post(f"/api/dedup/prechecks/{precheck['id']}/confirm").json()
        self.assertFalse(again["ok"])
        self.assertIn("重复确认不生效", again["message"])

    def test_interrupt_resume_flow(self) -> None:
        store.rows("dedup_record").append({
            "id": 11, "记录编号": "R-D03-06", "规则编号": "DEDU-0003", "目标字段": "证件号",
            "目标字段值": "ID-3301-0002", "来源顺序": 6, "采集时间": "2026-11-13 12:00:00",
            "来源标识": "源端D", "状态": "待入库"})
        precheck = self.post(
            "/api/dedup/prechecks",
            {"values": {"规则编号": "DEDU-0003", "max_groups": 1}},
        ).json()["entry"]
        self.assertEqual(precheck["状态"], "已中断")
        self.assertEqual(precheck["候选组总数"], 2)

        blocked = self.post(f"/api/dedup/prechecks/{precheck['id']}/confirm").json()
        self.assertFalse(blocked["ok"])

        resumed = self.post(
            f"/api/dedup/prechecks/{precheck['id']}/resume"
        ).json()["entry"]
        self.assertEqual(resumed["状态"], "待确认")
        self.assertEqual(resumed["已处理组数"], 2)

        self.post(f"/api/dedup/prechecks/{precheck['id']}/confirm")
        removals = self.client.get(
            "/api/dedup/removals/list", params={"rule_code": "DEDU-0003"}
        ).json()
        self.assertEqual(removals["total"], 4)

    def test_acceptance_sequence_k_removal_segments(self) -> None:
        for code in ("DEDU-0001", "DEDU-0002", "DEDU-0003"):
            precheck = self.post(
                "/api/dedup/prechecks", {"values": {"规则编号": code}}
            ).json()["entry"]
            self.post(f"/api/dedup/prechecks/{precheck['id']}/confirm")
        removals = self.client.get("/api/dedup/removals/list").json()["items"]
        groups = sorted({r["候选组编号"] for r in removals})
        self.assertEqual(
            groups, ["DEDU-0001-G01", "DEDU-0002-G01", "DEDU-0003-G01"]
        )
        counts = {
            code: len([r for r in removals if r["规则编号"] == code])
            for code in ("DEDU-0001", "DEDU-0002", "DEDU-0003")
        }
        self.assertEqual(counts, {"DEDU-0001": 1, "DEDU-0002": 2, "DEDU-0003": 4})


class WorkbenchAndGuardTest(DedupApiTestBase):
    def test_workbench_summary(self) -> None:
        precheck = self.post(
            "/api/dedup/prechecks", {"values": {"规则编号": "DEDU-0002"}}
        ).json()["entry"]
        self.post(f"/api/dedup/prechecks/{precheck['id']}/confirm")
        wb = self.client.get("/api/dedup/workbench/summary").json()
        self.assertEqual(wb["待入库总数"], 1)
        self.assertEqual(wb["剔除总数"], 2)
        self.assertEqual(wb["留档批次数"], 1)
        self.assertEqual(
            wb["按规则汇总"]["DEDU-0002"],
            {"待入库": 1, "已剔除": 2, "留档批次": 1},
        )

    def test_precheck_never_mutates_rule_config(self) -> None:
        before = self.client.get("/api/dedup").json()["items"]
        precheck = self.post(
            "/api/dedup/prechecks", {"values": {"规则编号": "DEDU-0001"}}
        ).json()["entry"]
        self.post(f"/api/dedup/prechecks/{precheck['id']}/confirm")
        after = self.client.get("/api/dedup").json()["items"]
        self.assertEqual(before, after)

    def test_overview_excludes_runtime_tables(self) -> None:
        overview = self.client.get("/api/overview").json()
        names = {m["name"] for m in overview["modules"]}
        self.assertEqual(len(names), 16)
        self.assertNotIn("dedup_precheck", names)
        self.assertNotIn("dedup_removal", names)
        self.assertNotIn("dedup_pending", names)
        self.assertNotIn("dedup_archive", names)
        self.assertNotIn("dedup_record", names)

    def test_invalid_inputs_rejected(self) -> None:
        self.assertFalse(self.post("/api/dedup/prechecks").json()["ok"])
        self.assertFalse(
            self.post("/api/dedup/prechecks",
                      {"values": {"规则编号": "NOPE"}}).json()["ok"]
        )
        self.assertFalse(self.post("/api/dedup/prechecks/999/confirm").json()["ok"])

    def test_cross_day_anchor_counts(self) -> None:
        a1 = self.post("/api/dedup/prechecks",
                       {"values": {"规则编号": "DEDU-0001",
                                   "anchor": "2026-11-12 00:00:00"}}).json()["entry"]
        a2 = self.post("/api/dedup/prechecks",
                       {"values": {"规则编号": "DEDU-0002",
                                   "anchor": "2026-11-13 00:01:00"}}).json()["entry"]
        a3 = self.post("/api/dedup/prechecks",
                       {"values": {"规则编号": "DEDU-0003",
                                   "anchor": "2026-11-12 23:59:00"}}).json()["entry"]
        self.assertEqual((a1["命中记录数"], a2["命中记录数"], a3["命中记录数"]),
                         (2, 3, 1))


class CrossModuleLinkageTest(DedupApiTestBase):
    """剔除追溯联动存储清单、待入库联动任务工作台，且与去重侧同源。"""

    def _confirm_one(self, code: str = "DEDU-0002") -> dict:
        precheck = self.post(
            "/api/dedup/prechecks", {"values": {"规则编号": code}}
        ).json()["entry"]
        self.post(f"/api/dedup/prechecks/{precheck['id']}/confirm")
        return precheck

    def test_storage_lists_dedup_removal_trace(self) -> None:
        self._confirm_one()
        resp = self.client.get("/api/storage/dedup-removals")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["total"], 2)
        self.assertTrue(all("命中窗口边界" in row for row in payload["items"]))
        # 与去重侧剔除清单同源
        dedup_side = self.client.get(
            "/api/dedup/removals/list", params={"rule_code": "DEDU-0002"}
        ).json()["items"]
        self.assertEqual(
            [r["剔除编号"] for r in payload["items"]],
            [r["剔除编号"] for r in dedup_side],
        )

    def test_storage_export_route_not_shadowed(self) -> None:
        # 与去重模块同类的路由顺序问题在存储侧也已修正
        self.assertEqual(self.client.get("/api/storage/export").status_code, 200)

    def test_task_workbench_lists_pending_immediately(self) -> None:
        self._confirm_one()
        wb = self.client.get("/api/task/dedup-workbench").json()
        self.assertEqual(wb["待入库总数"], 1)
        pending = wb["待入库条目"]
        self.assertEqual(pending[0]["记录编号"], "R-D02-01")
        self.assertEqual(pending[0]["状态"], "待入库")
        self.assertEqual(wb["按规则汇总"]["DEDU-0002"]["待入库"], 1)

    def test_task_export_route_not_shadowed(self) -> None:
        self.assertEqual(self.client.get("/api/task/export").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
