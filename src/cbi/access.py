"""访问控制：角色校验与受限气候数据的存在性不可区分。"""

from __future__ import annotations

from typing import Any

from .errors import AccessDenied, AuthorizationError, NotFound
from .model import CLIMATE_VIEWER_ROLES, SENSITIVITY_RESTRICTED, Principal


def require_role(principal: Principal, role: str) -> None:
    """要求主体具备某一角色。"""
    if not principal.has(role):
        raise AuthorizationError(f"用户 {principal.user_id} 缺少角色 {role}")


def require_any_role(principal: Principal, roles: frozenset[str] | set[str]) -> None:
    if not any(principal.has(role) for role in roles):
        raise AuthorizationError(
            f"用户 {principal.user_id} 至少需要角色之一: {sorted(roles)}"
        )


def require_distinct_user(principal: Principal, other_user: str, action: str) -> None:
    """职责分离：批准人不得是提议人本人。"""
    if principal.user_id == other_user:
        raise AuthorizationError(f"{action}必须由提议人之外的另一角色执行")


def public_or_visible(record: dict[str, Any] | None, principal: Principal) -> dict[str, Any]:
    """获取单条气候记录。

    无权访问受限数据时，“受限存在”和“记录不存在”抛出完全相同的错误，
    错误信息不含存在性线索，调用方无法据此区分。
    """
    if record is None:
        raise NotFound("气候数据不存在或不可见")
    if record.get("sensitivity") == SENSITIVITY_RESTRICTED and not (
        principal.roles & CLIMATE_VIEWER_ROLES
    ):
        # 与“不存在”使用同一类型、同一文案。
        raise NotFound("气候数据不存在或不可见")
    return record


def filter_visible_emissions(
    records: list[dict[str, Any]], principal: Principal
) -> list[dict[str, Any]]:
    """搜索场景：受限记录对无权角色整体不可见（不出现在结果中）。"""
    if principal.roles & CLIMATE_VIEWER_ROLES:
        return list(records)
    return [record for record in records if record.get("sensitivity") != SENSITIVITY_RESTRICTED]


def assert_climate_viewer(principal: Principal) -> None:
    """计算等内部链路使用：显式确认主体可见受限数据。"""
    if not (principal.roles & CLIMATE_VIEWER_ROLES):
        raise AccessDenied("气候数据不存在或不可见")
