"""端到端测试：数据水位、方法版本、权重守恒、事件窗口、批次幂等、
可恢复调仓、重述审批分离与复现报告。"""

from __future__ import annotations

import tempfile
import unittest
from fractions import Fraction

from src.cbi import IndexService
from src.cbi.errors import (
    AuthorizationError,
    BatchBlocked,
    ConflictError,
    NotFound,
    ValidationError,
)
from src.cbi.model import SENSITIVITY_RESTRICTED

from tests.builders import (
    APPROVER,
    CALC,
    INVESTOR,
    OWNER,
    OWNER_WITH_APPROVAL,
    PROVIDER,
    STEWARD,
    build_family_world,
    propose_v1,
)


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = IndexService(self.tmp.name)
        self.info = build_family_world(self.svc)
        self.method_key = propose_v1(self.svc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ------------------------------------------------------------ 基础计算

    def test_core_reduction_exceeds_19_percent_and_is_exactly_reproducible(self) -> None:
        self.svc.run_rebalance(CALC, "2025-12-01")
        run = self.svc.run_rebalance(CALC, "2026-09-01")
        core = run["snapshot"]["indexes"]["CB-CORE"]

        self.assertEqual(core["constituent_count"], 8)
        self.assertEqual(Fraction(core["weight_sum"]), Fraction(1))
        # 211.25 = (80+110+150+190+230+270+310+350)/8
        self.assertEqual(Fraction(core["waci_exact"]), Fraction(845, 4))
        # 基准 275 → (275-211.25)/275 = 23.1818...% > 19%
        reduction = float(Fraction(core["reduction_exact"]) * 100)
        self.assertGreater(reduction, 19.0)
        self.assertAlmostEqual(reduction, 23.1818, places=3)

        # 审计重算：冻结水位+方法版本必须逐指数精确复现
        check = self.svc.verify_run_recomputable(CALC, run["run_id"])
        self.assertTrue(check["all_match"])

    def test_eligibility_is_shared_and_weights_conserved_independently(self) -> None:
        run = self.svc.run_rebalance(CALC, "2026-09-01")
        snap = run["snapshot"]
        self.assertEqual(len(snap["indexes"]), 7)

        # 六只子指数成员均为核心合格集的子集
        core_bonds = {row["bond_id"] for row in snap["indexes"]["CB-CORE"]["constituents"]}
        expected = {
            "CB-UTIL": 2,
            "CB-TRANS": 2,
            "CB-REALTY": 2,
            "CB-ENERGY": 2,
            "CB-INVA": 4,
            "CB-SHORT": 3,
        }
        for index_id, count in expected.items():
            result = snap["indexes"][index_id]
            self.assertEqual(result["constituent_count"], count)
            # 每个指数在自己范围内独立守恒（分数精确等于 1）
            self.assertEqual(
                sum((Fraction(row["weight"]) for row in result["constituents"]), Fraction(0)),
                Fraction(1),
                f"{index_id} 权重不守恒",
            )
            sub_bonds = {row["bond_id"] for row in result["constituents"]}
            self.assertTrue(sub_bonds <= core_bonds)

        # 等权下子指数单只权重与核心不同但各自合理
        util = snap["indexes"]["CB-UTIL"]
        self.assertTrue(all(Fraction(row["weight"]) == Fraction(1, 2) for row in util["constituents"]))

    def test_calculation_requires_active_methodology(self) -> None:
        # 草稿方法不能用于计算
        self.svc.propose_methodology(
            OWNER,
            method_id="CBI-METH",
            version=9,
            carbon_scope="S12",
            weight_scheme="equal",
            screens={"require_green": True, "min_grade": "BBB-", "require_climate_data": True},
            windows={"rating_lag_days": 1, "suspension_grace_days": 3, "withdrawal_cure_days": 10},
            baseline_date="2025-12-01",
        )
        with self.assertRaises(ValidationError):
            self.svc.run_rebalance(CALC, "2026-09-01", method_key="CBI-METH:9")

    # ------------------------------------------------------------ 生效窗口

    def test_rating_change_enters_after_notification_lag(self) -> None:
        # B02 在 9月15日被下调至 BB+（低于 BBB-）；滞后 1 天
        self.svc.add_rating_event(STEWARD, bond_id="B02", announced="2026-09-14", grade="BB+")
        run = self.svc.run_rebalance(CALC, "2026-09-15")
        eligibility = {row["bond_id"]: row for row in run["snapshot"]["shared_eligibility"]}
        self.assertFalse(eligibility["B02"]["eligible"])
        self.assertTrue(any("低于筛选下限" in reason for reason in eligibility["B02"]["reasons"]))
        # 核心剩余 7 只，权重仍守恒
        core = run["snapshot"]["indexes"]["CB-CORE"]
        self.assertEqual(core["constituent_count"], 7)
        self.assertEqual(Fraction(core["weight_sum"]), Fraction(1))

    def test_rating_announcement_inside_lag_window_does_not_apply(self) -> None:
        # 9月15日当天公告，9月15日调仓时仍未越过 1 天滞后窗口
        self.svc.add_rating_event(STEWARD, bond_id="B02", announced="2026-09-15", grade="BB+")
        run = self.svc.run_rebalance(CALC, "2026-09-15")
        eligibility = {row["bond_id"]: row for row in run["snapshot"]["shared_eligibility"]}
        self.assertTrue(eligibility["B02"]["eligible"])

    def test_suspension_uses_grace_window(self) -> None:
        # B01 自 9月12日停牌，补救期 3 天（12-15 日保留），9月16日起退出
        self.svc.add_suspension_event(STEWARD, bond_id="B01", start="2026-09-12")
        run_inside = self.svc.run_rebalance(CALC, "2026-09-15")
        elig_in = {row["bond_id"]: row for row in run_inside["snapshot"]["shared_eligibility"]}
        self.assertTrue(elig_in["B01"]["eligible"])

        run_out = self.svc.run_rebalance(CALC, "2026-09-16")
        elig_out = {row["bond_id"]: row for row in run_out["snapshot"]["shared_eligibility"]}
        self.assertFalse(elig_out["B01"]["eligible"])
        self.assertTrue(any("停牌" in reason for reason in elig_out["B01"]["reasons"]))

    def test_resumption_restores_eligibility(self) -> None:
        self.svc.add_suspension_event(
            STEWARD, bond_id="B01", start="2026-09-12", resume="2026-09-14"
        )
        run = self.svc.run_rebalance(CALC, "2026-09-16")
        eligibility = {row["bond_id"]: row for row in run["snapshot"]["shared_eligibility"]}
        self.assertTrue(eligibility["B01"]["eligible"])

    def test_maturity_exits_on_maturity_date(self) -> None:
        # B02 到期日 2029-06-30；登记一个更早到期日验证边界
        # 直接登记新债券 2026-09-15 到期
        self.svc.register_issuer(STEWARD, issuer_id="I99", name="到期发行人", sector="新能源", jurisdiction="CN")
        self.svc.register_bond(
            STEWARD, bond_id="B99", issuer_id="I99", name="临期债券",
            face_amount="1000", maturity="2026-09-15", green=True,
        )
        self.svc.add_rating_event(STEWARD, bond_id="B99", announced="2026-01-01", grade="A")
        self.svc.submit_emission(
            PROVIDER, record_id="EM-I99-2025", issuer_id="I99", source_id="CDS-1",
            period_start="2025-01-01", period_end="2025-12-31", received="2026-06-01",
            revenue="1", emissions_by_scope={"S12": "120"},
        )
        run = self.svc.run_rebalance(CALC, "2026-09-15")
        eligibility = {row["bond_id"]: row for row in run["snapshot"]["shared_eligibility"]}
        self.assertFalse(eligibility["B99"]["eligible"])
        self.assertIn("到期", eligibility["B99"]["reasons"][0])

    def test_data_withdrawal_uses_cure_period(self) -> None:
        # 撤回 B01 发行人 I01 的 2025 披露：补救期 10 天
        self.svc.withdraw_emission(PROVIDER, "EM-I01-2025", withdrawn_on="2026-09-05")
        run_cure = self.svc.run_rebalance(CALC, "2026-09-15")
        elig = {row["bond_id"]: row for row in run_cure["snapshot"]["shared_eligibility"]}
        self.assertTrue(elig["B01"]["eligible"])

        self.svc.register_rebalance_date(CALC, "2026-09-17")
        run_later = self.svc.run_rebalance(CALC, "2026-09-17")
        elig2 = {row["bond_id"]: row for row in run_later["snapshot"]["shared_eligibility"]}
        self.assertFalse(elig2["B01"]["eligible"])
        self.assertTrue(any("撤回" in reason for reason in elig2["B01"]["reasons"]))

    # ------------------------------------------------------------ 水位固定

    def test_watermark_pins_available_data(self) -> None:
        # 同一批 2025 披露在 2026-06-01 到达；6 月与 9 月两次调仓看到相同数据，
        # 即便中间没有任何变化，结果也逐位一致——水位固定了“可用数据”。
        self.svc.register_rebalance_date(CALC, "2026-06-01")
        run_june = self.svc.run_rebalance(CALC, "2026-06-01", watermark="2026-06-01")
        run_sept = self.svc.run_rebalance(CALC, "2026-09-01")
        self.assertEqual(
            run_june["snapshot"]["indexes"]["CB-CORE"]["waci_exact"],
            run_sept["snapshot"]["indexes"]["CB-CORE"]["waci_exact"],
        )
        self.assertEqual(run_sept["snapshot"]["indexes"]["CB-CORE"]["waci"], "211.25000000")

    def test_future_data_beyond_watermark_is_invisible(self) -> None:
        # I01 另一条更新的披露在 2026-06-02 才到达
        self.svc.submit_emission(
            PROVIDER,
            record_id="EM-I01-TRAILING",
            issuer_id="I01",
            source_id="CDS-1",
            period_start="2025-06-01",
            period_end="2026-05-31",
            received="2026-06-02",
            revenue="1",
            emissions_by_scope={"S12": "50"},
        )
        # 水位 2026-06-01 看不到它
        self.svc.register_rebalance_date(CALC, "2026-06-01")
        run_before = self.svc.run_rebalance(CALC, "2026-06-01", watermark="2026-06-01")
        self.assertEqual(run_before["snapshot"]["indexes"]["CB-CORE"]["waci"], "211.25000000")
        # 9 月水位看得到：B01 强度变 50，核心 WACI = 211.25 - (80-50)/8 = 207.5
        run_after = self.svc.run_rebalance(CALC, "2026-09-01")
        self.assertEqual(run_after["snapshot"]["indexes"]["CB-CORE"]["waci"], "207.50000000")

    def test_withdrawal_after_watermark_does_not_change_frozen_run(self) -> None:
        run = self.svc.run_rebalance(CALC, "2026-09-01")
        waci_before = run["snapshot"]["indexes"]["CB-CORE"]["waci_exact"]
        # 调仓之后供应方才撤回旧披露
        self.svc.withdraw_emission(PROVIDER, "EM-I01-2025", withdrawn_on="2026-09-20")
        # 用冻结水位/方法审计重算：未来撤回不可见，数字逐位不变
        check = self.svc.verify_run_recomputable(CALC, run["run_id"])
        self.assertTrue(check["all_match"])
        self.assertEqual(
            self.svc.store.state["runs"][run["run_id"]]["snapshot"]["indexes"]["CB-CORE"]["waci_exact"],
            waci_before,
        )

    # ------------------------------------------------------------ 批次幂等/阻断

    def _quote_batch(self, price: str) -> dict:
        return {"quotes": [{"bond_id": "B01", "date": "2026-09-01", "price": price}]}

    def test_batch_replay_with_same_content_is_idempotent(self) -> None:
        payload = self._quote_batch("101.5")
        first = self.svc.submit_batch(STEWARD, "BQ-1", "quote", payload, received="2026-08-31")
        second = self.svc.submit_batch(STEWARD, "BQ-1", "quote", payload, received="2026-09-02")
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["state"], "replayed")
        # 行情没有被重复写两份（状态中只有一条）
        self.assertEqual(len(self.svc.store.state["quotes"]["B01"]), 1)

    def test_batch_replay_with_different_content_blocks_publication(self) -> None:
        self.svc.submit_batch(
            STEWARD, "BQ-2", "quote", self._quote_batch("101.5"), received="2026-08-31"
        )
        run = self.svc.run_rebalance(CALC, "2026-09-01")
        self.assertEqual(run["status"], "published")

        # 重送同批次但价格不同 → 阻断，并把相关发布标记为 blocked
        with self.assertRaises(BatchBlocked):
            self.svc.submit_batch(
                STEWARD, "BQ-2", "quote", self._quote_batch("99.0"), received="2026-09-03"
            )
        self.assertEqual(self.svc.store.state["batches"]["BQ-2"]["state"], "blocked")
        self.assertEqual(self.svc.store.state["runs"][run["run_id"]]["status"], "blocked")

        # 复现接口明确告知权威发布被阻断，而不是默默给出数字
        with self.assertRaisesRegex(ConflictError, "阻断"):
            self.svc.reproduce(INVESTOR, rebalance_date="2026-09-01")

    def test_blocked_batch_blocks_future_publish_too(self) -> None:
        payload = {"quotes": [{"bond_id": "B01", "date": "2026-09-15", "price": "101.5"}]}
        self.svc.submit_batch(STEWARD, "BQ-3", "quote", payload, received="2026-09-10")
        # 批次先被污染阻断
        with self.assertRaises(BatchBlocked):
            self.svc.submit_batch(
                STEWARD,
                "BQ-3",
                "quote",
                {"quotes": [{"bond_id": "B01", "date": "2026-09-15", "price": "90.0"}]},
                received="2026-09-11",
            )
        # 消费该批次的新发布必须被阻止
        with self.assertRaises(BatchBlocked):
            self.svc.run_rebalance(CALC, "2026-09-15")

    # ------------------------------------------------------------ 崩溃恢复

    def test_rebalance_resumes_after_crash_before_compute(self) -> None:
        with self.assertRaises(RuntimeError):
            self.svc.run_rebalance(CALC, "2026-09-01", crash_after="started")
        # 模拟进程重开：同一目录新建服务，悬挂任务恢复
        svc2 = IndexService(self.tmp.name)
        recovered = svc2.recover_interrupted(CALC)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["status"], "published")
        self.assertEqual(recovered[0]["snapshot"]["indexes"]["CB-CORE"]["waci"], "211.25000000")
        # 任务日志记录了恢复
        events = svc2.store.read_task_events()
        self.assertIn("recovered", [event["event"] for event in events])

    def test_rebalance_resumes_after_crash_between_compute_and_publish(self) -> None:
        with self.assertRaises(RuntimeError):
            self.svc.run_rebalance(CALC, "2026-09-01", crash_after="computed")
        svc2 = IndexService(self.tmp.name)
        recovered = svc2.recover_interrupted(CALC)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["status"], "published")
        # 恢复不会产生重复 run
        runs = [r for r in svc2.store.state["runs"].values()]
        self.assertEqual(len(runs), 1)

    # ------------------------------------------------------------ 重述审批分离

    def test_restatement_requires_separation_of_duties(self) -> None:
        # 提议人不能自己批准
        proposal = self.svc.propose_restatement(
            OWNER,
            kind="data",
            reason="发行人更正范围二外购电力排放因子",
            new_emission={
                "issuer_id": "I01",
                "version_of": "EM-I01-2025",
                "source_id": "CDS-1",
                "period_start": "2025-01-01",
                "period_end": "2025-12-31",
                "revenue": "1",
                "emissions": {"S1": "35", "S12": "70"},  # 原 S12=80 → 70
            },
            supersedes_record_id="EM-I01-2025",
        )
        with self.assertRaises(AuthorizationError):
            self.svc.approve_restatement(OWNER_WITH_APPROVAL, proposal["restatement_id"])
        with self.assertRaises(AuthorizationError):
            self.svc.approve_restatement(OWNER, proposal["restatement_id"])
        # 投资者无权批准
        with self.assertRaises(AuthorizationError):
            self.svc.approve_restatement(INVESTOR, proposal["restatement_id"])

    def test_restatement_creates_versioned_revision_and_reproduction_explains_it(self) -> None:
        original = self.svc.run_rebalance(CALC, "2026-09-01")
        original_waci = Fraction(original["snapshot"]["indexes"]["CB-CORE"]["waci_exact"])

        proposal = self.svc.propose_restatement(
            OWNER,
            kind="data",
            reason="发行人更正范围二外购电力排放因子，S12 由 80 调整为 70",
            new_emission={
                "issuer_id": "I01",
                "version_of": "EM-I01-2025",
                "source_id": "CDS-1",
                "period_start": "2025-01-01",
                "period_end": "2025-12-31",
                "revenue": "1",
                "emissions": {"S1": "35", "S12": "70"},
            },
            supersedes_record_id="EM-I01-2025",
        )
        # 未批准前，新版本不进入任何水位（只有原记录一条）
        pending = self.svc.search_emissions(PROVIDER, issuer_id="I01")
        self.assertEqual(len([r for r in pending if r["version_of"] == "EM-I01-2025"]), 1)

        approved = self.svc.approve_restatement(APPROVER, proposal["restatement_id"])
        self.assertTrue(approved["applied"])
        new_id = approved["application"]["new_record_id"]
        # 批准后出现 v2，v1 被标记取代
        versions = [r for r in self.svc.search_emissions(PROVIDER, issuer_id="I01")
                    if r["version_of"] == "EM-I01-2025"]
        self.assertEqual({r["version"] for r in versions}, {1, 2})

        # 原发布冻结：默认复现仍返回原数字，而不是最新数字
        report = self.svc.reproduce(INVESTOR, rebalance_date="2026-09-01")
        self.assertEqual(report["label"], "original")
        core_report = report["indexes"]["CB-CORE"]
        self.assertEqual(core_report["waci"], "211.25000000")

        # 报告列明后来重述及原因
        later = report["later_restatements"]
        self.assertEqual(len(later), 1)
        self.assertEqual(later[0]["reason"], "发行人更正范围二外购电力排放因子，S12 由 80 调整为 70")
        self.assertEqual(later[0]["approved_by"], "u-approver")
        flagged = [c for c in core_report["constituents"] if c["restated_afterward"]]
        self.assertEqual({row["bond_id"] for row in flagged}, {"B01"})

        # 重算视角：以批准日为水位重算历史调仓日，单独发布
        restated = self.svc.run_restated_view(
            CALC, "2026-09-01", proposal["restatement_id"], watermark=approved["decided_on"]
        )
        restated_waci = Fraction(restated["snapshot"]["indexes"]["CB-CORE"]["waci_exact"])
        self.assertEqual(restated_waci, original_waci - Fraction(10, 8))  # 80→70，等权 /8
        # 原运行仍在
        self.assertEqual(self.svc.store.state["runs"][original["run_id"]]["status"], "published")

        # 成分行指向新版本
        b01_row = next(
            row for row in restated["snapshot"]["indexes"]["CB-CORE"]["constituents"]
            if row["bond_id"] == "B01"
        )
        self.assertEqual(b01_row["emission_record_id"], new_id)
        self.assertEqual(b01_row["emission_version"], 2)

    def test_method_restatement_keeps_history_reproducible(self) -> None:
        original = self.svc.run_rebalance(CALC, "2026-09-01")
        # 方法维护者提议 v2：改用 S1 口径
        proposal = self.svc.propose_restatement(
            OWNER,
            kind="method",
            reason="指数方法升级：碳强度改用范围一口径",
            new_methodology={
                "method_id": "CBI-METH",
                "version": 2,
                "carbon_scope": "S1",
                "weight_scheme": "equal",
                "screens": {"require_green": True, "min_grade": "BBB-", "require_climate_data": True},
                "windows": {"rating_lag_days": 1, "suspension_grace_days": 3, "withdrawal_cure_days": 10},
                "baseline_date": "2025-12-01",
            },
        )
        self.svc.approve_restatement(APPROVER, proposal["restatement_id"])
        # v1 已停用，普通计算不能再引用
        with self.assertRaises(ValidationError):
            self.svc.run_rebalance(CALC, "2026-09-15", method_key="CBI-METH:1")
        # 但旧快照仍可凭冻结版本审计复现
        check = self.svc.verify_run_recomputable(CALC, original["run_id"])
        self.assertTrue(check["all_match"])

    # ------------------------------------------------------------ 受限数据访问

    def test_restricted_emission_is_indistinguishable_from_absent(self) -> None:
        # 供应方登记一条受限披露
        record = self.svc.submit_emission(
            PROVIDER,
            record_id="EM-SECRET",
            issuer_id="I02",
            source_id="CDS-1",
            period_start="2025-01-01",
            period_end="2025-12-31",
            received="2026-08-01",
            revenue="1",
            emissions_by_scope={"S12": "123"},
            sensitivity=SENSITIVITY_RESTRICTED,
        )
        # 有权角色可见
        self.assertEqual(self.svc.get_emission(CALC, "EM-SECRET")["record_id"], "EM-SECRET")
        # 投资者直取：与不存在同型同文案
        with self.assertRaisesRegex(NotFound, "气候数据不存在或不可见"):
            self.svc.get_emission(INVESTOR, "EM-SECRET")
        with self.assertRaisesRegex(NotFound, "气候数据不存在或不可见"):
            self.svc.get_emission(INVESTOR, "EM-DOES-NOT-EXIST")
        # 搜索结果中不出现，无法据此确认存在性
        found = self.svc.search_emissions(INVESTOR, issuer_id="I02")
        self.assertNotIn("EM-SECRET", [row["record_id"] for row in found])
        # 无权角色不能撤回（即使猜到记录号，错误也不暴露存在性）
        with self.assertRaises(AuthorizationError):
            self.svc.withdraw_emission(INVESTOR, "EM-SECRET")

    def test_reproduction_masks_restricted_values_for_investors(self) -> None:
        self.svc.submit_emission(
            PROVIDER,
            record_id="EM-SECRET-2",
            issuer_id="I03",
            source_id="CDS-1",
            period_start="2025-01-01",
            period_end="2025-12-31",
            received="2026-08-01",
            revenue="1",
            emissions_by_scope={"S12": "140"},
            sensitivity=SENSITIVITY_RESTRICTED,
        )
        run = self.svc.run_rebalance(CALC, "2026-09-01")
        self.assertTrue(run["snapshot"]["restricted_emission_used"])
        report = self.svc.reproduce(INVESTOR, rebalance_date="2026-09-01")
        b03 = next(
            row for row in report["indexes"]["CB-CORE"]["constituents"] if row["bond_id"] == "B03"
        )
        self.assertIsNone(b03["carbon_intensity"])
        self.assertTrue(b03["restricted_value_masked"])
        # 计算角色可见原值
        report_calc = self.svc.reproduce(CALC, rebalance_date="2026-09-01")
        b03_calc = next(
            row for row in report_calc["indexes"]["CB-CORE"]["constituents"] if row["bond_id"] == "B03"
        )
        self.assertIsNotNone(b03_calc["carbon_intensity"])

    # ------------------------------------------------------------ 权限

    def test_role_enforcement(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.svc.register_issuer(INVESTOR, issuer_id="IX", name="x", sector="s", jurisdiction="j")
        with self.assertRaises(AuthorizationError):
            self.svc.propose_methodology(
                INVESTOR, method_id="M", version=1, carbon_scope="S12", weight_scheme="equal",
                screens={"require_green": True, "min_grade": "BBB-", "require_climate_data": True},
                windows={"rating_lag_days": 1, "suspension_grace_days": 3, "withdrawal_cure_days": 10},
                baseline_date="2025-12-01",
            )
        with self.assertRaises(AuthorizationError):
            self.svc.activate_methodology(OWNER, "CBI-METH", 1)
        with self.assertRaises(AuthorizationError):
            self.svc.run_rebalance(INVESTOR, "2026-09-01")

    # ------------------------------------------------------------ 复现报告内容

    def test_reproduction_report_lists_full_content(self) -> None:
        self.svc.run_rebalance(CALC, "2025-12-01")
        self.svc.run_rebalance(CALC, "2026-09-01")
        report = self.svc.reproduce(
            INVESTOR, rebalance_date="2026-09-01", index_id="CB-CORE"
        )
        self.assertEqual(len(report["indexes"]), 1)
        core = report["indexes"]["CB-CORE"]
        self.assertEqual(core["constituent_count"], 8)
        # 权重、碳指标、基准差异齐全
        for row in core["constituents"]:
            self.assertIn("weight", row)
            self.assertIn("carbon_intensity", row)
        self.assertIsNotNone(core["baseline_waci"])
        self.assertIsNotNone(core["reduction_vs_baseline_pct"])
        # 水位与方法版本固定且明示
        self.assertEqual(report["watermark"], "2026-09-01")
        self.assertEqual(report["method"]["version"], 1)
        self.assertEqual(report["method"]["carbon_scope"], "S12")
        # 合格性审计（含不合格原因）随附
        self.assertEqual(len(report["shared_eligibility"]), 8)

    def test_original_run_is_frozen_against_later_recomputation(self) -> None:
        self.svc.run_rebalance(CALC, "2026-09-01")
        # 即便数据与方法变化，重复触发原始调仓也被拒绝
        with self.assertRaisesRegex(ConflictError, "冻结"):
            self.svc.run_rebalance(CALC, "2026-09-01", watermark="2026-12-31")

    def test_market_value_weighting_conserves_independently_per_index(self) -> None:
        # 新方法族：市值加权（v1 等权方法保持激活互不影响，用独立 method_id）
        self.svc.propose_methodology(
            OWNER,
            method_id="CBI-MV",
            version=1,
            carbon_scope="S12",
            weight_scheme="mv",
            screens={"require_green": True, "min_grade": "BBB-", "require_climate_data": True},
            windows={"rating_lag_days": 1, "suspension_grace_days": 3, "withdrawal_cure_days": 10},
            baseline_date="2025-12-01",
        )
        self.svc.activate_methodology(APPROVER, "CBI-MV", 1)
        # 基准日与 9 月调仓日都需要全部 8 只的行情（价格不同 → 权重不等）
        for day in ("2025-12-01", "2026-09-01"):
            entries = {
                "quotes": [
                    {"bond_id": f"B0{i}", "date": day, "price": str(100 + i)}
                    for i in range(1, 9)
                ]
            }
            self.svc.submit_batch(
                STEWARD, f"BQ-MV-{day}", "quote", entries, received=day
            )
        run = self.svc.run_rebalance(CALC, "2026-09-01", method_key="CBI-MV:1")
        for index_id, result in run["snapshot"]["indexes"].items():
            weights = [Fraction(row["weight"]) for row in result["constituents"]]
            self.assertEqual(sum(weights, Fraction(0)), Fraction(1), f"{index_id} 权重不守恒")
            self.assertTrue(any(w != Fraction(1, result["constituent_count"]) for w in weights))

    def test_disclosure_batch_replay_idempotent_and_mismatch_blocks(self) -> None:
        entries = {
            "emissions": [
                {
                    "record_id": "EM-I01-TRAILING2",
                    "issuer_id": "I01",
                    "source_id": "CDS-1",
                    "period_start": "2025-07-01",
                    "period_end": "2026-06-30",
                    "received": "2026-08-15",
                    "revenue": "1",
                    "emissions": {"S12": "80"},
                }
            ]
        }
        first = self.svc.submit_batch(PROVIDER, "BD-9", "disclosure", entries, received="2026-08-16")
        second = self.svc.submit_batch(PROVIDER, "BD-9", "disclosure", entries, received="2026-08-17")
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        # 不重复入账：仍只有一条该记录
        self.assertEqual(
            len([r for r in self.svc.store.state["emissions"] if r["record_id"] == "EM-I01-TRAILING2"]),
            1,
        )
        # 9月15日发布消费该披露（更新报告期优先于 2025 年报）
        run = self.svc.run_rebalance(CALC, "2026-09-15")
        self.assertEqual(run["status"], "published")
        # 重送内容不同 → 阻断该发布
        polluted = {
            "emissions": [
                {
                    "record_id": "EM-I01-TRAILING2",
                    "issuer_id": "I01",
                    "source_id": "CDS-1",
                    "period_start": "2025-07-01",
                    "period_end": "2026-06-30",
                    "received": "2026-08-15",
                    "revenue": "1",
                    "emissions": {"S12": "66"},
                }
            ]
        }
        with self.assertRaises(BatchBlocked):
            self.svc.submit_batch(PROVIDER, "BD-9", "disclosure", polluted, received="2026-09-18")
        self.assertEqual(self.svc.store.state["runs"][run["run_id"]]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
