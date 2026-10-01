"""主数据与方法登记：发行人、债券、来源、口径、筛选、权重、指数族、日历。"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any

from .access_control import Principal, require
from .errors import DuplicateError, NotFoundError, ValidationError
from .models import (RATING_ORDER, Bond, ClimateDataSource, ComplianceScreen,
                     EmissionsScope, IndexDef, Issuer, MethodVersion,
                     RebalanceCalendarEntry, WeightMethod)
from .store import Store

# 该族约定的子指数数量
EXPECTED_SUB_INDEX_COUNT = 6


def _put(state: dict[str, Any], collection: str, key: str, value: dict[str, Any]) -> None:
    if key in state[collection]:
        raise DuplicateError(f"{collection} 中 {key} 已登记")
    state[collection][key] = value


def register_issuer(store: Store, principal: Principal, issuer: Issuer) -> str:
    require(principal, "master_data:write")
    if not issuer.issuer_id.strip() or not issuer.name.strip():
        raise ValidationError("发行人编号与名称不能为空")
    if issuer.rating not in RATING_ORDER:
        raise ValidationError(f"未知评级：{issuer.rating}")

    def fn(state: dict[str, Any]) -> str:
        _put(state, "issuers", issuer.issuer_id, asdict(issuer))
        return issuer.issuer_id

    return store.mutate(fn)


def register_bond(store: Store, principal: Principal, bond: Bond) -> str:
    require(principal, "master_data:write")
    if bond.par_amount <= 0:
        raise ValidationError("发行量必须为正")

    def fn(state: dict[str, Any]) -> str:
        if bond.issuer_id not in state["issuers"]:
            raise NotFoundError(f"发行人 {bond.issuer_id} 未登记")
        _put(state, "bonds", bond.bond_id, asdict(bond))
        return bond.bond_id

    return store.mutate(fn)


def register_source(store: Store, principal: Principal,
                    source: ClimateDataSource) -> str:
    require(principal, "master_data:write")
    if not source.authorized_roles and source.restriction.value == "restricted":
        raise ValidationError("受限来源必须登记授权角色")

    def fn(state: dict[str, Any]) -> str:
        payload = asdict(source)
        payload["authorized_roles"] = sorted(r.value for r in source.authorized_roles)
        payload["restriction"] = source.restriction.value
        _put(state, "sources", source.source_id, payload)
        return source.source_id

    return store.mutate(fn)


def register_scope(store: Store, principal: Principal, scope: EmissionsScope) -> str:
    require(principal, "method:write")
    if not (scope.include_scope1 or scope.include_scope2 or scope.include_scope3):
        raise ValidationError("排放口径至少包含一个排放范围")

    def fn(state: dict[str, Any]) -> str:
        _put(state, "scopes", scope.scope_id, asdict(scope))
        return scope.scope_id

    return store.mutate(fn)


def register_screen(store: Store, principal: Principal,
                    screen: ComplianceScreen) -> str:
    require(principal, "method:write")
    if screen.min_rating is not None and screen.min_rating not in RATING_ORDER:
        raise ValidationError(f"筛选规则使用未知评级：{screen.min_rating}")

    def fn(state: dict[str, Any]) -> str:
        _put(state, "screens", screen.screen_id, asdict(screen))
        return screen.screen_id

    return store.mutate(fn)


def register_weight_method(store: Store, principal: Principal,
                           method: WeightMethod) -> str:
    require(principal, "method:write")
    if method.scheme not in {"market_cap", "par", "equal"}:
        raise ValidationError(f"未知权重方案：{method.scheme}")
    if method.cap_pct is not None and not Decimal("0") < method.cap_pct <= Decimal("1"):
        raise ValidationError("权重上限必须在 (0,1] 之间")

    def fn(state: dict[str, Any]) -> str:
        _put(state, "weight_methods", method.method_id, asdict(method))
        return method.method_id

    return store.mutate(fn)


def register_index(store: Store, principal: Principal, index: IndexDef) -> str:
    """登记指数定义并校验族依赖：至多一个核心，子指数必须挂在已登记核心下。

    族内共享合格性判断与排放口径，因此子指数必须与核心使用同一筛选与口径；
    权重方法可以不同，各指数在自身范围内独立守恒。
    """

    require(principal, "method:write")

    def fn(state: dict[str, Any]) -> str:
        for coll, key, label in (
            ("screens", index.screen_id, "合规筛选"),
            ("weight_methods", index.weight_method_id, "权重方法"),
            ("scopes", index.scope_id, "排放口径"),
        ):
            if key not in state[coll]:
                raise NotFoundError(f"{label} {key} 未登记")

        if index.parent_id is None:
            for other in state["indexes"].values():
                if other["parent_id"] is None:
                    raise DuplicateError("核心指数已存在，一个指数族只能有一个核心")
        else:
            parent = state["indexes"].get(index.parent_id)
            if parent is None:
                raise NotFoundError(f"父指数 {index.parent_id} 未登记")
            if parent["parent_id"] is not None:
                raise ValidationError("子指数只能挂在核心指数下")
            if parent["screen_id"] != index.screen_id:
                raise ValidationError("子指数必须与核心共享合规筛选（合格性判断族内共享）")
            if parent["scope_id"] != index.scope_id:
                raise ValidationError("子指数必须与核心使用同一排放口径")
            siblings = [o for o in state["indexes"].values()
                        if o["parent_id"] == index.parent_id]
            if len(siblings) >= EXPECTED_SUB_INDEX_COUNT:
                raise ValidationError(
                    f"核心下已有 {EXPECTED_SUB_INDEX_COUNT} 只子指数")
        _put(state, "indexes", index.index_id, asdict(index))
        return index.index_id

    return store.mutate(fn)


def validate_family_complete(state: dict[str, Any]) -> tuple[str, list[str]]:
    """返回 (核心指数id, 子指数id列表)，并要求恰为核心 + 六只子指数。"""

    cores = [i for i in state["indexes"].values() if i["parent_id"] is None]
    if len(cores) != 1:
        raise ValidationError("指数族必须恰好包含一个核心指数")
    core_id = cores[0]["index_id"]
    subs = sorted(i["index_id"] for i in state["indexes"].values()
                  if i["parent_id"] == core_id)
    if len(subs) != EXPECTED_SUB_INDEX_COUNT:
        raise ValidationError(
            f"核心指数下必须有 {EXPECTED_SUB_INDEX_COUNT} 只子指数，当前 {len(subs)} 只")
    return core_id, subs


def register_method_version(store: Store, principal: Principal,
                            mv: MethodVersion) -> str:
    """登记方法版本。版本号只能递增，版本内容不可变。"""

    require(principal, "method:write")
    allowed_kinds = {"maturity", "suspension", "resumption",
                     "rating_change", "data_withdrawal"}
    bad = set(mv.window_days) - allowed_kinds
    if bad:
        raise ValidationError(f"生效窗口包含未知事件类型：{sorted(bad)}")
    if any(d < 0 for d in mv.window_days.values()):
        raise ValidationError("生效窗口天数不能为负")

    def fn(state: dict[str, Any]) -> str:
        for coll, key, label in (
            ("screens", mv.screen_id, "合规筛选"),
            ("weight_methods", mv.weight_method_id, "权重方法"),
            ("scopes", mv.scope_id, "排放口径"),
        ):
            if key not in state[coll]:
                raise NotFoundError(f"{label} {key} 未登记")
        if mv.method_version_id in state["method_versions"]:
            raise DuplicateError(f"方法版本 {mv.method_version_id} 已登记")
        same_no = [v for v in state["method_versions"].values()
                   if v["version_no"] == mv.version_no]
        if same_no:
            raise DuplicateError(f"方法版本号 {mv.version_no} 已被使用")
        higher = [v for v in state["method_versions"].values()
                  if v["version_no"] > mv.version_no]
        if higher:
            raise ValidationError("方法版本号必须严格递增，不能插入旧版本")
        # 旧版本自动失活，保证同一时刻只有一个生效版本（历史版本仍可被历史 run 引用）
        for old in state["method_versions"].values():
            old["active"] = False
        payload = asdict(mv)
        state["method_versions"][mv.method_version_id] = payload
        return mv.method_version_id

    return store.mutate(fn)


def add_calendar_entry(store: Store, principal: Principal,
                       entry: RebalanceCalendarEntry) -> None:
    require(principal, "calendar:write")

    def fn(state: dict[str, Any]) -> None:
        dates = {e["rebalance_date"] for e in state["calendar"]}
        if entry.rebalance_date in dates:
            raise DuplicateError(f"调仓日 {entry.rebalance_date} 已在日历中")
        state["calendar"].append(asdict(entry))
        state["calendar"].sort(key=lambda e: e["rebalance_date"])

    store.mutate(fn)
