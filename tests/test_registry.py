"""主数据登记、指数族依赖与方法版本规则测试。"""

import unittest
from decimal import Decimal

from src.bond_index import (Bond, ClimateDataSource, ComplianceScreen,
                            DuplicateError, EmissionsScope, IndexDef, Issuer,
                            MethodVersion, PermissionDeniedError, Principal,
                            RestrictionLevel, Role, Store, ValidationError,
                            WeightMethod)
from src.bond_index.registry import (register_bond, register_index,
                                     register_issuer, register_method_version,
                                     register_screen, register_scope,
                                     register_source, register_weight_method,
                                     validate_family_complete)
from tests.helpers import make_principals


class RegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.p = make_principals()

    def test_issuer_requires_permission(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            register_issuer(self.store, self.p["investor"],
                            Issuer("I1", "甲", "电力", "AA"))

    def test_issuer_rejects_unknown_rating(self) -> None:
        with self.assertRaises(ValidationError):
            register_issuer(self.store, self.p["master"],
                            Issuer("I1", "甲", "电力", "ZZ"))

    def test_bond_requires_registered_issuer(self) -> None:
        with self.assertRaises(Exception):
            register_bond(self.store, self.p["master"], Bond(
                "B1", "I404", "CNY", "2030-01-01", Decimal("100")))

    def test_duplicate_issuer_rejected(self) -> None:
        register_issuer(self.store, self.p["master"],
                        Issuer("I1", "甲", "电力", "AA"))
        with self.assertRaises(DuplicateError):
            register_issuer(self.store, self.p["master"],
                            Issuer("I1", "甲二", "交通", "A"))

    def test_restricted_source_requires_authorized_roles(self) -> None:
        with self.assertRaises(ValidationError):
            register_source(self.store, self.p["master"], ClimateDataSource(
                "S1", "受限源", RestrictionLevel.RESTRICTED, frozenset()))

    def test_method_version_must_increase(self) -> None:
        register_scope(self.store, self.p["method"], EmissionsScope("SC", "口径"))
        register_screen(self.store, self.p["method"], ComplianceScreen("SCR", "筛选"))
        register_weight_method(self.store, self.p["method"], WeightMethod("WM", "权重"))
        kw = dict(screen_id="SCR", weight_method_id="WM", scope_id="SC")
        register_method_version(self.store, self.p["method"],
                                MethodVersion("MV2", 2, **kw))
        # 不能插入更小版本号
        with self.assertRaises(ValidationError):
            register_method_version(self.store, self.p["method"],
                                    MethodVersion("MV1", 1, **kw))
        # 版本号不能重复
        with self.assertRaises(DuplicateError):
            register_method_version(self.store, self.p["method"],
                                    MethodVersion("MV2X", 2, **kw))

    def test_family_requires_single_core_and_six_subs(self) -> None:
        register_scope(self.store, self.p["method"], EmissionsScope("SC", "口径"))
        register_screen(self.store, self.p["method"], ComplianceScreen("SCR", "筛选"))
        register_weight_method(self.store, self.p["method"], WeightMethod("WM", "权重"))
        for iid in ("I1", "I2"):
            register_issuer(self.store, self.p["master"],
                            Issuer(iid, f"公司{iid}", "电力", "AA"))
            register_bond(self.store, self.p["master"],
                          Bond(f"B{iid}", iid, "CNY", "2030-01-01", Decimal("100")))
        register_index(self.store, self.p["method"], IndexDef(
            "CORE", "核心", None, "SCR", "WM", "SC"))
        # 第二个核心不允许
        with self.assertRaises(DuplicateError):
            register_index(self.store, self.p["method"], IndexDef(
                "CORE2", "核心二", None, "SCR", "WM", "SC"))
        # 族不完整
        with self.assertRaises(ValidationError):
            validate_family_complete(self.store.view())
        # 子指数必须与核心共享筛选与口径
        register_screen(self.store, self.p["method"], ComplianceScreen("SCR2", "另一筛选"))
        with self.assertRaises(ValidationError):
            register_index(self.store, self.p["method"], IndexDef(
                "SX", "异筛选子", "CORE", "SCR2", "WM", "SC"))
        # 挂在不存在的核心
        with self.assertRaises(Exception):
            register_index(self.store, self.p["method"], IndexDef(
                "SY", "孤儿子", "NOPE", "SCR", "WM", "SC"))
        # 补齐六只
        for n in range(1, 7):
            register_index(self.store, self.p["method"], IndexDef(
                f"S{n}", f"子{n}", "CORE", "SCR", "WM", "SC",
                issuer_filter={"issuer_ids": ["I1"]}))
        core, subs = validate_family_complete(self.store.view())
        self.assertEqual(core, "CORE")
        self.assertEqual(len(subs), 6)
        # 第七只被拒绝
        with self.assertRaises(ValidationError):
            register_index(self.store, self.p["method"], IndexDef(
                "S7", "子7", "CORE", "SCR", "WM", "SC"))

    def test_investor_cannot_register_methods(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            register_scope(self.store, self.p["investor"],
                           EmissionsScope("SC", "口径"))


if __name__ == "__main__":
    unittest.main()
