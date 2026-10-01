"""气候债券指数编制服务。

公共入口：
- :class:`BondIndexService` 编制、发布、重述、调仓恢复、复现；
- :class:`Store` 文件状态仓库（原子写入）；
- :class:`Principal` 调用身份；
- 登记与批次函数见 :mod:`bond_index.registry` / :mod:`bond_index.ingest`。
"""

from __future__ import annotations

from .access_control import Principal
from .errors import (BatchConflictError, DuplicateError, IndexServiceError,
                     NotFoundError, PermissionDeniedError,
                     PublishBlockedError, RestrictedDataError, ValidationError,
                     WeightConservationError, WorkflowError)
from .models import (BatchKind, Bond, ClimateDataSource, ComplianceScreen,
                     DisclosureRecord, EmissionsScope, EventKind, IndexDef,
                     Issuer, MethodVersion, RatingEvent,
                     RebalanceCalendarEntry, RebalanceStatus,
                     RestatementStatus, RestrictionLevel, Role, RunKind,
                     WeightMethod)
from .service import BondIndexService
from .store import Store

__all__ = [
    "BondIndexService", "Store", "Principal",
    "Issuer", "Bond", "ClimateDataSource", "EmissionsScope",
    "ComplianceScreen", "WeightMethod", "IndexDef", "MethodVersion",
    "RebalanceCalendarEntry", "RatingEvent", "DisclosureRecord",
    "Role", "EventKind", "BatchKind", "RestrictionLevel",
    "RestatementStatus", "RebalanceStatus", "RunKind",
    "IndexServiceError", "ValidationError", "NotFoundError",
    "DuplicateError", "BatchConflictError", "PermissionDeniedError",
    "RestrictedDataError", "WorkflowError", "PublishBlockedError",
    "WeightConservationError",
]
