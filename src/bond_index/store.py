"""JSON 原子持久化与状态仓库。

无外部依赖：全部状态保存在单个 JSON 文件中，写入采用
"临时文件 + os.replace" 原子替换，崩溃不会留下半截状态。
Decimal 以带标记字符串往返，避免浮点化。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")


def canonical_hash(payload: Any) -> str:
    """对任意可 JSON 化内容计算稳定哈希，用于批次幂等判定。"""

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=_default)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return {"__decimal__": str(obj)}
    if hasattr(obj, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(obj)
    if isinstance(obj, frozenset):
        return {"__frozenset__": sorted(obj, key=str)}
    raise TypeError(f"不可序列化的类型: {type(obj)!r}")


def _restore(value: Any) -> Any:
    if isinstance(value, dict):
        if "__decimal__" in value:
            return Decimal(value["__decimal__"])
        if "__frozenset__" in value:
            return frozenset(value["__frozenset__"])
        return {k: _restore(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_restore(v) for v in value]
    return value


def _empty_state() -> dict[str, Any]:
    return {
        "issuers": {},
        "bonds": {},
        "sources": {},
        "scopes": {},
        "screens": {},
        "weight_methods": {},
        "indexes": {},
        "method_versions": {},
        "calendar": [],
        "disclosures": {},
        "events": {},
        "benchmark": [],
        "market_prices": [],          # [{bond_id, as_of, price, batch_id}]
        "batches": {},                # batch_id -> {kind, hash, status, received_at}
        "runs": {},                   # run_id -> 计算结果快照
        "publications": [],           # 已发布记录
        "restatements": {},
        "rebalance_tasks": {},
        "locks": {},                  # 发布互斥标记
    }


class Store:
    """文件支持的状态仓库。

    每次 :meth:`mutate` 在锁内读取最新状态、执行变更并原子落盘，
    保证同进程内串行化；跨进程依赖原子替换的最后写入（测试与单实例部署足够）。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        if self.path and self.path.exists():
            self._state = _restore(json.loads(self.path.read_text(encoding="utf-8")))
        else:
            self._state = _empty_state()
            self._flush()

    # ------------------------------------------------------------------ 基础

    def _flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(self._state, ensure_ascii=False, sort_keys=True,
                             indent=2, default=_default)
        fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp",
                                   dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            os.unlink(tmp)
            raise

    def view(self) -> dict[str, Any]:
        """只读状态快照（同一把锁，防止读到写入中途）。"""

        with self._lock:
            return self._state

    def mutate(self, fn: Callable[[dict[str, Any]], T]) -> T:
        """在锁内变更状态并落盘，fn 的返回值透传给调用方。"""

        with self._lock:
            result = fn(self._state)
            self._flush()
            return result

    def reset(self) -> None:
        """清空状态（主要供测试与示例使用）。"""

        with self._lock:
            self._state = _empty_state()
            self._flush()
