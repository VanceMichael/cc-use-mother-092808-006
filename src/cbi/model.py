"""领域模型：以 JSON 兼容字典表示的登记记录与枚举。

日期统一使用 ISO 字符串 ``YYYY-MM-DD``，金额与排放量使用十进制字符串或整数，
权重在计算层使用 :class:`fractions.Fraction` 保证精确守恒。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from .errors import ValidationError

# --------------------------------------------------------------------------- 角色


ROLE_DATA_STEWARD = "data_steward"        # 债券主数据人员
ROLE_CLIMATE_PROVIDER = "climate_provider"  # 气候数据供应方
ROLE_METHOD_OWNER = "method_owner"        # 方法维护者
ROLE_METHOD_APPROVER = "method_approver"  # 方法审批人员（须与提议者不同）
ROLE_INDEX_CALCULATOR = "index_calculator"  # 指数编制操作员
ROLE_INVESTOR = "investor"                # 机构投资者

ALL_ROLES = frozenset(
    {
        ROLE_DATA_STEWARD,
        ROLE_CLIMATE_PROVIDER,
        ROLE_METHOD_OWNER,
        ROLE_METHOD_APPROVER,
        ROLE_INDEX_CALCULATOR,
        ROLE_INVESTOR,
    }
)

# 可以看到受限气候数据数值的角色；其他角色连数据是否存在都无法确认。
CLIMATE_VIEWER_ROLES = frozenset(
    {ROLE_CLIMATE_PROVIDER, ROLE_INDEX_CALCULATOR, ROLE_METHOD_APPROVER}
)

# --------------------------------------------------------------------------- 枚举

# 排放口径（GHG Protocol 范围）
SCOPE_S1 = "S1"        # 范围一
SCOPE_S12 = "S12"      # 范围一加范围二
SCOPE_S123 = "S123"    # 范围一加范围二加范围三
CARBON_SCOPES = (SCOPE_S1, SCOPE_S12, SCOPE_S123)

# 数据密级
SENSITIVITY_PUBLIC = "public"
SENSITIVITY_RESTRICTED = "restricted"

# 批次类型
BATCH_MASTER = "master"        # 主数据批次
BATCH_QUOTE = "quote"          # 行情批次
BATCH_DISCLOSURE = "disclosure"  # 披露批次
BATCH_TYPES = (BATCH_MASTER, BATCH_QUOTE, BATCH_DISCLOSURE)

BATCH_STATE_RECEIVED = "received"      # 已入账
BATCH_STATE_REPLAYED = "replayed"      # 重送且内容一致（不重复入账）
BATCH_STATE_BLOCKED = "blocked"        # 重送但内容不同（阻断相关发布）

# 权重方法
WEIGHT_EQUAL = "equal"          # 等权
WEIGHT_MARKET_VALUE = "mv"      # 市值加权
WEIGHT_SCHEMES = (WEIGHT_EQUAL, WEIGHT_MARKET_VALUE)

# 方法版本状态
METHOD_DRAFT = "draft"
METHOD_ACTIVE = "active"
METHOD_DEACTIVATED = "deactivated"

# 调仓任务状态（任务日志）
TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_DONE = "done"
TASK_FAILED = "failed"

# 重述提议状态
RESTATE_PROPOSED = "proposed"
RESTATE_APPROVED = "approved"
RESTATE_REJECTED = "rejected"

# 重述类型
RESTATE_DATA = "data"
RESTATE_METHOD = "method"

# --------------------------------------------------------------------------- 评级

# 序号越小信用越高
RATING_GRADES = (
    "AAA", "AA+", "AA", "AA-", "A+", "A", "A-",
    "BBB+", "BBB", "BBB-",
    "BB+", "BB", "BB-", "B+", "B", "B-",
    "CCC", "CC", "C",
)
_RATING_INDEX = {grade: index for index, grade in enumerate(RATING_GRADES)}


def rating_at_least(grade: str, minimum: str) -> bool:
    """``grade`` 是否达到 ``minimum``（含）以上。"""
    if grade not in _RATING_INDEX or minimum not in _RATING_INDEX:
        raise ValidationError(f"未知评级: {grade!r}")
    return _RATING_INDEX[grade] <= _RATING_INDEX[minimum]


# --------------------------------------------------------------------------- 基础校验


def iso_date(value: Any, field: str) -> str:
    """校验并归一化 ISO 日期。"""
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 字符串")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValidationError(f"{field} 日期无效: {value!r}") from exc


def require(value: Any, field: str) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError(f"{field} 不能为空")
    return value


def positive_number(value: Any, field: str) -> str:
    """正数归一化为 Decimal 字符串，拒绝 NaN/无穷。"""
    try:
        decimal = Decimal(str(value))
    except Exception as exc:
        raise ValidationError(f"{field} 不是数值") from exc
    if not decimal.is_finite() or decimal <= 0:
        raise ValidationError(f"{field} 必须为正数")
    return str(decimal)


def non_negative_number(value: Any, field: str) -> str:
    try:
        decimal = Decimal(str(value))
    except Exception as exc:
        raise ValidationError(f"{field} 不是数值") from exc
    if not decimal.is_finite() or decimal < 0:
        raise ValidationError(f"{field} 不能为负")
    return str(decimal)


# --------------------------------------------------------------------------- 记录工厂


@dataclass(frozen=True)
class Principal:
    """调用主体：用户及其角色。"""

    user_id: str
    roles: frozenset[str]

    @staticmethod
    def of(user_id: str, roles: Iterable[str]) -> "Principal":
        require(user_id, "用户标识")
        roles = frozenset(roles)
        unknown = roles - ALL_ROLES
        if unknown:
            raise ValidationError(f"未知角色: {sorted(unknown)}")
        return Principal(user_id=user_id.strip(), roles=roles)

    def has(self, role: str) -> bool:
        return role in self.roles


def issuer(issuer_id: str, name: str, sector: str, jurisdiction: str) -> dict[str, Any]:
    """发行人主数据。"""
    require(issuer_id, "发行人标识")
    require(name, "发行人名称")
    require(sector, "行业")
    require(jurisdiction, "司法辖区")
    return {
        "issuer_id": issuer_id.strip(),
        "name": name.strip(),
        "sector": sector.strip(),
        "jurisdiction": jurisdiction.strip(),
    }


def bond(
    bond_id: str,
    issuer_id: str,
    name: str,
    face_amount: Any,
    maturity: str,
    green: bool,
) -> dict[str, Any]:
    """债券主数据。face_amount 为发行量（面值）。"""
    require(bond_id, "债券标识")
    require(issuer_id, "发行人标识")
    require(name, "债券名称")
    if not isinstance(green, bool):
        raise ValidationError("绿色标识必须为布尔值")
    return {
        "bond_id": bond_id.strip(),
        "issuer_id": issuer_id.strip(),
        "name": name.strip(),
        "face_amount": positive_number(face_amount, "发行量"),
        "maturity": iso_date(maturity, "到期日"),
        "green": green,
    }


def rating_event(bond_id: str, announced: str, grade: str) -> dict[str, Any]:
    """评级事件：announced 为公告日，按方法中的通知滞后天数进入生效窗口。"""
    require(bond_id, "债券标识")
    if grade not in _RATING_INDEX:
        raise ValidationError(f"未知评级: {grade!r}")
    return {
        "bond_id": bond_id.strip(),
        "announced": iso_date(announced, "评级公告日"),
        "grade": grade,
    }


def suspension_event(bond_id: str, start: str, resume: str | None = None) -> dict[str, Any]:
    """停牌/复牌事件；resume 为空表示截至处理时仍未复牌。"""
    require(bond_id, "债券标识")
    record = {
        "bond_id": bond_id.strip(),
        "start": iso_date(start, "停牌起始日"),
        "resume": iso_date(resume, "复牌日") if resume else None,
    }
    if resume and record["resume"] < record["start"]:
        raise ValidationError("复牌日不能早于停牌起始日")
    return record


def climate_source(source_id: str, name: str) -> dict[str, Any]:
    """气候数据来源登记。"""
    require(source_id, "来源标识")
    require(name, "来源名称")
    return {"source_id": source_id.strip(), "name": name.strip()}


def emission_record(
    record_id: str,
    issuer_id: str,
    source_id: str,
    period_start: str,
    period_end: str,
    received: str,
    revenue: Any,
    emissions_by_scope: dict[str, Any],
    sensitivity: str = SENSITIVITY_PUBLIC,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """一条排放披露（不可变版本，重述通过新版本表达）。

    排放量按口径存放：``{"S1": .., "S12": .., "S123": ..}``，单位 tCO2e；
    revenue 为报告期收入。received 为数据到达时间（水位判定用）。
    """
    require(record_id, "排放记录标识")
    require(issuer_id, "发行人标识")
    require(source_id, "气候数据来源")
    start = iso_date(period_start, "报告期开始")
    end = iso_date(period_end, "报告期结束")
    if end < start:
        raise ValidationError("报告期结束不能早于开始")
    received_iso = iso_date(received, "数据到达日")
    if not isinstance(emissions_by_scope, dict) or not emissions_by_scope:
        raise ValidationError("至少需要一个排放口径的数据")
    scopes: dict[str, str] = {}
    for scope_key, value in emissions_by_scope.items():
        if scope_key not in CARBON_SCOPES:
            raise ValidationError(f"未知排放口径: {scope_key!r}")
        scopes[scope_key] = non_negative_number(value, f"排放量({scope_key})")
    if sensitivity not in (SENSITIVITY_PUBLIC, SENSITIVITY_RESTRICTED):
        raise ValidationError("未知数据密级")
    return {
        "record_id": record_id.strip(),
        "issuer_id": issuer_id.strip(),
        "source_id": source_id.strip(),
        "period_start": start,
        "period_end": end,
        "received": received_iso,
        "revenue": positive_number(revenue, "报告期收入"),
        "emissions": scopes,
        "sensitivity": sensitivity,
        "batch_id": batch_id,
        "withdrawn": False,
        "withdrawn_on": None,
        "version_of": record_id.strip(),  # 同一业务记录的稳定标识
        "version": 1,
        "superseded_by": None,
        "restatement_id": None,
    }


def screening_rules(
    require_green: bool = True,
    min_grade: str = "BBB-",
    require_climate_data: bool = True,
) -> dict[str, Any]:
    """核心指数的合规筛选规则（核心与子指数共享的合格性判断）。"""
    if min_grade not in _RATING_INDEX:
        raise ValidationError(f"未知评级: {min_grade!r}")
    return {
        "require_green": bool(require_green),
        "min_grade": min_grade,
        "require_climate_data": bool(require_climate_data),
    }


def event_windows(
    rating_lag_days: int = 1,
    suspension_grace_days: int = 3,
    withdrawal_cure_days: int = 10,
) -> dict[str, int]:
    """生效窗口参数：评级通知滞后、停牌补救期、数据撤回补救期（自然日）。"""
    for name, value in (
        ("评级滞后天数", rating_lag_days),
        ("停牌补救天数", suspension_grace_days),
        ("撤回补救天数", withdrawal_cure_days),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValidationError(f"{name}必须为非负整数")
    return {
        "rating_lag_days": rating_lag_days,
        "suspension_grace_days": suspension_grace_days,
        "withdrawal_cure_days": withdrawal_cure_days,
    }


def methodology_version(
    method_id: str,
    version: int,
    carbon_scope: str,
    weight_scheme: str,
    screens: dict[str, Any],
    windows: dict[str, int],
    baseline_date: str,
    proposed_by: str = "",
    note: str = "",
) -> dict[str, Any]:
    """方法版本：筛选、口径、权重、窗口、基准日的不可变组合。"""
    require(method_id, "方法标识")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValidationError("方法版本号必须为正整数")
    if carbon_scope not in CARBON_SCOPES:
        raise ValidationError("碳强度排放口径无效")
    if weight_scheme not in WEIGHT_SCHEMES:
        raise ValidationError("权重方案无效")
    # 结构校验
    screening_rules(
        require_green=screens.get("require_green", True),
        min_grade=screens.get("min_grade", "BBB-"),
        require_climate_data=screens.get("require_climate_data", True),
    )
    event_windows(
        rating_lag_days=windows.get("rating_lag_days", 1),
        suspension_grace_days=windows.get("suspension_grace_days", 3),
        withdrawal_cure_days=windows.get("withdrawal_cure_days", 10),
    )
    return {
        "method_id": method_id.strip(),
        "version": version,
        "carbon_scope": carbon_scope,
        "weight_scheme": weight_scheme,
        "screens": screens,
        "windows": windows,
        "baseline_date": iso_date(baseline_date, "基准日"),
        "status": METHOD_DRAFT,
        "proposed_by": proposed_by,
        "activated_by": None,
        "activated_on": None,
        "note": note,
    }


def index_definition(
    index_id: str,
    name: str,
    parent_id: str | None = None,
    dimension_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """指数定义。

    核心指数 ``parent_id`` 为 None；子指数通过 ``dimension_filter`` 表达
    行业/评级/剩余期限等维度，其成员是核心合格集的子集。
    """
    require(index_id, "指数标识")
    require(name, "指数名称")
    if dimension_filter is not None and not isinstance(dimension_filter, dict):
        raise ValidationError("维度筛选必须是字典")
    allowed = {"sector", "min_grade", "max_remaining_years"}
    if dimension_filter:
        unknown = set(dimension_filter) - allowed
        if unknown:
            raise ValidationError(f"不支持的子指数维度: {sorted(unknown)}")
        if "min_grade" in dimension_filter and dimension_filter["min_grade"] not in _RATING_INDEX:
            raise ValidationError("子指数评级阈值无效")
        if "max_remaining_years" in dimension_filter:
            positive_number(dimension_filter["max_remaining_years"], "剩余期限上限")
    return {
        "index_id": index_id.strip(),
        "name": name.strip(),
        "parent_id": parent_id.strip() if parent_id else None,
        "dimension_filter": dimension_filter or {},
    }
