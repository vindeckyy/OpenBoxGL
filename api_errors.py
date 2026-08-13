"""Structured API errors with stable machine-readable codes.

Handlers raise these instead of returning ad-hoc 400/404 payloads. The
dispatcher maps each class to an HTTP status and a JSON body of:

    {"error": <human text>, "code": <stable code>, "request_id": <id>}

Stable codes let the browser branch on the failure instead of matching
strings. Adding a code means adding a class here; never reuse a code with
a different meaning.
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


class Unauthorized(ApiError):
    status = 403
    code = "UNAUTHORIZED"


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


class MediaJobRunning(Conflict):
    code = "MEDIA_JOB_RUNNING"


class ServiceUnavailable(ApiError):
    status = 503
    code = "SERVICE_UNAVAILABLE"


class StateUnavailable(ServiceUnavailable):
    code = "STATE_UNAVAILABLE"
