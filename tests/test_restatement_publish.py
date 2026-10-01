"""重述 maker-checker 与发布闸门测试。"""

import unittest
from decimal import Decimal

from src.bond_index import (BatchKind, EmissionsScope, MethodVersion,
                            PermissionDeniedError, Principal, PublishBlockedError,
                            Role, Store, ValidationError, WeightMethod,
                            WorkflowError)
from src.bond_index.ingest import ingest_batch
from src.bond_index.registry import register_method_version
from tests.helpers import AS_OF, build_minimal, make_principals


def _publish_original(store, svc, p):
    run_id = svc.compute_run(p["provider"], AS_OF)
    svc.publish_run(p["provider"], run_id)
    return run_id


def _add_v2(store, p):
    register_method_version(store, p["method"], MethodVersion(
        method_version_id="MV2", version_no=2,
        screen_id="SCR", weight_method_id="WM", scope_id="SC",
        window_days={"rating_change": 3}))


class RestatementApprovalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.p = make_principals()
        self.svc = build_minimal(self.store)
        self.run_id = _publish_original(self.store, self.svc, self.p)

    def test_restatement_requires_reason(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.propose_restatement(
                self.p["method"], AS_OF, None, "  ")

    def test_investor_cannot_propose(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.svc.propose_restatement(
                self.p["investor"], AS_OF, None, "原因")

    def test_cannot_restate_missing_date(self) -> None:
        from src.bond_index.errors import NotFoundError

        with self.assertRaises(NotFoundError):
            self.svc.propose_restatement(
                self.p["method"], "2030-01-01", None, "原因")

    def test_same_person_cannot_self_approve(self) -> None:
        # provider 同时拥有提议与批准权，但不能自批
        rid = self.svc.propose_restatement(
            self.p["provider"], AS_OF, None, "统一提议")
        with self.assertRaises(WorkflowError):
            self.svc.approve_restatement(self.p["provider"], rid)

    def test_same_role_cannot_approve(self) -> None:
        rid = self.svc.propose_restatement(
            self.p["provider"], AS_OF, None, "统一提议")
        other = Principal("p2", Role.INDEX_PROVIDER)
        with self.assertRaises(WorkflowError):
            self.svc.approve_restatement(other, rid)

    def test_unauthorized_role_cannot_approve(self) -> None:
        rid = self.svc.propose_restatement(
            self.p["method"], AS_OF, None, "维护者提议")
        with self.assertRaises(PermissionDeniedError):
            self.svc.approve_restatement(self.p["investor"], rid)

    def test_approve_by_other_role_creates_restated_run(self) -> None:
        _add_v2(self.store, self.p)
        rid = self.svc.propose_restatement(
            self.p["method"], AS_OF, "MV2", "改用更短评级窗")
        restated_id = self.svc.approve_restatement(self.p["approver"], rid)
        self.assertNotEqual(restated_id, self.run_id)
        restated = self.svc.get_run(restated_id)
        self.assertEqual(restated["kind"], "restated")
        self.assertEqual(restated["method_version_id"], "MV2")
        self.assertEqual(restated["linked_restatement"], rid)
        rs = self.store.view()["restatements"][rid]
        self.assertEqual(rs["status"], "approved")
        self.assertIn("impact", rs)

    def test_cannot_approve_twice(self) -> None:
        _add_v2(self.store, self.p)
        rid = self.svc.propose_restatement(
            self.p["method"], AS_OF, "MV2", "x")
        self.svc.approve_restatement(self.p["approver"], rid)
        with self.assertRaises(WorkflowError):
            self.svc.approve_restatement(self.p["approver"], rid)

    def test_original_run_preserved_after_restatement(self) -> None:
        _add_v2(self.store, self.p)
        before = self.svc.get_run(self.run_id)["fingerprint"]
        rid = self.svc.propose_restatement(
            self.p["method"], AS_OF, "MV2", "x")
        self.svc.approve_restatement(self.p["approver"], rid)
        after = self.svc.get_run(self.run_id)["fingerprint"]
        self.assertEqual(before, after)


class PublishGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.p = make_principals()
        self.svc = build_minimal(self.store)

    def test_pending_restatement_blocks_publish(self) -> None:
        run_id = self.svc.compute_run(self.p["provider"], AS_OF)
        _add_v2(self.store, self.p)
        self.svc.propose_restatement(
            self.p["method"], AS_OF, "MV2", "待批")
        with self.assertRaises(PublishBlockedError):
            self.svc.publish_run(self.p["provider"], run_id)

    def test_conflict_batch_blocks_related_publish(self) -> None:
        run_id = self.svc.compute_run(self.p["provider"], AS_OF)
        # 异文重送披露批次（业务 reported_on 2026-08-01 <= 调仓日，相关）
        bad = [{
            "record_id": "D-I1", "issuer_id": "I1", "source_id": "SRC",
            "report_period": "2025", "emissions_tco2e": "999000",
            "revenue": "1000", "reported_on": "2026-08-01"}]
        with self.assertRaises(Exception):
            ingest_batch(self.store, self.p["vendor"], "B-D",
                         BatchKind.DISCLOSURE, bad,
                         received_at="2026-09-10T00:00:00")
        with self.assertRaises(PublishBlockedError):
            self.svc.publish_run(self.p["provider"], run_id)

    def test_publish_is_idempotent(self) -> None:
        run_id = self.svc.compute_run(self.p["provider"], AS_OF)
        pub1 = self.svc.publish_run(self.p["provider"], run_id)
        pub2 = self.svc.publish_run(self.p["provider"], run_id)
        self.assertEqual(pub1["publication_id"], pub2["publication_id"])
        self.assertEqual(len(self.store.view()["publications"]), 1)

    def test_investor_cannot_publish(self) -> None:
        run_id = self.svc.compute_run(self.p["provider"], AS_OF)
        with self.assertRaises(PermissionDeniedError):
            self.svc.publish_run(self.p["investor"], run_id)


if __name__ == "__main__":
    unittest.main()
