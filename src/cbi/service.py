"""债券指数编制服务门面。

聚合登记、批次入账、调仓任务、重述审批与复现查询。所有变更在存储锁内完成并
原子落盘；调仓任务带检查点，故障后可凭任务日志恢复重放。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from . import model
from .access import (
    filter_visible_emissions,
    public_or_visible,
    require_any_role,
    require_distinct_user,
    require_role,
)
from .engine import compute_family
from .errors import (
    BatchBlocked,
    ConflictError,
    NotFound,
    TaskPending,
    ValidationError,
)
from .model import (
    BATCH_DISCLOSURE,
    BATCH_MASTER,
    BATCH_QUOTE,
    BATCH_STATE_BLOCKED,
    BATCH_STATE_RECEIVED,
    BATCH_STATE_REPLAYED,
    BATCH_TYPES,
    METHOD_ACTIVE,
    METHOD_DEACTIVATED,
    RESTATE_APPROVED,
    RESTATE_DATA,
    RESTATE_METHOD,
    RESTATE_PROPOSED,
    RESTATE_REJECTED,
    ROLE_CLIMATE_PROVIDER,
    ROLE_DATA_STEWARD,
    ROLE_INDEX_CALCULATOR,
    ROLE_INVESTOR,
    ROLE_METHOD_APPROVER,
    ROLE_METHOD_OWNER,
    SENSITIVITY_RESTRICTED,
    TASK_DONE,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
    Principal,
)
from .store import Store, canonical_fingerprint

# --------------------------------------------------------------------------- 常量

RUN_PUBLISHED = "published"
RUN_BLOCKED = "blocked"

_BATCH_ROLES = {
    BATCH_MASTER: frozenset({ROLE_DATA_STEWARD}),
    BATCH_QUOTE: frozenset({ROLE_DATA_STEWARD}),
    BATCH_DISCLOSURE: frozenset({ROLE_CLIMATE_PROVIDER}),
}


class IndexService:
    """编制服务。线程安全；可指向同一目录在故障后重开并恢复。"""

    def __init__(self, directory: str, clock: Callable[[], str] | None = None):
        self.store = Store(directory)
        self._clock = clock or (lambda: date.today().isoformat())
        # 进程内正在执行的任务（崩溃后随进程消失，落盘的 running 即“悬挂”任务）。
        self._in_flight: set[str] = set()

    def _today(self) -> str:
        return self._clock()

    @staticmethod
    def _key(method_id: str, version: int) -> str:
        return f"{method_id}:{version}"

    # ================================================================ 主数据登记

    def register_issuer(self, principal: Principal, **kwargs: Any) -> dict[str, Any]:
        require_role(principal, ROLE_DATA_STEWARD)
        record = model.issuer(**kwargs)
        with self.store.lock:
            if record["issuer_id"] in self.store.state["issuers"]:
                raise ConflictError(f"发行人 {record['issuer_id']} 已登记")
            self.store.state["issuers"][record["issuer_id"]] = record
            self.store.save()
        return record

    def register_bond(self, principal: Principal, **kwargs: Any) -> dict[str, Any]:
        require_role(principal, ROLE_DATA_STEWARD)
        record = model.bond(**kwargs)
        with self.store.lock:
            if record["bond_id"] in self.store.state["bonds"]:
                raise ConflictError(f"债券 {record['bond_id']} 已登记")
            if record["issuer_id"] not in self.store.state["issuers"]:
                raise ValidationError("发行人主数据尚未登记")
            self.store.state["bonds"][record["bond_id"]] = record
            self.store.save()
        return record

    def add_rating_event(
        self, principal: Principal, bond_id: str, announced: str, grade: str,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        require_role(principal, ROLE_DATA_STEWARD)
        event = model.rating_event(bond_id, announced, grade)
        event["batch_id"] = batch_id
        with self.store.lock:
            if bond_id not in self.store.state["bonds"]:
                raise ValidationError("债券主数据尚未登记")
            if event in self.store.state["rating_events"]:
                return event
            self.store.state["rating_events"].append(event)
            self.store.save()
        return event

    def add_suspension_event(
        self, principal: Principal, bond_id: str, start: str,
        resume: str | None = None, batch_id: str | None = None,
    ) -> dict[str, Any]:
        require_role(principal, ROLE_DATA_STEWARD)
        event = model.suspension_event(bond_id, start, resume)
        event["batch_id"] = batch_id
        with self.store.lock:
            if bond_id not in self.store.state["bonds"]:
                raise ValidationError("债券主数据尚未登记")
            self.store.state["suspensions"][bond_id] = event
            self.store.save()
        return event

    def register_climate_source(self, principal: Principal, source_id: str, name: str) -> dict[str, Any]:
        require_role(principal, ROLE_CLIMATE_PROVIDER)
        record = model.climate_source(source_id, name)
        with self.store.lock:
            if record["source_id"] in self.store.state["climate_sources"]:
                raise ConflictError("气候数据来源已登记")
            self.store.state["climate_sources"][record["source_id"]] = record
            self.store.save()
        return record

    def submit_quote(
        self, principal: Principal, bond_id: str, on_date: str, price: Any,
        batch_id: str | None = None,
    ) -> None:
        require_role(principal, ROLE_DATA_STEWARD)
        price_str = model.positive_number(price, "行情价格")
        on_date = model.iso_date(on_date, "行情日期")
        with self.store.lock:
            if bond_id not in self.store.state["bonds"]:
                raise ValidationError("债券主数据尚未登记")
            self.store.state["quotes"].setdefault(bond_id, {})[on_date] = {
                "price": price_str,
                "batch_id": batch_id,
            }
            self.store.save()

    # ================================================================ 指数与日历

    def register_index(self, principal: Principal, definition: dict[str, Any]) -> dict[str, Any]:
        require_role(principal, ROLE_DATA_STEWARD)
        record = model.index_definition(**definition)
        with self.store.lock:
            if record["index_id"] in self.store.state["indexes"]:
                raise ConflictError(f"指数 {record['index_id']} 已登记")
            if record["parent_id"] is not None:
                if record["parent_id"] not in self.store.state["indexes"]:
                    raise ValidationError("父指数不存在，须先登记核心指数")
                parent = self.store.state["indexes"][record["parent_id"]]
                if parent["parent_id"] is not None:
                    raise ValidationError("仅支持核心指数的一层子指数")
            self.store.state["indexes"][record["index_id"]] = record
            self.store.save()
        return record

    def set_core_index(self, principal: Principal, index_id: str) -> None:
        require_role(principal, ROLE_DATA_STEWARD)
        with self.store.lock:
            if index_id not in self.store.state["indexes"]:
                raise NotFound("指数不存在")
            if self.store.state["indexes"][index_id]["parent_id"] is not None:
                raise ValidationError("核心指数不能是子指数")
            self.store.state["core_index_id"] = index_id
            self.store.save()

    def register_rebalance_date(self, principal: Principal, rebalance_date: str) -> str:
        require_role(principal, ROLE_INDEX_CALCULATOR)
        day = model.iso_date(rebalance_date, "调仓日")
        with self.store.lock:
            calendar = self.store.state["rebalance_calendar"]
            if day in calendar:
                return day
            calendar.append(day)
            calendar.sort()
            self.store.save()
        return day

    # ================================================================ 方法版本

    def propose_methodology(self, principal: Principal, **kwargs: Any) -> dict[str, Any]:
        """方法维护者登记新版本（草稿）。不能自行激活。"""
        require_role(principal, ROLE_METHOD_OWNER)
        record = model.methodology_version(**kwargs)
        record["proposed_by"] = principal.user_id
        key = self._key(record["method_id"], record["version"])
        with self.store.lock:
            if key in self.store.state["methods"]:
                raise ConflictError(f"方法版本 {key} 已存在")
            self.store.state["methods"][key] = record
            self.store.state["method_order"].append(key)
            self.store.save()
        return record

    def activate_methodology(self, principal: Principal, method_id: str, version: int) -> dict[str, Any]:
        """由另一个角色（方法审批人员，且非提议人本人）激活。"""
        require_role(principal, ROLE_METHOD_APPROVER)
        key = self._key(method_id, version)
        with self.store.lock:
            record = self.store.state["methods"].get(key)
            if record is None:
                raise NotFound("方法版本不存在")
            require_distinct_user(principal, record["proposed_by"], "方法激活")
            if record["status"] == METHOD_ACTIVE:
                return record
            # 停用当前激活版本
            current_key = self.store.state["active_method_key"]
            if current_key and current_key in self.store.state["methods"]:
                self.store.state["methods"][current_key]["status"] = METHOD_DEACTIVATED
            record["status"] = METHOD_ACTIVE
            record["activated_by"] = principal.user_id
            record["activated_on"] = self._today()
            self.store.state["active_method_key"] = key
            self.store.save()
        return record

    def _require_active_method(self, method_key: str | None) -> tuple[str, dict[str, Any]]:
        state = self.store.state
        key = method_key or state["active_method_key"]
        if not key or key not in state["methods"]:
            raise ValidationError("未指定方法版本，且没有已激活方法")
        record = state["methods"][key]
        if record["status"] != METHOD_ACTIVE:
            raise ValidationError(f"方法版本 {key} 当前状态为 {record['status']}，不可用于计算")
        return key, record

    # ================================================================ 批次入账

    def submit_batch(
        self,
        principal: Principal,
        batch_id: str,
        batch_type: str,
        entries: dict[str, Any],
        received: str | None = None,
    ) -> dict[str, Any]:
        """登记行情/披露/主数据批次。

        幂等：同一 ``batch_id`` 重送且内容一致 → 不重复入账；
        阻断：同 ``batch_id`` 重送但内容不同 → 批次置为 blocked，
        并阻止所有消费该批次的发布继续作为有效发布。
        """
        model.require(batch_id, "批次标识")
        if batch_type not in BATCH_TYPES:
            raise ValidationError("批次类型无效")
        require_any_role(principal, _BATCH_ROLES[batch_type])
        received = model.iso_date(received or self._today(), "批次到达日")
        fingerprint = canonical_fingerprint(entries)

        with self.store.lock:
            existing = self.store.state["batches"].get(batch_id)
            if existing is not None:
                if existing["fingerprint"] == fingerprint:
                    existing["deliveries"] += 1
                    existing["last_delivered_on"] = received
                    existing["state"] = BATCH_STATE_REPLAYED
                    self.store.save()
                    return {
                        "batch_id": batch_id,
                        "state": BATCH_STATE_REPLAYED,
                        "idempotent": True,
                        "message": "重送内容一致，未重复入账",
                    }
                # 内容不同：阻断相关发布
                existing["state"] = BATCH_STATE_BLOCKED
                existing["blocked_on"] = received
                existing["blocked_fingerprint"] = fingerprint
                existing["deliveries"] += 1
                blocked_runs = []
                for run in self.store.state["runs"].values():
                    if run["status"] == RUN_PUBLISHED and batch_id in run.get("input_batches", []):
                        run["status"] = RUN_BLOCKED
                        run["blocked_by_batch"] = batch_id
                        blocked_runs.append(run["run_id"])
                self.store.save()
                raise BatchBlocked(batch_id, existing["fingerprint"], fingerprint)

            # 首次入账：按类型应用条目
            counts = self._apply_batch_entries(batch_type, batch_id, entries)
            self.store.state["batches"][batch_id] = {
                "batch_id": batch_id,
                "type": batch_type,
                "received": received,
                "fingerprint": fingerprint,
                "state": BATCH_STATE_RECEIVED,
                "entries_count": counts,
                "deliveries": 1,
                "last_delivered_on": received,
            }
            self.store.save()
            return {
                "batch_id": batch_id,
                "state": BATCH_STATE_RECEIVED,
                "idempotent": False,
                "applied": counts,
            }

    def _apply_batch_entries(self, batch_type: str, batch_id: str, entries: dict[str, Any]) -> int:
        state = self.store.state
        count = 0

        if batch_type == BATCH_MASTER:
            for item in entries.get("ratings", []):
                event = model.rating_event(
                    item["bond_id"], item["announced"], item["grade"]
                )
                event["batch_id"] = batch_id
                if item["bond_id"] not in state["bonds"]:
                    raise ValidationError(f"债券 {item['bond_id']} 尚未登记")
                if event not in state["rating_events"]:
                    state["rating_events"].append(event)
                count += 1
            for item in entries.get("suspensions", []):
                event = model.suspension_event(
                    item["bond_id"], item["start"], item.get("resume")
                )
                event["batch_id"] = batch_id
                if item["bond_id"] not in state["bonds"]:
                    raise ValidationError(f"债券 {item['bond_id']} 尚未登记")
                state["suspensions"][item["bond_id"]] = event
                count += 1
            return count

        if batch_type == BATCH_QUOTE:
            for item in entries.get("quotes", []):
                if item["bond_id"] not in state["bonds"]:
                    raise ValidationError(f"债券 {item['bond_id']} 尚未登记")
                day = model.iso_date(item["date"], "行情日期")
                price = model.positive_number(item["price"], "行情价格")
                state["quotes"].setdefault(item["bond_id"], {})[day] = {
                    "price": price,
                    "batch_id": batch_id,
                }
                count += 1
            return count

        # 披露批次
        for item in entries.get("emissions", []):
            record = model.emission_record(
                record_id=item["record_id"],
                issuer_id=item["issuer_id"],
                source_id=item["source_id"],
                period_start=item["period_start"],
                period_end=item["period_end"],
                received=item.get("received", self._today()),
                revenue=item["revenue"],
                emissions_by_scope=item["emissions"],
                sensitivity=item.get("sensitivity", model.SENSITIVITY_PUBLIC),
                batch_id=batch_id,
            )
            self._store_emission(record)
            count += 1
        return count

    def submit_emission(self, principal: Principal, **kwargs: Any) -> dict[str, Any]:
        """非批次路径登记单条披露（供应方直送）。"""
        require_role(principal, ROLE_CLIMATE_PROVIDER)
        kwargs.setdefault("received", self._today())
        record = model.emission_record(**kwargs)
        with self.store.lock:
            self._store_emission(record)
            self.store.save()
        return record

    def _store_emission(self, record: dict[str, Any]) -> None:
        state = self.store.state
        if record["source_id"] not in state["climate_sources"]:
            raise ValidationError("气候数据来源尚未登记")
        if record["issuer_id"] not in state["issuers"]:
            raise ValidationError("发行人主数据尚未登记")
        if any(item["record_id"] == record["record_id"] for item in state["emissions"]):
            raise ConflictError(f"排放记录 {record['record_id']} 已存在")
        state["emissions"].append(record)

    def withdraw_emission(
        self, principal: Principal, record_id: str, withdrawn_on: str | None = None
    ) -> dict[str, Any]:
        """供应方撤回披露：进入补救期窗口，不删除历史。"""
        require_role(principal, ROLE_CLIMATE_PROVIDER)
        day = model.iso_date(withdrawn_on or self._today(), "撤回日")
        with self.store.lock:
            target = next(
                (item for item in self.store.state["emissions"] if item["record_id"] == record_id),
                None,
            )
            if target is None:
                # 对受限数据同样不暴露存在性
                raise NotFound("气候数据不存在或不可见")
            target["withdrawn"] = True
            target["withdrawn_on"] = day
            self.store.state["withdrawals"].append(
                {"version_of": target["version_of"], "record_id": record_id, "withdrawn_on": day}
            )
            self.store.save()
            return dict(target)

    # ================================================================ 调仓任务

    def run_rebalance(
        self,
        principal: Principal,
        rebalance_date: str,
        watermark: str | None = None,
        method_key: str | None = None,
        *,
        label: str = "original",
        crash_after: str | None = None,
    ) -> dict[str, Any]:
        """触发一次指数族调仓。

        ``label='original'``（默认）为该调仓日的权威发布：一旦成功即冻结，
        以后更高水位或新方法都不会覆盖它；重算视角通过
        :meth:`run_restated_view` 单独发布。故障恢复后调用
        :meth:`recover_interrupted` 继续。
        """
        require_role(principal, ROLE_INDEX_CALCULATOR)
        day = model.iso_date(rebalance_date, "调仓日")
        with self.store.lock:
            if day not in self.store.state["rebalance_calendar"]:
                raise ValidationError(f"{day} 不在调仓日历内")
            _, method = self._require_active_method(method_key)
            key = method_key or self.store.state["active_method_key"]
            wm = model.iso_date(watermark or day, "数据水位")
            if wm < day:
                raise ValidationError("数据水位不能早于调仓日")

            # 同一（调仓日，视角标签）只允许一个任务：重入即恢复而非重开。
            existing = next(
                (
                    task
                    for task in self.store.state["tasks"].values()
                    if task["rebalance_date"] == day and task["label"] == label
                ),
                None,
            )
            if existing and existing["status"] in (TASK_PENDING, TASK_RUNNING):
                if existing["task_id"] in self._in_flight:
                    raise TaskPending(f"调仓任务 {existing['task_id']} 正在执行")
                # 悬挂任务：直接走恢复路径继续。
                return self._execute_task(existing, crash_after=crash_after)
            if (
                existing
                and existing["status"] == TASK_DONE
                and crash_after is None
            ):
                done_run = self.store.state["runs"].get(existing["run_id"])
                if done_run and done_run["status"] == RUN_PUBLISHED:
                    raise ConflictError(
                        f"调仓日 {day} 的 {label} 视角已由任务 {existing['task_id']} "
                        "冻结发布，请使用复现接口；重算请用 run_restated_view"
                    )

            task_id = self.store.next_seq("TASK")
            run_id = self.store.next_seq("RUN")
            task = {
                "task_id": task_id,
                "run_id": run_id,
                "rebalance_date": day,
                "label": label,
                "watermark": wm,
                "method_key": key,
                "status": TASK_PENDING,
                "created_on": self._today(),
                "checkpoint": "created",
                "error": None,
            }
            self.store.state["tasks"][task_id] = task
            self.store.save()
            self._log(task, "created")
            return self._execute_task(task, crash_after=crash_after)

    def run_restated_view(
        self,
        principal: Principal,
        rebalance_date: str,
        restatement_id: str,
        watermark: str,
        method_key: str | None = None,
    ) -> dict[str, Any]:
        """用重述批准后的新水位/方法重算历史调仓日，单独打标签发布。

        不覆盖原始发布；投资者复现默认仍取回原始冻结数字，差异通过报告中的
        重述原因与本视角运行呈现。
        """
        require_role(principal, ROLE_INDEX_CALCULATOR)
        with self.store.lock:
            record = self.store.state["restatements"].get(restatement_id)
            if record is None:
                raise NotFound("重述提议不存在")
            if record["status"] != RESTATE_APPROVED:
                raise ConflictError("仅已批准重述可产生重算视角")
        label = f"restated:{restatement_id}"
        return self.run_rebalance(
            principal,
            rebalance_date,
            watermark=watermark,
            method_key=method_key,
            label=label,
        )

    def _execute_task(self, task: dict[str, Any], *, crash_after: str | None = None) -> dict[str, Any]:
        self._in_flight.add(task["task_id"])
        try:
            with self.store.lock:
                task["status"] = TASK_RUNNING
                task["checkpoint"] = "started"
                task["started_on"] = self._today()
                self.store.save()
                self._log(task, "started")

            if crash_after == "started":
                raise RuntimeError("模拟崩溃：计算开始前")

            with self.store.lock:
                method = self.store.state["methods"][task["method_key"]]
                snapshot = compute_family(
                    self.store.state,
                    task["rebalance_date"],
                    task["watermark"],
                    method,
                )
                task["checkpoint"] = "computed"
                self.store.save()
                self._log(task, "computed")

            if crash_after == "computed":
                raise RuntimeError("模拟崩溃：计算完成、发布前")

            with self.store.lock:
                input_batches = self._collect_input_batches(snapshot)
                blocked = [
                    bid
                    for bid in input_batches
                    if self.store.state["batches"].get(bid, {}).get("state") == BATCH_STATE_BLOCKED
                ]
                run = {
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                    "rebalance_date": task["rebalance_date"],
                    "label": task.get("label", "original"),
                    "watermark": task["watermark"],
                    "method_key": task["method_key"],
                    "published_on": self._today(),
                    "status": RUN_PUBLISHED,
                    "snapshot": snapshot,
                    "input_batches": sorted(input_batches),
                    "blocked_by_batch": None,
                }
                if blocked:
                    run["status"] = RUN_BLOCKED
                    run["blocked_by_batch"] = blocked[0]
                    task["status"] = TASK_FAILED
                    task["error"] = f"publication_blocked:{blocked[0]}"
                    task["checkpoint"] = "publish_blocked"
                    self.store.state["runs"][run["run_id"]] = run
                    self.store.save()
                    self._log(task, "publish_blocked", {"batch_id": blocked[0]})
                    raise BatchBlocked(blocked[0], "", "")

                # 原始发布冻结为该调仓日的权威版本；重算视角独立存放，互不覆盖。
                self.store.state["runs"][run["run_id"]] = run
                if run["rebalance_date"] not in self.store.state["run_order"]:
                    self.store.state["run_order"].append(run["rebalance_date"])
                task["status"] = TASK_DONE
                task["checkpoint"] = "published"
                task["error"] = None
                self.store.save()
                self._log(task, "published", {"run_id": run["run_id"]})
                return dict(run)
        finally:
            self._in_flight.discard(task["task_id"])

    def recover_interrupted(self, principal: Principal) -> list[dict[str, Any]]:
        """扫描落盘状态中悬挂（pending/running）的调仓任务并继续执行。

        计算是确定性的纯函数，恢复即重放；任务号与运行号沿用，不会产生重复发布。
        """
        require_role(principal, ROLE_INDEX_CALCULATOR)
        recovered = []
        with self.store.lock:
            stale = [
                task
                for task in self.store.state["tasks"].values()
                if task["status"] in (TASK_PENDING, TASK_RUNNING)
                and task["task_id"] not in self._in_flight
            ]
        for task in sorted(stale, key=lambda item: item["task_id"]):
            self._log(task, "recovered")
            recovered.append(self._execute_task(task))
        return recovered

    def _collect_input_batches(self, snapshot: dict[str, Any]) -> set[str]:
        state = self.store.state
        batches: set[str] = set()
        day = snapshot["rebalance_date"]
        for quotes in state["quotes"].values():
            quote = quotes.get(day)
            if quote and quote.get("batch_id"):
                batches.add(quote["batch_id"])
        record_ids = {
            row["emission_record_id"]
            for row in snapshot["shared_eligibility"]
            if row["emission_record_id"]
        }
        for record in state["emissions"]:
            if record["record_id"] in record_ids and record.get("batch_id"):
                batches.add(record["batch_id"])
        for event in state["rating_events"]:
            if event.get("batch_id") and event["announced"] <= snapshot["watermark"]:
                batches.add(event["batch_id"])
        for event in state["suspensions"].values():
            if event.get("batch_id") and event["start"] <= snapshot["watermark"]:
                batches.add(event["batch_id"])
        return batches

    def _log(self, task: dict[str, Any], event: str, detail: dict[str, Any] | None = None) -> None:
        self.store.append_task_event(
            {
                "at": self._today(),
                "task_id": task["task_id"],
                "run_id": task["run_id"],
                "rebalance_date": task["rebalance_date"],
                "event": event,
                "detail": detail or {},
            }
        )

    # ================================================================ 重述（职责分离）

    def propose_restatement(
        self,
        principal: Principal,
        kind: str,
        reason: str,
        *,
        new_emission: dict[str, Any] | None = None,
        new_methodology: dict[str, Any] | None = None,
        supersedes_record_id: str | None = None,
    ) -> dict[str, Any]:
        """方法维护者提出回溯重述。提出后必须由方法审批人员（另一用户）批准。"""
        require_role(principal, ROLE_METHOD_OWNER)
        model.require(reason, "重述原因")
        if kind not in (RESTATE_DATA, RESTATE_METHOD):
            raise ValidationError("重述类型无效")
        if kind == RESTATE_DATA and not new_emission:
            raise ValidationError("数据重述必须提供新的排放数据")
        if kind == RESTATE_METHOD and not new_methodology:
            raise ValidationError("方法重述必须提供新的方法版本内容")

        with self.store.lock:
            rid = self.store.next_seq("R")
            record = {
                "restatement_id": rid,
                "kind": kind,
                "reason": reason.strip(),
                "status": RESTATE_PROPOSED,
                "proposed_by": principal.user_id,
                "proposed_on": self._today(),
                "decided_by": None,
                "decided_on": None,
                "supersedes_record_id": supersedes_record_id,
                "new_emission": new_emission,
                "new_methodology": new_methodology,
                "applied": False,
            }
            self.store.state["restatements"][rid] = record
            self.store.save()
        return dict(record)

    def approve_restatement(self, principal: Principal, restatement_id: str) -> dict[str, Any]:
        require_role(principal, ROLE_METHOD_APPROVER)
        with self.store.lock:
            record = self.store.state["restatements"].get(restatement_id)
            if record is None:
                raise NotFound("重述提议不存在")
            require_distinct_user(principal, record["proposed_by"], "重述批准")
            if record["status"] != RESTATE_PROPOSED:
                raise ConflictError(f"重述 {restatement_id} 已{record['status']}")

            if record["kind"] == RESTATE_DATA:
                applied = self._apply_data_restatement(record, principal.user_id)
            else:
                applied = self._apply_method_restatement(record, principal.user_id)

            record["status"] = RESTATE_APPROVED
            record["decided_by"] = principal.user_id
            record["decided_on"] = self._today()
            record["applied"] = True
            record["application"] = applied
            # 提议负载不再需要长期保留（含原始数值），仅保留审批线索。
            record["new_emission"] = None
            record["new_methodology"] = None
            self.store.save()
            return dict(record)

    def reject_restatement(self, principal: Principal, restatement_id: str, note: str = "") -> dict[str, Any]:
        require_role(principal, ROLE_METHOD_APPROVER)
        with self.store.lock:
            record = self.store.state["restatements"].get(restatement_id)
            if record is None:
                raise NotFound("重述提议不存在")
            require_distinct_user(principal, record["proposed_by"], "重述驳回")
            if record["status"] != RESTATE_PROPOSED:
                raise ConflictError("仅待审批提议可驳回")
            record["status"] = RESTATE_REJECTED
            record["decided_by"] = principal.user_id
            record["decided_on"] = self._today()
            record["reject_note"] = note
            record["new_emission"] = None
            record["new_methodology"] = None
            self.store.save()
            return dict(record)

    def _apply_data_restatement(self, restatement: dict[str, Any], approver: str) -> dict[str, Any]:
        state = self.store.state
        payload = dict(restatement["new_emission"])
        supersedes_id = restatement.get("supersedes_record_id")
        old = next(
            (item for item in state["emissions"] if item["record_id"] == supersedes_id),
            None,
        )
        if old is None:
            # 找到该发行人该业务事实的当前版本
            old = self._current_emission(payload["issuer_id"], payload.get("version_of"))
        if old is None:
            raise ValidationError("被重述的原记录不存在")

        new_id = self.store.next_seq("EM")
        new_record = model.emission_record(
            record_id=new_id,
            issuer_id=old["issuer_id"],
            source_id=payload.get("source_id", old["source_id"]),
            period_start=payload["period_start"],
            period_end=payload["period_end"],
            received=self._today(),  # 批准日才进入水位：历史水位看不到新版本
            revenue=payload["revenue"],
            emissions_by_scope=payload["emissions"],
            sensitivity=payload.get("sensitivity", old["sensitivity"]),
        )
        new_record["version_of"] = old["version_of"]
        new_record["version"] = old["version"] + 1
        new_record["superseded_by"] = None
        new_record["restatement_id"] = restatement["restatement_id"]
        state["emissions"].append(new_record)
        old["superseded_by"] = new_id
        return {
            "superseded_record_id": old["record_id"],
            "new_record_id": new_id,
            "new_version": new_record["version"],
            "visible_from_watermark": new_record["received"],
        }

    def _current_emission(self, issuer_id: str, version_of: str | None) -> dict[str, Any] | None:
        candidates = [
            item
            for item in self.store.state["emissions"]
            if item["issuer_id"] == issuer_id
            and (version_of is None or item["version_of"] == version_of)
            and item["superseded_by"] is None
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item["version"], item["record_id"]))
        return candidates[-1]

    def _apply_method_restatement(self, restatement: dict[str, Any], approver: str) -> dict[str, Any]:
        spec = dict(restatement["new_methodology"])
        record = model.methodology_version(**spec)
        key = self._key(record["method_id"], record["version"])
        state = self.store.state
        if key in state["methods"]:
            raise ConflictError(f"方法版本 {key} 已存在")
        record["status"] = METHOD_ACTIVE
        record["proposed_by"] = restatement["proposed_by"]
        record["activated_by"] = approver
        record["activated_on"] = self._today()
        record["restatement_id"] = restatement["restatement_id"]
        if state["active_method_key"]:
            state["methods"][state["active_method_key"]]["status"] = METHOD_DEACTIVATED
        state["methods"][key] = record
        state["method_order"].append(key)
        old_key = state["active_method_key"]
        state["active_method_key"] = key
        return {"old_method_key": old_key, "new_method_key": key}

    # ================================================================ 查询与复现

    def search_emissions(self, principal: Principal, issuer_id: str | None = None) -> list[dict[str, Any]]:
        """搜索披露。无权角色的结果集中根本不包含受限记录（无法据此确认存在性）。"""
        records = [
            item
            for item in self.store.state["emissions"]
            if issuer_id is None or item["issuer_id"] == issuer_id
        ]
        return [dict(item) for item in filter_visible_emissions(records, principal)]

    def get_emission(self, principal: Principal, record_id: str) -> dict[str, Any]:
        record = next(
            (item for item in self.store.state["emissions"] if item["record_id"] == record_id),
            None,
        )
        return dict(public_or_visible(record, principal))

    def list_runs(
        self, principal: Principal, rebalance_date: str | None = None
    ) -> list[dict[str, Any]]:
        require_any_role(
            principal,
            frozenset({ROLE_INVESTOR, ROLE_INDEX_CALCULATOR, ROLE_METHOD_APPROVER, ROLE_METHOD_OWNER}),
        )
        runs = sorted(self.store.state["runs"].values(), key=lambda item: item["run_id"])
        if rebalance_date:
            runs = [item for item in runs if item["rebalance_date"] == rebalance_date]
        return [
            {
                "run_id": item["run_id"],
                "rebalance_date": item["rebalance_date"],
                "watermark": item["watermark"],
                "method_key": item["method_key"],
                "status": item["status"],
                "published_on": item["published_on"],
                "blocked_by_batch": item.get("blocked_by_batch"),
            }
            for item in runs
        ]

    def reproduce(
        self,
        principal: Principal,
        *,
        run_id: str | None = None,
        rebalance_date: str | None = None,
        index_id: str | None = None,
    ) -> dict[str, Any]:
        """对外复现：返回某日冻结的成分、权重、碳指标、基准差异及后来重述原因。

        不会用最新数据重算；默认取该调仓日 **当时发布** 的运行（被取代的运行
        只能通过 run_id 显式索取），并列出发布之后发生的重述。
        """
        require_any_role(
            principal,
            frozenset({ROLE_INVESTOR, ROLE_INDEX_CALCULATOR, ROLE_METHOD_APPROVER, ROLE_METHOD_OWNER}),
        )
        run = self._resolve_run(run_id, rebalance_date)
        snapshot = run["snapshot"]

        later_restatements = self._later_restatements(run)
        affected_records = {
            affected
            for item in later_restatements
            if item["kind"] == RESTATE_DATA
            for affected in item["affects"]
        }

        indexes_out: dict[str, Any] = {}
        for idx_id, result in snapshot["indexes"].items():
            if index_id and idx_id != index_id:
                continue
            constituents = []
            for row in result["constituents"]:
                visible = row["emission_sensitivity"] != SENSITIVITY_RESTRICTED or bool(
                    principal.roles & model.CLIMATE_VIEWER_ROLES
                )
                constituents.append(
                    {
                        "bond_id": row["bond_id"],
                        "issuer_id": row["issuer_id"],
                        "name": row["name"],
                        "weight": row["weight"],
                        "weight_decimal": row["weight_decimal"],
                        "carbon_intensity": row["carbon_intensity"] if visible else None,
                        "emission_record_id": row["emission_record_id"] if visible else None,
                        "emission_version": row["emission_version"] if visible else None,
                        "restated_afterward": row["emission_record_id"] in affected_records,
                        "restricted_value_masked": not visible,
                    }
                )
            indexes_out[idx_id] = {
                key: result[key]
                for key in (
                    "name", "parent_id", "constituent_count", "weight_sum",
                    "waci", "baseline_waci", "reduction_vs_baseline_pct",
                    "delta_vs_core", "weight_scheme",
                )
                if key in result
            }
            indexes_out[idx_id]["constituents"] = constituents

        can_view_restricted = bool(principal.roles & model.CLIMATE_VIEWER_ROLES)
        eligibility_out = []
        for row in snapshot["shared_eligibility"]:
            if (
                row.get("emission_sensitivity") == SENSITIVITY_RESTRICTED
                and not can_view_restricted
            ):
                row = dict(row)
                row["emission_record_id"] = None
                row["emission_version"] = None
                row["emission_masked"] = True
                row.pop("emission_sensitivity", None)
            else:
                row = dict(row)
                row["emission_masked"] = False
            eligibility_out.append(row)

        return {
            "run_id": run["run_id"],
            "task_id": run["task_id"],
            "rebalance_date": run["rebalance_date"],
            "label": run.get("label", "original"),
            "published_on": run["published_on"],
            "publication_status": run["status"],
            "blocked_by_batch": run.get("blocked_by_batch"),
            "watermark": snapshot["watermark"],
            "method": snapshot["method_snapshot"],
            "input_batches": run.get("input_batches", []),
            "indexes": indexes_out,
            "shared_eligibility": eligibility_out,
            "later_restatements": later_restatements,
            "reproducibility_note": (
                "本结果冻结自已发布运行的快照：固定数据水位与方法版本，"
                "不以最新数据重算。later_restatements 列出发布之后影响这些数字的重述。"
            ),
        }

    def _resolve_run(self, run_id: str | None, rebalance_date: str | None) -> dict[str, Any]:
        state = self.store.state
        if run_id is not None:
            run = state["runs"].get(run_id)
            if run is None:
                raise NotFound("运行不存在")
            return run
        if rebalance_date is None:
            raise ValidationError("必须提供 run_id 或 rebalance_date")
        candidates = [
            item
            for item in state["runs"].values()
            if item["rebalance_date"] == rebalance_date
        ]
        if not candidates:
            raise NotFound("该调仓日没有发布记录")
        # 默认权威视角：该调仓日的 original 发布。
        originals = [
            item
            for item in candidates
            if item.get("label", "original") == "original"
        ]
        target = sorted(originals, key=lambda item: item["run_id"])[-1] if originals else None
        if target is not None and target["status"] == RUN_PUBLISHED:
            return target
        if target is not None and target["status"] == RUN_BLOCKED:
            raise ConflictError(
                f"该调仓日权威发布被批次 {target.get('blocked_by_batch')} 阻断；"
                "重算视角可凭 run_id 查看"
            )
        blocked = [item for item in candidates if item["status"] == RUN_BLOCKED]
        if blocked and target is None:
            newest_blocked = sorted(blocked, key=lambda item: item["run_id"])[-1]
            raise ConflictError(
                f"该调仓日无权威发布，最近发布被批次 "
                f"{newest_blocked.get('blocked_by_batch')} 阻断"
            )
        # 只有重算视角时也必须显式 run_id，避免默认给出“最新数字”。
        raise NotFound("该调仓日无原始冻结发布，请以 run_id 指定重算视角")

    def _later_restatements(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        snapshot = run["snapshot"]
        used_records = {
            row["emission_record_id"]
            for row in snapshot["shared_eligibility"]
            if row["emission_record_id"]
        }
        used_method = snapshot["method_key"]
        result = []
        for item in self.store.state["restatements"].values():
            if item["status"] != RESTATE_APPROVED:
                continue
            if item["decided_on"] < run["published_on"]:
                continue
            touches = []
            if item["kind"] == RESTATE_DATA:
                application = item.get("application") or {}
                if application.get("superseded_record_id") in used_records:
                    touches.append(application["superseded_record_id"])
            elif item["kind"] == RESTATE_METHOD and used_method != self.store.state["active_method_key"]:
                # 运行所用方法后来被停用即受影响
                if self.store.state["methods"].get(used_method, {}).get("status") == METHOD_DEACTIVATED:
                    touches.append(used_method)
            if touches:
                result.append(
                    {
                        "restatement_id": item["restatement_id"],
                        "kind": item["kind"],
                        "reason": item["reason"],
                        "proposed_by": item["proposed_by"],
                        "approved_by": item["decided_by"],
                        "approved_on": item["decided_on"],
                        "affects": touches,
                    }
                )
        return result

    def verify_run_recomputable(self, principal: Principal, run_id: str) -> dict[str, Any]:
        """用冻结的水位与方法版本重算，证明快照可复现（调试/审计用）。"""
        require_role(principal, ROLE_INDEX_CALCULATOR)
        run = self.store.state["runs"].get(run_id)
        if run is None:
            raise NotFound("运行不存在")
        method = self.store.state["methods"][run["method_key"]]
        replay = compute_family(
            self.store.state,
            run["rebalance_date"],
            run["watermark"],
            method,
            allow_inactive=True,
        )
        matches = {
            idx_id: replay["indexes"][idx_id]["waci_exact"]
            == run["snapshot"]["indexes"][idx_id]["waci_exact"]
            for idx_id in replay["indexes"]
        }
        return {
            "run_id": run_id,
            "waci_matches": matches,
            "all_match": all(matches.values()),
        }
