"""领域模型与枚举。

时间一律使用 ``YYYY-MM-DD`` 字符串，保持序列化后可读、可比较。
金额与权重使用 :class:`decimal.Decimal`，避免浮点误差破坏权重守恒。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class Role(str, Enum):
    """服务内角色。重述审批要求提出人与批准人为不同角色身份。"""

    INDEX_PROVIDER = "index_provider"        # 指数编制机构
    DATA_VENDOR = "data_vendor"              # 气候数据供应方
    MASTER_DATA = "master_data"              # 债券主数据人员
    METHOD_MAINTAINER = "method_maintainer"  # 方法维护者
    METHOD_APPROVER = "method_approver"      # 方法批准人（另一角色）
    INVESTOR = "investor"                    # 机构投资者
    ADMIN = "admin"                          # 服务管理员，不自动拥有受限读权


class EventKind(str, Enum):
    """债券期限与评级事件类型。"""

    MATURITY = "maturity"        # 到期：按窗口退出
    SUSPENSION = "suspension"    # 停牌：暂停纳入，复牌可恢复
    RESUMPTION = "resumption"    # 复牌
    RATING_CHANGE = "rating_change"  # 评级变化：可能影响合格性
    DATA_WITHDRAWAL = "data_withdrawal"  # 气候数据撤回：相关披露失效


class BatchKind(str, Enum):
    """入账批次类别。"""

    MARKET = "market"       # 行情批次
    DISCLOSURE = "disclosure"  # 气候披露批次
    EVENT = "event"         # 期限/评级事件批次
    BENCHMARK = "benchmark"  # 基准数据批次


class RestrictionLevel(str, Enum):
    """气候数据受限级别。"""

    PUBLIC = "public"              # 公开
    RESTRICTED = "restricted"      # 受限：存在性本身也不可向无权者确认


class RestatementStatus(str, Enum):
    PROPOSED = "proposed"          # 已提出，待批准
    APPROVED = "approved"          # 已由另一角色批准
    REJECTED = "rejected"          # 已驳回
    SUPERSEDED = "superseded"      # 被更新的重述取代


class RebalanceStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PUBLISHED = "published"
    FAILED = "failed"
    BLOCKED = "blocked"


class RunKind(str, Enum):
    ORIGINAL = "original"    # 当时数据水位下的首次计算
    RESTATED = "restated"    # 批准回溯重述后的重算


# 各类事件的默認生效窗口（天）；方法版本可覆盖
DEFAULT_WINDOWS: dict[str, int] = {
    EventKind.MATURITY.value: 0,
    EventKind.SUSPENSION.value: 1,
    EventKind.RESUMPTION.value: 1,
    EventKind.RATING_CHANGE.value: 5,
    EventKind.DATA_WITHDRAWAL.value: 0,
}


# ---------------------------------------------------------------------------
# 主数据
# ---------------------------------------------------------------------------

@dataclass
class Issuer:
    issuer_id: str
    name: str
    sector: str
    rating: str                      # 发行人主体评级
    restricted_climate: bool = False  # 是否持有受限气候数据（仅授权角色可读）


@dataclass
class Bond:
    bond_id: str
    issuer_id: str
    currency: str
    maturity_date: str               # 到期日
    par_amount: Decimal              # 发行量（用于市值/权重基数）
    coupon: Decimal = Decimal("0")
    suspended: bool = False          # 当前是否停牌（由事件窗口推进）


@dataclass
class ClimateDataSource:
    """气候数据来源登记。"""

    source_id: str
    name: str
    restriction: RestrictionLevel
    authorized_roles: frozenset[Role]
    description: str = ""


@dataclass
class EmissionsScope:
    """排放口径：范围1/2/3 与归一基数。

    carbon_intensity = 排放量(吨CO2e) / 基数（这里以营收，单位百万元），
    不同口径版本不得在同一次计算中混用——由方法版本锁定 scope_id。
    """

    scope_id: str
    name: str
    include_scope1: bool = True
    include_scope2: bool = True
    include_scope3: bool = False
    basis: str = "revenue_million_cny"  # 归一基数
    version: int = 1


@dataclass
class DisclosureRecord:
    """单条气候披露观测（按发行人、来源、报告期）。"""

    record_id: str
    issuer_id: str
    source_id: str
    report_period: str             # 如 2025 财年：2025
    emissions_tco2e: Decimal       # 口径对应排放量
    revenue: Decimal               # 归一基数实际值
    reported_on: str               # 供应方报告日期
    withdrawn: bool = False        # 是否被撤回
    superseded_by: str | None = None  # 被哪条新记录取代


@dataclass
class RatingEvent:
    """评级/期限事件，带生效窗口。"""

    event_id: str
    bond_id: str | None            # 发行人级评级事件可为空、以 issuer_id 为准
    issuer_id: str | None
    kind: EventKind
    event_date: str                # 事件发生日
    effective_date: str            # 规则窗口算出的生效日
    old_rating: str | None = None
    new_rating: str | None = None
    record_id: str | None = None   # DATA_WITHDRAWAL 关联的披露记录
    applied: bool = False          # 是否已推进到主数据状态


@dataclass
class ComplianceScreen:
    """合规筛选规则（被方法版本引用）。"""

    screen_id: str
    name: str
    min_rating: str | None = None       # 最低评级（评级序见 RATING_ORDER）
    exclude_suspended: bool = True
    require_live_disclosure: bool = True  # 计算水位日必须有未撤回披露
    minimum_time_to_maturity_days: int = 0  # 生效日距到期的最短剩余期限


# 评级由高到低的全序，min_rating 比较时使用
RATING_ORDER: dict[str, int] = {
    "AAA": 1, "AA+": 2, "AA": 3, "AA-": 4,
    "A+": 5, "A": 6, "A-": 7,
    "BBB+": 8, "BBB": 9, "BBB-": 10,
    "BB+": 11, "BB": 12, "BB-": 13,
    "B": 14, "CCC": 15, "CC": 16, "C": 17, "D": 18,
}


@dataclass
class WeightMethod:
    """权重方法。"""

    method_id: str
    name: str
    scheme: str = "market_cap"   # market_cap / par / equal
    cap_pct: Decimal | None = None  # 单券权重上限（如 0.10）


@dataclass
class IndexDef:
    """指数定义。核心指数 parent_id 为 None；子指数 parent_id 指向核心。"""

    index_id: str
    name: str
    parent_id: str | None
    screen_id: str
    weight_method_id: str
    scope_id: str
    issuer_filter: dict[str, Any] = field(default_factory=dict)
    # issuer_filter 例：{"sectors": ["电力"]} 或 {"bond_currencies": ["CNY"]}


@dataclass
class MethodVersion:
    """方法版本：一次计算固定其编号，锁定筛选、权重、口径与窗口规则。"""

    method_version_id: str
    version_no: int
    screen_id: str
    weight_method_id: str
    scope_id: str
    # 生效窗口（天）：不同事件进入各自窗口
    window_days: dict[str, int] = field(default_factory=dict)
    active: bool = True
    superseded_by: str | None = None
    changelog: str = ""


@dataclass
class RebalanceCalendarEntry:
    """调仓日历：每个调仓日对应一次调仓任务。"""

    rebalance_date: str
    label: str


@dataclass
class BenchmarkPoint:
    """基准碳强度序列点，用于计算指数相对基准差异。"""

    as_of: str
    carbon_intensity: Decimal
