"""端到端场景构造（全部为虚构发行人/债券，不含真实主体信息）。

叙事线：
- 2026-09-01 调仓：方法版本 v1（市值加权、评级窗口 5 天），核心指数相对
  基准碳强度降幅约 20.6%（>19%）；
- 2026-10-15：绿源电力更正 2025 年排放（原报偏低），同时一只债券评级下调；
- 方法维护者登记 v2（面值加权、评级窗口 3 天）并提议回溯重述；
- 另一角色批准后重算：降幅降至约 15.7%，原 19%+ 结果仍可按原水位/方法复现。
"""

from __future__ import annotations

from decimal import Decimal

from src.bond_index import (BatchKind, Bond, BondIndexService,
                            ClimateDataSource, ComplianceScreen, EmissionsScope,
                            EventKind, IndexDef, Issuer, MethodVersion,
                            Principal, RebalanceCalendarEntry, RestrictionLevel,
                            Role, Store, WeightMethod)
from src.bond_index.ingest import ingest_batch
from src.bond_index.registry import (add_calendar_entry, register_bond,
                                     register_index, register_issuer,
                                     register_method_version, register_screen,
                                     register_scope, register_source,
                                     register_weight_method)

REBALANCE_DATE = "2026-09-01"
BENCHMARK_INTENSITY = Decimal("500")


def principals() -> dict[str, Principal]:
    return {
        "provider": Principal("u-provider", Role.INDEX_PROVIDER),
        "vendor": Principal("u-vendor", Role.DATA_VENDOR),
        "master": Principal("u-master", Role.MASTER_DATA),
        "method": Principal("u-method", Role.METHOD_MAINTAINER),
        "approver": Principal("u-approver", Role.METHOD_APPROVER),
        "investor": Principal("u-investor", Role.INVESTOR),
    }


# (发行人, 行业, 评级, 债券, 发行量, 原始碳强度)
_ISSUERS: list[tuple[str, str, str, str, Decimal, Decimal, bool]] = [
    ("I01", "绿源电力", "电力", "AA",  "B0101", Decimal("800"), Decimal("300"), False),
    ("I02", "清风电网", "电力", "A",   "B0201", Decimal("600"), Decimal("340"), False),
    ("I03", "驰达轨交", "交通", "AA-", "B0301", Decimal("500"), Decimal("360"), False),
    ("I04", "远海航运", "交通", "BBB+", "B0401", Decimal("400"), Decimal("520"), False),
    ("I05", "广厦建设", "建筑", "A",   "B0501", Decimal("300"), Decimal("420"), False),
    ("I06", "新材绿建", "建筑", "A-",  "B0601", Decimal("200"), Decimal("440"), False),
    ("I07", "恒晟制造", "制造", "BBB+", "B0701", Decimal("250"), Decimal("480"), True),
    ("I08", "北辰燃气", "公用", "BBB", "B0801", Decimal("150"), Decimal("700"), False),
]

# 为让排放量/营收恰为目标碳强度，统一营收基数
REVENUE = Decimal("1000")  # 百万元


def build_world(store: Store) -> BondIndexService:
    """登记主数据、方法、指数族、日历，并送达原始批次。返回服务。"""

    p = principals()
    svc = BondIndexService(store)

    # ---- 数据来源（含一个受限来源）-----------------------------------
    register_source(store, p["master"], ClimateDataSource(
        source_id="S-PUB", name="公开气候披露库",
        restriction=RestrictionLevel.PUBLIC,
        authorized_roles=frozenset(), description="公开年报与ESG报告"))
    register_source(store, p["master"], ClimateDataSource(
        source_id="S-RES", name="受限排放数据交换",
        restriction=RestrictionLevel.RESTRICTED,
        authorized_roles=frozenset({Role.MASTER_DATA, Role.DATA_VENDOR,
                                    Role.INDEX_PROVIDER}),
        description="授权数据供应"))

    # ---- 发行人/债券 --------------------------------------------------
    for iid, name, sector, rating, bid, par, _ci, restricted in _ISSUERS:
        register_issuer(store, p["master"],
                        Issuer(issuer_id=iid, name=name, sector=sector,
                               rating=rating, restricted_climate=restricted))
        register_bond(store, p["master"], Bond(
            bond_id=bid, issuer_id=iid, currency="CNY",
            maturity_date="2029-06-01", par_amount=par))

    # ---- 口径/筛选/权重/方法版本 --------------------------------------
    register_scope(store, p["method"], EmissionsScope(
        scope_id="SCOPE-12", name="范围1+2/营收",
        include_scope1=True, include_scope2=True, include_scope3=False,
        basis="revenue_million_cny", version=1))
    register_screen(store, p["method"], ComplianceScreen(
        screen_id="SCR-CORE", name="投资级且有有效披露",
        min_rating="BBB-", exclude_suspended=True,
        require_live_disclosure=True, minimum_time_to_maturity_days=30))
    register_weight_method(store, p["method"], WeightMethod(
        method_id="WM-MCAP", name="市值加权", scheme="market_cap"))
    register_weight_method(store, p["method"], WeightMethod(
        method_id="WM-PAR", name="发行量加权", scheme="par"))

    register_method_version(store, p["method"], MethodVersion(
        method_version_id="MV-V1", version_no=1,
        screen_id="SCR-CORE", weight_method_id="WM-MCAP",
        scope_id="SCOPE-12",
        window_days={"maturity": 0, "suspension": 1, "resumption": 1,
                     "rating_change": 5, "data_withdrawal": 0},
        changelog="初始方法"))

    # ---- 指数族：核心 + 六只子指数（共享筛选与口径） ------------------
    register_index(store, p["method"], IndexDef(
        index_id="IDX-CORE", name="气候债券核心指数", parent_id=None,
        screen_id="SCR-CORE", weight_method_id="WM-MCAP", scope_id="SCOPE-12"))
    subs = [
        ("IDX-PWR", "电力子指数", {"sectors": ["电力"]}),
        ("IDX-TRN", "交通子指数", {"sectors": ["交通"]}),
        ("IDX-BLD", "建筑子指数", {"sectors": ["建筑"]}),
        ("IDX-CNY", "人民币子指数", {"bond_currencies": ["CNY"]}),
        ("IDX-HG", "核心发行人精选", {"issuer_ids": ["I01", "I02", "I03", "I04", "I05"]}),
        ("IDX-LRG", "大盘子指数", {"min_par": "400"}),
    ]
    for sid, sname, flt in subs:
        register_index(store, p["method"], IndexDef(
            index_id=sid, name=sname, parent_id="IDX-CORE",
            screen_id="SCR-CORE", weight_method_id="WM-MCAP",
            scope_id="SCOPE-12", issuer_filter=flt))

    # ---- 调仓日历 -----------------------------------------------------
    add_calendar_entry(store, p["provider"],
                       RebalanceCalendarEntry(REBALANCE_DATE, "三季度调仓"))

    # ---- 披露批次（公开 7 条 + 受限 1 条） -----------------------------
    public_disc, restricted_disc = [], []
    for iid, _n, _s, _r, _b, _par, ci, restricted in _ISSUERS:
        entry = {
            "record_id": f"D-{iid}-2025-V1",
            "issuer_id": iid,
            "source_id": "S-RES" if restricted else "S-PUB",
            "report_period": "2025",
            "emissions_tco2e": str(ci * REVENUE),
            "revenue": str(REVENUE),
            "reported_on": "2026-08-15",
        }
        (restricted_disc if restricted else public_disc).append(entry)
    ingest_batch(store, p["vendor"], "B-DISC-01", BatchKind.DISCLOSURE,
                 public_disc, received_at="2026-08-16T09:00:00")
    ingest_batch(store, p["vendor"], "B-DISC-RES-01", BatchKind.DISCLOSURE,
                 restricted_disc, received_at="2026-08-18T09:00:00")

    # ---- 行情批次 -----------------------------------------------------
    prices = [{"bond_id": row[4], "as_of": "2026-08-31",
               "price": "100", "currency": "CNY"} for row in _ISSUERS]
    ingest_batch(store, p["vendor"], "B-MKT-01", BatchKind.MARKET,
                 prices, received_at="2026-09-01T07:30:00")

    # ---- 基准批次 -----------------------------------------------------
    ingest_batch(store, p["vendor"], "B-BMK-01", BatchKind.BENCHMARK,
                 [{"as_of": "2026-08-31",
                   "carbon_intensity": str(BENCHMARK_INTENSITY)}],
                 received_at="2026-09-01T07:00:00")
    return svc


def apply_october_correction(store: Store) -> None:
    """事后数据：排放更正、旧披露撤回、评级下调（方法窗口差异由此显现）。"""

    p = principals()
    # 方法维护者在 10 月发布 V2：评级窗口 5 -> 3 天，权重改用发行量加权
    register_method_version(store, p["method"], MethodVersion(
        method_version_id="MV-V2", version_no=2,
        screen_id="SCR-CORE", weight_method_id="WM-PAR",
        scope_id="SCOPE-12",
        window_days={"maturity": 0, "suspension": 1, "resumption": 1,
                     "rating_change": 3, "data_withdrawal": 0},
        changelog="评级生效窗缩短为3天，权重改用发行量加权"))
    # 绿源电力 2025 年排放由 300,000 更正为 420,000（碳强度 300 -> 420）
    ingest_batch(store, p["vendor"], "B-DISC-02", BatchKind.DISCLOSURE, [{
        "record_id": "D-I01-2025-V2",
        "issuer_id": "I01",
        "source_id": "S-PUB",
        "report_period": "2025",
        "emissions_tco2e": "420000",
        "revenue": "1000",
        "reported_on": "2026-10-15",
    }], received_at="2026-10-15T10:00:00")
    # 旧披露按撤回事件进入"立即生效"窗口（生效日 10-15，不改变 9-11 月前状态）
    ingest_batch(store, p["vendor"], "B-EVT-02", BatchKind.EVENT, [{
        "event_id": "EV-WD-I01",
        "issuer_id": "I01",
        "kind": EventKind.DATA_WITHDRAWAL.value,
        "event_date": "2026-10-15",
        "record_id": "D-I01-2025-V1",
    }], received_at="2026-10-15T10:05:00")
    # 北辰燃气 8-28 评级下调 BBB -> BB+：
    # v1 窗口 5 天 -> 9-2 生效（9-1 仍合格）；v2 窗口 3 天 -> 8-31 生效（重述剔除）
    ingest_batch(store, p["vendor"], "B-EVT-03", BatchKind.EVENT, [{
        "event_id": "EV-RT-I08",
        "bond_id": "B0801",
        "issuer_id": "I08",
        "kind": EventKind.RATING_CHANGE.value,
        "event_date": "2026-08-28",
        "old_rating": "BBB",
        "new_rating": "BB+",
    }], received_at="2026-10-16T09:00:00")
