"""
LellaVerse Smart Organizer Pro  (v3)

Install:  pip install watchdog pystray pillow
Run:      python lellaverse_organizer.py
(pystray + pillow are only needed for the system-tray icon; the app works without them.)
"""
import calendar
import copy
import filecmp
import hashlib
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from glob import glob
from tkinter import filedialog, messagebox, ttk

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
APP_NAME = "LellaVerse Smart Organizer"
APP_VERSION = "1.0.0"
AUTHOR = "Lelum Anuradha Fernando"
GITHUB_URL = "https://github.com/LelumAnuradha/LellaVerse-Smart-Organizer"   # <- change YOUR-USERNAME
HOME = os.path.expanduser("~")
CONFIG_FILE = os.path.join(HOME, "LellaVerse_Organizer_config.json")
HISTORY_FILE = os.path.join(HOME, "LellaVerse_Organizer_history.json")
LOCK_PORT = 48653                 # used to stop two copies running at once
STARTUP_DELAY_MS = 20000          # when started by Windows, wait so the PC boots fast
HISTORY_LIMIT = 300
IS_WIN = sys.platform == "win32"

SPECIAL = ["Folders", "Infected_Malware", "Others", "Duplicates"]
NO_DATE_FOLDERS = {"folders", "infected_malware", "duplicates"}
TEMP_EXT = (".crdownload", ".part", ".tmp", ".download", ".partial", ".opdownload")
IGNORED_NAMES = {"desktop.ini", "thumbs.db"}
INVALID_CHARS = set('\\/:*?"<>|')
EXT_RE = re.compile(r"^\.[a-z0-9_+\-]{1,15}$")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

DATE_MODES = ["off", "week", "month"]
DATE_LABELS = [
    "Off  -  files go straight into the category folder",
    "Week by week  (e.g.  2026-10-Oct-Week3)",
    "Month by month  (e.g.  2026-10-Oct)",
]
DATE_SHORT = {"off": "Off", "week": "Weekly", "month": "Monthly"}
KIND_LABELS = {"move": "Sorted", "duplicate": "Duplicate", "virus": "Virus"}

DEFAULT_CATEGORIES = {
    "Images": [".jpg", ".jpeg", ".png", ".gif", ".webp"],
    "Software": [".exe", ".msi"],
    "Compressed": [".zip", ".rar", ".7z", ".tar", ".gz"],
    "Documents": [".pdf", ".docx", ".txt", ".pptx", ".xlsx", ".csv"],
    "Media": [".mp4", ".mp3", ".wav", ".mkv"],
}

DEFAULT_SETTINGS = {
    "sort_existing": True,        # sort what is already inside when START is pressed
    "scan_virus": True,           # Defender scan for NEW files
    "scan_existing": False,       # Defender scan for old files during first sort (slow)
    "check_duplicates": True,     # identical files -> Duplicates folder
    "auto_start_watching": False,  # start watching when the app opens
    "close_to_tray": True,        # X button hides to tray
    "start_with_windows": False,
}

CONFIG_LOAD_ERROR = None


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def norm(p):
    return os.path.normcase(os.path.abspath(p))


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def new_folder_rule(path, categories=None, date_mode="off"):
    return {
        "path": path,
        "categories": copy.deepcopy(categories if categories is not None else DEFAULT_CATEGORIES),
        "date_mode": date_mode if date_mode in DATE_MODES else "off",
    }


def valid_categories(c):
    return (isinstance(c, dict) and c
            and all(isinstance(k, str) and isinstance(v, list) and all(isinstance(e, str) for e in v)
                    for k, v in c.items()))


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
def default_config():
    c = dict(DEFAULT_SETTINGS)
    c["version"] = 3
    c["folders"] = [new_folder_rule(os.path.join(HOME, "Downloads"))]
    return c


def load_config():
    global CONFIG_LOAD_ERROR
    c = default_config()
    if not os.path.exists(CONFIG_FILE):
        return c
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in DEFAULT_SETTINGS.items():
            if k in data and isinstance(data[k], type(v)):
                c[k] = data[k]
        if isinstance(data.get("folders"), list):
            folders = []
            for f in data["folders"]:
                if isinstance(f, dict) and isinstance(f.get("path"), str):
                    cats = f.get("categories") if valid_categories(f.get("categories")) else DEFAULT_CATEGORIES
                    folders.append(new_folder_rule(f["path"], cats, f.get("date_mode", "off")))
            c["folders"] = folders
        else:  # settings file from an older version
            paths = data.get("watch_paths")
            if not isinstance(paths, list) and isinstance(data.get("watch_path"), str):
                paths = [data["watch_path"]]
            if isinstance(paths, list) and paths:
                cats = data.get("categories") if valid_categories(data.get("categories")) else DEFAULT_CATEGORIES
                c["folders"] = [new_folder_rule(p, cats) for p in paths if isinstance(p, str)]
    except Exception as e:
        CONFIG_LOAD_ERROR = f"Could not read the settings file, using defaults. ({e})"
    return c


def save_config():
    try:
        write_json(CONFIG_FILE, cfg)
        return True, ""
    except Exception as e:
        return False, str(e)


cfg = load_config()

# ----------------------------------------------------------------------------
# Thread -> UI messages
# ----------------------------------------------------------------------------
ui_queue = queue.Queue()


def log(text, level="info"):
    ui_queue.put(("log", text, level))


def stat(key):
    ui_queue.put(("stat", key))


# ----------------------------------------------------------------------------
# History (for Undo)
# ----------------------------------------------------------------------------
_history = []
_pinned = set()          # files the user undid: never auto-sort them again
_hist_lock = threading.Lock()


def save_history():
    try:
        write_json(HISTORY_FILE, {"moves": _history, "pinned": sorted(_pinned)})
    except Exception:
        pass


def load_history():
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            need = {"id", "time", "src", "dst", "kind"}
            moves = [m for m in data.get("moves", []) if isinstance(m, dict) and need <= set(m)]
            _history[:] = moves[-HISTORY_LIMIT:]
            _pinned.update(p for p in data.get("pinned", []) if isinstance(p, str) and os.path.exists(p))
    except Exception:
        pass


def record_move(src, dst, kind):
    with _hist_lock:
        _history.append({"id": time.time_ns(), "time": datetime.now().isoformat(timespec="seconds"),
                         "src": src, "dst": dst, "kind": kind})
        del _history[:-HISTORY_LIMIT]
        save_history()
    ui_queue.put(("history",))


def is_pinned(path):
    return norm(path) in _pinned


def forget_pins():
    with _hist_lock:
        _pinned.clear()
        save_history()


def unique_numbered(dest_dir, filename):
    base, ext = os.path.splitext(filename)
    dest = os.path.join(dest_dir, filename)
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(dest_dir, f"{base} ({n}){ext}")
        n += 1
    return dest


def undo_move(entry_id):
    """Put a moved file back. Returns (ok, message)."""
    with _hist_lock:
        entry = next((e for e in _history if e["id"] == entry_id), None)
    if entry is None:
        return False, "That move is not in the history any more."
    src, dst = entry["src"], entry["dst"]
    name = os.path.basename(src)
    if not os.path.exists(dst):
        return False, f"'{name}' is no longer at:\n{dst}\n\nIt was moved or deleted."
    target_dir = os.path.dirname(src)
    if not os.path.isdir(target_dir):
        return False, f"The original folder no longer exists:\n{target_dir}"
    target = src if not os.path.exists(src) else unique_numbered(target_dir, name)
    with _hist_lock:
        _pinned.add(norm(target))       # pin BEFORE moving so the watcher ignores it
    try:
        shutil.move(dst, target)
    except Exception as e:
        with _hist_lock:
            _pinned.discard(norm(target))
        return False, f"Could not undo '{name}': {e}"
    with _hist_lock:
        _history[:] = [e for e in _history if e["id"] != entry_id]
        save_history()
    # remove the empty date folder (e.g. Images\2026-10-Oct-Week3), never the category folder
    parent = os.path.dirname(dst)
    if norm(os.path.dirname(parent)) != norm(target_dir):
        try:
            os.rmdir(parent)
        except OSError:
            pass
    ui_queue.put(("history",))
    return True, f"Restored '{name}' to {target_dir}"


load_history()

# ----------------------------------------------------------------------------
# Windows Defender
# ----------------------------------------------------------------------------
def find_defender():
    paths = [r"C:\Program Files\Windows Defender\MpCmdRun.exe"]
    paths += sorted(glob(r"C:\ProgramData\Microsoft\Windows Defender\Platform\*\MpCmdRun.exe"), reverse=True)
    for p in paths:
        if os.path.exists(p):
            return p
    return None


DEFENDER_PATH = find_defender()
_warned_defender = False
_scan_sem = threading.Semaphore(1)       # one scan at a time = light on the PC


def scan_for_malware(file_path):
    global _warned_defender
    if not DEFENDER_PATH:
        if not _warned_defender:
            _warned_defender = True
            log("Windows Defender was not found - virus scan is skipped.", "warning")
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | (0x00004000 if IS_WIN else 0)  # below-normal priority
    try:
        with _scan_sem:
            result = subprocess.run(
                [DEFENDER_PATH, "-Scan", "-ScanType", "3", "-File", file_path],
                capture_output=True, text=True, timeout=180, creationflags=flags)
        return result.returncode == 2          # 2 = threat found
    except subprocess.TimeoutExpired:
        log(f"Virus scan timed out: {os.path.basename(file_path)}", "warning")
    except Exception as e:
        log(f"Virus scan failed for {os.path.basename(file_path)}: {e}", "warning")
    return False


# ----------------------------------------------------------------------------
# Organizer engine
# ----------------------------------------------------------------------------
_lock = threading.Lock()
_busy = set()
_organizing = threading.Event()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="organizer")
SELF_PATHS = {norm(sys.argv[0]), norm(sys.executable)}


def rule_for(path):
    """The folder rule whose folder directly contains `path`."""
    parent = norm(os.path.dirname(os.path.abspath(path)))
    for r in cfg["folders"]:
        if norm(r["path"]) == parent:
            return r
    return None


def managed_names(rule):
    return {n.lower() for n in list(rule["categories"].keys()) + SPECIAL}


def is_ignored(path):
    name = os.path.basename(path).lower()
    return name in IGNORED_NAMES or name.endswith(TEMP_EXT) or norm(path) in SELF_PATHS


def check_watch_path(p):
    if not p or not os.path.isdir(p):
        return False, f"This folder does not exist:\n{p}\n\nPlease choose another folder."
    if os.path.dirname(os.path.abspath(p)) == os.path.abspath(p):
        return False, "Please do not choose a whole drive (like C:\\). Choose a normal folder."
    if not os.access(p, os.W_OK):
        return False, f"No permission to change files in:\n{p}\n\nChoose another folder or run as administrator."
    return True, ""


def folder_conflict(path):
    """Message if `path` duplicates / overlaps a folder already in the list, else None."""
    b = norm(path)
    for r in cfg["folders"]:
        a = norm(r["path"])
        if a == b:
            return f"This folder is already in the list:\n{path}"
        if b.startswith(a + os.sep) or a.startswith(b + os.sep):
            return (f"This folder is inside (or contains) another folder in the list:\n{r['path']}\n\n"
                    "Choose a folder that is separate from the others.")
    return None


def create_category_folders(rule):
    created = 0
    for name in list(rule["categories"].keys()) + SPECIAL:
        p = os.path.join(rule["path"], name)
        if not os.path.isdir(p):
            os.makedirs(p, exist_ok=True)
            created += 1
    return created


def find_category(rule, ext):
    for folder, exts in rule["categories"].items():
        if ext in exts:
            return folder
    return None


def file_date(path):
    """When the file arrived on this PC (creation time on Windows)."""
    try:
        st = os.stat(path)
        ts = st.st_ctime if IS_WIN else getattr(st, "st_birthtime", st.st_mtime)
        return datetime.fromtimestamp(ts)
    except OSError:
        return datetime.now()


def date_subfolder(dt, mode):
    """'2026-10-Oct'  or  '2026-10-Oct-Week3'  (weeks follow the calendar rows, Monday first)."""
    month = f"{dt.year}-{dt.month:02d}-{MONTHS[dt.month - 1]}"
    if mode == "month":
        return month
    for i, row in enumerate(calendar.monthcalendar(dt.year, dt.month), 1):
        if dt.day in row:
            return f"{month}-Week{i}"
    return month


def quick_hash(path):
    """Fast fingerprint: size + first 1 MB + last 1 MB."""
    h = hashlib.sha1()
    try:
        size = os.path.getsize(path)
        h.update(str(size).encode())
        with open(path, "rb") as f:
            h.update(f.read(1024 * 1024))
            if size > 2 * 1024 * 1024:
                f.seek(-1024 * 1024, os.SEEK_END)
                h.update(f.read(1024 * 1024))
    except OSError:
        pass
    return h.hexdigest()


def find_duplicate(src, search_dir):
    """A file inside search_dir with exactly the same content as src, or None."""
    try:
        size = os.path.getsize(src)
    except OSError:
        return None
    if size == 0 or not os.path.isdir(search_dir):
        return None
    me = norm(src)
    for root, _dirs, files in os.walk(search_dir):
        for f in files:
            p = os.path.join(root, f)
            try:
                if os.path.getsize(p) == size and norm(p) != me and filecmp.cmp(src, p, shallow=False):
                    return p
            except OSError:
                continue
    return None


def clash_path(src, dest_dir, name, is_dir=False):
    """Same name but different file: name_20261017_a3f9c1.ext  (date + content fingerprint)."""
    base, ext = os.path.splitext(name)
    tag = file_date(src).strftime("%Y%m%d")
    if not is_dir:
        tag += "_" + quick_hash(src)[:6]
    cand = os.path.join(dest_dir, f"{base}_{tag}{ext}")
    n = 1
    while os.path.exists(cand):
        cand = os.path.join(dest_dir, f"{base}_{tag} ({n}){ext}")
        n += 1
    return cand


def wait_until_ready(path, timeout=600):
    """Wait until the file stops growing and is not locked (download finished)."""
    try:
        if time.time() - os.path.getmtime(path) > 10:
            return True
    except OSError:
        return False
    last_size = -1
    start = time.time()
    while time.time() - start < timeout:
        if not os.path.exists(path):
            return False
        try:
            size = os.path.getsize(path)
            with open(path, "rb"):
                pass
            if size == last_size:
                return True
            last_size = size
        except OSError:
            pass
        time.sleep(1.5)
    return False


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def wait_dir_ready(path, timeout=120):
    try:
        if time.time() - os.path.getmtime(path) > 10:
            return True
    except OSError:
        return False
    last = -1
    start = time.time()
    while time.time() - start < timeout:
        if not os.path.exists(path):
            return False
        size = dir_size(path)
        if size == last:
            return True
        last = size
        time.sleep(1.5)
    return False


def move_item(src, rule, folder, sub=None, kind="move", check_dupes=False):
    name = os.path.basename(src)
    watch = rule["path"]
    is_dir = os.path.isdir(src)
    try:
        if check_dupes and cfg["check_duplicates"] and not is_dir:
            twin = find_duplicate(src, os.path.join(watch, folder))
            if twin:
                log(f"Duplicate: '{name}' is identical to '{os.path.relpath(twin, watch)}'", "warning")
                stat("dupes")
                folder, sub, kind = "Duplicates", None, "duplicate"
        dest_dir = os.path.join(watch, folder, sub) if sub else os.path.join(watch, folder)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, name)
        if os.path.exists(dest):
            dest = clash_path(src, dest_dir, name, is_dir)
            log(f"Same name, different file: '{name}' saved as '{os.path.basename(dest)}'", "info")
        shutil.move(src, dest)
        record_move(src, dest, kind)
        stat("moved")
        log(f"{name}  →  {folder + os.sep + sub if sub else folder}", "success")
        return True
    except PermissionError:
        stat("errors")
        log(f"Cannot move '{name}' - it is open in another program or access is denied.", "error")
    except Exception as e:
        stat("errors")
        log(f"Cannot move '{name}': {e}", "error")
    return False


def process_item(path, scan=True):
    path = os.path.abspath(path)
    rule = rule_for(path)
    if rule is None:
        return
    name = os.path.basename(path)
    if name.lower() in managed_names(rule) or is_ignored(path) or is_pinned(path):
        return

    with _lock:
        if path in _busy:
            return
        _busy.add(path)
    try:
        if not os.path.exists(path):
            return

        if os.path.isdir(path):
            if not wait_dir_ready(path):
                log(f"Skipped folder (still changing): {name}", "warning")
                return
            move_item(path, rule, "Folders")
            return

        if not wait_until_ready(path):
            log(f"Skipped (still in use or not finished): {name}", "warning")
            return

        if scan and cfg["scan_virus"] and scan_for_malware(path):
            stat("infected")
            log(f"VIRUS DETECTED: {name} → Infected_Malware", "error")
            ui_queue.put(("notify", "Virus detected", f"{name} was moved to Infected_Malware"))
            move_item(path, rule, "Infected_Malware", kind="virus")
            return

        ext = os.path.splitext(name)[1].lower()
        folder = find_category(rule, ext) or "Others"
        sub = None
        if rule.get("date_mode", "off") != "off" and folder.lower() not in NO_DATE_FOLDERS:
            sub = date_subfolder(file_date(path), rule["date_mode"])
        move_item(path, rule, folder, sub, check_dupes=True)
    except Exception as e:
        stat("errors")
        log(f"Unexpected error with '{name}': {e}", "error")
    finally:
        with _lock:
            _busy.discard(path)


def organize_existing():
    """Sort everything already inside the watched folders (background thread)."""
    if _organizing.is_set():
        log("Sorting is already running...", "warning")
        return
    _organizing.set()

    def run():
        try:
            todo = []
            for rule in list(cfg["folders"]):
                try:
                    items = sorted(os.listdir(rule["path"]))
                except Exception as e:
                    ui_queue.put(("popup", "Cannot read folder", f"{rule['path']}\n\n{e}"))
                    continue
                names = managed_names(rule)
                todo += [os.path.join(rule["path"], i) for i in items
                         if i.lower() not in names and not is_ignored(os.path.join(rule["path"], i))]
            total = len(todo)
            if total == 0:
                log("Nothing to sort - the folders are already clean.", "info")
            ui_queue.put(("progress", 0, total))
            for i, p in enumerate(todo, 1):
                process_item(p, scan=cfg["scan_existing"])
                ui_queue.put(("progress", i, total))
            if total:
                log(f"Finished sorting {total} item(s).", "success")
        finally:
            _organizing.clear()
            ui_queue.put(("done",))

    threading.Thread(target=run, daemon=True).start()


class OrganizerHandler(FileSystemEventHandler):
    @staticmethod
    def _spawn(path):
        try:
            _executor.submit(process_item, path, True)
        except RuntimeError:
            pass  # app is closing

    def on_created(self, event):
        self._spawn(event.src_path)

    def on_moved(self, event):      # Chrome/Edge rename .crdownload -> real file
        self._spawn(event.dest_path)


# ----------------------------------------------------------------------------
# Windows start-up, priority, single instance, tray
# ----------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")     # no black console window
    if os.path.exists(pyw):
        exe = pyw
    return f'"{exe}" "{os.path.abspath(sys.argv[0])}" --minimized'


def set_autostart(enable):
    if not IS_WIN:
        raise RuntimeError("Start with Windows only works on Windows.")
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


def lower_priority():
    """Run below-normal priority so the app never slows the PC down."""
    if IS_WIN:
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000)
        except Exception:
            pass


_instance_sock = None


def acquire_single_instance():
    global _instance_sock
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        _instance_sock = s
        return True
    except OSError:
        return False


class Tray:
    """System tray icon (needs: pip install pystray pillow). Optional."""

    def __init__(self):
        self.icon = None

    @property
    def running(self):
        return self.icon is not None

    def start(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return False
        try:
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(0, 212, 170, 255))
            d.polygon([(32, 14), (48, 22), (48, 36), (32, 50), (16, 36), (16, 22)], fill=(15, 18, 24, 255))

            def cmd(name):
                return lambda icon, item: ui_queue.put(("cmd", name))

            menu = pystray.Menu(
                pystray.MenuItem("Open", cmd("show"), default=True),
                pystray.MenuItem("Sort now", cmd("sort")),
                pystray.MenuItem("Pause / Resume watching", cmd("toggle")),
                pystray.MenuItem("Quit", cmd("quit")),
            )
            self.icon = pystray.Icon("LellaVerseOrganizer", img, APP_NAME, menu)
            self.icon.run_detached()
            return True
        except Exception:
            self.icon = None
            return False

    def set_title(self, text):
        try:
            if self.icon:
                self.icon.title = text
        except Exception:
            pass

    def notify(self, title, msg):
        try:
            if self.icon:
                self.icon.notify(msg, title)
        except Exception:
            pass

    def stop(self):
        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass
        self.icon = None


# ----------------------------------------------------------------------------
# Category validation
# ----------------------------------------------------------------------------
def validate_category(name, ext_text, cats, editing=None):
    name = name.strip()
    if not name:
        return False, "Please enter a folder name."
    if len(name) > 60:
        return False, "Folder name is too long (max 60 characters)."
    if any(c in INVALID_CHARS for c in name):
        return False, 'A folder name cannot contain  \\ / : * ? " < > |'
    if name.endswith("."):
        return False, "A folder name cannot end with a dot."
    if name.lower() in {s.lower() for s in SPECIAL}:
        return False, f'"{name}" is used by the app. Please choose another name.'
    for existing in cats:
        if existing.lower() == name.lower() and existing != editing:
            return False, f'A folder called "{existing}" already exists in this list.'

    raw = [e.strip().lower() for e in re.split(r"[,\s;]+", ext_text) if e.strip()]
    if not raw:
        return False, "Please enter at least one file extension (example: .py, .html)."
    exts = []
    for e in raw:
        e = e.lstrip("*")
        if not e.startswith("."):
            e = "." + e
        if not EXT_RE.match(e):
            return False, f'"{e}" is not a valid extension. Use letters/numbers only, like .py'
        if e not in exts:
            exts.append(e)
    for cat, lst in cats.items():
        if cat == editing:
            continue
        clash = [e for e in exts if e in lst]
        if clash:
            return False, f'{", ".join(clash)} already belongs to "{cat}". Remove it there first.'
    return True, (name, exts)


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
BG = "#0f1218"
CARD = "#181d27"
CARD2 = "#232a3a"
ACCENT = "#00d4aa"
TEXT = "#e8ebf1"
MUTED = "#8c95a6"
RED = "#ff5c6c"
ORANGE = "#ffb347"
GREEN = "#3ddc84"
BTN = "#2d3547"
BTN_HOVER = "#3a4359"
FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
FONT_H = ("Segoe UI", 11, "bold")
FONT_S = ("Segoe UI", 9)
FONT_MONO = ("Consolas", 9)


class FlatButton(tk.Button):
    def __init__(self, parent, text, command, color=BTN, hover=BTN_HOVER, fg=TEXT, **kw):
        super().__init__(
            parent, text=text, command=command, bg=color, fg=fg,
            activebackground=hover, activeforeground=fg, bd=0, relief="flat",
            padx=14, pady=7, font=FONT_B, cursor="hand2", highlightthickness=0,
            disabledforeground="#6b7280", **kw)
        self.color, self.hover = color, hover
        self.bind("<Enter>", lambda e: self["state"] == "normal" and self.config(bg=self.hover))
        self.bind("<Leave>", lambda e: self["state"] == "normal" and self.config(bg=self.color))

    def set_enabled(self, enabled):
        if enabled:
            self.config(state="normal", bg=self.color, cursor="hand2")
        else:
            self.config(state="disabled", bg="#2a2f3a", cursor="arrow")


def card(parent, title, subtitle=None):
    frame = tk.Frame(parent, bg=CARD, padx=16, pady=12)
    head = tk.Frame(frame, bg=CARD)
    head.pack(fill="x")
    tk.Label(head, text=title, font=FONT_H, bg=CARD, fg=TEXT).pack(side="left")
    if subtitle:
        tk.Label(head, text=subtitle, font=FONT_S, bg=CARD, fg=MUTED).pack(side="left", padx=10)
    return frame


def dark_entry(parent, var, width=42):
    return tk.Entry(parent, textvariable=var, width=width, bg=CARD2, fg=TEXT, insertbackground=TEXT,
                    bd=0, font=FONT, highlightthickness=1, highlightbackground=CARD2, highlightcolor=ACCENT)


class CategoryDialog(tk.Toplevel):
    def __init__(self, parent, title, cats, name="", exts="", editing=None):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=CARD, padx=22, pady=18)
        self.resizable(False, False)
        self.cats, self.editing, self.result = cats, editing, None

        tk.Label(self, text=title, font=FONT_H, bg=CARD, fg=ACCENT).grid(row=0, column=0, sticky="w", pady=(0, 12))
        tk.Label(self, text="Folder name", font=FONT, bg=CARD, fg=TEXT).grid(row=1, column=0, sticky="w")
        self.name_var = tk.StringVar(value=name)
        e1 = dark_entry(self, self.name_var)
        e1.grid(row=2, column=0, ipady=6, pady=(2, 10), sticky="we")
        tk.Label(self, text="File extensions (separate with commas)", font=FONT, bg=CARD, fg=TEXT).grid(row=3, column=0, sticky="w")
        self.ext_var = tk.StringVar(value=exts)
        dark_entry(self, self.ext_var).grid(row=4, column=0, ipady=6, pady=(2, 2), sticky="we")
        tk.Label(self, text="Example:  .py, .html, .css", font=FONT_S, bg=CARD, fg=MUTED).grid(row=5, column=0, sticky="w")
        self.err = tk.Label(self, text="", font=("Segoe UI", 9, "bold"), bg=CARD, fg=RED, wraplength=330, justify="left")
        self.err.grid(row=6, column=0, sticky="w", pady=(8, 0))
        btns = tk.Frame(self, bg=CARD)
        btns.grid(row=7, column=0, sticky="e", pady=(12, 0))
        FlatButton(btns, "Cancel", self.destroy).pack(side="left", padx=6)
        FlatButton(btns, "Save", self.save, color=ACCENT, hover="#00b894", fg="#06241d").pack(side="left")
        self.bind("<Return>", lambda e: self.save())
        self.bind("<Escape>", lambda e: self.destroy())
        self.transient(parent)
        self.grab_set()
        e1.focus_set()

    def save(self):
        ok, res = validate_category(self.name_var.get(), self.ext_var.get(), self.cats, self.editing)
        if not ok:
            self.err.config(text="✖  " + res)
            return
        self.result = res
        self.destroy()


class NewFolderDialog(tk.Toplevel):
    """Asked when you add a folder: which rules should it use?"""

    def __init__(self, parent, path, rules):
        super().__init__(parent)
        self.title("Rules for this folder")
        self.configure(bg=CARD, padx=22, pady=18)
        self.resizable(False, False)
        self.rules, self.result = rules, None

        tk.Label(self, text="New folder to organize", font=FONT_H, bg=CARD, fg=ACCENT).pack(anchor="w")
        tk.Label(self, text=path, font=FONT_MONO, bg=CARD, fg=TEXT, wraplength=470, justify="left").pack(anchor="w", pady=(4, 12))

        tk.Label(self, text="Which folders & file types should it use?", font=FONT_B, bg=CARD, fg=TEXT).pack(anchor="w")
        self.mode = tk.StringVar(value="default")
        rb = dict(bg=CARD, fg=TEXT, selectcolor=CARD2, activebackground=CARD, activeforeground=ACCENT,
                  font=FONT, bd=0, highlightthickness=0, anchor="w")
        tk.Radiobutton(self, text="The default set (Images, Documents, Media ...)", variable=self.mode,
                       value="default", **rb).pack(anchor="w", pady=(4, 0))
        if rules:
            tk.Radiobutton(self, text="Copy the rules of another folder:", variable=self.mode,
                           value="copy", **rb).pack(anchor="w")
            self.copy_var = tk.StringVar(value=rules[0]["path"])
            ttk.Combobox(self, textvariable=self.copy_var, values=[r["path"] for r in rules],
                         state="readonly", style="Dark.TCombobox", width=58).pack(anchor="w", padx=(22, 0), pady=(2, 0))

        tk.Label(self, text="Date folders inside each category", font=FONT_B, bg=CARD, fg=TEXT).pack(anchor="w", pady=(14, 2))
        self.date_combo = ttk.Combobox(self, values=DATE_LABELS, state="readonly", style="Dark.TCombobox", width=58)
        self.date_combo.current(0)
        self.date_combo.pack(anchor="w")
        tk.Label(self, text="You can change all of this later.", font=FONT_S, bg=CARD, fg=MUTED).pack(anchor="w", pady=(8, 0))

        btns = tk.Frame(self, bg=CARD)
        btns.pack(anchor="e", pady=(14, 0))
        FlatButton(btns, "Cancel", self.destroy).pack(side="left", padx=6)
        FlatButton(btns, "Add folder", self.ok, color=ACCENT, hover="#00b894", fg="#06241d").pack(side="left")
        self.bind("<Escape>", lambda e: self.destroy())
        self.transient(parent)
        self.grab_set()

    def ok(self):
        cats = DEFAULT_CATEGORIES
        if self.mode.get() == "copy":
            src = next((r for r in self.rules if r["path"] == self.copy_var.get()), None)
            if src:
                cats = src["categories"]
        self.result = (copy.deepcopy(cats), DATE_MODES[self.date_combo.current()])
        self.destroy()


class OrganizerApp:
    def __init__(self, root, minimized=False):
        self.root = root
        self.minimized = minimized
        self.observer = None
        self.watches = {}
        self.is_running = False
        self.visible = True
        self.tray_hint_shown = False
        self.stats = {"moved": 0, "dupes": 0, "infected": 0, "errors": 0}
        self.tray = Tray()
        self.tray_ok = self.tray.start()

        root.title(APP_NAME + " Pro")
        root.geometry("960x800")
        root.minsize(880, 720)
        root.configure(bg=BG)
        self.setup_styles()
        self.build_ui()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.refresh_folders(select=0)
        self.refresh_history()
        self.poll_queue()
        self.log("Ready. Add your folders, check the rules, then press START.", "info")
        if CONFIG_LOAD_ERROR:
            self.log(CONFIG_LOAD_ERROR, "warning")
        if not self.tray_ok:
            self.log("Tray icon is off. To enable it:  pip install pystray pillow", "info")

        if minimized:
            self.hide_window()
        if minimized or cfg["auto_start_watching"]:
            root.after(STARTUP_DELAY_MS if minimized else 600, self.auto_start)

    # ---------- window / tray ----------
    def hide_window(self):
        if self.tray.running:
            self.root.withdraw()
        else:
            self.root.iconify()
        self.visible = False

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass
        self.visible = True

    def on_close(self):
        if cfg["close_to_tray"] and self.tray.running:
            self.hide_window()
            if not self.tray_hint_shown:
                self.tray_hint_shown = True
                self.tray.notify(APP_NAME, "Still running in the system tray. Right-click the icon to quit.")
            return
        if self.is_running and not messagebox.askyesno("Quit", "The organizer is running.\nStop it and quit?", parent=self.root):
            return
        self.quit_app()

    def quit_app(self):
        try:
            if self.observer:
                self.observer.stop()
        except Exception:
            pass
        self.tray.stop()
        _executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    # ---------- styles ----------
    def setup_styles(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure("Treeview", background=CARD2, fieldbackground=CARD2, foreground=TEXT,
                    rowheight=28, borderwidth=0, font=FONT)
        s.configure("Treeview.Heading", background=BTN, foreground=TEXT, relief="flat", font=FONT_B)
        s.map("Treeview", background=[("selected", "#0a7f6b")], foreground=[("selected", "#ffffff")])
        s.map("Treeview.Heading", background=[("active", BTN_HOVER)])
        s.configure("Accent.Horizontal.TProgressbar", troughcolor=CARD2, background=ACCENT,
                    bordercolor=CARD, lightcolor=ACCENT, darkcolor=ACCENT, thickness=10)
        s.configure("Dark.TCheckbutton", background=CARD, foreground=TEXT, font=FONT)
        s.map("Dark.TCheckbutton", background=[("active", CARD)], foreground=[("active", ACCENT)])
        s.configure("Vertical.TScrollbar", background=BTN, troughcolor=CARD2, bordercolor=CARD2,
                    lightcolor=BTN, darkcolor=BTN, arrowcolor=TEXT, relief="flat")
        s.map("Vertical.TScrollbar", background=[("active", BTN_HOVER)], lightcolor=[("active", BTN_HOVER)],
              darkcolor=[("active", BTN_HOVER)])
        s.configure("TNotebook", background=BG, borderwidth=0, bordercolor=BG, lightcolor=BG,
                    darkcolor=BG, tabmargins=(0, 0, 0, 0))
        s.configure("TNotebook.Tab", background=CARD, foreground=MUTED, padding=(18, 9), font=FONT_B,
                    borderwidth=0, bordercolor=BG, lightcolor=CARD, darkcolor=CARD)
        s.map("TNotebook.Tab", background=[("selected", CARD2)], foreground=[("selected", ACCENT)],
              lightcolor=[("selected", CARD2)], darkcolor=[("selected", CARD2)])
        s.configure("Dark.TCombobox", fieldbackground=CARD2, background=BTN, foreground=TEXT,
                    arrowcolor=TEXT, bordercolor=CARD2, lightcolor=CARD2, darkcolor=CARD2, padding=5)
        s.map("Dark.TCombobox", fieldbackground=[("readonly", CARD2)], foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", CARD2)], selectforeground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", CARD2)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#0a7f6b")
        self.root.option_add("*TCombobox*Listbox.font", FONT)

    # ---------- layout ----------
    def build_ui(self):
        outer = tk.Frame(self.root, bg=BG, padx=20, pady=14)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x", pady=(0, 10))
        titles = tk.Frame(header, bg=BG)
        titles.pack(side="left")
        tk.Label(titles, text="🛡️  LellaVerse Smart Organizer", font=("Segoe UI", 17, "bold"), bg=BG, fg=ACCENT).pack(anchor="w")
        tk.Label(titles, text="Keeps your folders clean automatically - with virus scan, duplicate check and undo",
                 font=FONT_S, bg=BG, fg=MUTED).pack(anchor="w")
        self.status_pill = tk.Label(header, text="●  STOPPED", font=FONT_B, bg=CARD, fg=RED, padx=14, pady=6)
        self.status_pill.pack(side="right")

        ctrl = tk.Frame(outer, bg=BG)
        ctrl.pack(fill="x", pady=(0, 4))
        self.start_btn = FlatButton(ctrl, "▶  START", self.start_organizer, color="#00b37e", hover="#00cc8f", fg="#ffffff")
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = FlatButton(ctrl, "⏹  STOP", self.stop_organizer, color="#d94452", hover="#f0566a", fg="#ffffff")
        self.stop_btn.pack(side="left", padx=(0, 8))
        self.stop_btn.set_enabled(False)
        self.now_btn = FlatButton(ctrl, "⚡  Sort now", self.sort_now)
        self.now_btn.pack(side="left", padx=(0, 8))
        self.undo_btn = FlatButton(ctrl, "↩  Undo last", self.undo_last)
        self.undo_btn.pack(side="left")
        self.stats_label = tk.Label(ctrl, text="", font=FONT_B, bg=BG, fg=MUTED)
        self.stats_label.pack(side="right")
        self.update_stats()

        prog = tk.Frame(outer, bg=BG)
        prog.pack(fill="x", pady=(2, 8))
        self.progress = ttk.Progressbar(prog, style="Accent.Horizontal.TProgressbar", mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_label = tk.Label(prog, text="", font=FONT_S, bg=BG, fg=MUTED, width=18, anchor="e")
        self.progress_label.pack(side="left", padx=(8, 0))

        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True)
        self.tab_rules = tk.Frame(self.nb, bg=BG, pady=8)
        self.tab_log = tk.Frame(self.nb, bg=BG, pady=8)
        self.tab_hist = tk.Frame(self.nb, bg=BG, pady=8)
        self.tab_set = tk.Frame(self.nb, bg=BG, pady=8)
        self.tab_about = tk.Frame(self.nb, bg=BG, pady=8)
        self.nb.add(self.tab_rules, text="📁  Folders & rules")
        self.nb.add(self.tab_log, text="📜  Activity")
        self.nb.add(self.tab_hist, text="↩  History / Undo")
        self.nb.add(self.tab_set, text="⚙  Settings")
        self.nb.add(self.tab_about, text="ℹ  About")
        self.build_rules_tab()
        self.build_log_tab()
        self.build_history_tab()
        self.build_settings_tab()
        self.build_about_tab()

        self.last_label = tk.Label(outer, text="", font=FONT_S, bg=BG, fg=MUTED, anchor="w")
        self.last_label.pack(fill="x", pady=(6, 0))

    def build_rules_tab(self):
        c1 = card(self.tab_rules, "Folders to organize", "Each folder has its own rules")
        c1.pack(fill="x")
        row = tk.Frame(c1, bg=CARD)
        row.pack(fill="x", pady=(8, 0))
        wrap = tk.Frame(row, bg=CARD)
        wrap.pack(side="left", fill="x", expand=True)
        self.folder_tree = ttk.Treeview(wrap, columns=("path", "dates", "cats"), show="headings", height=4, selectmode="browse")
        self.folder_tree.heading("path", text="Folder")
        self.folder_tree.heading("dates", text="Date folders")
        self.folder_tree.heading("cats", text="Categories")
        self.folder_tree.column("path", width=420, anchor="w")
        self.folder_tree.column("dates", width=90, anchor="center")
        self.folder_tree.column("cats", width=80, anchor="center")
        self.folder_tree.pack(side="left", fill="x", expand=True)
        self.folder_tree.bind("<<TreeviewSelect>>", lambda e: self.refresh_rules())
        side = tk.Frame(row, bg=CARD)
        side.pack(side="left", padx=(12, 0), anchor="n")
        FlatButton(side, "＋  Add folder", self.add_folder, color=ACCENT, hover="#00b894", fg="#06241d").pack(fill="x", pady=1)
        self.remove_btn = FlatButton(side, "Remove", self.remove_folder)
        self.remove_btn.pack(fill="x", pady=1)
        FlatButton(side, "Open", self.open_folder).pack(fill="x", pady=1)

        c2 = card(self.tab_rules, "Rules for the selected folder")
        c2.pack(fill="both", expand=True, pady=(8, 0))
        self.rules_title = tk.Label(c2, text="", font=FONT_MONO, bg=CARD, fg=ACCENT, anchor="w")
        self.rules_title.pack(fill="x", pady=(4, 6))
        body = tk.Frame(c2, bg=CARD)
        body.pack(fill="both", expand=True)
        tw = tk.Frame(body, bg=CARD)
        tw.pack(side="left", fill="both", expand=True)
        self.cat_tree = ttk.Treeview(tw, columns=("folder", "ext"), show="headings", height=6, selectmode="browse")
        self.cat_tree.heading("folder", text="Folder")
        self.cat_tree.heading("ext", text="Extensions")
        self.cat_tree.column("folder", width=150, anchor="w")
        self.cat_tree.column("ext", width=430, anchor="w")
        sb = ttk.Scrollbar(tw, orient="vertical", command=self.cat_tree.yview)
        self.cat_tree.configure(yscrollcommand=sb.set)
        self.cat_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.cat_tree.bind("<Double-1>", lambda e: self.edit_category())
        cside = tk.Frame(body, bg=CARD)
        cside.pack(side="left", padx=(12, 0), anchor="n")
        FlatButton(cside, "＋  Add folder type", self.add_category, color=ACCENT, hover="#00b894", fg="#06241d").pack(fill="x", pady=2)
        FlatButton(cside, "✎  Edit", self.edit_category).pack(fill="x", pady=2)
        FlatButton(cside, "🗑  Delete", self.delete_category).pack(fill="x", pady=2)
        FlatButton(cside, "↺  Reset", self.reset_categories).pack(fill="x", pady=2)

        drow = tk.Frame(c2, bg=CARD)
        drow.pack(fill="x", pady=(10, 0))
        tk.Label(drow, text="Date folders inside each category:", font=FONT_B, bg=CARD, fg=TEXT).pack(side="left")
        self.date_combo = ttk.Combobox(drow, values=DATE_LABELS, state="readonly", style="Dark.TCombobox", width=50)
        self.date_combo.pack(side="left", padx=10)
        self.date_combo.bind("<<ComboboxSelected>>", self.on_date_mode)
        tk.Label(c2, text="Automatic folders:  Folders = sub-folders  ·  Others = unknown types  ·  Duplicates = identical files  ·  Infected_Malware = virus found",
                 font=FONT_S, bg=CARD, fg=MUTED).pack(anchor="w", pady=(8, 0))

    def build_log_tab(self):
        c = card(self.tab_log, "Live activity")
        c.pack(fill="both", expand=True)
        FlatButton(c.winfo_children()[0], "Clear", self.clear_log).pack(side="right")
        wrap = tk.Frame(c, bg=CARD)
        wrap.pack(fill="both", expand=True, pady=(8, 0))
        self.log_box = tk.Text(wrap, bg="#0b0e13", fg=MUTED, font=FONT_MONO, bd=0, padx=10, pady=8, state="disabled", wrap="word")
        lsb = ttk.Scrollbar(wrap, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=lsb.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        lsb.pack(side="left", fill="y")
        for tag, color in (("info", MUTED), ("success", GREEN), ("warning", ORANGE), ("error", RED), ("time", "#5b6475")):
            self.log_box.tag_config(tag, foreground=color)

    def build_history_tab(self):
        c = card(self.tab_hist, "Moves the organizer made", "Select one or more and press Undo")
        c.pack(fill="both", expand=True)
        wrap = tk.Frame(c, bg=CARD)
        wrap.pack(fill="both", expand=True, pady=(8, 0))
        self.hist_tree = ttk.Treeview(wrap, columns=("time", "file", "to", "kind"), show="headings", selectmode="extended")
        for col, text, w in (("time", "When", 110), ("file", "File", 230), ("to", "Moved to", 340), ("kind", "Type", 80)):
            self.hist_tree.heading(col, text=text)
            self.hist_tree.column(col, width=w, anchor="w")
        hsb = ttk.Scrollbar(wrap, orient="vertical", command=self.hist_tree.yview)
        self.hist_tree.configure(yscrollcommand=hsb.set)
        self.hist_tree.pack(side="left", fill="both", expand=True)
        hsb.pack(side="left", fill="y")
        row = tk.Frame(c, bg=CARD)
        row.pack(fill="x", pady=(10, 0))
        FlatButton(row, "↩  Undo selected", self.undo_selected, color=ACCENT, hover="#00b894", fg="#06241d").pack(side="left")
        FlatButton(row, "Allow re-sorting of undone files", self.forget_undone).pack(side="left", padx=8)
        tk.Label(row, text="Undone files stay where you put them until you allow re-sorting.",
                 font=FONT_S, bg=CARD, fg=MUTED).pack(side="left", padx=6)

    def build_settings_tab(self):
        c = card(self.tab_set, "Settings")
        c.pack(fill="x")
        self.setting_vars = {}

        def group(title):
            tk.Label(c, text=title, font=FONT_B, bg=CARD, fg=ACCENT).pack(anchor="w", pady=(12, 2))

        def check(key, text):
            var = tk.BooleanVar(value=cfg[key])
            self.setting_vars[key] = var
            ttk.Checkbutton(c, text=text, variable=var, style="Dark.TCheckbutton",
                            command=lambda k=key: self.on_setting(k)).pack(anchor="w", pady=1)

        group("Organizing")
        check("sort_existing", "Sort the files & folders already inside when I press START")
        check("check_duplicates", 'Find duplicate files (same content) and move them to "Duplicates"')
        group("Virus scan")
        check("scan_virus", "Scan new downloads with Windows Defender")
        check("scan_existing", "Also scan the old files during the first sort (slower)")
        group("Background")
        check("auto_start_watching", "Start watching automatically when the app opens")
        check("close_to_tray", "Closing the window keeps the app running in the system tray")
        check("start_with_windows", "Start with Windows  (hidden in the tray, starts 20 seconds after login, low priority)")
        tray_text = ("Tray icon: ready." if self.tray_ok else "Tray icon: not installed.  Run:  pip install pystray pillow")
        tk.Label(c, text=tray_text, font=FONT_S, bg=CARD, fg=GREEN if self.tray_ok else ORANGE).pack(anchor="w", pady=(10, 0))
        tk.Label(c, text="Same name but a different file?  It is saved as  name_20261017_a3f9c1.ext  (date + content fingerprint), so nothing is overwritten.",
                 font=FONT_S, bg=CARD, fg=MUTED, wraplength=820, justify="left").pack(anchor="w", pady=(6, 0))

    def build_about_tab(self):
        c = card(self.tab_about, f"{APP_NAME}", f"Version {APP_VERSION}")
        c.pack(fill="both", expand=True)

        def heading(text):
            tk.Label(c, text=text, font=FONT_B, bg=CARD, fg=ACCENT).pack(anchor="w", pady=(12, 2))

        def line(text, color=TEXT):
            tk.Label(c, text=text, font=FONT, bg=CARD, fg=color, justify="left", wraplength=840, anchor="w").pack(anchor="w")

        line("Keeps your Downloads (and any other folder) clean. New files are sorted automatically into folders "
             "like Images, Documents and Media - checked for viruses first, and never overwritten.")
        heading("What it does")
        line("•  Sorts files by type, and optionally by week or month (e.g.  Images\\2026-10-Oct-Week3)")
        line("•  Skips downloads that are not finished yet")
        line("•  Scans new files with Windows Defender and quarantines infected ones")
        line("•  Moves identical files to a Duplicates folder; same-name files get a unique name")
        line("•  Undo any move from the History tab")
        line("•  Runs quietly in the system tray and can start with Windows")
        heading("Your privacy")
        line("Everything happens on your PC. Nothing is uploaded and there is no internet connection or tracking. "
             "The app only moves files inside the folders YOU add. Virus scanning uses the Windows Defender "
             "already on your PC - this app is not an antivirus by itself.")
        heading("Where your settings are saved")
        line(CONFIG_FILE, MUTED)
        line(HISTORY_FILE, MUTED)
        heading("Links")
        row = tk.Frame(c, bg=CARD)
        row.pack(anchor="w", pady=(2, 0))
        FlatButton(row, "🌐  Project page (GitHub)", lambda: webbrowser.open(GITHUB_URL)).pack(side="left", padx=(0, 8))
        FlatButton(row, "🐞  Report a problem", lambda: webbrowser.open(GITHUB_URL + "/issues")).pack(side="left", padx=(0, 8))
        FlatButton(row, "📂  Open settings folder", self.open_settings_folder).pack(side="left")
        tk.Label(c, text=f"Made by {AUTHOR}  ·  MIT License  ·  Built with Python, watchdog, pystray and Pillow",
                 font=FONT_S, bg=CARD, fg=MUTED).pack(side="bottom", anchor="w", pady=(12, 0))

    def open_settings_folder(self):
        try:
            os.startfile(HOME)  # Windows
        except AttributeError:
            messagebox.showinfo("Settings folder", HOME, parent=self.root)
        except Exception as e:
            messagebox.showerror("Cannot open folder", str(e), parent=self.root)

    # ---------- logging / stats ----------
    def log(self, text, level="info"):
        icon = {"info": "•", "success": "✔", "warning": "⚠", "error": "✖"}.get(level, "•")
        self.log_box.config(state="normal")
        self.log_box.insert("end", datetime.now().strftime("[%H:%M:%S] "), "time")
        self.log_box.insert("end", f"{icon} {text}\n", level)
        self.log_box.see("end")
        self.log_box.config(state="disabled")
        color = {"success": GREEN, "warning": ORANGE, "error": RED}.get(level, MUTED)
        self.last_label.config(text=f"{icon} {text}", fg=color)

    def clear_log(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")

    def update_stats(self):
        s = self.stats
        self.stats_label.config(
            text=f"Moved {s['moved']}  ·  Duplicates {s['dupes']}  ·  Infected {s['infected']}  ·  Errors {s['errors']}",
            fg=RED if s["errors"] or s["infected"] else MUTED)

    def poll_queue(self):
        try:
            while True:
                item = ui_queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.log(item[1], item[2])
                elif kind == "stat":
                    self.stats[item[1]] += 1
                    self.update_stats()
                elif kind == "progress":
                    done, total = item[1], item[2]
                    self.progress.config(maximum=max(total, 1), value=done)
                    self.progress_label.config(text=f"Sorting {done} / {total}" if total else "")
                    self.now_btn.set_enabled(False)
                elif kind == "done":
                    self.now_btn.set_enabled(True)
                    self.progress_label.config(text="Done ✔")
                elif kind == "history":
                    self.refresh_history()
                elif kind == "notify":
                    if not self.visible:
                        self.tray.notify(item[1], item[2])
                elif kind == "popup":
                    self.log(f"{item[1]}: {item[2]}", "error")
                    messagebox.showerror(item[1], item[2], parent=self.root)
                elif kind == "cmd":
                    self.handle_command(item[1])
        except queue.Empty:
            pass
        self.root.after(150 if self.visible else 500, self.poll_queue)

    def handle_command(self, cmd):
        if cmd == "show":
            self.show_window()
        elif cmd == "sort":
            self.sort_now(silent=True)
        elif cmd == "toggle":
            self.stop_organizer() if self.is_running else self.start_organizer(silent=True)
        elif cmd == "quit":
            self.quit_app()

    # ---------- settings ----------
    def save(self):
        ok, err = save_config()
        if not ok:
            self.log(f"Could not save settings: {err}", "error")
            messagebox.showerror("Cannot save settings", err, parent=self.root)
        return ok

    def on_setting(self, key):
        var = self.setting_vars[key]
        value = var.get()
        if key == "start_with_windows":
            try:
                set_autostart(value)
            except Exception as e:
                var.set(not value)
                messagebox.showerror("Cannot change start-up setting", str(e), parent=self.root)
                self.log(f"Start-up setting failed: {e}", "error")
                return
            self.log("Will start with Windows." if value else "Will not start with Windows.", "info")
        cfg[key] = value
        self.save()

    # ---------- folders ----------
    def current_index(self):
        sel = self.folder_tree.selection()
        if sel:
            idx = int(sel[0])
            if 0 <= idx < len(cfg["folders"]):
                return idx
        return None

    def current_rule(self):
        idx = self.current_index()
        return cfg["folders"][idx] if idx is not None else None

    def refresh_folders(self, select=None):
        if select is None:
            select = self.current_index()
        self.folder_tree.delete(*self.folder_tree.get_children())
        for i, r in enumerate(cfg["folders"]):
            self.folder_tree.insert("", "end", iid=str(i),
                                    values=(r["path"], DATE_SHORT[r["date_mode"]], len(r["categories"])))
        if cfg["folders"]:
            select = min(select if select is not None else 0, len(cfg["folders"]) - 1)
            self.folder_tree.selection_set(str(select))
        self.refresh_rules()

    def refresh_rules(self):
        rule = self.current_rule()
        self.cat_tree.delete(*self.cat_tree.get_children())
        if rule is None:
            self.rules_title.config(text="No folder yet - press  ＋ Add folder  above.")
            self.date_combo.set("")
            return
        self.rules_title.config(text=rule["path"])
        for name, exts in rule["categories"].items():
            self.cat_tree.insert("", "end", iid=name, values=(name, ", ".join(exts)))
        self.date_combo.current(DATE_MODES.index(rule["date_mode"]))

    def on_date_mode(self, _event=None):
        rule = self.current_rule()
        if rule is None:
            return
        rule["date_mode"] = DATE_MODES[self.date_combo.current()]
        self.save()
        self.refresh_folders()
        self.log(f"Date folders for {rule['path']}: {DATE_SHORT[rule['date_mode']]}", "info")

    def add_folder(self):
        start = cfg["folders"][-1]["path"] if cfg["folders"] else HOME
        path = filedialog.askdirectory(initialdir=start, title="Choose a folder to organize", parent=self.root)
        if not path:
            return
        path = os.path.normpath(path)
        ok, err = check_watch_path(path)
        if not ok:
            messagebox.showerror("Cannot use this folder", err, parent=self.root)
            self.log(f"Folder rejected: {path}", "error")
            return
        conflict = folder_conflict(path)
        if conflict:
            messagebox.showerror("Cannot use this folder", conflict, parent=self.root)
            return
        dlg = NewFolderDialog(self.root, path, cfg["folders"])
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        cats, mode = dlg.result
        rule = new_folder_rule(path, cats, mode)
        cfg["folders"].append(rule)
        self.save()
        self.refresh_folders(select=len(cfg["folders"]) - 1)
        self.log(f"Added folder: {path}", "success")
        if self.is_running:
            self.attach_watch(rule)
            if cfg["sort_existing"]:
                self.log("Sorting existing files and folders...", "info")
                organize_existing()

    def remove_folder(self):
        idx = self.current_index()
        if idx is None:
            messagebox.showinfo("Remove folder", "Please click a folder in the list first.", parent=self.root)
            return
        rule = cfg["folders"][idx]
        if not messagebox.askyesno("Remove folder",
                                   f"Stop organizing this folder?\n\n{rule['path']}\n\nYour files stay where they are.",
                                   parent=self.root):
            return
        del cfg["folders"][idx]
        key = norm(rule["path"])
        if self.observer and key in self.watches:
            try:
                self.observer.unschedule(self.watches.pop(key))
            except Exception as e:
                self.log(f"Could not stop watching '{rule['path']}': {e}", "warning")
        self.save()
        self.refresh_folders(select=max(idx - 1, 0))
        self.log(f"Removed folder: {rule['path']}", "info")

    def open_folder(self):
        rule = self.current_rule()
        if rule is None:
            messagebox.showinfo("Open folder", "Add a folder first.", parent=self.root)
            return
        try:
            os.startfile(rule["path"])  # Windows
        except AttributeError:
            messagebox.showinfo("Folder", rule["path"], parent=self.root)
        except Exception as e:
            messagebox.showerror("Cannot open folder", str(e), parent=self.root)

    def attach_watch(self, rule):
        try:
            create_category_folders(rule)
            self.watches[norm(rule["path"])] = self.observer.schedule(OrganizerHandler(), rule["path"], recursive=False)
            self.log(f"Watching: {rule['path']}", "info")
        except Exception as e:
            self.log(f"Could not watch '{rule['path']}': {e}", "error")
            messagebox.showerror("Cannot watch folder", f"{rule['path']}\n\n{e}", parent=self.root)

    # ---------- categories (of the selected folder) ----------
    def need_rule(self):
        rule = self.current_rule()
        if rule is None:
            messagebox.showinfo("Folder rules", "Add a folder first, then select it in the list.", parent=self.root)
        return rule

    def selected_category(self):
        sel = self.cat_tree.selection()
        return sel[0] if sel else None

    def make_folder_now(self, rule, name):
        if check_watch_path(rule["path"])[0]:
            try:
                os.makedirs(os.path.join(rule["path"], name), exist_ok=True)
            except Exception as e:
                self.log(f"Could not create '{name}': {e}", "error")
                messagebox.showerror("Cannot create folder", f"{name}\n\n{e}", parent=self.root)

    def add_category(self):
        rule = self.need_rule()
        if rule is None:
            return
        dlg = CategoryDialog(self.root, "Add a new folder type", rule["categories"])
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        name, exts = dlg.result
        rule["categories"][name] = exts
        if self.save():
            self.make_folder_now(rule, name)
            self.refresh_folders()
            self.log(f"Added '{name}' ({', '.join(exts)}) to {rule['path']}", "success")

    def edit_category(self):
        rule = self.need_rule()
        if rule is None:
            return
        old = self.selected_category()
        if not old:
            messagebox.showinfo("Edit", "Please click a folder type in the list first.", parent=self.root)
            return
        dlg = CategoryDialog(self.root, "Edit folder type", rule["categories"], old, ", ".join(rule["categories"][old]), editing=old)
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        name, exts = dlg.result
        rule["categories"] = {(name if k == old else k): (exts if k == old else v) for k, v in rule["categories"].items()}
        if name != old:
            src, dst = os.path.join(rule["path"], old), os.path.join(rule["path"], name)
            if os.path.isdir(src) and not os.path.exists(dst):
                try:
                    os.rename(src, dst)
                    self.log(f"Renamed folder '{old}' → '{name}'", "info")
                except Exception as e:
                    self.log(f"Could not rename folder on disk: {e}", "error")
                    messagebox.showerror("Cannot rename folder", f"{e}\n\nThe list was updated but the folder on disk keeps the old name.", parent=self.root)
        if self.save():
            self.make_folder_now(rule, name)
            self.refresh_folders()
            self.log(f"Updated '{name}' ({', '.join(exts)})", "success")

    def delete_category(self):
        rule = self.need_rule()
        if rule is None:
            return
        name = self.selected_category()
        if not name:
            messagebox.showinfo("Delete", "Please click a folder type in the list first.", parent=self.root)
            return
        if messagebox.askyesno("Delete folder type",
                               f'Remove "{name}" from this folder\'s rules?\n\nThe folder and files on your disk are NOT deleted.',
                               parent=self.root):
            del rule["categories"][name]
            if self.save():
                self.refresh_folders()
                self.log(f"Removed '{name}'", "info")

    def reset_categories(self):
        rule = self.need_rule()
        if rule is None:
            return
        if messagebox.askyesno("Reset", "Go back to the default folder types for this folder?", parent=self.root):
            rule["categories"] = copy.deepcopy(DEFAULT_CATEGORIES)
            if self.save():
                self.refresh_folders()
                self.log("Folder types reset to default.", "info")

    # ---------- history / undo ----------
    def refresh_history(self):
        self.hist_tree.delete(*self.hist_tree.get_children())
        with _hist_lock:
            entries = list(_history)
        for e in reversed(entries):
            try:
                rel = os.path.relpath(e["dst"], os.path.dirname(e["src"]))
            except ValueError:
                rel = e["dst"]
            when = e["time"][5:16].replace("T", "  ")
            self.hist_tree.insert("", "end", iid=str(e["id"]),
                                  values=(when, os.path.basename(e["src"]), rel, KIND_LABELS.get(e["kind"], e["kind"])))
        self.undo_btn.set_enabled(bool(entries))

    def do_undo(self, ids):
        with _hist_lock:
            kinds = {e["id"]: e["kind"] for e in _history}
        if any(kinds.get(i) == "virus" for i in ids):
            if not messagebox.askyesno("Restore infected file?",
                                       "At least one of these files was flagged as a VIRUS.\n\nRestore it anyway?",
                                       icon="warning", parent=self.root):
                return
        done = 0
        for i in ids:
            ok, msg = undo_move(i)
            if ok:
                done += 1
                self.log(msg, "success")
            else:
                self.log(msg.replace("\n\n", " ").replace("\n", " "), "error")
                messagebox.showerror("Cannot undo", msg, parent=self.root)
        if done:
            self.log(f"Undo finished: {done} file(s) restored.", "success")
        self.refresh_history()

    def undo_selected(self):
        ids = [int(i) for i in self.hist_tree.selection()]
        if not ids:
            messagebox.showinfo("Undo", "Select one or more moves in the list first.", parent=self.root)
            return
        self.do_undo(ids)

    def undo_last(self):
        with _hist_lock:
            last = _history[-1]["id"] if _history else None
        if last is None:
            messagebox.showinfo("Undo", "There is nothing to undo yet.", parent=self.root)
            return
        self.do_undo([last])

    def forget_undone(self):
        forget_pins()
        self.log("Undone files can be sorted again (press Sort now).", "info")

    # ---------- run ----------
    def problem(self, title, msg, silent):
        self.log(msg.replace("\n\n", " ").replace("\n", " "), "error")
        if silent:
            self.tray.notify(title, msg[:200])
        else:
            messagebox.showerror(title, msg, parent=self.root)

    def prepare_folders(self, silent=False):
        if not cfg["folders"]:
            self.problem("Cannot start", "Please add at least one folder first (＋ Add folder).", silent)
            return False
        bad = [check_watch_path(r["path"])[1] for r in cfg["folders"] if not check_watch_path(r["path"])[0]]
        if bad:
            self.problem("Cannot start", "\n\n".join(bad) + "\n\nFix the problem or Remove that folder from the list.", silent)
            return False
        total_new = 0
        for r in cfg["folders"]:
            try:
                total_new += create_category_folders(r)
            except Exception as e:
                self.problem("Cannot create folders", f"{r['path']}\n\n{e}", silent)
                return False
        self.log(f"Folders ready in {len(cfg['folders'])} folder(s) ({total_new} new).", "info")
        return True

    def auto_start(self):
        if not self.is_running:
            self.start_organizer(silent=True)

    def start_organizer(self, silent=False):
        if self.is_running:
            return
        if not self.prepare_folders(silent):
            return
        if cfg["scan_virus"] and not DEFENDER_PATH:
            self.log("Windows Defender not found - scan will be skipped.", "warning")
        try:
            self.observer = Observer()
            self.watches = {}
            for r in cfg["folders"]:
                self.watches[norm(r["path"])] = self.observer.schedule(OrganizerHandler(), r["path"], recursive=False)
            self.observer.start()
        except Exception as e:
            self.observer, self.watches = None, {}
            self.problem("Cannot start watching", str(e), silent)
            return
        self.is_running = True
        self.set_running_ui(True)
        for r in cfg["folders"]:
            self.log(f"Watching: {r['path']}", "info")
        if cfg["sort_existing"]:
            self.log("Sorting existing files and folders...", "info")
            organize_existing()

    def stop_organizer(self):
        if self.is_running and self.observer:
            self.observer.stop()
            self.observer.join(timeout=3)
            self.observer = None
            self.watches = {}
        self.is_running = False
        self.set_running_ui(False)
        self.log("Stopped watching.", "info")

    def sort_now(self, silent=False):
        if self.prepare_folders(silent):
            self.log("Sorting existing files and folders...", "info")
            organize_existing()

    def set_running_ui(self, running):
        self.start_btn.set_enabled(not running)
        self.stop_btn.set_enabled(running)
        self.status_pill.config(text="●  RUNNING" if running else "●  STOPPED", fg=GREEN if running else RED)
        n = len(cfg["folders"])
        self.tray.set_title(f"{APP_NAME} - " + (f"running ({n} folder{'s' if n != 1 else ''})" if running else "stopped"))


def main():
    minimized = "--minimized" in sys.argv
    root = tk.Tk()
    root.withdraw()
    if not acquire_single_instance():
        messagebox.showinfo(APP_NAME, "The organizer is already running.\nLook for its icon in the system tray (near the clock).")
        return
    lower_priority()
    app = OrganizerApp(root, minimized)
    if not minimized:
        app.show_window()
    root.mainloop()


if __name__ == "__main__":
    main()
