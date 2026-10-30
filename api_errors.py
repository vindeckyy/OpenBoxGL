"""Structured API errors with stable machine-readable codes.

Handlers raise these; the dispatcher maps each class to an HTTP status and a JSON body with error/code/request_id.
"""


class ApiError(Exception):
    """Base class for all structured API errors."""

    status = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message, *, code=None, detail=None):
        super().__init__(message)
        self.message = str(message)
        self.code = code or self.code
        self.detail = detail

    def to_payload(self, request_id=""):
        payload = {"error": self.message, "code": self.code}
        if self.detail is not None:
            payload["detail"] = self.detail
        if request_id:
            payload["request_id"] = request_id
        return payload


class BadRequest(ApiError):
    status = 400
    code = "BAD_REQUEST"


class V1ApiDisabled(ApiError):
    """The /api/v1 surface is off unless OPENBOX_ENABLE_V1_API=1 (ADR 0091)."""

    status = 410
    code = "V1_API_DISABLED"


class NotFound(ApiError):
    status = 404
    code = "NOT_FOUND"


class GameNotFound(NotFound):
    code = "GAME_NOT_FOUND"


class MediaNotFound(NotFound):
    code = "MEDIA_NOT_FOUND"


class DocumentNotFound(NotFound):
    code = "DOCUMENT_NOT_FOUND"


class BadgeNotFound(NotFound):
    code = "BADGE_NOT_FOUND"


class PlatformDocumentNotFound(NotFound):
    code = "PLATFORM_DOCUMENT_NOT_FOUND"


class RouteNotFound(NotFound):
    code = "ROUTE_NOT_FOUND"


class Conflict(ApiError):
    status = 409
    code = "CONFLICT"


class JobStateConflict(Conflict):
    code = "JOB_STATE_CONFLICT"


class JobNotCancellable(Conflict):
    code = "JOB_NOT_CANCELLABLE"


class JobNotResumable(Conflict):
    code = "JOB_NOT_RESUMABLE"


class PreviewNotFound(NotFound):
    code = "PREVIEW_NOT_FOUND"


class PreviewExpired(Conflict):
    code = "PREVIEW_EXPIRED"


class PreviewStale(Conflict):
    code = "PREVIEW_STALE"


class PreviewLibraryChanged(Conflict):
    code = "PREVIEW_LIBRARY_CHANGED"


class UnresolvedCandidates(Conflict):
    code = "UNRESOLVED_CANDIDATES"


class PreviewLimitExceeded(BadRequest):
    code = "PREVIEW_LIMIT_EXCEEDED"


class PreviewEntryLimitExceeded(BadRequest):
    code = "PREVIEW_ENTRY_LIMIT_EXCEEDED"


class RangeParseError(ValueError):
    """``send_file`` could not parse the request's ``Range`` header.

    A ``ValueError`` subclass so legacy ``except ValueError`` handlers keep
    working; byte-serving routes catch this exact type to answer 416 without
    also swallowing unrelated validation errors.
    """


PROBLEM_TYPE_BASE = "https://openbox.local/problems/"
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_STATUS_TITLES = {
    400: "Bad request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not found",
    405: "Method not allowed",
    409: "Conflict",
    410: "Gone",
    413: "Payload too large",
    415: "Unsupported media type",
    422: "Unprocessable content",
    429: "Too many requests",
    500: "Internal error",
    503: "Service unavailable",
}


def problem_payload(status, payload, *, request_id="", instance=""):
    """Return an RFC 9457-style problem document built from a legacy error payload.

    ``code`` is the stable machine-readable key and ``type`` is derived from it,
    so both survive rewording. ``error`` duplicates ``detail`` for one major cycle.
    """
    title = _STATUS_TITLES.get(status, "Request failed")
    message = str(payload.get("error") or payload.get("detail") or title)
    code = str(payload.get("code") or f"HTTP_{status}")
    body = dict(payload)
    body.update({
        "type": PROBLEM_TYPE_BASE + code.lower().replace("_", "-"),
        "title": title,
        "status": status,
        "detail": message,
        "code": code,
        "error": message,
        "retryable": status in _RETRYABLE_STATUSES or "retry_after" in payload,
    })
    if request_id:
        body["request_id"] = request_id
    if instance:
        body["instance"] = instance
    return body


class HostApiUnsupported(Conflict):
    """A native host sent a major API version this server does not speak."""

    code = "HOST_API_UNSUPPORTED"


class BackupFormatUnsupported(BadRequest):
    """A backup manifest declares a format this build cannot read or write (ADR 0093)."""

    code = "BACKUP_FORMAT_UNSUPPORTED"


class MovePlanStale(Conflict):
    """The library changed after a move preview; the preview must be run again (ADR 0090)."""

    code = "MOVE_PLAN_STALE"
