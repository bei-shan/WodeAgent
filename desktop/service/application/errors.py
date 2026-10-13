"""Stable application failures; transport adapters select their HTTP status."""

class ServiceError(Exception):
    kind = "internal"


class NotFoundError(ServiceError):
    kind = "not_found"


class ConflictError(ServiceError):
    kind = "conflict"


class InvalidInputError(ServiceError):
    kind = "invalid_input"
