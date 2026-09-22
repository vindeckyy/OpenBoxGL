/*
 * OpenBox native host for Windows: a small WebView2 window rendering the web
 * app over loopback. Windows counterpart of native_host.c (WebKitGTK/GTK3),
 * implementing the same Python-facing contract: it owns the Python server
 * lifecycle, exposes native dialogs and window chrome via a JS bridge, and
 * shuts the server down when the window closes.
 *
 * Business logic stays in Python; this file is chrome, bridge, and process
 * ownership only.
 *
 * Build (repo root, from a Developer Command Prompt):
 *   scripts\build_native_host_windows.ps1
 * or directly:
 *   cl /O2 /W3 /DUNICODE /D_UNICODE native_host_win.c /Fe:native_host.exe \
 *      /I<webview2 sdk>\include <webview2 sdk>\x64\WebView2LoaderStatic.lib \
 *      ole32.lib oleaut32.lib uuid.lib user32.lib gdi32.lib shell32.lib \
 *      shlwapi.lib advapi32.lib ws2_32.lib /link /SUBSYSTEM:WINDOWS
 *
 * Deliberate divergences from the Linux host, all forced by the platform:
 *   - Single instance uses a named pipe instead of an AF_UNIX socket.
 *   - Process-group signalling is a Win32 job object (kill-on-close) plus a
 *     console CTRL_BREAK_EVENT for the graceful path.
 *   - The bridge is window.chrome.webview.postMessage instead of the WebKit
 *     message handler; the payload is a flat JSON string (see the parser).
 *   - "owner-only" file checks use reparse-point rejection plus the per-user
 *     profile ACL, since Windows has no POSIX mode bits.
 */

#define WIN32_LEAN_AND_MEAN
#define COBJMACROS
#include <windows.h>
#include <shellapi.h>
#include <shlobj.h>
#include <shlwapi.h>
#include <objbase.h>
#include <ws2tcpip.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#include "WebView2.h"

#define DEFAULT_DATA_DIR_NAME L"openbox-game-launcher"
#define BOOT_TIMEOUT_SECONDS 30
#define API_TIMEOUT_SECONDS 30
#define SHUTDOWN_GRACE_SECONDS 3
#define MAX_NATIVE_VALUE_BYTES 4096
#define MAX_IPC_MESSAGE_BYTES 8192
#define MAX_BRIDGE_MESSAGE_BYTES 8192

/*
 * Version strings surfaced in openbox-native.log at startup and echoed by
 * the Windows CI job (.github/workflows/ci.yml). WEBVIEW2_SDK_VERSION must
 * match the SdkVersion default in scripts/build_native_host_windows.ps1;
 * the SDK's NuGet cache key is microsoft.web.webview2/<version>.
 */
#define NATIVE_HOST_VERSION "1.13.1"
#define WEBVIEW2_SDK_VERSION "1.0.2651.64"

#define WM_APP_NATIVE_REQUEST (WM_APP + 1)
#define WM_APP_NATIVE_FOCUS (WM_APP + 2)
#define WM_APP_TRAY (WM_APP + 3)

#define TRAY_ICON_ID 1
#define IDM_TRAY_SHOW 40001
#define IDM_TRAY_QUIT 40002

typedef enum {
    NATIVE_REQUEST_NONE = 0,
    NATIVE_REQUEST_START,
    NATIVE_REQUEST_SHOWGAME,
    NATIVE_REQUEST_SEARCH,
    NATIVE_REQUEST_BIGBOX,
    NATIVE_REQUEST_SETTINGS,
    NATIVE_REQUEST_MOMENT,
    NATIVE_REQUEST_CLIP,
    NATIVE_REQUEST_LAUNCH,
    NATIVE_REQUEST_RESUME,
} NativeRequestKind;

typedef struct {
    NativeRequestKind kind;
    char *value;
} NativeRequest;

typedef enum {
    NATIVE_ARGS_NONE = 0,
    NATIVE_ARGS_REQUEST,
    NATIVE_ARGS_INVALID,
} NativeArgsResult;

/* ------------------------------------------------------------------ */
/* Host state                                                          */
/* ------------------------------------------------------------------ */

static HINSTANCE g_instance;
static HWND g_window;
static ICoreWebView2Controller *g_controller;
static ICoreWebView2 *g_webview;
static char *g_origin;          /* http://127.0.0.1:<port> */
static wchar_t *g_origin_wide;
static char *g_token;
static unsigned short g_port;
static char *g_data_dir;        /* absolute, UTF-8 */
static wchar_t *g_data_dir_wide;
static wchar_t *g_web_app_path;
static wchar_t *g_python_path;
static HANDLE g_server_process;
static HANDLE g_server_job;
static DWORD g_server_pid;
static HANDLE g_pipe;
static HANDLE g_pipe_thread;
static wchar_t *g_pipe_name;
static char *g_geometry_path;
static wchar_t *g_icon_path;
static int g_default_width = 1280;
static int g_default_height = 780;
static int g_window_maximized;
static int g_tray_enabled;
static int g_minimize_to_tray;
static int g_tray_added;
static int g_webview_ready;
static int g_closing;
static int g_pending_focus;
static NativeRequest g_pending_request = { NATIVE_REQUEST_NONE, NULL };
static RECT g_saved_window_rect;
static DWORD g_saved_window_style;
static int g_fullscreen;
static char *g_log_path;
static CRITICAL_SECTION g_log_lock;

/* ------------------------------------------------------------------ */
/* Logging                                                             */
/* ------------------------------------------------------------------ */

static void
log_line(const char *format, ...)
{
    char message[2048];
    va_list args;
    va_start(args, format);
    vsnprintf(message, sizeof(message), format, args);
    va_end(args);

    OutputDebugStringA(message);

    if (!g_log_path) {
        return;
    }
    EnterCriticalSection(&g_log_lock);
    FILE *handle = fopen(g_log_path, "ab");
    if (handle) {
        SYSTEMTIME now;
        GetLocalTime(&now);
        fprintf(handle, "%04d-%02d-%02d %02d:%02d:%02d %s",
                now.wYear, now.wMonth, now.wDay,
                now.wHour, now.wMinute, now.wSecond, message);
        fclose(handle);
    }
    LeaveCriticalSection(&g_log_lock);
}

/* ------------------------------------------------------------------ */
/* Growable string buffer                                              */
/* ------------------------------------------------------------------ */

typedef struct {
    char *data;
    size_t length;
    size_t capacity;
} StrBuf;

static void
sb_init(StrBuf *buffer)
{
    buffer->data = NULL;
    buffer->length = 0;
    buffer->capacity = 0;
}

static int
sb_reserve(StrBuf *buffer, size_t extra)
{
    size_t needed = buffer->length + extra + 1;
    if (needed <= buffer->capacity) {
        return 1;
    }
    size_t capacity = buffer->capacity ? buffer->capacity : 64;
    while (capacity < needed) {
        capacity *= 2;
    }
    char *data = (char *)realloc(buffer->data, capacity);
    if (!data) {
        return 0;
    }
    buffer->data = data;
    buffer->capacity = capacity;
    return 1;
}

static int
sb_append(StrBuf *buffer, const char *text, size_t length)
{
    if (!sb_reserve(buffer, length)) {
        return 0;
    }
    memcpy(buffer->data + buffer->length, text, length);
    buffer->length += length;
    buffer->data[buffer->length] = '\0';
    return 1;
}

static int
sb_append_str(StrBuf *buffer, const char *text)
{
    return sb_append(buffer, text, text ? strlen(text) : 0);
}

static int
sb_append_c(StrBuf *buffer, char character)
{
    return sb_append(buffer, &character, 1);
}

static int
sb_appendf(StrBuf *buffer, const char *format, ...)
{
    char stack[1024];
    va_list args;
    va_start(args, format);
    int written = vsnprintf(stack, sizeof(stack), format, args);
    va_end(args);
    if (written < 0) {
        return 0;
    }
    if ((size_t)written < sizeof(stack)) {
        return sb_append(buffer, stack, (size_t)written);
    }
    char *heap = (char *)malloc((size_t)written + 1);
    if (!heap) {
        return 0;
    }
    va_start(args, format);
    vsnprintf(heap, (size_t)written + 1, format, args);
    va_end(args);
    int ok = sb_append(buffer, heap, (size_t)written);
    free(heap);
    return ok;
}

static char *
sb_take(StrBuf *buffer)
{
    char *data = buffer->data;
    if (!data) {
        data = (char *)malloc(1);
        if (data) {
            data[0] = '\0';
        }
    }
    buffer->data = NULL;
    buffer->length = 0;
    buffer->capacity = 0;
    return data;
}

static void
sb_free(StrBuf *buffer)
{
    free(buffer->data);
    buffer->data = NULL;
    buffer->length = 0;
    buffer->capacity = 0;
}

static char *
dup_str(const char *text)
{
    if (!text) {
        return NULL;
    }
    size_t length = strlen(text);
    char *copy = (char *)malloc(length + 1);
    if (copy) {
        memcpy(copy, text, length + 1);
    }
    return copy;
}

static char *
dup_strn(const char *text, size_t length)
{
    char *copy = (char *)malloc(length + 1);
    if (copy) {
        memcpy(copy, text, length);
        copy[length] = '\0';
    }
    return copy;
}

static void
trim_in_place(char *text)
{
    if (!text) {
        return;
    }
    size_t length = strlen(text);
    while (length > 0 && (unsigned char)text[length - 1] <= 0x20) {
        text[--length] = '\0';
    }
    size_t start = 0;
    while (text[start] != '\0' && (unsigned char)text[start] <= 0x20) {
        start++;
    }
    if (start > 0) {
        memmove(text, text + start, length - start + 1);
    }
}

/* ------------------------------------------------------------------ */
/* UTF-8 and wide-string helpers                                       */
/* ------------------------------------------------------------------ */

static int
utf8_is_valid(const char *text, size_t length)
{
    size_t index = 0;
    while (index < length) {
        unsigned char lead = (unsigned char)text[index];
        size_t extra;
        unsigned int codepoint;
        if (lead < 0x80) {
            index++;
            continue;
        } else if ((lead & 0xE0) == 0xC0) {
            extra = 1;
            codepoint = lead & 0x1Fu;
        } else if ((lead & 0xF0) == 0xE0) {
            extra = 2;
            codepoint = lead & 0x0Fu;
        } else if ((lead & 0xF8) == 0xF0) {
            extra = 3;
            codepoint = lead & 0x07u;
        } else {
            return 0;
        }
        if (index + extra >= length) {
            return 0;
        }
        for (size_t offset = 1; offset <= extra; offset++) {
            unsigned char continuation = (unsigned char)text[index + offset];
            if ((continuation & 0xC0) != 0x80) {
                return 0;
            }
            codepoint = (codepoint << 6) | (continuation & 0x3Fu);
        }
        if (extra == 1 && codepoint < 0x80) {
            return 0;
        }
        if (extra == 2 && codepoint < 0x800) {
            return 0;
        }
        if (extra == 3 && codepoint < 0x10000) {
            return 0;
        }
        if (codepoint > 0x10FFFF || (codepoint >= 0xD800 && codepoint <= 0xDFFF)) {
            return 0;
        }
        index += extra + 1;
    }
    return 1;
}

static wchar_t *
utf8_to_wide(const char *text)
{
    if (!text) {
        return NULL;
    }
    int needed = MultiByteToWideChar(CP_UTF8, 0, text, -1, NULL, 0);
    if (needed <= 0) {
        return NULL;
    }
    wchar_t *wide = (wchar_t *)malloc((size_t)needed * sizeof(wchar_t));
    if (!wide) {
        return NULL;
    }
    if (MultiByteToWideChar(CP_UTF8, 0, text, -1, wide, needed) <= 0) {
        free(wide);
        return NULL;
    }
    return wide;
}

static char *
wide_to_utf8(const wchar_t *text)
{
    if (!text) {
        return NULL;
    }
    int needed = WideCharToMultiByte(CP_UTF8, 0, text, -1, NULL, 0, NULL, NULL);
    if (needed <= 0) {
        return NULL;
    }
    char *narrow = (char *)malloc((size_t)needed);
    if (!narrow) {
        return NULL;
    }
    if (WideCharToMultiByte(CP_UTF8, 0, text, -1, narrow, needed, NULL, NULL) <= 0) {
        free(narrow);
        return NULL;
    }
    return narrow;
}

/* ------------------------------------------------------------------ */
/* JSON helpers                                                        */
/* ------------------------------------------------------------------ */

static char *
json_escape_string(const char *value)
{
    StrBuf buffer;
    sb_init(&buffer);
    for (const unsigned char *cursor = (const unsigned char *)value;
         cursor && *cursor != '\0'; cursor++) {
        switch (*cursor) {
        case '"':
            sb_append_str(&buffer, "\\\"");
            break;
        case '\\':
            sb_append_str(&buffer, "\\\\");
            break;
        case '\b':
            sb_append_str(&buffer, "\\b");
            break;
        case '\f':
            sb_append_str(&buffer, "\\f");
            break;
        case '\n':
            sb_append_str(&buffer, "\\n");
            break;
        case '\r':
            sb_append_str(&buffer, "\\r");
            break;
        case '\t':
            sb_append_str(&buffer, "\\t");
            break;
        default:
            if (*cursor < 0x20) {
                sb_appendf(&buffer, "\\u%04x", *cursor);
            } else {
                sb_append_c(&buffer, (char)*cursor);
            }
            break;
        }
    }
    return sb_take(&buffer);
}

static int
hex_value(unsigned char character)
{
    if (character >= '0' && character <= '9') {
        return character - '0';
    }
    if (character >= 'a' && character <= 'f') {
        return character - 'a' + 10;
    }
    if (character >= 'A' && character <= 'F') {
        return character - 'A' + 10;
    }
    return -1;
}

/*
 * Flat bridge message. The injected script posts a JSON object whose values
 * are all strings, so the host can parse it strictly without a general JSON
 * parser: anything nested, numeric, or unknown is rejected outright rather
 * than silently ignored. Every field is optional and defaults to NULL.
 */
typedef struct {
    char *id;
    char *method;
    char *kind;
    char *title;
    char *target;
    char *path;
    char *action;
} BridgeMessage;

static void
bridge_message_clear(BridgeMessage *message)
{
    free(message->id);
    free(message->method);
    free(message->kind);
    free(message->title);
    free(message->target);
    free(message->path);
    free(message->action);
    memset(message, 0, sizeof(*message));
}

static char **
bridge_message_slot(BridgeMessage *message, const char *key, size_t length)
{
    if (length == 2 && memcmp(key, "id", 2) == 0) {
        return &message->id;
    }
    if (length == 6 && memcmp(key, "method", 6) == 0) {
        return &message->method;
    }
    if (length == 4 && memcmp(key, "kind", 4) == 0) {
        return &message->kind;
    }
    if (length == 5 && memcmp(key, "title", 5) == 0) {
        return &message->title;
    }
    if (length == 6 && memcmp(key, "target", 6) == 0) {
        return &message->target;
    }
    if (length == 4 && memcmp(key, "path", 4) == 0) {
        return &message->path;
    }
    if (length == 6 && memcmp(key, "action", 6) == 0) {
        return &message->action;
    }
    return NULL;
}

static int
json_parse_flat_object(const char *json, BridgeMessage *message)
{
    bridge_message_clear(message);
    if (!json || json[0] != '{') {
        return 0;
    }
    size_t length = strlen(json);
    if (length > MAX_BRIDGE_MESSAGE_BYTES || json[length - 1] != '}') {
        return 0;
    }
    const char *cursor = json + 1;
    const char *end = json + length - 1;
    while (cursor < end && (*cursor == ' ' || *cursor == '\t')) {
        cursor++;
    }
    if (cursor == end) {
        return 1; /* empty object: valid, every field absent */
    }
    for (;;) {
        if (cursor >= end || *cursor != '"') {
            goto invalid;
        }
        cursor++;
        const char *key = cursor;
        while (cursor < end && *cursor != '"') {
            if ((unsigned char)*cursor < 0x20 || *cursor == '\\') {
                goto invalid;
            }
            cursor++;
        }
        if (cursor >= end) {
            goto invalid;
        }
        size_t key_length = (size_t)(cursor - key);
        cursor++; /* closing quote */
        char **slot = bridge_message_slot(message, key, key_length);
        if (!slot) {
            goto invalid;
        }
        while (cursor < end && (*cursor == ' ' || *cursor == '\t')) {
            cursor++;
        }
        if (cursor >= end || *cursor != ':') {
            goto invalid;
        }
        cursor++;
        while (cursor < end && (*cursor == ' ' || *cursor == '\t')) {
            cursor++;
        }
        if (cursor >= end || *cursor != '"') {
            goto invalid;
        }
        cursor++;
        StrBuf value;
        sb_init(&value);
        while (cursor < end && *cursor != '"') {
            unsigned char character = (unsigned char)*cursor;
            if (character < 0x20) {
                sb_free(&value);
                goto invalid;
            }
            if (character != '\\') {
                if (!sb_append_c(&value, (char)character)) {
                    sb_free(&value);
                    goto invalid;
                }
                cursor++;
                continue;
            }
            cursor++;
            if (cursor >= end) {
                sb_free(&value);
                goto invalid;
            }
            switch (*cursor) {
            case '"': sb_append_c(&value, '"'); break;
            case '\\': sb_append_c(&value, '\\'); break;
            case '/': sb_append_c(&value, '/'); break;
            case 'b': sb_append_c(&value, '\b'); break;
            case 'f': sb_append_c(&value, '\f'); break;
            case 'n': sb_append_c(&value, '\n'); break;
            case 'r': sb_append_c(&value, '\r'); break;
            case 't': sb_append_c(&value, '\t'); break;
            case 'u': {
                if (cursor + 4 >= end) {
                    sb_free(&value);
                    goto invalid;
                }
                int code = 0;
                for (int index = 1; index <= 4; index++) {
                    int digit = hex_value((unsigned char)cursor[index]);
                    if (digit < 0) {
                        sb_free(&value);
                        goto invalid;
                    }
                    code = (code << 4) | digit;
                }
                /* Only ASCII escapes are meaningful for the bridge fields. */
                if (code > 0x7F) {
                    sb_free(&value);
                    goto invalid;
                }
                sb_append_c(&value, (char)code);
                cursor += 4;
                break;
            }
            default:
                sb_free(&value);
                goto invalid;
            }
            cursor++;
        }
        if (cursor >= end || *cursor != '"') {
            sb_free(&value);
            goto invalid;
        }
        cursor++;
        if (value.length > MAX_NATIVE_VALUE_BYTES) {
            sb_free(&value);
            goto invalid;
        }
        free(*slot);
        *slot = sb_take(&value);

        while (cursor < end && (*cursor == ' ' || *cursor == '\t')) {
            cursor++;
        }
        if (cursor == end) {
            return 1;
        }
        if (*cursor != ',') {
            goto invalid;
        }
        cursor++;
        while (cursor < end && (*cursor == ' ' || *cursor == '\t')) {
            cursor++;
        }
    }

invalid:
    bridge_message_clear(message);
    return 0;
}

/* ------------------------------------------------------------------ */
/* URI helpers                                                         */
/* ------------------------------------------------------------------ */

static int
native_text_is_valid(const char *value, size_t length, int allow_empty)
{
    if (!value || (!allow_empty && length == 0) || length > MAX_NATIVE_VALUE_BYTES) {
        return 0;
    }
    if (!utf8_is_valid(value, length)) {
        return 0;
    }
    for (size_t index = 0; index < length; index++) {
        unsigned char character = (unsigned char)value[index];
        if (character == '\0' || character < 0x20 || character == 0x7F) {
            return 0;
        }
    }
    return 1;
}

static int
native_text_string_is_valid(const char *value, int allow_empty)
{
    return value && native_text_is_valid(value, strlen(value), allow_empty);
}

static char *
native_uri_unescape(const char *value)
{
    if (!native_text_string_is_valid(value, 1)) {
        return NULL;
    }
    StrBuf decoded;
    sb_init(&decoded);
    for (size_t index = 0; value[index] != '\0'; index++) {
        if (value[index] == '%') {
            if (value[index + 1] == '\0' || value[index + 2] == '\0') {
                sb_free(&decoded);
                return NULL;
            }
            int high = hex_value((unsigned char)value[index + 1]);
            int low = hex_value((unsigned char)value[index + 2]);
            if (high < 0 || low < 0) {
                sb_free(&decoded);
                return NULL;
            }
            sb_append_c(&decoded, (char)((high << 4) | low));
            index += 2;
        } else {
            sb_append_c(&decoded, value[index]);
        }
        if (decoded.length > MAX_NATIVE_VALUE_BYTES) {
            sb_free(&decoded);
            return NULL;
        }
    }
    char *result = sb_take(&decoded);
    if (!native_text_is_valid(result, strlen(result), 1)) {
        free(result);
        return NULL;
    }
    return result;
}

static int
uri_char_is_unreserved(unsigned char character)
{
    return (character >= 'A' && character <= 'Z') ||
           (character >= 'a' && character <= 'z') ||
           (character >= '0' && character <= '9') ||
           character == '-' || character == '.' || character == '_' || character == '~';
}

static char *
native_uri_escape(const char *value)
{
    StrBuf escaped;
    sb_init(&escaped);
    for (const unsigned char *cursor = (const unsigned char *)value;
         cursor && *cursor != '\0'; cursor++) {
        if (uri_char_is_unreserved(*cursor)) {
            sb_append_c(&escaped, (char)*cursor);
        } else {
            sb_appendf(&escaped, "%%%02X", *cursor);
        }
    }
    return sb_take(&escaped);
}

/* ------------------------------------------------------------------ */
/* Native requests (argv, openbox:// URIs, IPC)                        */
/* ------------------------------------------------------------------ */

static void
native_request_clear(NativeRequest *request)
{
    if (!request) {
        return;
    }
    free(request->value);
    request->value = NULL;
    request->kind = NATIVE_REQUEST_NONE;
}

static int
native_request_copy(NativeRequest *destination, const NativeRequest *source)
{
    if (!destination || !source) {
        return 0;
    }
    native_request_clear(destination);
    destination->kind = source->kind;
    destination->value = source->value ? dup_str(source->value) : NULL;
    return !source->value || destination->value != NULL;
}

static int
native_request_set_value(NativeRequest *request, NativeRequestKind kind,
                         char *value, int allow_empty)
{
    if (!request || !value) {
        free(value);
        return 0;
    }
    if (!native_text_string_is_valid(value, allow_empty)) {
        free(value);
        return 0;
    }
    native_request_clear(request);
    request->kind = kind;
    request->value = value;
    return 1;
}

static int
native_request_set_uri_value(NativeRequest *request, NativeRequestKind kind,
                             const char *raw_value, int trim, int allow_empty)
{
    char *value = native_uri_unescape(raw_value ? raw_value : "");
    if (!value) {
        return 0;
    }
    if (trim) {
        trim_in_place(value);
    }
    return native_request_set_value(request, kind, value, allow_empty);
}

static int
text_starts_with(const char *text, const char *prefix)
{
    return strncmp(text, prefix, strlen(prefix)) == 0;
}

static int
text_starts_with_ci(const char *text, const char *prefix)
{
    return _strnicmp(text, prefix, strlen(prefix)) == 0;
}

static int
native_request_from_uri(const char *uri, NativeRequest *request)
{
    NativeRequest parsed = { NATIVE_REQUEST_NONE, NULL };
    char *text = uri ? dup_str(uri) : NULL;
    if (!text) {
        return 0;
    }
    trim_in_place(text);
    if (!native_text_string_is_valid(text, 0)) {
        free(text);
        return 0;
    }

    const char *route = NULL;
    if (text_starts_with(text, "openbox://")) {
        const char *rest = text + strlen("openbox://");
        const char *slash = strchr(rest, '/');
        if (slash) {
            char *authority = dup_strn(rest, (size_t)(slash - rest));
            int local_authority = authority[0] == '\0' ||
                                  _stricmp(authority, "localhost") == 0 ||
                                  _stricmp(authority, "openbox") == 0;
            int foreign_authority = strchr(authority, '.') != NULL ||
                                    strchr(authority, ':') != NULL;
            if (!local_authority && foreign_authority) {
                free(authority);
                free(text);
                return 0;
            }
            route = local_authority ? slash + 1 : rest;
            free(authority);
        } else {
            int local_authority = rest[0] == '\0' ||
                                  _stricmp(rest, "localhost") == 0 ||
                                  _stricmp(rest, "openbox") == 0;
            if ((!local_authority && strchr(rest, '.') != NULL) ||
                (!local_authority && strchr(rest, ':') != NULL)) {
                free(text);
                return 0;
            }
            route = local_authority ? "" : rest;
        }
    } else if (text_starts_with(text, "openbox:")) {
        route = text + strlen("openbox:");
    } else {
        free(text);
        return 0;
    }

    while (route[0] == '/') {
        route++;
    }
    char *route_copy = dup_str(route);
    char *separator = strchr(route_copy, '/');
    char *remainder = (char *)"";
    if (separator) {
        *separator = '\0';
        remainder = separator + 1;
    }

    if (_stricmp(route_copy, "start") == 0 || route_copy[0] == '\0') {
        parsed.kind = NATIVE_REQUEST_START;
    } else if (_stricmp(route_copy, "showgame") == 0 ||
               _stricmp(route_copy, "game") == 0) {
        if (!native_request_set_uri_value(&parsed, NATIVE_REQUEST_SHOWGAME,
                                          remainder, 1, 0)) {
            goto invalid;
        }
    } else if (_stricmp(route_copy, "search") == 0) {
        if (!native_request_set_uri_value(&parsed, NATIVE_REQUEST_SEARCH,
                                          remainder, 0, 1)) {
            goto invalid;
        }
    } else if (_stricmp(route_copy, "launch") == 0) {
        if (!native_request_set_uri_value(&parsed, NATIVE_REQUEST_LAUNCH,
                                          remainder, 1, 0)) {
            goto invalid;
        }
    } else if (_stricmp(route_copy, "resume") == 0) {
        if (!native_request_set_uri_value(&parsed, NATIVE_REQUEST_RESUME,
                                          remainder, 1, 0)) {
            goto invalid;
        }
    } else if (_stricmp(route_copy, "moment") == 0) {
        if (!native_request_set_uri_value(&parsed, NATIVE_REQUEST_MOMENT,
                                          remainder, 1, 0)) {
            goto invalid;
        }
    } else if (_stricmp(route_copy, "clip") == 0) {
        if (!native_request_set_uri_value(&parsed, NATIVE_REQUEST_CLIP,
                                          remainder, 1, 0)) {
            goto invalid;
        }
    } else if (_stricmp(route_copy, "bigbox") == 0 ||
               _stricmp(route_copy, "fullscreen") == 0) {
        parsed.kind = NATIVE_REQUEST_BIGBOX;
    } else if (_stricmp(route_copy, "settings") == 0) {
        parsed.kind = NATIVE_REQUEST_SETTINGS;
    } else {
        goto invalid;
    }

    native_request_clear(request);
    *request = parsed;
    free(route_copy);
    free(text);
    return 1;

invalid:
    native_request_clear(&parsed);
    free(route_copy);
    free(text);
    return 0;
}

static int
native_request_set_play(NativeRequest *request, const char *value)
{
    char *game_id = value ? dup_str(value) : NULL;
    if (!game_id) {
        return 0;
    }
    trim_in_place(game_id);
    return native_request_set_value(request, NATIVE_REQUEST_LAUNCH, game_id, 0);
}

static NativeArgsResult
native_request_from_argv(int argc, char **argv, NativeRequest *request)
{
    native_request_clear(request);
    for (int index = 1; index < argc; index++) {
        if (strcmp(argv[index], "--uri") == 0) {
            if (index + 1 >= argc ||
                !native_request_from_uri(argv[index + 1], request)) {
                return NATIVE_ARGS_INVALID;
            }
            return NATIVE_ARGS_REQUEST;
        }
    }
    for (int index = 1; index < argc; index++) {
        if (strcmp(argv[index], "--play") == 0) {
            if (index + 1 >= argc ||
                !native_request_set_play(request, argv[index + 1])) {
                return NATIVE_ARGS_INVALID;
            }
            return NATIVE_ARGS_REQUEST;
        }
    }
    for (int index = 1; index < argc; index++) {
        if (text_starts_with(argv[index], "openbox:")) {
            if (!native_request_from_uri(argv[index], request)) {
                return NATIVE_ARGS_INVALID;
            }
            return NATIVE_ARGS_REQUEST;
        }
    }
    return NATIVE_ARGS_NONE;
}

static const char *
native_request_action(NativeRequestKind kind)
{
    switch (kind) {
    case NATIVE_REQUEST_SHOWGAME:
    case NATIVE_REQUEST_LAUNCH:
        return "showgame";
    case NATIVE_REQUEST_SEARCH:
        return "search";
    case NATIVE_REQUEST_BIGBOX:
        return "bigbox";
    case NATIVE_REQUEST_SETTINGS:
        return "settings";
    case NATIVE_REQUEST_MOMENT:
        return "moment";
    case NATIVE_REQUEST_CLIP:
        return "clip";
    case NATIVE_REQUEST_RESUME:
        return "resume";
    default:
        return NULL;
    }
}

static char *
native_request_to_uri(const NativeRequest *request)
{
    if (!request || request->kind == NATIVE_REQUEST_NONE) {
        return NULL;
    }
    if (request->kind == NATIVE_REQUEST_START) {
        return dup_str("openbox://start");
    }
    const char *action = native_request_action(request->kind);
    if (!action) {
        return NULL;
    }
    char *escaped = request->value ? native_uri_escape(request->value) : dup_str("");
    if (!escaped) {
        return NULL;
    }
    StrBuf buffer;
    sb_init(&buffer);
    sb_appendf(&buffer, "openbox://%s/%s", action, escaped);
    free(escaped);
    return sb_take(&buffer);
}

static char *
native_authenticated_url(const NativeRequest *request)
{
    if (!g_origin || !g_token) {
        return NULL;
    }
    StrBuf buffer;
    sb_init(&buffer);
    sb_appendf(&buffer, "%s/?token=%s", g_origin, g_token);
    if (!request || request->kind == NATIVE_REQUEST_NONE ||
        request->kind == NATIVE_REQUEST_START) {
        return sb_take(&buffer);
    }

    const char *action = native_request_action(request->kind);
    char *escaped = request->value ? native_uri_escape(request->value) : dup_str("");
    if (!action || !escaped) {
        free(escaped);
        sb_free(&buffer);
        return NULL;
    }
    const char *parameter = strcmp(action, "search") == 0 ? "q" : "id";
    if (request->kind == NATIVE_REQUEST_BIGBOX ||
        request->kind == NATIVE_REQUEST_SETTINGS) {
        sb_appendf(&buffer, "&deeplink=%s", action);
    } else {
        sb_appendf(&buffer, "&deeplink=%s&%s=%s", action, parameter, escaped);
    }
    free(escaped);
    return sb_take(&buffer);
}

/* ------------------------------------------------------------------ */
/* Filesystem and data dir                                             */
/* ------------------------------------------------------------------ */

static int
ensure_directory(const wchar_t *path)
{
    size_t length = wcslen(path);
    if (length == 0 || length >= MAX_PATH * 2) {
        return 0;
    }
    wchar_t *buffer = (wchar_t *)malloc((length + 1) * sizeof(wchar_t));
    if (!buffer) {
        return 0;
    }
    wcscpy(buffer, path);
    for (size_t index = 0; index < length; index++) {
        if (buffer[index] == L'\\' || buffer[index] == L'/') {
            wchar_t saved = buffer[index];
            buffer[index] = L'\0';
            if (buffer[0] != L'\0' && buffer[1] != L':' ) {
                CreateDirectoryW(buffer, NULL);
            }
            buffer[index] = saved;
        }
    }
    BOOL created = CreateDirectoryW(buffer, NULL);
    DWORD error = GetLastError();
    free(buffer);
    return created || error == ERROR_ALREADY_EXISTS;
}

static wchar_t *
path_join(const wchar_t *base, const wchar_t *name)
{
    size_t base_length = wcslen(base);
    size_t name_length = wcslen(name);
    wchar_t *joined = (wchar_t *)malloc((base_length + name_length + 2) * sizeof(wchar_t));
    if (!joined) {
        return NULL;
    }
    wcscpy(joined, base);
    size_t index = base_length;
    if (index > 0 && joined[index - 1] != L'\\' && joined[index - 1] != L'/') {
        joined[index++] = L'\\';
    }
    wcscpy(joined + index, name);
    return joined;
}

static wchar_t *
resolve_default_data_dir(void)
{
    static const wchar_t *const variables[] = { L"LOCALAPPDATA", L"APPDATA", NULL };
    wchar_t base[MAX_PATH * 2];
    for (size_t index = 0; variables[index]; index++) {
        if (GetEnvironmentVariableW(variables[index], base, MAX_PATH * 2) > 0) {
            return path_join(base, DEFAULT_DATA_DIR_NAME);
        }
    }
    wchar_t profile[MAX_PATH * 2];
    if (GetEnvironmentVariableW(L"USERPROFILE", profile, MAX_PATH * 2) > 0) {
        wchar_t *local = path_join(profile, L"AppData");
        if (!local) {
            return NULL;
        }
        wchar_t *local_appdata = path_join(local, L"Local");
        free(local);
        if (!local_appdata) {
            return NULL;
        }
        wchar_t *result = path_join(local_appdata, DEFAULT_DATA_DIR_NAME);
        free(local_appdata);
        return result;
    }
    return NULL;
}

static int
path_exists(const wchar_t *path)
{
    if (!path) {
        return 0;
    }
    DWORD attributes = GetFileAttributesW(path);
    return attributes != INVALID_FILE_ATTRIBUTES;
}

/*
 * Boot files carry the loopback port and the API token, so they must not be
 * attacker-substitutable: reject anything that is not a small regular file
 * under the per-user data directory, and refuse reparse points (the Windows
 * equivalent of O_NOFOLLOW).
 */
static char *
read_secure_trimmed_file(const wchar_t *path)
{
    HANDLE handle = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL,
                                OPEN_EXISTING,
                                FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
                                NULL);
    if (handle == INVALID_HANDLE_VALUE) {
        log_line("native_host: could not open %ls (error %lu)\n", path, GetLastError());
        return NULL;
    }
    BY_HANDLE_FILE_INFORMATION info;
    if (!GetFileInformationByHandle(handle, &info) ||
        (info.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) ||
        info.nFileSizeLow == 0 || info.nFileSizeLow > MAX_NATIVE_VALUE_BYTES) {
        log_line("native_host: rejecting insecure server file %ls\n", path);
        CloseHandle(handle);
        return NULL;
    }
    size_t size = (size_t)info.nFileSizeLow;
    char *contents = (char *)malloc(size + 1);
    if (!contents) {
        CloseHandle(handle);
        return NULL;
    }
    size_t used = 0;
    while (used < size) {
        DWORD chunk = 0;
        DWORD want = (DWORD)(size - used);
        if (!ReadFile(handle, contents + used, want, &chunk, NULL) || chunk == 0) {
            log_line("native_host: could not read %ls\n", path);
            free(contents);
            CloseHandle(handle);
            return NULL;
        }
        used += chunk;
    }
    CloseHandle(handle);
    contents[used] = '\0';
    trim_in_place(contents);
    return contents;
}

static int
secure_file_ready(const wchar_t *path)
{
    WIN32_FILE_ATTRIBUTE_DATA info;
    if (!GetFileAttributesExW(path, GetFileExInfoStandard, &info)) {
        return 0;
    }
    if ((info.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) ||
        info.nFileSizeHigh != 0 || info.nFileSizeLow == 0 ||
        info.nFileSizeLow > MAX_NATIVE_VALUE_BYTES) {
        return 0;
    }
    return 1;
}

static void
remove_boot_file(const wchar_t *path)
{
    if (!DeleteFileW(path)) {
        DWORD error = GetLastError();
        if (error != ERROR_FILE_NOT_FOUND && error != ERROR_PATH_NOT_FOUND) {
            log_line("native_host: could not remove stale %ls (error %lu)\n", path, error);
        }
    }
}

static int
parse_server_port(const char *text, unsigned short *parsed_port)
{
    if (!text || !text[0]) {
        return 0;
    }
    char *end = NULL;
    long value = strtol(text, &end, 10);
    if (end == text || *end != '\0' || value < 1 || value > 65535) {
        return 0;
    }
    *parsed_port = (unsigned short)value;
    return 1;
}

static int
token_is_valid(const char *value)
{
    if (!value) {
        return 0;
    }
    size_t length = strlen(value);
    if (length < 16 || length > 256) {
        return 0;
    }
    for (const unsigned char *cursor = (const unsigned char *)value;
         *cursor != '\0'; cursor++) {
        if (!isalnum(*cursor) && *cursor != '-' && *cursor != '_') {
            return 0;
        }
    }
    return 1;
}

/* ------------------------------------------------------------------ */
/* Server process ownership                                            */
/* ------------------------------------------------------------------ */

static int
append_quoted_arg(StrBuf *buffer, const wchar_t *argument)
{
    if (buffer->length > 0) {
        sb_append_c(buffer, ' ');
    }
    sb_append_c(buffer, '"');
    size_t backslashes = 0;
    for (const wchar_t *cursor = argument; *cursor; cursor++) {
        if (*cursor == L'\\') {
            backslashes++;
            continue;
        }
        if (*cursor == L'"') {
            for (size_t index = 0; index < backslashes * 2 + 1; index++) {
                sb_append_c(buffer, '\\');
            }
            sb_append_c(buffer, '"');
            backslashes = 0;
            continue;
        }
        for (size_t index = 0; index < backslashes; index++) {
            sb_append_c(buffer, '\\');
        }
        backslashes = 0;
        char narrow[8];
        int written = WideCharToMultiByte(CP_UTF8, 0, cursor, 1, narrow, sizeof(narrow), NULL, NULL);
        if (written > 0) {
            sb_append(buffer, narrow, (size_t)written);
        }
    }
    for (size_t index = 0; index < backslashes * 2; index++) {
        sb_append_c(buffer, '\\');
    }
    sb_append_c(buffer, '"');
    return 1;
}

static int
boot_server(void)
{
    wchar_t *port_file = path_join(g_data_dir_wide, L"server.port");
    wchar_t *token_file = path_join(g_data_dir_wide, L"server.token");
    if (!port_file || !token_file) {
        free(port_file);
        free(token_file);
        return 0;
    }

    /* Never accept a pair left behind by a crashed or interrupted server. */
    remove_boot_file(port_file);
    remove_boot_file(token_file);

    StrBuf command;
    sb_init(&command);
    append_quoted_arg(&command, g_python_path);
    sb_append_str(&command, " -B ");
    append_quoted_arg(&command, g_web_app_path);
    sb_append_str(&command, " --no-browser");
    char *command_utf8 = sb_take(&command);
    wchar_t *command_line = utf8_to_wide(command_utf8);
    free(command_utf8);
    if (!command_line) {
        free(port_file);
        free(token_file);
        return 0;
    }

    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    memset(&startup, 0, sizeof(startup));
    memset(&process, 0, sizeof(process));
    startup.cb = sizeof(startup);

    /*
     * CREATE_NEW_PROCESS_GROUP gives the child its own console control group
     * (the Windows analogue of setpgid) so shutdown can raise CTRL_BREAK
     * there. CREATE_NO_WINDOW keeps that console invisible. CREATE_SUSPENDED
     * closes the race where the child could spawn grandchildren before the
     * job object owns it.
     */
    DWORD flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | CREATE_SUSPENDED;
    if (!CreateProcessW(NULL, command_line, NULL, NULL, FALSE, flags, NULL,
                        g_data_dir_wide, &startup, &process)) {
        log_line("native_host: could not start the server (error %lu)\n", GetLastError());
        free(command_line);
        free(port_file);
        free(token_file);
        return 0;
    }
    free(command_line);

    g_server_process = process.hProcess;
    g_server_pid = process.dwProcessId;

    g_server_job = CreateJobObjectW(NULL, NULL);
    if (g_server_job) {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits;
        memset(&limits, 0, sizeof(limits));
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if (!SetInformationJobObject(g_server_job, JobObjectExtendedLimitInformation,
                                     &limits, sizeof(limits))) {
            log_line("native_host: could not configure the server job (error %lu)\n",
                     GetLastError());
        }
        if (!AssignProcessToJobObject(g_server_job, g_server_process)) {
            log_line("native_host: could not assign the server job (error %lu)\n",
                     GetLastError());
        }
    }

    /*
     * The child was created suspended so the job object owns it before any
     * grandchild can spawn. Resume it now, then drop the thread handle.
     */
    ResumeThread(process.hThread);
    CloseHandle(process.hThread);

    /*
     * Wait for the server to publish its port and token. The wait is on the
     * child process handle, so a crashed server wakes it immediately instead
     * of burning the whole timeout.
     */
    ULONGLONG deadline = GetTickCount64() + (ULONGLONG)BOOT_TIMEOUT_SECONDS * 1000;
    int ready = 0;
    while (GetTickCount64() < deadline) {
        if (secure_file_ready(port_file) && secure_file_ready(token_file)) {
            ready = 1;
            break;
        }
        if (WaitForSingleObject(g_server_process, 20) == WAIT_OBJECT_0) {
            break;
        }
    }

    if (!ready) {
        log_line("native_host: server did not boot within %d seconds\n", BOOT_TIMEOUT_SECONDS);
        free(port_file);
        free(token_file);
        return 0;
    }

    char *port = read_secure_trimmed_file(port_file);
    g_token = read_secure_trimmed_file(token_file);
    free(port_file);
    free(token_file);

    if (!port || !g_token || !parse_server_port(port, &g_port) || !token_is_valid(g_token)) {
        log_line("native_host: could not read server.port or server.token\n");
        free(port);
        free(g_token);
        g_token = NULL;
        return 0;
    }
    free(port);

    StrBuf origin;
    sb_init(&origin);
    sb_appendf(&origin, "http://127.0.0.1:%u", g_port);
    g_origin = sb_take(&origin);
    g_origin_wide = utf8_to_wide(g_origin);
    return g_origin_wide != NULL;
}

static int
write_all_socket(SOCKET socket_handle, const char *data, size_t length)
{
    size_t written = 0;
    while (written < length) {
        int count = send(socket_handle, data + written, (int)(length - written), 0);
        if (count == SOCKET_ERROR) {
            return 0;
        }
        written += (size_t)count;
    }
    return 1;
}

static int
api_post_json(const char *path, const char *body)
{
    if (!path || !body || !g_token || g_port == 0) {
        return 0;
    }
    SOCKET socket_handle = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (socket_handle == INVALID_SOCKET) {
        log_line("native_host: could not create loopback API socket (error %d)\n",
                 WSAGetLastError());
        return 0;
    }
    DWORD timeout = API_TIMEOUT_SECONDS * 1000;
    setsockopt(socket_handle, SOL_SOCKET, SO_SNDTIMEO, (const char *)&timeout, sizeof(timeout));
    setsockopt(socket_handle, SOL_SOCKET, SO_RCVTIMEO, (const char *)&timeout, sizeof(timeout));

    struct sockaddr_in address;
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_port = htons(g_port);
    inet_pton(AF_INET, "127.0.0.1", &address.sin_addr);
    if (connect(socket_handle, (struct sockaddr *)&address, sizeof(address)) == SOCKET_ERROR) {
        log_line("native_host: could not connect to the loopback API (error %d)\n",
                 WSAGetLastError());
        closesocket(socket_handle);
        return 0;
    }

    StrBuf request;
    sb_init(&request);
    sb_appendf(&request,
               "POST %s HTTP/1.1\r\n"
               "Host: 127.0.0.1:%u\r\n"
               "X-OpenBox-Token: %s\r\n"
               "Content-Type: application/json\r\n"
               "Content-Length: %zu\r\n"
               "Connection: close\r\n\r\n%s",
               path, g_port, g_token, strlen(body), body);
    char *request_text = sb_take(&request);
    int sent = write_all_socket(socket_handle, request_text, strlen(request_text));
    free(request_text);
    if (!sent) {
        log_line("native_host: could not send loopback API request (error %d)\n",
                 WSAGetLastError());
        closesocket(socket_handle);
        return 0;
    }

    char response[128];
    size_t used = 0;
    while (used + 1 < sizeof(response)) {
        int count = recv(socket_handle, response + used, (int)(sizeof(response) - used - 1), 0);
        if (count <= 0) {
            break;
        }
        used += (size_t)count;
        if (memchr(response, '\n', used) != NULL) {
            break;
        }
    }
    response[used] = '\0';
    int status = 0;
    int ok = sscanf(response, "HTTP/%*s %d", &status) == 1 && status >= 200 && status < 300;
    if (!ok) {
        log_line("native_host: loopback API request failed (HTTP %d)\n", status);
    }
    closesocket(socket_handle);
    return ok;
}

static int
native_request_is_decimal(const char *value)
{
    if (!value || !value[0]) {
        return 0;
    }
    for (const unsigned char *cursor = (const unsigned char *)value;
         *cursor != '\0'; cursor++) {
        if (*cursor < '0' || *cursor > '9') {
            return 0;
        }
    }
    return 1;
}

static char *
native_request_api_body(const char *value)
{
    if (native_request_is_decimal(value)) {
        const char *first_nonzero = value;
        while (first_nonzero[0] == '0' && first_nonzero[1] != '\0') {
            first_nonzero++;
        }
        StrBuf buffer;
        sb_init(&buffer);
        sb_appendf(&buffer, "{\"id\":%s}", first_nonzero);
        return sb_take(&buffer);
    }
    char *escaped = json_escape_string(value);
    StrBuf buffer;
    sb_init(&buffer);
    sb_appendf(&buffer, "{\"game_id\":\"%s\"}", escaped ? escaped : "");
    free(escaped);
    return sb_take(&buffer);
}

static int
dispatch_native_api_request(const NativeRequest *request)
{
    if (!request || !request->value) {
        return 0;
    }
    const char *path = request->kind == NATIVE_REQUEST_RESUME
        ? "/api/v2/resume"
        : "/api/launch";
    char *body = native_request_api_body(request->value);
    int ok = api_post_json(path, body);
    free(body);
    return ok;
}

/*
 * Ask the server to stop. /api/shutdown stops running game sessions through
 * the same code path the web UI uses; CTRL_BREAK_EVENT then raises
 * KeyboardInterrupt in the child's main thread so web_app.py's finally block
 * runs its full teardown (web_app.py registers only SIGTERM and SIGINT
 * handlers, both raising KeyboardInterrupt; SIGBREAK is left at CPython's
 * default disposition, which raises KeyboardInterrupt on Windows). The job
 * object is the backstop.
 */
static void
stop_server(void)
{
    if (!g_server_process) {
        return;
    }
    api_post_json("/api/shutdown", "{\"force\":false}");

    int signalled = 0;
    if (AttachConsole(g_server_pid)) {
        SetConsoleCtrlHandler(NULL, TRUE); /* do not let CTRL_BREAK hit us */
        signalled = GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, g_server_pid) != 0;
        FreeConsole();
        if (!signalled) {
            log_line("native_host: could not signal the server (error %lu)\n", GetLastError());
        }
    }

    ULONGLONG deadline = GetTickCount64() + (ULONGLONG)SHUTDOWN_GRACE_SECONDS * 1000;
    while (GetTickCount64() < deadline) {
        if (WaitForSingleObject(g_server_process, 50) == WAIT_OBJECT_0) {
            break;
        }
    }

    if (WaitForSingleObject(g_server_process, 0) != WAIT_OBJECT_0) {
        log_line("native_host: server still alive; terminating the process tree\n");
        if (g_server_job) {
            TerminateJobObject(g_server_job, 0);
        }
        WaitForSingleObject(g_server_process, 5000);
    }

    CloseHandle(g_server_process);
    g_server_process = NULL;
    g_server_pid = 0;
    if (g_server_job) {
        CloseHandle(g_server_job);
        g_server_job = NULL;
    }
    if (g_data_dir_wide) {
        wchar_t *port_file = path_join(g_data_dir_wide, L"server.port");
        wchar_t *token_file = path_join(g_data_dir_wide, L"server.token");
        if (port_file) {
            DeleteFileW(port_file);
            free(port_file);
        }
        if (token_file) {
            DeleteFileW(token_file);
            free(token_file);
        }
    }
}

/* ------------------------------------------------------------------ */
/* Geometry and tray flags                                             */
/* ------------------------------------------------------------------ */

static void
load_geometry(void)
{
    char *contents = read_secure_trimmed_file(g_geometry_path ? utf8_to_wide(g_geometry_path) : NULL);
    if (contents) {
        int width = 0, height = 0, maximized = 0;
        if (sscanf(contents, "%d %d %d", &width, &height, &maximized) == 3) {
            if (width >= 400 && width <= 8000 && height >= 300 && height <= 8000) {
                g_default_width = width;
                g_default_height = height;
            }
            g_window_maximized = maximized != 0;
        }
        free(contents);
    }
}

static void
save_geometry(void)
{
    if (!g_geometry_path || !g_window) {
        return;
    }
    if (IsZoomed(g_window)) {
        g_window_maximized = 1;
    }
    RECT rect;
    if (!GetWindowRect(g_window, &rect)) {
        return;
    }
    int width = rect.right - rect.left;
    int height = rect.bottom - rect.top;
    if (width < 400 || height < 300) {
        return;
    }
    char contents[64];
    snprintf(contents, sizeof(contents), "%d %d %d\n", width, height, g_window_maximized ? 1 : 0);
    wchar_t *wide_path = utf8_to_wide(g_geometry_path);
    if (!wide_path) {
        return;
    }
    HANDLE handle = CreateFileW(wide_path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                                FILE_ATTRIBUTE_NORMAL, NULL);
    free(wide_path);
    if (handle == INVALID_HANDLE_VALUE) {
        return;
    }
    DWORD written = 0;
    WriteFile(handle, contents, (DWORD)strlen(contents), &written, NULL);
    CloseHandle(handle);
}

static void
load_tray_flags(void)
{
    /* web_app.py writes "tray_enabled minimize_to_tray" at boot, owner-only. */
    wchar_t *flags_path = path_join(g_data_dir_wide, L"native-host-flags");
    if (!flags_path) {
        return;
    }
    char *contents = read_secure_trimmed_file(flags_path);
    free(flags_path);
    if (contents) {
        int enabled = 0, minimize = 0;
        if (sscanf(contents, "%d %d", &enabled, &minimize) == 2) {
            g_tray_enabled = enabled != 0;
            g_minimize_to_tray = minimize != 0;
        }
        free(contents);
    }
}

/* ------------------------------------------------------------------ */
/* Single instance (named pipe)                                        */
/* ------------------------------------------------------------------ */

static int
write_all_handle(HANDLE handle, const char *data, size_t length)
{
    size_t written = 0;
    while (written < length) {
        DWORD chunk = 0;
        if (!WriteFile(handle, data + written, (DWORD)(length - written), &chunk, NULL) ||
            chunk == 0) {
            return 0;
        }
        written += chunk;
    }
    return 1;
}

static int
send_single_instance_request(HANDLE handle, const NativeRequest *request)
{
    if (!request || request->kind == NATIVE_REQUEST_NONE) {
        return write_all_handle(handle, "focus\n", strlen("focus\n"));
    }
    char *uri = native_request_to_uri(request);
    if (!uri) {
        return 0;
    }
    StrBuf message;
    sb_init(&message);
    sb_appendf(&message, "deeplink %s\n", uri);
    free(uri);
    char *text = sb_take(&message);
    int sent = write_all_handle(handle, text, strlen(text));
    free(text);
    return sent;
}

static void
dispatch_native_request(const NativeRequest *request);

static DWORD WINAPI
pipe_server_thread(LPVOID parameter)
{
    (void)parameter;
    for (;;) {
        /*
         * The first pass takes the instance that acquire_single_instance
         * already created: the guard holds the name, so creating another
         * first-instance pipe here would fail with ERROR_PIPE_BUSY. Later
         * passes create the next instance, since each accepted connection
         * disconnects and closes its handle.
         */
        HANDLE pipe = g_pipe;
        g_pipe = NULL;
        if (!pipe) {
            pipe = CreateNamedPipeW(
                g_pipe_name,
                PIPE_ACCESS_INBOUND,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1, MAX_IPC_MESSAGE_BYTES, MAX_IPC_MESSAGE_BYTES, 0, NULL);
        }
        if (pipe == INVALID_HANDLE_VALUE) {
            log_line("native_host: could not create the IPC pipe (error %lu)\n", GetLastError());
            return 0;
        }
        BOOL connected = ConnectNamedPipe(pipe, NULL) ||
                         GetLastError() == ERROR_PIPE_CONNECTED;
        if (!connected) {
            CloseHandle(pipe);
            continue;
        }
        char buffer[MAX_IPC_MESSAGE_BYTES];
        DWORD used = 0;
        int valid = 0;
        while (used < sizeof(buffer) - 1) {
            DWORD chunk = 0;
            if (!ReadFile(pipe, buffer + used, 1, &chunk, NULL) || chunk == 0) {
                break;
            }
            if (buffer[used] == '\n') {
                valid = 1;
                break;
            }
            unsigned char character = (unsigned char)buffer[used];
            if (character == '\0' || character < 0x20 || character == 0x7F) {
                break;
            }
            used++;
        }
        buffer[used] = '\0';
        DisconnectNamedPipe(pipe);
        CloseHandle(pipe);

        if (!valid) {
            log_line("native_host: rejected invalid single-instance message\n");
            continue;
        }
        if (strcmp(buffer, "focus") == 0) {
            PostMessageW(g_window, WM_APP_NATIVE_FOCUS, 0, 0);
        } else if (text_starts_with(buffer, "deeplink ")) {
            NativeRequest *request = (NativeRequest *)calloc(1, sizeof(NativeRequest));
            if (!request) {
                continue;
            }
            if (!native_request_from_uri(buffer + strlen("deeplink "), request)) {
                log_line("native_host: rejected invalid single-instance deeplink\n");
                free(request);
                continue;
            }
            if (!PostMessageW(g_window, WM_APP_NATIVE_REQUEST, 0, (LPARAM)request)) {
                native_request_clear(request);
                free(request);
            }
        } else {
            log_line("native_host: rejected invalid single-instance message\n");
        }
    }
    return 0;
}

static int
acquire_single_instance(const NativeRequest *request)
{
    /*
     * A first-instance-only pipe creation is the atomic test-and-set: if it
     * fails, another host already owns the name, so this process forwards its
     * request and exits.
     */
    HANDLE pipe = CreateNamedPipeW(
        g_pipe_name,
        PIPE_ACCESS_INBOUND | FILE_FLAG_FIRST_PIPE_INSTANCE,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
        1, MAX_IPC_MESSAGE_BYTES, MAX_IPC_MESSAGE_BYTES, 0, NULL);
    if (pipe == INVALID_HANDLE_VALUE) {
        DWORD error = GetLastError();
        if (error == ERROR_ACCESS_DENIED || error == ERROR_PIPE_BUSY ||
            error == ERROR_ALREADY_EXISTS) {
            HANDLE client = CreateFileW(g_pipe_name, GENERIC_WRITE, 0, NULL,
                                        OPEN_EXISTING, 0, NULL);
            if (client != INVALID_HANDLE_VALUE) {
                send_single_instance_request(client, request);
                FlushFileBuffers(client);
                CloseHandle(client);
            } else {
                log_line("native_host: could not reach the running instance (error %lu)\n",
                         GetLastError());
            }
            return 0;
        }
        log_line("native_host: could not create the IPC pipe (error %lu)\n", error);
        return 1;
    }
    g_pipe = pipe;
    g_pipe_thread = CreateThread(NULL, 0, pipe_server_thread, NULL, 0, NULL);
    if (!g_pipe_thread) {
        log_line("native_host: could not start the IPC thread (error %lu)\n", GetLastError());
    }
    return 1;
}

static void
release_single_instance(void)
{
    if (g_pipe) {
        /* Unblock ConnectNamedPipe in the listener thread, then let it exit. */
        CloseHandle(g_pipe);
        g_pipe = NULL;
    }
    if (g_pipe_thread) {
        WaitForSingleObject(g_pipe_thread, 2000);
        CloseHandle(g_pipe_thread);
        g_pipe_thread = NULL;
    }
    if (g_pipe_name) {
        /* The pipe name is kernel-scoped; there is no filesystem node to remove. */
        free(g_pipe_name);
        g_pipe_name = NULL;
    }
}

/* ------------------------------------------------------------------ */
/* Bridge results                                                      */
/* ------------------------------------------------------------------ */

static void
evaluate_script(const char *script)
{
    if (!g_webview || !script) {
        return;
    }
    wchar_t *wide = utf8_to_wide(script);
    if (!wide) {
        return;
    }
    ICoreWebView2_ExecuteScript(g_webview, wide, NULL);
    free(wide);
}

static char *
js_single_quote_escape(const char *value)
{
    StrBuf escaped;
    sb_init(&escaped);
    for (const unsigned char *cursor = (const unsigned char *)value;
         cursor && *cursor != '\0'; cursor++) {
        if (*cursor == '\'' || *cursor == '\\') {
            sb_append_c(&escaped, '\\');
        }
        sb_append_c(&escaped, (char)*cursor);
    }
    return sb_take(&escaped);
}

static void
resolve_bridge(const char *id, const char *result_json)
{
    /*
     * Invariant: result_json must be valid JS/JSON produced only by callers
     * in this file (literal objects or json_escape_string'd values), never raw
     * attacker data -- it is spliced verbatim into the page. Cheap defensive
     * guard: if a future caller ever threads untrusted text through here,
     * reject it instead of evaluating it.
     */
    if (!result_json || strstr(result_json, ";") || strstr(result_json, "//") ||
        strstr(result_json, "*/")) {
        log_line("native_host: resolve_bridge rejected suspicious result payload\n");
        return;
    }
    char *escaped_id = js_single_quote_escape(id ? id : "");
    StrBuf script;
    sb_init(&script);
    sb_appendf(&script, "window.__openboxResolve && window.__openboxResolve('%s', %s);",
               escaped_id, result_json);
    free(escaped_id);
    char *text = sb_take(&script);
    evaluate_script(text);
    free(text);
}

/* ------------------------------------------------------------------ */
/* Origin and navigation policy                                        */
/* ------------------------------------------------------------------ */

static int
origin_matches(const char *uri)
{
    if (!uri || !g_origin) {
        return 0;
    }
    if (!text_starts_with_ci(uri, "http://")) {
        return 0;
    }
    const char *authority = uri + strlen("http://");
    const char *end = authority;
    while (*end && *end != '/' && *end != '?' && *end != '#') {
        end++;
    }
    char *candidate = dup_strn(authority, (size_t)(end - authority));
    if (!candidate) {
        return 0;
    }
    int ok = _stricmp(candidate, g_origin + strlen("http://")) == 0;
    free(candidate);
    return ok;
}

static int
webview_at_expected_origin(void)
{
    if (!g_webview) {
        return 0;
    }
    LPWSTR source = NULL;
    if (FAILED(ICoreWebView2_get_Source(g_webview, &source)) || !source) {
        return 0;
    }
    char *uri = wide_to_utf8(source);
    CoTaskMemFree(source);
    if (!uri) {
        return 0;
    }
    int ok = origin_matches(uri);
    free(uri);
    return ok;
}

/* ------------------------------------------------------------------ */
/* Bridge methods                                                      */
/* ------------------------------------------------------------------ */

static int
uri_scheme_allowed(const char *uri)
{
    if (!uri) {
        return 0;
    }
    /* Reject control characters (chars < 0x20 or 0x7F) outright. */
    for (const unsigned char *cursor = (const unsigned char *)uri; *cursor != '\0'; cursor++) {
        if (*cursor < 0x20 || *cursor == 0x7F) {
            return 0;
        }
    }
    const char *separator = strstr(uri, "://");
    if (!separator) {
        return 0;
    }
    size_t scheme_length = (size_t)(separator - uri);
    int http = scheme_length == 4 && _strnicmp(uri, "http", 4) == 0;
    int https = scheme_length == 5 && _strnicmp(uri, "https", 5) == 0;
    if (!http && !https) {
        return 0;
    }
    const char *authority = separator + 3;
    const char *end = authority;
    while (*end && *end != '/' && *end != '?' && *end != '#') {
        end++;
    }
    if (end == authority) {
        return 0;
    }
    /* Security: never hand a URI with embedded credentials to the shell. */
    for (const char *cursor = authority; cursor < end; cursor++) {
        if (*cursor == '@') {
            return 0;
        }
    }
    return 1;
}

static void
handle_open_external(const char *id, const char *target)
{
    int ok = 0;
    if (target) {
        if (!uri_scheme_allowed(target)) {
            log_line("native_host: open external rejected scheme in '%s'\n", target);
        } else {
            wchar_t *wide = utf8_to_wide(target);
            if (wide) {
                HINSTANCE result = ShellExecuteW(g_window, L"open", wide, NULL,
                                                 g_data_dir_wide, SW_SHOWNORMAL);
                ok = (INT_PTR)result > 32;
                if (!ok) {
                    log_line("native_host: open external failed (code %lld)\n",
                             (long long)(INT_PTR)result);
                }
                free(wide);
            }
        }
    }
    resolve_bridge(id, ok ? "{\"ok\":true}" : "{\"ok\":false}");
}

static int
path_is_under(const wchar_t *path, const wchar_t *base)
{
    size_t base_length = wcslen(base);
    if (_wcsnicmp(path, base, base_length) != 0) {
        return 0;
    }
    return path[base_length] == L'\0' || path[base_length] == L'\\' || path[base_length] == L'/';
}

static wchar_t *
dup_wstr(const wchar_t *text)
{
    size_t length = wcslen(text);
    wchar_t *copy = (wchar_t *)malloc((length + 1) * sizeof(wchar_t));
    if (copy) {
        memcpy(copy, text, (length + 1) * sizeof(wchar_t));
    }
    return copy;
}

static wchar_t *
user_profile_path(void)
{
    wchar_t buffer[MAX_PATH * 2];
    if (GetEnvironmentVariableW(L"USERPROFILE", buffer, MAX_PATH * 2) == 0) {
        return NULL;
    }
    return dup_wstr(buffer);
}

static void
handle_reveal(const char *id, const char *path)
{
    int ok = 0;
    if (path) {
        wchar_t *wide = utf8_to_wide(path);
        if (wide) {
            wchar_t canonical[MAX_PATH * 4];
            DWORD length = GetFullPathNameW(wide, MAX_PATH * 4, canonical, NULL);
            if (length == 0 || length >= MAX_PATH * 4) {
                log_line("native_host: reveal rejected unresolvable path '%s'\n", path);
            } else {
                wchar_t *profile = user_profile_path();
                int inside = path_is_under(canonical, g_data_dir_wide) ||
                             (profile && path_is_under(canonical, profile));
                free(profile);
                if (!inside) {
                    /* Security: only reveal files under the data dir or the user's home dir. */
                    log_line("native_host: reveal rejected path outside data/home dirs: '%ls'\n",
                             canonical);
                } else if (!path_exists(canonical)) {
                    log_line("native_host: reveal rejected missing path '%ls'\n", canonical);
                } else {
                    /*
                     * Security: /select, opens the containing folder with the
                     * item highlighted; it never executes the path itself.
                     *
                     * Keep the lpCommandLine-only CreateProcessW pattern used
                     * by boot_server: with lpApplicationName NULL,
                     * CreateProcessW takes the first white-space-delimited
                     * token of the command line as the module name, so the
                     * executable must be an explicit first token here. (An
                     * earlier revision passed only /select,"<path>" and left
                     * an unused explorer.exe variable behind, so reveal
                     * always failed.)
                     */
                    StrBuf command;
                    sb_init(&command);
                    sb_append_str(&command, "explorer.exe /select,\"");
                    char *narrow = wide_to_utf8(canonical);
                    sb_append_str(&command, narrow ? narrow : "");
                    free(narrow);
                    sb_append_c(&command, '"');
                    char *command_text = sb_take(&command);
                    wchar_t *wide_command = utf8_to_wide(command_text);
                    free(command_text);
                    if (wide_command) {
                        STARTUPINFOW startup;
                        PROCESS_INFORMATION process;
                        memset(&startup, 0, sizeof(startup));
                        memset(&process, 0, sizeof(process));
                        startup.cb = sizeof(startup);
                        if (CreateProcessW(NULL, wide_command, NULL, NULL, FALSE,
                                           CREATE_NO_WINDOW, NULL, NULL, &startup, &process)) {
                            ok = 1;
                            CloseHandle(process.hThread);
                            CloseHandle(process.hProcess);
                        } else {
                            log_line("native_host: reveal failed (error %lu)\n", GetLastError());
                        }
                        free(wide_command);
                    }
                }
            }
            free(wide);
        }
    }
    resolve_bridge(id, ok ? "{\"ok\":true}" : "{\"ok\":false}");
}

static void
handle_dialog(const char *id, const char *kind, const char *title)
{
    IFileDialog *dialog = NULL;
    HRESULT result = CoCreateInstance(&CLSID_FileOpenDialog, NULL, CLSCTX_INPROC_SERVER,
                                      &IID_IFileDialog, (void **)&dialog);
    if (FAILED(result) || !dialog) {
        log_line("native_host: could not create the file dialog (0x%08lx)\n", (unsigned long)result);
        resolve_bridge(id, "{\"path\":null,\"cancelled\":true}");
        return;
    }

    FILEOPENDIALOGOPTIONS options = 0;
    IFileDialog_GetOptions(dialog, &options);
    int saving = kind && strcmp(kind, "save") == 0;
    if (kind && strcmp(kind, "file") == 0) {
        options |= FOS_FILEMUSTEXIST | FOS_FORCEFILESYSTEM;
    } else if (saving) {
        options |= FOS_OVERWRITEPROMPT;
    } else {
        options |= FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM;
    }
    IFileDialog_SetOptions(dialog, options);

    if (title) {
        wchar_t *wide_title = utf8_to_wide(title);
        if (wide_title) {
            IFileDialog_SetTitle(dialog, wide_title);
            free(wide_title);
        }
    }

    char *result_json = NULL;
    if (SUCCEEDED(IFileDialog_Show(dialog, g_window))) {
        IShellItem *item = NULL;
        if (SUCCEEDED(IFileDialog_GetResult(dialog, &item)) && item) {
            LPWSTR display = NULL;
            if (SUCCEEDED(IShellItem_GetDisplayName(item, SIGDN_FILESYSPATH, &display)) && display) {
                char *narrow = wide_to_utf8(display);
                CoTaskMemFree(display);
                if (narrow) {
                    char *escaped = json_escape_string(narrow);
                    StrBuf buffer;
                    sb_init(&buffer);
                    sb_appendf(&buffer, "{\"path\":\"%s\",\"cancelled\":false}",
                               escaped ? escaped : "");
                    free(escaped);
                    free(narrow);
                    result_json = sb_take(&buffer);
                }
            }
            IShellItem_Release(item);
        }
    }
    IFileDialog_Release(dialog);
    if (!result_json) {
        result_json = dup_str("{\"path\":null,\"cancelled\":true}");
    }
    resolve_bridge(id, result_json);
    free(result_json);
}

static void
apply_fullscreen(int enable)
{
    if (!g_window || g_fullscreen == enable) {
        return;
    }
    if (enable) {
        GetWindowRect(g_window, &g_saved_window_rect);
        g_saved_window_style = (DWORD)GetWindowLongPtrW(g_window, GWL_STYLE);
        MONITORINFO monitor;
        memset(&monitor, 0, sizeof(monitor));
        monitor.cbSize = sizeof(monitor);
        HMONITOR handle = MonitorFromWindow(g_window, MONITOR_DEFAULTTONEAREST);
        GetMonitorInfoW(handle, &monitor);
        SetWindowLongPtrW(g_window, GWL_STYLE,
                          (LONG_PTR)((g_saved_window_style & ~(WS_CAPTION | WS_THICKFRAME)) | WS_POPUP));
        SetWindowPos(g_window, HWND_TOP,
                     monitor.rcMonitor.left, monitor.rcMonitor.top,
                     monitor.rcMonitor.right - monitor.rcMonitor.left,
                     monitor.rcMonitor.bottom - monitor.rcMonitor.top,
                     SWP_FRAMECHANGED | SWP_NOOWNERZORDER);
        g_fullscreen = 1;
    } else {
        SetWindowLongPtrW(g_window, GWL_STYLE, (LONG_PTR)g_saved_window_style);
        SetWindowPos(g_window, NULL,
                     g_saved_window_rect.left, g_saved_window_rect.top,
                     g_saved_window_rect.right - g_saved_window_rect.left,
                     g_saved_window_rect.bottom - g_saved_window_rect.top,
                     SWP_FRAMECHANGED | SWP_NOZORDER | SWP_NOOWNERZORDER);
        g_fullscreen = 0;
    }
}

static void
handle_window_action(const char *id, const char *action)
{
    if (action && g_window) {
        if (strcmp(action, "minimize") == 0) {
            ShowWindow(g_window, SW_MINIMIZE);
        } else if (strcmp(action, "toggle-maximize") == 0) {
            ShowWindow(g_window, IsZoomed(g_window) ? SW_RESTORE : SW_MAXIMIZE);
        } else if (strcmp(action, "set-fullscreen") == 0) {
            apply_fullscreen(1);
        } else if (strcmp(action, "unset-fullscreen") == 0) {
            apply_fullscreen(0);
        } else if (strcmp(action, "close") == 0) {
            PostMessageW(g_window, WM_CLOSE, 0, 0);
        }
    }
    resolve_bridge(id, "{\"ok\":true}");
}

/* ------------------------------------------------------------------ */
/* WebView2 handlers                                                   */
/* ------------------------------------------------------------------ */

typedef struct {
    ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandlerVtbl *lpVtbl;
    LONG reference;
} EnvironmentHandler;

static HRESULT STDMETHODCALLTYPE
environment_handler_query(ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler *self,
                          REFIID riid, void **object)
{
    (void)riid;
    *object = self;
    ((EnvironmentHandler *)self)->reference++;
    return S_OK;
}

static ULONG STDMETHODCALLTYPE
environment_handler_addref(ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler *self)
{
    return (ULONG)InterlockedIncrement(&((EnvironmentHandler *)self)->reference);
}

static ULONG STDMETHODCALLTYPE
environment_handler_release(ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler *self)
{
    LONG remaining = InterlockedDecrement(&((EnvironmentHandler *)self)->reference);
    if (remaining == 0) {
        free(self);
    }
    return (ULONG)remaining;
}

static void create_controller(ICoreWebView2Environment *environment);

static HRESULT STDMETHODCALLTYPE
environment_handler_invoke(ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler *self,
                           HRESULT error_code, ICoreWebView2Environment *environment)
{
    (void)self;
    if (FAILED(error_code) || !environment) {
        log_line("native_host: WebView2 environment creation failed (0x%08lx). "
                 "Install the WebView2 runtime: "
                 "https://developer.microsoft.com/microsoft-edge/webview2/\n",
                 (unsigned long)error_code);
        PostQuitMessage(1);
        return S_OK;
    }
    create_controller(environment);
    return S_OK;
}

static ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandlerVtbl environment_vtbl = {
    environment_handler_query,
    environment_handler_addref,
    environment_handler_release,
    environment_handler_invoke,
};

typedef struct {
    ICoreWebView2CreateCoreWebView2ControllerCompletedHandlerVtbl *lpVtbl;
    LONG reference;
} ControllerHandler;

static HRESULT STDMETHODCALLTYPE
controller_handler_query(ICoreWebView2CreateCoreWebView2ControllerCompletedHandler *self,
                         REFIID riid, void **object)
{
    (void)riid;
    *object = self;
    ((ControllerHandler *)self)->reference++;
    return S_OK;
}

static ULONG STDMETHODCALLTYPE
controller_handler_addref(ICoreWebView2CreateCoreWebView2ControllerCompletedHandler *self)
{
    return (ULONG)InterlockedIncrement(&((ControllerHandler *)self)->reference);
}

static ULONG STDMETHODCALLTYPE
controller_handler_release(ICoreWebView2CreateCoreWebView2ControllerCompletedHandler *self)
{
    LONG remaining = InterlockedDecrement(&((ControllerHandler *)self)->reference);
    if (remaining == 0) {
        free(self);
    }
    return (ULONG)remaining;
}

static void on_webview_ready(void);
static void resize_webview(void);

static HRESULT STDMETHODCALLTYPE
controller_handler_invoke(ICoreWebView2CreateCoreWebView2ControllerCompletedHandler *self,
                          HRESULT error_code, ICoreWebView2Controller *controller)
{
    (void)self;
    if (FAILED(error_code) || !controller) {
        log_line("native_host: WebView2 controller creation failed (0x%08lx)\n",
                 (unsigned long)error_code);
        PostQuitMessage(1);
        return S_OK;
    }
    g_controller = controller;
    ICoreWebView2Controller_AddRef(g_controller);
    if (FAILED(ICoreWebView2Controller_get_CoreWebView2(g_controller, &g_webview)) || !g_webview) {
        log_line("native_host: could not obtain the WebView2 core\n");
        PostQuitMessage(1);
        return S_OK;
    }
    ICoreWebView2_AddRef(g_webview);

    ICoreWebView2Controller2 *controller2 = NULL;
    if (SUCCEEDED(ICoreWebView2Controller_QueryInterface(
            g_controller, &IID_ICoreWebView2Controller2, (void **)&controller2)) && controller2) {
        COREWEBVIEW2_COLOR background = { 255, 17, 16, 14 }; /* #11100e, A R G B */
        ICoreWebView2Controller2_put_DefaultBackgroundColor(controller2, background);
        ICoreWebView2Controller2_Release(controller2);
    }

    ICoreWebView2Controller_put_IsVisible(g_controller, TRUE);
    resize_webview();
    on_webview_ready();
    return S_OK;
}

static ICoreWebView2CreateCoreWebView2ControllerCompletedHandlerVtbl controller_vtbl = {
    controller_handler_query,
    controller_handler_addref,
    controller_handler_release,
    controller_handler_invoke,
};

typedef struct {
    ICoreWebView2WebMessageReceivedEventHandlerVtbl *lpVtbl;
    LONG reference;
} MessageHandler;

static HRESULT STDMETHODCALLTYPE
message_handler_query(ICoreWebView2WebMessageReceivedEventHandler *self, REFIID riid, void **object)
{
    (void)riid;
    *object = self;
    ((MessageHandler *)self)->reference++;
    return S_OK;
}

static ULONG STDMETHODCALLTYPE
message_handler_addref(ICoreWebView2WebMessageReceivedEventHandler *self)
{
    return (ULONG)InterlockedIncrement(&((MessageHandler *)self)->reference);
}

static ULONG STDMETHODCALLTYPE
message_handler_release(ICoreWebView2WebMessageReceivedEventHandler *self)
{
    LONG remaining = InterlockedDecrement(&((MessageHandler *)self)->reference);
    if (remaining == 0) {
        free(self);
    }
    return (ULONG)remaining;
}

/*
 * Best-effort recovery of the "id" field from a rejected bridge payload.
 * The strict json_parse_flat_object clears every field when the payload is
 * malformed, so this scanner tolerates truncated or hostile JSON and
 * extracts just the id string (escapes decoded the same way as the strict
 * parser; anything unrecognised aborts the scan). Returns a heap string, or
 * NULL when no id can be recovered. The id is only a lookup key for
 * window.__openboxResolve and is single-quote escaped by resolve_bridge
 * before splicing, so a hostile value cannot break out of the call.
 */
static char *
bridge_rejected_id(const char *json)
{
    const char *cursor = json;
    for (;;) {
        cursor = strstr(cursor, "\"id\"");
        if (!cursor) {
            return NULL;
        }
        const char *value = cursor + 4;
        while (*value == ' ' || *value == '\t') {
            value++;
        }
        if (*value == '\0') {
            return NULL;
        }
        if (*value++ != ':') {
            cursor = value;
            continue;
        }
        while (*value == ' ' || *value == '\t') {
            value++;
        }
        if (*value == '\0') {
            return NULL;
        }
        if (*value++ != '"') {
            cursor = value;
            continue;
        }
        StrBuf id;
        sb_init(&id);
        int complete = 0;
        while (*value) {
            unsigned char character = (unsigned char)*value;
            if (character < 0x20) {
                break;
            }
            if (character == '"') {
                complete = id.length <= MAX_NATIVE_VALUE_BYTES;
                break;
            }
            if (character != '\\') {
                if (!sb_append_c(&id, (char)character)) {
                    break;
                }
                value++;
                continue;
            }
            value++;
            switch (*value) {
            case '"': character = '"'; break;
            case '\\': character = '\\'; break;
            case '/': character = '/'; break;
            case 'b': character = '\b'; break;
            case 'f': character = '\f'; break;
            case 'n': character = '\n'; break;
            case 'r': character = '\r'; break;
            case 't': character = '\t'; break;
            default: character = 0; break;
            }
            if (!character) {
                break;
            }
            if (!sb_append_c(&id, (char)character) || id.length > MAX_NATIVE_VALUE_BYTES) {
                break;
            }
            value++;
        }
        if (complete) {
            return sb_take(&id);
        }
        sb_free(&id);
        return NULL;
    }
}

/*
 * Resolve (with an error) the frontend promise for a bridge message that
 * failed the origin gate or JSON parse. Without this, the page's
 * window.__openboxResolve promise hangs forever: the bridge contract is
 * that every postMessage gets a resolution. Unknown ids are a harmless
 * no-op in the page's resolver.
 */
static void
reject_bridge_message(ICoreWebView2WebMessageReceivedEventArgs *args)
{
    LPWSTR payload = NULL;
    if (FAILED(ICoreWebView2WebMessageReceivedEventArgs_TryGetWebMessageAsString(args, &payload)) ||
        !payload) {
        return;
    }
    char *json = wide_to_utf8(payload);
    CoTaskMemFree(payload);
    if (!json) {
        return;
    }
    char *id = bridge_rejected_id(json);
    free(json);
    if (id) {
        resolve_bridge(id, "{\"ok\":false,\"error\":\"bridge: rejected\"}");
        free(id);
    }
}

static HRESULT STDMETHODCALLTYPE
message_handler_invoke(ICoreWebView2WebMessageReceivedEventHandler *self,
                       ICoreWebView2 *sender, ICoreWebView2WebMessageReceivedEventArgs *args)
{
    (void)self;
    (void)sender;

    LPWSTR source = NULL;
    if (SUCCEEDED(ICoreWebView2WebMessageReceivedEventArgs_get_Source(args, &source)) && source) {
        char *uri = wide_to_utf8(source);
        CoTaskMemFree(source);
        int allowed = origin_matches(uri) && webview_at_expected_origin();
        free(uri);
        if (!allowed) {
            /* Security: only the exact booted app origin may drive the bridge. */
            log_line("native_host: rejecting bridge message from unexpected origin\n");
            reject_bridge_message(args);
            return S_OK;
        }
    } else {
        return S_OK;
    }

    LPWSTR payload = NULL;
    if (FAILED(ICoreWebView2WebMessageReceivedEventArgs_TryGetWebMessageAsString(args, &payload)) ||
        !payload) {
        log_line("native_host: rejecting non-string bridge message\n");
        return S_OK;
    }
    char *json = wide_to_utf8(payload);
    CoTaskMemFree(payload);
    if (!json) {
        return S_OK;
    }

    BridgeMessage message;
    if (!json_parse_flat_object(json, &message)) {
        log_line("native_host: rejecting malformed bridge message\n");
        reject_bridge_message(args);
        free(json);
        return S_OK;
    }
    free(json);

    if (message.id && message.method) {
        if (strcmp(message.method, "dialog") == 0) {
            handle_dialog(message.id, message.kind, message.title);
        } else if (strcmp(message.method, "openExternal") == 0) {
            handle_open_external(message.id, message.target);
        } else if (strcmp(message.method, "reveal") == 0) {
            handle_reveal(message.id, message.path);
        } else if (strcmp(message.method, "windowAction") == 0) {
            handle_window_action(message.id, message.action);
        } else {
            resolve_bridge(message.id, "{\"ok\":false}");
        }
    }
    bridge_message_clear(&message);
    return S_OK;
}

static ICoreWebView2WebMessageReceivedEventHandlerVtbl message_vtbl = {
    message_handler_query,
    message_handler_addref,
    message_handler_release,
    message_handler_invoke,
};

typedef struct {
    ICoreWebView2NavigationStartingEventHandlerVtbl *lpVtbl;
    LONG reference;
} NavigationHandler;

static HRESULT STDMETHODCALLTYPE
navigation_handler_query(ICoreWebView2NavigationStartingEventHandler *self,
                         REFIID riid, void **object)
{
    (void)riid;
    *object = self;
    ((NavigationHandler *)self)->reference++;
    return S_OK;
}

static ULONG STDMETHODCALLTYPE
navigation_handler_addref(ICoreWebView2NavigationStartingEventHandler *self)
{
    return (ULONG)InterlockedIncrement(&((NavigationHandler *)self)->reference);
}

static ULONG STDMETHODCALLTYPE
navigation_handler_release(ICoreWebView2NavigationStartingEventHandler *self)
{
    LONG remaining = InterlockedDecrement(&((NavigationHandler *)self)->reference);
    if (remaining == 0) {
        free(self);
    }
    return (ULONG)remaining;
}

static HRESULT STDMETHODCALLTYPE
navigation_handler_invoke(ICoreWebView2NavigationStartingEventHandler *self,
                          ICoreWebView2 *sender, ICoreWebView2NavigationStartingEventArgs *args)
{
    (void)self;
    (void)sender;
    LPWSTR uri = NULL;
    if (SUCCEEDED(ICoreWebView2NavigationStartingEventArgs_get_Uri(args, &uri)) && uri) {
        char *narrow = wide_to_utf8(uri);
        CoTaskMemFree(uri);
        if (!origin_matches(narrow)) {
            log_line("native_host: blocking navigation away from the app origin\n");
            ICoreWebView2NavigationStartingEventArgs_put_Cancel(args, TRUE);
        }
        free(narrow);
    }
    return S_OK;
}

static ICoreWebView2NavigationStartingEventHandlerVtbl navigation_vtbl = {
    navigation_handler_query,
    navigation_handler_addref,
    navigation_handler_release,
    navigation_handler_invoke,
};

typedef struct {
    ICoreWebView2NewWindowRequestedEventHandlerVtbl *lpVtbl;
    LONG reference;
} NewWindowHandler;

static HRESULT STDMETHODCALLTYPE
new_window_handler_query(ICoreWebView2NewWindowRequestedEventHandler *self,
                         REFIID riid, void **object)
{
    (void)riid;
    *object = self;
    ((NewWindowHandler *)self)->reference++;
    return S_OK;
}

static ULONG STDMETHODCALLTYPE
new_window_handler_addref(ICoreWebView2NewWindowRequestedEventHandler *self)
{
    return (ULONG)InterlockedIncrement(&((NewWindowHandler *)self)->reference);
}

static ULONG STDMETHODCALLTYPE
new_window_handler_release(ICoreWebView2NewWindowRequestedEventHandler *self)
{
    LONG remaining = InterlockedDecrement(&((NewWindowHandler *)self)->reference);
    if (remaining == 0) {
        free(self);
    }
    return (ULONG)remaining;
}

static HRESULT STDMETHODCALLTYPE
new_window_handler_invoke(ICoreWebView2NewWindowRequestedEventHandler *self,
                          ICoreWebView2 *sender, ICoreWebView2NewWindowRequestedEventArgs *args)
{
    (void)self;
    (void)sender;
    /* The app is a single window; external links go through the bridge. */
    ICoreWebView2NewWindowRequestedEventArgs_put_Handled(args, TRUE);
    return S_OK;
}

static ICoreWebView2NewWindowRequestedEventHandlerVtbl new_window_vtbl = {
    new_window_handler_query,
    new_window_handler_addref,
    new_window_handler_release,
    new_window_handler_invoke,
};

static const char *BRIDGE_SCRIPT =
    "window.__openboxPending = {};\n"
    "window.__openboxResolve = function(id, result) {\n"
    "  if (window.__openboxPending[id]) {\n"
    "    window.__openboxPending[id](result);\n"
    "    delete window.__openboxPending[id];\n"
    "  }\n"
    "};\n"
    "(function(){\n"
    "  function post(id, method, args) {\n"
    "    var payload = {id:id, method:method, kind:'', title:'', target:'', path:'', action:''};\n"
    "    if (args) {\n"
    "      for (var key in args) {\n"
    "        if (Object.prototype.hasOwnProperty.call(args, key) && typeof args[key] === 'string') {\n"
    "          payload[key] = args[key];\n"
    "        }\n"
    "      }\n"
    "    }\n"
    "    window.chrome.webview.postMessage(JSON.stringify(payload));\n"
    "  }\n"
    "  function request(prefix, method, args) {\n"
    "    return new Promise(function(resolve) {\n"
    "      var id = prefix + Date.now() + Math.random();\n"
    "      window.__openboxPending[id] = resolve;\n"
    "      post(id, method, args);\n"
    "    });\n"
    "  }\n"
    "  window.openboxNative = {\n"
    "    dialog: function(kind, opts) {\n"
    "      var args = Object.assign({kind:kind}, opts || {});\n"
    "      return request('d', 'dialog', args);\n"
    "    },\n"
    "    openExternal: function(target) { return request('o', 'openExternal', {target:target}); },\n"
    "    reveal: function(path) { return request('r', 'reveal', {path:path}); },\n"
    "    windowAction: function(action) { return request('w', 'windowAction', {action:action}); },\n"
    "    onGamepad: function(callback) {}\n"
    "  };\n"
    "})();\n"
    "true;\n";

static void
create_controller(ICoreWebView2Environment *environment)
{
    ControllerHandler *handler = (ControllerHandler *)calloc(1, sizeof(ControllerHandler));
    if (!handler) {
        PostQuitMessage(1);
        return;
    }
    handler->lpVtbl = &controller_vtbl;
    handler->reference = 1;
    HRESULT result = ICoreWebView2Environment_CreateCoreWebView2Controller(
        environment, g_window,
        (ICoreWebView2CreateCoreWebView2ControllerCompletedHandler *)handler);
    if (FAILED(result)) {
        log_line("native_host: CreateCoreWebView2Controller failed (0x%08lx)\n",
                 (unsigned long)result);
        controller_handler_release(
            (ICoreWebView2CreateCoreWebView2ControllerCompletedHandler *)handler);
        PostQuitMessage(1);
    }
}

static void
on_webview_ready(void)
{
    ICoreWebView2Settings *settings = NULL;
    if (SUCCEEDED(ICoreWebView2_get_Settings(g_webview, &settings)) && settings) {
        ICoreWebView2Settings_put_AreDevToolsEnabled(settings, FALSE);
        ICoreWebView2Settings_put_AreDefaultContextMenusEnabled(settings, FALSE);
        ICoreWebView2Settings_put_IsStatusBarEnabled(settings, FALSE);
        ICoreWebView2Settings_put_IsZoomControlEnabled(settings, FALSE);
        ICoreWebView2Settings_Release(settings);
    }

    MessageHandler *message_handler = (MessageHandler *)calloc(1, sizeof(MessageHandler));
    if (message_handler) {
        message_handler->lpVtbl = &message_vtbl;
        message_handler->reference = 1;
        EventRegistrationToken token;
        ICoreWebView2_add_WebMessageReceived(
            g_webview, (ICoreWebView2WebMessageReceivedEventHandler *)message_handler, &token);
    }

    NavigationHandler *navigation_handler = (NavigationHandler *)calloc(1, sizeof(NavigationHandler));
    if (navigation_handler) {
        navigation_handler->lpVtbl = &navigation_vtbl;
        navigation_handler->reference = 1;
        EventRegistrationToken token;
        ICoreWebView2_add_NavigationStarting(
            g_webview, (ICoreWebView2NavigationStartingEventHandler *)navigation_handler, &token);
    }

    NewWindowHandler *new_window_handler = (NewWindowHandler *)calloc(1, sizeof(NewWindowHandler));
    if (new_window_handler) {
        new_window_handler->lpVtbl = &new_window_vtbl;
        new_window_handler->reference = 1;
        EventRegistrationToken token;
        ICoreWebView2_add_NewWindowRequested(
            g_webview, (ICoreWebView2NewWindowRequestedEventHandler *)new_window_handler, &token);
    }

    wchar_t *script = utf8_to_wide(BRIDGE_SCRIPT);
    if (script) {
        ICoreWebView2_AddScriptToExecuteOnDocumentCreated(g_webview, script, NULL);
        free(script);
    }

    g_webview_ready = 1;

    NativeRequest initial = g_pending_request;
    if (initial.kind == NATIVE_REQUEST_NONE) {
        initial.kind = NATIVE_REQUEST_START;
    }
    dispatch_native_request(&initial);
    if (g_pending_focus) {
        g_pending_focus = 0;
        ShowWindow(g_window, SW_SHOW);
        SetForegroundWindow(g_window);
    }
}

/* ------------------------------------------------------------------ */
/* Window                                                              */
/* ------------------------------------------------------------------ */

static void
resize_webview(void)
{
    if (!g_controller) {
        return;
    }
    RECT bounds;
    GetClientRect(g_window, &bounds);
    ICoreWebView2Controller_put_Bounds(g_controller, bounds);
}

static void
present_window(void)
{
    if (!g_window) {
        return;
    }
    if (!IsWindowVisible(g_window)) {
        ShowWindow(g_window, SW_SHOW);
    }
    if (IsIconic(g_window)) {
        ShowWindow(g_window, SW_RESTORE);
    }
    SetForegroundWindow(g_window);
}

static void
dispatch_native_request(const NativeRequest *request)
{
    if (!request || request->kind == NATIVE_REQUEST_NONE) {
        return;
    }
    if (!g_webview_ready) {
        native_request_copy(&g_pending_request, request);
        return;
    }

    present_window();
    if (request->kind == NATIVE_REQUEST_LAUNCH ||
        request->kind == NATIVE_REQUEST_RESUME) {
        /* Keep --play/launch semantics identical to parity_deeplinks: the
         * game starts through the authenticated API, then the page lands on
         * the corresponding UI deeplink in the native window. */
        dispatch_native_api_request(request);
    }
    char *url = native_authenticated_url(request);
    if (!url) {
        log_line("native_host: could not build the authenticated deeplink\n");
        return;
    }
    wchar_t *wide = utf8_to_wide(url);
    free(url);
    if (wide) {
        ICoreWebView2_Navigate(g_webview, wide);
        free(wide);
    }
}

static void
show_tray_menu(void)
{
    HMENU menu = CreatePopupMenu();
    if (!menu) {
        return;
    }
    AppendMenuW(menu, MF_STRING, IDM_TRAY_SHOW, L"Show OpenBox");
    AppendMenuW(menu, MF_SEPARATOR, 0, NULL);
    AppendMenuW(menu, MF_STRING, IDM_TRAY_QUIT, L"Quit");
    POINT cursor;
    GetCursorPos(&cursor);
    /* A tray icon whose only action hides the window would strand the user. */
    SetForegroundWindow(g_window);
    TrackPopupMenu(menu, TPM_RIGHTBUTTON, cursor.x, cursor.y, 0, g_window, NULL);
    DestroyMenu(menu);
}

static void
add_tray_icon(void)
{
    if (!g_tray_enabled || g_tray_added) {
        return;
    }
    NOTIFYICONDATAW data;
    memset(&data, 0, sizeof(data));
    data.cbSize = sizeof(data);
    data.hWnd = g_window;
    data.uID = TRAY_ICON_ID;
    data.uFlags = NIF_MESSAGE | NIF_TIP | NIF_ICON;
    data.uCallbackMessage = WM_APP_TRAY;
    data.hIcon = (HICON)LoadImageW(NULL, g_icon_path, IMAGE_ICON, 0, 0,
                                   LR_LOADFROMFILE | LR_DEFAULTSIZE);
    if (!data.hIcon) {
        data.hIcon = LoadIconW(NULL, IDI_APPLICATION);
    }
    wcscpy(data.szTip, L"OpenBox Game Launcher");
    if (Shell_NotifyIconW(NIM_ADD, &data)) {
        g_tray_added = 1;
    } else {
        log_line("native_host: could not add the tray icon (error %lu)\n", GetLastError());
    }
}

static void
remove_tray_icon(void)
{
    if (!g_tray_added) {
        return;
    }
    NOTIFYICONDATAW data;
    memset(&data, 0, sizeof(data));
    data.cbSize = sizeof(data);
    data.hWnd = g_window;
    data.uID = TRAY_ICON_ID;
    Shell_NotifyIconW(NIM_DELETE, &data);
    g_tray_added = 0;
}

static void
quit_host(void)
{
    if (g_closing) {
        return;
    }
    g_closing = 1;
    save_geometry();
    remove_tray_icon();
    stop_server();
    release_single_instance();
    PostQuitMessage(0);
}

static LRESULT CALLBACK
window_proc(HWND window, UINT message, WPARAM wparam, LPARAM lparam)
{
    switch (message) {
    case WM_SIZE:
        resize_webview();
        return 0;
    case WM_DPICHANGED:
        if (lparam) {
            RECT *suggested = (RECT *)lparam;
            SetWindowPos(window, NULL, suggested->left, suggested->top,
                         suggested->right - suggested->left,
                         suggested->bottom - suggested->top,
                         SWP_NOZORDER | SWP_NOACTIVATE);
        }
        resize_webview();
        return 0;
    case WM_GETMINMAXINFO: {
        MINMAXINFO *info = (MINMAXINFO *)lparam;
        if (info) {
            info->ptMinTrackSize.x = 400;
            info->ptMinTrackSize.y = 300;
        }
        return 0;
    }
    case WM_CLOSE:
        if (g_minimize_to_tray && g_tray_added) {
            ShowWindow(window, SW_HIDE);
            return 0;
        }
        quit_host();
        return 0;
    case WM_DESTROY:
        quit_host();
        return 0;
    case WM_APP_NATIVE_REQUEST: {
        NativeRequest *request = (NativeRequest *)lparam;
        if (request) {
            dispatch_native_request(request);
            native_request_clear(request);
            free(request);
        }
        return 0;
    }
    case WM_APP_NATIVE_FOCUS:
        if (g_window) {
            present_window();
        } else {
            g_pending_focus = 1;
        }
        return 0;
    case WM_APP_TRAY:
        if (LOWORD(lparam) == WM_LBUTTONUP || LOWORD(lparam) == NIN_SELECT) {
            present_window();
        } else if (LOWORD(lparam) == WM_RBUTTONUP || LOWORD(lparam) == WM_CONTEXTMENU) {
            show_tray_menu();
        }
        return 0;
    case WM_COMMAND:
        if (LOWORD(wparam) == IDM_TRAY_SHOW) {
            present_window();
            return 0;
        }
        if (LOWORD(wparam) == IDM_TRAY_QUIT) {
            ShowWindow(window, SW_SHOW);
            quit_host();
            return 0;
        }
        return 0;
    case WM_ENDSESSION:
        quit_host();
        return 0;
    default:
        return DefWindowProcW(window, message, wparam, lparam);
    }
}

/* ------------------------------------------------------------------ */
/* Entry point                                                         */
/* ------------------------------------------------------------------ */

static wchar_t *
resolve_icon_path(void)
{
    if (g_web_app_path) {
        const wchar_t *slash = wcsrchr(g_web_app_path, L'\\');
        if (slash) {
            wchar_t *directory = dup_wstr(g_web_app_path);
            if (directory) {
                directory[slash - g_web_app_path] = L'\0';
                wchar_t *candidate = path_join(directory, L"openbox.ico");
                free(directory);
                if (candidate && path_exists(candidate)) {
                    return candidate;
                }
                free(candidate);
            }
        }
    }
    wchar_t module[MAX_PATH * 2];
    if (GetModuleFileNameW(NULL, module, MAX_PATH * 2) > 0) {
        const wchar_t *slash = wcsrchr(module, L'\\');
        if (slash) {
            wchar_t saved = *slash;
            *(wchar_t *)slash = L'\0';
            wchar_t *candidate = path_join(module, L"openbox.ico");
            *(wchar_t *)slash = saved;
            if (candidate && path_exists(candidate)) {
                return candidate;
            }
            free(candidate);
        }
    }
    return NULL;
}

static int
host_build_pipe_name(void)
{
    /*
     * Namespace the pipe by data dir so two profiles (or an
     * OPENBOX_DATA_DIR override) each get their own single instance.
     */
    unsigned long hash = 2166136261u;
    for (const unsigned char *cursor = (const unsigned char *)g_data_dir; *cursor; cursor++) {
        hash ^= *cursor;
        hash *= 16777619u;
    }
    wchar_t name[128];
    _snwprintf(name, 128, L"\\\\.\\pipe\\openbox-native-host-%08lx",
               hash & 0xFFFFFFFFul);
    g_pipe_name = dup_wstr(name);
    return g_pipe_name != NULL;
}

static int
wide_argv_to_utf8(int argc, wchar_t **argv, char ***out_argv)
{
    char **converted = (char **)calloc((size_t)argc + 1, sizeof(char *));
    if (!converted) {
        return 0;
    }
    for (int index = 0; index < argc; index++) {
        converted[index] = wide_to_utf8(argv[index]);
        if (!converted[index]) {
            for (int released = 0; released < index; released++) {
                free(converted[released]);
            }
            free(converted);
            return 0;
        }
    }
    *out_argv = converted;
    return 1;
}

static void
free_argv(int argc, char **argv)
{
    if (!argv) {
        return;
    }
    for (int index = 0; index < argc; index++) {
        free(argv[index]);
    }
    free(argv);
}

int WINAPI
wWinMain(HINSTANCE instance, HINSTANCE previous, LPWSTR command_line, int show_command)
{
    (void)previous;
    (void)command_line;
    (void)show_command;

    g_instance = instance;
    InitializeCriticalSection(&g_log_lock);

    int argc = 0;
    wchar_t **wide_argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!wide_argv) {
        return 2;
    }
    char **argv = NULL;
    if (!wide_argv_to_utf8(argc, wide_argv, &argv)) {
        LocalFree(wide_argv);
        return 2;
    }
    LocalFree(wide_argv);

    NativeRequest startup_request = { NATIVE_REQUEST_NONE, NULL };
    NativeArgsResult args_result = native_request_from_argv(argc, argv, &startup_request);
    if (args_result == NATIVE_ARGS_INVALID) {
        free_argv(argc, argv);
        native_request_clear(&startup_request);
        return 2;
    }
    free_argv(argc, argv);

    /* Resolve configurable paths from the environment. */
    wchar_t buffer[MAX_PATH * 2];
    if (GetEnvironmentVariableW(L"OPENBOX_WEB_APP", buffer, MAX_PATH * 2) > 0) {
        g_web_app_path = dup_wstr(buffer);
    }
    if (GetEnvironmentVariableW(L"OPENBOX_PYTHON", buffer, MAX_PATH * 2) > 0) {
        g_python_path = dup_wstr(buffer);
    } else {
        wchar_t resolved[MAX_PATH * 2];
        if (SearchPathW(NULL, L"python.exe", NULL, MAX_PATH * 2, resolved, NULL) > 0) {
            g_python_path = dup_wstr(resolved);
        } else if (SearchPathW(NULL, L"py.exe", NULL, MAX_PATH * 2, resolved, NULL) > 0) {
            g_python_path = dup_wstr(resolved);
        } else {
            g_python_path = dup_wstr(L"python.exe");
        }
    }

    if (GetEnvironmentVariableW(L"OPENBOX_DATA_DIR", buffer, MAX_PATH * 2) > 0) {
        g_data_dir_wide = dup_wstr(buffer);
    } else {
        g_data_dir_wide = resolve_default_data_dir();
    }
    if (!g_data_dir_wide) {
        return 1;
    }
    if (!ensure_directory(g_data_dir_wide)) {
        return 1;
    }
    g_data_dir = wide_to_utf8(g_data_dir_wide);
    if (!g_data_dir) {
        return 1;
    }

    /* Capture failures in the data dir so a double-click is not silent. */
    wchar_t *log_file = path_join(g_data_dir_wide, L"openbox-native.log");
    if (log_file) {
        g_log_path = wide_to_utf8(log_file);
        free(log_file);
    }
    log_line("=== native_host (windows) starting ===\n");
    log_line("native_host version %s; WebView2 SDK cache key microsoft.web.webview2/%s\n",
             NATIVE_HOST_VERSION, WEBVIEW2_SDK_VERSION);

    wchar_t *geometry_file = path_join(g_data_dir_wide, L"window-geometry");
    if (geometry_file) {
        g_geometry_path = wide_to_utf8(geometry_file);
        free(geometry_file);
    }
    g_icon_path = resolve_icon_path();

    /*
     * The loopback API calls in api_post_json() fail with WSANOTINITIALISED
     * unless Winsock is started first, which would silently drop deeplink
     * dispatch and the graceful /api/shutdown POST.
     */
    WSADATA wsa_data;
    if (WSAStartup(MAKEWORD(2, 2), &wsa_data) != 0) {
        log_line("native_host: WSAStartup failed; loopback API calls are unavailable\n");
    }

    if (!host_build_pipe_name() || !acquire_single_instance(&startup_request)) {
        /* Another instance is running; its owner handles focus and dispatch. */
        log_line("native_host: an OpenBox window is already open; request forwarded\n");
        native_request_clear(&startup_request);
        return 0;
    }

    /*
     * Only the owning instance needs a web app to boot, so this check runs
     * after the single-instance handoff: a forwarding instance (a deeplink or
     * protocol invocation) must not fail just because the environment is bare.
     */
    if (!g_web_app_path || !path_exists(g_web_app_path)) {
        log_line("native_host: OPENBOX_WEB_APP must point at web_app.py\n");
        release_single_instance();
        return 1;
    }

    /* Signal web_app.py that the native host is present (the child inherits). */
    SetEnvironmentVariableW(L"OPENBOX_NATIVE_HOST", L"1");

    if (FAILED(CoInitializeEx(NULL, COINIT_APARTMENTTHREADED))) {
        log_line("native_host: COM initialization failed\n");
        release_single_instance();
        return 1;
    }

    if (!boot_server()) {
        log_line("native_host: could not start the OpenBox server. "
                 "Is Python installed and OPENBOX_WEB_APP correct?\n");
        stop_server();
        release_single_instance();
        native_request_clear(&startup_request);
        return 1;
    }

    load_geometry();
    load_tray_flags();

    WNDCLASSEXW window_class;
    memset(&window_class, 0, sizeof(window_class));
    window_class.cbSize = sizeof(window_class);
    window_class.lpfnWndProc = window_proc;
    window_class.hInstance = instance;
    window_class.hCursor = LoadCursorW(NULL, IDC_ARROW);
    window_class.hIcon = (HICON)LoadImageW(NULL, g_icon_path, IMAGE_ICON, 0, 0,
                                           LR_LOADFROMFILE | LR_DEFAULTSIZE);
    window_class.hIconSm = window_class.hIcon;
    window_class.lpszClassName = L"OpenBoxNativeHostWindow";
    if (!RegisterClassExW(&window_class)) {
        log_line("native_host: could not register the window class (error %lu)\n",
                 GetLastError());
        stop_server();
        release_single_instance();
        return 1;
    }

    int screen_width = GetSystemMetrics(SM_CXSCREEN);
    int screen_height = GetSystemMetrics(SM_CYSCREEN);
    int width = g_default_width < screen_width ? g_default_width : screen_width;
    int height = g_default_height < screen_height ? g_default_height : screen_height;
    int left = (screen_width - width) / 2;
    int top = (screen_height - height) / 2;

    g_window = CreateWindowExW(
        0, window_class.lpszClassName, L"OpenBox Game Launcher",
        WS_OVERLAPPEDWINDOW,
        left < 0 ? CW_USEDEFAULT : left, top < 0 ? CW_USEDEFAULT : top,
        width, height,
        NULL, NULL, instance, NULL);
    if (!g_window) {
        log_line("native_host: could not create the window (error %lu)\n", GetLastError());
        stop_server();
        release_single_instance();
        return 1;
    }
    if (window_class.hIcon) {
        SendMessageW(g_window, WM_SETICON, ICON_BIG, (LPARAM)window_class.hIcon);
        SendMessageW(g_window, WM_SETICON, ICON_SMALL, (LPARAM)window_class.hIcon);
    }
    ShowWindow(g_window, g_window_maximized ? SW_MAXIMIZE : SW_SHOW);
    UpdateWindow(g_window);

    wchar_t *user_data_folder = path_join(g_data_dir_wide, L"webview2");
    if (!user_data_folder) {
        stop_server();
        release_single_instance();
        return 1;
    }
    ensure_directory(user_data_folder);

    EnvironmentHandler *handler = (EnvironmentHandler *)calloc(1, sizeof(EnvironmentHandler));
    if (!handler) {
        free(user_data_folder);
        stop_server();
        release_single_instance();
        return 1;
    }
    handler->lpVtbl = &environment_vtbl;
    handler->reference = 1;

    HRESULT result = CreateCoreWebView2EnvironmentWithOptions(
        NULL, user_data_folder, NULL,
        (ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler *)handler);
    free(user_data_folder);
    if (FAILED(result)) {
        log_line("native_host: WebView2 environment creation failed (0x%08lx). "
                 "Install the WebView2 runtime: "
                 "https://developer.microsoft.com/microsoft-edge/webview2/\n",
                 (unsigned long)result);
        environment_handler_release(
            (ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler *)handler);
        DestroyWindow(g_window);
        return 1;
    }

    add_tray_icon();

    MSG message;
    while (GetMessageW(&message, NULL, 0, 0) > 0) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }

    if (g_webview) {
        ICoreWebView2_Release(g_webview);
        g_webview = NULL;
    }
    if (g_controller) {
        ICoreWebView2Controller_Release(g_controller);
        g_controller = NULL;
    }
    if (window_class.hIcon) {
        DestroyIcon(window_class.hIcon);
    }
    free(g_icon_path);
    free(g_geometry_path);
    free(g_log_path);
    free(g_data_dir);
    free(g_data_dir_wide);
    free(g_origin);
    free(g_origin_wide);
    free(g_token);
    free(g_web_app_path);
    free(g_python_path);
    native_request_clear(&g_pending_request);
    native_request_clear(&startup_request);
    DeleteCriticalSection(&g_log_lock);
    WSACleanup();
    return 0;
}
