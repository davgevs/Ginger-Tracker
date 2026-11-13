#Requires Python 3 on Windows
import ctypes
import math
import hashlib
import queue
import subprocess
import urllib.request
import urllib.error
import json
import os
import shutil
import sys
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk
from datetime import date, datetime
from pathlib import Path

from pywinauto import Desktop
import pyautogui

# Store data outside PyInstaller's temporary _MEIPASS extraction directory.
# This location is stable across upgrades, restarts, and different launch folders.
APP_DATA = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / 'AppData' / 'Local')) / 'GingerTracker'
DATA_FILE = APP_DATA / 'ginger_tracker_v2.json'
OLD_FILE = APP_DATA / 'ginger_count.json'


def migrate_existing_data():
    """Copy an existing save to the stable location without modifying the original."""
    APP_DATA.mkdir(parents=True, exist_ok=True)
    if DATA_FILE.exists():
        return  # Never overwrite a save already in the new location.
    executable_folder = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
    candidates = [
        Path.home() / 'Desktop' / 'ginger_tracker_v2.json',
        Path.home() / 'OneDrive' / 'Desktop' / 'ginger_tracker_v2.json',
        executable_folder / 'ginger_tracker_v2.json',
        executable_folder.parent / 'ginger_tracker_v2.json',
        Path.cwd() / 'ginger_tracker_v2.json',
        Path(__file__).resolve().parent / 'ginger_tracker_v2.json',
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                data = json.loads(candidate.read_text(encoding='utf-8'))
                if isinstance(data, dict) and isinstance(data.get('days'), dict):
                    shutil.copy2(candidate, DATA_FILE)
                    return
        except (OSError, ValueError, TypeError):
            continue
    # Also preserve the legacy lifetime counter when present.
    for folder in [executable_folder, executable_folder.parent, Path.home() / 'Desktop']:
        legacy = folder / 'ginger_count.json'
        if legacy.is_file() and not OLD_FILE.exists():
            shutil.copy2(legacy, OLD_FILE)
            break


APP_VERSION = '6.1.2'
RELEASE_API = 'https://api.github.com/repos/davgevs/Ginger-Tracker/releases/latest'
MAX_EXE_BYTES = 150 * 1024 * 1024


def latest_release():
    req = urllib.request.Request(RELEASE_API, headers={
        'User-Agent': 'GingerTracker/' + APP_VERSION,
        'Accept': 'application/vnd.github+json',
    })
    with urllib.request.urlopen(req, timeout=12) as response:
        info = json.load(response)
    tag = str(info.get('tag_name', '')).strip().lstrip('vV')
    if not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', tag):
        raise ValueError('Release tag must look like v5.9 or v5.9.0')
    def version(v):
        return tuple(int(n) for n in (v.split('.') + ['0', '0'])[:3])
    if info.get('draft') or info.get('prerelease') or version(tag) <= version(APP_VERSION):
        return None
    assets = {a.get('name'): a for a in info.get('assets', [])}
    exe = assets.get('GingerTracker.exe')
    checksum = assets.get('GingerTracker.exe.sha256')
    if not exe or not checksum:
        raise ValueError('Release needs GingerTracker.exe and GingerTracker.exe.sha256 assets')
    for a in (exe, checksum):
        url = a.get('browser_download_url', '')
        if not url.startswith('https://github.com/davgevs/Ginger-Tracker/releases/download/'):
            raise ValueError('Unexpected release asset URL')
    return {'version': tag, 'exe': exe['browser_download_url'],
            'checksum': checksum['browser_download_url'],
            'notes': str(info.get('body') or '')[:500]}


def fetch_bytes(url, max_bytes):
    req = urllib.request.Request(url, headers={'User-Agent': 'GingerTracker/' + APP_VERSION})
    with urllib.request.urlopen(req, timeout=30) as response:
        if not response.geturl().startswith('https://'):
            raise ValueError('Insecure download redirect')
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError('Download exceeds allowed size')
    return data


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def launch_replace_helper(staged):
    """Start a logged, detached Windows installer; never touch task data."""
    target = Path(sys.executable).resolve()
    staged = Path(staged).resolve()
    if not getattr(sys, 'frozen', False) or target.suffix.lower() != '.exe':
        raise RuntimeError('Automatic installation requires the packaged EXE.')
    if not staged.is_file() or staged.stat().st_size < 1024 * 1024:
        raise RuntimeError('Verified update file is missing or incomplete.')
    APP_DATA.mkdir(parents=True, exist_ok=True)
    backup = APP_DATA / 'GingerTracker.previous.exe'
    script = APP_DATA / 'install_update.ps1'
    log = APP_DATA / 'update_install.log'
    pid = os.getpid()
    # PowerShell uses Copy-Item for the verified staged file, so it stays available
    # for recovery. Log every step, including failures before the swap.
    body = f"""$ErrorActionPreference = 'Stop'
$target = {ps_quote(target)}
$staged = {ps_quote(staged)}
$backup = {ps_quote(backup)}
$log = {ps_quote(log)}
$pidToWait = {pid}
function Log([string]$message) {{
    Add-Content -LiteralPath $log -Value (('[{{0}}] {{1}}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message)) -Encoding UTF8
}}
try {{
    Log 'Installer started'
    # Wait for the exact original process to exit, not just a reused PID.
    $old = Get-Process -Id $pidToWait -ErrorAction SilentlyContinue
    if ($null -ne $old) {{
        try {{ $old.WaitForExit(120000) | Out-Null }} catch {{ Log ('Wait warning: ' + $_.Exception.Message) }}
    }}
    for ($i = 0; $i -lt 30; $i++) {{
        if (-not (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue)) {{ break }}
        Start-Sleep -Milliseconds 500
    }}
    if (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue) {{ throw 'Old app did not exit within timeout' }}
    if (-not (Test-Path -LiteralPath $staged -PathType Leaf)) {{ throw 'Staged update missing' }}
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {{ throw 'Installed EXE missing' }}
    Log 'Old app exited; replacing executable'
    # Retry sharing violations from Windows Defender or slow filesystem release.
    $swapped = $false
    $backupReady = $false
    for ($attempt = 1; $attempt -le 20; $attempt++) {{
        try {{
            Copy-Item -LiteralPath $target -Destination $backup -Force -ErrorAction Stop
            $backupReady = $true
            Copy-Item -LiteralPath $staged -Destination $target -Force -ErrorAction Stop
            $swapped = $true
            break
        }} catch {{
            Log ('Copy attempt ' + $attempt + ': ' + $_.Exception.Message)
            Start-Sleep -Milliseconds 750
        }}
    }}
    if (-not $swapped) {{ throw 'Could not replace EXE after retries' }}
    # Compare exact downloaded bytes after the installation.
    $expected = (Get-FileHash -LiteralPath $staged -Algorithm SHA256).Hash
    $actual = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    if ($expected -ne $actual) {{ throw 'Installed EXE checksum mismatch' }}
    Log 'Installed checksum verified'
    $proc = Start-Process -FilePath $target -WorkingDirectory (Split-Path -Parent $target) -PassThru -ErrorAction Stop
    Log ('Launched new EXE PID=' + $proc.Id)
    Start-Sleep -Seconds 3
    $proc.Refresh()
    if ($proc.HasExited) {{ throw ('Updated app exited immediately, exit code=' + $proc.ExitCode) }}
    Log 'Update completed; app still running after 3 seconds'
    Remove-Item -LiteralPath $staged -Force -ErrorAction SilentlyContinue
}} catch {{
    Log ('ERROR: ' + ($_ | Out-String))
    try {{
        if ($backupReady -and (Test-Path -LiteralPath $backup -PathType Leaf)) {{
            Copy-Item -LiteralPath $backup -Destination $target -Force -ErrorAction Stop
            Log 'Restored previous executable'
            Start-Process -FilePath $target -WorkingDirectory (Split-Path -Parent $target) -ErrorAction Stop | Out-Null
            Log 'Restarted previous executable'
        }}
    }} catch {{ Log ('ROLLBACK ERROR: ' + ($_ | Out-String)) }}
    exit 1
}}
"""
    script.write_text(body, encoding='utf-8-sig')
    # Explicitly ask PowerShell to run the helper without a console window.
    proc = subprocess.Popen(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-WindowStyle', 'Hidden', '-File', str(script)],
        cwd=str(APP_DATA),
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP |
                      subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True)
    # Detect immediate launcher failures before the application closes.
    time.sleep(0.3)
    if proc.poll() is not None and proc.returncode != 0:
        raise RuntimeError('Update helper exited early. See update_install.log.')


POLL = 0.5
STABLE = 3.0
POPUP_QUIET = 4.0
# Default matches the proven V5.9.4 timing. Fast mode is opt-in.
DETECTION_MODES = {
    'Safe': {'poll': 0.5, 'stable': 3.0, 'quiet': 4.0, 'absent': 3.0, 'confirmed_absent': 6.0, 'new_task_cooldown': 5.0},
    'Balanced': {'poll': 0.3, 'stable': 1.5, 'quiet': 2.5, 'absent': 2.0, 'confirmed_absent': 4.0, 'new_task_cooldown': 3.0},
    'Fast': {'poll': 0.2, 'stable': 1.0, 'quiet': 2.0, 'absent': 1.5, 'confirmed_absent': 3.0, 'new_task_cooldown': 2.0},
}
BACKUP_DIR = APP_DATA / 'backups'
BACKUP_INTERVAL = 30 * 60
BACKUP_KEEP = 20
TIMEOUT = 600.0
SAMPLES = [(0.34, 0.35), (0.48, 0.52), (0.68, 0.44), (0.36, 0.76), (0.69, 0.76)]
BLANK = (243, 243, 243)
TOLERANCE = 12
user32 = ctypes.windll.user32


def day_key():
    return date.today().isoformat()


def default_day():
    return {'count': 0, 'seconds': 0.0, 'completed_seconds': 0.0, 'timed_count': 0, 'history': []}


def load_data():
    migrate_existing_data()
    try:
        data = json.loads(DATA_FILE.read_text(encoding='utf-8'))
        if isinstance(data.get('days'), dict):
            return data
    except (OSError, ValueError, TypeError):
        pass
    old_count = 0
    try:
        old_count = max(0, int(json.loads(OLD_FILE.read_text(encoding='utf-8')).get('count', 0)))
    except (OSError, ValueError, TypeError):
        pass
    # Import the existing counter as a lifetime baseline, not as today's completed work.
    return {'baseline': old_count, 'days': {}}


def backup_data(force=False):
    """Keep a verified snapshot of the existing JSON, without overwriting it."""
    try:
        if not DATA_FILE.is_file():
            return False
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        existing = sorted(BACKUP_DIR.glob('ginger_tracker_*.json'))
        if not force and existing and time.time() - existing[-1].stat().st_mtime < BACKUP_INTERVAL:
            return False
        raw = DATA_FILE.read_bytes()
        parsed = json.loads(raw.decode('utf-8'))
        if not isinstance(parsed, dict) or not isinstance(parsed.get('days'), dict):
            return False
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        target = BACKUP_DIR / f'ginger_tracker_{stamp}.json'
        target.write_bytes(raw)
        for old in sorted(BACKUP_DIR.glob('ginger_tracker_*.json'))[:-BACKUP_KEEP]:
            old.unlink(missing_ok=True)
        return True
    except (OSError, ValueError, TypeError):
        return False


def safe_rate(value, default=100.0):
    try:
        rate = float(value)
        return rate if math.isfinite(rate) and 0 <= rate <= 100000 else default
    except (ValueError, TypeError):
        return default


def save_data(data):
    tmp = DATA_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    tmp.replace(DATA_FILE)


# User-adjustable, unofficial complexity factors. These are NOT Ginger pay rates.
DIFFICULTIES = {
    'Easy': ('#36C65A', 'Mostly empty, water, or very few features', 0.70),
    'Medium': ('#3478F6', 'Some roads and features to annotate', 0.90),
    'Hard': ('#FFA20B', 'Busy map with many features', 1.00),
    'Very Hard': ('#E43F50', 'Dense intersections or complex features', 1.20),
}


def auto_suggest_difficulty(item):
    """Duration-only heuristic, NOT a measurement of actual map complexity.

    A slow worker on an easy map can get a high suggestion; users should review.
    Thresholds are provisional and do not represent official Ginger standards.
    """
    try:
        minutes = float(item.get('duration_seconds', 0) or 0) / 60.0
    except (TypeError, ValueError, AttributeError):
        return 'Medium'
    if not math.isfinite(minutes) or minutes <= 0:
        return 'Medium'
    if minutes < 5:
        return 'Easy'
    if minutes < 12:
        return 'Medium'
    if minutes < 22:
        return 'Hard'
    return 'Very Hard'


def task_difficulty(item):
    if not isinstance(item, dict):
        return 'Medium'
    value = item.get('difficulty', 'Auto')
    if value == 'Auto':
        return auto_suggest_difficulty(item)
    return value if value in DIFFICULTIES else 'Medium'


def estimated_day_pay(day, hourly_rate):
    """Preserve hourly baseline for time not represented by saved task records."""
    seconds = max(0.0, float(day.get('seconds', 0) or 0))
    history = day.get('history', [])
    adjustment = 0.0
    if isinstance(history, list):
        for item in history:
            if not isinstance(item, dict):
                continue
            try:
                duration = max(0.0, float(item.get('duration_seconds', 0) or 0))
            except (ValueError, TypeError):
                continue
            if not math.isfinite(duration):
                continue
            adjustment += duration * (DIFFICULTIES[task_difficulty(item)][2] - 1.0)
    return max(0.0, (seconds + adjustment) * safe_rate(hourly_rate) / 3600.0)


def fmt(seconds):
    n = max(0, int(seconds))
    return f'{n // 3600:02}:{n // 60 % 60:02}:{n % 60:02}'


def get_windows():
    main, popup = None, False
    try:
        for w in Desktop(backend='win32').windows(visible_only=True):
            try:
                title = w.window_text().strip().lower()
                if title.startswith('ginger-2025') and 'not responding' not in title:
                    main = w
                elif title == 'ginger':
                    popup = True
            except Exception:
                continue
    except Exception:
        pass
    return main, popup


def blank_workspace(w):
    try:
        hwnd = w.handle
        if user32.IsIconic(hwnd):
            return None
        # The floating tracker can be focused while Ginger remains visible.
        # Sample points are away from the tracker at the upper-left.
        rect = w.rectangle()
        if rect.width() < 700 or rect.height() < 400:
            return None
        for fx, fy in SAMPLES:
            x = rect.left + round(rect.width() * fx)
            y = rect.top + round(rect.height() * fy)
            rgb = pyautogui.pixel(x, y)
            if any(abs(a - b) > TOLERANCE for a, b in zip(rgb, BLANK)):
                return False
        return True
    except Exception:
        return None


ELAPSED_RE = re.compile(
    r'(?i)time\s*elapsed\s*[:\-]?\s*'
    r'(?:(\d+)\s*h(?:ours?)?\s*(\d+)\s*m(?:in(?:utes?)?)?\s*(-?\d+)\s*s'
    r'|(-?\d{1,3}):([0-5][0-9]):([0-5][0-9]))'
)


def parse_elapsed(text):
    """Parse Ginger elapsed, including 00h 00m -7s countdown values."""
    match = ELAPSED_RE.search(text)
    if not match:
        return None
    parts = match.groups()
    if parts[0] is not None:
        h, m, sec = map(int, parts[:3])
        return h * 3600 + m * 60 + sec
    h, m, sec = map(int, parts[3:])
    return (-1 if h < 0 else 1) * (abs(h) * 3600 + m * 60 + sec)


def ginger_elapsed(main):
    """Read text from the Ginger window through Win32/UIA; no screenshots/OCR.

    Returns None if Ginger does not expose the field. This function never
    interacts with or changes Ginger's controls.
    """
    if main is None:
        return None
    hwnd = main.handle
    for backend in ('win32', 'uia'):
        try:
            window = Desktop(backend=backend).window(handle=hwnd)
            # Collect individual control texts, since label and value may
            # appear in separate controls in a Windows UI.
            parts = []
            for control in window.descendants():
                try:
                    t = control.window_text().strip()
                    if t and len(t) < 500:
                        parts.append(t)
                except Exception:
                    continue
            for i, part in enumerate(parts):
                val = parse_elapsed(part)
                if val is not None:
                    return val
                if 'time elapsed' in part.lower():
                    nearby = ' '.join(parts[i:i + 4])
                    val = parse_elapsed(nearby)
                    if val is not None:
                        return val
        except Exception:
            continue
    return None


# Ginger's Tasks tree sometimes needs Task 0 selected before it exposes
# the Time Elapsed control. Avoid screen coordinates: locate the task by its
# accessible name. Only click when Ginger is the foreground window so the
# tracker cannot unexpectedly steal focus from another application.
TASK_ZERO_RE = re.compile(r'(?i)^\s*task\s*0(?=$|[^0-9])')
AUTO_SELECT_COOLDOWN = 0.5
UNSYNCED_ELAPSED_POLL = 0.5


def ginger_is_foreground(main):
    """Accept focus on a Ginger-owned child, but never another app."""
    try:
        fg = user32.GetForegroundWindow()
        if not fg or user32.IsIconic(main.handle):
            return False
        GA_ROOT = 2
        return (fg == main.handle or
                user32.GetAncestor(fg, GA_ROOT) == main.handle)
    except Exception:
        return False


def task0_debug(message):
    """Leave evidence of each auto-select failure in a text file."""
    try:
        APP_DATA.mkdir(parents=True, exist_ok=True)
        with (APP_DATA / 'task0_debug.log').open('a', encoding='utf-8') as f:
            f.write(datetime.now().strftime('%H:%M:%S.%f')[:-3] + ' ' + str(message) + '\n')
    except Exception:
        pass


def task_zero_state(main):
    """Return True/False when Task 0 row presence is readable, None otherwise.

    False is accepted only when the Tasks pane itself is accessible. This
    avoids interpreting a temporary UIA failure as a completed task.
    """
    if main is None:
        return None
    for backend in ('uia', 'win32'):
        try:
            window = Desktop(backend=backend).window(handle=main.handle)
            controls = window.descendants()
            names = []
            for c in controls:
                try:
                    t = c.window_text().strip()
                    if t:
                        names.append(t)
                except Exception:
                    continue
            if any(TASK_ZERO_RE.search(t) for t in names):
                return True
            if any(t.lower() == 'tasks' for t in names):
                return False
        except Exception:
            continue
    return None


def select_ginger_task_zero(main):
    """Double-click the Task 0 TreeItem, even when its name has metadata."""
    if main is None or not ginger_is_foreground(main):
        task0_debug('SKIP: Ginger not foreground')
        return False
    found = []
    for backend in ('uia', 'win32'):
        try:
            window = Desktop(backend=backend).window(handle=main.handle)
            for control in window.descendants():
                try:
                    name = control.window_text().strip()
                    if not TASK_ZERO_RE.match(name):
                        continue
                    # Prefer the actual Tasks tree row, not unrelated text controls.
                    if backend == 'uia' and control.element_info.control_type != 'TreeItem':
                        continue
                    rect = control.rectangle()
                    found.append((backend, name, rect.left, rect.top, rect.right, rect.bottom))
                    if not (control.is_visible() and control.is_enabled()):
                        continue
                    if rect.width() < 8 or rect.height() < 8:
                        continue
                    x, y = (rect.left + rect.right)//2, (rect.top + rect.bottom)//2
                    original = pyautogui.position()
                    try:
                        if not ginger_is_foreground(main):
                            task0_debug('SKIP: focus changed before click')
                            return False
                        if not user32.SetCursorPos(int(x), int(y)):
                            task0_debug('FAIL: SetCursorPos')
                            continue
                        pyautogui.doubleClick(interval=0.055, _pause=False)
                        task0_debug(f'CLICK: {backend} Task 0 at ({x},{y})')
                        return True
                    finally:
                        user32.SetCursorPos(int(original.x), int(original.y))
                except Exception as exc:
                    task0_debug(f'CONTROL ERROR {backend}: {type(exc).__name__}: {exc}')
        except Exception as exc:
            task0_debug(f'BACKEND ERROR {backend}: {type(exc).__name__}: {exc}')
    task0_debug(f'NO CLICK: matching controls={found!r}')
    return False


class Tracker:
    BG = '#F3F6FB'
    WHITE = '#FFFFFF'
    INK = '#172033'
    MUTED = '#64748B'
    BLUE = '#2563EB'
    LINE = '#E2E8F0'

    def __init__(self):
        self.data = load_data()
        self.data.setdefault('baseline', 0)
        self.data.setdefault('days', {})
        self.data.setdefault('settings', {})
        settings = self.data['settings']
        settings.setdefault('goal_hours', 8.0)
        settings.setdefault('goal_tasks', 30)
        settings.setdefault('hourly_rate', 100.0)
        settings.setdefault('detection_mode', 'Safe')
        settings.setdefault('benchmark_minutes', 15)
        settings.setdefault('benchmark_warning_minutes', 1)
        settings.setdefault('benchmark_sound', True)
        settings.setdefault('benchmark_enabled', True)
        if settings['detection_mode'] not in DETECTION_MODES:
            settings['detection_mode'] = 'Safe'
        settings['hourly_rate'] = safe_rate(settings['hourly_rate'])
        # A first-run snapshot preserves the last V5.9.4 save before any V6 writes.
        backup_data(force=not bool(list(BACKUP_DIR.glob('ginger_tracker_*.json'))) if BACKUP_DIR.exists() else True)
        self.task_seconds = 0.0
        self.task_running = False
        self.sync_offset = 0.0
        self.last_ginger_elapsed = None
        self.sync_active = False
        self.status_text = 'Starting detector...'
        self.benchmark_alert_text = ''
        self.benchmark_alert_until = 0.0
        self.benchmark_alert_queue = queue.Queue()
        self.reminder_toast = None
        self.alarm_generation = 0
        self.alarm_end_at = 0.0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.root = tk.Tk()
        self.root.title('Ginger Tracker V6.1.2')
        self.root.geometry(self.data['settings'].get('window_geometry', '265x365+25+65'))
        self.root.minsize(215, 290)
        self.root.resizable(True, True)
        self.root.configure(bg=self.BG)
        self.root.attributes('-topmost', True)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.update_events = queue.Queue()
        self.available_release = None
        self.update_busy = False
        self.update_prompted = False
        self.update_status = 'Checking for updates...'
        self.page = 'dashboard'
        self.page_frame = None
        self._previous_geometry = None
        self.build_dashboard()
        self.root.after(250, self.refresh)
        threading.Thread(target=self.monitor, daemon=True).start()
        threading.Thread(target=self.check_updates, daemon=True).start()

    def day(self):
        return self.data['days'].setdefault(day_key(), default_day())

    def label(self, parent, text, size=10, color=None, bold=False, **kw):
        return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=color or self.INK,
                        font=('Segoe UI', size, 'bold' if bold else 'normal'), **kw)

    def card(self, parent, pad=14):
        f = tk.Frame(parent, bg=self.WHITE, highlightthickness=1, highlightbackground='#E7EDF5')
        f.pack(fill='x', pady=5)
        inner = tk.Frame(f, bg=self.WHITE, padx=pad, pady=pad)
        inner.pack(fill='both', expand=True)
        return inner

    def button(self, parent, text, action, compact=False):
        btn = tk.Button(parent, text=text, command=action, bg=self.WHITE, fg=self.BLUE,
                         activebackground='#BFDBFE', activeforeground=self.BLUE,
                         relief='flat', bd=0, cursor='hand2', font=('Segoe UI', 10, 'bold'),
                         padx=10 if compact else 15, pady=7)
        btn.bind('<Enter>', lambda e: btn.configure(bg='#DBEAFE'))
        btn.bind('<Leave>', lambda e: btn.configure(bg=self.WHITE))
        return btn

    def build_dashboard(self):
        # One canvas, drawn in proportional coordinates (Roblox Scale-style).
        # Unlike nested Tk widgets, labels, padding and cards all scale together.
        self.dashboard_canvas = tk.Canvas(self.root, bg=self.BG, bd=0,
                                          highlightthickness=0, cursor='arrow')
        self.dashboard_canvas.pack(fill='both', expand=True)
        self.dashboard_canvas.bind('<Configure>', lambda event: self.draw_dashboard())
        self.dashboard_canvas.bind('<Button-1>', self.dashboard_click)
        self.dashboard_canvas.bind('<Motion>', self.dashboard_motion)
        self.dashboard_canvas.bind('<Leave>', self.dashboard_leave)
        self.root.minsize(215, 290)
        self.root.geometry(self.data['settings'].get('window_geometry', '265x365+25+65'))
        self._hitboxes = []
        self._hover_action = None
        self._hover_region = None

    def dashboard_motion(self, event):
        match = next(((bounds, action) for bounds, action in self._hitboxes
                      if bounds[0] <= event.x <= bounds[2] and bounds[1] <= event.y <= bounds[3]), None)
        region, action = match if match else (None, None)
        self.dashboard_canvas.configure(cursor='hand2' if action else 'arrow')
        if action != self._hover_action or region != self._hover_region:
            self._hover_action, self._hover_region = action, region
            self.draw_dashboard()

    def dashboard_leave(self, event):
        self.dashboard_canvas.configure(cursor='arrow')
        if self._hover_action is not None:
            self._hover_action = None
            self._hover_region = None
            self.draw_dashboard()

    def dashboard_click(self, event):
        for (x1, y1, x2, y2), action in self._hitboxes:
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                action()
                return

    def draw_dashboard(self):
        c = self.dashboard_canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 2 or h < 2:
            return
        c.delete('all')
        self._hitboxes = []
        # Responsive layout: compact when the window is short or narrow.
        # Keep the original proportional canvas design within each mode.
        compact = h < 440 or w < 285
        design_w, design_h = (265.0, 365.0) if compact else (390.0, 560.0)
        sx, sy = w / design_w, h / design_h
        font_scale = min(sx, sy)

        def rect(x1, y1, x2, y2, fill, r=0):
            x1, y1, x2, y2 = x1*sx, y1*sy, x2*sx, y2*sy
            radius = min(r*font_scale, (x2-x1)/2, (y2-y1)/2)
            if radius <= 1:
                c.create_rectangle(x1, y1, x2, y2, fill=fill, outline='')
                return
            pts = [x1+radius,y1,x2-radius,y1,x2,y1,x2,y1+radius,
                   x2,y2-radius,x2,y2,x2-radius,y2,x1+radius,y2,
                   x1,y2,x1,y2-radius,x1,y1+radius,x1,y1]
            c.create_polygon(pts, fill=fill, outline='', smooth=True, splinesteps=12)

        def txt(x, y, value, size=12, color=None, bold=False, anchor='nw', max_width=None):
            c.create_text(x*sx, y*sy, text=str(value), anchor=anchor,
                          fill=color or self.INK,
                          font=('Segoe UI', max(6, round(size*font_scale)),
                                'bold' if bold else 'normal'),
                          width=max_width*sx if max_width else 0)

        def hit(x1, y1, x2, y2, action):
            self._hitboxes.append(((x1*sx,y1*sy,x2*sx,y2*sy), action))

        def hovered(x1, y1, x2, y2):
            return self._hover_region == (x1*sx, y1*sy, x2*sx, y2*sy)

        with self.lock:
            d = self.day()
            total = self.data['baseline'] + sum(int(x.get('count',0)) for x in self.data['days'].values())
            task = fmt(self.task_seconds) if self.task_running else '00:00:00'
            seconds = float(d.get('seconds',0))
            count = int(d.get('count',0))
            rate = f'{count/(seconds/3600):.2f}' if seconds>0 else '—'
            avg = fmt(d['completed_seconds']/d['timed_count']) if d.get('timed_count') else '—'
            gh = max(.1,float(self.data['settings'].get('goal_hours',8)))
            gt = max(1,int(self.data['settings'].get('goal_tasks',30)))
            synced = self.task_running and self.sync_active
            status = self.status_text
            hourly_rate = safe_rate(self.data['settings'].get('hourly_rate', 100))
            earnings = seconds / 3600 * hourly_rate

        if compact:
            # Purpose-built narrow layout: no tiny stretched fullscreen labels.
            rect(0, 0, 265, 365, self.BG)
            txt(13, 12, '◉  Ginger' if self.available_release else '◉  Ginger Tracker', 16, self.INK, True)
            txt(14, 41, '●  Synced with Ginger' if synced else
                '●  Task active' if self.task_running else '●  Waiting for a task',
                9, '#16A34A' if self.task_running else self.MUTED)
            if self.available_release:
                rect(157, 9, 255, 34, '#BFDBFE' if self._hover_action == self.open_update else '#DBEAFE', 8)
                txt(206, 21, 'Update ' + self.available_release['version'], 9, self.BLUE, True, 'center')
                hit(157, 9, 255, 36, self.open_update)
            rect(10, 63, 255, 142, self.WHITE, 11)
            txt(23, 74, 'CURRENT TASK', 9, self.MUTED, True)
            txt(23, 94, task, 26, self.INK, True)
            tiles = [('Completed today', str(count)), ('Today worked', fmt(seconds)),
                     ('Tasks / hour', rate), ('Average task', avg)]
            for i, (name, value) in enumerate(tiles):
                x = 10 + (i % 2) * 125
                y = 151 + (i // 2) * 76
                rect(x, y, x + 120, y + 68, self.WHITE, 10)
                txt(x + 9, y + 9, name, 9, self.MUTED)
                txt(x + 9, y + 30, value, 15 if len(value) < 7 else 12,
                    self.INK, True)
            rect(10, 305, 255, 350, self.WHITE, 10)
            if hovered(14, 329, 130, 349):
                rect(14, 329, 130, 349, '#EAF2FF', 5)
            elif hovered(138, 329, 253, 349):
                rect(138, 329, 253, 349, '#EAF2FF', 5)
            if hovered(15, 307, 250, 326):
                rect(15, 307, 250, 326, '#EAF2FF', 5)
            txt(19, 310, f'Estimated pay: PHP {earnings:,.1f}', 10, self.INK, True)
            txt(19, 332, 'History', 10, self.BLUE, True)
            txt(151, 332, 'Edit rate / Settings', 9, self.BLUE, True)
            hit(14, 329, 130, 349, self.open_history)
            hit(138, 329, 253, 349, self.open_settings)
            hit(15, 307, 250, 326, self.open_settings)
            if self.benchmark_alert_text:
                rect(5, 4, 260, 58, '#FFF1D6', 8)
                txt(12, 10, 'TASK TIME REMINDER', 10, '#92400E', True)
                txt(12, 30, self.benchmark_alert_text, 9, '#92400E')
            return

        rect(0,0,390,design_h,self.BG)
        txt(19,16,'◉  Ginger Tracker',18,self.INK,True)
        if self.available_release:
            rect(264, 15, 375, 45, '#BFDBFE' if self._hover_action == self.open_update else '#DBEAFE', 9)
            txt(319, 30, 'Update ' + self.available_release['version'], 10, self.BLUE, True, 'center')
            hit(264, 15, 375, 45, self.open_update)
        # No redundant Settings button: goal settings are accessible via Edit Goal.
        txt(20,53,'●  Synced with Ginger' if synced else
            '●  Task active · local timer' if self.task_running else
            '●  Waiting for a task',10,'#16A34A' if self.task_running else self.MUTED)
        rect(15,77,375,158,self.WHITE,14)
        txt(29,88,'CURRENT TASK',10,self.MUTED,True)
        txt(29,106,task,25,self.INK,True)
        if not compact:
            txt(29,139,'Ginger elapsed time when available',8,self.BLUE)

        tiles=[('Completed today',str(count)),('Today worked',fmt(seconds)),
               ('Tasks / hour',rate),('Average task',avg)]
        for i,(name,value) in enumerate(tiles):
            col=i%2; row=i//2
            x=15+col*185; y=168+row*66
            rect(x,y,x+175,y+59,self.WHITE,11)
            txt(x+11,y+8,name,10,self.MUTED)
            txt(x+11,y+26,value,17,self.INK,True)

        if not compact:
            rect(15,319,375,467,self.WHITE,14)
            txt(29,332,'Daily goals',13,self.INK,True)
            rect(274,327,361,357,'#BFDBFE' if hovered(270,324,366,362) else '#EAF2FF',9)
            txt(317,342,'Edit goal',10,self.BLUE,True,'center')
            hit(270,324,366,362,self.open_settings)
            for y,label,caption,ratio in [
                (373,'Work hours',f'{seconds/3600:.1f} / {gh:g} h',seconds/(gh*3600)),
                (419,'Completed tasks',f'{count} / {gt}',count/gt)]:
                txt(29,y,label,10,self.INK,True)
                txt(360,y,caption,10,self.BLUE,True,'ne')
                rect(29,y+21,361,y+29,self.LINE,4)
                rect(29,y+21,29+332*max(0,min(1,ratio)),y+29,self.BLUE,4)
            rect(15,478,375,520,self.WHITE,10)
            if hovered(23,484,366,515):
                rect(23, 484, 366, 515, '#EAF2FF', 7)
            txt(29,489,'View activity history',12,self.BLUE,True)
            txt(357,487,'❯',19,self.BLUE,True,'ne')
            hit(23,484,366,515,self.open_history)
            if hovered(29, 526, 360, 542):
                rect(27, 524, 363, 545, '#EAF2FF', 5)
            txt(29, 526, f'Estimated pay: PHP {earnings:,} @ PHP {hourly_rate:g}/hr', 10, self.BLUE, True)
            hit(29, 526, 360, 542, self.open_settings)
            txt(195,548,f'Lifetime: {total}  ·  {status}',8,self.MUTED,
                anchor='n',max_width=370)

        if self.benchmark_alert_text:
            width = max(215, c.winfo_width())
            c.create_rectangle(5, 4, width - 5, 53, fill='#FFF1D6', outline='#E9A23B', width=2)
            c.create_text(width / 2, 17, text='TASK TIME REMINDER',
                          fill='#92400E', font=('Segoe UI', 10, 'bold'))
            c.create_text(width / 2, 36, text=self.benchmark_alert_text,
                          fill='#92400E', font=('Segoe UI', 9), width=width - 18)

    def check_updates(self):
        try:
            self.update_events.put(('release', latest_release()))
        except Exception as exc:
            self.update_events.put(('check_error', str(exc)))

    def prompt_update_available(self):
        """Show one optional update prompt per launch; never interrupt a running task."""
        if self.stop.is_set() or not self.available_release:
            return
        with self.lock:
            active = self.task_running
        if active:
            # The dashboard still displays an Update button until the user is ready.
            return
        wants_update = messagebox.askyesno(
            'Ginger Tracker — Update Available',
            'Ginger Tracker ' + self.available_release['version'] +
            ' is available.\n\nOpen the update page now?\n\n'
            'Yes = Update Now    No = Remind Me Later',
            parent=self.root)
        if wants_update and self.page == 'dashboard':
            self.open_update()

    def open_update(self):
        if self.page != 'dashboard' or not self.available_release:
            return
        self.show_page('update')
        self.page_header('Software update', lambda: self.show_page('dashboard'))
        area = tk.Frame(self.page_frame, bg=self.BG, padx=20, pady=12)
        area.pack(fill='both', expand=True)
        card = self.card(area, 18)
        self.label(card, 'Ginger Tracker ' + self.available_release['version'], 17,
                   self.INK, True).pack(anchor='w', pady=(0, 8))
        self.label(card, 'New version available. Your saved tasks and goals stay on this PC.',
                   10, self.MUTED, wraplength=320, justify='left').pack(anchor='w', pady=(0, 14))
        notes = self.available_release.get('notes', '') or 'Maintenance update.'
        self.label(card, notes[:350], 10, self.MUTED, wraplength=320,
                   justify='left').pack(anchor='w', pady=(0, 12))
        self.update_message = tk.StringVar(value='Ready to download')
        self.label(card, '', 10, self.BLUE, textvariable=self.update_message,
                   wraplength=310, justify='left').pack(anchor='w', pady=(0, 12))
        self.update_button = tk.Button(card, text='Update Now', command=self.start_update,
                                       bg=self.BLUE, fg='white', relief='flat',
                                       font=('Segoe UI', 11, 'bold'), pady=10)
        self.update_button.pack(fill='x')
        if self.update_busy:
            self.update_button.config(state='disabled')

    def start_update(self):
        if self.update_busy or not self.available_release:
            return
        if not getattr(sys, 'frozen', False):
            self.update_message.set('Use the packaged GingerTracker.exe to install updates.')
            return
        with self.lock:
            active = self.task_running
        if active:
            self.update_message.set('Finish your current Ginger task before updating.')
            return
        self.update_busy = True
        self.update_button.config(state='disabled')
        self.update_message.set('Downloading and verifying update...')
        release = dict(self.available_release)
        threading.Thread(target=self.download_update, args=(release,), daemon=True).start()

    def download_update(self, release):
        staged = APP_DATA / 'GingerTracker.update.exe'
        try:
            # Only trust release files that match the separately published checksum.
            checksum_text = fetch_bytes(release['checksum'], 4096).decode('utf-8-sig').strip()
            match = re.search(r'(?i)\b([a-f0-9]{64})\b', checksum_text)
            if not match:
                raise ValueError('Release checksum is invalid')
            expected = match.group(1).lower()
            data = fetch_bytes(release['exe'], MAX_EXE_BYTES)
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError('SHA-256 mismatch: update was not installed')
            staged.write_bytes(data)
            self.update_events.put(('downloaded', str(staged)))
        except Exception as exc:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
            self.update_events.put(('download_error', str(exc)))

    def process_update_events(self):
        while True:
            try:
                kind, payload = self.update_events.get_nowait()
            except queue.Empty:
                break
            if kind == 'release':
                self.available_release = payload
                self.update_status = 'Update available' if payload else 'Up to date'
                if payload and not self.update_prompted:
                    self.update_prompted = True
                    self.root.after(100, self.prompt_update_available)
            elif kind == 'check_error':
                self.update_status = 'Update check unavailable: ' + payload
            elif kind == 'download_error':
                self.update_busy = False
                if self.page == 'update':
                    self.update_message.set('Download failed: ' + payload)
                    self.update_button.config(state='normal')
            elif kind == 'downloaded':
                with self.lock:
                    active = self.task_running
                if active:
                    self.update_busy = False
                    if self.page == 'update':
                        self.update_message.set('Task started during download. Finish it and retry.')
                        self.update_button.config(state='normal')
                    continue
                try:
                    # Backup data before closing; updater never modifies JSON files.
                    with self.lock:
                        backup_data(force=True)
                        save_data(self.data)
                        shutil.copy2(DATA_FILE, APP_DATA / 'ginger_tracker_v2.preupdate.json')
                    launch_replace_helper(Path(payload))
                    self.close()
                except Exception as exc:
                    self.update_busy = False
                    if self.page == 'update':
                        self.update_message.set('Installation could not start: ' + str(exc))
                        self.update_button.config(state='normal')

    def stop_reminder_alarm(self):
        """Stop future alarm repetitions without affecting Ginger focus."""
        self.alarm_generation += 1
        self.alarm_end_at = 0.0
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    def dismiss_reminder_toast(self, toast=None):
        self.stop_reminder_alarm()
        toast = toast or self.reminder_toast
        if toast is not None:
            try:
                toast.destroy()
            except tk.TclError:
                pass
        if self.reminder_toast is toast:
            self.reminder_toast = None
        self.benchmark_alert_text = ''

    def play_reminder_alarm(self, generation):
        """Play a two-tone 'ton ton' burst, repeating every two seconds."""
        if generation != self.alarm_generation or self.stop.is_set():
            return
        if time.monotonic() >= self.alarm_end_at or self.reminder_toast is None:
            self.stop_reminder_alarm()
            return

        def double_tone():
            try:
                import winsound
                # Two short tones, with a short gap. Worker avoids blocking Tk.
                winsound.Beep(880, 140)
                if generation != self.alarm_generation or self.stop.is_set():
                    return
                time.sleep(0.11)
                if generation != self.alarm_generation or self.stop.is_set():
                    return
                winsound.Beep(880, 140)
            except Exception:
                # If PC beep is unavailable, fall back to a Windows sound.
                try:
                    import winsound
                    winsound.PlaySound('SystemExclamation', winsound.SND_ALIAS | winsound.SND_ASYNC)
                except Exception:
                    pass

        threading.Thread(target=double_tone, daemon=True).start()
        self.root.after(2000, lambda g=generation: self.play_reminder_alarm(g))

    def show_reminder_toast(self, message, play_sound=True):
        """Clean light, borderless CustomTkinter-style card; no focus grab."""
        with self.lock:
            if not self.data['settings'].get('benchmark_enabled', True):
                return
        self.dismiss_reminder_toast()
        toast = tk.Toplevel(self.root)
        self.reminder_toast = toast
        toast.overrideredirect(True)
        # Transparent outer window reveals the CTk card's genuinely rounded corners.
        # Supported on Windows; the fallback retains the previous border color.
        transparent_key = '#FF00FE'
        toast.configure(bg=transparent_key)
        try:
            toast.wm_attributes('-transparentcolor', transparent_key)
        except tk.TclError:
            toast.configure(bg='#F3F6FB')
        toast.attributes('-topmost', True)
        toast.resizable(False, False)
        # Native Win32 no-activate extended style: clicks are still supported.
        try:
            hwnd = toast.winfo_id()
            GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = -20, 0x08000000, 0x00000080
            get_style = ctypes.windll.user32.GetWindowLongW
            set_style = ctypes.windll.user32.SetWindowLongW
            set_style(hwnd, GWL_EXSTYLE, get_style(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:
            pass
        # Exact 220x145 outer dimensions, including the transparent 1px inset.
        outer = ctk.CTkFrame(toast, width=218, height=143, corner_radius=18,
                             fg_color='#F3F6FB', bg_color='transparent', border_width=0)
        outer.pack(padx=1, pady=1)
        outer.pack_propagate(False)
        header = ctk.CTkFrame(outer, fg_color='transparent', height=28)
        header.pack(fill='x', padx=11, pady=(9, 0))
        header.pack_propagate(False)
        symbol = ctk.CTkFrame(header, width=27, height=27, corner_radius=9,
                              fg_color='#DBEAFE')
        symbol.pack(side='left', padx=(0, 7))
        symbol.pack_propagate(False)
        ctk.CTkLabel(symbol, text='◷', font=('Segoe UI Symbol', 16),
                     text_color='#2563EB').pack(expand=True)
        heading = ctk.CTkFrame(header, fg_color='transparent')
        heading.pack(side='left', fill='x', expand=True)
        ctk.CTkLabel(heading, text='Time reminder', height=15,
                     font=('Segoe UI', 11, 'bold'), text_color='#172033',
                     anchor='w').pack(fill='x')
        ctk.CTkLabel(heading, text='GINGER TRACKER', height=11,
                     font=('Segoe UI', 7), text_color='#64748B',
                     anchor='w').pack(fill='x')
        with self.lock:
            seconds = max(0, self.task_seconds)
            limit = max(1, int(self.data['settings'].get('benchmark_minutes', 15)))
        ctk.CTkLabel(outer, text=fmt(seconds), height=25,
                     font=('Segoe UI', 21, 'bold'), text_color='#172033',
                     anchor='w').pack(fill='x', padx=12, pady=(3, 0))
        short_message = (message if len(message) <= 43 else message[:40] + '...')
        ctk.CTkLabel(outer, text=short_message, height=15,
                     font=('Segoe UI', 8), text_color='#64748B',
                     anchor='w').pack(fill='x', padx=12, pady=(0, 2))
        progress = ctk.CTkProgressBar(outer, height=4, corner_radius=4,
                                      fg_color='#DFE7F2', progress_color='#2563EB')
        progress.pack(fill='x', padx=12)
        progress.set(min(1.0, seconds / (limit * 60)))
        ctk.CTkButton(outer, text='Stop alarm / Dismiss', height=25, corner_radius=8,
                      fg_color='#2563EB', hover_color='#1D4ED8',
                      font=('Segoe UI', 9, 'bold'),
                      command=lambda t=toast: self.dismiss_reminder_toast(t)).pack(
                          fill='x', padx=12, pady=(5, 6))
        toast.update_idletasks()
        sw, sh = toast.winfo_screenwidth(), toast.winfo_screenheight()
        toast.geometry(f'220x145+{max(0, sw-245)}+{max(0, sh-230)}')
        if play_sound:
            self.alarm_end_at = time.monotonic() + 60  # 60-second safety timeout
            generation = self.alarm_generation
            self.play_reminder_alarm(generation)

    def process_benchmark_alerts(self):
        while True:
            try:
                message, play_sound = self.benchmark_alert_queue.get_nowait()
            except queue.Empty:
                break
            with self.lock:
                enabled = bool(self.data['settings'].get('benchmark_enabled', True))
            if not enabled:
                continue
            self.benchmark_alert_text = message
            self.benchmark_alert_until = time.monotonic() + 12
            self.show_reminder_toast(message, play_sound)
        if self.benchmark_alert_text and time.monotonic() > self.benchmark_alert_until:
            self.benchmark_alert_text = ''

    def refresh(self):
        self.process_update_events()
        self.process_benchmark_alerts()
        if self.page == 'dashboard':
            self.draw_dashboard()
        if not self.stop.is_set():
            self.root.after(400,self.refresh)

    def show_page(self, page):
        if getattr(self, '_modern_history', None) is not None:
            self._modern_history.destroy()
            self._modern_history = None
            self._modern_top = None
            self._modern_body = None
        # Keep the dashboard's actual window dimensions across every page.
        if getattr(self, '_page_shell', None) is not None:
            self._page_shell.destroy()
            self._page_shell = None
            self.page_frame = None
        self.page = page
        if page == 'dashboard':
            self.dashboard_canvas.pack(fill='both', expand=True)
            self.draw_dashboard()
            return
        self.dashboard_canvas.pack_forget()
        self._page_shell = tk.Frame(self.root, bg=self.BG)
        self._page_shell.pack(fill='both', expand=True)
        # The header is separate from the scrolling form; this prevents
        # clipped/overlapping Back and title text at portrait widths.
        self._page_header = tk.Frame(self._page_shell, bg=self.BG)
        self._page_header.pack(side='top', fill='x')
        body = tk.Frame(self._page_shell, bg=self.BG)
        body.pack(side='top', fill='both', expand=True)
        scroller = tk.Canvas(body, bg=self.BG, highlightthickness=0, bd=0)
        bar = tk.Scrollbar(body, orient='vertical', command=scroller.yview, width=10)
        scroller.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        scroller.pack(side='left', fill='both', expand=True)
        self.page_frame = tk.Frame(scroller, bg=self.BG)
        window_id = scroller.create_window((0, 0), window=self.page_frame, anchor='nw')
        self.page_frame.bind('<Configure>', lambda e: scroller.configure(scrollregion=scroller.bbox('all')))
        def resize(e):
            scroller.itemconfigure(window_id, width=e.width)
            self.root.after_idle(self.scale_page)
        scroller.bind('<Configure>', resize)
        def wheel(e):
            if self.page != 'dashboard':
                scroller.yview_scroll(-1 if e.delta > 0 else 1, 'units')
        scroller.bind('<Enter>', lambda e: scroller.bind_all('<MouseWheel>', wheel))
        scroller.bind('<Leave>', lambda e: scroller.unbind_all('<MouseWheel>'))
        self._page_scroller = scroller

    def scale_page(self):
        """Adapt every subpage to portrait widths without horizontal clipping."""
        if self.page == 'dashboard' or self.page_frame is None:
            return
        width = max(1, self._page_scroller.winfo_width())
        if width < 20:
            return
        compact = width < 275
        factor = max(.77, min(1.12, width / 350.0))
        import tkinter.font as tkfont
        def visit(widget):
            if isinstance(widget, (tk.Label, tk.Button, tk.Entry, tk.OptionMenu)):
                try:
                    if not hasattr(widget, '_base_font'):
                        widget._base_font = widget.cget('font')
                    base = tkfont.Font(font=widget._base_font)
                    base.configure(size=max(8, round(abs(base.cget('size')) * factor)))
                    widget.configure(font=base)
                    widget._scaled_font = base
                    if isinstance(widget, tk.Label):
                        # Wrap all labels, not just the few that originally had
                        # a wraplength. A fixed-size label must never widen a card.
                        widget.configure(wraplength=max(90, width - (38 if compact else 54)),
                                         justify='left')
                    if isinstance(widget, tk.Entry):
                        widget.configure(width=1)
                except (tk.TclError, ValueError):
                    pass
            for child in widget.winfo_children():
                visit(child)
        visit(self.page_frame)
        # Padding must shrink too; changing fonts alone isn't responsive.
        for child in self.page_frame.winfo_children():
            if isinstance(child, tk.Frame):
                try:
                    child.configure(padx=5 if compact else 10)
                except tk.TclError:
                    pass
        self._page_scroller.configure(scrollregion=self._page_scroller.bbox('all'))

    def page_header(self, title, back):
        for child in self._page_header.winfo_children():
            child.destroy()
        width = self.root.winfo_width()
        compact = width < 300
        bar = tk.Frame(self._page_header, bg=self.BG, padx=5 if compact else 12, pady=7)
        bar.pack(fill='x')
        back_btn = self.button(bar, '❮' if compact else '❮ Back', back, True)
        back_btn.configure(padx=6 if compact else 9, pady=5,
                           font=('Segoe UI', 9, 'bold'))
        back_btn.pack(side='left')
        short_title = ('Settings' if 'setting' in title.lower() or 'goal' in title.lower()
                       else 'History' if 'history' in title.lower()
                       else 'Day details' if 'day' in title.lower() else title)
        heading = self.label(bar, short_title if compact else title,
                             10 if compact else 12, self.INK, True)
        heading.pack(side='left', padx=5 if compact else 9, fill='x', expand=True)
        # Recalculate header on window resize without creating extra widgets.
        def update_header(event):
            narrow = event.width < 300
            back_btn.configure(text='❮' if narrow else '❮ Back',
                               padx=6 if narrow else 9)
            heading.configure(text=short_title if narrow else title,
                              font=('Segoe UI', 10 if narrow else 12, 'bold'),
                              wraplength=max(70, event.width - (65 if narrow else 110)))
        self._page_header.bind('<Configure>', update_header)

    def open_settings(self):
        """Modern rounded Settings UI; preserves the existing settings schema."""
        if self.page != 'dashboard':
            return
        self.show_page('dashboard')
        self.page = 'goals'
        self.dashboard_canvas.pack_forget()
        ctk.set_appearance_mode('light')
        self._modern_history = ctk.CTkFrame(self.root, fg_color=self.BG, corner_radius=0)
        self._modern_history.pack(fill='both', expand=True)
        self._modern_top = ctk.CTkFrame(self._modern_history, fg_color='transparent')
        self._modern_top.pack(fill='x', padx=9, pady=(9, 5))
        self._modern_body = ctk.CTkScrollableFrame(
            self._modern_history, fg_color='transparent', corner_radius=0,
            scrollbar_button_color='#CDD8EA', scrollbar_button_hover_color='#9FB3D2')
        self._modern_body.pack(fill='both', expand=True, padx=(7, 3), pady=(0, 8))
        self._modern_header('Settings', lambda: self.show_page('dashboard'))
        self._modern_text(self._modern_body, 'Customize your tracker', 12, self.INK, True).pack(
            anchor='w', padx=8, pady=(6, 7))

        with self.lock:
            current = dict(self.data['settings'])
        hours_var = tk.StringVar(value=str(current.get('goal_hours', 8)))
        tasks_var = tk.StringVar(value=str(current.get('goal_tasks', 30)))
        rate_var = tk.StringVar(value=str(current.get('hourly_rate', 100)))
        mode_var = tk.StringVar(value=current.get('detection_mode', 'Safe'))
        benchmark_var = tk.StringVar(value=str(current.get('benchmark_minutes', 15)) if int(current.get('benchmark_minutes', 15)) in (15, 20) else 'Custom')
        warning_var = tk.StringVar(value=str(current.get('benchmark_warning_minutes', 1)))
        sound_var = tk.BooleanVar(value=bool(current.get('benchmark_sound', True)))
        enabled_var = tk.BooleanVar(value=bool(current.get('benchmark_enabled', True)))

        def field(title, var, hint):
            card = self._modern_card(pad=11)
            self._modern_text(card, title, 10, self.MUTED).pack(anchor='w', pady=(0, 5))
            entry = ctk.CTkEntry(card, textvariable=var, height=34, corner_radius=9,
                                 border_width=1, border_color='#DCE5F2',
                                 fg_color='#F8FAFE', text_color=self.INK,
                                 font=('Segoe UI', 12))
            entry.pack(fill='x')
            self._modern_text(card, hint, 9, self.MUTED).pack(anchor='w', pady=(5, 0))
            return entry

        field('Work hours per day', hours_var, 'Your daily time goal')
        field('Completed tasks per day', tasks_var, 'Your daily completed-task target')
        field('Estimated hourly rate (PHP)', rate_var, 'For unofficial earnings estimates')

        card = self._modern_card(pad=11)
        self._modern_text(card, 'Detection speed', 10, self.MUTED).pack(anchor='w', pady=(0, 6))
        ctk.CTkOptionMenu(card, variable=mode_var, values=list(DETECTION_MODES.keys()),
                          height=34, corner_radius=9, fg_color='#E8F0FF',
                          button_color='#D4E3FF', button_hover_color='#C5D9FC',
                          text_color=self.INK, dropdown_fg_color='#FFFFFF',
                          dropdown_text_color=self.INK,
                          font=('Segoe UI', 11)).pack(fill='x')
        self._modern_text(card, 'Safe uses the proven V5.9.4 timings. Faster modes are experimental.',
                          9, self.MUTED, wraplength=210).pack(anchor='w', pady=(7, 0))

        benchmark_card = self._modern_card(pad=11)
        self._modern_text(benchmark_card, 'Task time reminder', 11, self.INK, True).pack(anchor='w', pady=(0, 5))
        self._modern_text(benchmark_card, 'Re-alerts when you adjust the time, even if the new threshold has passed.',
                          9, self.MUTED, wraplength=210).pack(anchor='w', pady=(0, 7))
        def toggle_alerts():
            # Turning reminders off immediately stops a sounding alarm and
            # removes an existing toast; no Ginger detector settings change.
            if not enabled_var.get():
                self.dismiss_reminder_toast()
            with self.lock:
                self.data['settings']['benchmark_enabled'] = bool(enabled_var.get())
                save_data(self.data)
        ctk.CTkSwitch(benchmark_card, text='Enable alerts', variable=enabled_var,
                      command=toggle_alerts, text_color=self.INK,
                      font=('Segoe UI', 10), progress_color=self.BLUE).pack(
                          anchor='w', pady=(3, 8))
        self._modern_text(benchmark_card, 'Time limit (minutes)', 10, self.MUTED).pack(anchor='w')
        ctk.CTkOptionMenu(benchmark_card, variable=benchmark_var,
                          values=['15', '20', 'Custom'], height=32,
                          fg_color='#E8F0FF', button_color='#D4E3FF',
                          text_color=self.INK).pack(fill='x', pady=(3, 5))
        custom_limit_var = tk.StringVar(value=str(current.get('benchmark_minutes', 15)))
        custom_entry = ctk.CTkEntry(benchmark_card, textvariable=custom_limit_var,
                                    placeholder_text='Custom minutes (e.g. 18)', height=32)
        custom_entry.pack(fill='x', pady=(0, 6))
        self._modern_text(benchmark_card, 'Notify this many minutes before limit',
                          9, self.MUTED, wraplength=210).pack(anchor='w')
        ctk.CTkOptionMenu(benchmark_card, variable=warning_var, values=['1', '2', '3'],
                          height=32, fg_color='#E8F0FF', button_color='#D4E3FF',
                          text_color=self.INK).pack(fill='x', pady=(3, 6))
        ctk.CTkCheckBox(benchmark_card, text='Play alert sound', variable=sound_var,
                         text_color=self.INK, font=('Segoe UI', 10)).pack(anchor='w', pady=(3, 2))
        ctk.CTkButton(benchmark_card, text='Test alert + sound now', height=32,
                      command=lambda: self.benchmark_alert_queue.put(
                          ('This is a test reminder.', bool(sound_var.get())))).pack(fill='x', pady=(7, 2))

        msg = self._modern_text(self._modern_body, '', 9, '#DC2626', wraplength=210)
        msg.pack(anchor='w', padx=8, pady=(4, 0))

        def save_goals():
            try:
                h, t, r = float(hours_var.get()), int(tasks_var.get()), float(rate_var.get())
                limit = int(custom_limit_var.get()) if benchmark_var.get() == 'Custom' else int(benchmark_var.get())
                lead = int(warning_var.get())
                if not (math.isfinite(h) and 0 < h <= 24 and 0 < t <= 100000 and
                        math.isfinite(r) and 0 <= r <= 100000 and mode_var.get() in DETECTION_MODES and
                        1 <= limit <= 240 and 0 < lead < limit):
                    raise ValueError
            except ValueError:
                msg.configure(text='Enter valid goals and an hourly rate from 0 to 100000.')
                return
            with self.lock:
                self.data['settings']['goal_hours'] = h
                self.data['settings']['goal_tasks'] = t
                self.data['settings']['hourly_rate'] = r
                self.data['settings']['detection_mode'] = mode_var.get()
                self.data['settings']['benchmark_minutes'] = limit
                self.data['settings']['benchmark_warning_minutes'] = lead
                self.data['settings']['benchmark_sound'] = bool(sound_var.get())
                self.data['settings']['benchmark_enabled'] = bool(enabled_var.get())
                save_data(self.data)
            self.show_page('dashboard')

        ctk.CTkButton(self._modern_body, text='Save settings & hourly rate',
                      command=save_goals, height=38, corner_radius=10,
                      fg_color=self.BLUE, hover_color='#1D4ED8',
                      font=('Segoe UI', 11, 'bold')).pack(fill='x', padx=(1, 4), pady=(9, 12))

        disclaimer = self._modern_card(pad=11)
        self._modern_text(disclaimer, 'About & disclaimer', 11, self.INK, True).pack(
            anchor='w', pady=(0, 7))
        notice = (
            'Ginger Tracker is an independently developed, unofficial productivity '
            'and motivation tool. It is not owned, operated, sponsored, endorsed, '
            'or affiliated with Ginger or its parent company.\n\n'
            'Task counts, work time, hourly rates, productivity statistics and '
            'estimated earnings are for personal reference only. They may differ '
            "from Ginger's official tracking, evaluation, and payment calculations. "
            'No earnings or payment amounts are guaranteed.\n\n'
            "For accurate work records and compensation, refer to Ginger's "
            'official platform and policies.'
        )
        self._modern_text(disclaimer, notice, 9, self.MUTED,
                          wraplength=205).pack(anchor='w', fill='x')

    def open_history(self):
        if self.page != 'dashboard':
            return
        self.show_page('history')
        self.page_header('Activity history', lambda: self.show_page('dashboard'))
        self.history_content = tk.Frame(self.page_frame, bg=self.BG, padx=8, pady=5)
        self.history_content.pack(fill='both', expand=True)
        self.history_canvas = self._page_scroller
        self.show_history_list()
        self.root.after_idle(self.scale_page)

    def clear_history(self):
        for child in self.history_content.winfo_children():
            child.destroy()
        self.history_canvas.yview_moveto(0)

    def show_history_list(self):
        self.clear_history()
        # Replace only the header's back action when viewing a specific day.
        self.page_header('Activity history', lambda: self.show_page('dashboard'))
        self.label(self.history_content, 'Previous workdays', 10, self.MUTED).pack(anchor='w', pady=(3, 9))
        with self.lock:
            days = [(key, dict(value)) for key, value in self.data['days'].items()]
        days.sort(key=lambda pair: pair[0], reverse=True)
        if not days:
            self.label(self.history_content, 'No recorded days yet.', 11, self.MUTED).pack(pady=20)
        for key, day in days:
            try:
                date_title = datetime.strptime(key, '%Y-%m-%d').strftime('%A, %b %d, %Y')
            except ValueError:
                date_title = key
            c = self.card(self.history_content, 13)
            head = tk.Frame(c, bg=self.WHITE)
            head.pack(fill='x')
            self.button(head, '❯', lambda k=key: self.show_history_day(k), True).pack(side='right')
            self.label(head, date_title, 11, self.INK, True,
                       wraplength=130, justify='left').pack(side='left', fill='x', expand=True)
            seconds = float(day.get('seconds', 0))
            count = int(day.get('count', 0))
            rate = count * 3600 / seconds if seconds else 0
            self.label(c, f'{count} tasks  ·  {fmt(seconds)}  ·  {rate:.2f} tasks/hr', 10, self.MUTED).pack(anchor='w', pady=(4, 2))

        self.root.after_idle(self.scale_page)

    def set_task_difficulty(self, key, index, difficulty, earnings_label, hourly_rate):
        """Persist a reviewed selection without rebuilding the scrolling page."""
        if difficulty != 'Auto' and difficulty not in DIFFICULTIES:
            return
        with self.lock:
            day = self.data['days'].get(key)
            history = day.get('history', []) if isinstance(day, dict) else []
            if not isinstance(history, list) or not (0 <= index < len(history)):
                return
            if not isinstance(history[index], dict):
                return
            history[index]['difficulty'] = difficulty
            history[index]['difficulty_reviewed'] = (difficulty != 'Auto')
            # A save failure must not silently be mistaken for a saved selection.
            try:
                save_data(self.data)
            except OSError as exc:
                history[index]['difficulty_reviewed'] = False
                self.status_text = f'Could not save difficulty: {exc}'
                return
            total = estimated_day_pay(day, hourly_rate)
        earnings_label.configure(text=f'PHP {total:,.2f}')

    def task_difficulty_selector(self, parent, key, index, item, hourly_rate, earnings_label):
        """Compact inline difficulty selector matching the original portrait History UI."""
        duration = max(0.0, float(item.get('duration_seconds', 0) or 0))
        head = tk.Frame(parent, bg=self.WHITE)
        head.pack(fill='x', pady=(7, 2))
        self.label(head, 'Map difficulty', 9, self.MUTED).pack(side='left')
        self.label(head, 'Est. earnings', 9, self.MUTED).pack(side='right')

        row = tk.Frame(parent, bg=self.WHITE)
        row.pack(fill='x')
        pay = self.label(row, '', 10, self.BLUE, True)
        pay.pack(side='right', padx=(5, 0))
        selector = tk.Button(
            row, relief='solid', bd=1, highlightthickness=0,
            cursor='hand2', bg='#F8FAFF', activebackground='#EAF1FF',
            fg=self.INK, anchor='w', padx=7, pady=4,
            font=('Segoe UI', 9))
        selector.pack(side='left', fill='x', expand=True)

        detail = tk.Frame(parent, bg=self.WHITE)
        note = self.label(parent, '', 8, self.MUTED)
        note.pack(anchor='w', pady=(4, 0))

        def update():
            level = task_difficulty(item)
            color, _description, factor = DIFFICULTIES[level]
            display = item.get('difficulty', 'Auto')
            if display not in DIFFICULTIES and display != 'Auto':
                display = 'Auto'
            selector.configure(
                text=('✦  Auto' if display == 'Auto' else '●  ' + display)
                     + ('  ▴' if detail.winfo_manager() else '  ▾'),
                fg=self.BLUE if display == 'Auto' else color)
            pay.configure(text=f'PHP {duration * hourly_rate / 3600 * factor:,.2f}')
            note.configure(
                text='Manually reviewed' if item.get('difficulty_reviewed')
                else f'Auto suggestion: {level} · unverified')

        def choose(level):
            self.set_task_difficulty(key, index, level, earnings_label, hourly_rate)
            with self.lock:
                saved = self.data['days'][key]['history'][index]
                item.update(saved)
            detail.pack_forget()
            update()
            self.root.after_idle(self.scale_page)

        def add_option(name, color, description):
            # A compact, subtle card with only the colored dot accented.
            outer = tk.Frame(detail, bg='#E0E6EF', padx=1, pady=1)
            outer.pack(fill='x', pady=2)
            inner = tk.Frame(outer, bg='#FFFFFF', padx=7, pady=4)
            inner.pack(fill='both', expand=True)
            dot = tk.Label(inner, text='✦' if name == 'Auto' else '●',
                           fg=color, bg='#FFFFFF', font=('Segoe UI', 9))
            dot.pack(side='left', anchor='n', padx=(0, 5))
            content = tk.Frame(inner, bg='#FFFFFF')
            content.pack(side='left', fill='x', expand=True)
            title = tk.Label(content, text=name, fg=self.INK, bg='#FFFFFF',
                             anchor='w', font=('Segoe UI', 9, 'bold'))
            title.pack(anchor='w')
            subtitle = tk.Label(content, text=description, fg=self.MUTED,
                                bg='#FFFFFF', anchor='w', justify='left',
                                wraplength=170, font=('Segoe UI', 8))
            subtitle.pack(anchor='w')
            widgets = (outer, inner, dot, content, title, subtitle)
            for widget in widgets:
                widget.bind('<Button-1>', lambda _event, value=name: choose(value))
                widget.configure(cursor='hand2')

        add_option('Auto', self.BLUE, 'Automatic, unverified suggestion')
        for level, (color, description, _factor) in DIFFICULTIES.items():
            add_option(level, color, description)

        def toggle():
            if detail.winfo_manager():
                detail.pack_forget()
            else:
                detail.pack(fill='x', pady=(4, 0), before=note)
            update()
            self.root.after_idle(self.scale_page)

        selector.configure(command=toggle)
        update()

    def show_history_day(self, key):
        self.clear_history()
        self.page_header('Day details', self.show_history_list)
        with self.lock:
            day = dict(self.data['days'].get(key, default_day()))
            history = list(day.get('history', []))
        self.label(self.history_content, key, 12, self.MUTED).pack(anchor='w', pady=(2, 10))
        count = int(day.get('count', 0))
        seconds = float(day.get('seconds', 0))
        timed = int(day.get('timed_count', 0))
        completed = float(day.get('completed_seconds', 0))
        rate = count * 3600 / seconds if seconds else None
        avg = completed / timed if timed else None
        for title, value in [('Completed tasks', str(count)), ('Tracked work time', fmt(seconds)),
                             ('Average completed task', fmt(avg) if avg is not None else '—'),
                             ('Tasks per hour', f'{rate:.2f}' if rate is not None else '—')]:
            c = self.card(self.history_content, 11)
            self.label(c, title, 9, self.MUTED).pack(anchor='w')
            self.label(c, value, 16, self.INK, True).pack(anchor='w', pady=(3, 0))
        with self.lock:
            hr = safe_rate(self.data['settings'].get('hourly_rate', 100))
        paycard = self.card(self.history_content, 11)
        self.label(paycard, 'Estimated earnings (difficulty-adjusted model)', 9, self.MUTED).pack(anchor='w')
        earnings_label = self.label(paycard, f'PHP {estimated_day_pay(day, hr):,.2f}', 16, self.BLUE, True)
        earnings_label.pack(anchor='w')
        self.label(paycard, 'Unofficial estimate; actual pay may differ.', 8, self.MUTED).pack(anchor='w', pady=(3, 0))
        self.label(self.history_content, 'Individual completed tasks', 12, self.INK, True).pack(anchor='w', pady=(14, 6))
        if not history:
            self.label(self.history_content, 'No individual task records for this day.', 10, self.MUTED).pack(anchor='w')
        for i, item in enumerate(history, 1):
            c = self.card(self.history_content, 10)
            row = tk.Frame(c, bg=self.WHITE)
            row.pack(fill='x')
            self.label(row, f'Task #{i}', 10, self.INK, True).pack(side='left')
            self.label(row, fmt(float(item.get('duration_seconds', 0))), 11, self.BLUE, True).pack(side='right')
            at = str(item.get('at', ''))
            try:
                at = datetime.fromisoformat(at).strftime('%I:%M:%S %p')
            except ValueError:
                at = 'Completion time unavailable'
            self.label(c, at, 9, self.MUTED).pack(anchor='w', pady=(3, 0))
            self.task_difficulty_selector(c, key, i - 1, item, hr, earnings_label)
        self.root.after_idle(self.scale_page)

    # Rounded, modern History presentation. Detection/dashboard logic remains unchanged.
    def open_history(self):
        if self.page != 'dashboard':
            return
        self.show_page('dashboard')
        self.page = 'history'
        self.dashboard_canvas.pack_forget()
        ctk.set_appearance_mode('light')
        self._modern_history = ctk.CTkFrame(self.root, fg_color=self.BG, corner_radius=0)
        self._modern_history.pack(fill='both', expand=True)
        self._modern_top = ctk.CTkFrame(self._modern_history, fg_color='transparent')
        self._modern_top.pack(fill='x', padx=9, pady=(9, 5))
        self._modern_body = ctk.CTkScrollableFrame(
            self._modern_history, fg_color='transparent', corner_radius=0,
            scrollbar_button_color='#CDD8EA', scrollbar_button_hover_color='#9FB3D2')
        self._modern_body.pack(fill='both', expand=True, padx=(7, 3), pady=(0, 8))
        self.show_history_list()

    def _modern_clear(self):
        for child in self._modern_top.winfo_children():
            child.destroy()
        for child in self._modern_body.winfo_children():
            child.destroy()

    def _modern_header(self, title, back):
        ctk.CTkButton(self._modern_top, text='‹', width=30, height=30,
                      corner_radius=9, fg_color='#E8F0FF', hover_color='#D7E6FF',
                      text_color=self.BLUE, font=('Segoe UI', 22),
                      command=back).pack(side='left')
        ctk.CTkLabel(self._modern_top, text=title, text_color=self.INK,
                     font=('Segoe UI', 13, 'bold')).pack(side='left', padx=11)

    def _modern_card(self, parent=None, pad=12):
        card = ctk.CTkFrame(parent or self._modern_body, fg_color=self.WHITE,
                            corner_radius=13, border_width=1,
                            border_color='#E7EDF5')
        card.pack(fill='x', pady=5, padx=(1, 4))
        inside = ctk.CTkFrame(card, fg_color='transparent')
        inside.pack(fill='x', padx=pad, pady=pad)
        return inside

    def _modern_text(self, parent, text, size=11, color=None, bold=False, **kw):
        return ctk.CTkLabel(parent, text=text, text_color=color or self.INK,
                            font=('Segoe UI', size, 'bold' if bold else 'normal'),
                            anchor='w', justify='left', **kw)

    def show_history_list(self):
        self._modern_clear()
        self._modern_header('History', lambda: self.show_page('dashboard'))
        self._modern_text(self._modern_body, 'Previous workdays', 11, self.MUTED).pack(
            anchor='w', padx=7, pady=(5, 7))
        with self.lock:
            days = [(key, dict(value)) for key, value in self.data['days'].items()]
        days.sort(key=lambda pair: pair[0], reverse=True)
        if not days:
            self._modern_text(self._modern_body, 'No recorded days yet.', 11, self.MUTED).pack(pady=15)
        for key, day in days:
            try:
                heading = datetime.strptime(key, '%Y-%m-%d').strftime('%a, %b %d, %Y')
            except ValueError:
                heading = key
            card = self._modern_card()
            button = ctk.CTkButton(
                card, text=heading + '   ›', anchor='w', fg_color='transparent',
                hover_color='#F2F6FE', text_color=self.INK, height=31,
                font=('Segoe UI', 11, 'bold'), command=lambda k=key: self.show_history_day(k))
            button.pack(fill='x')
            seconds = float(day.get('seconds', 0))
            count = int(day.get('count', 0))
            speed = count * 3600 / seconds if seconds else 0
            self._modern_text(card, f'{count} tasks · {fmt(seconds)} · {speed:.2f} tasks/hr',
                              9, self.MUTED).pack(anchor='w', padx=5)

    def show_history_day(self, key):
        self._modern_clear()
        self._modern_header('Day details', self.show_history_list)
        with self.lock:
            day = dict(self.data['days'].get(key, default_day()))
            history = [dict(v) if isinstance(v, dict) else {} for v in day.get('history', [])]
            hourly_rate = safe_rate(self.data['settings'].get('hourly_rate', 100))
        self._modern_text(self._modern_body, key, 10, self.MUTED).pack(
            anchor='w', padx=8, pady=(5, 7))
        count = int(day.get('count', 0))
        seconds = float(day.get('seconds', 0))
        timed = int(day.get('timed_count', 0))
        completed = float(day.get('completed_seconds', 0))
        metrics = [
            ('Completed tasks', str(count)),
            ('Tracked work time', fmt(seconds)),
            ('Average completed task', fmt(completed / timed) if timed else '—'),
            ('Tasks per hour', f'{count * 3600 / seconds:.2f}' if seconds else '—')
        ]
        for title, value in metrics:
            card = self._modern_card(pad=10)
            self._modern_text(card, title, 10, self.MUTED).pack(anchor='w')
            self._modern_text(card, value, 17, self.INK, True).pack(anchor='w')
        pay_card = self._modern_card()
        self._modern_text(pay_card, 'Estimated earnings', 10, self.MUTED).pack(anchor='w')
        total_label = self._modern_text(
            pay_card, f'PHP {estimated_day_pay(day, hourly_rate):,.2f}',
            19, self.BLUE, True)
        total_label.pack(anchor='w')
        self._modern_text(pay_card, 'Unofficial difficulty-adjusted estimate',
                          9, self.MUTED).pack(anchor='w')
        self._modern_text(self._modern_body, 'Individual completed tasks',
                          12, self.INK, True).pack(anchor='w', padx=8, pady=(13, 5))
        if not history:
            self._modern_text(self._modern_body, 'No individual task records.', 10, self.MUTED).pack(padx=8)
        for index, item in enumerate(history):
            card = self._modern_card(pad=10)
            top = ctk.CTkFrame(card, fg_color='transparent')
            top.pack(fill='x')
            self._modern_text(top, f'Task #{index + 1}', 11, self.INK, True).pack(side='left')
            duration = max(0, float(item.get('duration_seconds', 0) or 0))
            self._modern_text(top, fmt(duration), 10, self.BLUE, True).pack(side='right')
            at = str(item.get('at', ''))
            try:
                at = datetime.fromisoformat(at).strftime('%I:%M:%S %p')
            except ValueError:
                at = 'Completion time unavailable'
            self._modern_text(card, at, 9, self.MUTED).pack(anchor='w', pady=(0, 5))
            line = ctk.CTkFrame(card, fg_color='#EDF1F7', height=1)
            line.pack(fill='x', pady=(2, 7))
            titles = ctk.CTkFrame(card, fg_color='transparent')
            titles.pack(fill='x')
            self._modern_text(titles, 'Map difficulty', 9, self.MUTED).pack(side='left')
            self._modern_text(titles, 'Est. earnings', 9, self.MUTED).pack(side='right')
            row = ctk.CTkFrame(card, fg_color='transparent')
            row.pack(fill='x', pady=(4, 0))
            earning = self._modern_text(row, '', 10, self.BLUE, True)
            earning.pack(side='right', padx=(7, 0))
            options = ctk.CTkFrame(card, fg_color='#F8FAFD', corner_radius=10)
            note = self._modern_text(card, '', 9, self.MUTED)
            note.pack(anchor='w', pady=(4, 0))
            choice = ctk.CTkButton(
                row, text='', height=31, corner_radius=9, fg_color='#F4F7FD',
                hover_color='#E8F0FF', text_color=self.INK, anchor='w',
                font=('Segoe UI', 10, 'bold'))
            choice.pack(side='left', fill='x', expand=True)
            def refresh_item(item=item, duration=duration, choice=choice,
                             earning=earning, note=note, options=options):
                level = task_difficulty(item)
                selected = item.get('difficulty', 'Auto')
                if selected not in DIFFICULTIES and selected != 'Auto':
                    selected = 'Auto'
                color = self.BLUE if selected == 'Auto' else DIFFICULTIES[selected][0]
                choice.configure(text=('✦  ' if selected == 'Auto' else '●  ') +
                                 selected + ('  ▴' if options.winfo_manager() else '  ▾'),
                                 text_color=color)
                earning.configure(text=f'PHP {duration * hourly_rate / 3600 * DIFFICULTIES[level][2]:,.2f}')
                note.configure(text=('Manually reviewed' if item.get('difficulty_reviewed')
                                     else f'Auto suggestion: {level} · unverified'))
            def select_level(level, item=item, index=index, options=options,
                             refresh_item=refresh_item):
                self.set_task_difficulty(key, index, level, total_label, hourly_rate)
                with self.lock:
                    saved = self.data['days'][key]['history'][index]
                    item.update(saved)
                options.pack_forget()
                refresh_item()
            for level, description in [('Auto', 'Automatic, unverified suggestion')] + [
                (name, data[1]) for name, data in DIFFICULTIES.items()
            ]:
                color = self.BLUE if level == 'Auto' else DIFFICULTIES[level][0]
                opt = ctk.CTkButton(
                    options, text=('✦  ' if level == 'Auto' else '●  ') + level,
                    height=29, corner_radius=7, anchor='w', fg_color='#FFFFFF',
                    hover_color='#EAF1FF', text_color=color,
                    font=('Segoe UI', 10, 'bold'),
                    command=lambda v=level, fn=select_level: fn(v))
                opt.pack(fill='x', padx=5, pady=(4, 0))
                self._modern_text(options, description, 9, self.MUTED).pack(
                    anchor='w', padx=13, pady=(0, 3))
            def toggle(options=options, note=note, refresh_item=refresh_item):
                if options.winfo_manager():
                    options.pack_forget()
                else:
                    options.pack(fill='x', before=note, pady=(5, 0))
                refresh_item()
            choice.configure(command=toggle)
            refresh_item()

    def duration_debug(self, event, **values):
        """Append diagnostic timing only; never change counting or saved records."""
        try:
            APP_DATA.mkdir(parents=True, exist_ok=True)
            details = ' '.join(f'{k}={v!r}' for k, v in values.items())
            with (APP_DATA / 'duration_debug.log').open('a', encoding='utf-8') as log:
                log.write(f'{datetime.now().isoformat(timespec="milliseconds")} {event} {details}\n')
        except Exception:
            pass

    def monitor(self):
        # State machine: a loaded task -> one or more submission popups ->
        # stable blank workspace. Popup bursts are treated as one session.
        state = 'waiting'
        blank_start = None
        last_popup = None
        saw_popup = False
        last_tick = time.monotonic()
        last_save = last_tick
        last_backup = last_tick
        # Count a task only once per verified load -> completion lifecycle.
        counted_current_task = False
        last_read = 0.0
        latest_ginger = None
        candidate_since = None
        last_completion = None
        previous_wait_elapsed = None
        previous_wait_read_at = None
        last_elapsed_read_at = None
        last_auto_select = 0.0
        last_timer_sample = None
        timer_advancing = False
        last_timer_change_at = None
        # Once a task has synced, do not double-click again during annotation.
        synced_this_task = False
        benchmark_alerted_this_task = False
        benchmark_alert_config = None
        last_task_row_read = 0.0
        task_row = None
        saw_task_row = False
        absent_since = None
        # Auto-download can skip the stable blank workspace entirely.
        # Only a submission followed by a substantial timer reset and two
        # advancing readings is accepted as a completed task.
        submitted_elapsed_peak = None
        task_elapsed_peak = 0.0
        last_duration_debug = 0.0
        reset_candidate = None
        reset_confirmations = 0
        while not self.stop.is_set():
            now = time.monotonic()
            elapsed = min(now - last_tick, 2.0)
            last_tick = now
            with self.lock:
                mode = self.data['settings'].get('detection_mode', 'Safe')
            cfg = DETECTION_MODES.get(mode, DETECTION_MODES['Safe'])
            main, popup = get_windows()
            blank = blank_workspace(main) if main else None
            # Independently of the load-detection state, keep checking Ginger's
            # timer. A visible Task 0 is eligible for retries even before the
            # tracker has recognized the task as loaded.
            read_interval = UNSYNCED_ELAPSED_POLL if not timer_advancing else 0.5
            if main is None:
                latest_ginger = None
                last_elapsed_read_at = None
                last_timer_sample = None
                timer_advancing = False
                last_timer_change_at = None
            elif now - last_read >= read_interval:
                latest_ginger = ginger_elapsed(main)
                last_read = time.monotonic()
                last_elapsed_read_at = now
                if latest_ginger is None:
                    timer_advancing = False
                    last_timer_sample = None
                    last_timer_change_at = None
                else:
                    # A readable but frozen elapsed label is not a running timer.
                    # A countdown (negative seconds) also counts as progression.
                    if last_timer_sample is None or latest_ginger != last_timer_sample:
                        last_timer_change_at = now
                    timer_advancing = (last_timer_change_at is not None and
                                       now - last_timer_change_at < 2.5)
                    last_timer_sample = latest_ginger
                # Retry on missing OR non-advancing timer, regardless of whether
                # the local tracker has entered its 'loaded' state. Never touch
                # Ginger while a submission is in progress or another app is active.
                if (not timer_advancing and not synced_this_task and state not in ('submitted',)
                        and not popup and ginger_is_foreground(main)
                        and now - last_auto_select >= AUTO_SELECT_COOLDOWN):
                    last_auto_select = time.monotonic()
                    # Attempt directly; do not gate selection on a separate
                    # accessibility scan that may disagree with the click scan.
                    if select_ginger_task_zero(main):
                        self.status_text = 'Selecting Task 0 — waiting for timer sync'
                    else:
                        task0_debug('AUTO-SELECT: no clickable Task 0 TreeItem')
            if main is None:
                task_row = None
            elif now - last_task_row_read >= 2.0:
                task_row = task_zero_state(main)
                last_task_row_read = now
            if task_row is True:
                saw_task_row = True
                absent_since = None
            elif task_row is False and saw_task_row and absent_since is None:
                absent_since = now
            with self.lock:
                # Capture the old task duration BEFORE the new Ginger timer can
                # overwrite it. A reset alone is never a completion signal.
                fresh_timer = last_elapsed_read_at == now and latest_ginger is not None
                if state == 'submitted' and fresh_timer and submitted_elapsed_peak is not None:
                    if (not popup and submitted_elapsed_peak >= 15 and
                            0 <= latest_ginger <= 45 and
                            submitted_elapsed_peak - latest_ginger >= 12):
                        if reset_candidate is None or latest_ginger < reset_candidate:
                            reset_candidate = latest_ginger
                            reset_confirmations = 1
                        elif latest_ginger > reset_candidate:
                            reset_confirmations += 1
                            reset_candidate = latest_ginger
                    elif reset_candidate is not None and (popup or latest_ginger > 45):
                        reset_candidate = None
                        reset_confirmations = 0
                auto_transition = (state == 'submitted' and not counted_current_task and
                                   reset_confirmations >= 2 and not popup)
                if auto_transition:
                    self.duration_debug('SAVE_AUTO', saved=submitted_elapsed_peak, peak=task_elapsed_peak,
                                        tracker=self.task_seconds, ginger=latest_ginger, state=state)
                    d = self.day()
                    d['count'] += 1
                    d['timed_count'] += 1
                    d['completed_seconds'] += submitted_elapsed_peak
                    d['history'].append({'at': datetime.now().isoformat(timespec='seconds'),
                                         'duration_seconds': round(submitted_elapsed_peak, 1),
                                         'difficulty': 'Auto', 'difficulty_reviewed': False})
                    # Today's worked time has already accumulated during the task.
                    # Avoid adding the new task's reset time to the old task.
                    state = 'loaded'
                    counted_current_task = False
                    self.task_running = True
                    self.task_seconds = max(0.0, float(latest_ginger))
                    task_elapsed_peak = self.task_seconds
                    self.sync_offset = 0.0
                    self.last_ginger_elapsed = latest_ginger
                    self.sync_active = True
                    synced_this_task = True
                    benchmark_alerted_this_task = False
                    benchmark_alert_config = None
                    last_completion = now
                    saw_popup = False
                    last_popup = None
                    blank_start = None
                    saw_task_row = task_row is True
                    absent_since = None
                    submitted_elapsed_peak = None
                    reset_candidate = None
                    reset_confirmations = 0
                    self.status_text = 'Completed +1 — auto-downloaded next task'
                    save_data(self.data)
                if self.task_running:
                    previous = self.task_seconds
                    if state == 'submitted' and reset_candidate is not None:
                        # Freeze old task duration while verifying the new timer.
                        pass
                    elif latest_ginger is not None:
                        # Ginger is authoritative. Keep manual adjustments as
                        # an offset; don't overwrite them on the next poll.
                        if self.last_ginger_elapsed is None:
                            # Initial sync may discover time spent before the
                            # tracker recognized the task.
                            self.sync_offset = 0.0
                        elif latest_ginger + 5 < self.last_ginger_elapsed:
                            # A reset likely means a new task; avoid subtracting
                            # already-recorded time from today's total.
                            self.sync_offset = self.task_seconds - latest_ginger
                        self.last_ginger_elapsed = latest_ginger
                        self.sync_active = True
                        synced_this_task = True
                        self.task_seconds = max(0.0, latest_ginger + self.sync_offset)
                    elif state != 'submitted' or reset_candidate is None:
                        self.sync_active = False
                        self.task_seconds += elapsed
                    # Retain the highest verified elapsed value for this task.
                    # Ginger can briefly expose a reset/stale timer during submission.
                    if state == 'loaded':
                        task_elapsed_peak = max(task_elapsed_peak, self.task_seconds)
                    elif state == 'submitted' and submitted_elapsed_peak is not None:
                        submitted_elapsed_peak = max(submitted_elapsed_peak, task_elapsed_peak)
                    if now - last_duration_debug >= 20:
                        self.duration_debug('TICK', state=state, tracker=round(self.task_seconds, 1),
                                            peak=round(task_elapsed_peak, 1),
                                            submitted=submitted_elapsed_peak, ginger=latest_ginger,
                                            offset=round(self.sync_offset, 1), popup=popup)
                        last_duration_debug = now
                    self.day()['seconds'] = max(0.0, self.day()['seconds'] + self.task_seconds - previous)
                    # One threshold reminder per verified task, including a task
                    # already running before this tracker was launched.
                    limit = max(1, int(self.data['settings'].get('benchmark_minutes', 15)))
                    lead = max(0, int(self.data['settings'].get('benchmark_warning_minutes', 1)))
                    # Re-arm whenever the saved time/lead setting changes.
                    # Keep one alert per configuration per task; changing the
                    # threshold after an alert permits a fresh immediate alert.
                    alert_config = (limit, lead)
                    if alert_config != benchmark_alert_config:
                        benchmark_alert_config = alert_config
                        benchmark_alerted_this_task = False
                    if (self.data['settings'].get('benchmark_enabled', True) and
                            not benchmark_alerted_this_task and
                            self.task_seconds >= max(0, limit - lead) * 60):
                        benchmark_alerted_this_task = True
                        self.benchmark_alert_queue.put((
                            f'Approaching your {limit}-minute task limit.',
                            bool(self.data['settings'].get('benchmark_sound', True))))
                if popup and state != 'waiting':
                    saw_popup = True
                    last_popup = now
                if main is None:
                    previous_wait_elapsed = None
                    previous_wait_read_at = None
                    self.status_text = 'Ginger not visible/detected — waiting'
                elif state == 'waiting':
                    self.status_text = 'Waiting for a loaded task'
                    # Discord streams can change desktop pixels, but cannot
                    # advance the local Ginger window's own UI timer.
                    # Require Ginger's elapsed control to advance across reads.
                    # A stale elapsed value left after submission cannot pass.
                    # The Ginger UI timer belongs to the real local Ginger window.
                    # Its progression is sufficient proof even while our floating
                    # tracker (or another app) has keyboard focus. Discord video
                    # pixels cannot affect this UI Automation value.
                    ginger_available = bool(main and not user32.IsIconic(main.handle))
                    if last_elapsed_read_at == now and latest_ginger is not None and not popup and ginger_available:
                        if previous_wait_elapsed is not None and latest_ginger > previous_wait_elapsed:
                            cooldown_over = last_completion is None or now - last_completion >= cfg['new_task_cooldown']
                            if cooldown_over:
                                state = 'loaded'
                                synced_this_task = False
                                benchmark_alerted_this_task = False
                                benchmark_alert_config = None
                                counted_current_task = False
                                self.task_running = True
                                self.task_seconds = 0.0
                                task_elapsed_peak = 0.0
                                self.sync_offset = 0.0
                                self.last_ginger_elapsed = None
                                self.sync_active = False
                                saw_popup = False
                                last_popup = None
                                candidate_since = None
                                previous_wait_elapsed = None
                                previous_wait_read_at = None
                                self.duration_debug('TASK_LOADED', ginger=latest_ginger, tracker=self.task_seconds)
                                self.status_text = 'New Ginger task verified — syncing timer'
                                saw_task_row = task_row is True or saw_task_row
                        else:
                            self.status_text = 'Checking Ginger timer for a new task'
                        if state == 'waiting':
                            previous_wait_elapsed = latest_ginger
                            previous_wait_read_at = now
                    elif not ginger_available:
                        previous_wait_elapsed = None
                        previous_wait_read_at = None
                        self.status_text = 'Waiting for Ginger window to become visible'
                    elif last_elapsed_read_at == now and latest_ginger is None:
                        previous_wait_elapsed = None
                        previous_wait_read_at = None
                        self.status_text = 'Waiting for Ginger task timer (Discord ignored)'
                    elif popup:
                        previous_wait_elapsed = None
                        previous_wait_read_at = None
                elif state == 'loaded':
                    self.status_text = 'Task loaded — synced to Ginger' if self.sync_active else 'Task loaded — local timer (Ginger text unavailable)'
                    if popup:
                        state = 'submitted'
                        submitted_elapsed_peak = max(task_elapsed_peak, self.task_seconds, float(latest_ginger or 0))
                        reset_candidate = None
                        reset_confirmations = 0
                        blank_start = None
                        self.duration_debug('SUBMIT_POPUP', tracker=self.task_seconds, peak=task_elapsed_peak, ginger=latest_ginger, captured=submitted_elapsed_peak)
                    elif (saw_task_row and task_row is False and
                          absent_since is not None and now - absent_since >= cfg['absent'] and
                          blank is True):
                        # The task row disappeared and the workspace cleared.
                        # This can happen while the submission popup is missed.
                        state = 'submitted'
                        submitted_elapsed_peak = max(task_elapsed_peak, self.task_seconds, float(latest_ginger or 0))
                        reset_candidate = None
                        reset_confirmations = 0
                        blank_start = now
                        self.duration_debug('SUBMIT_BLANK', tracker=self.task_seconds, peak=task_elapsed_peak, ginger=latest_ginger, captured=submitted_elapsed_peak)
                elif state == 'submitted':
                    # Preserve the last known old-task duration until a verified
                    # auto-download transition or the normal blank confirmation.
                    if submitted_elapsed_peak is None:
                        submitted_elapsed_peak = self.task_seconds
                    if popup and latest_ginger is not None and fresh_timer:
                        submitted_elapsed_peak = max(submitted_elapsed_peak, task_elapsed_peak, self.task_seconds)
                    # Do not reset the monitoring session for every new popup.
                    # The blank state must be stable AND popup activity must be quiet.
                    confirmed_disappearance = (saw_task_row and task_row is False and
                                               absent_since is not None and
                                               now - absent_since >= cfg['confirmed_absent'])
                    if blank is True and (saw_popup or confirmed_disappearance):
                        if blank_start is None:
                            blank_start = now
                        quiet = (last_popup is None or now - last_popup >= cfg['quiet'])
                        if not popup and quiet and now - blank_start >= cfg['stable'] and not counted_current_task:
                            # Submission may take minutes after its first popup.
                            # If Ginger's old timer is no longer readable, use the
                            # continuously running tracker timer at confirmed completion.
                            # Do not apply this to the new-task timer reset path.
                            if latest_ginger is None:
                                submitted_elapsed_peak = max(float(submitted_elapsed_peak or 0), self.task_seconds)
                            self.duration_debug('SAVE_BLANK', tracker=self.task_seconds, peak=task_elapsed_peak, ginger=latest_ginger, captured=submitted_elapsed_peak)
                            counted_current_task = True
                            d = self.day()
                            d['count'] += 1
                            d['timed_count'] += 1
                            d['completed_seconds'] += (submitted_elapsed_peak if submitted_elapsed_peak is not None else self.task_seconds)
                            d['history'].append({'at': datetime.now().isoformat(timespec='seconds'),
                                                 'duration_seconds': round(submitted_elapsed_peak if submitted_elapsed_peak is not None else self.task_seconds, 1),
                                                 'difficulty': 'Auto',
                                                 'difficulty_reviewed': False})
                            self.task_running = False
                            self.task_seconds = 0.0
                            task_elapsed_peak = 0.0
                            self.sync_active = False
                            self.last_ginger_elapsed = None
                            state = 'waiting'
                            synced_this_task = False
                            benchmark_alerted_this_task = False
                            benchmark_alert_config = None
                            last_completion = now
                            candidate_since = None
                            latest_ginger = None
                            previous_wait_elapsed = None
                            previous_wait_read_at = None
                            saw_popup = False
                            saw_task_row = False
                            absent_since = None
                            last_popup = None
                            blank_start = None
                            submitted_elapsed_peak = None
                            reset_candidate = None
                            reset_confirmations = 0
                            self.status_text = 'Completed +1 — waiting for next task'
                            save_data(self.data)
                        else:
                            self.status_text = 'Blank detected — confirming completion'
                    elif blank is False:
                        blank_start = None
                        self.status_text = 'Task/errors visible — waiting for completion'
                    else:
                        blank_start = None
                        self.status_text = 'Cannot see Ginger workspace — keep it visible'
                    # Deliberately do not time out: error correction can take minutes.
                    # Continue watching until the task clears or user restarts.
                if now - last_backup >= BACKUP_INTERVAL:
                    backup_data()
                    last_backup = now
                if now - last_save >= 5:
                    save_data(self.data)
                    last_save = now
            self.stop.wait(cfg['poll'])

    def close(self):
        # Save the dashboard size and position, not the temporary History/Goal page.
        geometry = self.root.geometry()
        self.stop.set()
        self.stop_reminder_alarm()
        with self.lock:
            self.data['settings']['window_geometry'] = geometry
            save_data(self.data)
        self.root.destroy()


if __name__ == '__main__':
    Tracker().root.mainloop()
