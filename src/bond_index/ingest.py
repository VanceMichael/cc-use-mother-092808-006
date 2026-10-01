"""行情、披露、事件、基准批次入账。

幂等规则：
- 同 ``batch_id`` 重送且内容哈希一致 → 识别为重复，直接跳过，不重复入账；
- 同 ``batch_id`` 重送但内容哈希不同 → 批次置为 ``conflict`` 并隔离，
  同时阻止相关指数发布，直到冲突解除（以批准重述流程处理）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from .access_control import Principal, gate_source, require
from .errors import (BatchConflictError, NotFoundError, RestrictedDataError,
                     ValidationError)
from .models import (DEFAULT_WINDOWS, BatchKind, ClimateDataSource, EventKind,
                     RestrictionLevel, Role)
from .store import Store, canonical_hash

# ---------------------------------------------------------------------------
# 生效窗口
# ---------------------------------------------------------------------------

def _add_days(d: str, days: int) -> str:
    y, m, dd = (int(x) for x in d.split("-"))
    base = date(y, m, dd).toordinal()
    return date.fromordinal(base + days).isoformat()


def effective_date(event_date: str, kind: EventKind,
                   window_days: dict[str, int] | None = None) -> str:
    """按事件类型对应的生效窗口计算生效日（默认窗口见 models.DEFAULT_WINDOWS）。

    供登记前预览；计算时生效日由引擎按 run 锁定的方法版本窗口解析。
    """

    windows = dict(DEFAULT_WINDOWS)
    if window_days:
        windows.update(window_days)
    return _add_days(event_date, int(windows.get(kind.value, 0)))


# ---------------------------------------------------------------------------
# 批次管理
# ---------------------------------------------------------------------------

def _state_batch(state: dict[str, Any], batch_id: str) -> dict[str, Any] | None:
    return state["batches"].get(batch_id)


def ingest_batch(store: Store, principal: Principal, batch_id: str,
                 kind: BatchKind, entries: list[dict[str, Any]],
                 *, received_at: str) -> str:
    """通用批次入账，返回状态：``accepted`` / ``duplicate`` / ``conflict``。"""

    require(principal, "batch:ingest")
    if not entries:
        raise ValidationError("批次内容不能为空")
    digest = canonical_hash({"kind": kind.value, "entries": entries})

    def fn(state: dict[str, Any]) -> str:
        existing = _state_batch(state, batch_id)
        if existing is not None:
            if existing["hash"] == digest:
                return "duplicate"  # 完全重送：不重复入账
            existing["status"] = "conflict"
            existing["blocked"] = True
            existing["conflicting_hash"] = digest
            existing["conflict_kind"] = kind.value
            # 冲突不在此抛出，落盘隔离后由调用方/发布闸门感知
            return "conflict"

        state["batches"][batch_id] = {
            "batch_id": batch_id,
            "kind": kind.value,
            "hash": digest,
            "status": "accepted",
            "blocked": False,
            "received_at": received_at,
            "data_as_of": _batch_data_as_of(kind, entries),
        }
        _apply_entries(state, batch_id, kind, entries, received_at)
        return "accepted"

    status = store.mutate(fn)
    if status == "conflict":
        # 抛出便于调用方即时感知；状态已隔离，相关发布由闸门阻止
        raise BatchConflictError(batch_id)
    return status


def _apply_entries(state: dict[str, Any], batch_id: str, kind: BatchKind,
                   entries: list[dict[str, Any]], received_at: str) -> None:
    if kind is BatchKind.MARKET:
        for e in entries:
            state["market_prices"].append({
                "bond_id": e["bond_id"], "as_of": e["as_of"],
                "price": Decimal(str(e["price"])),
                "currency": e.get("currency", "CNY"),
                "batch_id": batch_id,
            })
    elif kind is BatchKind.DISCLOSURE:
        for e in entries:
            rec_id = e["record_id"]
            if rec_id in state["disclosures"]:
                raise ValidationError(f"披露记录 {rec_id} 已存在于其他批次")
            state["disclosures"][rec_id] = {
                "record_id": rec_id,
                "issuer_id": e["issuer_id"],
                "source_id": e["source_id"],
                "report_period": str(e["report_period"]),
                "emissions_tco2e": Decimal(str(e["emissions_tco2e"])),
                "revenue": Decimal(str(e["revenue"])),
                "reported_on": e["reported_on"],
                "batch_id": batch_id,
                "withdrawn": False,
                "superseded_by": None,
            }
    elif kind is BatchKind.EVENT:
        for e in entries:
            ev_id = e["event_id"]
            if ev_id in state["events"]:
                raise ValidationError(f"事件 {ev_id} 已存在于其他批次")
            kind_enum = EventKind(e["kind"])
            # 生效日不在入账时按"当前激活版本"烧录：事件只登记事件日，
            # 生效日由每次计算按其锁定的方法版本窗口解析，
            # 以保证不同方法版本重算历史时窗口规则各自成立。
            # 允许登记方给出显式生效日（监管裁定等），此时标记为不可覆盖。
            explicit = "effective_date" in e
            state["events"][ev_id] = {
                "event_id": ev_id,
                "bond_id": e.get("bond_id"),
                "issuer_id": e.get("issuer_id"),
                "kind": kind_enum.value,
                "event_date": e["event_date"],
                "effective_date": e["effective_date"] if explicit else None,
                "effective_explicit": explicit,
                "old_rating": e.get("old_rating"),
                "new_rating": e.get("new_rating"),
                "record_id": e.get("record_id"),
                "batch_id": batch_id,
                "received_at": received_at,
                "applied": False,
            }
    elif kind is BatchKind.BENCHMARK:
        for e in entries:
            state["benchmark"].append({
                "as_of": e["as_of"],
                "carbon_intensity": Decimal(str(e["carbon_intensity"])),
                "batch_id": batch_id,
            })
            state["benchmark"].sort(key=lambda p: p["as_of"])


def _batch_data_as_of(kind: BatchKind, entries: list[dict[str, Any]]) -> str | None:
    """批次覆盖的业务数据日期（行情/基准为 as_of，披露为 reported_on）。"""

    keys = {
        BatchKind.MARKET: "as_of",
        BatchKind.BENCHMARK: "as_of",
        BatchKind.DISCLOSURE: "reported_on",
        BatchKind.EVENT: "event_date",
    }
    key = keys[kind]
    dates = sorted(e[key] for e in entries if e.get(key))
    return dates[-1] if dates else None


def has_open_conflict(state: dict[str, Any], *, kinds: set[str] | None = None) -> bool:
    """发布闸门用：是否存在未解除的批次冲突。"""

    for b in state["batches"].values():
        if b.get("status") == "conflict" and (kinds is None or b["kind"] in kinds):
            return True
    return False


def conflicts_relevant_to(state: dict[str, Any], as_of: str) -> list[str]:
    """返回影响指定调仓日发布的冲突批次编号。

    冲突批次业务数据日期 <= 调仓日即"相关"（该数据本应进入该日水位）；
    事后日期的批次冲突不阻断更早调仓日的发布。
    """

    return sorted(
        bid for bid, b in state["batches"].items()
        if b.get("status") == "conflict"
        and (b.get("data_as_of") is None or b["data_as_of"] <= as_of)
    )


# ---------------------------------------------------------------------------
# 受限数据检索
# ---------------------------------------------------------------------------

def _source_of(state: dict[str, Any], source_id: str) -> ClimateDataSource | None:
    raw = state["sources"].get(source_id)
    if raw is None:
        return None
    return ClimateDataSource(
        source_id=raw["source_id"], name=raw["name"],
        restriction=RestrictionLevel(raw["restriction"]),
        authorized_roles=frozenset(Role(r) for r in raw["authorized_roles"]),
        description=raw.get("description", ""))


def get_disclosure(store: Store, principal: Principal, record_id: str) -> dict[str, Any]:
    """按编号取披露。受限来源对无权者既不返回内容也不确认存在。"""

    state = store.view()
    record = state["disclosures"].get(record_id)
    if record is None:
        # 关闭存在性侧信道：对没有受限读权的角色，"编号不存在"与
        # "存在但受限"返回完全相同的拒绝，使其无法枚举受限记录编号。
        from .access_control import PERMISSIONS

        if principal.role not in PERMISSIONS["restricted:read"]:
            raise RestrictedDataError(
                "受限气候数据访问被拒绝（不披露相关数据是否存在）")
        raise NotFoundError(f"披露记录 {record_id} 不存在")
    gate_source(principal, _source_of(state, record["source_id"]))
    return record


def search_disclosures(store: Store, principal: Principal, *,
                       issuer_id: str | None = None,
                       source_id: str | None = None,
                       report_period: str | None = None) -> list[dict[str, Any]]:
    """检索披露。

    若检索条件显式指定了受限来源，无权调用者直接被存在性闸门拦截；
    跨来源检索时，无权者结果集中的受限记录被整条移除（而非返回打码行，
    因为打码行本身就会确认存在性）。
    """

    state = store.view()
    if source_id is not None:
        gate_source(principal, _source_of(state, source_id))

    results: list[dict[str, Any]] = []
    for rec in state["disclosures"].values():
        if issuer_id is not None and rec["issuer_id"] != issuer_id:
            continue
        if source_id is not None and rec["source_id"] != source_id:
            continue
        if report_period is not None and rec["report_period"] != report_period:
            continue
        source = _source_of(state, rec["source_id"])
        if source is not None and source.restriction is RestrictionLevel.RESTRICTED:
            # 显式受限来源的情况上面已拦截；这里覆盖跨来源检索：
            # 无权者看到的结果集中根本不出现受限记录。
            from .access_control import can_read_restricted

            if not can_read_restricted(principal, source):
                continue
        results.append(rec)
    return results


def has_restricted_target(store: Store, principal: Principal,
                          source_id: str) -> bool:
    """供外部探测接口使用：无权者对受限来源只会收到拒绝，不得到布尔值。"""

    state = store.view()
    gate_source(principal, _source_of(state, source_id))
    return source_id in state["sources"]
