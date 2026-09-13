/*
 * OpenBox native host: a small WebKitGTK 4.1 window rendering the web app
 * over loopback. Owns the Python server lifecycle, exposes native dialogs
 * and window chrome via a JS bridge, shuts the server down on window close.
 *
 * Business logic stays in Python; this file is chrome, bridge, and process
 * ownership only.
 *
 * Build (repo root):
 *   gcc -O2 native_host.c -o native_host $(pkg-config --cflags --libs webkit2gtk-4.1)
 */

#include <gtk/gtk.h>
#include <webkit2/webkit2.h>
#include <gio/gio.h>
#include <glib.h>
#include <glib-unix.h>
#include <arpa/inet.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/socket.h>
#include <sys/inotify.h>
#include <sys/un.h>
#include <sys/time.h>
#include <time.h>
#include <poll.h>
#include <unistd.h>

#define DEFAULT_DATA_DIR "/.local/share/openbox-game-launcher"
#define BOOT_TIMEOUT_SECONDS 30
#define API_TIMEOUT_SECONDS 30
#define MAX_NATIVE_VALUE_BYTES 4096
#define MAX_IPC_MESSAGE_BYTES 8192

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

static pid_t server_pid = 0;
static GtkWidget *main_window = NULL;
static WebKitWebView *main_view = NULL;
static char *token = NULL;
static char *origin = NULL;
static guint16 server_port = 0;
static const char *data_dir = NULL;
static const char *web_app_path = NULL;
static const char *python_path = "python3";
static int server_sock_fd = -1;
static guint socket_watch_id = 0;
static char *geometry_path = NULL;
static int default_width = 1280;
static int default_height = 780;
static gboolean window_maximized = FALSE;
static gboolean tray_enabled = FALSE;
static gboolean minimize_to_tray = FALSE;
static GtkStatusIcon *tray_icon = NULL;
static char *application_icon_path = NULL;
static NativeRequest pending_request = { NATIVE_REQUEST_NONE, NULL };
static gboolean pending_focus = FALSE;
/* ------------------------------------------------------------------ */
/* Process ownership                                                   */
/* ------------------------------------------------------------------ */

static gboolean
path_exists(const char *path)
{
    return path && g_file_test(path, G_FILE_TEST_EXISTS);
}

static char *
read_secure_trimmed_file(const char *path)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) {
        g_printerr("native_host: could not open %s: %s\n", path, strerror(errno));
        return NULL;
    }
    struct stat info;
    if (fstat(fd, &info) < 0 || !S_ISREG(info.st_mode) ||
        info.st_uid != geteuid() || (info.st_mode & 0077) != 0 ||
        info.st_size <= 0 || info.st_size > 4096) {
        g_printerr("native_host: rejecting insecure server file %s\n", path);
        close(fd);
        return NULL;
    }
    gsize capacity = (gsize)info.st_size + 1;
    char *contents = g_malloc(capacity);
    gsize length = 0;
    while (length < (gsize)info.st_size) {
        ssize_t count = read(fd, contents + length, (gsize)info.st_size - length);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            g_printerr("native_host: could not read %s\n", path);
            close(fd);
            g_free(contents);
            return NULL;
        }
        length += (gsize)count;
    }
    close(fd);
    contents[length] = '\0';
    char *end = contents + length;
    while (end > contents && (end[-1] == '\n' || end[-1] == '\r')) {
        *--end = '\0';
    }
    return contents;
}

static gboolean
secure_file_ready(const char *path)
{
    struct stat info;
    return lstat(path, &info) == 0 && S_ISREG(info.st_mode) &&
           info.st_uid == geteuid() && (info.st_mode & 0077) == 0 &&
           info.st_size > 0 && info.st_size <= 4096;
}

static gboolean
remove_boot_file(const char *path)
{
    if (unlink(path) == 0 || errno == ENOENT) {
        return TRUE;
    }
    g_printerr("native_host: could not remove stale %s: %s\n", path, strerror(errno));
    return FALSE;
}

static gboolean
parse_server_port(const char *text, guint16 *parsed_port)
{
    if (!text || !text[0]) {
        return FALSE;
    }
    errno = 0;
    char *end = NULL;
    unsigned long value = strtoul(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || value < 1 || value > 65535) {
        return FALSE;
    }
    *parsed_port = (guint16)value;
    return TRUE;
}

static gboolean
token_is_valid(const char *value)
{
    if (!value) {
        return FALSE;
    }
    gsize length = strlen(value);
    if (length < 16 || length > 256) {
        return FALSE;
    }
    for (const unsigned char *cursor = (const unsigned char *)value;
         *cursor != '\0'; cursor++) {
        if (!g_ascii_isalnum(*cursor) && *cursor != '-' && *cursor != '_') {
            return FALSE;
        }
    }
    return TRUE;
}

static void
native_request_clear(NativeRequest *request)
{
    if (!request) {
        return;
    }
    g_clear_pointer(&request->value, g_free);
    request->kind = NATIVE_REQUEST_NONE;
}

static gboolean
native_request_copy(NativeRequest *destination, const NativeRequest *source)
{
    if (!destination || !source) {
        return FALSE;
    }
    native_request_clear(destination);
    destination->kind = source->kind;
    destination->value = source->value ? g_strdup(source->value) : NULL;
    return !source->value || destination->value != NULL;
}

static gboolean
native_text_is_valid(const char *value, gsize length, gboolean allow_empty)
{
    if (!value || (!allow_empty && length == 0) || length > MAX_NATIVE_VALUE_BYTES ||
        !g_utf8_validate(value, (gssize)length, NULL)) {
        return FALSE;
    }
    for (gsize index = 0; index < length; index++) {
        unsigned char character = (unsigned char)value[index];
        if (character == '\0' || character < 0x20 || character == 0x7F) {
            return FALSE;
        }
    }
    return TRUE;
}

static gboolean
native_text_string_is_valid(const char *value, gboolean allow_empty)
{
    return value && native_text_is_valid(value, strlen(value), allow_empty);
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

static char *
native_uri_unescape(const char *value)
{
    if (!native_text_string_is_valid(value, TRUE)) {
        return NULL;
    }
    GString *decoded = g_string_sized_new(strlen(value));
    for (gsize index = 0; value[index] != '\0'; index++) {
        if (value[index] == '%') {
            if (value[index + 1] == '\0' || value[index + 2] == '\0') {
                g_string_free(decoded, TRUE);
                return NULL;
            }
            int high = hex_value((unsigned char)value[index + 1]);
            int low = hex_value((unsigned char)value[index + 2]);
            if (high < 0 || low < 0) {
                g_string_free(decoded, TRUE);
                return NULL;
            }
            g_string_append_c(decoded, (char)((high << 4) | low));
            index += 2;
        } else {
            g_string_append_c(decoded, value[index]);
        }
        if (decoded->len > MAX_NATIVE_VALUE_BYTES) {
            g_string_free(decoded, TRUE);
            return NULL;
        }
    }
    if (!native_text_is_valid(decoded->str, decoded->len, TRUE)) {
        g_string_free(decoded, TRUE);
        return NULL;
    }
    return g_string_free(decoded, FALSE);
}

static gboolean
native_request_set_value(NativeRequest *request, NativeRequestKind kind,
                         char *value, gboolean allow_empty)
{
    if (!request || !value) {
        g_free(value);
        return FALSE;
    }
    if (!native_text_string_is_valid(value, allow_empty)) {
        g_free(value);
        return FALSE;
    }
    native_request_clear(request);
    request->kind = kind;
    request->value = value;
    return TRUE;
}

static gboolean
native_request_set_uri_value(NativeRequest *request, NativeRequestKind kind,
                             const char *raw_value, gboolean trim,
                             gboolean allow_empty)
{
    char *value = native_uri_unescape(raw_value ? raw_value : "");
    if (!value) {
        return FALSE;
    }
    if (trim) {
        g_strstrip(value);
    }
    return native_request_set_value(request, kind, value, allow_empty);
}

static gboolean
native_request_from_uri(const char *uri, NativeRequest *request)
{
    NativeRequest parsed = { NATIVE_REQUEST_NONE, NULL };
    char *text = uri ? g_strdup(uri) : NULL;
    if (!text) {
        return FALSE;
    }
    g_strstrip(text);
    if (!native_text_string_is_valid(text, FALSE)) {
        g_free(text);
        return FALSE;
    }

    const char *route = NULL;
    if (g_str_has_prefix(text, "openbox://")) {
        const char *rest = text + strlen("openbox://");
        const char *slash = strchr(rest, '/');
        if (slash) {
            char *authority = g_strndup(rest, (gsize)(slash - rest));
            gboolean local_authority = authority[0] == '\0' ||
                                       g_ascii_strcasecmp(authority, "localhost") == 0 ||
                                       g_ascii_strcasecmp(authority, "openbox") == 0;
            gboolean foreign_authority = strchr(authority, '.') != NULL ||
                                         strchr(authority, ':') != NULL;
            if (!local_authority && foreign_authority) {
                g_free(authority);
                g_free(text);
                return FALSE;
            }
            route = local_authority ? slash + 1 : rest;
            g_free(authority);
        } else {
            gboolean local_authority = rest[0] == '\0' ||
                                       g_ascii_strcasecmp(rest, "localhost") == 0 ||
                                       g_ascii_strcasecmp(rest, "openbox") == 0;
            if ((!local_authority && strchr(rest, '.') != NULL) ||
                (!local_authority && strchr(rest, ':') != NULL)) {
                g_free(text);
                return FALSE;
            }
            route = local_authority ? "" : rest;
        }
    } else if (g_str_has_prefix(text, "openbox:")) {
        route = text + strlen("openbox:");
    } else {
        g_free(text);
        return FALSE;
    }

    while (route[0] == '/') {
        route++;
    }
    char *route_copy = g_strdup(route);
    char *separator = strchr(route_copy, '/');
    char *remainder = "";
    if (separator) {
        *separator = '\0';
        remainder = separator + 1;
    }

    if (g_ascii_strcasecmp(route_copy, "start") == 0 || route_copy[0] == '\0') {
        parsed.kind = NATIVE_REQUEST_START;
    } else if (g_ascii_strcasecmp(route_copy, "showgame") == 0 ||
               g_ascii_strcasecmp(route_copy, "game") == 0) {
        if (!native_request_set_uri_value(&parsed, NATIVE_REQUEST_SHOWGAME,
                                          remainder, TRUE, FALSE)) {
            goto invalid;
        }
    } else if (g_ascii_strcasecmp(route_copy, "search") == 0) {
        if (!native_request_set_uri_value(&parsed, NATIVE_REQUEST_SEARCH,
                                          remainder, FALSE, TRUE)) {
            goto invalid;
        }
    } else if (g_ascii_strcasecmp(route_copy, "launch") == 0) {
        if (!native_request_set_uri_value(&parsed, NATIVE_REQUEST_LAUNCH,
                                          remainder, TRUE, FALSE)) {
            goto invalid;
        }
    } else if (g_ascii_strcasecmp(route_copy, "resume") == 0) {
        if (!native_request_set_uri_value(&parsed, NATIVE_REQUEST_RESUME,
                                          remainder, TRUE, FALSE)) {
            goto invalid;
        }
    } else if (g_ascii_strcasecmp(route_copy, "moment") == 0) {
        if (!native_request_set_uri_value(&parsed, NATIVE_REQUEST_MOMENT,
                                          remainder, TRUE, FALSE)) {
            goto invalid;
        }
    } else if (g_ascii_strcasecmp(route_copy, "clip") == 0) {
        if (!native_request_set_uri_value(&parsed, NATIVE_REQUEST_CLIP,
                                          remainder, TRUE, FALSE)) {
            goto invalid;
        }
    } else if (g_ascii_strcasecmp(route_copy, "bigbox") == 0 ||
               g_ascii_strcasecmp(route_copy, "fullscreen") == 0) {
        parsed.kind = NATIVE_REQUEST_BIGBOX;
    } else if (g_ascii_strcasecmp(route_copy, "settings") == 0) {
        parsed.kind = NATIVE_REQUEST_SETTINGS;
    } else {
        goto invalid;
    }

    native_request_clear(request);
    *request = parsed;
    g_free(route_copy);
    g_free(text);
    return TRUE;

invalid:
    native_request_clear(&parsed);
    g_free(route_copy);
    g_free(text);
    return FALSE;
}

static gboolean
native_request_set_play(NativeRequest *request, const char *value)
{
    char *game_id = value ? g_strdup(value) : NULL;
    if (!game_id) {
        return FALSE;
    }
    g_strstrip(game_id);
    return native_request_set_value(request, NATIVE_REQUEST_LAUNCH, game_id, FALSE);
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
        if (g_str_has_prefix(argv[index], "openbox:")) {
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
        return g_strdup("openbox://start");
    }
    const char *action = native_request_action(request->kind);
    if (request->kind == NATIVE_REQUEST_LAUNCH) {
        action = "launch";
    }
    if (!action) {
        return NULL;
    }
    char *escaped = request->value
        ? g_uri_escape_string(request->value, "", FALSE)
        : g_strdup("");
    if (!escaped) {
        return NULL;
    }
    char *result = g_strdup_printf("openbox://%s/%s", action, escaped);
    g_free(escaped);
    return result;
}

static char *
native_authenticated_url(const NativeRequest *request)
{
    if (!origin || !token) {
        return NULL;
    }
    char *base = g_strdup_printf("%s/?token=%s", origin, token);
    if (!request || request->kind == NATIVE_REQUEST_NONE ||
        request->kind == NATIVE_REQUEST_START) {
        return base;
    }

    const char *action = native_request_action(request->kind);
    char *escaped = request->value
        ? g_uri_escape_string(request->value, "", FALSE)
        : g_strdup("");
    if (!action || !escaped) {
        g_free(escaped);
        g_free(base);
        return NULL;
    }
    const char *parameter = strcmp(action, "search") == 0 ? "q" : "id";
    char *result = NULL;
    if (request->kind == NATIVE_REQUEST_BIGBOX ||
        request->kind == NATIVE_REQUEST_SETTINGS) {
        result = g_strdup_printf("%s&deeplink=%s", base, action);
    } else {
        result = g_strdup_printf("%s&deeplink=%s&%s=%s",
                                 base, action, parameter, escaped);
    }
    g_free(escaped);
    g_free(base);
    return result;
}

static char *
json_escape_string(const char *value)
{
    GString *escaped = g_string_new(NULL);
    for (const unsigned char *cursor = (const unsigned char *)value;
         cursor && *cursor != '\0'; cursor++) {
        switch (*cursor) {
        case '"':
            g_string_append(escaped, "\\\"");
            break;
        case '\\':
            g_string_append(escaped, "\\\\");
            break;
        case '\b':
            g_string_append(escaped, "\\b");
            break;
        case '\f':
            g_string_append(escaped, "\\f");
            break;
        case '\n':
            g_string_append(escaped, "\\n");
            break;
        case '\r':
            g_string_append(escaped, "\\r");
            break;
        case '\t':
            g_string_append(escaped, "\\t");
            break;
        default:
            if (*cursor < 0x20) {
                g_string_append_printf(escaped, "\\u%04x", *cursor);
            } else {
                g_string_append_c(escaped, (char)*cursor);
            }
            break;
        }
    }
    return g_string_free(escaped, FALSE);
}

static gboolean
write_all_fd(int fd, const char *data, gsize length)
{
    gsize written = 0;
    while (written < length) {
        ssize_t count = send(fd, data + written, length - written, MSG_NOSIGNAL);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            return FALSE;
        }
        written += (gsize)count;
    }
    return TRUE;
}

static gboolean
api_post_json(const char *path, const char *body)
{
    if (!path || !body || !token || server_port == 0) {
        return FALSE;
    }
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        g_printerr("native_host: could not create loopback API socket: %s\n",
                   strerror(errno));
        return FALSE;
    }
    struct timeval timeout = { API_TIMEOUT_SECONDS, 0 };
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));

    struct sockaddr_in address;
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_port = htons(server_port);
    if (inet_pton(AF_INET, "127.0.0.1", &address.sin_addr) != 1 ||
        connect(fd, (struct sockaddr *)&address, sizeof(address)) < 0) {
        g_printerr("native_host: could not connect to the loopback API: %s\n",
                   strerror(errno));
        close(fd);
        return FALSE;
    }

    char *request = g_strdup_printf(
        "POST %s HTTP/1.1\r\n"
        "Host: 127.0.0.1:%u\r\n"
        "X-OpenBox-Token: %s\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: %zu\r\n"
        "Connection: close\r\n\r\n%s",
        path, server_port, token, strlen(body), body);
    gboolean sent = write_all_fd(fd, request, strlen(request));
    g_free(request);
    if (!sent) {
        g_printerr("native_host: could not send loopback API request: %s\n",
                   strerror(errno));
        close(fd);
        return FALSE;
    }

    char response[128];
    gsize used = 0;
    while (used + 1 < sizeof(response)) {
        ssize_t count = read(fd, response + used, sizeof(response) - used - 1);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            break;
        }
        used += (gsize)count;
        if (memchr(response, '\n', used) != NULL) {
            break;
        }
    }
    response[used] = '\0';
    int status = 0;
    gboolean ok = sscanf(response, "HTTP/%*s %d", &status) == 1 &&
                  status >= 200 && status < 300;
    if (!ok) {
        g_printerr("native_host: loopback API request failed (HTTP %d)\n", status);
    }
    close(fd);
    return ok;
}

static gboolean
native_request_is_decimal(const char *value)
{
    if (!value || !value[0]) {
        return FALSE;
    }
    for (const unsigned char *cursor = (const unsigned char *)value;
         *cursor != '\0'; cursor++) {
        if (*cursor < '0' || *cursor > '9') {
            return FALSE;
        }
    }
    return TRUE;
}

static char *
native_request_api_body(const char *value)
{
    if (native_request_is_decimal(value)) {
        const char *first_nonzero = value;
        while (first_nonzero[0] == '0' && first_nonzero[1] != '\0') {
            first_nonzero++;
        }
        return g_strdup_printf("{\"id\":%s}", first_nonzero);
    }
    char *escaped = json_escape_string(value);
    char *body = g_strdup_printf("{\"game_id\":\"%s\"}", escaped);
    g_free(escaped);
    return body;
}

static gboolean
dispatch_native_api_request(const NativeRequest *request)
{
    if (!request || !request->value) {
        return FALSE;
    }
    const char *path = request->kind == NATIVE_REQUEST_RESUME
        ? "/api/v2/resume"
        : "/api/launch";
    char *body = native_request_api_body(request->value);
    gboolean ok = api_post_json(path, body);
    g_free(body);
    return ok;
}

static void stop_server(void);
static gboolean
boot_files_ready(const char *port_file, const char *token_file)
{
    return secure_file_ready(port_file) && secure_file_ready(token_file);
}

static gboolean
wait_for_boot_files(const char *port_file, const char *token_file)
{
    gint64 deadline = g_get_monotonic_time() + ((gint64)BOOT_TIMEOUT_SECONDS * G_USEC_PER_SEC);
    int notify_fd = inotify_init1(IN_NONBLOCK | IN_CLOEXEC);
    int watch_fd = -1;
    if (notify_fd >= 0) {
        watch_fd = inotify_add_watch(
            notify_fd,
            data_dir,
            IN_ATTRIB | IN_CLOSE_WRITE | IN_CREATE | IN_MOVED_TO
        );
    }

    while (g_get_monotonic_time() < deadline) {
        if (boot_files_ready(port_file, token_file)) {
            if (notify_fd >= 0) close(notify_fd);
            return TRUE;
        }
        int child_status = 0;
        pid_t child = waitpid(server_pid, &child_status, WNOHANG);
        if (child == server_pid) {
            server_pid = 0;
            if (notify_fd >= 0) close(notify_fd);
            return FALSE;
        }


        gint64 remaining = deadline - g_get_monotonic_time();
        if (notify_fd >= 0 && watch_fd >= 0) {
            struct pollfd pfd = { .fd = notify_fd, .events = POLLIN, .revents = 0 };
            int timeout_ms = (int)CLAMP(remaining / 1000, 1, 50);
            if (poll(&pfd, 1, timeout_ms) > 0 && (pfd.revents & POLLIN)) {
                char buffer[4096];
                while (read(notify_fd, buffer, sizeof(buffer)) > 0) {
                }
            }
        } else {
            g_usleep((gulong)MIN(remaining, 2 * 1000));
        }
    }

    if (notify_fd >= 0) close(notify_fd);
    return boot_files_ready(port_file, token_file);
}


static gboolean
boot_server(void)
{
    char *port_file = g_build_filename(data_dir, "server.port", NULL);
    char *token_file = g_build_filename(data_dir, "server.token", NULL);

    /* Never accept a pair left behind by a crashed or interrupted server. */
    if (!remove_boot_file(port_file) || !remove_boot_file(token_file)) {
        g_free(port_file);
        g_free(token_file);
        return FALSE;
    }

    server_pid = fork();
    if (server_pid < 0) {
        g_printerr("native_host: fork failed: %s\n", strerror(errno));
        g_free(port_file);
        g_free(token_file);
        return FALSE;
    }

    if (server_pid == 0) {
        /* Child: run the web server without a browser. */
        setpgid(0, 0);
        const char *web_app = web_app_path;
        if (!web_app || !path_exists(web_app)) {
            g_printerr("native_host: OPENBOX_WEB_APP must point at web_app.py\n");
            _exit(127);
        }
        execlp(python_path, python_path, "-B", web_app, "--no-browser", (char *)NULL);
        g_printerr("native_host: failed to exec %s: %s\n", web_app, strerror(errno));
        _exit(127);
    }

    /* Parent: wait for the server to publish its port and token. */
    if (!wait_for_boot_files(port_file, token_file)) {
        g_printerr("native_host: server did not boot within %d seconds\n", BOOT_TIMEOUT_SECONDS);
        stop_server();
        g_free(port_file);
        g_free(token_file);
        return FALSE;
    }

    char *port = read_secure_trimmed_file(port_file);
    token = read_secure_trimmed_file(token_file);
    g_free(port_file);
    g_free(token_file);

    if (!port || !token || !parse_server_port(port, &server_port) || !token_is_valid(token)) {
        g_printerr("native_host: could not read server.port or server.token\n");
        stop_server();
        g_free(port);
        g_clear_pointer(&token, g_free);
        return FALSE;
    }
    origin = g_strdup_printf("http://127.0.0.1:%u", server_port);
    g_free(port);
    return TRUE;
}

static void
stop_server(void)
{
    if (server_pid <= 0) {
        return;
    }
    kill(-server_pid, SIGTERM);
    int status = 0;
    for (int i = 0; i < 20; i++) {
        pid_t result = waitpid(server_pid, &status, WNOHANG);
        if (result == server_pid) {
            break;
        }
        g_usleep(100 * 1000);
    }
    /* Backstop: force kill if it is still alive. */
    if (kill(-server_pid, 0) == 0) {
        kill(-server_pid, SIGKILL);
        waitpid(server_pid, &status, 0);
    }
    server_pid = 0;

    if (data_dir) {
        char *port_file = g_build_filename(data_dir, "server.port", NULL);
        char *token_file = g_build_filename(data_dir, "server.token", NULL);
        unlink(port_file);
        unlink(token_file);
        g_free(port_file);
        g_free(token_file);
    }
}

static void
present_main_window(void)
{
    if (main_window) {
        gtk_widget_show(main_window);
        gtk_window_present(GTK_WINDOW(main_window));
    }
}

static void
dispatch_native_request(const NativeRequest *request)
{
    if (!request || request->kind == NATIVE_REQUEST_NONE) {
        return;
    }
    if (!main_view) {
        native_request_copy(&pending_request, request);
        return;
    }

    present_main_window();
    if (request->kind == NATIVE_REQUEST_LAUNCH ||
        request->kind == NATIVE_REQUEST_RESUME) {
        /* Keep --play/launch semantics identical to parity_deeplinks: the
         * game starts through the authenticated API, then the page lands on
         * the corresponding UI deeplink in the native window. */
        dispatch_native_api_request(request);
    }
    char *url = native_authenticated_url(request);
    if (!url) {
        g_printerr("native_host: could not build the authenticated deeplink\n");
        return;
    }
    webkit_web_view_load_uri(main_view, url);
    g_free(url);
}

static gboolean
read_ipc_line(int fd, char *buffer, gsize capacity)
{
    if (!buffer || capacity < 2) {
        return FALSE;
    }
    gsize used = 0;
    while (used + 1 < capacity) {
        ssize_t count = read(fd, buffer + used, capacity - used - 1);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            return FALSE;
        }
        for (ssize_t index = 0; index < count; index++) {
            unsigned char character = (unsigned char)buffer[used + (gsize)index];
            if (character == '\n') {
                if ((gsize)index != (gsize)count - 1) {
                    return FALSE;
                }
                buffer[used + (gsize)index] = '\0';
                return TRUE;
            }
            if (character == '\0' || character < 0x20 || character == 0x7F) {
                return FALSE;
            }
        }
        used += (gsize)count;
    }
    return FALSE;
}

static gboolean
on_socket_connection(GIOChannel *source, GIOCondition cond, gpointer data)
{
    (void)cond;
    (void)data;
    int client = accept(g_io_channel_unix_get_fd(source), NULL, NULL);
    if (client >= 0) {
        char message[MAX_IPC_MESSAGE_BYTES];
        if (read_ipc_line(client, message, sizeof(message))) {
            if (strcmp(message, "focus") == 0) {
                if (main_window) {
                    present_main_window();
                } else {
                    pending_focus = TRUE;
                }
            } else if (g_str_has_prefix(message, "deeplink ")) {
                NativeRequest request = { NATIVE_REQUEST_NONE, NULL };
                const char *uri = message + strlen("deeplink ");
                if (native_request_from_uri(uri, &request)) {
                    dispatch_native_request(&request);
                } else {
                    g_printerr("native_host: rejected invalid single-instance deeplink\n");
                }
                native_request_clear(&request);
            } else {
                g_printerr("native_host: rejected invalid single-instance message\n");
            }
        }
        close(client);
    }
    return TRUE;  // keep watching
}

static gboolean
send_single_instance_request(int fd, const NativeRequest *request)
{
    if (!request || request->kind == NATIVE_REQUEST_NONE) {
        return write_all_fd(fd, "focus\n", strlen("focus\n"));
    }
    char *uri = native_request_to_uri(request);
    if (!uri) {
        return FALSE;
    }
    char *message = g_strdup_printf("deeplink %s\n", uri);
    gboolean sent = write_all_fd(fd, message, strlen(message));
    g_free(message);
    g_free(uri);
    return sent;
}

static gboolean
acquire_single_instance(const NativeRequest *request)
{
    char *sock_path = g_build_filename(data_dir, "openbox.sock", NULL);
    int sock_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sock_fd < 0) {
        g_free(sock_path);
        return TRUE;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", sock_path);

    if (connect(sock_fd, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
        send_single_instance_request(sock_fd, request);
        close(sock_fd);
        g_free(sock_path);
        return FALSE;
    }

    unlink(sock_path);

    if (bind(sock_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(sock_fd);
        g_free(sock_path);
        return TRUE;
    }
    if (listen(sock_fd, 1) < 0) {
        close(sock_fd);
        g_free(sock_path);
        return TRUE;
    }
    chmod(sock_path, 0600);
    server_sock_fd = sock_fd;

    GIOChannel *channel = g_io_channel_unix_new(sock_fd);
    socket_watch_id = g_io_add_watch(channel, G_IO_IN, on_socket_connection, NULL);
    g_io_channel_unref(channel);

    g_free(sock_path);
    return TRUE;
}

static void
release_single_instance(void)
{
    if (socket_watch_id > 0) {
        g_source_remove(socket_watch_id);
        socket_watch_id = 0;
    }
    if (server_sock_fd >= 0) {
        close(server_sock_fd);
        server_sock_fd = -1;
    }
    if (data_dir) {
        char *sock_path = g_build_filename(data_dir, "openbox.sock", NULL);
        unlink(sock_path);
        g_free(sock_path);
    }
}

static void
load_geometry(void)
{
    geometry_path = g_build_filename(data_dir, "window-geometry", NULL);
    GError *error = NULL;
    char *contents = NULL;
    if (g_file_get_contents(geometry_path, &contents, NULL, &error) && contents) {
        int w, h, maximized;
        if (sscanf(contents, "%d %d %d", &w, &h, &maximized) == 3) {
            if (w >= 400 && w <= 8000 && h >= 300 && h <= 8000) {
                default_width = w;
                default_height = h;
            }
            window_maximized = maximized != 0;
        }
        g_free(contents);
    }
    if (error) {
        g_error_free(error);
    }
}

static void
save_geometry(void)
{
    if (!geometry_path || !main_window) {
        return;
    }
    if (gtk_window_is_maximized(GTK_WINDOW(main_window))) {
        window_maximized = TRUE;
    }
    int w = 0, h = 0;
    gtk_window_get_size(GTK_WINDOW(main_window), &w, &h);
    if (w >= 400 && h >= 300) {
        char *contents = g_strdup_printf("%d %d %d\n", w, h, window_maximized ? 1 : 0);
        GError *error = NULL;
        g_file_set_contents(geometry_path, contents, -1, &error);
        if (error) {
            g_error_free(error);
        }
        g_free(contents);
    }
}
static void
load_tray_flags(void)
{
    /* web_app.py writes "tray_enabled minimize_to_tray" at boot, owner-only. */
    char *flags_path = g_build_filename(data_dir, "native-host-flags", NULL);
    GError *error = NULL;
    char *contents = NULL;
    if (g_file_get_contents(flags_path, &contents, NULL, &error) && contents) {
        int enabled, minimize;
        if (sscanf(contents, "%d %d", &enabled, &minimize) == 2) {
            tray_enabled = enabled != 0;
            minimize_to_tray = minimize != 0;
        }
        g_free(contents);
    }
    if (error) {
        g_error_free(error);
    }
    g_free(flags_path);
}

static void
on_tray_activate(GtkStatusIcon *icon, gpointer user_data)
{
    (void)icon;
    (void)user_data;
    if (main_window) {
        gtk_widget_show(main_window);
        gtk_window_present(GTK_WINDOW(main_window));
    }
}

static void
setup_tray(void)
{
    if (!tray_enabled) {
        return;
    }
    G_GNUC_BEGIN_IGNORE_DEPRECATIONS
    if (application_icon_path) {
        tray_icon = gtk_status_icon_new_from_file(application_icon_path);
    }
    if (!tray_icon) {
        tray_icon = gtk_status_icon_new_from_icon_name("io.openbox.GameLauncher");
    }
    if (!tray_icon) {
        tray_icon = gtk_status_icon_new();
    }
    gtk_status_icon_set_title(tray_icon, "OpenBox Game Launcher");
    gtk_status_icon_set_tooltip_text(tray_icon, "OpenBox Game Launcher");
    g_signal_connect(tray_icon, "activate", G_CALLBACK(on_tray_activate), NULL);
    G_GNUC_END_IGNORE_DEPRECATIONS
}

static gboolean
on_window_delete(GtkWidget *widget, GdkEvent *event, gpointer user_data)
{
    (void)event;
    (void)user_data;
    if (minimize_to_tray && tray_icon) {
        gtk_widget_hide(widget);
        return TRUE; /* consume the delete so the window hides instead of closing */
    }
    return FALSE;
}

static void
evaluate(WebKitWebView *view, const char *script)
{
    webkit_web_view_evaluate_javascript(view, script, -1, NULL, NULL, NULL, NULL, NULL);
}

static void
resolve_bridge(WebKitWebView *view, const char *id, const char *result_json)
{
    /*
     * Invariant: result_json must be valid JS/JSON produced only by callers
     * in this file (literal objects or g_strescape'd values), never raw
     * attacker data — it is spliced verbatim into the page. Cheap defensive
     * guard: if a future caller ever threads untrusted text through here,
     * reject it instead of evaluating it.
     */
    if (!result_json || strstr(result_json, ";") || strstr(result_json, "//") ||
        strstr(result_json, "*/")) {
        g_printerr("native_host: resolve_bridge rejected suspicious result payload\n");
        return;
    }
    /* result_json is valid JS verbatim; only id needs escaping. */
    char *escaped_id = g_strescape(id, NULL);
    char *script = g_strdup_printf(
        "window.__openboxResolve && window.__openboxResolve('%s', %s);",
        escaped_id, result_json);
    evaluate(view, script);
    g_free(script);
    g_free(escaped_id);
}

static void
handle_dialog(WebKitWebView *view, const char *id, JSCValue *args)
{
    /* args: {kind, title, filters, default} */
    GtkFileChooserAction action = GTK_FILE_CHOOSER_ACTION_SELECT_FOLDER;
    JSCValue *kind = jsc_value_object_get_property(args, "kind");
    if (kind && jsc_value_is_string(kind)) {
        char *kind_str = jsc_value_to_string(kind);
        if (kind_str && strcmp(kind_str, "file") == 0) {
            action = GTK_FILE_CHOOSER_ACTION_OPEN;
        } else if (kind_str && strcmp(kind_str, "save") == 0) {
            action = GTK_FILE_CHOOSER_ACTION_SAVE;
        }
        g_free(kind_str);
    }

    GtkFileChooserNative *dialog = gtk_file_chooser_native_new(
        "OpenBox", GTK_WINDOW(main_window), action, "Select", "Cancel");

    GtkFileFilter *filter = gtk_file_filter_new();
    gtk_file_filter_add_pattern(filter, "*");
    gtk_file_filter_set_name(filter, "All files");
    gtk_file_chooser_add_filter(GTK_FILE_CHOOSER(dialog), filter);

    gint result = gtk_native_dialog_run(GTK_NATIVE_DIALOG(dialog));
    char *result_json = NULL;
    if (result == GTK_RESPONSE_ACCEPT) {
        char *filename = gtk_file_chooser_get_filename(GTK_FILE_CHOOSER(dialog));
        if (filename) {
            char *escaped = g_strescape(filename, NULL);
            char *quoted = g_strdup_printf("\"%s\"", escaped);
            result_json = g_strdup_printf("{\"path\":%s,\"cancelled\":false}", quoted);
            g_free(quoted);
            g_free(escaped);
            g_free(filename);
        } else {
            result_json = g_strdup("{\"path\":null,\"cancelled\":false}");
        }
    } else {
        result_json = g_strdup("{\"path\":null,\"cancelled\":true}");
    }
    gtk_native_dialog_destroy(GTK_NATIVE_DIALOG(dialog));
    resolve_bridge(view, id, result_json);
    g_free(result_json);
}

static gboolean
uri_scheme_allowed(const char *uri)
{
    if (!uri) {
        return FALSE;
    }
    /* Reject control characters (chars < 0x20 or 0x7F) outright. */
    for (const unsigned char *p = (const unsigned char *)uri; *p != '\0'; p++) {
        if (*p < 0x20 || *p == 0x7F) {
            return FALSE;
        }
    }
    GUri *parsed = g_uri_parse(uri, G_URI_FLAGS_NONE, NULL);
    if (!parsed) {
        return FALSE;
    }
    const char *scheme = g_uri_get_scheme(parsed);
    const char *host = g_uri_get_host(parsed);
    const char *userinfo = g_uri_get_userinfo(parsed);
    gboolean ok = scheme && host && host[0] != '\0' && !userinfo &&
                  (g_ascii_strcasecmp(scheme, "http") == 0 ||
                   g_ascii_strcasecmp(scheme, "https") == 0);
    g_uri_unref(parsed);
    return ok;
}

static void
handle_open_external(WebKitWebView *view, const char *id, JSCValue *args)
{
    JSCValue *target = jsc_value_object_get_property(args, "target");
    char *target_str = target ? jsc_value_to_string(target) : NULL;
    gboolean ok = FALSE;
    if (target_str) {
        if (!uri_scheme_allowed(target_str)) {
            /* Security: never hand a non-allowlisted URI to the default handler. */
            g_printerr("native_host: open external rejected scheme in '%s'\n", target_str);
        } else {
            GError *error = NULL;
            ok = g_app_info_launch_default_for_uri(target_str, NULL, &error);
            if (error) {
                g_printerr("native_host: open external failed: %s\n", error->message);
                g_error_free(error);
            }
        }
    }
    char *result_json = g_strdup_printf("{\"ok\":%s}", ok ? "true" : "false");
    resolve_bridge(view, id, result_json);
    g_free(result_json);
    g_free(target_str);
}

static gboolean
path_is_under(const char *path, const char *base)
{
    /* Both args are canonicalized absolute paths; base must be absolute. */
    gsize base_len = strlen(base);
    return g_str_has_prefix(path, base) &&
           (path[base_len] == '\0' || path[base_len] == G_DIR_SEPARATOR);
}

static void
handle_reveal(WebKitWebView *view, const char *id, JSCValue *args)
{
    JSCValue *path = jsc_value_object_get_property(args, "path");
    char *path_str = path ? jsc_value_to_string(path) : NULL;
    const char *home = g_get_home_dir();
    gboolean ok = FALSE;
    if (path_str) {
        if (!g_path_is_absolute(path_str)) {
            /* Security: never resolve a relative path against our cwd. */
            g_printerr("native_host: reveal rejected relative path '%s'\n", path_str);
        } else {
            char *canon = g_canonicalize_filename(path_str, NULL);
            if (!path_is_under(canon, data_dir) && !path_is_under(canon, home)) {
                /* Security: only reveal files under the data dir or the user's home dir. */
                g_printerr("native_host: reveal rejected path outside data/home dirs: '%s'\n",
                           canon);
            } else if (path_exists(canon)) {
                /* Security: reveal the containing folder only; never launch the path itself. */
                char *dir = g_path_get_dirname(canon);
                GError *error = NULL;
                char *dir_uri = g_filename_to_uri(dir, NULL, &error);
                ok = dir_uri ? g_app_info_launch_default_for_uri(dir_uri, NULL, &error) : FALSE;
                if (error) {
                    g_printerr("native_host: reveal failed: %s\n", error->message);
                    g_error_free(error);
                }
                g_free(dir_uri);
                g_free(dir);
            }
            g_free(canon);
        }
    }
    char *result_json = g_strdup_printf("{\"ok\":%s}", ok ? "true" : "false");
    resolve_bridge(view, id, result_json);
    g_free(result_json);
    g_free(path_str);
}

static void
handle_window(WebKitWebView *view, const char *id, JSCValue *args)
{
    JSCValue *action = jsc_value_object_get_property(args, "action");
    char *action_str = action ? jsc_value_to_string(action) : NULL;
    if (action_str) {
        if (strcmp(action_str, "minimize") == 0) {
            gtk_window_iconify(GTK_WINDOW(main_window));
        } else if (strcmp(action_str, "toggle-maximize") == 0) {
            if (gtk_window_is_maximized(GTK_WINDOW(main_window))) {
                gtk_window_unmaximize(GTK_WINDOW(main_window));
            } else {
                gtk_window_maximize(GTK_WINDOW(main_window));
            }
        } else if (strcmp(action_str, "set-fullscreen") == 0) {
            gtk_window_fullscreen(GTK_WINDOW(main_window));
        } else if (strcmp(action_str, "unset-fullscreen") == 0) {
            gtk_window_unfullscreen(GTK_WINDOW(main_window));
        } else if (strcmp(action_str, "close") == 0) {
            gtk_widget_destroy(main_window);
        }
    }
    resolve_bridge(view, id, "{\"ok\":true}");
    g_free(action_str);
}

static gboolean
uri_matches_expected_origin(const char *uri)
{
    if (!uri || !origin) {
        return FALSE;
    }
    GUri *parsed = g_uri_parse(uri, G_URI_FLAGS_NONE, NULL);
    GUri *expected = g_uri_parse(origin, G_URI_FLAGS_NONE, NULL);
    if (!parsed || !expected) {
        if (parsed) {
            g_uri_unref(parsed);
        }
        if (expected) {
            g_uri_unref(expected);
        }
        return FALSE;
    }
    const char *scheme = g_uri_get_scheme(parsed);
    const char *expected_scheme = g_uri_get_scheme(expected);
    const char *host = g_uri_get_host(parsed);
    const char *expected_host = g_uri_get_host(expected);
    const char *userinfo = g_uri_get_userinfo(parsed);
    gboolean ok = scheme && expected_scheme && host && expected_host &&
                  !userinfo &&
                  g_ascii_strcasecmp(scheme, expected_scheme) == 0 &&
                  g_ascii_strcasecmp(host, expected_host) == 0 &&
                  g_uri_get_port(parsed) == g_uri_get_port(expected);
    g_uri_unref(parsed);
    g_uri_unref(expected);
    return ok;
}

static gboolean
is_expected_origin(WebKitWebView *view)
{
    return uri_matches_expected_origin(webkit_web_view_get_uri(view));
}

static gboolean
decide_policy(WebKitWebView *view, WebKitPolicyDecision *decision,
              WebKitPolicyDecisionType type, gpointer user_data)
{
    (void)view;
    (void)user_data;
    switch (type) {
    case WEBKIT_POLICY_DECISION_TYPE_NAVIGATION_ACTION: {
        WebKitNavigationPolicyDecision *nav =
            WEBKIT_NAVIGATION_POLICY_DECISION(decision);
        WebKitNavigationAction *action =
            webkit_navigation_policy_decision_get_navigation_action(nav);
        WebKitURIRequest *request =
            action ? webkit_navigation_action_get_request(action) : NULL;
        if (!uri_matches_expected_origin(request ? webkit_uri_request_get_uri(request) : NULL)) {
            webkit_policy_decision_ignore(decision);
            return TRUE;
        }
        return FALSE;
    }
    case WEBKIT_POLICY_DECISION_TYPE_NEW_WINDOW_ACTION:
        webkit_policy_decision_ignore(decision);
        return TRUE;
    case WEBKIT_POLICY_DECISION_TYPE_RESPONSE: {
        WebKitResponsePolicyDecision *resp =
            WEBKIT_RESPONSE_POLICY_DECISION(decision);
        WebKitURIResponse *response = webkit_response_policy_decision_get_response(resp);
        if (!uri_matches_expected_origin(response ? webkit_uri_response_get_uri(response) : NULL)) {
            webkit_policy_decision_ignore(decision);
            return TRUE;
        }
        return FALSE;
    }
    default:
        return FALSE;
    }
}

static void
message_received(WebKitUserContentManager *mgr,
                 WebKitJavascriptResult *result,
                 gpointer user_data)
{
    WebKitWebView *view = WEBKIT_WEB_VIEW(user_data);
    JSCValue *value = webkit_javascript_result_get_js_value(result);
    if (!jsc_value_is_object(value)) {
        return;
    }
    JSCValue *id_val = jsc_value_object_get_property(value, "id");
    JSCValue *method_val = jsc_value_object_get_property(value, "method");
    JSCValue *args_val = jsc_value_object_get_property(value, "args");
    char *id = jsc_value_to_string(id_val);
    char *method = jsc_value_to_string(method_val);

    if (!is_expected_origin(view)) {
        /* Security: only the exact booted app origin may drive the bridge. */
        g_printerr("native_host: rejecting bridge message from unexpected origin\n");
        if (id) {
            resolve_bridge(view, id, "{\"ok\":false}");
        }
        g_free(id);
        g_free(method);
        return;
    }

    if (id && method) {
        if (strcmp(method, "dialog") == 0) {
            handle_dialog(view, id, args_val);
        } else if (strcmp(method, "openExternal") == 0) {
            handle_open_external(view, id, args_val);
        } else if (strcmp(method, "reveal") == 0) {
            handle_reveal(view, id, args_val);
        } else if (strcmp(method, "windowAction") == 0) {
            handle_window(view, id, args_val);
        } else {
            resolve_bridge(view, id, "{\"ok\":false}");
        }
    }
    g_free(id);
    g_free(method);
}

static void
inject_bridge(WebKitWebView *view)
{
    const char *script =
        "window.__openboxPending = {};\n"
        "window.__openboxResolve = function(id, result) {\n"
        "  if (window.__openboxPending[id]) {\n"
        "    window.__openboxPending[id](result);\n"
        "    delete window.__openboxPending[id];\n"
        "  }\n"
        "};\n"
        "window.openboxNative = {\n"
        "  dialog: function(kind, opts) {\n"
        "    return new Promise(function(resolve) {\n"
        "      var id = 'd' + Date.now() + Math.random();\n"
        "      window.__openboxPending[id] = resolve;\n"
        "      window.webkit.messageHandlers.openbox.postMessage({id:id, method:'dialog', args:Object.assign({kind:kind}, opts)});\n"
        "    });\n"
        "  },\n"
        "  openExternal: function(target) {\n"
        "    return new Promise(function(resolve) {\n"
        "      var id = 'o' + Date.now() + Math.random();\n"
        "      window.__openboxPending[id] = resolve;\n"
        "      window.webkit.messageHandlers.openbox.postMessage({id:id, method:'openExternal', args:{target:target}});\n"
        "    });\n"
        "  },\n"
        "  reveal: function(path) {\n"
        "    return new Promise(function(resolve) {\n"
        "      var id = 'r' + Date.now() + Math.random();\n"
        "      window.__openboxPending[id] = resolve;\n"
        "      window.webkit.messageHandlers.openbox.postMessage({id:id, method:'reveal', args:{path:path}});\n"
        "    });\n"
        "  },\n"
        "  windowAction: function(action) {\n"
        "    return new Promise(function(resolve) {\n"
        "      var id = 'w' + Date.now() + Math.random();\n"
        "      window.__openboxPending[id] = resolve;\n"
        "      window.webkit.messageHandlers.openbox.postMessage({id:id, method:'windowAction', args:{action:action}});\n"
        "    });\n"
        "  },\n"
        "  onGamepad: function(callback) {}\n"
        "};\n"
        "true;\n";
    /* Security: top frame only so embedded iframes cannot reach the bridge. */
    webkit_user_content_manager_add_script(
        webkit_web_view_get_user_content_manager(view),
        webkit_user_script_new(script, WEBKIT_USER_CONTENT_INJECT_TOP_FRAME,
                               WEBKIT_USER_SCRIPT_INJECT_AT_DOCUMENT_START, NULL, NULL));
}

/* ------------------------------------------------------------------ */
/* GTK setup                                                           */
/* ------------------------------------------------------------------ */

static char *
icon_path_in_directory(const char *directory, const char *filename)
{
    if (!directory || !directory[0]) {
        return NULL;
    }
    char *candidate = g_build_filename(directory, filename, NULL);
    if (g_file_test(candidate, G_FILE_TEST_IS_REGULAR)) {
        return candidate;
    }
    g_free(candidate);
    return NULL;
}

static char *
resolve_application_icon_path(void)
{
    /* The launcher passes an absolute web_app.py path in every bundled
     * layout, so its directory is the most reliable source-run/install path.
     * Keeping the lookup relative to the application files also avoids a
     * dependency on the desktop environment's icon theme cache. */
    if (web_app_path) {
        char *share_dir = g_path_get_dirname(web_app_path);
        char *candidate = icon_path_in_directory(share_dir, "openbox.svg");
        g_free(share_dir);
        if (candidate) {
            return candidate;
        }
    }

    /* Direct execution from a source checkout remains useful even when the
     * caller did not launch through openbox-native.sh. */
    char *cwd = g_get_current_dir();
    char *candidate = icon_path_in_directory(cwd, "openbox.svg");
    g_free(cwd);
    if (candidate) {
        return candidate;
    }

    /* Make the native host work from an installed tree where the executable
     * and the bundled data are colocated, including custom prefixes. */
    char *executable = g_file_read_link("/proc/self/exe", NULL);
    if (executable) {
        char *executable_dir = g_path_get_dirname(executable);
        candidate = icon_path_in_directory(executable_dir, "openbox.svg");
        g_free(executable_dir);
        g_free(executable);
        if (candidate) {
            return candidate;
        }
    }

    /* Flatpak keeps the desktop icon under the standard icon data directory,
     * and update-installed desktop integration can do the same in the user's
     * data directory. */
    const char *user_data_dir = g_get_user_data_dir();
    candidate = icon_path_in_directory(
        user_data_dir,
        "icons/hicolor/scalable/apps/io.openbox.GameLauncher.svg");
    if (candidate) {
        return candidate;
    }

    const gchar * const *system_data_dirs = g_get_system_data_dirs();
    for (gsize index = 0; system_data_dirs && system_data_dirs[index]; index++) {
        candidate = icon_path_in_directory(
            system_data_dirs[index],
            "icons/hicolor/scalable/apps/io.openbox.GameLauncher.svg");
        if (candidate) {
            return candidate;
        }
    }
    return NULL;
}

static gboolean
apply_application_icon(GtkWindow *window)
{
    if (!application_icon_path) {
        gtk_window_set_icon_name(window, "io.openbox.GameLauncher");
        return FALSE;
    }

    GError *error = NULL;
    gboolean loaded = gtk_window_set_icon_from_file(
        window, application_icon_path, &error);
    if (!loaded) {
        g_printerr("native_host: could not load application icon %s: %s\n",
                   application_icon_path,
                   error ? error->message : "unknown error");
        g_clear_error(&error);
        gtk_window_set_icon_name(window, "io.openbox.GameLauncher");
    }
    return loaded;
}

static char **
gtk_argv_without_native_args(int argc, char **argv, int *filtered_argc)
{
    char **filtered = g_new0(char *, (gsize)argc + 1);
    int count = 0;
    for (int index = 0; index < argc; index++) {
        if (index > 0 &&
            (strcmp(argv[index], "--play") == 0 ||
             strcmp(argv[index], "--uri") == 0)) {
            index++; /* The validated application value is not a GTK option. */
            continue;
        }
        if (index > 0 && g_str_has_prefix(argv[index], "openbox:")) {
            continue;
        }
        filtered[count++] = argv[index];
    }
    filtered[count] = NULL;
    *filtered_argc = count;
    return filtered;
}

static void
on_close_request(GtkWidget *widget, gpointer user_data)
{
    (void)widget;
    (void)user_data;
    save_geometry();
    stop_server();
    release_single_instance();
    gtk_main_quit();
}
static gboolean
on_signal(gpointer user_data)
{
    (void)user_data;
    save_geometry();
    stop_server();
    release_single_instance();
    gtk_widget_destroy(main_window);
    return G_SOURCE_REMOVE;
}


int
main(int argc, char **argv)
{
    NativeRequest startup_request = { NATIVE_REQUEST_NONE, NULL };
    NativeArgsResult args_result = native_request_from_argv(argc, argv, &startup_request);
    if (args_result == NATIVE_ARGS_INVALID) {
        g_printerr("native_host: unsupported or invalid --play/--uri argument\n");
        native_request_clear(&startup_request);
        return 2;
    }

    /* Resolve configurable paths from argv or environment. */
    web_app_path = g_getenv("OPENBOX_WEB_APP");
    python_path = g_getenv("OPENBOX_PYTHON") ?: "python3";
    const char *home = g_get_home_dir();
    data_dir = g_getenv("OPENBOX_DATA_DIR");
    char *default_data_dir = NULL;
    if (!data_dir) {
        default_data_dir = g_build_filename(home, DEFAULT_DATA_DIR, NULL);
        data_dir = default_data_dir;
    }

    /*
     * Capture everything the host prints on failure in the data dir so a
     * file-manager double-click (no terminal) is not silent: the single-
     * instance message, boot errors, and any loader/GTK failures land here.
     */
    int log_fd = open(data_dir, O_RDONLY | O_DIRECTORY);
    if (log_fd >= 0) {
        close(log_fd);
        char *log_path = g_build_filename(data_dir, "openbox-native.log", NULL);
        int fd = open(log_path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC | O_NOFOLLOW, 0600);
        g_free(log_path);
        if (fd >= 0) {
            dup2(fd, STDERR_FILENO);
            if (fd != STDERR_FILENO) {
                close(fd);
            }
            time_t now = time(NULL);
            char stamp[64];
            struct tm local;
            if (localtime_r(&now, &local) &&
                strftime(stamp, sizeof(stamp), "%Y-%m-%d %H:%M:%S", &local) > 0) {
                g_printerr("=== native_host %s ===\n", stamp);
            } else {
                g_printerr("=== native_host ===\n");
            }
        }
    }

    if (!acquire_single_instance(&startup_request)) {
        /* Another instance is running; its owner handles focus and dispatch. */
        g_printerr("native_host: an OpenBox window is already open; request forwarded\n");
        native_request_clear(&startup_request);
        g_free(default_data_dir);
        return 0;
    }

    /* Signal web_app.py that the native host is present (env survives fork+exec). */
    setenv("OPENBOX_NATIVE_HOST", "1", 1);

    if (!boot_server()) {
        g_printerr("native_host: could not start the OpenBox server. "
                   "Is python3 installed and OPENBOX_WEB_APP correct?\n");
        release_single_instance();
        native_request_clear(&startup_request);
        g_free(default_data_dir);
        return 1;
    }

    /*
     * WebKitGTK's dmabuf renderer can fail silently on AMD GPUs (Steam Deck),
     * leaving a blank window; opt out before GTK/WebKit initialization unless
     * the user explicitly opts in.
     */
    if (!g_getenv("OPENBOX_ENABLE_DMABUF")) {
        g_setenv("WEBKIT_DISABLE_DMABUF_RENDERER", "1", TRUE);
    }

    int gtk_argc = 0;
    char **gtk_argv = gtk_argv_without_native_args(argc, argv, &gtk_argc);
    gtk_init(&gtk_argc, &gtk_argv);
    g_free(gtk_argv);
    load_geometry();
    load_tray_flags();
    application_icon_path = resolve_application_icon_path();
    main_window = gtk_window_new(GTK_WINDOW_TOPLEVEL);
    gtk_window_set_title(GTK_WINDOW(main_window), "OpenBox Game Launcher");
    apply_application_icon(GTK_WINDOW(main_window));
    gtk_window_set_default_size(GTK_WINDOW(main_window), default_width, default_height);
    if (window_maximized) {
        gtk_window_maximize(GTK_WINDOW(main_window));
    }
    WebKitUserContentManager *mgr = webkit_user_content_manager_new();
    webkit_user_content_manager_register_script_message_handler(mgr, "openbox");

    WebKitWebView *view = WEBKIT_WEB_VIEW(
        webkit_web_view_new_with_user_content_manager(mgr));
    /* Match app theme (#11100e) and enable smooth scrolling/GPU acceleration. */
    WebKitSettings *settings = webkit_web_view_get_settings(view);
    webkit_settings_set_enable_smooth_scrolling(settings, TRUE);

    const char *accel_policy = g_getenv("OPENBOX_WEBKIT_HARDWARE_ACCELERATION");
    webkit_settings_set_hardware_acceleration_policy(
        settings,
        g_strcmp0(accel_policy, "always") == 0
            ? WEBKIT_HARDWARE_ACCELERATION_POLICY_ALWAYS
            : WEBKIT_HARDWARE_ACCELERATION_POLICY_ON_DEMAND);
    G_GNUC_BEGIN_IGNORE_DEPRECATIONS
    webkit_settings_set_enable_accelerated_2d_canvas(settings, TRUE);
    G_GNUC_END_IGNORE_DEPRECATIONS
    GdkRGBA bg = {0.067, 0.063, 0.055, 1.0};
    webkit_web_view_set_background_color(view, &bg);
    g_signal_connect(mgr, "script-message-received::openbox",
                     G_CALLBACK(message_received), view);
    g_signal_connect(view, "decide-policy", G_CALLBACK(decide_policy), NULL);
    inject_bridge(view);
    gtk_container_add(GTK_CONTAINER(main_window), GTK_WIDGET(view));

    main_view = view;
    NativeRequest initial_request = startup_request;
    if (initial_request.kind == NATIVE_REQUEST_NONE) {
        initial_request.kind = NATIVE_REQUEST_START;
    }
    dispatch_native_request(&initial_request);
    if (pending_focus) {
        pending_focus = FALSE;
        present_main_window();
    }

    g_signal_connect(main_window, "delete-event", G_CALLBACK(on_window_delete), NULL);
    g_signal_connect(main_window, "destroy", G_CALLBACK(on_close_request), NULL);
    setup_tray();
    g_unix_signal_add(SIGINT, on_signal, NULL);
    gtk_widget_show_all(main_window);
    gtk_main();
    native_request_clear(&startup_request);
    native_request_clear(&pending_request);
    g_free(origin);
    g_free(token);
    g_free(application_icon_path);
    g_free(default_data_dir);
    return 0;
}
