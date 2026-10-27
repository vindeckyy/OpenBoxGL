/*
 * OpenBox native host for macOS.
 *
 * A small WKWebView window that renders the same web app the Linux
 * (WebKitGTK) and Windows (WebView2) hosts render, over the same loopback
 * server.  It owns the Python child process, exposes the same
 * window.openboxNative bridge, forwards openbox:// deeplinks, and shuts the
 * child down when the window closes.
 *
 * Business logic stays in Python; this file is chrome, bridge, and process
 * ownership only.  The contract it implements is docs/native-host-contract.md.
 *
 * Build (repo root):
 *   scripts/build_native_host_macos.sh
 */

#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>
#import <objc/runtime.h>

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

#define BOOT_TIMEOUT_SECONDS 30
#define API_TIMEOUT_SECONDS 30
#define MAX_NATIVE_VALUE_BYTES 4096
#define MAX_IPC_MESSAGE_BYTES 8192
#define NATIVE_HOST_VERSION "1.13.1"
#define DEFAULT_WINDOW_WIDTH 1280
#define DEFAULT_WINDOW_HEIGHT 780

typedef NS_ENUM(NSInteger, NativeRequestKind) {
    NativeRequestNone = 0,
    NativeRequestStart,
    NativeRequestShowGame,
    NativeRequestSearch,
    NativeRequestBigBox,
    NativeRequestSettings,
    NativeRequestMoment,
    NativeRequestClip,
    NativeRequestLaunch,
    NativeRequestResume,
};

#pragma mark - Small helpers

static BOOL path_exists(NSString *path)
{
    if (path.length == 0) {
        return NO;
    }
    return [[NSFileManager defaultManager] fileExistsAtPath:path];
}

/* Read a file the server wrote for us: owner-only, small, and not a link. */
static NSString *read_secure_trimmed_file(NSString *path)
{
    int descriptor = open(path.fileSystemRepresentation, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (descriptor < 0) {
        fprintf(stderr, "native_host: could not open %s: %s\n",
                path.fileSystemRepresentation, strerror(errno));
        return nil;
    }
    struct stat info;
    if (fstat(descriptor, &info) < 0 || !S_ISREG(info.st_mode) ||
        info.st_uid != geteuid() || (info.st_mode & 0077) != 0 ||
        info.st_size <= 0 || info.st_size > 4096) {
        fprintf(stderr, "native_host: rejecting insecure server file %s\n",
                path.fileSystemRepresentation);
        close(descriptor);
        return nil;
    }
    char buffer[4097];
    size_t length = 0;
    while (length < (size_t)info.st_size) {
        ssize_t count = read(descriptor, buffer + length, (size_t)info.st_size - length);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            close(descriptor);
            return nil;
        }
        length += (size_t)count;
    }
    close(descriptor);
    buffer[length] = '\0';
    NSString *value = [NSString stringWithUTF8String:buffer];
    return [value stringByTrimmingCharactersInSet:
                [NSCharacterSet whitespaceAndNewlineCharacterSet]];
}

static BOOL secure_file_ready(NSString *path)
{
    struct stat info;
    return lstat(path.fileSystemRepresentation, &info) == 0 && S_ISREG(info.st_mode) &&
           info.st_uid == geteuid() && (info.st_mode & 0077) == 0 &&
           info.st_size > 0 && info.st_size <= 4096;
}

static BOOL remove_boot_file(NSString *path)
{
    if (unlink(path.fileSystemRepresentation) == 0 || errno == ENOENT) {
        return YES;
    }
    fprintf(stderr, "native_host: could not remove stale %s: %s\n",
            path.fileSystemRepresentation, strerror(errno));
    return NO;
}

static BOOL parse_server_port(NSString *text, uint16_t *parsed)
{
    if (text.length == 0) {
        return NO;
    }
    errno = 0;
    char *end = NULL;
    unsigned long value = strtoul(text.UTF8String, &end, 10);
    if (errno != 0 || end == text.UTF8String || *end != '\0' || value < 1 || value > 65535) {
        return NO;
    }
    *parsed = (uint16_t)value;
    return YES;
}

static BOOL token_is_valid(NSString *value)
{
    if (value.length < 16 || value.length > 256) {
        return NO;
    }
    for (NSUInteger index = 0; index < value.length; index++) {
        unichar character = [value characterAtIndex:index];
        BOOL ok = (character >= 'a' && character <= 'z') ||
                  (character >= 'A' && character <= 'Z') ||
                  (character >= '0' && character <= '9') ||
                  character == '-' || character == '_';
        if (!ok) {
            return NO;
        }
    }
    return YES;
}

static NSString *json_escape(NSString *value)
{
    NSMutableString *escaped = [NSMutableString stringWithCapacity:value.length];
    for (NSUInteger index = 0; index < value.length; index++) {
        unichar character = [value characterAtIndex:index];
        switch (character) {
        case '"':
            [escaped appendString:@"\\\""];
            break;
        case '\\':
            [escaped appendString:@"\\\\"];
            break;
        case '\b':
            [escaped appendString:@"\\b"];
            break;
        case '\f':
            [escaped appendString:@"\\f"];
            break;
        case '\n':
            [escaped appendString:@"\\n"];
            break;
        case '\r':
            [escaped appendString:@"\\r"];
            break;
        case '\t':
            [escaped appendString:@"\\t"];
            break;
        default:
            if (character < 0x20) {
                [escaped appendFormat:@"\\u%04x", character];
            } else {
                [escaped appendFormat:@"%C", character];
            }
            break;
        }
    }
    return escaped;
}

/* Build the dialog bridge result. The path is JSON-escaped with non-ASCII
 * text kept intact, and ';' and '/' become \u escapes so a legal filename
 * such as "a;b" never trips resolveBridge's payload guard and leaves the
 * page's promise unresolved. */
static NSString *dialog_result_json(NSString *path)
{
    NSString *escaped = json_escape(path);
    NSMutableString *safe = [NSMutableString stringWithCapacity:escaped.length];
    for (NSUInteger index = 0; index < escaped.length; index++) {
        unichar character = [escaped characterAtIndex:index];
        if (character == ';') {
            [safe appendString:@"\\u003b"];
        } else if (character == '/') {
            [safe appendString:@"\\u002f"];
        } else {
            [safe appendFormat:@"%C", character];
        }
    }
    return [NSString stringWithFormat:@"{\"path\":\"%@\",\"cancelled\":false}", safe];
}

static NSString *percent_escape(NSString *value)
{
    NSMutableCharacterSet *allowed = [NSMutableCharacterSet alphanumericCharacterSet];
    [allowed addCharactersInString:@"-._~"];
    return [value stringByAddingPercentEncodingWithAllowedCharacters:allowed] ?: @"";
}

/* A game id may hold anything; the page never sees it unescaped. */
static NSString *component_escape(NSString *value)
{
    NSMutableString *escaped = [NSMutableString string];
    NSData *data = [value dataUsingEncoding:NSUTF8StringEncoding];
    const unsigned char *bytes = data.bytes;
    for (NSUInteger index = 0; index < data.length; index++) {
        unsigned char character = bytes[index];
        if ((character >= 'a' && character <= 'z') ||
            (character >= 'A' && character <= 'Z') ||
            (character >= '0' && character <= '9') ||
            character == '-' || character == '_' || character == '.' || character == '~') {
            [escaped appendFormat:@"%C", (unichar)character];
        } else {
            [escaped appendFormat:@"%%%02X", character];
        }
    }
    return escaped;
}

#pragma mark - The bridge script

/* The same script the GTK host injects, byte for byte: the page only
 * knows window.webkit.messageHandlers.openbox. */
static NSString *BridgeScript(void)
{
    return @"window.__openboxPending = {};\n"
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
}

#pragma mark - The host

@interface OpenBoxHost : NSObject <NSApplicationDelegate, NSWindowDelegate, NSMenuDelegate, WKScriptMessageHandler, WKNavigationDelegate>
- (BOOL)bootServer;
- (void)stopServer;
- (BOOL)acquireSingleInstance;
- (BOOL)parseUri:(NSString *)uri;
- (void)dispatchUri:(NSString *)uri;
- (void)presentWindow;
- (void)quitApplication;
- (void)watchServer;
- (void)serverDidExit;
- (NSString *)serverOrigin;
- (NSString *)serverToken;
- (void)setDataDirectory:(NSString *)path;
@end

@implementation OpenBoxHost {
    pid_t _server_pid;
    NSWindow *_window;
    WKWebView *_web_view;
    NSStatusItem *_status_item;
    NSString *_token;
    NSString *_origin;
    NSString *_data_dir;
    NSString *_share_dir;
    NSString *_geometry_path;
    NSString *_native_request_value;
    NativeRequestKind _native_request_kind;
    uint16_t _server_port;
    BOOL _tray_enabled;
    BOOL _minimize_to_tray;
    BOOL _quitting;
    BOOL _stopping;
    NSInteger _server_socket;
    NSRect _pending_frame;
    dispatch_source_t _server_watch;
    int _server_exit_status;
    BOOL _pending_maximized;
}

#pragma mark Paths

- (NSString *)serverOrigin
{
    return _origin;
}

- (NSString *)serverToken
{
    return _token;
}

- (void)setDataDirectory:(NSString *)path
{
    _data_dir = path;
}

- (NSString *)dataDirectory
{
    if (_data_dir) {
        return _data_dir;
    }
    NSString *override = [[[NSProcessInfo processInfo] environment] objectForKey:@"OPENBOX_DATA_DIR"];
    if (override.length > 0) {
        _data_dir = [override stringByExpandingTildeInPath];
    } else {
        _data_dir = [NSHomeDirectory() stringByAppendingPathComponent:
                        @"Library/Application Support/openbox-game-launcher"];
    }
    return _data_dir;
}

/* The runtime payload: the app bundle keeps it under Resources/openbox, a
 * source checkout keeps it beside the host binary. */
- (NSString *)shareDirectory
{
    if (_share_dir) {
        return _share_dir;
    }
    /* The other hosts accept an explicit web_app.py path; keep the same
     * override so a host built beside a checkout can point at it. */
    NSString *override = [[[NSProcessInfo processInfo] environment] objectForKey:@"OPENBOX_WEB_APP"];
    if (override.length > 0) {
        _share_dir = [override stringByStandardizingPath].stringByDeletingLastPathComponent;
        return _share_dir;
    }
    NSString *executable = [[NSBundle mainBundle] bundlePath];
    if (executable.length == 0) {
        executable = [[NSProcessInfo processInfo].arguments.firstObject stringByStandardizingPath];
    }
    NSString *bundleResources = [executable stringByAppendingPathComponent:@"Contents/Resources/openbox"];
    if ([[NSFileManager defaultManager] fileExistsAtPath:
            [bundleResources stringByAppendingPathComponent:@"web_app.py"]]) {
        _share_dir = bundleResources;
    } else {
        _share_dir = [[[NSProcessInfo processInfo].arguments.firstObject stringByStandardizingPath]
                        stringByDeletingLastPathComponent];
    }
    return _share_dir;
}

- (NSString *)pythonPath
{
    NSString *override = [[[NSProcessInfo processInfo] environment] objectForKey:@"OPENBOX_PYTHON"];
    if (override.length > 0) {
        return override;
    }
    NSString *bundled = [[[NSBundle mainBundle] bundlePath]
                            stringByAppendingPathComponent:@"Contents/Resources/python/bin/python3"];
    if (path_exists(bundled)) {
        return bundled;
    }
    return @"python3";
}

/* posix_spawn does not search PATH the way execvp does, so a bare
 * interpreter name has to be resolved before it is spawned. */
- (NSString *)resolvedPythonPath
{
    NSString *python = [self pythonPath];
    if ([python containsString:@"/"]) {
        return python;
    }
    NSString *search = [[[NSProcessInfo processInfo] environment] objectForKey:@"PATH"] ?: @"";
    for (NSString *folder in [search componentsSeparatedByString:@":"]) {
        if (folder.length == 0) {
            continue;
        }
        NSString *candidate = [folder stringByAppendingPathComponent:python];
        if ([[NSFileManager defaultManager] isExecutableFileAtPath:candidate]) {
            return candidate;
        }
    }
    return python;
}

#pragma mark The child process

- (BOOL)bootServer
{
    NSString *port_file = [[self dataDirectory] stringByAppendingPathComponent:@"server.port"];
    NSString *token_file = [[self dataDirectory] stringByAppendingPathComponent:@"server.token"];
    [[NSFileManager defaultManager] createDirectoryAtPath:[self dataDirectory]
                              withIntermediateDirectories:YES
                                               attributes:nil
                                                    error:NULL];
    /* The child resolves its data directory from the environment, so the
     * host has to export the one it is about to read the boot files from. */
    setenv("OPENBOX_DATA_DIR", [self dataDirectory].fileSystemRepresentation, 1);
    if (!remove_boot_file(port_file) || !remove_boot_file(token_file)) {
        return NO;
    }

    NSString *web_app = [[self shareDirectory] stringByAppendingPathComponent:@"web_app.py"];
    if (!path_exists(web_app)) {
        fprintf(stderr, "native_host: OPENBOX_WEB_APP must point at web_app.py\n");
        return NO;
    }
    NSString *python = [self resolvedPythonPath];

    setenv("OPENBOX_NATIVE_HOST", "1", 1);
    setenv("OPENBOX_WEB_APP", web_app.fileSystemRepresentation, 1);
    setenv("OPENBOX_PYTHON", python.fileSystemRepresentation, 1);
    NSString *certificate = [[self shareDirectory] stringByAppendingPathComponent:@"cacert.pem"];
    if (path_exists(certificate) &&
        [[[NSProcessInfo processInfo] environment] objectForKey:@"SSL_CERT_FILE"] == nil) {
        setenv("SSL_CERT_FILE", certificate.fileSystemRepresentation, 1);
    }

    NSString *log_path = [[self dataDirectory] stringByAppendingPathComponent:@"native-host.log"];
    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_addopen(&actions, STDOUT_FILENO,
                                     log_path.fileSystemRepresentation,
                                     O_WRONLY | O_CREAT | O_APPEND, 0600);
    posix_spawn_file_actions_addopen(&actions, STDERR_FILENO,
                                     log_path.fileSystemRepresentation,
                                     O_WRONLY | O_CREAT | O_APPEND, 0600);
    posix_spawnattr_t attributes;
    posix_spawnattr_init(&attributes);
    posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETPGROUP);
    posix_spawnattr_setpgroup(&attributes, 0);

    char *arguments[] = {
        (char *)python.fileSystemRepresentation,
        (char *)"-B",
        (char *)web_app.fileSystemRepresentation,
        (char *)"--no-browser",
        NULL
    };
    pid_t pid = 0;
    int result = posix_spawn(&pid, python.fileSystemRepresentation, &actions, &attributes,
                             arguments, environ);
    posix_spawn_file_actions_destroy(&actions);
    posix_spawnattr_destroy(&attributes);
    if (result != 0) {
        fprintf(stderr, "native_host: could not start the server: %s\n", strerror(result));
        return NO;
    }
    _server_pid = pid;

    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:BOOT_TIMEOUT_SECONDS];
    while ([deadline timeIntervalSinceNow] > 0) {
        if (secure_file_ready(port_file) && secure_file_ready(token_file)) {
            break;
        }
        usleep(50000);
    }
    if (!secure_file_ready(port_file) || !secure_file_ready(token_file)) {
        fprintf(stderr, "native_host: server did not boot within %d seconds\n", BOOT_TIMEOUT_SECONDS);
        [self stopServer];
        return NO;
    }

    NSString *port_text = read_secure_trimmed_file(port_file);
    NSString *token = read_secure_trimmed_file(token_file);
    uint16_t port = 0;
    if (port_text == nil || token == nil || !parse_server_port(port_text, &port) ||
        !token_is_valid(token)) {
        fprintf(stderr, "native_host: could not read server.port or server.token\n");
        [self stopServer];
        return NO;
    }
    _server_port = port;
    [self watchServer];
    _token = token;
    _origin = [NSString stringWithFormat:@"http://127.0.0.1:%u", (unsigned)port];
    return YES;
}

- (void)stopServer
{
    if (_server_watch != nil) {
        dispatch_source_cancel(_server_watch);
    }
    if (_server_pid > 0) {
        kill(-_server_pid, SIGTERM);
        int status = 0;
        for (int index = 0; index < 20; index++) {
            pid_t result = waitpid(_server_pid, &status, WNOHANG);
            if (result == _server_pid || (result < 0 && errno == ECHILD)) {
                _server_pid = 0;
                break;
            }
            usleep(100000);
        }
        if (_server_pid > 0) {
            kill(-_server_pid, SIGKILL);
            waitpid(_server_pid, &status, 0);
            _server_pid = 0;
        }
    }
    NSString *port_file = [[self dataDirectory] stringByAppendingPathComponent:@"server.port"];
    NSString *token_file = [[self dataDirectory] stringByAppendingPathComponent:@"server.token"];
    unlink(port_file.fileSystemRepresentation);
    unlink(token_file.fileSystemRepresentation);
}

- (void)watchServer
{
    /* A server that dies after boot must not leave a window that cannot
     * reach it: OpenBox closes and exits with the server's status. */
    dispatch_source_t source = dispatch_source_create(
        DISPATCH_SOURCE_TYPE_PROC, (uintptr_t)_server_pid, DISPATCH_PROC_EXIT, dispatch_get_main_queue());
    __weak OpenBoxHost *weakSelf = self;
    dispatch_source_set_event_handler(source, ^{
        [weakSelf serverDidExit];
    });
    dispatch_resume(source);
    _server_watch = source;
}

- (void)serverDidExit
{
    if (_quitting || _server_pid <= 0) {
        return;
    }
    pid_t pid = _server_pid;
    int status = 0;
    pid_t reaped = waitpid(pid, &status, WNOHANG);
    _server_pid = 0;
    if (reaped == pid && WIFEXITED(status)) {
        _server_exit_status = WEXITSTATUS(status);
    } else if (reaped == pid && WIFSIGNALED(status)) {
        _server_exit_status = 128 + WTERMSIG(status);
    } else {
        _server_exit_status = 1;
    }
    fprintf(stderr, "native_host: the server exited with status %d; closing OpenBox\n", _server_exit_status);
    [self quitApplication];
}

#pragma mark Single instance

- (BOOL)acquireSingleInstance
{
    NSString *socket_path = [[self dataDirectory] stringByAppendingPathComponent:@"openbox.sock"];
    struct sockaddr_un address;
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    /* Darwin caps sun_path at 104 bytes of filesystem representation, not
     * UTF-16 units: a non-ASCII path can fit NSString.length and still be
     * truncated by strlcpy, which would name the wrong socket. */
    const char *socket_name = socket_path.fileSystemRepresentation;
    if (socket_name == NULL || strlen(socket_name) >= sizeof(address.sun_path)) {
        /* A truncated path would connect to the wrong socket, so the host
         * runs without single instance. */
        fprintf(stderr, "native_host: the data directory path is too long for a unix socket\n");
        return YES;
    }
    strlcpy(address.sun_path, socket_name, sizeof(address.sun_path));

    int descriptor = socket(AF_UNIX, SOCK_STREAM, 0);
    if (descriptor < 0) {
        return YES;
    }
    if (connect(descriptor, (struct sockaddr *)&address, sizeof(address)) == 0) {
        NSString *message = _native_request_kind == NativeRequestNone
            ? @"focus\n"
            : [NSString stringWithFormat:@"%s\n", [[self requestUri] UTF8String]];
        write(descriptor, message.UTF8String, strlen(message.UTF8String));
        close(descriptor);
        return NO;
    }
    unlink(socket_name);
    if (bind(descriptor, (struct sockaddr *)&address, sizeof(address)) < 0 ||
        listen(descriptor, 1) < 0) {
        close(descriptor);
        return YES;
    }
    chmod(socket_name, 0600);
    _server_socket = descriptor;

    /* The listener runs on the main run loop. */
    dispatch_async(dispatch_get_main_queue(), ^{
        [self acceptSingleInstanceConnections];
    });
    return YES;
}

- (void)acceptSingleInstanceConnections
{
    if (_server_socket < 0) {
        return;
    }
    dispatch_source_t source = dispatch_source_create(DISPATCH_SOURCE_TYPE_READ, (uintptr_t)_server_socket, 0, dispatch_get_main_queue());
    __weak OpenBoxHost *weakSelf = self;
    dispatch_source_set_event_handler(source, ^{
        OpenBoxHost *strongSelf = weakSelf;
        if (strongSelf == nil) {
            return;
        }
        int client = accept(strongSelf->_server_socket, NULL, NULL);
        if (client < 0) {
            return;
        }
        char buffer[MAX_IPC_MESSAGE_BYTES];
        size_t length = 0;
        while (length < sizeof(buffer) - 1) {
            ssize_t count = read(client, buffer + length, sizeof(buffer) - 1 - length);
            if (count <= 0) {
                break;
            }
            length += (size_t)count;
            if (buffer[length - 1] == '\n') {
                break;
            }
        }
        buffer[length] = '\0';
        close(client);
        NSString *message = [NSString stringWithUTF8String:buffer];
        message = [message stringByTrimmingCharactersInSet:
                        [NSCharacterSet whitespaceAndNewlineCharacterSet]];
        if ([message isEqualToString:@"focus"]) {
            [strongSelf presentWindow];
            return;
        }
        if ([message hasPrefix:@"openbox://"]) {
            [strongSelf dispatchUri:message];
        }
    });
    dispatch_resume(source);
    /* The source keeps the descriptor alive for the life of the app. */
    objc_setAssociatedObject(self, @selector(acceptSingleInstanceConnections), source, OBJC_ASSOCIATION_RETAIN);
}

#pragma mark Requests

- (NSString *)requestUri
{
    switch (_native_request_kind) {
    case NativeRequestStart:
        return @"openbox://start";
    case NativeRequestShowGame:
    case NativeRequestLaunch:
    case NativeRequestSearch:
    case NativeRequestBigBox:
    case NativeRequestSettings:
    case NativeRequestMoment:
    case NativeRequestClip:
    case NativeRequestResume:
        break;
    default:
        return nil;
    }
    NSString *action = nil;
    switch (_native_request_kind) {
    case NativeRequestLaunch:
        action = @"launch";
        break;
    case NativeRequestSearch:
        action = @"search";
        break;
    case NativeRequestBigBox:
        action = @"bigbox";
        break;
    case NativeRequestSettings:
        action = @"settings";
        break;
    case NativeRequestMoment:
        action = @"moment";
        break;
    case NativeRequestClip:
        action = @"clip";
        break;
    case NativeRequestResume:
        action = @"resume";
        break;
    default:
        action = @"showgame";
        break;
    }
    return [NSString stringWithFormat:@"openbox://%@/%@", action,
            component_escape(_native_request_value ?: @"")];
}

- (BOOL)parseUri:(NSString *)uri
{
    if (uri.length == 0) {
        return NO;
    }
    for (NSUInteger index = 0; index < uri.length; index++) {
        unichar character = [uri characterAtIndex:index];
        if (character < 0x20 || character == 0x7F) {
            return NO;
        }
    }
    NSString *body = uri;
    if ([body hasPrefix:@"openbox://"]) {
        body = [body substringFromIndex:[@"openbox://" length]];
    } else {
        NSRange separator = [body rangeOfString:@"://"];
        if (separator.location == NSNotFound) {
            return NO;
        }
        NSString *authority = [body substringToIndex:separator.location];
        /* Only a local authority may drive the host. */
        if (authority.length > 0 && ![authority isEqualToString:@"localhost"] &&
            ![authority isEqualToString:@"openbox"]) {
            return NO;
        }
        body = [body substringFromIndex:NSMaxRange(separator)];
    }
    /* Strip a query and a fragment. */
    NSRange cut = [body rangeOfCharacterFromSet:[NSCharacterSet characterSetWithCharactersInString:@"?#"]];
    if (cut.location != NSNotFound) {
        body = [body substringToIndex:cut.location];
    }
    while ([body hasSuffix:@"/"]) {
        body = [body substringToIndex:body.length - 1];
    }
    if (body.length == 0) {
        return NO;
    }
    NSString *action = body;
    NSString *value = @"";
    NSRange slash = [body rangeOfString:@"/"];
    if (slash.location != NSNotFound) {
        action = [body substringToIndex:slash.location];
        value = [body substringFromIndex:NSMaxRange(slash)];
    }
    value = [value stringByRemovingPercentEncoding] ?: value;
    if (value.length > MAX_NATIVE_VALUE_BYTES) {
        return NO;
    }
    action = action.lowercaseString;
    if ([action isEqualToString:@"start"]) {
        _native_request_kind = NativeRequestStart;
        _native_request_value = nil;
    } else if ([action isEqualToString:@"showgame"] || [action isEqualToString:@"game"]) {
        _native_request_kind = NativeRequestShowGame;
        _native_request_value = value;
    } else if ([action isEqualToString:@"launch"]) {
        _native_request_kind = NativeRequestLaunch;
        _native_request_value = value;
    } else if ([action isEqualToString:@"resume"]) {
        _native_request_kind = NativeRequestResume;
        _native_request_value = value;
    } else if ([action isEqualToString:@"search"]) {
        _native_request_kind = NativeRequestSearch;
        _native_request_value = value;
    } else if ([action isEqualToString:@"bigbox"] || [action isEqualToString:@"fullscreen"]) {
        _native_request_kind = NativeRequestBigBox;
        _native_request_value = nil;
    } else if ([action isEqualToString:@"settings"]) {
        _native_request_kind = NativeRequestSettings;
        _native_request_value = value.length > 0 ? value : nil;
    } else if ([action isEqualToString:@"moment"]) {
        _native_request_kind = NativeRequestMoment;
        _native_request_value = value;
    } else if ([action isEqualToString:@"clip"]) {
        _native_request_kind = NativeRequestClip;
        _native_request_value = value;
    } else {
        return NO;
    }
    return YES;
}

- (NSString *)authenticatedUrl
{
    if (_origin == nil || _token == nil) {
        return nil;
    }
    NSString *base = [NSString stringWithFormat:@"%@/?token=%@", _origin, _token];
    if (_native_request_kind == NativeRequestNone || _native_request_kind == NativeRequestStart) {
        return base;
    }
    NSString *action = nil;
    switch (_native_request_kind) {
    case NativeRequestShowGame:
    case NativeRequestLaunch:
        action = @"showgame";
        break;
    case NativeRequestSearch:
        action = @"search";
        break;
    case NativeRequestBigBox:
        action = @"bigbox";
        break;
    case NativeRequestSettings:
        action = @"settings";
        break;
    case NativeRequestMoment:
        action = @"moment";
        break;
    case NativeRequestClip:
        action = @"clip";
        break;
    case NativeRequestResume:
        action = @"resume";
        break;
    default:
        return base;
    }
    NSString *escaped = component_escape(_native_request_value ?: @"");
    if (_native_request_kind == NativeRequestBigBox || _native_request_kind == NativeRequestSettings) {
        return [NSString stringWithFormat:@"%@&deeplink=%@", base, action];
    }
    NSString *parameter = [action isEqualToString:@"search"] ? @"q" : @"id";
    return [NSString stringWithFormat:@"%@&deeplink=%@&%@=%@", base, action, parameter, escaped];
}

/* The launch and resume actions call the server first, so the page only
 * has to show the game. */
- (void)postApi:(NSString *)path body:(NSString *)body
{
    if (_server_port == 0 || _token == nil) {
        return;
    }
    int descriptor = socket(AF_INET, SOCK_STREAM, 0);
    if (descriptor < 0) {
        return;
    }
    struct timeval timeout = { API_TIMEOUT_SECONDS, 0 };
    setsockopt(descriptor, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    setsockopt(descriptor, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    struct sockaddr_in address;
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_port = htons(_server_port);
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(descriptor, (struct sockaddr *)&address, sizeof(address)) < 0) {
        close(descriptor);
        return;
    }
    /* Content-Length counts UTF-8 bytes, not NSString UTF-16 units: a game id
     * with non-ASCII text would otherwise truncate the body. */
    NSUInteger body_length = [body lengthOfBytesUsingEncoding:NSUTF8StringEncoding];
    NSString *request = [NSString stringWithFormat:
        @"POST %@ HTTP/1.1\r\nHost: 127.0.0.1\r\nX-OpenBox-Token: %@\r\n"
         "Content-Type: application/json\r\nContent-Length: %lu\r\nConnection: close\r\n\r\n%@",
        path, _token, (unsigned long)body_length, body];
    const char *bytes = request.UTF8String;
    size_t remaining = strlen(bytes);
    while (remaining > 0) {
        ssize_t written = write(descriptor, bytes, remaining);
        if (written <= 0) {
            break;
        }
        bytes += written;
        remaining -= (size_t)written;
    }
    char buffer[4096];
    while (read(descriptor, buffer, sizeof(buffer)) > 0) {
        /* The response body is not needed. */
    }
    close(descriptor);
}

- (void)dispatchRequest
{
    if (_native_request_kind == NativeRequestNone) {
        return;
    }
    if (_web_view == nil) {
        return;
    }
    [self presentWindow];
    if (_native_request_kind == NativeRequestLaunch ||
        _native_request_kind == NativeRequestResume) {
        NSString *value = _native_request_value ?: @"";
        NSString *body = nil;
        BOOL decimal = value.length > 0 && [value rangeOfCharacterFromSet:
            [[NSCharacterSet decimalDigitCharacterSet] invertedSet]].location == NSNotFound;
        if (decimal) {
            /* A decimal id is a legacy index; anything else is a stable id. */
            NSUInteger index = 0;
            while (index < value.length && [value characterAtIndex:index] == '0') {
                index += 1;
            }
            if (index == value.length) {
                body = @"{\"id\":1}";
            } else {
                body = [NSString stringWithFormat:@"{\"id\":%@}",
                        [value substringFromIndex:index]];
            }
        } else {
            body = [NSString stringWithFormat:@"{\"game_id\":\"%@\"}", json_escape(value)];
        }
        if (_native_request_kind == NativeRequestLaunch) {
            [self postApi:@"/api/launch" body:body];
        } else {
            [self postApi:@"/api/v2/resume" body:body];
        }
    }
    NSString *url = [self authenticatedUrl];
    if (url == nil) {
        fprintf(stderr, "native_host: could not build the authenticated deeplink\n");
        return;
    }
    NSURL *target = [NSURL URLWithString:url];
    if (target != nil) {
        [_web_view loadRequest:[NSURLRequest requestWithURL:target]];
    }
}

- (void)dispatchUri:(NSString *)uri
{
    if (![self parseUri:uri]) {
        fprintf(stderr, "native_host: rejected invalid deeplink\n");
        return;
    }
    [self dispatchRequest];
}

#pragma mark The window

- (void)presentWindow
{
    if (_window == nil) {
        return;
    }
    [_window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
}

- (void)saveGeometry
{
    if (_window == nil || _geometry_path == nil) {
        return;
    }
    NSRect frame = _window.frame;
    if (frame.size.width < 400 || frame.size.height < 300) {
        return;
    }
    NSString *contents = [NSString stringWithFormat:@"%d %d %d\n",
        (int)frame.size.width, (int)frame.size.height, _window.isZoomed ? 1 : 0];
    [[NSFileManager defaultManager] createDirectoryAtPath:[_geometry_path stringByDeletingLastPathComponent]
                              withIntermediateDirectories:YES
                                               attributes:nil
                                                    error:NULL];
    [[contents dataUsingEncoding:NSUTF8StringEncoding]
        writeToFile:_geometry_path atomically:YES];
}

- (void)loadGeometry
{
    _geometry_path = [[self dataDirectory] stringByAppendingPathComponent:@"window-geometry"];
    NSString *contents = [NSString stringWithContentsOfFile:_geometry_path
                                                    encoding:NSUTF8StringEncoding
                                                       error:NULL];
    if (contents == nil) {
        return;
    }
    NSArray<NSString *> *parts = [contents componentsSeparatedByString:@" "];
    if (parts.count < 2) {
        return;
    }
    NSInteger width = parts[0].integerValue;
    NSInteger height = parts[1].integerValue;
    if (width >= 400 && height >= 300 && width <= 8000 && height <= 8000) {
        _pending_frame = NSMakeRect(0, 0, width, height);
        _pending_maximized = parts.count >= 3 && parts[2].integerValue != 0;
    }
}

- (void)applyBackground
{
    NSString *path = [[self dataDirectory] stringByAppendingPathComponent:@"native-background"];
    NSString *value = [NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:NULL];
    if (value.length == 0) {
        return;
    }
    value = [value stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
    if ([value hasPrefix:@"#"]) {
        value = [value substringFromIndex:1];
    }
    NSCharacterSet *notHex = [[NSCharacterSet characterSetWithCharactersInString:@"0123456789abcdefABCDEF"] invertedSet];
    if (value.length != 6 || [value rangeOfCharacterFromSet:notHex].location != NSNotFound) {
        return;
    }
    unsigned int rgb = 0;
    [[NSScanner scannerWithString:value] scanHexInt:&rgb];
    NSColor *color = [NSColor colorWithCalibratedRed:((rgb >> 16) & 0xFF) / 255.0
                                                green:((rgb >> 8) & 0xFF) / 255.0
                                                 blue:(rgb & 0xFF) / 255.0
                                                alpha:1.0];
    _window.backgroundColor = color;
    if (@available(macOS 12.0, *)) {
        _web_view.underPageBackgroundColor = color;
    }
}

- (void)readHostFlags
{
    NSString *path = [[self dataDirectory] stringByAppendingPathComponent:@"native-host-flags"];
    NSString *value = [NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:NULL];
    if (value == nil) {
        return;
    }
    NSArray<NSString *> *parts = [[value stringByTrimmingCharactersInSet:
                                    [NSCharacterSet whitespaceAndNewlineCharacterSet]]
                                    componentsSeparatedByString:@" "];
    if (parts.count >= 1) {
        _tray_enabled = parts[0].integerValue != 0;
    }
    if (parts.count >= 2) {
        _minimize_to_tray = parts[1].integerValue != 0;
    }
}

- (void)buildWindow
{
    NSRect frame = _pending_frame;
    if (NSIsEmptyRect(frame)) {
        frame = NSMakeRect(0, 0, DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT);
    }
    _window = [[NSWindow alloc] initWithContentRect:frame
                                           styleMask:(NSWindowStyleMaskTitled |
                                                      NSWindowStyleMaskClosable |
                                                      NSWindowStyleMaskMiniaturizable |
                                                      NSWindowStyleMaskResizable)
                                             backing:NSBackingStoreBuffered
                                               defer:NO];
    _window.title = @"OpenBox";
    _window.delegate = self;
    [_window center];

    WKWebViewConfiguration *configuration = [WKWebViewConfiguration new];
    WKUserContentController *controller = [WKUserContentController new];
    [controller addScriptMessageHandler:(id<WKScriptMessageHandler>)self name:@"openbox"];
    [controller addUserScript:[[WKUserScript alloc] initWithSource:BridgeScript()
                                                       injectionTime:WKUserScriptInjectionTimeAtDocumentStart
                                                    forMainFrameOnly:YES]];
    configuration.userContentController = controller;
    if (@available(macOS 10.15, *)) {
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = NO;
    }
    if (@available(macOS 12.3, *)) {
        configuration.preferences.elementFullscreenEnabled = YES;
    }
    configuration.websiteDataStore = [WKWebsiteDataStore defaultDataStore];

    _web_view = [[WKWebView alloc] initWithFrame:NSMakeRect(0, 0, frame.size.width, frame.size.height)
                                    configuration:configuration];
    _web_view.navigationDelegate = self;
    _web_view.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    _web_view.allowsBackForwardNavigationGestures = NO;
    [_window.contentView addSubview:_web_view];
    [self applyBackground];
}

- (void)loadUrl
{
    NSString *url = [self authenticatedUrl];
    if (url == nil) {
        return;
    }
    NSURL *target = [NSURL URLWithString:url];
    if (target != nil) {
        [_web_view loadRequest:[NSURLRequest requestWithURL:target]];
    }
}

#pragma mark The tray

- (void)updateTray
{
    if (!_tray_enabled) {
        if (_status_item != nil) {
            [[NSStatusBar systemStatusBar] removeStatusItem:_status_item];
            _status_item = nil;
        }
        return;
    }
    if (_status_item != nil) {
        [self rebuildTrayMenu];
        return;
    }
    _status_item = [[NSStatusBar systemStatusBar]
        statusItemWithLength:NSVariableStatusItemLength];
    NSString *logo = [[self shareDirectory] stringByAppendingPathComponent:@"assets/openbox-logo.png"];
    NSImage *icon = [[NSImage alloc] initWithContentsOfFile:logo];
    if (icon != nil) {
        icon.template = YES;
        icon.size = NSMakeSize(18, 18);
    }
    _status_item.button.image = icon;
    _status_item.button.title = icon == nil ? @"OB" : @"";
    _status_item.button.toolTip = @"OpenBox Game Launcher";
    [self rebuildTrayMenu];
}

- (void)rebuildTrayMenu
{
    if (_status_item == nil) {
        return;
    }
    NSMenu *menu = _status_item.menu;
    if (menu == nil) {
        menu = [NSMenu new];
        menu.delegate = self;
    }
    [menu removeAllItems];
    [menu addItemWithTitle:@"Show OpenBox" action:@selector(presentWindow) keyEquivalent:@""].target = self;
    [menu addItem:[NSMenuItem separatorItem]];

    NSString *path = [[self dataDirectory] stringByAppendingPathComponent:@"native-host-recent"];
    NSString *contents = [NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:NULL];
    NSInteger added = 0;
    if (contents.length > 0) {
        for (NSString *line in [contents componentsSeparatedByString:@"\n"]) {
            if (line.length == 0 || added >= 8) {
                continue;
            }
            NSRange tab = [line rangeOfString:@"\t"];
            if (tab.location == NSNotFound) {
                continue;
            }
            NSString *name = [line substringToIndex:tab.location];
            NSString *gameId = [line substringFromIndex:NSMaxRange(tab)];
            if (name.length == 0 || gameId.length == 0) {
                continue;
            }
            NSMenuItem *item = [menu addItemWithTitle:name
                                                action:@selector(trayLaunchGame:)
                                         keyEquivalent:@""];
            item.target = self;
            item.representedObject = gameId;
            added += 1;
        }
    }
    if (added > 0) {
        [menu addItem:[NSMenuItem separatorItem]];
    }
    [menu addItemWithTitle:@"Quit OpenBox" action:@selector(quitApplication) keyEquivalent:@"q"].target = self;
    _status_item.menu = menu;
}

- (void)menuNeedsUpdate:(NSMenu *)menu
{
    /* The recent-games file changes while the app runs; Linux and Windows
     * rebuild their tray menu on every popup, so do the same here. */
    if (menu == _status_item.menu) {
        [self rebuildTrayMenu];
    }
}

- (void)trayLaunchGame:(id)sender
{
    NSString *gameId = [(NSMenuItem *)sender representedObject];
    if (gameId.length == 0) {
        return;
    }
    _native_request_kind = NativeRequestLaunch;
    _native_request_value = gameId;
    [self dispatchRequest];
}

#pragma mark The bridge

- (void)resolveBridge:(NSString *)identifier result:(NSString *)resultJson
{
    if (identifier.length == 0 || resultJson.length == 0) {
        return;
    }
    if ([resultJson containsString:@";"] || [resultJson containsString:@"//"] ||
        [resultJson containsString:@"*/"]) {
        fprintf(stderr, "native_host: resolve_bridge rejected suspicious result payload\n");
        return;
    }
    NSString *escaped = [identifier stringByReplacingOccurrencesOfString:@"\\" withString:@"\\\\"];
    escaped = [escaped stringByReplacingOccurrencesOfString:@"'" withString:@"\\'"];
    escaped = [escaped stringByReplacingOccurrencesOfString:@"\n" withString:@"\\n"];
    escaped = [escaped stringByReplacingOccurrencesOfString:@"\r" withString:@"\\r"];
    NSString *script = [NSString stringWithFormat:
        @"window.__openboxResolve && window.__openboxResolve('%@', %@);", escaped, resultJson];
    [_web_view evaluateJavaScript:script completionHandler:nil];
}

- (void)handleDialog:(NSString *)identifier arguments:(NSDictionary *)arguments
{
    NSString *kind = [arguments[@"kind"] isKindOfClass:[NSString class]] ? arguments[@"kind"] : @"folder";
    NSString *start = [arguments[@"start_path"] isKindOfClass:[NSString class]] ? arguments[@"start_path"] : nil;
    NSArray *filters = [arguments[@"filters"] isKindOfClass:[NSArray class]] ? arguments[@"filters"] : @[];

    void (^finish)(NSString *) = ^(NSString *path) {
        if (path == nil) {
            [self resolveBridge:identifier result:@"{\"path\":null,\"cancelled\":true}"];
            return;
        }
        [self resolveBridge:identifier result:dialog_result_json(path)];
    };

    NSString *title = [arguments[@"title"] isKindOfClass:[NSString class]] ? arguments[@"title"] : @"OpenBox";
    if ([kind isEqualToString:@"save"]) {
        NSSavePanel *panel = [NSSavePanel new];
        panel.title = title;
        panel.nameFieldStringValue = @"";
        if (start.length > 0 && [start hasPrefix:@"/"]) {
            panel.directoryURL = [NSURL fileURLWithPath:start];
        }
        [panel beginSheetModalForWindow:_window completionHandler:^(NSModalResponse response) {
            if (response == NSModalResponseOK) {
                finish(panel.URL.path);
            } else {
                finish(nil);
            }
        }];
        return;
    }

    NSOpenPanel *panel = [NSOpenPanel openPanel];
    panel.title = title;
    panel.canChooseFiles = ![kind isEqualToString:@"folder"];
    panel.canChooseDirectories = [kind isEqualToString:@"folder"];
    panel.allowsMultipleSelection = NO;
    NSMutableArray<NSString *> *extensions = [NSMutableArray array];
    for (id filter in filters) {
        if ([filter isKindOfClass:[NSString class]]) {
            NSString *value = [(NSString *)filter stringByTrimmingCharactersInSet:
                                [NSCharacterSet whitespaceAndNewlineCharacterSet]];
            if ([value hasPrefix:@"."]) {
                value = [value substringFromIndex:1];
            }
            if (value.length > 0) {
                [extensions addObject:value];
            }
        }
    }
    if (extensions.count > 0) {
        panel.allowedFileTypes = extensions;
    }
    if (start.length > 0 && [start hasPrefix:@"/"]) {
        panel.directoryURL = [NSURL fileURLWithPath:start];
    }
    [panel beginSheetModalForWindow:_window completionHandler:^(NSModalResponse response) {
        if (response == NSModalResponseOK) {
            finish(panel.URL.path);
        } else {
            finish(nil);
        }
    }];
}

- (BOOL)uriSchemeAllowed:(NSString *)value
{
    if (value.length == 0) {
        return NO;
    }
    for (NSUInteger index = 0; index < value.length; index++) {
        unichar character = [value characterAtIndex:index];
        if (character < 0x20 || character == 0x7F) {
            return NO;
        }
    }
    NSURL *url = [NSURL URLWithString:value];
    NSString *scheme = url.scheme.lowercaseString;
    if ([scheme isEqualToString:@"http"] || [scheme isEqualToString:@"https"]) {
        return url.host.length > 0;
    }
    if ([scheme isEqualToString:@"file"]) {
        NSString *path = url.path;
        return path.length > 0 && [[NSFileManager defaultManager] isReadableFileAtPath:path];
    }
    return NO;
}

- (void)handleOpenExternal:(NSString *)identifier arguments:(NSDictionary *)arguments
{
    NSString *target = [arguments[@"target"] isKindOfClass:[NSString class]] ? arguments[@"target"] : nil;
    BOOL ok = NO;
    if (target.length > 0) {
        if (![self uriSchemeAllowed:target]) {
            fprintf(stderr, "native_host: open external rejected scheme in '%s'\n", target.UTF8String);
        } else {
            NSURL *url = [NSURL URLWithString:target];
            if (url != nil) {
                ok = [[NSWorkspace sharedWorkspace] openURL:url];
            }
        }
    }
    [self resolveBridge:identifier
                 result:[NSString stringWithFormat:@"{\"ok\":%@}", ok ? @"true" : @"false"]];
}

- (void)handleReveal:(NSString *)identifier arguments:(NSDictionary *)arguments
{
    NSString *path = [arguments[@"path"] isKindOfClass:[NSString class]] ? arguments[@"path"] : nil;
    BOOL ok = NO;
    if (path.length > 0) {
        if (![path hasPrefix:@"/"]) {
            fprintf(stderr, "native_host: reveal rejected relative path '%s'\n", path.UTF8String);
        } else {
            NSString *canonical = [[NSString stringWithFormat:@"%@", path] stringByStandardizingPath];
            NSString *home = NSHomeDirectory();
            NSString *data = [self dataDirectory];
            BOOL allowed = [canonical hasPrefix:[data stringByAppendingString:@"/"]] ||
                           [canonical hasPrefix:[home stringByAppendingString:@"/"]];
            if (!allowed) {
                fprintf(stderr, "native_host: reveal rejected path outside data/home dirs: '%s'\n",
                        canonical.UTF8String);
            } else if (path_exists(canonical)) {
                [[NSWorkspace sharedWorkspace]
                    activateFileViewerSelectingURLs:@[[NSURL fileURLWithPath:canonical]]];
                ok = YES;
            } else {
                [[NSWorkspace sharedWorkspace]
                    activateFileViewerSelectingURLs:@[[NSURL fileURLWithPath:
                        [canonical stringByDeletingLastPathComponent]]]];
                ok = YES;
            }
        }
    }
    [self resolveBridge:identifier
                 result:[NSString stringWithFormat:@"{\"ok\":%@}", ok ? @"true" : @"false"]];
}

- (void)handleWindowAction:(NSString *)identifier arguments:(NSDictionary *)arguments
{
    NSString *action = [arguments[@"action"] isKindOfClass:[NSString class]] ? arguments[@"action"] : nil;
    if ([action isEqualToString:@"close"]) {
        [self resolveBridge:identifier result:@"{\"ok\":true}"];
        [self quitApplication];
        return;
    }
    if ([action isEqualToString:@"minimize"]) {
        [_window miniaturize:nil];
    } else if ([action isEqualToString:@"toggle-maximize"]) {
        [_window zoom:nil];
    } else if ([action isEqualToString:@"set-fullscreen"]) {
        if (!(_window.styleMask & NSWindowStyleMaskFullScreen)) {
            [_window toggleFullScreen:nil];
        }
    } else if ([action isEqualToString:@"unset-fullscreen"]) {
        if (_window.styleMask & NSWindowStyleMaskFullScreen) {
            [_window toggleFullScreen:nil];
        }
    }
    [self resolveBridge:identifier result:@"{\"ok\":true}"];
}

- (void)userContentController:(WKUserContentController *)controller
      didReceiveScriptMessage:(WKScriptMessage *)message
{
    if (!message.frameInfo.isMainFrame) {
        return;
    }
    NSDictionary *body = [message.body isKindOfClass:[NSDictionary class]] ? message.body : nil;
    if (body == nil) {
        return;
    }
    NSString *identifier = [body[@"id"] isKindOfClass:[NSString class]] ? body[@"id"] : nil;
    NSString *method = [body[@"method"] isKindOfClass:[NSString class]] ? body[@"method"] : nil;
    NSDictionary *arguments = [body[@"args"] isKindOfClass:[NSDictionary class]] ? body[@"args"] : @{};
    if (identifier.length == 0 || method.length == 0) {
        return;
    }
    if ([method isEqualToString:@"dialog"]) {
        [self handleDialog:identifier arguments:arguments];
    } else if ([method isEqualToString:@"openExternal"]) {
        [self handleOpenExternal:identifier arguments:arguments];
    } else if ([method isEqualToString:@"reveal"]) {
        [self handleReveal:identifier arguments:arguments];
    } else if ([method isEqualToString:@"windowAction"]) {
        [self handleWindowAction:identifier arguments:arguments];
    } else {
        [self resolveBridge:identifier result:@"{\"ok\":false}"];
    }
}

#pragma mark Navigation

- (BOOL)uriMatchesExpectedOrigin:(NSString *)value
{
    if (_origin == nil || value.length == 0) {
        return NO;
    }
    NSURL *url = [NSURL URLWithString:value];
    if (url == nil) {
        return NO;
    }
    if ([url.scheme.lowercaseString isEqualToString:@"about"]) {
        return [url.absoluteString isEqualToString:@"about:blank"];
    }
    if (![url.scheme.lowercaseString isEqualToString:@"http"]) {
        return NO;
    }
    return [url.host isEqualToString:@"127.0.0.1"] && url.port.unsignedIntegerValue == (NSUInteger)_server_port;
}

- (void)webView:(WKWebView *)webView
    decidePolicyForNavigationAction:(WKNavigationAction *)navigationAction
                    decisionHandler:(void (^)(WKNavigationActionPolicy))decisionHandler
{
    NSString *value = navigationAction.request.URL.absoluteString;
    if ([self uriMatchesExpectedOrigin:value]) {
        decisionHandler(WKNavigationActionPolicyAllow);
        return;
    }
    decisionHandler(WKNavigationActionPolicyCancel);
    if ([value hasPrefix:@"http://"] || [value hasPrefix:@"https://"]) {
        NSURL *url = [NSURL URLWithString:value];
        if (url != nil) {
            [[NSWorkspace sharedWorkspace] openURL:url];
        }
    }
}

#pragma mark Window delegate

- (BOOL)windowShouldClose:(NSWindow *)sender
{
    if (_minimize_to_tray && _status_item != nil && !_quitting) {
        [sender orderOut:nil];
        return NO;
    }
    [self quitApplication];
    return NO;
}

- (void)windowDidResize:(NSNotification *)notification
{
    [self saveGeometry];
}

- (void)windowDidMove:(NSNotification *)notification
{
    [self saveGeometry];
}

#pragma mark Lifecycle

- (void)quitApplication
{
    if (_quitting) {
        return;
    }
    _quitting = YES;
    [NSApp terminate:nil];
}

- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)sender
{
    /* The stop sequence runs here, on the main thread, and takes at most
     * about 2 seconds. NSTerminateLater is not used: AppKit's terminate:
     * blocks on the reply when it is returned from a dispatch-source
     * handler (SIGTERM, child exit), which left the host hung. */
    _quitting = YES;
    if (!_stopping) {
        _stopping = YES;
        [self saveGeometry];
        [self stopServer];
    }
    return NSTerminateNow;
}

- (void)applicationWillTerminate:(NSNotification *)notification
{
    /* AppKit exits the process right after this returns, so a server that
     * exited on its own sets the process status here. */
    if (_server_exit_status != 0) {
        exit(_server_exit_status);
    }
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender
{
    return NO;
}

- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender hasVisibleWindows:(BOOL)visible
{
    if (!visible) {
        [self presentWindow];
    }
    return YES;
}

- (void)application:(NSApplication *)application openURLs:(NSArray<NSURL *> *)urls
{
    for (NSURL *url in urls) {
        if ([url.scheme.lowercaseString isEqualToString:@"openbox"]) {
            [self dispatchUri:url.absoluteString];
        }
    }
}

- (void)installSignalHandlers
{
    /* A SIGTERM must tear the child down with the host, the way the GTK
     * host does it: the Python server runs in its own process group and
     * would survive the host otherwise. */
    const int signals[] = {SIGTERM, SIGINT, SIGHUP};
    /* The sources are kept in one array. Re-associating a single object per
     * signal would release the earlier sources, and a released dispatch
     * source is cancelled, so only the last signal would stay handled. */
    NSMutableArray *sources = [NSMutableArray arrayWithCapacity:sizeof(signals) / sizeof(signals[0])];
    for (size_t index = 0; index < sizeof(signals) / sizeof(signals[0]); index++) {
        int signal_number = signals[index];
        signal(signal_number, SIG_IGN);
        dispatch_source_t source = dispatch_source_create(
            DISPATCH_SOURCE_TYPE_SIGNAL, (uintptr_t)signal_number, 0, dispatch_get_main_queue());
        __weak OpenBoxHost *weakSelf = self;
        dispatch_source_set_event_handler(source, ^{
            OpenBoxHost *strongSelf = weakSelf;
            if (strongSelf == nil) {
                return;
            }
            [strongSelf quitApplication];
        });
        dispatch_resume(source);
        [sources addObject:source];
    }
    objc_setAssociatedObject(self, @selector(installSignalHandlers), sources, OBJC_ASSOCIATION_RETAIN);
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification
{
    [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
    [self buildMenus];
    [self loadGeometry];
    [self buildWindow];
    [self readHostFlags];
    [self updateTray];
    [self installSignalHandlers];
    [self presentWindow];
    if (_pending_maximized) {
        [_window zoom:nil];
    }
    [self loadUrl];
    if (_native_request_kind != NativeRequestNone) {
        /* The request the command line carried is dispatched once the
         * window exists, the same way the GTK host does it. */
        [self dispatchRequest];
    }
}

- (void)buildMenus
{
    NSMenu *mainMenu = [[NSMenu alloc] init];

    NSMenuItem *appItem = [[NSMenuItem alloc] init];
    NSMenu *appMenu = [NSMenu new];
    [appMenu addItemWithTitle:@"About OpenBox" action:@selector(orderFrontStandardAboutPanel:) keyEquivalent:@""];
    [appMenu addItem:[NSMenuItem separatorItem]];
    [appMenu addItemWithTitle:@"Hide OpenBox" action:@selector(hide:) keyEquivalent:@"h"];
    [appMenu addItemWithTitle:@"Quit OpenBox" action:@selector(quitApplication) keyEquivalent:@"q"].target = self;
    appItem.submenu = appMenu;
    [mainMenu addItem:appItem];

    NSMenuItem *editItem = [[NSMenuItem alloc] init];
    NSMenu *editMenu = [[NSMenu alloc] initWithTitle:@"Edit"];
    [editMenu addItemWithTitle:@"Undo" action:@selector(undo:) keyEquivalent:@"z"];
    [editMenu addItemWithTitle:@"Redo" action:@selector(redo:) keyEquivalent:@"Z"];
    [editMenu addItem:[NSMenuItem separatorItem]];
    [editMenu addItemWithTitle:@"Cut" action:@selector(cut:) keyEquivalent:@"x"];
    [editMenu addItemWithTitle:@"Copy" action:@selector(copy:) keyEquivalent:@"c"];
    [editMenu addItemWithTitle:@"Paste" action:@selector(paste:) keyEquivalent:@"v"];
    [editMenu addItemWithTitle:@"Select All" action:@selector(selectAll:) keyEquivalent:@"a"];
    editItem.submenu = editMenu;
    [mainMenu addItem:editItem];

    NSMenuItem *viewItem = [[NSMenuItem alloc] init];
    NSMenu *viewMenu = [[NSMenu alloc] initWithTitle:@"View"];
    [viewMenu addItemWithTitle:@"Enter Full Screen" action:@selector(toggleFullScreen:) keyEquivalent:@"f"].keyEquivalentModifierMask = NSEventModifierFlagControl | NSEventModifierFlagCommand;
    viewItem.submenu = viewMenu;
    [mainMenu addItem:viewItem];

    NSMenuItem *windowItem = [[NSMenuItem alloc] init];
    NSMenu *windowMenu = [[NSMenu alloc] initWithTitle:@"Window"];
    [windowMenu addItemWithTitle:@"Minimize" action:@selector(performMiniaturize:) keyEquivalent:@"m"];
    [windowMenu addItemWithTitle:@"Close" action:@selector(performClose:) keyEquivalent:@"w"];
    windowItem.submenu = windowMenu;
    [mainMenu addItem:windowItem];

    NSApp.mainMenu = mainMenu;
    NSApp.windowsMenu = windowMenu;
}

@end

#pragma mark - The self test

static BOOL selftest_escaping(void)
{
    NSString *path = @"/Users/example/Jeux Vidéo/a;b.iso";
    NSString *json = dialog_result_json(path);
    NSData *data = [json dataUsingEncoding:NSUTF8StringEncoding];
    NSDictionary *parsed = [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL];
    if (![parsed isKindOfClass:[NSDictionary class]] ||
        ![parsed[@"path"] isEqualToString:path]) {
        fprintf(stderr, "native_host: selftest could not round-trip a non-ASCII dialog path\n");
        return NO;
    }
    if ([json containsString:@";"] || [json containsString:@"//"]) {
        fprintf(stderr, "native_host: selftest emitted a dialog payload the bridge guard rejects\n");
        return NO;
    }
    return YES;
}

static void print_log_tail(NSString *path)
{
    NSString *log = [NSString stringWithContentsOfFile:path
                                             encoding:NSUTF8StringEncoding
                                                error:NULL];
    if (log.length == 0) {
        return;
    }
    NSArray<NSString *> *lines = [log componentsSeparatedByString:@"\n"];
    NSUInteger start = lines.count > 20 ? lines.count - 20 : 0;
    NSString *tail = [[lines subarrayWithRange:NSMakeRange(start, lines.count - start)]
                        componentsJoinedByString:@"\n"];
    fprintf(stderr, "native_host: server log:\n%s\n", tail.UTF8String);
}

static int selftest_server(OpenBoxHost *host)
{
    /* The self test drives the same boot path, but without a window. */
    if (![host bootServer]) {
        print_log_tail([[host dataDirectory] stringByAppendingPathComponent:@"native-host.log"]);
        fprintf(stderr, "native_host: selftest could not boot the server\n");
        return 1;
    }
    /* The capabilities response proves the child, the token, and the
     * loopback API all work together. */
    NSString *origin = [host serverOrigin];
    NSString *token = [host serverToken];
    if (origin == nil || token == nil) {
        fprintf(stderr, "native_host: selftest could not read the server identity\n");
        return 1;
    }
    NSURL *url = [NSURL URLWithString:
        [NSString stringWithFormat:@"%@/api/native/capabilities", origin]];
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:url];
    [request setValue:token forHTTPHeaderField:@"X-OpenBox-Token"];
    [request setValue:@"application/json" forHTTPHeaderField:@"Accept"];
    __block NSString *payload = nil;
    __block NSError *failure = nil;
    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    [[[NSURLSession sharedSession] dataTaskWithRequest:request
                                     completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        if (data != nil) {
            payload = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
        }
        failure = error;
        dispatch_semaphore_signal(done);
    }] resume];
    dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, 30 * NSEC_PER_SEC));
    if (payload == nil) {
        fprintf(stderr, "native_host: selftest could not read the capabilities: %s\n",
                failure ? failure.localizedDescription.UTF8String : "no response");
        return 1;
    }
    NSData *json = [payload dataUsingEncoding:NSUTF8StringEncoding];
    NSDictionary *parsed = [NSJSONSerialization JSONObjectWithData:json options:0 error:NULL];
    if (![parsed isKindOfClass:[NSDictionary class]]) {
        fprintf(stderr, "native_host: selftest got an unreadable capabilities response\n");
        return 1;
    }
    NSString *platform = parsed[@"platform"];
    BOOL webview = [parsed[@"webview"] boolValue];
    if (![platform isEqualToString:@"darwin"] || !webview) {
        fprintf(stderr, "native_host: selftest saw platform=%s webview=%d\n",
                platform.UTF8String, webview ? 1 : 0);
        return 1;
    }
    printf("selftest ok platform=%s\n", platform.UTF8String);
    return 0;
}

static int run_selftest(void)
{
    @autoreleasepool {
        if (!selftest_escaping()) {
            return 1;
        }
        OpenBoxHost *host = [OpenBoxHost new];
        /* The self test must not touch the data directory of a running app,
         * so it boots against a scratch one. */
        NSString *data_dir = [NSTemporaryDirectory() stringByAppendingPathComponent:
            [NSString stringWithFormat:@"openbox-selftest-%@", [[NSUUID UUID] UUIDString]]];
        [host setDataDirectory:data_dir];
        int status = selftest_server(host);
        [host stopServer];
        [[NSFileManager defaultManager] removeItemAtPath:data_dir error:NULL];
        return status;
    }
}

#pragma mark - Entry point

int main(int argc, const char *argv[])
{
    @autoreleasepool {
        BOOL selftest = NO;
        NSString *pending_uri = nil;
        for (int index = 1; index < argc; index++) {
            NSString *argument = [NSString stringWithUTF8String:argv[index]];
            if ([argument isEqualToString:@"--selftest"]) {
                selftest = YES;
                continue;
            }
            if ([argument isEqualToString:@"--uri"] && index + 1 < argc) {
                index += 1;
                pending_uri = [NSString stringWithUTF8String:argv[index]];
                continue;
            }
            if ([argument isEqualToString:@"--play"] && index + 1 < argc) {
                index += 1;
                pending_uri = [NSString stringWithFormat:@"openbox://launch/%@",
                                percent_escape([NSString stringWithUTF8String:argv[index]])];
                continue;
            }
        }

        OpenBoxHost *host = [OpenBoxHost new];
        NSString *override = [[[NSProcessInfo processInfo] environment] objectForKey:@"OPENBOX_DATA_DIR"];
        if (override.length > 0) {
            [host setDataDirectory:override];
        }

        if (selftest) {
            return run_selftest();
        }

        if (pending_uri.length > 0) {
            [host parseUri:pending_uri];
        }
        if (![host acquireSingleInstance]) {
            /* Another instance owns the window; it received our request. */
            return 0;
        }
        if (![host bootServer]) {
            return 1;
        }
        /* NSApp is nil until the shared application exists; without this the
         * delegate assignment and the run loop are silent no-ops. */
        [NSApplication sharedApplication];
        [NSApp setDelegate:host];
        [NSApp run];
        /* The run loop can end without the teardown path (a session that
         * has no window server, for example). The child must never outlive
         * the host, so it is stopped here as well. */
        [host stopServer];
        return 0;
    }
}
