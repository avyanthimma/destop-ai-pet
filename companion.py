r"""
companion.py - local services behind Opus's companion features.

Nothing here uses Qt, and everything is stored on this machine under %APPDATA%\Opus.
The only network access is the optional calendar URL fetch, and it asks the offline
switch (NetworkPolicy) first, like every other request in Opus.

    Config                 small JSON settings file
    NetworkPolicy          the offline kill switch; every outgoing request checks it first
    SystemStats            CPU, memory, battery, disk, idle time and the foreground app (Win32 via ctypes)
    HabitTracker           logs app use and breaks, learns your usual focus stretch, suggests breaks
    PetNeeds               energy, fun and fullness, driven by real CPU use, idle time and activity
    FolderWatcher          polls a folder for saved or new files (code review, downloads)
    EventInbox             events dropped by opus_run.py when a command finishes
    ClipboardStore         local clipboard history with search and topics
    KnowledgeBase          BM25 search over your notes, PDFs and old projects
    Personality            pet personalities stored as small text files
    next_calendar_event    the next event from an .ics file or URL
    extract_python / run_script   mini-scripts written by the model, run only after you approve
"""
from __future__ import annotations

import ast
import ctypes
import difflib
import json
import logging
import math
import operator
import os
import queue
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("stacky.companion")

_WINDOWS = sys.platform == "win32"


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def app_dir() -> Path:
    """%APPDATA%\\Opus, created on first use."""
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / "Opus"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Config:
    """A small JSON settings file. Writes go to a temporary file first, so a crash cannot corrupt it."""

    def __init__(self, path: Path | None = None):
        self._path = path or app_dir() / "config.json"
        self._lock = threading.Lock()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._data = {}

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = value
            tmp = self._path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
                os.replace(tmp, self._path)
            except OSError as exc:
                log.warning("Could not save config: %s", exc)


# --------------------------------------------------------------------------- #
# Offline kill switch
# --------------------------------------------------------------------------- #
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class OfflineError(Exception):
    """Raised when offline mode blocks a request to another machine."""


class NetworkPolicy:
    """
    The offline kill switch. While offline, Opus only talks to this machine (localhost),
    which keeps the local Ollama server working and blocks everything else.
    """

    def __init__(self):
        self._offline = threading.Event()

    @property
    def offline(self) -> bool:
        return self._offline.is_set()

    def set_offline(self, offline: bool) -> None:
        if offline:
            self._offline.set()
        else:
            self._offline.clear()

    def allows(self, url: str) -> bool:
        if not self.offline:
            return True
        return (urlparse(url).hostname or "").lower() in LOCAL_HOSTS

    def check(self, url: str) -> None:
        if not self.allows(url):
            raise OfflineError(f"Offline mode is on, so Opus did not contact {urlparse(url).hostname}.")


network = NetworkPolicy()


# --------------------------------------------------------------------------- #
# Win32 bindings
# --------------------------------------------------------------------------- #
if _WINDOWS:
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    class _FILETIME(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    class _SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
            ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
            ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD),
        ]

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    _kernel32.GetSystemTimes.argtypes = [ctypes.POINTER(_FILETIME)] * 3
    _kernel32.GetSystemTimes.restype = wintypes.BOOL
    _kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
    _kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    _kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(_SYSTEM_POWER_STATUS)]
    _kernel32.GetSystemPowerStatus.restype = wintypes.BOOL
    _kernel32.GetTickCount.restype = wintypes.DWORD
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
    _user32.GetLastInputInfo.restype = wintypes.BOOL
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    _user32.keybd_event.restype = None


VK_LBUTTON = 0x01
VK_ESCAPE = 0x1B


def key_down(vk: int) -> bool:
    """True while the given virtual key or mouse button is held down, anywhere on the system."""
    if not _WINDOWS:
        return False
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def start_voice_typing() -> bool:
    """
    Open Windows voice typing (the same as pressing Win+H) for the focused text box.

    This gives Opus speech input with no extra install: Windows does the speech-to-text
    and types the words into the prompt box. Returns False where it is not available.
    """
    if not _WINDOWS:
        return False
    vk_lwin, vk_h, keyup = 0x5B, 0x48, 0x0002
    _user32.keybd_event(vk_lwin, 0, 0, 0)
    _user32.keybd_event(vk_h, 0, 0, 0)
    _user32.keybd_event(vk_h, 0, keyup, 0)
    _user32.keybd_event(vk_lwin, 0, keyup, 0)
    return True


def play_sound(kind: str) -> None:
    """Short Windows system sound: 'success', 'error' or 'notify'."""
    if not _WINDOWS:
        return
    import winsound
    flags = {"success": winsound.MB_ICONASTERISK, "error": winsound.MB_ICONHAND}.get(kind, winsound.MB_OK)
    try:
        winsound.MessageBeep(flags)
    except RuntimeError:
        pass


# --------------------------------------------------------------------------- #
# System stats
# --------------------------------------------------------------------------- #
FRIENDLY_APPS = {
    "code": "VS Code", "cursor": "Cursor", "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox",
    "explorer": "File Explorer", "winword": "Word", "excel": "Excel", "powerpnt": "PowerPoint",
    "outlook": "Outlook", "olk": "Outlook", "ms-teams": "Teams", "teams": "Teams", "slack": "Slack",
    "discord": "Discord", "spotify": "Spotify", "notepad": "Notepad", "notepad++": "Notepad++",
    "windowsterminal": "Terminal", "pycharm64": "PyCharm", "idea64": "IntelliJ IDEA",
    "devenv": "Visual Studio", "obsidian": "Obsidian", "notion": "Notion", "zoom": "Zoom",
    "claude": "Claude", "arduino ide": "Arduino IDE", "thonny": "Thonny",
}


def friendly_app_name(exe_stem: str) -> str:
    return FRIENDLY_APPS.get(exe_stem.lower(), exe_stem)


class SystemStats:
    """
    Live system readings through Win32 calls; values are None where unknown.

    Call sample() on a timer from one place. Everyone else reads latest(), so the CPU
    measurement (which compares against the previous sample) is not disturbed.
    """

    def __init__(self):
        self._last_cpu: tuple[int, int] | None = None
        self._lock = threading.Lock()
        self._latest: dict = {}

    @staticmethod
    def _ft(ft) -> int:
        return (ft.high << 32) | ft.low

    def _cpu_percent(self) -> float | None:
        if not _WINDOWS:
            return None
        idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
        if not _kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        idle_t = self._ft(idle)
        total = self._ft(kernel) + self._ft(user)  # kernel time includes idle time
        previous, self._last_cpu = self._last_cpu, (idle_t, total)
        if previous is None or total <= previous[1]:
            return None
        busy = 1.0 - (idle_t - previous[0]) / (total - previous[1])
        return max(0.0, min(100.0, busy * 100.0))

    @staticmethod
    def _memory_percent() -> float | None:
        if not _WINDOWS:
            return None
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return float(status.dwMemoryLoad)

    @staticmethod
    def _battery() -> tuple[int | None, bool | None]:
        """(percent, plugged in), or (None, None) on a desktop without a battery."""
        if not _WINDOWS:
            return None, None
        status = _SYSTEM_POWER_STATUS()
        if not _kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return None, None
        if status.BatteryFlag == 128 or status.BatteryLifePercent == 255:
            return None, None
        return int(status.BatteryLifePercent), {0: False, 1: True}.get(status.ACLineStatus)

    @staticmethod
    def _disk_percent() -> float | None:
        try:
            usage = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
        except OSError:
            return None
        return 100.0 * usage.used / usage.total

    @staticmethod
    def idle_seconds() -> float:
        """Seconds since the last keyboard or mouse input anywhere on the system."""
        if not _WINDOWS:
            return 0.0
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not _user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return ((_kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0

    @staticmethod
    def foreground_app() -> tuple[str, str, int]:
        """(friendly app name, window title, process id) of the window in front, or ("", "", 0)."""
        if not _WINDOWS:
            return "", "", 0
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return "", "", 0
        length = _user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, title, length + 1)
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = ""
        handle = _kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            try:
                size = wintypes.DWORD(1024)
                buf = ctypes.create_unicode_buffer(1024)
                if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    exe = Path(buf.value).stem
            finally:
                _kernel32.CloseHandle(handle)
        return friendly_app_name(exe), title.value, pid.value

    def sample(self) -> dict:
        battery, plugged = self._battery()
        reading = {
            "cpu": self._cpu_percent(),
            "memory": self._memory_percent(),
            "battery": battery,
            "plugged": plugged,
            "disk": self._disk_percent(),
            "idle": self.idle_seconds(),
        }
        with self._lock:
            if reading["cpu"] is None and "cpu" in self._latest:
                reading["cpu"] = self._latest["cpu"]  # keep the last good CPU value
            self._latest = reading
        return dict(reading)

    def latest(self) -> dict:
        with self._lock:
            return dict(self._latest)

    def describe(self) -> str:
        """Plain-text summary for the model's system_stats tool."""
        r = self.latest()

        def pct(value):
            return "unknown" if value is None else f"{value:.0f}%"

        if r.get("battery") is None:
            battery = "no battery (desktop)"
        else:
            battery = f"{r['battery']}% ({'charging' if r.get('plugged') else 'on battery'})"
        return (f"CPU {pct(r.get('cpu'))}, memory {pct(r.get('memory'))}, "
                f"disk {pct(r.get('disk'))} used, battery {battery}.")


def format_duration(minutes: float) -> str:
    """'30 seconds', '1 minute', '45 minutes', '1 hour', '2.5 hours'."""
    if minutes < 1:
        return f"{max(1, round(minutes * 60))} seconds"
    if minutes < 60:
        whole = round(minutes)
        return "1 minute" if whole == 1 else f"{whole} minutes"
    hours = round(minutes / 60 * 2) / 2
    return "1 hour" if hours == 1 else f"{hours:g} hours"


# --------------------------------------------------------------------------- #
# Habits
# --------------------------------------------------------------------------- #
@dataclass
class HabitUpdate:
    nudge: str | None = None             # something worth saying, or None
    break_minutes: float | None = None   # set once, when a break has just ended


class HabitTracker:
    """
    Logs which app you use and when you take breaks to %APPDATA%\\Opus\\habits.jsonl.

    It learns how long you usually work before a break (the median of your recent focus
    stretches) and suggests one when the current stretch runs well past that, or when
    you have been in one app for a long time. Nothing leaves the machine.
    """

    BREAK_SECONDS = 5 * 60         # this much idle time counts as a break
    ACTIVE_IDLE_SECONDS = 60       # under this much idle time you count as active
    APP_SWITCH_SECONDS = 120       # a new app has to hold the foreground this long to count as a switch
    APP_NUDGE_MINUTES = 120        # in one app for this long -> suggest a stretch
    STRETCH_BOUNDS = (45.0, 120.0)  # limits for the learned "time for a break" point, in minutes
    NUDGE_COOLDOWN = 45 * 60

    def __init__(self, folder: Path | None = None):
        folder = folder or app_dir()
        self._log_path = folder / "habits.jsonl"
        self._summary_path = folder / "habits_summary.json"
        self._lock = threading.Lock()
        try:
            self._summary = json.loads(self._summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._summary = {}
        self._summary.setdefault("stretches", [])
        self._summary.setdefault("days", {})
        now = time.time()
        self._app = ""
        self._app_since = now
        self._candidate = ""
        self._candidate_since = now
        self._stretch_start = now
        self._break_start: float | None = None
        self._last_tick: float | None = None
        self._last_nudge: dict[str, float] = {}

    # -- recording ----------------------------------------------------------- #
    def tick(self, app: str, idle_seconds: float, now: float | None = None) -> HabitUpdate:
        """Call every few seconds with the foreground app and the system idle time."""
        now = time.time() if now is None else now
        update = HabitUpdate()
        dt = 0.0 if self._last_tick is None else max(0.0, min(now - self._last_tick, 60.0))
        self._last_tick = now

        with self._lock:
            if idle_seconds >= self.BREAK_SECONDS:
                if self._break_start is None:
                    self._break_start = now - idle_seconds
                    self._finish_stretch(self._break_start)
                    self._log({"type": "break_start"}, self._break_start)
                return update
            if self._break_start is not None:
                minutes = (now - self._break_start) / 60
                self._log({"type": "break_end", "minutes": round(minutes, 1)}, now)
                update.break_minutes = minutes
                self._break_start = None
                self._stretch_start = now
                self._app_since = now

            self._track_app(app, now)
            if self._app and idle_seconds < self.ACTIVE_IDLE_SECONDS and dt:
                days = self._summary["days"]
                today = days.setdefault(date.today().isoformat(), {})
                today[self._app] = today.get(self._app, 0.0) + dt
                for old in sorted(days)[:-30]:  # keep a month of daily totals
                    del days[old]

            update.nudge = self._nudge(now)
        return update

    def _track_app(self, app: str, now: float) -> None:
        # Quick alt-tabs should not reset "you've been in VS Code for 2 hours"
        if not app or app == self._app:
            self._candidate = ""
            return
        if app != self._candidate:
            self._candidate, self._candidate_since = app, now
        if not self._app or now - self._candidate_since >= self.APP_SWITCH_SECONDS:
            self._app, self._app_since = app, (self._candidate_since if self._app else now)
            self._candidate = ""
            self._log({"type": "app", "app": app}, now)

    def _finish_stretch(self, end: float) -> None:
        minutes = (end - self._stretch_start) / 60
        if minutes >= 10:
            stretches = self._summary["stretches"]
            stretches.append(round(minutes, 1))
            del stretches[:-30]

    def _log(self, event: dict, when: float) -> None:
        event = {"t": datetime.fromtimestamp(when).isoformat(timespec="seconds"), **event}
        try:
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")
        except OSError as exc:
            log.debug("Habit log write failed: %s", exc)

    # -- learning and nudges -------------------------------------------------- #
    def break_threshold(self) -> float:
        """Minutes of focus after which a break is suggested, learned from your recent stretches."""
        stretches = self._summary["stretches"]
        typical = statistics.median(stretches) if len(stretches) >= 3 else 75.0
        low, high = self.STRETCH_BOUNDS
        return max(low, min(high, typical * 1.25))

    def _can_nudge(self, kind: str, now: float) -> bool:
        if now - self._last_nudge.get(kind, 0.0) < self.NUDGE_COOLDOWN:
            return False
        self._last_nudge[kind] = now
        return True

    def _nudge(self, now: float) -> str | None:
        app_minutes = (now - self._app_since) / 60
        if self._app and app_minutes >= self.APP_NUDGE_MINUTES and self._can_nudge("app", now):
            return f"You've been in {self._app} for {format_duration(app_minutes)}. Stretch?"
        stretch_minutes = (now - self._stretch_start) / 60
        if stretch_minutes >= self.break_threshold() and self._can_nudge("break", now):
            return f"You've been going for {format_duration(stretch_minutes)} without a break. Quick walk?"
        return None

    # -- reading ------------------------------------------------------------- #
    def focus_minutes(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        with self._lock:
            return 0.0 if self._break_start is not None else (now - self._stretch_start) / 60

    def today_summary(self, top: int = 4) -> str:
        with self._lock:
            today = dict(self._summary["days"].get(date.today().isoformat(), {}))
        if not today:
            return "No app time recorded today yet."
        ranked = sorted(today.items(), key=lambda item: item[1], reverse=True)[:top]
        parts = []
        for app, seconds in ranked:
            minutes = seconds / 60
            parts.append(f"{app} {int(minutes // 60)}h {int(minutes % 60)}m" if minutes >= 60
                         else f"{app} {max(1, round(minutes))}m")
        return "Today: " + ", ".join(parts)

    def save(self) -> None:
        with self._lock:
            data = json.dumps(self._summary)
        try:
            self._summary_path.write_text(data, encoding="utf-8")
        except OSError as exc:
            log.debug("Habit summary save failed: %s", exc)


# --------------------------------------------------------------------------- #
# Pet needs
# --------------------------------------------------------------------------- #
class PetNeeds:
    """
    Energy, fun and fullness, each from 0 to 100, driven by your real usage.

        energy    drains while the CPU is busy, and slowly while you work; recovers when things are calm
        fun       drains while you are away or ignore the pet; interacting with it brings it back
        fullness  drains as you put in active time; feeding the pet or taking a break fills it

    mood() turns these into one word that picks the pet's idle look, and mood_prompt()
    tells the model how the pet feels so it shows in what it says.
    """

    def __init__(self, state: dict | None = None):
        self.energy = 80.0
        self.fun = 80.0
        self.fullness = 80.0
        if state:
            self.energy = float(state.get("energy", self.energy))
            self.fun = float(state.get("fun", self.fun))
            self.fullness = float(state.get("fullness", self.fullness))
            # Time with the app closed: the pet rested, but also got a little hungry and bored
            away_hours = max(0.0, (time.time() - float(state.get("saved_at", time.time()))) / 3600)
            self.energy += 10 * min(away_hours, 8)
            self.fullness -= 3 * min(away_hours, 12)
            self.fun -= 2 * min(away_hours, 12)
        self._clamp()

    def _clamp(self) -> None:
        self.energy = max(0.0, min(100.0, self.energy))
        self.fun = max(0.0, min(100.0, self.fun))
        self.fullness = max(0.0, min(100.0, self.fullness))

    def tick(self, seconds: float, cpu: float | None, idle_seconds: float) -> None:
        minutes = seconds / 60
        active = idle_seconds < 60
        if cpu is not None and cpu >= 70:
            self.energy -= (1.0 + 2.0 * (cpu - 70) / 30) * minutes  # heavy CPU is tiring
        elif cpu is not None and cpu < 30 and not active:
            self.energy += 1.5 * minutes                           # a calm, quiet machine is restful
        else:
            self.energy -= 0.15 * minutes
        self.fun -= (1.0 if idle_seconds > 600 else 0.1) * minutes  # a long idle stretch is boring
        if active:
            self.fullness -= 0.25 * minutes
        self._clamp()

    def interact(self) -> None:
        self.fun += 12
        self._clamp()

    def feed(self) -> None:
        self.fullness += 40
        self.fun += 5
        self._clamp()

    def break_taken(self, minutes: float) -> None:
        self.fullness += min(30.0, minutes * 2)  # you ate, so it ate
        self.energy += min(20.0, minutes)
        self._clamp()

    def mood(self) -> str:
        if self.energy < 25:
            return "sleepy"
        if self.fullness < 25:
            return "hungry"
        if self.fun < 25:
            return "bored"
        return "content"

    def mood_prompt(self) -> str:
        return {
            "sleepy": "Right now you feel sleepy, because the computer has been working hard. "
                      "Let it show a little, perhaps a small yawn, but stay helpful.",
            "hungry": "Right now you are a bit hungry. You may hint once that a snack would be nice "
                      "(the user can feed you from your menu).",
            "bored": "Right now you are a little bored, so you are glad to have something to do.",
        }.get(self.mood(), "")

    def state(self) -> dict:
        return {"energy": round(self.energy, 1), "fun": round(self.fun, 1),
                "fullness": round(self.fullness, 1), "saved_at": time.time()}


# --------------------------------------------------------------------------- #
# Folder watching
# --------------------------------------------------------------------------- #
CODE_EXTENSIONS = {
    ".py", ".pyw", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".c", ".h", ".cpp", ".hpp",
    ".cc", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".lua", ".sh", ".ps1", ".sql", ".html",
    ".css", ".scss", ".vue", ".svelte", ".ino",
}
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env", ".tox",
             "dist", "build", ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".next", "target"}
PARTIAL_SUFFIXES = (".crdownload", ".part", ".partial", ".tmp", ".download", ".opdownload")


class FolderWatcher:
    """
    Polls a folder from a background thread and puts changed or new files on `events`.

    A file is reported once it has stopped changing for `settle` seconds, so editors that
    save in several steps and downloads that are still being written are not reported early.
    Polling (rather than OS notifications) behaves the same on every drive and editor.
    """

    def __init__(self, root: str | Path, extensions: set[str] | None = None, recursive: bool = True,
                 interval: float = 2.0, settle: float = 1.0, max_files: int = 5000, new_only: bool = False):
        self.root = Path(root)
        self.events: queue.Queue[Path] = queue.Queue()
        self._extensions = extensions
        self._recursive = recursive
        self._interval = interval
        self._settle = settle
        self._max_files = max_files
        self._new_only = new_only  # report only files that did not exist before (downloads)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"watch-{self.root.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _wanted(self, name: str) -> bool:
        lower = name.lower()
        if lower.startswith(("~$", ".~")) or lower.endswith(PARTIAL_SUFFIXES):
            return False
        return self._extensions is None or Path(lower).suffix in self._extensions

    def _scan(self) -> dict[Path, tuple[float, int]]:
        found: dict[Path, tuple[float, int]] = {}
        try:
            for folder, dirs, files in os.walk(self.root):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")] if self._recursive else []
                for name in files:
                    if not self._wanted(name):
                        continue
                    path = Path(folder) / name
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    found[path] = (st.st_mtime, st.st_size)
                    if len(found) >= self._max_files:
                        return found
        except OSError as exc:
            log.debug("Scan of %s failed: %s", self.root, exc)
        return found

    def _run(self) -> None:
        known = self._scan()  # baseline: files that already exist are not reported
        pending: dict[Path, tuple[tuple[float, int], float]] = {}
        while not self._stop.wait(self._interval):
            now = time.time()
            current = self._scan()
            for path, stamp in current.items():
                if known.get(path) == stamp:
                    continue
                if self._new_only and path in known:
                    known[path] = stamp  # an existing file changed; not a new arrival
                    continue
                seen = pending.get(path)
                if seen is None or seen[0] != stamp:
                    pending[path] = (stamp, now)          # still changing; wait for it to settle
                elif now - seen[1] >= self._settle:
                    known[path] = stamp
                    del pending[path]
                    self.events.put(path)
            for path in [p for p in known if p not in current]:
                del known[path]

    def drain(self) -> list[Path]:
        """All events so far, without duplicates, in arrival order."""
        paths: list[Path] = []
        while True:
            try:
                path = self.events.get_nowait()
            except queue.Empty:
                return paths
            if path not in paths:
                paths.append(path)


# --------------------------------------------------------------------------- #
# Code review on save
# --------------------------------------------------------------------------- #
REVIEW_SYSTEM_PROMPT = (
    "You review a file the user just saved. Look only for a real problem in the changed code: "
    "a bug, a crash, an infinite loop, a shadowed built-in, an off-by-one, a resource leak or a security hole. "
    "If you find one, reply with ONE short line of at most 15 words, like \"this loop never exits\" "
    "or \"you're shadowing list\". No preamble, no code. "
    "If nothing is clearly wrong, reply with exactly: NONE"
)
REVIEW_MAX_CHARS = 12000


def build_review_prompt(path: Path, old_text: str | None, new_text: str) -> str:
    """The file with its latest change marked, cut to a size a local model handles quickly."""
    header = f"File: {path.name}\n"
    if old_text is not None and old_text != new_text:
        diff = "\n".join(difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(), "before", "after", n=6, lineterm=""))
        body = f"Changes since the last save (unified diff):\n{diff[:REVIEW_MAX_CHARS // 2]}\n\n"
        return header + body + f"Current file:\n{new_text[:REVIEW_MAX_CHARS // 2]}"
    return header + f"Current file:\n{new_text[:REVIEW_MAX_CHARS]}"


def parse_review(reply: str) -> str | None:
    """The one-line review, or None when the model had nothing useful to say."""
    for line in reply.strip().splitlines():
        line = line.strip().strip("-*•`\"' ").strip()
        if not line:
            continue
        if line.upper().startswith("NONE") or line.lower() in {"looks good", "lgtm", "no issues"}:
            return None
        return line[:140]
    return None


# --------------------------------------------------------------------------- #
# Event inbox (opus_run.py)
# --------------------------------------------------------------------------- #
def inbox_dir() -> Path:
    path = app_dir() / "inbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def post_event(event: dict) -> Path:
    """Drop an event for the running pet to pick up. Written to a temp name first, so it is never read half-written."""
    folder = inbox_dir()
    name = f"{time.time_ns()}_{os.getpid()}"
    tmp = folder / f"{name}.tmp"
    final = folder / f"{name}.json"
    tmp.write_text(json.dumps(event), encoding="utf-8")
    os.replace(tmp, final)
    return final


class EventInbox:
    """Events left by opus_run.py (and anything else that calls post_event)."""

    def poll(self) -> list[dict]:
        events = []
        for path in sorted(inbox_dir().glob("*.json")):
            try:
                events.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                log.debug("Skipping unreadable event %s: %s", path.name, exc)
            try:
                path.unlink()
            except OSError:
                pass
        return events


FAILURE_SYSTEM_PROMPT = (
    "A command the user ran has just failed. From the end of its output, say in ONE short line "
    "(at most 20 words) the most likely cause. No preamble."
)


# --------------------------------------------------------------------------- #
# Clipboard history
# --------------------------------------------------------------------------- #
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\."),  # JSON web tokens
]


def looks_secret(text: str) -> bool:
    """Skip things that look like keys, tokens or passwords, so they never land in the history."""
    if any(p.search(text) for p in _SECRET_PATTERNS):
        return True
    word = text.strip()
    if 8 <= len(word) <= 64 and not re.search(r"\s", word):
        classes = sum(bool(re.search(p, word)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^\w]"))
        if classes == 4:
            return True  # a single "word" with every kind of character is probably a password
    return False


_STOPWORDS = set("""
a an and are as at be but by can did do does for from had has have how i if in is it its me my of on or
our so that the their them then there these this to was we were what when where which who why will with
you your yesterday today copied copy earlier last that thing
""".split())


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9_]{2,}", text.lower()) if t not in _STOPWORDS]


def _time_window(query: str) -> float | None:
    """Hours to look back for phrases like 'yesterday' or 'last week'; None means no limit."""
    q = query.lower()
    if "today" in q:
        midnight = datetime.combine(date.today(), datetime.min.time())
        return (datetime.now() - midnight).total_seconds() / 3600
    if "yesterday" in q:
        return 48.0
    if "last week" in q or "this week" in q:
        return 24.0 * 8
    return None


class ClipboardStore:
    """Text you copy, kept in %APPDATA%\\Opus\\clipboard.db, searchable by meaning-ish keywords and time."""

    MAX_ITEMS = 2000
    MAX_CHARS = 20000
    ERROR_WORDS = ("error", "exception", "traceback", "failed", "fatal", "errno", "denied")

    def __init__(self, path: Path | None = None):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path or app_dir() / "clipboard.db"), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS clips ("
                         "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, text TEXT NOT NULL, topic TEXT)")
        self._db.commit()

    def add(self, text: str) -> bool:
        text = (text or "").strip()
        if not text or looks_secret(text):
            return False
        text = text[: self.MAX_CHARS]
        with self._lock:
            last = self._db.execute("SELECT text FROM clips ORDER BY id DESC LIMIT 1").fetchone()
            if last and last[0] == text:
                return False
            self._db.execute("INSERT INTO clips (ts, text) VALUES (?, ?)", (time.time(), text))
            self._db.execute("DELETE FROM clips WHERE id NOT IN (SELECT id FROM clips ORDER BY id DESC LIMIT ?)",
                             (self.MAX_ITEMS,))
            self._db.commit()
        return True

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, text, topic FROM clips ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "ts": r[1], "text": r[2], "topic": r[3]} for r in rows]

    def search(self, query: str, since_hours: float | None = None, limit: int = 6) -> list[dict]:
        hours = since_hours if since_hours is not None else _time_window(query)
        since = time.time() - hours * 3600 if hours else 0.0
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, text, topic FROM clips WHERE ts >= ? ORDER BY id DESC LIMIT 2000", (since,)).fetchall()
        wanted = set(tokens(query))
        wants_error = any(word in query.lower() for word in ("error", "exception", "traceback", "failed"))
        scored = []
        for index, (cid, ts, text, topic) in enumerate(rows):
            have = set(tokens(text)) | set(tokens(topic or ""))
            score = len(wanted & have) * 2.0
            if wants_error and any(word in text.lower() for word in self.ERROR_WORDS):
                score += 3.0
            if not wanted and not wants_error:
                score = 1.0  # no keywords: just the most recent clips in the window
            if score > 0:
                scored.append((score - index * 0.001, {"id": cid, "ts": ts, "text": text, "topic": topic}))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [clip for _, clip in scored[:limit]]

    def set_topics(self, topics: dict[int, str]) -> None:
        with self._lock:
            self._db.executemany("UPDATE clips SET topic = ? WHERE id = ?",
                                 [(topic, cid) for cid, topic in topics.items()])
            self._db.commit()

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM clips")
            self._db.commit()

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM clips").fetchone()[0]


def format_clips(clips: list[dict], width: int = 300) -> str:
    if not clips:
        return "No matching clipboard entries."
    lines = []
    for clip in clips:
        when = datetime.fromtimestamp(clip["ts"]).strftime("%a %d %b %H:%M")
        topic = f" [{clip['topic']}]" if clip.get("topic") else ""
        text = clip["text"].replace("\r", "")
        lines.append(f"- {when}{topic}: {text[:width]}{'…' if len(text) > width else ''}")
    return "\n".join(lines)


TOPIC_SYSTEM_PROMPT = (
    "Group the user's clipboard snippets by topic. Reply with JSON only, in this shape: "
    '{"topics": [{"name": "short topic name", "ids": [1, 2]}]}. Use 2 to 6 topics with names of 1 to 3 words. '
    "Every id must appear in exactly one topic."
)


def topic_prompt(clips: list[dict]) -> str:
    return "\n".join(f"{c['id']}: {c['text'][:200].replace(chr(10), ' ')}" for c in clips)


def parse_topics(reply: str) -> dict[int, str]:
    match = re.search(r"\{.*\}", reply, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {}
    topics: dict[int, str] = {}
    for group in data.get("topics", []) if isinstance(data, dict) else []:
        name = str(group.get("name", "")).strip()[:40]
        for cid in group.get("ids", []):
            if name and isinstance(cid, int):
                topics[cid] = name
    return topics


# --------------------------------------------------------------------------- #
# Local knowledge base
# --------------------------------------------------------------------------- #
NOTE_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".rst", ".org", ".tex", ".csv", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".html", ".htm",
} | CODE_EXTENSIONS


def _pdf_text(path: Path) -> str | None:
    """Text of a PDF via pypdf, or None when pypdf is not installed."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:300])
    except Exception as exc:  # noqa: BLE001 - one bad PDF must not stop the index
        log.debug("Could not read %s: %s", path, exc)
        return ""


class KnowledgeBase:
    """
    BM25 keyword search over your own files: notes, PDFs and old projects.

    The index is a list of text chunks with their source path, saved to
    %APPDATA%\\Opus\\knowledge.json. Search ranks chunks and every hit keeps its source,
    so answers can say which file they came from.
    """

    CHUNK_CHARS = 900
    OVERLAP = 150
    MAX_FILE_BYTES = 3_000_000
    MAX_CHUNKS = 60000
    K1, B = 1.5, 0.75

    def __init__(self, path: Path | None = None):
        self._path = path or app_dir() / "knowledge.json"
        self._lock = threading.Lock()
        self._chunks: list[tuple[str, str]] = []
        self._postings: dict[str, list[tuple[int, int]]] = {}
        self._lengths: list[int] = []
        self._avg_len = 1.0
        self.ready = False

    # -- building ------------------------------------------------------------ #
    def load(self) -> int:
        """Load the saved index. Returns the number of chunks."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            chunks = [(str(src), str(text)) for src, text in data.get("chunks", [])]
        except (OSError, ValueError):
            chunks = []
        self._install(chunks)
        return len(chunks)

    def build(self, folders: list[str]) -> dict:
        """Read every supported file under the folders, chunk it, index it and save. Slow; run it in a thread."""
        chunks: list[tuple[str, str]] = []
        files = skipped_pdfs = 0
        for root in folders:
            for folder, dirs, names in os.walk(root):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
                for name in names:
                    path = Path(folder) / name
                    suffix = path.suffix.lower()
                    if suffix not in NOTE_EXTENSIONS and suffix != ".pdf":
                        continue
                    try:
                        if path.stat().st_size > self.MAX_FILE_BYTES:
                            continue
                        if suffix == ".pdf":
                            text = _pdf_text(path)
                            if text is None:
                                skipped_pdfs += 1
                                continue
                        else:
                            text = path.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    files += 1
                    for piece in self._chunk(text):
                        chunks.append((str(path), piece))
                    if len(chunks) >= self.MAX_CHUNKS:
                        break
        self._install(chunks)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"built_at": time.time(), "folders": folders, "chunks": chunks}),
                       encoding="utf-8")
        os.replace(tmp, self._path)
        return {"files": files, "chunks": len(chunks), "skipped_pdfs": skipped_pdfs}

    def _chunk(self, text: str) -> list[str]:
        text = re.sub(r"[ \t]+", " ", text).strip()
        if not text:
            return []
        step = self.CHUNK_CHARS - self.OVERLAP
        return [text[i:i + self.CHUNK_CHARS] for i in range(0, len(text), step)]

    def _install(self, chunks: list[tuple[str, str]]) -> None:
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        lengths = []
        for index, (source, text) in enumerate(chunks):
            terms = tokens(text + " " + Path(source).stem.replace("_", " ").replace("-", " "))
            lengths.append(len(terms))
            for term, count in Counter(terms).items():
                postings[term].append((index, count))
        with self._lock:
            self._chunks = chunks
            self._postings = dict(postings)
            self._lengths = lengths
            self._avg_len = (sum(lengths) / len(lengths)) if lengths else 1.0
            self.ready = True

    # -- searching ----------------------------------------------------------- #
    def size(self) -> int:
        with self._lock:
            return len(self._chunks)

    def search(self, query: str, k: int = 5) -> list[tuple[float, str, str]]:
        """Top chunks as (score, source path, text)."""
        with self._lock:
            chunks, postings, lengths, avg = self._chunks, self._postings, self._lengths, self._avg_len
        n = len(chunks)
        if not n:
            return []
        scores: dict[int, float] = defaultdict(float)
        for term in set(tokens(query)):
            hits = postings.get(term)
            if not hits:
                continue
            idf = math.log(1 + (n - len(hits) + 0.5) / (len(hits) + 0.5))
            for index, tf in hits:
                norm = tf + self.K1 * (1 - self.B + self.B * lengths[index] / avg)
                scores[index] += idf * tf * (self.K1 + 1) / norm
        best = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        results, per_file = [], Counter()
        for index, score in best:
            source, text = chunks[index]
            if per_file[source] >= 2:  # keep the answer from leaning on one file
                continue
            per_file[source] += 1
            results.append((score, source, text))
            if len(results) >= k:
                break
        return results


def format_hits(hits: list[tuple[float, str, str]]) -> str:
    if not hits:
        return "Nothing in the user's indexed files matches that."
    return "\n\n".join(f"[{i}] {Path(src).name}  ({src})\n{text}" for i, (_, src, text) in enumerate(hits, 1))


NOTES_INSTRUCTIONS = (
    "Answer the question using only the excerpts from the user's own files below. "
    "After each fact, cite its file name in square brackets, like [notes.md]. "
    "If the excerpts do not contain the answer, say so plainly."
)


# --------------------------------------------------------------------------- #
# Personalities
# --------------------------------------------------------------------------- #
@dataclass
class Personality:
    key: str        # file name without .txt; stored in the config
    name: str       # shown in the menu
    greeting: str   # said when you switch to it
    prompt: str     # the system prompt


_DEFAULT_PERSONALITIES = {
    "opus": (
        "name: Opus (default)\n"
        "greeting: Hi! I'm here if you need me.\n"
        "---\n"
        "You are Opus, a small robot desktop companion living on the user's Windows desktop. "
        "Be friendly, concise and useful: usually two to four sentences, unless the user asks for detail.\n"
    ),
    "strict-reviewer": (
        "name: Strict code reviewer\n"
        "greeting: Show me the code. I'll be honest.\n"
        "---\n"
        "You are a strict, experienced code reviewer. Be direct and specific. Point out bugs, unclear names, "
        "missing error handling and needless complexity, most important first. No flattery, no filler. "
        "When something is genuinely good, say so in a few words.\n"
    ),
    "sarcastic-study-buddy": (
        "name: Sarcastic study buddy\n"
        "greeting: Oh good, studying. My favourite. Let's go.\n"
        "---\n"
        "You are a sarcastic but kind study buddy. Tease lightly and keep it fun, but always give a correct, "
        "clear explanation. Quiz the user with a short question now and then. Never be mean about mistakes.\n"
    ),
    "calm-writing-coach": (
        "name: Calm writing coach\n"
        "greeting: Take a breath. Let's look at your writing together.\n"
        "---\n"
        "You are a calm, encouraging writing coach. Suggest clearer wording, better structure and stronger "
        "openings, and explain why in a sentence. Keep the writer's own voice. Offer at most three changes at a time.\n"
    ),
    "rubber-duck": (
        "name: Rubber duck\n"
        "greeting: Quack. Tell me about the problem, from the start.\n"
        "---\n"
        "You are a rubber duck for debugging. Do NOT give answers, fixes or code. Ask ONE short clarifying "
        "question at a time that helps the user explain their problem and find the answer themselves: what they "
        "expected, what actually happened, what they already tried, what changed recently, and what each step "
        "of their code does. If they reach the answer, congratulate them briefly.\n"
    ),
}


def personalities_dir() -> Path:
    """%APPDATA%\\Opus\\personalities. Seeded with the defaults the first time it is created."""
    folder = app_dir() / "personalities"
    if not folder.exists():
        folder.mkdir(parents=True)
        for key, text in _DEFAULT_PERSONALITIES.items():
            (folder / f"{key}.txt").write_text(text, encoding="utf-8")
    return folder


def parse_personality(key: str, text: str) -> Personality:
    """
    Personality file format: optional 'name:' and 'greeting:' lines, a '---' line, then the prompt.
    A file with no header is all prompt, named after the file.
    """
    name, greeting, prompt = key.replace("-", " ").capitalize(), "", text.strip()
    head, sep, body = text.partition("\n---")
    if sep:
        for line in head.splitlines():
            field_name, _, value = line.partition(":")
            if field_name.strip().lower() == "name" and value.strip():
                name = value.strip()
            elif field_name.strip().lower() == "greeting":
                greeting = value.strip()
        prompt = body.lstrip("-").strip()
    return Personality(key=key, name=name, greeting=greeting, prompt=prompt)


def load_personalities() -> list[Personality]:
    found = []
    for path in sorted(personalities_dir().glob("*.txt")):
        try:
            found.append(parse_personality(path.stem, path.read_text(encoding="utf-8")))
        except OSError:
            continue
    if not any(p.key == "opus" for p in found):
        found.append(parse_personality("opus", _DEFAULT_PERSONALITIES["opus"]))
    found.sort(key=lambda p: (p.key != "opus", p.name.lower()))  # the default first, then A to Z
    return found


def builtin_personality(key: str) -> Personality:
    """A default personality even if its file was deleted (duck mode always needs the duck)."""
    path = personalities_dir() / f"{key}.txt"
    try:
        return parse_personality(key, path.read_text(encoding="utf-8"))
    except OSError:
        return parse_personality(key, _DEFAULT_PERSONALITIES[key])


# --------------------------------------------------------------------------- #
# Calendar (.ics)
# --------------------------------------------------------------------------- #
def _parse_ics_time(value: str, params: str) -> datetime | None:
    """ICS date or date-time to a naive local datetime. TZID times are taken as local time."""
    value = value.strip()
    try:
        if "VALUE=DATE" in params.upper() or len(value) == 8:
            return datetime.strptime(value[:8], "%Y%m%d")
        if value.endswith("Z"):
            utc = datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            return utc.astimezone().replace(tzinfo=None)
        return datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None


def parse_ics_events(text: str) -> list[tuple[datetime, str]]:
    """(start, summary) for each VEVENT. Recurring rules (RRULE) are not expanded."""
    lines: list[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]  # folded line continues the previous one
        else:
            lines.append(raw)
    events, start, summary, inside = [], None, "", False
    for line in lines:
        if line == "BEGIN:VEVENT":
            inside, start, summary = True, None, ""
        elif line == "END:VEVENT" and inside:
            if start:
                events.append((start, summary or "(no title)"))
            inside = False
        elif inside:
            key, _, value = line.partition(":")
            name, _, params = key.partition(";")
            if name == "DTSTART":
                start = _parse_ics_time(value, params)
            elif name == "SUMMARY":
                summary = value.replace("\\,", ",").replace("\\n", " ").strip()
    return events


def next_calendar_event(source: str, now: datetime | None = None) -> tuple[datetime, str] | None:
    """The next upcoming event (within two weeks) from an .ics file path or http(s) URL."""
    if not source:
        return None
    if source.lower().startswith(("http://", "https://", "webcal://")):
        url = "https://" + source[len("webcal://"):] if source.lower().startswith("webcal://") else source
        network.check(url)
        import requests
        resp = requests.get(url, timeout=(5.0, 15.0))
        resp.raise_for_status()
        text = resp.text
    else:
        text = Path(source).read_text(encoding="utf-8", errors="ignore")
    now = now or datetime.now()
    upcoming = [(start, title) for start, title in parse_ics_events(text)
                if now <= start <= now + timedelta(days=14)]
    return min(upcoming) if upcoming else None


def describe_event(event: tuple[datetime, str] | None, now: datetime | None = None) -> str:
    if not event:
        return "No upcoming events"
    start, title = event
    now = now or datetime.now()
    if start.date() == now.date():
        when = start.strftime("%H:%M") if (start.hour or start.minute) else "today"
    elif start.date() == now.date() + timedelta(days=1):
        when = "tomorrow " + start.strftime("%H:%M") if (start.hour or start.minute) else "tomorrow"
    else:
        when = start.strftime("%a %d %b %H:%M")
    return f"{title} · {when}"


# --------------------------------------------------------------------------- #
# Mini-scripts
# --------------------------------------------------------------------------- #
SCRIPT_SYSTEM_PROMPT = (
    "Write a short, safe Python 3 script that does what the user asks. Use only the standard library. "
    "The script runs with its working directory set to the folder the user chose, so use relative paths "
    "or Path('.'). Print one line for every change it makes. Never delete files unless the user explicitly "
    "asked for deletion. Never touch files outside the working folder. If a name clash is possible, skip that "
    "file and print a warning rather than overwrite. Reply with a single ```python code block and nothing else."
)


def extract_python(reply: str) -> str | None:
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", reply, re.DOTALL)
    if match:
        return match.group(1).strip() + "\n"
    stripped = reply.strip()
    if stripped.startswith(("import ", "from ", "#", "def ", "for ", "with ")):
        return stripped + "\n"
    return None


def save_script(code: str) -> Path:
    folder = app_dir() / "scripts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"script_{datetime.now():%Y%m%d_%H%M%S}.py"
    path.write_text(code, encoding="utf-8")
    return path


def run_script(path: Path, cwd: str, timeout: float = 300.0) -> dict:
    """Run an approved script in its own Python process. Returns exit code, combined output and duration."""
    started = time.time()
    flags = 0x08000000 if _WINDOWS else 0  # CREATE_NO_WINDOW: no console flashes up
    try:
        done = subprocess.run([sys.executable, "-I", str(path)], cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, creationflags=flags)
        output, code, timed_out = (done.stdout or "") + (done.stderr or ""), done.returncode, False
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        code, timed_out = -1, True
    return {"exit_code": code, "output": output[-20000:], "timed_out": timed_out,
            "seconds": round(time.time() - started, 1)}


# --------------------------------------------------------------------------- #
# Calculator tool
# --------------------------------------------------------------------------- #
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}
_FUNCS = {name: getattr(math, name) for name in
          ("sqrt", "sin", "cos", "tan", "log", "log10", "exp", "floor", "ceil", "radians", "degrees")}
_FUNCS.update({"abs": abs, "round": round, "min": min, "max": max})
_CONSTS = {"pi": math.pi, "e": math.e}


def calculate(expression: str) -> str:
    """Safe arithmetic: numbers, + - * / // % **, and a few math functions. No names or attributes."""
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("exponent too large")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
            return _FUNCS[node.func.id](*[ev(arg) for arg in node.args])
        raise ValueError("unsupported expression")

    try:
        value = ev(ast.parse(expression.replace("^", "**"), mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError) as exc:
        return f"Could not calculate {expression!r}: {exc}"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    return f"{expression} = {value}"
