"""访问控制：角色鉴权与受限气候数据存在性隐藏。"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import PermissionDeniedError, RestrictedDataError
from .models import ClimateDataSource, RestrictionLevel, Role

# 各操作所需角色
PERMISSIONS: dict[str, frozenset[Role]] = {
    "master_data:write": frozenset({Role.MASTER_DATA, Role.ADMIN}),
    "calendar:write": frozenset({Role.INDEX_PROVIDER, Role.ADMIN}),
    "method:write": frozenset({Role.METHOD_MAINTAINER, Role.INDEX_PROVIDER, Role.ADMIN}),
    "restatement:propose": frozenset({Role.METHOD_MAINTAINER, Role.INDEX_PROVIDER}),
    "restatement:approve": frozenset({Role.METHOD_APPROVER, Role.INDEX_PROVIDER}),
    "batch:ingest": frozenset({Role.DATA_VENDOR, Role.MASTER_DATA, Role.ADMIN}),
    "rebalance:run": frozenset({Role.INDEX_PROVIDER, Role.ADMIN}),
    "rebalance:publish": frozenset({Role.INDEX_PROVIDER, Role.ADMIN}),
    "reproduction:read": frozenset({Role.INDEX_PROVIDER, Role.INVESTOR,
                                    Role.METHOD_MAINTAINER, Role.METHOD_APPROVER,
                                    Role.MASTER_DATA, Role.ADMIN}),
    "restricted:read": frozenset({Role.DATA_VENDOR, Role.MASTER_DATA,
                                  Role.INDEX_PROVIDER, Role.ADMIN}),
}


@dataclass(frozen=True)
class Principal:
    """调用者身份：编号 + 角色。

    重述 maker-checker 同时比较 user_id（不能自批）与角色
    （提出与批准必须是不同角色身份）。
    """

    user_id: str
    role: Role


def require(principal: Principal, permission: str) -> None:
    """无权限直接拒绝。"""

    allowed = PERMISSIONS.get(permission, frozenset())
    if principal.role not in allowed:
        raise PermissionDeniedError(
            f"角色 {principal.role.value} 无权执行 {permission}")


def can_read_restricted(principal: Principal, source: ClimateDataSource) -> bool:
    """公开来源人人可读；受限来源要求全局读权且在来源授权名单内。"""

    if source.restriction is RestrictionLevel.PUBLIC:
        return True
    if principal.role not in PERMISSIONS["restricted:read"]:
        return False
    return principal.role in source.authorized_roles


def gate_source(principal: Principal, source: ClimateDataSource | None) -> None:
    """受限来源的存在性闸门。

    对无权调用者，只要查询目标是受限来源（或可能命中受限来源的检索），
    无论记录是否存在都抛出 :class:`RestrictedDataError`，
    错误文案不区分"不存在"与"无权访问"，杜绝存在性推断。

    ``source is None`` 表示目标已确定为公开上下文，正常返回。
    """

    if source is None:
        return
    if source.restriction is RestrictionLevel.RESTRICTED and \
            not can_read_restricted(principal, source):
        raise RestrictedDataError(
            "受限气候数据访问被拒绝（不披露相关数据是否存在）")
