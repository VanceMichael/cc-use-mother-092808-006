"""角色权限与受限气候数据存在性隐藏测试。"""

import unittest

from src.bond_index import (BatchKind, ClimateDataSource, Principal,
                            RestrictedDataError, RestrictionLevel, Role, Store)
from src.bond_index.ingest import (get_disclosure, has_restricted_target,
                                   ingest_batch, search_disclosures)
from tests.helpers import build_minimal, make_principals


class RestrictedDataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.p = make_principals()
        build_minimal(self.store)
        # 增加一个受限来源与一条受限披露
        from src.bond_index.registry import register_source

        register_source(self.store, self.p["master"], ClimateDataSource(
            source_id="SRES", name="受限源",
            restriction=RestrictionLevel.RESTRICTED,
            authorized_roles=frozenset({Role.MASTER_DATA, Role.DATA_VENDOR})))
        ingest_batch(self.store, self.p["vendor"], "B-RD",
                     BatchKind.DISCLOSURE, [{
                         "record_id": "D-SECRET",
                         "issuer_id": "I1",
                         "source_id": "SRES",
                         "report_period": "2025",
                         "emissions_tco2e": "123000", "revenue": "1000",
                         "reported_on": "2026-08-01",
                     }], received_at="2026-08-02T00:00:00")

    def test_unauthorized_get_same_error_whether_exists_or_not(self) -> None:
        """无权者对"存在的受限记录"与"不存在编号"得到同类型同文案错误。"""

        inv = self.p["investor"]
        with self.assertRaises(RestrictedDataError) as c1:
            get_disclosure(self.store, inv, "D-SECRET")
        with self.assertRaises(RestrictedDataError) as c2:
            get_disclosure(self.store, inv, "D-DOES-NOT-EXIST")
        self.assertEqual(str(c1.exception), str(c2.exception))

    def test_unauthorized_search_omits_restricted_rows(self) -> None:
        inv = self.p["investor"]
        rows = search_disclosures(self.store, inv, issuer_id="I1")
        # 只能看到公开源 D-I1，受限行不出现（也不打码出现）
        ids = {r["record_id"] for r in rows}
        self.assertEqual(ids, {"D-I1"})

    def test_unauthorized_explicit_restricted_source_query_blocked(self) -> None:
        inv = self.p["investor"]
        with self.assertRaises(RestrictedDataError):
            search_disclosures(self.store, inv, source_id="SRES")

    def test_authorized_role_can_read(self) -> None:
        rec = get_disclosure(self.store, self.p["master"], "D-SECRET")
        self.assertEqual(rec["record_id"], "D-SECRET")

    def test_authorized_role_listing_excludes_unlisted_role(self) -> None:
        """INDEX_PROVIDER 有全局受限读权但不在 SRES 授权名单内，仍不可读。"""

        provider = self.p["provider"]
        with self.assertRaises(RestrictedDataError):
            get_disclosure(self.store, provider, "D-SECRET")

    def test_existence_probe_denied_for_unauthorized(self) -> None:
        with self.assertRaises(RestrictedDataError):
            has_restricted_target(self.store, self.p["investor"], "SRES")
        self.assertTrue(
            has_restricted_target(self.store, self.p["master"], "SRES"))

    def test_public_nonexistent_is_normal_not_found(self) -> None:
        from src.bond_index.errors import NotFoundError

        with self.assertRaises(NotFoundError):
            get_disclosure(self.store, self.p["master"], "D-PLAIN-MISSING")


if __name__ == "__main__":
    unittest.main()
