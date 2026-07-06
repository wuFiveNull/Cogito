"""领域异常层级。

所有领域异常继承自 CogitoError。用于表达：
- 实体未找到
- 非法状态转移
- 并发冲突
- 幂等违反
- 租约问题
- 审批/策略拒绝
- 路由/投递失败
"""


class CogitoError(Exception):
    """所有领域异常的基类。"""


class EntityNotFoundError(CogitoError):
    """请求的实体不存在。"""

    def __init__(self, entity_type: str, entity_id: str) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"{entity_type} not found: {entity_id}")


class InvalidStateTransitionError(CogitoError):
    """状态转移不被允许。"""

    def __init__(
        self, entity_type: str, entity_id: str, current_status: str, target_status: str
    ) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.current_status = current_status
        self.target_status = target_status
        super().__init__(
            f"{entity_type}({entity_id}): {current_status} -> {target_status} is not allowed"
        )


class ConcurrencyConflictError(CogitoError):
    """乐观并发控制冲突（版本不匹配）。"""

    def __init__(
        self, entity_type: str, entity_id: str, expected_version: int, actual_version: int
    ) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"{entity_type}({entity_id}): expected version {expected_version}, "
            f"got {actual_version}"
        )


class IdempotencyViolationError(CogitoError):
    """幂等键已存在且结果不同。"""

    def __init__(self, key: str, existing_entity_id: str) -> None:
        self.key = key
        self.existing_entity_id = existing_entity_id
        super().__init__(f"Idempotency key already used: {key} -> {existing_entity_id}")


class LeaseExpiredError(CogitoError):
    """Worker Lease 已过期，不能提交结果。"""

    def __init__(self, lease_owner: str, lease_version: int) -> None:
        self.lease_owner = lease_owner
        self.lease_version = lease_version
        super().__init__(f"Lease expired for owner={lease_owner}, version={lease_version}")


class LeaseConflictError(CogitoError):
    """Lease 条件更新冲突。"""

    def __init__(self, owner: str, expected_version: int, actual_version: int) -> None:
        self.owner = owner
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Lease conflict for owner={owner}: "
            f"expected version {expected_version}, got {actual_version}"
        )


class ApprovalExpiredError(CogitoError):
    """审批请求已过期。"""

    def __init__(self, approval_id: str, expired_at: str) -> None:
        self.approval_id = approval_id
        self.expired_at = expired_at
        super().__init__(f"Approval {approval_id} expired at {expired_at}")


class ApprovalUnauthorizedError(CogitoError):
    """响应者无权对该审批做出决定。"""

    def __init__(self, approval_id: str, responder_id: str) -> None:
        self.approval_id = approval_id
        self.responder_id = responder_id
        super().__init__(
            f"Responder {responder_id} not authorized for approval {approval_id}"
        )


class PolicyDeniedError(CogitoError):
    """Policy Engine 拒绝操作。"""

    def __init__(self, action: str, reason: str) -> None:
        self.action = action
        self.reason = reason
        super().__init__(f"Policy denied action '{action}': {reason}")


class RouteExpiredError(CogitoError):
    """Reply Token 已过期，无法路由回复。"""

    def __init__(self, reply_token: str, expired_at: str) -> None:
        self.reply_token = reply_token
        self.expired_at = expired_at
        super().__init__(f"Reply route expired for token {reply_token} at {expired_at}")


class DeliveryFailedError(CogitoError):
    """投递失败。"""

    def __init__(self, delivery_id: str, reason: str, retryable: bool = True) -> None:
        self.delivery_id = delivery_id
        self.reason = reason
        self.retryable = retryable
        super().__init__(f"Delivery {delivery_id} failed: {reason}")


class ValidationError(CogitoError):
    """领域数据校验失败。"""

    def __init__(self, field: str, value: object, reason: str) -> None:
        self.field = field
        self.value = value
        self.reason = reason
        super().__init__(f"Validation error on '{field}': {reason} (value={value!r})")
