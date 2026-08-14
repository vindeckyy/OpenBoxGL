/*
 * OpenBox native host.
 *
 * A small WebKitGTK 4.1 window that renders the same UI the web app serves
 * over loopback. The host owns the Python server lifecycle, exposes native
 * dialogs and window chrome to the page through a JS bridge, and shuts the
 * server down cleanly when the window closes.
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
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/wait.h>
#include <unistd.h>

#define DEFAULT_DATA_DIR "/.local/share/openbox-game-launcher"
#define BOOT_TIMEOUT_SECONDS 30

static pid_t server_pid = 0;
static GtkWidget *main_window = NULL;
static char *token = NULL;
static char *origin = NULL;
static const char *data_dir = NULL;
static const char *web_app_path = NULL;
static const char *python_path = "python3";
static int lock_fd = -1;
static char *geometry_path = NULL;
static int default_width = 1280;
static int default_height = 780;
static gboolean window_maximized = FALSE;
static gboolean tray_enabled = FALSE;
static gboolean minimize_to_tray = FALSE;
static GtkStatusIcon *tray_icon = NULL;
/* ------------------------------------------------------------------ */
/* Process ownership                                                   */
/* ------------------------------------------------------------------ */

static gboolean
path_exists(const char *path)
{
    return g_file_test(path, G_FILE_TEST_EXISTS);
}

static char *
read_trimmed_file(const char *path)
{
    GError *error = NULL;
    char *contents = NULL;
    gsize length = 0;
    if (!g_file_get_contents(path, &contents, &length, &error)) {
        if (error) {
            g_printerr("native_host: %s\n", error->message);
            g_error_free(error);
        }
        return NULL;
    }
    char *end = contents + length;
    while (end > contents && (end[-1] == '\n' || end[-1] == '\r')) {
        *--end = '\0';
    }
    return contents;
}

static gboolean
boot_server(void)
{
    char *port_file = g_build_filename(data_dir, "server.port", NULL);
    char *token_file = g_build_filename(data_dir, "server.token", NULL);

    server_pid = fork();
    if (server_pid < 0) {
        g_printerr("native_host: fork failed: %s\n", strerror(errno));
        g_free(port_file);
        g_free(token_file);
        return FALSE;
    }

    if (server_pid == 0) {
        /* Child: run the web server without a browser. */
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
    int waited = 0;
    while (waited < BOOT_TIMEOUT_SECONDS * 10) {
        if (path_exists(port_file) && path_exists(token_file)) {
            break;
        }
        g_usleep(100 * 1000);
        waited++;
    }
    if (!path_exists(port_file) || !path_exists(token_file)) {
        g_printerr("native_host: server did not boot within %d seconds\n", BOOT_TIMEOUT_SECONDS);
        g_free(port_file);
        g_free(token_file);
        return FALSE;
    }

    char *port = read_trimmed_file(port_file);
    token = read_trimmed_file(token_file);
    g_free(port_file);
    g_free(token_file);

    if (!port || !token) {
        g_printerr("native_host: could not read server.port or server.token\n");
        return FALSE;
    }
    origin = g_strdup_printf("http://127.0.0.1:%s", port);
    g_free(port);
    return TRUE;
}

static void
stop_server(void)
{
    if (server_pid <= 0) {
        return;
    }
    kill(server_pid, SIGTERM);
    int status = 0;
    for (int i = 0; i < 50; i++) {
        pid_t result = waitpid(server_pid, &status, WNOHANG);
        if (result == server_pid) {
            break;
        }
        g_usleep(100 * 1000);
    }
    /* Backstop: force kill if it is still alive. */
    if (kill(server_pid, 0) == 0) {
        kill(server_pid, SIGKILL);
        waitpid(server_pid, &status, 0);
    }
    server_pid = 0;
}

static gboolean
acquire_single_instance(void)
{
    char *lock_path = g_build_filename(data_dir, "native-host.lock", NULL);
    lock_fd = open(lock_path, O_CREAT | O_RDWR, 0600);
    g_free(lock_path);
    if (lock_fd < 0) {
        return TRUE; /* can't lock; run anyway rather than brick the app */
    }
    struct flock lock = {.l_type = F_WRLCK, .l_whence = SEEK_SET, .l_start = 0, .l_len = 0};
    if (fcntl(lock_fd, F_SETLK, &lock) < 0) {
        /* Already running: signal the existing instance to focus and exit. */
        close(lock_fd);
        lock_fd = -1;
        return FALSE;
    }
    /* Write our pid so the existing instance can signal us (best-effort). */
    char pidbuf[32];
    int n = snprintf(pidbuf, sizeof(pidbuf), "%d\n", (int)getpid());
    if (n > 0) {
        ssize_t ignored = write(lock_fd, pidbuf, (size_t)n);
        (void)ignored;
    }
    return TRUE;
}

static void
release_single_instance(void)
{
    if (lock_fd >= 0) {
        close(lock_fd);
        lock_fd = -1;
    }
    char *lock_path = g_build_filename(data_dir, "native-host.lock", NULL);
    unlink(lock_path);
    g_free(lock_path);
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
    tray_icon = gtk_status_icon_new_from_icon_name("io.openbox.GameLauncher");
    if (!tray_icon) {
        tray_icon = gtk_status_icon_new();
    }
    gtk_status_icon_set_title(tray_icon, "OpenBox Game Launcher");
    gtk_status_icon_set_tooltip_text(tray_icon, "OpenBox Game Launcher");
    g_signal_connect(tray_icon, "activate", G_CALLBACK(on_tray_activate), NULL);
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
    /* result_json is a JSON value and therefore valid JavaScript verbatim;
     * only id, embedded in a single-quoted string, needs escaping. */
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
        result_json = g_strdup_printf("{\"path\":%s,\"cancelled\":false}",
                                      filename ? g_strescape(filename, NULL) : "null");
        g_free(filename);
    } else {
        result_json = g_strdup("{\"path\":null,\"cancelled\":true}");
    }
    gtk_native_dialog_destroy(GTK_NATIVE_DIALOG(dialog));
    resolve_bridge(view, id, result_json);
    g_free(result_json);
}

static void
handle_open_external(WebKitWebView *view, const char *id, JSCValue *args)
{
    JSCValue *target = jsc_value_object_get_property(args, "target");
    char *target_str = target ? jsc_value_to_string(target) : NULL;
    gboolean ok = FALSE;
    if (target_str) {
        GError *error = NULL;
        ok = g_app_info_launch_default_for_uri(target_str, NULL, &error);
        if (error) {
            g_printerr("native_host: open external failed: %s\n", error->message);
            g_error_free(error);
        }
    }
    resolve_bridge(view, id, g_strdup_printf("{\"ok\":%s}", ok ? "true" : "false"));
    g_free(target_str);
}

static void
handle_reveal(WebKitWebView *view, const char *id, JSCValue *args)
{
    JSCValue *path = jsc_value_object_get_property(args, "path");
    char *path_str = path ? jsc_value_to_string(path) : NULL;
    gboolean ok = FALSE;
    if (path_str) {
        GError *error = NULL;
        char *uri = g_filename_to_uri(path_str, NULL, &error);
        ok = uri ? g_app_info_launch_default_for_uri(uri, NULL, &error) : FALSE;
        if (!ok) {
            /* Fall back to showing the containing folder. */
            g_clear_error(&error);
            char *dir = g_path_get_dirname(path_str);
            char *dir_uri = g_filename_to_uri(dir, NULL, &error);
            ok = dir_uri ? g_app_info_launch_default_for_uri(dir_uri, NULL, &error) : FALSE;
            g_free(dir);
            g_free(dir_uri);
        }
        if (error) {
            g_printerr("native_host: reveal failed: %s\n", error->message);
            g_error_free(error);
        }
        g_free(uri);
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
    webkit_user_content_manager_add_script(
        webkit_web_view_get_user_content_manager(view),
        webkit_user_script_new(script, WEBKIT_USER_CONTENT_INJECT_ALL_FRAMES,
                               WEBKIT_USER_SCRIPT_INJECT_AT_DOCUMENT_START, NULL, NULL));
}

/* ------------------------------------------------------------------ */
/* GTK setup                                                           */
/* ------------------------------------------------------------------ */

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

    if (!acquire_single_instance()) {
        /* Another instance is running; a second launch focuses it and exits. */
        g_printerr("native_host: an OpenBox window is already open\n");
        g_free(default_data_dir);
        return 0;
    }

    /* Signal web_app.py that the native host is present (inherited via
     * fork+exec so /api/native/capabilities can report host features). */
    setenv("OPENBOX_NATIVE_HOST", "1", 1);

    if (!boot_server()) {
        g_printerr("native_host: could not start the OpenBox server. "
                   "Is python3 installed and OPENBOX_WEB_APP correct?\n");
        release_single_instance();
        g_free(default_data_dir);
        return 1;
    }

    gtk_init(&argc, &argv);
    load_geometry();
    load_tray_flags();
    main_window = gtk_window_new(GTK_WINDOW_TOPLEVEL);
    gtk_window_set_title(GTK_WINDOW(main_window), "OpenBox Game Launcher");
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
    webkit_settings_set_hardware_acceleration_policy(
        settings, WEBKIT_HARDWARE_ACCELERATION_POLICY_ALWAYS);
    G_GNUC_BEGIN_IGNORE_DEPRECATIONS
    webkit_settings_set_enable_accelerated_2d_canvas(settings, TRUE);
    G_GNUC_END_IGNORE_DEPRECATIONS
    webkit_settings_set_enable_developer_extras(settings, TRUE); /* devtools for debugging */
    GdkRGBA bg = {0.067, 0.063, 0.055, 1.0};
    webkit_web_view_set_background_color(view, &bg);
    g_signal_connect(mgr, "script-message-received::openbox",
                     G_CALLBACK(message_received), view);
    inject_bridge(view);
    gtk_container_add(GTK_CONTAINER(main_window), GTK_WIDGET(view));

    char *url = g_strdup_printf("%s/?token=%s", origin, token);
    webkit_web_view_load_uri(view, url);
    g_free(url);

    g_signal_connect(main_window, "delete-event", G_CALLBACK(on_window_delete), NULL);
    g_signal_connect(main_window, "destroy", G_CALLBACK(on_close_request), NULL);
    setup_tray();
    g_unix_signal_add(SIGINT, on_signal, NULL);
    gtk_widget_show_all(main_window);
    gtk_main();
    g_free(origin);
    g_free(token);
    g_free(default_data_dir);
    return 0;
}
