"""端到端场景：复现"原调仓日 >19% 降幅事后无法重现"的完整业务链。"""

import unittest
from decimal import Decimal

from src.bond_index import RestrictedDataError, Store
from src.bond_index.ingest import get_disclosure
from tests.scenario import (REBALANCE_DATE, apply_october_correction,
                            build_world, principals)


class EndToEndScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.p = principals()
        self.svc = build_world(self.store)

    def test_nineteen_percent_reproduction_chain(self) -> None:
        # 1) 原调仓日：V1，核心相对基准降幅 > 19%
        run_id = self.svc.compute_run(self.p["provider"], REBALANCE_DATE)
        original = self.svc.get_run(run_id)
        o_core = original["indexes"]["IDX-CORE"]
        self.assertEqual(original["method_version_no"], 1)
        self.assertGreater(
            Decimal(o_core["benchmark_reduction_pct"]), Decimal("0.19"))
        self.svc.publish_run(self.p["provider"], run_id)

        # 2) 事后：排放更正、旧数据撤回、评级下调，方法维护者发布 V2
        apply_october_correction(self.store)

        # 3) 重述必须提议 -> 另一角色批准
        rid = self.svc.propose_restatement(
            self.p["method"], REBALANCE_DATE, "MV-V2",
            "绿源电力2025排放更正300→420；方法V2评级窗缩短至3天、改发行量加权")
        restated_id = self.svc.approve_restatement(self.p["approver"], rid)
        restated = self.svc.get_run(restated_id)
        r_core = restated["indexes"]["IDX-CORE"]

        # 4) 重述后降幅下降，且不再能重现 >19%
        self.assertEqual(restated["method_version_no"], 2)
        self.assertLess(
            Decimal(r_core["benchmark_reduction_pct"]),
            Decimal(o_core["benchmark_reduction_pct"]))
        self.assertLess(
            Decimal(r_core["benchmark_reduction_pct"]), Decimal("0.19"))

        # 5) 评级窗口缩短使 B0801 被剔除，其余权重全部重算
        impact = self.store.view()["restatements"][rid]["impact"]
        self.assertEqual(impact["constituent_diff"]["removed"], ["B0801"])
        self.assertTrue(impact["watermark_changed"])
        self.assertTrue(impact["method_version_changed"])
        self.assertEqual(len(impact["constituent_diff"]["weight_changed"]), 7)

        # 6) 原始数字按原水位/原方法仍可逐券复现（权重守恒）
        for vals in original["indexes"].values():
            if vals["constituents"]:
                self.assertEqual(
                    sum((Decimal(c["weight"]) for c in vals["constituents"]),
                        Decimal("0")),
                    Decimal("1"))

        # 7) 对外复现报告同时给出原始、重述原因、审批链与当前版本
        rep = self.svc.reproduction_report(self.p["investor"], REBALANCE_DATE)
        self.assertEqual(rep["original"]["run_id"], run_id)
        self.assertTrue(rep["has_restatement"])
        self.assertEqual(rep["current_run_id"], restated_id)
        rs = rep["restatements"][0]
        self.assertIn("排放更正", rs["reason"])
        self.assertEqual(rs["proposed_by_role"], "method_maintainer")
        self.assertEqual(rs["approved_by_role"], "method_approver")
        self.assertIsNotNone(rs["impact"])

    def test_restricted_issuer_existence_hidden_from_investor(self) -> None:
        with self.assertRaises(RestrictedDataError):
            get_disclosure(self.store, self.p["investor"], "D-I07-2025-V1")


if __name__ == "__main__":
    unittest.main()
