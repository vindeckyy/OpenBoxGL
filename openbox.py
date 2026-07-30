#!/usr/bin/env python3
"""Local-first Linux game library and launcher. Independent open-source software not affiliated with LaunchBox or Unbroken Software, LLC."""

import json
import logging
import os
import random
import shlex
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from archives import extract_game
from openbox_logging import configure_logging

CUSTOM_DATA_DIR = os.environ.get("OPENBOX_DATA_DIR")
APP_DIR = Path(CUSTOM_DATA_DIR or Path.home() / ".local/share/openbox-game-launcher").expanduser()
DATA = APP_DIR / "library.json"
LEGACY_DATA = Path.home() / ".local" / "share" / "launchbox-linux" / "library.json"
if not CUSTOM_DATA_DIR and not DATA.exists() and LEGACY_DATA.is_file():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LEGACY_DATA, DATA)
from parity_import import EXTENSIONS_EXTRA, PLATFORM_BY_EXTENSION_EXTRA

EXTENSIONS = {".sh", ".appimage", ".exe", ".iso", ".rom", ".nes", ".sfc", ".smc", ".gba", ".gb", ".gbc", ".zip", ".7z", ".rar"} | EXTENSIONS_EXTRA
PLATFORM_BY_EXTENSION = {
    ".nes": "NES", ".sfc": "SNES", ".smc": "SNES", ".gba": "Game Boy Advance",
    ".gb": "Game Boy", ".gbc": "Game Boy Color", ".iso": "Disc image",
    **PLATFORM_BY_EXTENSION_EXTRA,
}

# Development-only screenshot fixtures must never ship in user libraries.
DEMO_PATH_MARKERS = ("/tmp/openbox-screenshots/",)


def is_demo_game(game):
    if not isinstance(game, dict):
        return False
    if game.get("demo"):
        return True
    path = str(game.get("path", ""))
    return any(marker in path for marker in DEMO_PATH_MARKERS)


def purge_demo_games(state):
    games = state.get("games", [])
    if not isinstance(games, list):
        return 0
    kept = [game for game in games if not is_demo_game(game)]
    removed = len(games) - len(kept)
    if removed:
        state["games"] = kept
    return removed


def load_state():
    try:
        raw = json.loads(DATA.read_text())
        if isinstance(raw, list):
            return {"games": raw, "profiles": {}, "history": []}
        if not isinstance(raw, dict):
            raise AttributeError
        raw.setdefault("games", [])
        raw.setdefault("profiles", {})
        raw.setdefault("history", [])
        raw.setdefault("settings", {})
        raw.setdefault("playlists", [])
        return raw
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return {"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []}


def save_state(state):
    DATA.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2))
    temporary.replace(DATA)


def format_duration(seconds):
    minutes = int(seconds or 0) // 60
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def discover_profiles(which=shutil.which):
    candidates = {
        "DOSBox": ("dosbox", "dosbox {path}"),
        "Windows": ("wine", "wine {path}"),
        "Arcade": ("mame", "mame {path}"),
        "GameCube": ("dolphin-emu", "dolphin-emu -b -e {path}"),
        "Wii": ("dolphin-emu", "dolphin-emu -b -e {path}"),
        "PlayStation 2": ("pcsx2-qt", "pcsx2-qt {path}"),
        "PSP": ("ppsspp", "ppsspp {path}"),
        "PlayStation 3": ("rpcs3", "rpcs3 {path}"),
        "PlayStation": ("duckstation-qt", "duckstation-qt -batch {path}"),
    }
    return {platform: command for platform, (binary, command) in candidates.items() if which(binary)}


def build_launch(game, profiles):
    path = game.get("path", "")
    if not path:
        raise ValueError(f"{game.get('name', 'This game')} has no launch path.")
    if not Path(path).exists():
        raise FileNotFoundError(f"The configured path no longer exists:\n{path}")
    launch_path = str(extract_game(path, DATA.parent / "cache/archives", game.get("archive_member", ""))) if game.get("extract_archive") else path
    game_command = game.get("launch", "")
    command = game_command or profiles.get(game.get("platform", ""), "")
    if command:
        replacements = {
            "{path}": launch_path,
            "{name}": game.get("name", ""),
            "{app_id}": str(game.get("steam_app_id", "")),
            "{heroic_app_id}": str(game.get("heroic_app_id", "")),
            "{lutris_id}": str(game.get("lutris_id", "")),
            "{rom_name}": str(game.get("rom_name", "")),
        }
        args = shlex.split(command)
        for marker, value in replacements.items():
            args = [part.replace(marker, value) for part in args]
        if not game_command and "{path}" not in command:
            args.append(launch_path)
    elif Path(launch_path).suffix.lower() == ".sh":
        args = ["bash", launch_path]
    else:
        args = [launch_path]
    return args, str(Path(launch_path).parent)


class OpenBox(tk.Tk):
    BG = "#111318"
    PANEL = "#1b1e26"
    SIDEBAR = "#15171d"
    TEXT = "#f5f7fb"
    MUTED = "#9ca3af"
    ACCENT = "#ffb54a"

    def __init__(self):
        super().__init__()
        self.report_callback_exception = lambda exc_type, exc_value, traceback: logging.getLogger("openbox").error(
            "Unhandled Tk callback", exc_info=(exc_type, exc_value, traceback)
        )
        self.title("OpenBox")
        self.geometry("1280x780")
        self.minsize(900, 580)
        self.state = load_state()
        if purge_demo_games(self.state):
            save_state(self.state)
        self.games = self.state["games"]
        self.profiles = self.state["profiles"]
        self.history = self.state["history"]
        self.view = "all"
        self.query = tk.StringVar()
        self.platform = tk.StringVar(value="All platforms")
        self.genre = tk.StringVar(value="All genres")
        self.collection = tk.StringVar(value="All collections")
        self._build()
        self.render()

    def _build(self):
        self.configure(bg=self.BG)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TButton", padding=8)
        style.configure("Accent.TButton", padding=8, background=self.ACCENT, foreground="#21170b")
        style.map("Accent.TButton", background=[("active", "#ffd18a")])
        style.configure("Treeview", rowheight=34, background=self.PANEL, fieldbackground=self.PANEL, foreground=self.TEXT, borderwidth=0)
        style.map("Treeview", background=[("selected", "#4c3b29")], foreground=[("selected", self.TEXT)])
        style.configure("Treeview.Heading", background="#272b35", foreground=self.TEXT, relief="flat")

        sidebar = tk.Frame(self, bg=self.SIDEBAR, width=220)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="O  OpenBox", bg=self.SIDEBAR, fg=self.TEXT, font=("TkDefaultFont", 15, "bold"), pady=28).pack()
        self.nav = {}
        for label, view in (
            ("Library", "all"),
            ("Favorites", "favorites"),
            ("Continue playing", "recent"),
            ("Never played", "never"),
            ("Missing files", "missing"),
        ):
            button = tk.Button(sidebar, text=label, command=lambda v=view: self.set_view(v), relief="flat", anchor="w", padx=18, pady=9, bg=self.SIDEBAR, fg=self.MUTED, activebackground="#272b35", activeforeground=self.TEXT, bd=0)
            button.pack(fill="x", padx=12, pady=2)
            self.nav[view] = button
        tk.Label(sidebar, text="COLLECTIONS", bg=self.SIDEBAR, fg=self.MUTED, font=("TkDefaultFont", 9, "bold"), anchor="w", padx=18, pady=18).pack(fill="x")
        self.collections_frame = tk.Frame(sidebar, bg=self.SIDEBAR)
        self.collections_frame.pack(fill="x")
        tk.Frame(sidebar, bg=self.SIDEBAR).pack(fill="both", expand=True)
        for label, command in (
            ("Big Box view", self.big_box),
            ("Play history", self.play_history),
            ("Library health", self.library_health),
            ("Find emulators", self.find_emulators),
            ("Emulator profiles", self.profiles_dialog),
            ("Backup library", self.backup),
            ("Restore backup", self.restore_backup),
        ):
            ttk.Button(sidebar, text=label, command=command).pack(fill="x", padx=15, pady=3)

        body = tk.Frame(self, bg=self.BG, padx=32, pady=26)
        body.pack(side="left", fill="both", expand=True)
        tk.Label(body, text="LINUX EDITION", bg=self.BG, fg=self.ACCENT, font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        tk.Label(body, text="Make your games beautiful.", bg=self.BG, fg=self.TEXT, font=("TkDefaultFont", 27, "bold")).pack(anchor="w", pady=(2, 0))
        tk.Label(body, text="One library for native games, emulators, DOSBox, and everything in between.", bg=self.BG, fg=self.MUTED).pack(anchor="w", pady=(0, 20))
        tools = tk.Frame(body, bg=self.BG)
        tools.pack(fill="x", pady=(0, 14))
        search = ttk.Entry(tools, textvariable=self.query)
        search.pack(side="left", fill="x", expand=True, padx=(0, 8))
        search.insert(0, "")
        search.bind("<KeyRelease>", lambda _: self.render())
        self.platform_box = ttk.Combobox(tools, textvariable=self.platform, state="readonly", width=17)
        self.platform_box.pack(side="left", padx=(0, 8))
        self.platform_box.bind("<<ComboboxSelected>>", lambda _: self.render())
        self.genre_box = ttk.Combobox(tools, textvariable=self.genre, state="readonly", width=17)
        self.genre_box.pack(side="left", padx=(0, 8))
        self.genre_box.bind("<<ComboboxSelected>>", lambda _: self.render())
        self.collection_box = ttk.Combobox(tools, textvariable=self.collection, state="readonly", width=17)
        self.collection_box.pack(side="left", padx=(0, 8))
        self.collection_box.bind("<<ComboboxSelected>>", lambda _: self.render())
        self.surprise_button = ttk.Button(tools, text="Surprise me", command=self.surprise_me)
        self.surprise_button.pack(side="left", padx=(0, 8))
        ttk.Button(tools, text="Add", command=self.add_game).pack(side="left", padx=(0, 8))
        ttk.Button(tools, text="Import", style="Accent.TButton", command=self.import_folder).pack(side="left")
        self.stats = tk.Label(body, bg=self.BG, fg=self.MUTED, anchor="w")
        self.stats.pack(fill="x", pady=(0, 10))
        main = tk.PanedWindow(body, orient="vertical", sashrelief="flat", bg=self.BG, borderwidth=0)
        main.pack(fill="both", expand=True)
        table_frame = tk.Frame(main, bg=self.BG)
        self.table = ttk.Treeview(table_frame, columns=("name", "platform", "genre", "played", "playtime", "collection"), show="headings", selectmode="browse")
        for key, title, width in (("name", "Game", 220), ("platform", "Platform", 130), ("genre", "Genre", 170), ("played", "Played", 70), ("playtime", "Play time", 90), ("collection", "Collection", 150)):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, anchor="w")
        self.table.pack(fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", lambda _: self.show_details())
        self.table.bind("<Double-1>", lambda _: self.launch_selected())
        main.add(table_frame, minsize=250, stretch="always")
        detail = tk.Frame(main, bg=self.PANEL, padx=18, pady=14)
        self.detail_title = tk.Label(detail, text="Select a game", bg=self.PANEL, fg=self.TEXT, font=("TkDefaultFont", 16, "bold"), anchor="w")
        self.detail_title.pack(fill="x")
        self.detail_meta = tk.Label(detail, bg=self.PANEL, fg=self.MUTED, anchor="w")
        self.detail_meta.pack(fill="x", pady=(3, 5))
        self.detail_description = tk.Label(detail, bg=self.PANEL, fg=self.TEXT, anchor="w", justify="left", wraplength=900)
        self.detail_description.pack(fill="x")
        detail_actions = tk.Frame(detail, bg=self.PANEL)
        detail_actions.pack(fill="x", pady=(10, 0))
        self.game_actions = [
            ttk.Button(detail_actions, text="Launch selected", command=self.launch_selected),
            ttk.Button(detail_actions, text="Edit metadata", command=self.edit_selected),
            ttk.Button(detail_actions, text="Toggle favorite", command=self.toggle_favorite),
        ]
        self.game_actions[0].pack(side="left")
        self.game_actions[1].pack(side="left", padx=8)
        self.game_actions[2].pack(side="left")
        main.add(detail, minsize=125, stretch="never")

    def set_view(self, view):
        self.view = view
        self.collection.set("All collections")
        self.render()

    def render(self):
        for item in self.table.get_children():
            self.table.delete(item)
        platforms = sorted({g.get("platform", "Imported") for g in self.games})
        genres = sorted({g.get("genre", "Other") for g in self.games})
        collections = sorted({g.get("collection") for g in self.games if g.get("collection")})
        self.platform_box["values"] = ["All platforms", *platforms]
        self.genre_box["values"] = ["All genres", *genres]
        self.collection_box["values"] = ["All collections", *collections]
        if self.platform.get() not in self.platform_box["values"]:
            self.platform.set("All platforms")
        if self.genre.get() not in self.genre_box["values"]:
            self.genre.set("All genres")
        if self.collection.get() not in self.collection_box["values"]:
            self.collection.set("All collections")
        query = self.query.get().lower().strip()
        visible = []
        for game in self.games:
            path = game.get("path", "")
            matches_view = (
                self.view == "all"
                or (self.view == "favorites" and game.get("favorite"))
                or (self.view == "recent" and game.get("last_played"))
                or (self.view == "never" and not game.get("play_count"))
                or (self.view == "missing" and path and not Path(path).exists())
            )
            matches = (not query or query in " ".join(str(game.get(key, "")) for key in ("name", "genre", "platform", "developer", "series")).lower())
            matches = matches and (self.platform.get() == "All platforms" or game.get("platform") == self.platform.get()) and (self.genre.get() == "All genres" or game.get("genre") == self.genre.get()) and (self.collection.get() == "All collections" or game.get("collection") == self.collection.get())
            if matches_view and matches:
                visible.append(game)
        if self.view == "recent":
            visible.sort(key=lambda game: game.get("last_played", ""), reverse=True)
        for index, game in enumerate(visible):
            name = ("★ " if game.get("favorite") else "") + game.get("name", "Untitled")
            self.table.insert("", "end", iid=str(index), values=(
                name,
                game.get("platform", "Imported"),
                game.get("genre", "Other"),
                game.get("play_count", 0),
                format_duration(game.get("playtime_seconds", 0)),
                game.get("collection", ""),
            ))
        for view, button in self.nav.items():
            button.configure(bg="#272b35" if view == self.view else self.SIDEBAR, fg=self.TEXT if view == self.view else self.MUTED)
        for child in self.collections_frame.winfo_children():
            child.destroy()
        for name in sorted({g.get("collection") for g in self.games if g.get("collection")}):
            tk.Button(self.collections_frame, text=name, command=lambda n=name: self.set_collection(n), relief="flat", anchor="w", padx=28, pady=5, bg=self.SIDEBAR, fg=self.MUTED, activebackground="#272b35", activeforeground=self.TEXT, bd=0).pack(fill="x")
        total_playtime = sum(game.get("playtime_seconds", 0) for game in self.games)
        self.stats["text"] = f"{len(visible)} shown  ·  {len(self.games)} games  ·  {len(platforms)} platforms  ·  {format_duration(total_playtime)} played"
        self.visible = visible
        self.surprise_button.configure(state="normal" if visible else "disabled")
        self.show_details()

    def set_collection(self, name):
        self.view = "all"
        self.collection.set(name)
        self.render()

    def selected(self):
        selection = self.table.selection()
        return self.visible[int(selection[0])] if selection else None

    def show_details(self):
        game = self.selected()
        for action in self.game_actions:
            action.configure(state="normal" if game else "disabled")
        if not game:
            self.detail_title["text"] = "Your library is empty" if not self.games else "Select a game"
            self.detail_meta["text"] = ""
            self.detail_description["text"] = "Import a folder or add a game to begin." if not self.games else ""
            return
        self.detail_title["text"] = ("★ " if game.get("favorite") else "") + game.get("name", "Untitled")
        facts = [game.get("platform"), game.get("genre"), game.get("year"), game.get("developer")]
        facts.extend((f"{game.get('play_count', 0)} launches", format_duration(game.get("playtime_seconds", 0))))
        meta = "  ·  ".join(str(value) for value in facts if value)
        self.detail_meta["text"] = meta or "No metadata yet"
        self.detail_description["text"] = game.get("description", "No description yet.")

    def import_folder(self):
        folder = filedialog.askdirectory(title="Import game folder")
        if not folder:
            return
        found = [path for path in Path(folder).rglob("*") if path.is_file() and path.suffix.lower() in EXTENSIONS]
        existing = {game.get("path") for game in self.games}
        for path in found:
            if str(path) in existing:
                continue
            suffix = path.suffix.lower()
            self.games.append({"name": path.stem, "platform": PLATFORM_BY_EXTENSION.get(suffix, "Imported"), "genre": "Local file", "path": str(path), "collection": "Imported", "added_at": datetime.now().isoformat(timespec="seconds")})
        save_state(self.state)
        self.render()

    def edit_selected(self):
        game = self.selected()
        if game:
            self.game_dialog(game)

    def add_game(self):
        self.game_dialog({"name": "", "platform": "Linux", "genre": "", "year": "", "developer": "", "series": "", "collection": "", "description": "", "path": "", "launch": "", "added_at": datetime.now().isoformat(timespec="seconds")})

    def game_dialog(self, game):
        dialog = tk.Toplevel(self)
        dialog.title("Edit game metadata")
        dialog.transient(self)
        dialog.grab_set()
        dialog.configure(bg=self.BG)
        fields = (("Name", "name"), ("Platform", "platform"), ("Genre", "genre"), ("Year", "year"), ("Developer", "developer"), ("Series", "series"), ("Collection", "collection"), ("Path", "path"), ("Launch command", "launch"))
        entries = {}
        for row, (label, key) in enumerate(fields):
            tk.Label(dialog, text=label, bg=self.BG, fg=self.TEXT, anchor="w").grid(row=row, column=0, padx=14, pady=5, sticky="w")
            entry = ttk.Entry(dialog, width=54)
            entry.insert(0, str(game.get(key, "")))
            entry.grid(row=row, column=1, padx=14, pady=5, sticky="ew")
            entries[key] = entry
        tk.Label(dialog, text="Description", bg=self.BG, fg=self.TEXT, anchor="nw").grid(row=len(fields), column=0, padx=14, pady=5, sticky="nw")
        description = tk.Text(dialog, width=40, height=4, bg=self.PANEL, fg=self.TEXT, insertbackground=self.TEXT, relief="flat")
        description.insert("1.0", game.get("description", ""))
        description.grid(row=len(fields), column=1, padx=14, pady=5, sticky="ew")
        actions = tk.Frame(dialog, bg=self.BG)
        actions.grid(row=len(fields) + 1, column=1, padx=14, pady=14, sticky="e")

        def save():
            if not entries["name"].get().strip():
                messagebox.showerror("Missing name", "A game needs a name.", parent=dialog)
                return
            for key, entry in entries.items():
                game[key] = entry.get().strip()
            game["description"] = description.get("1.0", "end").strip()
            if game not in self.games:
                self.games.append(game)
            save_state(self.state)
            dialog.destroy()
            self.render()

        ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="left", padx=5)
        ttk.Button(actions, text="Save", style="Accent.TButton", command=save).pack(side="left")
        dialog.columnconfigure(1, weight=1)
        entries["name"].focus_set()

    def launch_selected(self):
        game = self.selected()
        if not game:
            return
        self.launch_game(game)

    def launch_game(self, game):
        try:
            args, cwd = build_launch(game, self.profiles)
            process = subprocess.Popen(args, cwd=cwd)
            started = datetime.now()
            game["recent"] = True
            game["last_played"] = started.isoformat(timespec="seconds")
            game["play_count"] = game.get("play_count", 0) + 1
            save_state(self.state)
            self.render()
            threading.Thread(target=self.track_session, args=(process, game, started), daemon=True).start()
        except (OSError, ValueError) as error:
            messagebox.showerror("Could not launch", str(error))

    def track_session(self, process, game, started):
        exit_code = process.wait()
        seconds = max(1, int((datetime.now() - started).total_seconds()))
        self.after(0, lambda: self.finish_session(game, started, seconds, exit_code))

    def finish_session(self, game, started, seconds, exit_code):
        game["playtime_seconds"] = game.get("playtime_seconds", 0) + seconds
        self.history.append({
            "game": game.get("name", "Untitled"),
            "started": started.isoformat(timespec="seconds"),
            "seconds": seconds,
            "exit_code": exit_code,
        })
        self.history[:] = self.history[-500:]
        save_state(self.state)
        self.render()

    def toggle_favorite(self):
        game = self.selected()
        if game:
            game["favorite"] = not game.get("favorite", False)
            save_state(self.state)
            self.render()

    def surprise_me(self):
        candidates = self.visible or self.games
        if not candidates:
            return
        game = random.choice(candidates)
        if game not in self.visible:
            self.set_view("all")
        index = self.visible.index(game)
        self.table.selection_set(str(index))
        self.table.see(str(index))
        self.show_details()

    def play_history(self):
        window = tk.Toplevel(self)
        window.title("Play history")
        window.geometry("760x480")
        table = ttk.Treeview(window, columns=("game", "started", "duration", "result"), show="headings")
        for key, title, width in (("game", "Game", 220), ("started", "Started", 190), ("duration", "Duration", 110), ("result", "Exit", 70)):
            table.heading(key, text=title)
            table.column(key, width=width, anchor="w")
        for session in reversed(self.history):
            table.insert("", "end", values=(
                session.get("game", "Untitled"),
                session.get("started", ""),
                format_duration(session.get("seconds", 0)),
                session.get("exit_code", ""),
            ))
        table.pack(fill="both", expand=True)

    def library_health(self):
        configured = [game for game in self.games if game.get("path")]
        missing = [game for game in configured if not Path(game["path"]).exists()]
        paths = [game["path"] for game in configured]
        duplicate_paths = len(paths) - len(set(paths))
        unconfigured = len(self.games) - len(configured)
        messagebox.showinfo(
            "Library health",
            f"{len(self.games)} games checked\n\n"
            f"{len(missing)} missing files\n"
            f"{duplicate_paths} duplicate paths\n"
            f"{unconfigured} games need a launch path",
        )
        if missing:
            self.set_view("missing")

    def find_emulators(self):
        found = discover_profiles()
        added = {platform: command for platform, command in found.items() if platform not in self.profiles}
        self.profiles.update(added)
        save_state(self.state)
        if added:
            messagebox.showinfo("Emulators found", "Added profiles for:\n" + "\n".join(sorted(added)))
        else:
            messagebox.showinfo("Emulators found", "No new supported emulators were found on PATH.")

    def big_box(self):
        window = tk.Toplevel(self)
        window.title("OpenBox · Big Box")
        window.geometry("1050x700")
        window.configure(bg="#090a0d")
        tk.Label(window, text="BIG BOX", bg="#090a0d", fg=self.ACCENT, font=("TkDefaultFont", 13, "bold"), pady=20).pack()
        listbox = tk.Listbox(window, bg="#161920", fg=self.TEXT, selectbackground="#4c3b29", selectforeground=self.TEXT, font=("TkDefaultFont", 22, "bold"), activestyle="none", borderwidth=0, highlightthickness=0)
        listbox.pack(fill="both", expand=True, padx=35, pady=(0, 20))
        for game in self.visible:
            listbox.insert("end", ("★ " if game.get("favorite") else "") + game.get("name", "Untitled") + "    ·    " + game.get("platform", "Imported"))
        def launch_box(_=None):
            selection = listbox.curselection()
            if selection:
                self.table.selection_set(str(selection[0])) if str(selection[0]) in self.table.get_children() else None
                window.destroy()
                self.launch_game(self.visible[selection[0]])
        listbox.bind("<Double-1>", launch_box)
        ttk.Button(window, text="Launch", command=launch_box).pack(pady=(0, 20))

    def profiles_dialog(self):
        dialog = tk.Toplevel(self)
        dialog.title("Emulator profiles")
        dialog.transient(self)
        dialog.grab_set()
        dialog.configure(bg=self.BG)
        tk.Label(dialog, text="One profile per line: Platform = command", bg=self.BG, fg=self.MUTED, padx=14, pady=12).pack(anchor="w")
        text = tk.Text(dialog, width=80, height=14, bg=self.PANEL, fg=self.TEXT, insertbackground=self.TEXT, relief="flat")
        text.pack(padx=14, pady=(0, 10))
        text.insert("1.0", "\n".join(f"{platform} = {command}" for platform, command in sorted(self.profiles.items())))
        tk.Label(dialog, text="Use {path} where the ROM should go. If omitted, the path is appended.", bg=self.BG, fg=self.MUTED, padx=14).pack(anchor="w")
        def save():
            profiles = {}
            for line in text.get("1.0", "end").splitlines():
                if "=" in line:
                    platform, command = line.split("=", 1)
                    if platform.strip() and command.strip():
                        profiles[platform.strip()] = command.strip()
            self.profiles.clear()
            self.profiles.update(profiles)
            save_state(self.state)
            dialog.destroy()
        ttk.Button(dialog, text="Save", style="Accent.TButton", command=save).pack(anchor="e", padx=14, pady=14)

    def backup(self):
        path = filedialog.asksaveasfilename(title="Backup library", defaultextension=".json", filetypes=[("JSON library", "*.json")])
        if path:
            Path(path).write_text(json.dumps(self.state, indent=2))

    def restore_backup(self):
        path = filedialog.askopenfilename(title="Restore library backup", filetypes=[("JSON library", "*.json")])
        if not path:
            return
        try:
            restored = json.loads(Path(path).read_text())
            if isinstance(restored, list):
                restored = {"games": restored, "profiles": {}, "history": []}
            if not isinstance(restored, dict) or not isinstance(restored.get("games"), list):
                raise ValueError("This file is not a valid library backup.")
        except (OSError, json.JSONDecodeError, ValueError) as error:
            messagebox.showerror("Could not restore", str(error))
            return
        if not messagebox.askyesno("Restore backup", f"Replace the current library with {len(restored['games'])} games?"):
            return
        DATA.parent.mkdir(parents=True, exist_ok=True)
        DATA.with_name("library.before-restore.json").write_text(json.dumps(self.state, indent=2))
        restored.setdefault("profiles", {})
        restored.setdefault("history", [])
        self.state = restored
        self.games = self.state["games"]
        self.profiles = self.state["profiles"]
        self.history = self.state["history"]
        save_state(self.state)
        self.set_view("all")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        assert {"games", "profiles", "history"} <= load_state().keys()
        assert "{path}" in "retroarch -L core.so {path}"
        assert shlex.split("retroarch -L core.so {path}")[-1] == "{path}"
        assert format_duration(3720) == "1h 2m"
        assert discover_profiles(lambda binary: f"/usr/bin/{binary}" if binary == "wine" else None) == {"Windows": "wine {path}"}
        try:
            build_launch({"name": "Missing", "path": ""}, {})
        except ValueError:
            pass
        else:
            raise AssertionError("empty paths must not launch")
        print("openbox self-test: ok")
    else:
        configure_logging(APP_DIR)
        OpenBox().mainloop()
