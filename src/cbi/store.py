"""原子化 JSON 状态存储与只增任务日志。

- ``state.json``：全部登记表与计算结果，临时文件 + ``os.replace`` 原子落盘，
  进程崩溃只可能留下旧版本或新版本，不会出现半写文件。
- ``tasks.jsonl``：调仓任务生命周期事件，只增不删，作为恢复与审计线索。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any


STATE_FILENAME = "state.json"
TASK_LOG_FILENAME = "tasks.jsonl"


def canonical_fingerprint(payload: Any) -> str:
    """与键顺序无关的规范指纹，用于批次幂等与内容比对。"""
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _empty_state() -> dict[str, Any]:
    return {
        "issuers": {},
        "bonds": {},
        "rating_events": [],          # [{bond_id, announced, grade}]
        "suspensions": {},            # bond_id -> event
        "climate_sources": {},
        "quotes": {},                 # bond_id -> {date: {price, batch_id}}
        "emissions": [],              # 全部版本（含被取代/撤回）
        "withdrawals": [],            # [{version_of, withdrawn_on, batch_id}]
        "methods": {},                # key "method_id:version" -> 方法版本
        "method_order": [],
        "active_method_key": None,
        "indexes": {},
        "core_index_id": None,
        "rebalance_calendar": [],     # 已登记调仓日（升序）
        "batches": {},                # batch_id -> 批次登记
        "tasks": {},                  # task_id -> 任务状态
        "runs": {},                   # run_key -> 计算运行（含历次重述版本）
        "run_order": [],
        "restatements": {},           # restatement_id -> 提议/审批
        "seq": 0,
    }


class Store:
    """线程安全的状态存储。"""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state_path = self.directory / STATE_FILENAME
        self.task_log_path = self.directory / TASK_LOG_FILENAME
        self._lock = threading.RLock()
        self.state = self._load()

    # ------------------------------------------------------------------ 持久化

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return _empty_state()
        raw = self.state_path.read_text(encoding="utf-8")
        if not raw.strip():
            return _empty_state()
        state = json.loads(raw)
        # 向前兼容：补齐缺失的顶层段。
        for key, default in _empty_state().items():
            state.setdefault(key, default if not isinstance(default, (dict, list)) else type(default)())
        return state

    def save(self) -> None:
        """原子写入：先写临时文件再替换。"""
        with self._lock:
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp, self.state_path)

    # ------------------------------------------------------------------ 锁

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # ------------------------------------------------------------------ 任务日志

    def append_task_event(self, event: dict[str, Any]) -> None:
        """向只增日志追加一条任务事件（每次追加都落盘并 flush）。"""
        line = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock:
            with self.task_log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def read_task_events(self) -> list[dict[str, Any]]:
        if not self.task_log_path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.task_log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    # ------------------------------------------------------------------ 序号

    def next_seq(self, prefix: str) -> str:
        with self._lock:
            self.state["seq"] += 1
            return f"{prefix}-{self.state['seq']:06d}"
