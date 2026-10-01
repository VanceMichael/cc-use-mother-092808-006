"""计算引擎：数据水位、事件生效、合格性、权重守恒与碳指标。

一次计算（run）固定三样不可变输入：
1. 调仓日 ``as_of``；
2. 方法版本（筛选规则、权重方法、排放口径、生效窗口）；
3. 可用数据水位——只纳入 ``reported_on/as_of/event effective_date <= as_of``
   且来自未冲突批次的数据。水位清单带哈希，写入 run 快照后永不改变。

合格性判断由核心指数的筛选规则统一计算，核心与六只子指数共享同一份
合格集合；子指数只在其上叠加自身范围过滤。每个指数在自己的成分范围内
独立归一权重，各自满足权重守恒（权重之和恰为 1）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from .models import DEFAULT_WINDOWS, EventKind, RATING_ORDER
from .store import canonical_hash

_Q = Decimal("0.00000000000001")


def _days_between(a: str, b: str) -> int:
    ya, ma, da = (int(x) for x in a.split("-"))
    yb, mb, db = (int(x) for x in b.split("-"))
    return date(yb, mb, db).toordinal() - date(ya, ma, da).toordinal()


def _add_days(d: str, days: int) -> str:
    y, m, dd = (int(x) for x in d.split("-"))
    return date.fromordinal(date(y, m, dd).toordinal() + days).isoformat()


def _q(value: Decimal) -> Decimal:
    return value.quantize(_Q)


# ---------------------------------------------------------------------------
# 数据水位
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Watermark:
    as_of: str
    method_version_id: str
    method_version_no: int
    scope_id: str
    included_batches: tuple[str, ...]
    excluded_conflict_batches: tuple[str, ...]
    selected_disclosures: dict[str, str]   # issuer_id -> record_id
    price_as_of: dict[str, str]            # bond_id -> 价格日期
    applied_events: tuple[str, ...]
    manifest_hash: str
    knowledge_as_of: str | None = None

    def manifest(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "knowledge_as_of": self.knowledge_as_of,
            "method_version_id": self.method_version_id,
            "method_version_no": self.method_version_no,
            "scope_id": self.scope_id,
            "included_batches": sorted(self.included_batches),
            "excluded_conflict_batches": sorted(self.excluded_conflict_batches),
            "selected_disclosures": dict(sorted(self.selected_disclosures.items())),
            "price_as_of": dict(sorted(self.price_as_of.items())),
            "applied_events": sorted(self.applied_events),
        }


def compute_watermark(state: dict[str, Any], as_of: str,
                      method_version_id: str, *,
                      knowledge_as_of: str | None = None) -> Watermark:
    """固定"可用"数据的水位。

    规则：
    - 冲突批次整批排除（其数据对该水位不可用）；
    - 披露取 reported_on <= 知识截止日、未在水位日前生效撤回的最新记录；
      原始计算知识截止日 = as_of（当时知道什么算什么）；
      事后重述知识截止日 = None（纳入事后送达的更正数据），
      从而"发行人更新排放数据"能够改变历史日的重算结果；
    - 行情取每只债券 as_of <= 调仓日的最新价格；
    - 事件按 effective_date <= as_of 生效（事后新登记的历史事件同样生效，
      其影响在重述影响对比中可见）；
    - 基准取 as_of <= 调仓日的最新点位。
    """

    mv = state["method_versions"][method_version_id]
    windows = dict(DEFAULT_WINDOWS)
    windows.update(mv.get("window_days") or {})

    cutoff = knowledge_as_of  # None 表示纳入全部已知数据（重述）

    def batch_known(batch_id: str) -> bool:
        b = state["batches"].get(batch_id)
        if b is None or b.get("status") == "conflict":
            return False
        return cutoff is None or b["received_at"] <= cutoff + "T23:59:59"

    blocked_batches = {
        bid for bid, b in state["batches"].items()
        if b.get("status") == "conflict"
    }

    def effective_of(ev: dict[str, Any]) -> str:
        if ev.get("effective_explicit"):
            return ev["effective_date"]
        return _add_days(ev["event_date"],
                         int(windows.get(ev["kind"], 0)))

    # 已生效撤回的记录
    withdrawn_records: set[str] = set()
    applied_events: list[str] = []
    for ev in state["events"].values():
        if not batch_known(ev["batch_id"]):
            continue  # 水位日之后才送达的事件不属于"当时可知"
        if effective_of(ev) <= as_of:
            applied_events.append(ev["event_id"])
            if ev["kind"] == EventKind.DATA_WITHDRAWAL.value and ev["record_id"]:
                withdrawn_records.add(ev["record_id"])

    # 每个发行人同一报告期的最新可用披露（口径由方法版本锁定）
    selected: dict[str, str] = {}
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in state["disclosures"].values():
        if not batch_known(rec["batch_id"]) \
                or rec["record_id"] in withdrawn_records or rec.get("withdrawn"):
            continue
        if knowledge_as_of is not None and rec["reported_on"] > knowledge_as_of:
            continue
        key_group = (rec["issuer_id"], rec["report_period"])
        cand = (rec["reported_on"], rec["record_id"])
        cur = best.get(key_group)
        if cur is None or cand > (cur["reported_on"], cur["record_id"]):
            best[key_group] = rec
    # 每个发行人取最新报告期的那条
    latest_period: dict[str, dict[str, Any]] = {}
    for (issuer_id, _period), rec in best.items():
        cur = latest_period.get(issuer_id)
        if cur is None or (rec["report_period"], rec["reported_on"]) > \
                (cur["report_period"], cur["reported_on"]):
            latest_period[issuer_id] = rec
    for issuer_id, rec in latest_period.items():
        selected[issuer_id] = rec["record_id"]

    # 每券最新价格
    price_as_of: dict[str, str] = {}
    latest_price: dict[str, tuple[str, Decimal]] = {}
    for p in state["market_prices"]:
        if not batch_known(p["batch_id"]) or p["as_of"] > as_of:
            continue
        cur = latest_price.get(p["bond_id"])
        if cur is None or p["as_of"] > cur[0]:
            latest_price[p["bond_id"]] = (p["as_of"], p["price"])
            price_as_of[p["bond_id"]] = p["as_of"]

    included = sorted(
        bid for bid, b in state["batches"].items()
        if b.get("status") != "conflict"
        and (knowledge_as_of is None
             or b["received_at"] <= knowledge_as_of + "T23:59:59")
    )
    manifest = {
        "as_of": as_of,
        "knowledge_as_of": knowledge_as_of,
        "method_version_id": method_version_id,
        "method_version_no": mv["version_no"],
        "scope_id": mv["scope_id"],
        "included_batches": included,
        "excluded_conflict_batches": sorted(blocked_batches),
        "selected_disclosures": dict(sorted(selected.items())),
        "price_as_of": dict(sorted(price_as_of.items())),
        "applied_events": sorted(applied_events),
    }
    return Watermark(
        as_of=as_of,
        method_version_id=method_version_id,
        method_version_no=mv["version_no"],
        scope_id=mv["scope_id"],
        included_batches=tuple(included),
        excluded_conflict_batches=tuple(sorted(blocked_batches)),
        selected_disclosures=selected,
        price_as_of=price_as_of,
        applied_events=tuple(sorted(applied_events)),
        manifest_hash=canonical_hash(manifest),
        knowledge_as_of=knowledge_as_of,
    )


# ---------------------------------------------------------------------------
# 事件推进：得到水位日的债券/评级状态
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EffectiveState:
    issuer_rating: dict[str, str]
    suspended_bonds: set[str]
    matured_bonds: set[str]
    withdrawn_records: set[str]


def derive_state(state: dict[str, Any], as_of: str, *,
                 method_version_id: str,
                 knowledge_as_of: str | None = None) -> EffectiveState:
    """推导调仓日的有效状态。

    与水位一致：只考虑知识截止日前送达、非冲突批次的事件；
    生效日按本 run 锁定方法版本的窗口解析，而不是读取入账时的烧录值。
    """

    mv = state["method_versions"][method_version_id]
    windows = dict(DEFAULT_WINDOWS)
    windows.update(mv.get("window_days") or {})

    def known(batch_id: str) -> bool:
        b = state["batches"].get(batch_id)
        if b is None or b.get("status") == "conflict":
            return False
        return knowledge_as_of is None \
            or b["received_at"] <= knowledge_as_of + "T23:59:59"

    def effective_of(ev: dict[str, Any]) -> str:
        if ev.get("effective_explicit"):
            return ev["effective_date"]
        return _add_days(ev["event_date"], int(windows.get(ev["kind"], 0)))

    issuer_rating = {iid: i["rating"] for iid, i in state["issuers"].items()}
    suspended = {bid for bid, b in state["bonds"].items() if b.get("suspended")}
    matured = {bid for bid, b in state["bonds"].items()
               if b["maturity_date"] <= as_of}
    withdrawn: set[str] = set()

    events = sorted(state["events"].values(),
                    key=lambda e: (effective_of(e), e["event_id"]))
    for ev in events:
        if not known(ev["batch_id"]):
            continue
        if effective_of(ev) > as_of:
            continue  # 生效日晚于调仓日
        kind = ev["kind"]
        if kind == EventKind.SUSPENSION.value and ev["bond_id"]:
            suspended.add(ev["bond_id"])
        elif kind == EventKind.RESUMPTION.value and ev["bond_id"]:
            suspended.discard(ev["bond_id"])
        elif kind == EventKind.RATING_CHANGE.value and ev["issuer_id"] \
                and ev["new_rating"]:
            issuer_rating[ev["issuer_id"]] = ev["new_rating"]
        elif kind == EventKind.DATA_WITHDRAWAL.value and ev["record_id"]:
            withdrawn.add(ev["record_id"])
    return EffectiveState(issuer_rating, suspended, matured, withdrawn)


# ---------------------------------------------------------------------------
# 合格性（族内共享）
# ---------------------------------------------------------------------------

def _passes_filter(index_def: dict[str, Any], issuer: dict[str, Any],
                   bond: dict[str, Any]) -> bool:
    flt = index_def.get("issuer_filter") or {}
    if "sectors" in flt and issuer["sector"] not in flt["sectors"]:
        return False
    if "issuer_ids" in flt and issuer["issuer_id"] not in flt["issuer_ids"]:
        return False
    if "bond_currencies" in flt and bond["currency"] not in flt["bond_currencies"]:
        return False
    if "min_par" in flt and bond["par_amount"] < Decimal(str(flt["min_par"])):
        return False
    return True


def shared_eligibility(state: dict[str, Any], as_of: str,
                       watermark: Watermark, eff: EffectiveState,
                       screen: dict[str, Any]) -> list[str]:
    """核心合格集合：债券通过即纳入核心，子指数在其上叠加范围过滤。"""

    eligible: list[str] = []
    min_rating = screen.get("min_rating")
    min_ttm = int(screen.get("minimum_time_to_maturity_days", 0))
    for bid, bond in sorted(state["bonds"].items()):
        iid = bond["issuer_id"]
        issuer = state["issuers"][iid]
        if bid in eff.matured_bonds:
            continue
        if screen.get("exclude_suspended", True) and bid in eff.suspended_bonds:
            continue
        if min_rating and RATING_ORDER.get(eff.issuer_rating[iid], 99) > \
                RATING_ORDER[min_rating]:
            continue
        if min_ttm and _days_between(as_of, bond["maturity_date"]) < min_ttm:
            continue
        if screen.get("require_live_disclosure", True) \
                and iid not in watermark.selected_disclosures:
            continue
        eligible.append(bid)
    return eligible


# ---------------------------------------------------------------------------
# 权重：在每个指数自身范围内独立守恒
# ---------------------------------------------------------------------------

def _raw_weights(state: dict[str, Any], bond_ids: list[str],
                 scheme: str, price_of: dict[str, Decimal]) -> dict[str, Decimal]:
    raw: dict[str, Decimal] = {}
    for bid in bond_ids:
        bond = state["bonds"][bid]
        if scheme == "equal":
            raw[bid] = Decimal("1")
        elif scheme == "par":
            raw[bid] = bond["par_amount"]
        else:  # market_cap：无行情价时按面值计（水位记录可追溯）
            price = price_of.get(bid, Decimal("100"))
            raw[bid] = bond["par_amount"] * price / Decimal("100")
    return raw


def conserve_weights(raw: dict[str, Decimal],
                     cap: Decimal | None) -> dict[str, Decimal]:
    """归一化并执行单券上限，权重之和在返回值中恰为 1。

    封顶采用确定性的迭代再分配：超限部分按未封顶券的原始占比摊回，
    直至无超限；最后一步舍入残差加到最大权重券，保证严格守恒。
    """

    if not raw:
        from .errors import WeightConservationError

        raise WeightConservationError("合格成分为空，无法在自身范围内守恒权重")
    if cap is not None and cap * len(raw) < Decimal("1"):
        from .errors import WeightConservationError

        raise WeightConservationError(
            f"单券上限 {cap} 对 {len(raw)} 只成分不可行（上限之和小于 1）")
    total = sum(raw.values(), Decimal("0"))
    weights = {k: v / total for k, v in raw.items()}

    if cap is not None:
        capped: set[str] = set()
        for _ in range(100):
            over = {k: w for k, w in weights.items()
                    if k not in capped and w > cap + _Q}
            if not over:
                break
            excess = sum(w - cap for w in over.values())
            for k in over:
                weights[k] = cap
                capped.add(k)
            free = {k: raw[k] for k in raw if k not in capped}
            free_total = sum(free.values(), Decimal("0"))
            if free_total == 0:
                break
            for k in free:
                weights[k] += excess * free[k] / free_total

    quantized = {k: _q(w) for k, w in weights.items()}
    residual = Decimal("1") - sum(quantized.values(), Decimal("0"))
    if residual:
        top = max(quantized, key=lambda k: (quantized[k], k))
        quantized[top] += residual
    total_w = sum(quantized.values(), Decimal("0"))
    if total_w != Decimal("1"):
        from .errors import WeightConservationError

        raise WeightConservationError(f"权重之和 {total_w} 不等于 1")
    return quantized


# ---------------------------------------------------------------------------
# 指数计算
# ---------------------------------------------------------------------------

def _issuer_carbon(state: dict[str, Any], watermark: Watermark,
                   issuer_id: str) -> Decimal:
    rec = state["disclosures"][watermark.selected_disclosures[issuer_id]]
    if rec["revenue"] <= 0:
        from .errors import ValidationError

        raise ValidationError(f"发行人 {issuer_id} 披露营收非正，碳强度不可定义")
    return rec["emissions_tco2e"] / rec["revenue"]


def benchmark_at(state: dict[str, Any], as_of: str,
                 included_batches: set[str]) -> Decimal | None:
    points = [p for p in state["benchmark"]
              if p["as_of"] <= as_of and p["batch_id"] in included_batches]
    if not points:
        return None
    return points[-1]["carbon_intensity"]


def compute_index_values(state: dict[str, Any], as_of: str,
                         watermark: Watermark, eff: EffectiveState,
                         core_eligible: list[str],
                         weight_method: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """对核心与全部子指数分别计算成分、权重与碳指标。

    权重方法由方法版本锁定，全族同一次计算共用同一方案；
    但每个指数在自己的成分范围内独立归一、独立守恒。
    """

    blocked = set(watermark.excluded_conflict_batches)
    included = set(watermark.included_batches)
    price_of = {}
    for p in state["market_prices"]:
        if p["as_of"] == watermark.price_as_of.get(p["bond_id"]):
            price_of[p["bond_id"]] = p["price"]

    results: dict[str, dict[str, Any]] = {}
    core_id = next(i["index_id"] for i in state["indexes"].values()
                   if i["parent_id"] is None)
    cap = weight_method.get("cap_pct")
    cap_dec = Decimal(str(cap)) if cap is not None else None

    def build(index_def: dict[str, Any], universe: list[str]) -> dict[str, Any]:
        if not universe:
            # 子指数范围过滤后可能为空：记录为空成分，不伪造权重。
            bench = benchmark_at(state, as_of, included)
            return {
                "index_id": index_def["index_id"],
                "name": index_def["name"],
                "parent_id": index_def["parent_id"],
                "constituents": [],
                "carbon_intensity": None,
                "benchmark_carbon_intensity": str(_q(bench)) if bench is not None else None,
                "benchmark_diff_absolute": None,
                "benchmark_reduction_pct": None,
                "weight_sum": "0",
                "empty_universe": True,
            }
        raw = _raw_weights(state, universe, weight_method["scheme"], price_of)
        weights = conserve_weights(raw, cap_dec)

        constituents: list[dict[str, Any]] = []
        intensity = Decimal("0")
        for bid in sorted(universe):
            iid = state["bonds"][bid]["issuer_id"]
            ci = _issuer_carbon(state, watermark, iid)
            w = weights[bid]
            intensity += w * ci
            constituents.append({
                "bond_id": bid,
                "issuer_id": iid,
                "sector": state["issuers"][iid]["sector"],
                "weight": str(_q(w)),
                "carbon_intensity": str(_q(ci)),
                "rating": eff.issuer_rating[iid],
                "disclosure_record_id": watermark.selected_disclosures[iid],
            })

        bench = benchmark_at(state, as_of, included)
        diff_abs = (bench - intensity) if bench is not None else None
        diff_pct = (diff_abs / bench) if bench is not None and bench else None
        return {
            "index_id": index_def["index_id"],
            "name": index_def["name"],
            "parent_id": index_def["parent_id"],
            "constituents": constituents,
            "carbon_intensity": str(_q(intensity)),
            "benchmark_carbon_intensity": str(_q(bench)) if bench is not None else None,
            "benchmark_diff_absolute": str(_q(diff_abs)) if diff_abs is not None else None,
            "benchmark_reduction_pct": str(_q(diff_pct)) if diff_pct is not None else None,
            "weight_sum": str(sum(weights.values(), Decimal("0"))),
        }

    # 核心
    core_def = state["indexes"][core_id]
    if not core_eligible:
        from .errors import WeightConservationError

        raise WeightConservationError(
            "核心指数合格成分为空，无法满足权重守恒；请检查筛选与水位")
    results[core_id] = build(core_def, core_eligible)
    # 子指数：共享合格集合，仅叠加自身范围
    for idef in sorted((i for i in state["indexes"].values()
                        if i["parent_id"] == core_id),
                       key=lambda i: i["index_id"]):
        universe = [
            bid for bid in core_eligible
            if _passes_filter(idef,
                              state["issuers"][state["bonds"][bid]["issuer_id"]],
                              state["bonds"][bid])
        ]
        results[idef["index_id"]] = build(idef, universe)
    return results
