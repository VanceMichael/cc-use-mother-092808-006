"""测试用世界构建：不含真实身份信息的合成数据。"""

from __future__ import annotations

from src.cbi import IndexService
from src.cbi.model import Principal

# 角色
STEWARD = Principal.of("u-steward", ["data_steward"])
PROVIDER = Principal.of("u-provider", ["climate_provider"])
OWNER = Principal.of("u-owner", ["method_owner"])
APPROVER = Principal.of("u-approver", ["method_approver"])
CALC = Principal.of("u-calc", ["index_calculator"])
INVESTOR = Principal.of("u-investor", ["investor"])
# 同时挂着审批角色但与提议人同一用户：职责分离必须仍拒绝
OWNER_WITH_APPROVAL = Principal.of("u-owner", ["method_owner", "method_approver"])

# 8 家发行人：四个行业，评级 A/BBB 交替
ISSUERS = [
    # (id, 行业, 评级, 2024强度, 2025强度, 到期日)
    ("I01", "公用事业", "A",   100, 80,  "2031-12-31"),
    ("I02", "公用事业", "BBB", 150, 110, "2029-06-30"),
    ("I03", "交通运输", "A",   200, 150, "2031-12-31"),
    ("I04", "交通运输", "BBB", 250, 190, "2029-09-30"),
    ("I05", "绿色地产", "A",   300, 230, "2031-12-31"),
    ("I06", "绿色地产", "BBB", 350, 270, "2030-03-31"),
    ("I07", "新能源",   "A",   400, 310, "2031-12-31"),
    ("I08", "新能源",   "BBB", 450, 350, "2031-12-31"),
]


def build_family_world(service: IndexService) -> dict:
    """登记核心 + 六只子指数所需的全部主数据、评级与两年披露。"""
    service.register_climate_source(PROVIDER, "CDS-1", "合成气候数据来源")

    for issuer_id, sector, _grade, _y24, _y25, _mat in ISSUERS:
        service.register_issuer(
            STEWARD,
            issuer_id=issuer_id,
            name=f"发行人 {issuer_id}",
            sector=sector,
            jurisdiction="CN",
        )
        service.register_bond(
            STEWARD,
            bond_id=f"B{issuer_id[1:]}",
            issuer_id=issuer_id,
            name=f"绿色债券 {issuer_id}",
            face_amount="100000000",
            maturity=_mat,
            green=True,
        )
        service.add_rating_event(
            STEWARD, bond_id=f"B{issuer_id[1:]}", announced="2025-10-01", grade=_grade
        )
        # 2024 年度披露（基准日水位可见）
        service.submit_emission(
            PROVIDER,
            record_id=f"EM-{issuer_id}-2024",
            issuer_id=issuer_id,
            source_id="CDS-1",
            period_start="2024-01-01",
            period_end="2024-12-31",
            received="2025-11-01",
            revenue="1",
            emissions_by_scope={"S1": str(_y24 // 2), "S12": str(_y24)},
        )
        # 2025 年度披露（2026-09-01 水位可见）
        service.submit_emission(
            PROVIDER,
            record_id=f"EM-{issuer_id}-2025",
            issuer_id=issuer_id,
            source_id="CDS-1",
            period_start="2025-01-01",
            period_end="2025-12-31",
            received="2026-06-01",
            revenue="1",
            emissions_by_scope={"S1": str(_y25 // 2), "S12": str(_y25)},
        )

    # 核心指数与六只子指数
    service.register_index(STEWARD, {"index_id": "CB-CORE", "name": "气候债券核心指数"})
    service.set_core_index(STEWARD, "CB-CORE")
    children = [
        ("CB-UTIL", "公用事业子指数", {"sector": "公用事业"}),
        ("CB-TRANS", "交通运输子指数", {"sector": "交通运输"}),
        ("CB-REALTY", "绿色地产子指数", {"sector": "绿色地产"}),
        ("CB-ENERGY", "新能源子指数", {"sector": "新能源"}),
        ("CB-INVA", "A级以上子指数", {"min_grade": "A"}),
        ("CB-SHORT", "短久期子指数", {"max_remaining_years": "4"}),
    ]
    for index_id, name, dim in children:
        service.register_index(
            STEWARD,
            {"index_id": index_id, "name": name, "parent_id": "CB-CORE", "dimension_filter": dim},
        )

    for day in ("2025-12-01", "2026-09-01", "2026-09-15", "2026-09-16"):
        service.register_rebalance_date(CALC, day)

    return {
        "core": "CB-CORE",
        "children": [item[0] for item in children],
        "issuer_ids": [item[0] for item in ISSUERS],
    }


def propose_v1(service: IndexService, weight_scheme: str = "equal") -> str:
    service.propose_methodology(
        OWNER,
        method_id="CBI-METH",
        version=1,
        carbon_scope="S12",
        weight_scheme=weight_scheme,
        screens={"require_green": True, "min_grade": "BBB-", "require_climate_data": True},
        windows={"rating_lag_days": 1, "suspension_grace_days": 3, "withdrawal_cure_days": 10},
        baseline_date="2025-12-01",
        note="v1：S12 口径、绿色+投资级筛选",
    )
    service.activate_methodology(APPROVER, "CBI-METH", 1)
    return "CBI-METH:1"
