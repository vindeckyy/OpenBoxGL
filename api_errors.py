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


class AmbiguousPlatform(BadRequest):
    code = "AMBIGUOUS_PLATFORM"


class EmulatorRequired(Conflict):
    code = "EMULATOR_REQUIRED"


class PreviewLimitExceeded(BadRequest):
    code = "PREVIEW_LIMIT_EXCEEDED"


class PreviewEntryLimitExceeded(BadRequest):
    code = "PREVIEW_ENTRY_LIMIT_EXCEEDED"
