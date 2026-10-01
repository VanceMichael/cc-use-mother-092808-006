"""权重守恒、族内共享合格性与碳指标计算测试。"""

import unittest
from decimal import Decimal

from src.bond_index import Store, WeightConservationError
from src.bond_index.engine import (compute_index_values, compute_watermark,
                                  conserve_weights, derive_state,
                                  shared_eligibility)
from tests.helpers import AS_OF, build_minimal, make_principals


def _run_parts(store: Store, as_of: str = AS_OF, mv: str = "MV1",
               knowledge: str | None = None):
    state = store.view()
    wm = compute_watermark(state, as_of, mv,
                           knowledge_as_of=as_of if knowledge is None else knowledge)
    eff = derive_state(state, as_of, method_version_id=mv,
                       knowledge_as_of=as_of if knowledge is None else knowledge)
    screen = state["screens"][state["method_versions"][mv]["screen_id"]]
    eligible = shared_eligibility(state, as_of, wm, eff, screen)
    wm_def = state["weight_methods"][state["method_versions"][mv]
                                     ["weight_method_id"]]
    values = compute_index_values(state, as_of, wm, eff, eligible, wm_def)
    return wm, eff, eligible, values


class WeightConservationTest(unittest.TestCase):
    def test_empty_raises(self) -> None:
        with self.assertRaises(WeightConservationError):
            conserve_weights({}, None)

    def test_sum_is_exactly_one(self) -> None:
        raw = {"a": Decimal("3"), "b": Decimal("7")}
        w = conserve_weights(raw, None)
        self.assertEqual(sum(w.values(), Decimal("0")), Decimal("1"))
        self.assertEqual(w["a"], Decimal("0.3"))

    def test_cap_redistributes_and_conserves(self) -> None:
        raw = {f"b{i}": Decimal(v) for i, v in enumerate(
            ["90", "2", "2", "2", "2"])}
        w = conserve_weights(raw, Decimal("0.30"))
        self.assertEqual(sum(w.values(), Decimal("0")), Decimal("1"))
        for weight in w.values():
            self.assertLessEqual(weight, Decimal("0.30") + Decimal("0.00000001"))

    def test_infeasible_cap_rejected(self) -> None:
        with self.assertRaises(WeightConservationError):
            conserve_weights({"a": Decimal("1"), "b": Decimal("1")},
                             Decimal("0.4"))


class IndexCalculationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.p = make_principals()
        build_minimal(self.store)

    def test_core_and_subs_each_conserve(self) -> None:
        _, _, eligible, values = _run_parts(self.store)
        self.assertEqual(len(eligible), 2)
        self.assertEqual(len(values), 7)  # 核心 + 6 子
        for index_id, vals in values.items():
            if vals["constituents"]:
                self.assertEqual(
                    Decimal(vals["weight_sum"]), Decimal("1"),
                    f"{index_id} 权重未守恒")
                total = sum(
                    (Decimal(c["weight"]) for c in vals["constituents"]),
                    Decimal("0"))
                self.assertEqual(total, Decimal("1"))

    def test_subindex_independent_weights(self) -> None:
        """子指数只含 B1 时，其权重在自身范围内归一为 1。"""

        _, _, _, values = _run_parts(self.store)
        s5 = values["S5"]
        self.assertEqual(len(s5["constituents"]), 1)
        self.assertEqual(s5["constituents"][0]["bond_id"], "B1")
        self.assertEqual(s5["constituents"][0]["weight"], "1.00000000000000")

    def test_shared_eligibility_is_subset_relation(self) -> None:
        _, _, eligible, values = _run_parts(self.store)
        core_bonds = {c["bond_id"] for c in values["CORE"]["constituents"]}
        self.assertEqual(set(eligible), core_bonds)
        for index_id, vals in values.items():
            if index_id == "CORE":
                continue
            sub_bonds = {c["bond_id"] for c in vals["constituents"]}
            self.assertTrue(sub_bonds <= core_bonds,
                            f"{index_id} 成分超出共享合格集合")

    def test_carbon_intensity_and_benchmark_gap(self) -> None:
        _, _, _, values = _run_parts(self.store)
        core = values["CORE"]
        # 市值等权（价均100，par 600/400）-> 权重 0.6/0.4
        # 强度 = 0.6*300 + 0.4*400 = 340；基准 500；降幅 0.32
        self.assertEqual(Decimal(core["carbon_intensity"]), Decimal("340"))
        self.assertEqual(Decimal(core["benchmark_carbon_intensity"]),
                         Decimal("500"))
        self.assertEqual(Decimal(core["benchmark_reduction_pct"]),
                         Decimal("0.32"))

    def test_core_empty_universe_raises(self) -> None:
        """两只都撤回披露后核心合格集为空，应明确失败而非伪造权重。"""

        from src.bond_index import BatchKind, EventKind
        from src.bond_index.ingest import ingest_batch
        ingest_batch(self.store, self.p["vendor"], "B-EW", BatchKind.EVENT, [
            {"event_id": "W1", "issuer_id": "I1",
             "kind": EventKind.DATA_WITHDRAWAL.value,
             "event_date": "2026-08-01", "record_id": "D-I1"},
            {"event_id": "W2", "issuer_id": "I2",
             "kind": EventKind.DATA_WITHDRAWAL.value,
             "event_date": "2026-08-01", "record_id": "D-I2"},
        ], received_at="2026-08-02T00:00:00")
        with self.assertRaises(WeightConservationError):
            _run_parts(self.store)


if __name__ == "__main__":
    unittest.main()
