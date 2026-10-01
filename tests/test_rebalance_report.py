"""调仓任务故障恢复与对外复现报告测试。"""

import tempfile
import unittest
from pathlib import Path

from src.bond_index import BondIndexService, RebalanceStatus, Store, ValidationError
from src.bond_index.registry import register_method_version
from src.bond_index.models import MethodVersion
from tests.helpers import AS_OF, build_minimal, make_principals


class RebalanceRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.store = Store(self.path)
        self.p = make_principals()
        self.svc = build_minimal(self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fresh_service(self) -> BondIndexService:
        """用同一状态文件新建实例，模拟崩溃后换进程恢复。"""

        return BondIndexService(Store(self.path))

    def test_task_requires_calendar_date(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.create_rebalance_task(self.p["provider"], "2026-12-31")

    def test_resume_from_each_checkpoint_without_duplicates(self) -> None:
        tid = self.svc.create_rebalance_task(self.p["provider"], AS_OF)

        # 在 prepare 后崩溃，新实例恢复，在 compute 后再次崩溃
        self.svc.run_rebalance_task(
            self.p["provider"], tid, crash_after_step="prepare")
        svc2 = self._fresh_service()
        svc2.run_rebalance_task(
            self.p["provider"], tid, crash_after_step="compute")
        task = svc2.get_task(tid)
        self.assertEqual(task["completed_steps"], ["prepare", "compute"])
        self.assertIsNotNone(task["run_id"])
        run_id = task["run_id"]

        # 第三个实例恢复到发布完成
        svc3 = self._fresh_service()
        final = svc3.resume_rebalance_task(self.p["provider"], tid)
        self.assertEqual(final["status"], RebalanceStatus.PUBLISHED.value)
        self.assertEqual(final["completed_steps"],
                         ["prepare", "compute", "publish"])
        self.assertEqual(final["run_id"], run_id)  # 未重复计算
        self.assertEqual(len(Store(self.path).view()["publications"]), 1)

    def test_resume_is_idempotent_after_completion(self) -> None:
        tid = self.svc.create_rebalance_task(self.p["provider"], AS_OF)
        self.svc.run_rebalance_task(self.p["provider"], tid)
        svc2 = self._fresh_service()
        again = svc2.resume_rebalance_task(self.p["provider"], tid)
        self.assertEqual(again["status"], RebalanceStatus.PUBLISHED.value)
        self.assertEqual(len(Store(self.path).view()["publications"]), 1)
        self.assertEqual(len(Store(self.path).view()["runs"]), 1)

    def test_duplicate_task_creation_rejected(self) -> None:
        self.svc.create_rebalance_task(self.p["provider"], AS_OF)
        with self.assertRaises(ValidationError):
            self.svc.create_rebalance_task(self.p["provider"], AS_OF)


class ReproductionReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.p = make_principals()
        self.svc = build_minimal(self.store)
        self.run_id = self.svc.compute_run(self.p["provider"], AS_OF)
        self.svc.publish_run(self.p["provider"], self.run_id)

    def test_report_lists_full_original_snapshot(self) -> None:
        rep = self.svc.reproduction_report(self.p["investor"], AS_OF)
        self.assertFalse(rep["has_restatement"])
        self.assertEqual(rep["current_run_id"], self.run_id)
        original = rep["original"]
        self.assertIn("watermark", original)
        self.assertIn("watermark_hash", original)
        self.assertIn("method_version_no", original)
        core = original["indexes"]["CORE"]
        self.assertTrue(core["constituents"])
        for c in core["constituents"]:
            self.assertIn("weight", c)
            self.assertIn("carbon_intensity", c)
            self.assertIn("disclosure_record_id", c)
        self.assertIsNotNone(core["benchmark_reduction_pct"])

    def test_report_records_restatement_reason_and_chain(self) -> None:
        register_method_version(self.store, self.p["method"], MethodVersion(
            method_version_id="MV2", version_no=2,
            screen_id="SCR", weight_method_id="WM", scope_id="SC",
            window_days={"rating_change": 3}))
        rid = self.svc.propose_restatement(
            self.p["method"], AS_OF, "MV2", "评级窗由5天缩短至3天")
        self.svc.approve_restatement(self.p["approver"], rid)

        rep = self.svc.reproduction_report(self.p["investor"], AS_OF)
        self.assertTrue(rep["has_restatement"])
        self.assertEqual(len(rep["restatements"]), 1)
        rs = rep["restatements"][0]
        self.assertEqual(rs["reason"], "评级窗由5天缩短至3天")
        self.assertEqual(rs["proposed_by_role"], "method_maintainer")
        self.assertEqual(rs["approved_by_role"], "method_approver")
        self.assertIsNotNone(rs.get("impact"))
        self.assertNotEqual(rep["current_run_id"], self.run_id)
        # 原始结果仍在报告中完整保留
        self.assertEqual(rep["original"]["run_id"], self.run_id)

    def test_report_requires_permission(self) -> None:
        from src.bond_index.errors import PermissionDeniedError

        # 数据供应方只负责送数，无复现读权
        with self.assertRaises(PermissionDeniedError):
            self.svc.reproduction_report(self.p["vendor"], AS_OF)


if __name__ == "__main__":
    unittest.main()
