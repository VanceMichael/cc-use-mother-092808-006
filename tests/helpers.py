"""测试辅助：构造最小但完整的指数族世界。"""

from __future__ import annotations

from decimal import Decimal

from src.bond_index import (BatchKind, Bond, BondIndexService,
                            ClimateDataSource, ComplianceScreen, EmissionsScope,
                            IndexDef, Issuer, MethodVersion, Principal,
                            RebalanceCalendarEntry, RestrictionLevel, Role,
                            Store, WeightMethod)
from src.bond_index.ingest import ingest_batch
from src.bond_index.registry import (add_calendar_entry, register_bond,
                                     register_index, register_issuer,
                                     register_method_version, register_screen,
                                     register_scope, register_source,
                                     register_weight_method)

AS_OF = "2026-09-01"


def make_principals() -> dict[str, Principal]:
    return {
        "provider": Principal("p1", Role.INDEX_PROVIDER),
        "vendor": Principal("v1", Role.DATA_VENDOR),
        "master": Principal("m1", Role.MASTER_DATA),
        "method": Principal("mm1", Role.METHOD_MAINTAINER),
        "approver": Principal("ma1", Role.METHOD_APPROVER),
        "investor": Principal("i1", Role.INVESTOR),
    }


def build_minimal(store: Store, *, register_calendar: bool = True,
                  intensities: dict[str, str] | None = None) -> BondIndexService:
    """注册 1 核心 + 6 子指数、2 只发行人债券、方法版本与基础批次。"""

    p = make_principals()
    svc = BondIndexService(store)

    register_source(store, p["master"], ClimateDataSource(
        source_id="SRC", name="公开源",
        restriction=RestrictionLevel.PUBLIC, authorized_roles=frozenset()))

    rows = [
        ("I1", "甲公司", "电力", "AA", "B1", Decimal("600"),
         (intensities or {}).get("I1", "300")),
        ("I2", "乙公司", "交通", "A", "B2", Decimal("400"),
         (intensities or {}).get("I2", "400")),
    ]
    for iid, name, sector, rating, bid, par, ci in rows:
        register_issuer(store, p["master"], Issuer(
            issuer_id=iid, name=name, sector=sector, rating=rating))
        register_bond(store, p["master"], Bond(
            bond_id=bid, issuer_id=iid, currency="CNY",
            maturity_date="2030-01-01", par_amount=par))

    register_scope(store, p["method"], EmissionsScope(
        scope_id="SC", name="范围1+2", version=1))
    register_screen(store, p["method"], ComplianceScreen(
        screen_id="SCR", name="投资级", min_rating="BBB-"))
    register_weight_method(store, p["method"], WeightMethod(
        method_id="WM", name="市值", scheme="market_cap"))

    register_method_version(store, p["method"], MethodVersion(
        method_version_id="MV1", version_no=1,
        screen_id="SCR", weight_method_id="WM", scope_id="SC",
        window_days={"rating_change": 5}))

    register_index(store, p["method"], IndexDef(
        index_id="CORE", name="核心", parent_id=None,
        screen_id="SCR", weight_method_id="WM", scope_id="SC"))
    subs = [
        ("S1", "电力", {"sectors": ["电力"]}),
        ("S2", "交通", {"sectors": ["交通"]}),
        ("S3", "建筑", {"sectors": ["建筑"]}),
        ("S4", "人民币", {"bond_currencies": ["CNY"]}),
        ("S5", "精选", {"issuer_ids": ["I1"]}),
        ("S6", "大盘", {"min_par": "500"}),
    ]
    for sid, sname, flt in subs:
        register_index(store, p["method"], IndexDef(
            index_id=sid, name=sname, parent_id="CORE",
            screen_id="SCR", weight_method_id="WM", scope_id="SC",
            issuer_filter=flt))

    if register_calendar:
        add_calendar_entry(store, p["provider"],
                           RebalanceCalendarEntry(AS_OF, "调仓"))

    disc = [{
        "record_id": f"D-{iid}",
        "issuer_id": iid,
        "source_id": "SRC",
        "report_period": "2025",
        "emissions_tco2e": str(Decimal(ci) * Decimal("1000")),
        "revenue": "1000",
        "reported_on": "2026-08-01",
    } for iid, _name, _sector, _rating, _bid, _par, ci in rows]
    ingest_batch(store, p["vendor"], "B-D", BatchKind.DISCLOSURE, disc,
                 received_at="2026-08-02T00:00:00")
    prices = [{"bond_id": r[4], "as_of": "2026-08-31", "price": "100",
               "currency": "CNY"} for r in rows]
    ingest_batch(store, p["vendor"], "B-M", BatchKind.MARKET, prices,
                 received_at="2026-09-01T00:00:00")
    ingest_batch(store, p["vendor"], "B-B", BatchKind.BENCHMARK,
                 [{"as_of": "2026-08-31", "carbon_intensity": "500"}],
                 received_at="2026-09-01T00:00:00")
    return svc
