"""事件生效窗口规则。

所有判断都以某个调仓日 ``d`` 为参照，事件只有越过自身窗口边界后才影响该调仓日：

- 到期：``maturity <= d`` 当日起退出；
- 评级：公告日 + 通知滞后天数生效（滞后内仍按旧评级）；
- 停牌：停牌起有 ``grace_days`` 个自然日补救期，期内保留，超期且未复牌则退出；
  复牌日（含）起恢复；
- 数据撤回：撤回起 ``cure_days`` 个自然日补救期内旧值仍可使用，超期视为数据缺失；
  撤回后若已有经批准重述的新版本，计算层直接改用新版本。
"""

from __future__ import annotations

from datetime import date
from typing import Any


def _d(value: str) -> date:
    return date.fromisoformat(value)


def matured(bond: dict[str, Any], on_date: str) -> bool:
    """到期日当日及以后不再属于指数。"""
    return _d(bond["maturity"]) <= _d(on_date)


def effective_rating(
    rating_events: list[dict[str, Any]], on_date: str, lag_days: int
) -> str | None:
    """计算调仓日有效的评级等级。

    取公告日 + 滞后天数不晚于 ``on_date`` 的最近一次评级；尚未越过通知窗口的
    公告不生效；从无评级事件则返回 None（筛选时按不合格处理）。
    """
    effective: tuple[str, str] | None = None  # (公告日, 等级)
    for event in rating_events:
        becomes_effective = date.fromordinal(_d(event["announced"]).toordinal() + lag_days)
        if becomes_effective <= _d(on_date):
            if effective is None or event["announced"] >= effective[0]:
                effective = (event["announced"], event["grade"])
    return effective[1] if effective else None


def is_suspended(
    suspension: dict[str, Any] | None, on_date: str, grace_days: int
) -> bool:
    """停牌是否已越过补救期而导致成分退出。"""
    if not suspension:
        return False
    today = _d(on_date)
    start = _d(suspension["start"])
    if today < start:
        return False
    resume = suspension.get("resume")
    if resume and _d(resume) <= today:
        return False  # 已复牌
    grace_end = date.fromordinal(start.toordinal() + grace_days)
    # 补救期最后一天（含）仍保留。
    return today > grace_end


def emission_within_cure_period(
    record: dict[str, Any], on_date: str, cure_days: int
) -> bool:
    """撤回的旧数据是否仍处于补救期内可用。"""
    if not record.get("withdrawn"):
        return True
    withdrawn_on = record.get("withdrawn_on")
    if not withdrawn_on:
        return True
    cure_end = date.fromordinal(_d(withdrawn_on).toordinal() + cure_days)
    return _d(on_date) <= cure_end


def rating_effective_date(announced: str, lag_days: int) -> str:
    """评级公告的最早生效日（供审计与解释使用）。"""
    return date.fromordinal(_d(announced).toordinal() + lag_days).isoformat()


def suspension_exit_date(start: str, grace_days: int) -> str:
    """停牌不获复牌时的退出生效日（补救期满次日）。"""
    return date.fromordinal(_d(start).toordinal() + grace_days + 1).isoformat()


def withdrawal_exit_date(withdrawn_on: str, cure_days: int) -> str:
    """数据撤回后补救期满、数据视为缺失的日期。"""
    return date.fromordinal(_d(withdrawn_on).toordinal() + cure_days + 1).isoformat()
