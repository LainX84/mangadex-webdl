from __future__ import annotations

from app.enums import ErrorKind


class MangaDexError(Exception):
    kind: ErrorKind = ErrorKind.INTERNAL

    def __init__(self, message: str, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


class NetworkError(MangaDexError):
    kind = ErrorKind.NETWORK


class RateLimitedError(MangaDexError):
    kind = ErrorKind.RATE_LIMIT

    def __init__(self, message: str, retry_after: float | None = None, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class ServerError(MangaDexError):
    kind = ErrorKind.SERVER


class MaintenanceError(ServerError):
    """MangaDex answered with its "Down for maintenance" page (HTTP 5xx, HTML).

    A global maintenance pause is started before this is raised; ``until_ms`` is when
    the next request is allowed to check whether MangaDex is back.
    """

    kind = ErrorKind.MAINTENANCE

    def __init__(self, message: str = "MangaDex is down for maintenance", until_ms: int | None = None, **kw):
        super().__init__(message, **kw)
        self.until_ms = until_ms


class NotFoundError(MangaDexError):
    kind = ErrorKind.NOT_FOUND


class InvalidResponseError(MangaDexError):
    kind = ErrorKind.INVALID_RESPONSE


class AccessDeniedError(MangaDexError):
    kind = ErrorKind.ACCESS


class BadRequestError(MangaDexError):
    """4xx other than 401/403/404/429 – our request is wrong; retrying will not help."""

    kind = ErrorKind.INVALID_RESPONSE


class InvalidImageError(MangaDexError):
    kind = ErrorKind.INVALID_IMAGE


class AccountAuthError(MangaDexError):
    """The MangaDex *account* login failed (bad credentials, client not approved, token rejected).

    Unlike :class:`AccessDeniedError` this never stops anonymous traffic: only the
    account features (followed list, followed feed) are affected.
    """

    kind = ErrorKind.ACCESS


class AccountNotConfigured(MangaDexError):
    kind = ErrorKind.ACCESS
