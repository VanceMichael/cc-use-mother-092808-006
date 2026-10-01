"""服务统一错误类型。"""

from __future__ import annotations


class CbiError(Exception):
    """所有服务错误的基类。"""


class ValidationError(CbiError):
    """登记或参数校验失败。"""


class NotFound(CbiError):
    """对象不存在。"""


class ConflictError(CbiError):
    """状态冲突（版本重复、内容不一致、发布被阻断等）。"""


class AuthorizationError(CbiError):
    """角色无权执行操作。"""


class AccessDenied(AuthorizationError):
    """无权访问受限气候数据。

    调用方对“存在但受限”和“不存在”得到完全相同的错误，
    避免通过响应差异确认受限数据是否存在。
    """


class InactiveMethodError(CbiError):
    """计算引用了尚未激活或已停用的方法版本。"""


class BatchBlocked(CbiError):
    """重送批次内容与首次入账不同，相关发布必须阻断。"""

    def __init__(self, batch_id: str, first_fingerprint: str, incoming_fingerprint: str):
        super().__init__(f"批次 {batch_id} 重送内容不一致，发布已阻断")
        self.batch_id = batch_id
        self.first_fingerprint = first_fingerprint
        self.incoming_fingerprint = incoming_fingerprint


class TaskPending(CbiError):
    """调仓任务仍在运行，禁止重复触发。"""
