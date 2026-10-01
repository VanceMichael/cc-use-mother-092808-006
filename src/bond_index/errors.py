"""债券指数编制服务的异常类型。

所有业务规则冲突都以 :class:`IndexErrorBase` 的子类抛出，
便于调用方按类别区分输入校验错误、权限错误与发布闸门错误。
"""

from __future__ import annotations


class IndexServiceError(Exception):
    """服务异常基类。"""


class ValidationError(IndexServiceError):
    """主数据或事件登记内容不合法。"""


class NotFoundError(IndexServiceError):
    """引用的发行人、债券、方法或指数不存在。"""


class DuplicateError(IndexServiceError):
    """主键、方法版本号或批次重复登记。"""


class BatchConflictError(IndexServiceError):
    """同编号批次重送但内容不同，关联发布被隔离。"""

    def __init__(self, batch_id: str, *, reason: str = "批次内容与首次入账不一致") -> None:
        super().__init__(f"批次 {batch_id} 冲突：{reason}")
        self.batch_id = batch_id


class PermissionDeniedError(IndexServiceError):
    """当前角色无权执行该操作。"""


class RestrictedDataError(IndexServiceError):
    """无权确认受限气候数据是否存在（存在性与内容同样保密）。

    抛出本异常时不区分"不存在"与"存在但无权访问"，
    避免调用方通过报错差异推断受限记录是否存在。
    """


class WorkflowError(IndexServiceError):
    """重述审批流或生效窗口状态不允许该操作。"""


class PublishBlockedError(IndexServiceError):
    """存在未决批次冲突、未批准重述等情况，发布被阻止。"""


class WeightConservationError(IndexServiceError):
    """权重无法在指数自身范围内守恒（合格集合为空等）。"""
