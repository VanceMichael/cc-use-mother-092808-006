"""主编排服务：计算运行、重述审批、发布闸门、调仓恢复、复现报告。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .access_control import Principal, require
from .engine import (EffectiveState, compute_index_values, compute_watermark,
                     derive_state)
from .errors import (NotFoundError, PublishBlockedError, ValidationError,
                     WorkflowError)
from .ingest import conflicts_relevant_to
from .models import RebalanceStatus, RestatementStatus, RunKind
from .registry import validate_family_complete
from .store import Store, canonical_hash

# 调仓任务的检查点（顺序执行，崩溃后从最后完成点之后继续）
STEPS = ("prepare", "compute", "publish")


@dataclass
class RunRequest:
    as_of: str
    method_version_id: str
    reason: str = ""


class BondIndexService:
    """无状态外壳 + 文件状态仓库，可随时用同一文件重建以模拟故障恢复。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------------ 计算

    def _method_version(self, state: dict[str, Any],
                        method_version_id: str | None) -> dict[str, Any]:
        if method_version_id is None:
            active = [v for v in state["method_versions"].values() if v["active"]]
            if not active:
                raise NotFoundError("没有生效的方法版本")
            return active[0]
        mv = state["method_versions"].get(method_version_id)
        if mv is None:
            raise NotFoundError(f"方法版本 {method_version_id} 不存在")
        return mv

    def compute_run(self, principal: Principal, as_of: str,
                    method_version_id: str | None = None, *,
                    kind: RunKind = RunKind.ORIGINAL,
                    linked_restatement: str | None = None,
                    reason: str = "") -> str:
        """固定水位与方法版本执行一次计算，结果以快照形式永久保存。

        同一 (as_of, 方法版本, 水位哈希, 类别) 的计算是确定的：
        重复执行返回既有 run_id，不会产生新版本。
        """

        require(principal, "rebalance:run")
        state = self.store.view()
        validate_family_complete(state)
        mv = self._method_version(state, method_version_id)
        screen = state["screens"][mv["screen_id"]]

        watermark = compute_watermark(state, as_of, mv["method_version_id"],
                                      knowledge_as_of=as_of)
        eff = derive_state(state, as_of, method_version_id=mv["method_version_id"],
                           knowledge_as_of=as_of)
        core_eligible = _shared_eligible(state, as_of, watermark, eff, screen)
        weight_method = state["weight_methods"][mv["weight_method_id"]]
        indexes = compute_index_values(state, as_of, watermark, eff,
                                       core_eligible, weight_method)

        fingerprint = canonical_hash({
            "as_of": as_of,
            "method_version_id": mv["method_version_id"],
            "method_version_no": mv["version_no"],
            "watermark": watermark.manifest(),
            "indexes": indexes,
            "kind": kind.value,
        })
        run_id = "run-" + fingerprint[:16]

        def fn(st: dict[str, Any]) -> str:
            if run_id in st["runs"]:
                return run_id  # 确定性：相同输入不重复生成
            st["runs"][run_id] = {
                "run_id": run_id,
                "as_of": as_of,
                "kind": kind.value,
                "status": "computed",
                "method_version_id": mv["method_version_id"],
                "method_version_no": mv["version_no"],
                "scope_id": mv["scope_id"],
                "watermark": watermark.manifest(),
                "watermark_hash": watermark.manifest_hash,
                "indexes": indexes,
                "core_eligible": core_eligible,
                "fingerprint": fingerprint,
                "linked_restatement": linked_restatement,
                "reason": reason,
            }
            by_date = st.setdefault("runs_by_date", {})
            slot = by_date.setdefault(as_of, {"original": None,
                                              "restatements": []})
            if kind is RunKind.ORIGINAL:
                if slot["original"] is None:
                    slot["original"] = run_id
            else:
                if run_id not in slot["restatements"]:
                    slot["restatements"].append(run_id)
            return run_id

        return self.store.mutate(fn)

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.view()["runs"].get(run_id)
        if run is None:
            raise NotFoundError(f"计算运行 {run_id} 不存在")
        return run

    # ------------------------------------------------------------------ 发布

    def _publish_blockers(self, state: dict[str, Any], as_of: str) -> list[str]:
        blockers: list[str] = []
        relevant = conflicts_relevant_to(state, as_of)
        if relevant:
            blockers.append(
                "存在内容不一致的重送批次 "
                f"{relevant}，相关发布被阻止，须先按重述流程处置")
        for rid, rs in state["restatements"].items():
            if rs["status"] == RestatementStatus.PROPOSED.value \
                    and rs["as_of"] <= as_of:
                blockers.append(f"该日存在待另一角色批准的回溯重述提议 {rid}")
        return blockers

    def publish_run(self, principal: Principal, run_id: str) -> dict[str, Any]:
        """发布闸门：冲突批次或待批重述未决时阻止发布。

        同一 run 重复发布幂等返回既有发布记录。
        """

        require(principal, "rebalance:publish")
        state = self.store.view()
        run = state["runs"].get(run_id)
        if run is None:
            raise NotFoundError(f"计算运行 {run_id} 不存在")
        blockers = self._publish_blockers(state, run["as_of"])
        if blockers:
            raise PublishBlockedError("；".join(blockers))

        def fn(st: dict[str, Any]) -> dict[str, Any]:
            for pub in st["publications"]:
                if pub["run_id"] == run_id:
                    return pub  # 幂等：已发布不重复入账
            pub = {
                "publication_id": "pub-" + canonical_hash(run_id)[:12],
                "run_id": run_id,
                "as_of": run["as_of"],
                "kind": run["kind"],
                "method_version_id": run["method_version_id"],
                "method_version_no": run["method_version_no"],
                "run_fingerprint": run["fingerprint"],
                "watermark_hash": run["watermark_hash"],
            }
            st["publications"].append(pub)
            st["runs"][run_id]["status"] = "published"
            return pub

        return self.store.mutate(fn)

    # ------------------------------------------------------------------ 重述

    def propose_restatement(self, principal: Principal, as_of: str,
                            method_version_id: str | None, reason: str) -> str:
        """方法维护者对历史调仓日提出回溯重述（待另一角色批准）。"""

        require(principal, "restatement:propose")
        if not reason.strip():
            raise ValidationError("重述原因不能为空")

        def fn(st: dict[str, Any]) -> str:
            slot = st.get("runs_by_date", {}).get(as_of)
            if slot is None or slot.get("original") is None:
                raise NotFoundError(f"{as_of} 没有已发布/计算的原始运行，无法重述")
            mv = self._method_version(st, method_version_id)
            rid = "rs-" + canonical_hash(
                [as_of, mv["method_version_id"], reason,
                 principal.user_id])[:12]
            if rid in st["restatements"]:
                raise ValidationError("相同的重述提议已存在")
            st["restatements"][rid] = {
                "restatement_id": rid,
                "as_of": as_of,
                "original_run_id": slot["original"],
                "method_version_id": mv["method_version_id"],
                "method_version_no": mv["version_no"],
                "reason": reason,
                "status": RestatementStatus.PROPOSED.value,
                "proposed_by": principal.user_id,
                "proposed_by_role": principal.role.value,
                "restated_run_id": None,
            }
            return rid

        return self.store.mutate(fn)

    def approve_restatement(self, principal: Principal,
                            restatement_id: str) -> str:
        """另一角色批准；禁止提出人自批，禁止同角色互批。批准即重算并留痕。"""

        require(principal, "restatement:approve")

        def fn(st: dict[str, Any]) -> str:
            rs = st["restatements"].get(restatement_id)
            if rs is None:
                raise NotFoundError(f"重述提议 {restatement_id} 不存在")
            if rs["status"] != RestatementStatus.PROPOSED.value:
                raise WorkflowError(
                    f"重述 {restatement_id} 状态为 {rs['status']}，不可批准")
            if rs["proposed_by"] == principal.user_id:
                raise WorkflowError("提出人与批准人不能为同一人（禁止自批）")
            if rs["proposed_by_role"] == principal.role.value:
                raise WorkflowError("重述必须由另一角色批准，同角色不能互批")

            # 批准成立：以当前水位 + 指定方法版本重算，生成 restated run
            self._mutate_locked_compute(st, rs, principal)
            rs["status"] = RestatementStatus.APPROVED.value
            rs["approved_by"] = principal.user_id
            rs["approved_by_role"] = principal.role.value
            return rs["restated_run_id"]

        return self.store.mutate(fn)

    def _mutate_locked_compute(self, st: dict[str, Any],
                               rs: dict[str, Any], principal: Principal) -> None:
        """在已持有的状态锁内完成重算（避免与外部 mutate 嵌套）。"""

        mv = st["method_versions"][rs["method_version_id"]]
        screen = st["screens"][mv["screen_id"]]
        watermark = compute_watermark(st, rs["as_of"], mv["method_version_id"])
        eff = derive_state(st, rs["as_of"],
                           method_version_id=mv["method_version_id"])
        eligible = _shared_eligible(st, rs["as_of"], watermark, eff, screen)
        weight_method = st["weight_methods"][mv["weight_method_id"]]
        indexes = compute_index_values(st, rs["as_of"], watermark, eff,
                                       eligible, weight_method)
        fingerprint = canonical_hash({
            "as_of": rs["as_of"],
            "method_version_id": mv["method_version_id"],
            "method_version_no": mv["version_no"],
            "watermark": watermark.manifest(),
            "indexes": indexes,
            "kind": RunKind.RESTATED.value,
        })
        run_id = "run-" + fingerprint[:16]
        if run_id not in st["runs"]:
            st["runs"][run_id] = {
                "run_id": run_id,
                "as_of": rs["as_of"],
                "kind": RunKind.RESTATED.value,
                "status": "computed",
                "method_version_id": mv["method_version_id"],
                "method_version_no": mv["version_no"],
                "scope_id": mv["scope_id"],
                "watermark": watermark.manifest(),
                "watermark_hash": watermark.manifest_hash,
                "indexes": indexes,
                "core_eligible": eligible,
                "fingerprint": fingerprint,
                "linked_restatement": rs["restatement_id"],
                "reason": rs["reason"],
            }
            slot = st["runs_by_date"][rs["as_of"]]
            if run_id not in slot["restatements"]:
                slot["restatements"].append(run_id)
        rs["restated_run_id"] = run_id

        # 记录与原始数字的差异，供复现报告解释"为何重述、改了多少"
        original = st["runs"][rs["original_run_id"]]
        rs["impact"] = _diff_runs(original, st["runs"][run_id])

    def reject_restatement(self, principal: Principal,
                           restatement_id: str) -> None:
        require(principal, "restatement:approve")

        def fn(st: dict[str, Any]) -> None:
            rs = st["restatements"].get(restatement_id)
            if rs is None:
                raise NotFoundError(f"重述提议 {restatement_id} 不存在")
            if rs["status"] != RestatementStatus.PROPOSED.value:
                raise WorkflowError(f"重述状态为 {rs['status']}，不可驳回")
            if rs["proposed_by"] == principal.user_id:
                raise WorkflowError("提出人不能驳回自己的提议")
            rs["status"] = RestatementStatus.REJECTED.value
            rs["approved_by"] = principal.user_id

        self.store.mutate(fn)

    # ------------------------------------------------------------------ 调仓

    def create_rebalance_task(self, principal: Principal, as_of: str,
                              method_version_id: str | None = None) -> str:
        require(principal, "rebalance:run")

        def fn(st: dict[str, Any]) -> str:
            mv = self._method_version(st, method_version_id)
            dates = {e["rebalance_date"] for e in st["calendar"]}
            if as_of not in dates:
                raise ValidationError(f"{as_of} 不在调仓日历中")
            task_id = "task-" + canonical_hash([as_of, mv["method_version_id"]])[:12]
            if task_id in st["rebalance_tasks"]:
                raise ValidationError("该调仓日任务已存在，请走恢复而非新建")
            st["rebalance_tasks"][task_id] = {
                "task_id": task_id,
                "as_of": as_of,
                "method_version_id": mv["method_version_id"],
                "status": RebalanceStatus.PENDING.value,
                "completed_steps": [],
                "run_id": None,
                "publication_id": None,
                "attempts": 0,
            }
            return task_id

        return self.store.mutate(fn)

    def run_rebalance_task(self, principal: Principal, task_id: str, *,
                           crash_after_step: str | None = None) -> dict[str, Any]:
        """执行（或恢复）调仓任务。

        每个检查点完成即落盘；``crash_after_step`` 模拟该步完成后崩溃，
        下次调用从后续步骤继续，已完成的计算/发布绝不重复入账。
        """

        require(principal, "rebalance:run")
        state = self.store.view()
        task = state["rebalance_tasks"].get(task_id)
        if task is None:
            raise NotFoundError(f"调仓任务 {task_id} 不存在")

        for step in STEPS:
            if step in task["completed_steps"]:
                continue  # 故障恢复：跳过已落盘步骤
            self._execute_step(principal, task_id, step)
            if crash_after_step == step:
                # 模拟进程崩溃：状态已落盘，直接返回，等待恢复
                return self.store.view()["rebalance_tasks"][task_id]
        return self.store.view()["rebalance_tasks"][task_id]

    def _execute_step(self, principal: Principal, task_id: str, step: str) -> None:
        def mark_start(st: dict[str, Any]) -> None:
            t = st["rebalance_tasks"][task_id]
            t["status"] = RebalanceStatus.RUNNING.value
            t["attempts"] += 1

        self.store.mutate(mark_start)

        if step == "prepare":
            # 校验：族完整、方法版本可用、无未决数据冲突（准备期即提前失败）
            st = self.store.view()
            validate_family_complete(st)
            self._method_version(st, st["rebalance_tasks"][task_id]
                                 ["method_version_id"])
        elif step == "compute":
            st = self.store.view()
            t = st["rebalance_tasks"][task_id]
            run_id = self.compute_run(
                principal, t["as_of"], t["method_version_id"])
            self.store.mutate(
                lambda s: s["rebalance_tasks"][task_id].update(run_id=run_id))
        elif step == "publish":
            st = self.store.view()
            t = st["rebalance_tasks"][task_id]
            try:
                pub = self.publish_run(principal, t["run_id"])
            except Exception:
                self.store.mutate(
                    lambda s: s["rebalance_tasks"][task_id].update(
                        status=RebalanceStatus.BLOCKED.value))
                raise

            def done(s: dict[str, Any]) -> None:
                tt = s["rebalance_tasks"][task_id]
                tt["publication_id"] = pub["publication_id"]
                tt["status"] = RebalanceStatus.PUBLISHED.value

            self.store.mutate(done)

        def mark_done(st: dict[str, Any]) -> None:
            st["rebalance_tasks"][task_id]["completed_steps"].append(step)

        self.store.mutate(mark_done)

    def resume_rebalance_task(self, principal: Principal,
                              task_id: str) -> dict[str, Any]:
        """故障恢复入口：语义等同于继续执行，未完成步骤接着跑。"""

        return self.run_rebalance_task(principal, task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        task = self.store.view()["rebalance_tasks"].get(task_id)
        if task is None:
            raise NotFoundError(f"调仓任务 {task_id} 不存在")
        return task

    # ------------------------------------------------------------------ 复现

    def reproduction_report(self, principal: Principal, as_of: str) -> dict[str, Any]:
        """对外复现结果。

        不是只给最新数字，而是完整列出：
        - 原始调仓日的方法版本、数据水位、成分、权重、碳指标、基准差异；
        - 此后每次已批准重述的原因、批准人、水位/方法变化与指标差异；
        - 当前应采用的版本指引。
        """

        require(principal, "reproduction:read")
        state = self.store.view()
        slot = state.get("runs_by_date", {}).get(as_of)
        if slot is None or not slot.get("original"):
            raise NotFoundError(f"没有 {as_of} 的计算运行，无法复现")

        original = state["runs"][slot["original"]]

        def view_of(run: dict[str, Any]) -> dict[str, Any]:
            return {
                "run_id": run["run_id"],
                "kind": run["kind"],
                "as_of": run["as_of"],
                "method_version_id": run["method_version_id"],
                "method_version_no": run["method_version_no"],
                "scope_id": run["scope_id"],
                "watermark": run["watermark"],
                "watermark_hash": run["watermark_hash"],
                "fingerprint": run["fingerprint"],
                "indexes": run["indexes"],
            }

        restatements: list[dict[str, Any]] = []
        for rid, rs in sorted(state["restatements"].items()):
            if rs["as_of"] != as_of:
                continue
            item = {
                "restatement_id": rid,
                "status": rs["status"],
                "reason": rs["reason"],
                "proposed_by_role": rs["proposed_by_role"],
                "approved_by_role": rs.get("approved_by_role"),
                "target_method_version_no": rs["method_version_no"],
            }
            if rs.get("restated_run_id"):
                item["restated_run_id"] = rs["restated_run_id"]
                item["impact"] = rs.get("impact")
                item["restated"] = view_of(state["runs"][rs["restated_run_id"]])
            restatements.append(item)

        approved = [r for r in restatements
                    if r["status"] == RestatementStatus.APPROVED.value]
        return {
            "as_of": as_of,
            "original": view_of(original),
            "restatements": restatements,
            "has_restatement": bool(approved),
            "current_run_id": approved[-1]["restated_run_id"] if approved
            else original["run_id"],
            "note": ("原始结果保留可复现；最新数字见 current_run_id 对应重述版本，"
                     "重述原因与审批链见 restatements") if approved
            else "该日无已批准重述，原始结果即当前版本",
        }


def _shared_eligible(state: dict[str, Any], as_of: str,
                     watermark: Any, eff: EffectiveState,
                     screen: dict[str, Any]) -> list[str]:
    from .engine import shared_eligibility

    return shared_eligibility(state, as_of, watermark, eff, screen)


def _diff_runs(original: dict[str, Any], restated: dict[str, Any]) -> dict[str, Any]:
    """对比两次运行的核心碳指标与成分变化，作为重述影响说明。"""

    def core(run: dict[str, Any]) -> dict[str, Any]:
        cid = next(iid for iid, v in run["indexes"].items()
                   if v["parent_id"] is None)
        return run["indexes"][cid]

    o, r = core(original), core(restated)
    return {
        "index_id": o["index_id"],
        "original_carbon_intensity": o["carbon_intensity"],
        "restated_carbon_intensity": r["carbon_intensity"],
        "original_benchmark_reduction_pct": o["benchmark_reduction_pct"],
        "restated_benchmark_reduction_pct": r["benchmark_reduction_pct"],
        "original_constituent_count": len(o["constituents"]),
        "restated_constituent_count": len(r["constituents"]),
        "watermark_changed":
            original["watermark_hash"] != restated["watermark_hash"],
        "method_version_changed":
            original["method_version_id"] != restated["method_version_id"],
        "constituent_diff": _constituent_diff(o, r),
    }


def _constituent_diff(o: dict[str, Any], r: dict[str, Any]) -> dict[str, Any]:
    om = {c["bond_id"]: c for c in o["constituents"]}
    rm = {c["bond_id"]: c for c in r["constituents"]}
    added = sorted(set(rm) - set(om))
    removed = sorted(set(om) - set(rm))
    weight_changed = sorted(
        bid for bid in set(om) & set(rm)
        if Decimal(om[bid]["weight"]) != Decimal(rm[bid]["weight"]))
    return {"added": added, "removed": removed, "weight_changed": weight_changed}
