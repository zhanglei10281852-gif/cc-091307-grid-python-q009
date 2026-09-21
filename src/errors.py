"""领域错误。"""
from __future__ import annotations


class DomainError(Exception):
    """所有业务校验错误的基类，携带机器可读 code。"""

    code = "domain_error"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class NotFoundError(DomainError):
    code = "not_found"


class DuplicateClientEvent(DomainError):
    """客户端事件幂等命中：同一 (client_id, client_event_seq) 重复发送。"""

    code = "duplicate_client_event"


class StaleUpdateError(DomainError):
    """过期更新：expected_version 落后于工单当前事件版本。"""

    code = "stale_update"


class InvalidTransition(DomainError):
    code = "invalid_transition"


class PermissionDenied(DomainError):
    code = "permission_denied"


class MergeError(DomainError):
    code = "merge_error"
