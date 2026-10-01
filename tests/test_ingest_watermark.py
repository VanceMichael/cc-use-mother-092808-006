"""批次幂等/异文隔离、生效窗口与数据水位测试。"""

import unittest
from decimal import Decimal

from src.bond_index import (BatchConflictError, BatchKind, Bond, EventKind,
                            Issuer, Store)
from src.bond_index.engine import compute_watermark, derive_state
from src.bond_index.ingest import effective_date, ingest_batch
from tests.helpers import AS_OF, build_minimal, make_principals


class BatchIngestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.p = make_principals()
        self.svc = build_minimal(self.store)

    def _price(self, price: str = "100"):
        return [{"bond_id": "B1", "as_of": "2026-08-31", "price": price,
                 "currency": "CNY"},
                {"bond_id": "B2", "as_of": "2026-08-31", "price": "100",
                 "currency": "CNY"}]

    def test_identical_resend_is_idempotent(self) -> None:
        before = len(self.store.view()["market_prices"])
        status = ingest_batch(self.store, self.p["vendor"], "B-M",
                              BatchKind.MARKET, self._price(),
                              received_at="2026-09-01T00:00:00")
        self.assertEqual(status, "duplicate")
        self.assertEqual(len(self.store.view()["market_prices"]), before)

    def test_different_resend_conflicts_and_blocks(self) -> None:
        bad = self._price("97")
        with self.assertRaises(BatchConflictError):
            ingest_batch(self.store, self.p["vendor"], "B-M",
                         BatchKind.MARKET, bad,
                         received_at="2026-09-10T00:00:00")
        batch = self.store.view()["batches"]["B-M"]
        self.assertEqual(batch["status"], "conflict")
        self.assertTrue(batch["blocked"])

    def test_effective_windows_per_event_kind(self) -> None:
        self.assertEqual(
            effective_date("2026-09-01", EventKind.MATURITY), "2026-09-01")
        self.assertEqual(
            effective_date("2026-09-01", EventKind.SUSPENSION), "2026-09-02")
        self.assertEqual(
            effective_date("2026-09-01", EventKind.RATING_CHANGE,
                           {"rating_change": 3}),
            "2026-09-04")

    def test_rating_event_respects_method_window(self) -> None:
        """8-28 评级下调：V1 窗口5天 9-2 生效（9-1仍合格）。"""

        ingest_batch(self.store, self.p["vendor"], "B-E", BatchKind.EVENT, [{
            "event_id": "E1", "bond_id": "B2", "issuer_id": "I2",
            "kind": EventKind.RATING_CHANGE.value,
            "event_date": "2026-08-28",
            "old_rating": "A", "new_rating": "D",
        }], received_at="2026-08-29T00:00:00")

        eff = derive_state(self.store.view(), AS_OF,
                           method_version_id="MV1", knowledge_as_of=AS_OF)
        # 事件日+5天 = 09-02，晚于 09-01，评级仍为 A
        self.assertEqual(eff.issuer_rating["I2"], "A")
        self.assertNotIn("B2", eff.suspended_bonds)

    def test_data_withdrawal_enters_zero_day_window(self) -> None:
        ingest_batch(self.store, self.p["vendor"], "B-E2", BatchKind.EVENT, [{
            "event_id": "E2", "issuer_id": "I1",
            "kind": EventKind.DATA_WITHDRAWAL.value,
            "event_date": "2026-08-20", "record_id": "D-I1",
        }], received_at="2026-08-20T00:00:00")
        wm = compute_watermark(self.store.view(), AS_OF, "MV1",
                               knowledge_as_of=AS_OF)
        self.assertNotIn("I1", wm.selected_disclosures)
        self.assertIn("E2", wm.applied_events)

    def test_watermark_excludes_future_knowledge(self) -> None:
        """9-1 的原始水位不得纳入 9-2 才送达的披露。"""

        ingest_batch(self.store, self.p["vendor"], "B-D2",
                     BatchKind.DISCLOSURE, [{
                         "record_id": "D-I1-NEW", "issuer_id": "I1",
                         "source_id": "SRC", "report_period": "2025",
                         "emissions_tco2e": "999000", "revenue": "1000",
                         "reported_on": "2026-10-01",
                     }], received_at="2026-10-01T00:00:00")
        wm = compute_watermark(self.store.view(), AS_OF, "MV1",
                               knowledge_as_of=AS_OF)
        # 原始水位仍选旧记录
        self.assertEqual(wm.selected_disclosures["I1"], "D-I1")
        # 重述水位（知识截止无限）纳入新记录
        wm2 = compute_watermark(self.store.view(), AS_OF, "MV1")
        self.assertEqual(wm2.selected_disclosures["I1"], "D-I1-NEW")
        self.assertNotEqual(wm.manifest_hash, wm2.manifest_hash)

    def test_conflict_batch_excluded_from_watermark(self) -> None:
        with self.assertRaises(BatchConflictError):
            ingest_batch(self.store, self.p["vendor"], "B-M",
                         BatchKind.MARKET, self._price("97"),
                         received_at="2026-08-31T12:00:00")
        wm = compute_watermark(self.store.view(), AS_OF, "MV1",
                               knowledge_as_of=AS_OF)
        self.assertIn("B-M", wm.excluded_conflict_batches)

    def test_suspension_resumption_and_maturity_windows(self) -> None:
        """停牌按1日窗口剔除，复牌按1日窗口恢复，到期当日退出。"""

        from src.bond_index import Bond, Issuer
        from src.bond_index.registry import register_bond, register_issuer
        # B3 在 2026-09-02 到期，9-1 仍合格
        register_issuer(self.store, self.p["master"],
                        Issuer("I3", "丙", "建筑", "A"))
        register_bond(self.store, self.p["master"], Bond(
            "B3", "I3", "CNY", "2026-09-02", Decimal("300")))
        ingest_batch(self.store, self.p["vendor"], "B-D3",
                     BatchKind.DISCLOSURE, [{
                         "record_id": "D-I3", "issuer_id": "I3",
                         "source_id": "SRC", "report_period": "2025",
                         "emissions_tco2e": "300000", "revenue": "1000",
                         "reported_on": "2026-08-01"}],
                     received_at="2026-08-02T00:00:00")
        ingest_batch(self.store, self.p["vendor"], "B-M3",
                     BatchKind.MARKET, [{"bond_id": "B3", "as_of": "2026-08-31",
                                         "price": "100", "currency": "CNY"}],
                     received_at="2026-09-01T00:00:00")

        def eligible_on(d: str):
            st = self.store.view()
            wm = compute_watermark(st, d, "MV1", knowledge_as_of=d)
            eff = derive_state(st, d, method_version_id="MV1",
                               knowledge_as_of=d)
            screen = st["screens"]["SCR"]
            from src.bond_index.engine import shared_eligibility
            return shared_eligibility(st, d, wm, eff, screen), eff

        # B1 在 8-31 停牌（窗口1天 -> 9-1 生效），9-1 被剔除
        ingest_batch(self.store, self.p["vendor"], "B-ES", BatchKind.EVENT, [{
            "event_id": "ES1", "bond_id": "B1", "issuer_id": "I1",
            "kind": EventKind.SUSPENSION.value, "event_date": "2026-08-31",
        }], received_at="2026-08-31T10:00:00")
        eligible, eff = eligible_on(AS_OF)
        self.assertIn("B1", eff.suspended_bonds)
        self.assertNotIn("B1", eligible)
        self.assertIn("B3", eligible)  # 9-2 才到期

        # 9-2：B3 到期退出
        eligible2, _ = eligible_on("2026-09-02")
        self.assertNotIn("B3", eligible2)

        # 9-3 复牌（窗口1天 -> 9-4 生效），B1 恢复
        ingest_batch(self.store, self.p["vendor"], "B-ER", BatchKind.EVENT, [{
            "event_id": "ER1", "bond_id": "B1", "issuer_id": "I1",
            "kind": EventKind.RESUMPTION.value, "event_date": "2026-09-03",
        }], received_at="2026-09-03T10:00:00")
        eligible3, eff3 = eligible_on("2026-09-03")
        self.assertNotIn("B1", eligible3)   # 复牌 9-4 才生效
        eligible4, eff4 = eligible_on("2026-09-04")
        self.assertNotIn("B1", eff4.suspended_bonds)
        self.assertIn("B1", eligible4)


if __name__ == "__main__":
    unittest.main()
