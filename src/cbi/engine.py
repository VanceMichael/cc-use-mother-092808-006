"""指数计算引擎。

一次家族计算固定 **数据水位**（watermark，只有 ``received/announced/start <= 水位``
的数据可见）与 **方法版本**（筛选规则、排放口径、权重方案、生效窗口、基准日）：

1. 对全部在册债券做一次共享合格性判断（核心与六只子指数共用）；
2. 各指数在自己的成员范围内独立计算权重，权重以 :class:`fractions.Fraction`
   精确归一化，各自严格守恒（权重和恰为 1）；
3. 计算碳强度（排放量/收入）、WACI（加权平均碳强度）、相对基准日差异；
4. 基准按同一方法在基准日重算，因此方法或水位一变，基准与当期同时改变——
   历史数字必须回到当时的水位与方法版本才能复现。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, getcontext
from fractions import Fraction
from typing import Any

from . import windows
from .errors import InactiveMethodError, ValidationError
from .model import (
    METHOD_ACTIVE,
    SENSITIVITY_RESTRICTED,
    WEIGHT_MARKET_VALUE,
    rating_at_least,
)

getcontext().prec = 40

# --------------------------------------------------------------------------- 工具


def _d(value: str) -> date:
    return date.fromisoformat(value)


def frac(value: Any) -> Fraction:
    """把十进制字符串精确转为 Fraction。"""
    return Fraction(Decimal(str(value)))


def decimal_str(value: Fraction, places: int = 10) -> str:
    """把 Fraction 转为定点十进制字符串（仅展示用，守恒以分数为准）。"""
    quotient = Decimal(value.numerator) / Decimal(value.denominator)
    return format(quotient, f".{places}f")


def pct_str(value: Fraction, places: int = 4) -> str:
    """分数转百分比字符串。"""
    quotient = Decimal(value.numerator) * Decimal(100) / Decimal(value.denominator)
    return format(quotient, f".{places}f")


# --------------------------------------------------------------------------- 数据选择


def _rating_for(bond_id: str, state: dict[str, Any], watermark: str, lag_days: int):
    events = [
        event
        for event in state["rating_events"]
        if event["bond_id"] == bond_id and event["announced"] <= watermark
    ]
    return windows.effective_rating(events, watermark, lag_days)


def _suspension_for(bond_id: str, state: dict[str, Any], watermark: str):
    event = state["suspensions"].get(bond_id)
    if not event or event["start"] > watermark:
        return None
    visible = dict(event)
    # 复牌事实尚未到达水位时，视为仍未复牌。
    if event.get("resume") and event["resume"] > watermark:
        visible["resume"] = None
    return visible


def _select_emission(
    issuer_id: str, scope: str, state: dict[str, Any], watermark: str
) -> dict[str, Any] | None:
    """在给定水位下选取发行人该口径最新可用的披露版本。

    同一 ``version_of`` 业务事实可能有多个版本（重述产生新版本）；
    水位只能看到已到达的最新版本。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in state["emissions"]:
        if record["issuer_id"] != issuer_id or record["received"] > watermark:
            continue
        if scope not in record["emissions"]:
            continue
        groups.setdefault(record["version_of"], []).append(record)

    chosen_per_group: list[dict[str, Any]] = []
    for versions in groups.values():
        versions.sort(key=lambda item: (item["version"], item["received"], item["record_id"]))
        chosen_per_group.append(versions[-1])

    if not chosen_per_group:
        return None
    # 不同事实之间取报告期最新，再以到达日、记录标识打破平局。
    chosen_per_group.sort(
        key=lambda item: (item["period_end"], item["received"], item["record_id"])
    )
    return chosen_per_group[-1]


# --------------------------------------------------------------------------- 合格性


def _build_views(
    state: dict[str, Any], on_date: str, watermark: str, method: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """对每只债券构造调仓日视图与共享合格性结论。"""
    screens = method["screens"]
    win = method["windows"]
    scope = method["carbon_scope"]

    views: dict[str, dict[str, Any]] = {}
    for bond_id, bond in sorted(state["bonds"].items()):
        reasons: list[str] = []
        issuer = state["issuers"].get(bond["issuer_id"])
        if issuer is None:
            reasons.append("发行人主数据缺失")

        if windows.matured(bond, on_date):
            reasons.append(f"已于 {bond['maturity']} 到期")

        suspension = _suspension_for(bond_id, state, watermark)
        suspended = windows.is_suspended(suspension, on_date, win["suspension_grace_days"])
        if suspended:
            exit_on = windows.suspension_exit_date(suspension["start"], win["suspension_grace_days"])
            reasons.append(f"停牌超出 {win['suspension_grace_days']} 日补救期（{exit_on} 起退出）")

        grade = _rating_for(bond_id, state, watermark, win["rating_lag_days"])
        if grade is None:
            reasons.append("无处于生效窗口内的评级")
        elif not rating_at_least(grade, screens["min_grade"]):
            effective_on = None
            for event in sorted(state["rating_events"], key=lambda item: item["announced"]):
                if event["bond_id"] == bond_id and event["grade"] == grade:
                    effective_on = windows.rating_effective_date(
                        event["announced"], win["rating_lag_days"]
                    )
            reasons.append(
                f"评级 {grade} 低于筛选下限 {screens['min_grade']}"
                + (f"（{effective_on} 起生效）" if effective_on else "")
            )

        if screens["require_green"] and not bond["green"]:
            reasons.append("非绿色债券")

        emission = None
        intensity: Fraction | None = None
        withdrawn_visible = False
        if issuer is not None:
            emission = _select_emission(issuer["issuer_id"], scope, state, watermark)
            if emission is None:
                if screens["require_climate_data"]:
                    reasons.append("水位内无该口径排放披露")
            else:
                # 撤回事实本身也要越过水位才可见：在旧水位下重算历史运行时，
                # 晚于水位的撤回视同尚未发生。
                withdrawn_visible = (
                    emission.get("withdrawn")
                    and bool(emission.get("withdrawn_on"))
                    and emission["withdrawn_on"] <= watermark
                )
                if withdrawn_visible and not windows.emission_within_cure_period(
                    emission, on_date, win["withdrawal_cure_days"]
                ):
                    exit_on = windows.withdrawal_exit_date(
                        emission["withdrawn_on"], win["withdrawal_cure_days"]
                    )
                    reasons.append(f"披露已撤回且补救期届满（{exit_on} 起缺失）")
                    emission = None
                else:
                    intensity = frac(emission["emissions"][scope]) / frac(emission["revenue"])

        views[bond_id] = {
            "bond_id": bond_id,
            "bond": bond,
            "issuer": issuer,
            "rating": grade,
            "suspended": suspended,
            "matured": windows.matured(bond, on_date),
            "emission": emission,
            "withdrawal_visible": withdrawn_visible if 'withdrawn_visible' in dir() else False,
            "carbon_intensity": intensity,
            "eligible": not reasons,
            "reasons": reasons,
        }
    return views


def _remaining_years(bond: dict[str, Any], on_date: str) -> Fraction:
    days = (_d(bond["maturity"]) - _d(on_date)).days
    return Fraction(days, 365)


def _passes_dimension(view: dict[str, Any], dim: dict[str, Any], on_date: str) -> bool:
    if "sector" in dim and (
        view["issuer"] is None or view["issuer"]["sector"] != dim["sector"]
    ):
        return False
    if "min_grade" in dim:
        if view["rating"] is None or not rating_at_least(view["rating"], dim["min_grade"]):
            return False
    if "max_remaining_years" in dim:
        if _remaining_years(view["bond"], on_date) > frac(dim["max_remaining_years"]):
            return False
    return True


# --------------------------------------------------------------------------- 权重


def _market_values(members: list[dict[str, Any]], state: dict[str, Any], on_date: str):
    """MV 权重：面值 × 行情价格/100。行情缺失即拒绝，保证确定性。"""
    quotes = state.get("quotes", {})
    values: dict[str, Fraction] = {}
    missing: list[str] = []
    for view in members:
        bond_id = view["bond_id"]
        quote = quotes.get(bond_id, {}).get(on_date)
        if quote is None:
            missing.append(bond_id)
            continue
        price = frac(quote["price"])
        values[bond_id] = frac(view["bond"]["face_amount"]) * price / frac(100)
    if missing:
        raise ValidationError(
            f"调仓日 {on_date} 市值加权缺少行情: {', '.join(sorted(missing))}"
        )
    return values


def _compute_weights(
    members: list[dict[str, Any]], method: dict[str, Any], state: dict[str, Any], on_date: str
) -> tuple[dict[str, Fraction], dict[str, Any]]:
    """在本指数自己的成员范围内独立归一化，精确守恒。"""
    if not members:
        raise ValidationError("指数在该调仓日无合格成分，无法满足权重守恒")

    if method["weight_scheme"] == "equal":
        denominator = len(members)
        weights = {view["bond_id"]: Fraction(1, denominator) for view in members}
        detail = {"scheme": "equal"}
    elif method["weight_scheme"] == WEIGHT_MARKET_VALUE:
        values = _market_values(members, state, on_date)
        total = sum(values.values(), Fraction(0))
        weights = {bond_id: value / total for bond_id, value in values.items()}
        detail = {
            "scheme": WEIGHT_MARKET_VALUE,
            "market_values": {bond_id: str(value) for bond_id, value in values.items()},
        }
    else:  # pragma: no cover - 方法登记时已校验
        raise ValidationError("未知权重方案")

    # 守恒断言（分数精确等于 1）。
    assert sum(weights.values(), Fraction(0)) == Fraction(1), "权重不守恒"
    return weights, detail


# --------------------------------------------------------------------------- 家族计算


def _index_result(
    definition: dict[str, Any],
    members: list[dict[str, Any]],
    weights: dict[str, Fraction],
    weight_detail: dict[str, Any],
    baseline_waci: Fraction | None,
) -> dict[str, Any]:
    constituents = []
    waci = Fraction(0)
    for view in members:
        bond_id = view["bond_id"]
        weight = weights[bond_id]
        intensity = view["carbon_intensity"]  # 合格成分必然有值
        waci += weight * intensity
        record = view["emission"]
        constituents.append(
            {
                "bond_id": bond_id,
                "issuer_id": view["bond"]["issuer_id"],
                "name": view["bond"]["name"],
                "weight": str(weight),
                "weight_decimal": decimal_str(weight),
                "carbon_intensity": decimal_str(intensity, 8),
                "carbon_intensity_exact": str(intensity),
                "emission_record_id": record["record_id"],
                "emission_version": record["version"],
                "emission_sensitivity": record["sensitivity"],
                "emission_withdrawn": view.get("withdrawal_visible", False),
            }
        )

    reduction = None
    if baseline_waci is not None and baseline_waci > 0:
        reduction = (baseline_waci - waci) / baseline_waci

    return {
        "index_id": definition["index_id"],
        "name": definition["name"],
        "parent_id": definition["parent_id"],
        "dimension_filter": definition["dimension_filter"],
        "constituent_count": len(constituents),
        "constituents": constituents,
        "weight_scheme": weight_detail["scheme"],
        "weight_sum": str(sum(weights.values(), Fraction(0))),
        "waci": decimal_str(waci, 8),
        "waci_exact": str(waci),
        "baseline_waci": decimal_str(baseline_waci, 8) if baseline_waci is not None else None,
        "reduction_vs_baseline_pct": pct_str(reduction) if reduction is not None else None,
        "reduction_exact": str(reduction) if reduction is not None else None,
    }


def _members_for(
    definition: dict[str, Any],
    views: dict[str, dict[str, Any]],
    core_eligible: list[dict[str, Any]],
    on_date: str,
) -> list[dict[str, Any]]:
    if definition["parent_id"] is None:
        return core_eligible
    # 子指数：核心合格集的子集，绝不溢出核心合格性。
    return [
        view
        for view in core_eligible
        if _passes_dimension(view, definition["dimension_filter"], on_date)
    ]


def compute_family(
    state: dict[str, Any],
    on_date: str,
    watermark: str,
    method: dict[str, Any],
    *,
    allow_inactive: bool = False,
) -> dict[str, Any]:
    """执行一次指数族计算（核心 + 全部子指数）。

    返回结构包含共享的合格性判断、每只指数独立守恒的权重结果，以及基准重算结果。
    生产发布只允许使用已激活方法（``allow_inactive=False``）；审计重算可用
    ``allow_inactive=True`` 用历史方法版本复现旧快照。
    """
    if method["status"] != METHOD_ACTIVE and not allow_inactive:
        raise InactiveMethodError(
            f"方法 {method['method_id']} v{method['version']} 未激活，不能用于计算"
        )
    if watermark < on_date:
        raise ValidationError("数据水位不能早于调仓日")

    core_id = state["core_index_id"]
    core_def = state["indexes"][core_id]

    views = _build_views(state, on_date, watermark, method)
    core_eligible = [views[bond_id] for bond_id in sorted(views) if views[bond_id]["eligible"]]

    # 基准：同一方法、基准日、水位取基准日，重算一次合格性。
    baseline_date = method["baseline_date"]
    baseline_waci_by_index: dict[str, Fraction | None] = {}
    if baseline_date <= on_date:
        baseline_views = _build_views(state, baseline_date, baseline_date, method)
        baseline_core_eligible = [
            view for _, view in sorted(baseline_views.items()) if view["eligible"]
        ]
        for definition in [core_def] + [
            state["indexes"][child_id]
            for child_id in sorted(state["indexes"])
            if state["indexes"][child_id]["parent_id"] == core_id
        ]:
            base_members = _members_for(definition, baseline_views, baseline_core_eligible, baseline_date)
            try:
                base_weights, _ = _compute_weights(base_members, method, state, baseline_date)
                baseline_waci_by_index[definition["index_id"]] = sum(
                    (base_weights[view["bond_id"]] * view["carbon_intensity"] for view in base_members),
                    Fraction(0),
                )
            except ValidationError:
                baseline_waci_by_index[definition["index_id"]] = None
    else:
        baseline_waci_by_index = {index_id: None for index_id in state["indexes"]}

    index_results: dict[str, Any] = {}
    index_weights: dict[str, Fraction] = {}
    core_waci: Fraction | None = None
    for definition in [core_def] + [
        state["indexes"][child_id]
        for child_id in sorted(state["indexes"])
        if state["indexes"][child_id]["parent_id"] == core_id
    ]:
        members = _members_for(definition, views, core_eligible, on_date)
        weights, detail = _compute_weights(members, method, state, on_date)
        result = _index_result(
            definition, members, weights, detail, baseline_waci_by_index[definition["index_id"]]
        )
        if definition["parent_id"] is None:
            core_waci = Fraction(result["waci_exact"])
        else:
            result["delta_vs_core"] = (
                decimal_str(Fraction(result["waci_exact"]) - core_waci, 8)
                if core_waci is not None
                else None
            )
        index_results[definition["index_id"]] = result
        index_weights.update(weights)

    eligibility_audit = [
        {
            "bond_id": bond_id,
            "eligible": view["eligible"],
            "rating": view["rating"],
            "matured": view["matured"],
            "suspended": view["suspended"],
            "emission_record_id": view["emission"]["record_id"] if view["emission"] else None,
            "emission_version": view["emission"]["version"] if view["emission"] else None,
            "emission_sensitivity": (
                view["emission"].get("sensitivity") if view["emission"] else None
            ),
            "reasons": view["reasons"],
        }
        for bond_id, view in sorted(views.items())
    ]

    return {
        "rebalance_date": on_date,
        "watermark": watermark,
        "method_key": f"{method['method_id']}:{method['version']}",
        "method_snapshot": {
            "method_id": method["method_id"],
            "version": method["version"],
            "carbon_scope": method["carbon_scope"],
            "weight_scheme": method["weight_scheme"],
            "screens": method["screens"],
            "windows": method["windows"],
            "baseline_date": method["baseline_date"],
        },
        "shared_eligibility": eligibility_audit,
        "core_index_id": core_id,
        "indexes": index_results,
        "restricted_emission_used": any(
            view["emission"] is not None
            and view["emission"].get("sensitivity") == SENSITIVITY_RESTRICTED
            and view["eligible"]
            for view in views.values()
        ),
    }
