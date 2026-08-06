"""Run-at-login (auto-start) for Blueforge One Sync — cross platform.

The app should start syncing automatically every time the computer boots, just
like Dropbox. Each OS has its own supported mechanism; we use the *user-level*
one everywhere so no admin rights are ever required:

  - Windows : a shortcut (.lnk) in the per-user Startup folder.
  - macOS   : a LaunchAgent plist in ~/Library/LaunchAgents (RunAtLoad).
  - Linux   : a .desktop file in ~/.config/autostart.

Everything here is pure Python and side-effect-free until you call
``enable()`` / ``disable()``, so the pathing logic can be unit-tested on any OS
by monkeypatching ``sys.platform`` and ``Path.home``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional


APP_NAME = "Blueforge One Sync"
# Stable identifiers used inside the generated files.
_LAUNCH_LABEL = "com.blueforge.cloudsync"
_LINUX_DESKTOP_NAME = "blueforge-one-sync.desktop"


# ---------------------------------------------------------------------------
# What command should run at login?
# ---------------------------------------------------------------------------
def launch_command() -> List[str]:
    """Return argv that (re)launches THIS app in tray mode.

    When frozen by PyInstaller, ``sys.executable`` is the app binary itself and
    it already boots into the tray, so no extra args are needed. When running
    from source, we relaunch the Python interpreter on this package.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable]
    # From source: python -m bfsync app
    return [sys.executable, "-m", "bfsync", "app"]


def _quote(arg: str) -> str:
    """Quote a single argument for a shell-ish command string."""
    if not arg or any(c in arg for c in ' \t"\\'):
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return arg


def command_string() -> str:
    return " ".join(_quote(a) for a in launch_command())


# ---------------------------------------------------------------------------
# Per-platform target paths
# ---------------------------------------------------------------------------
def _platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _windows_startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else (Path.home() / "AppData" / "Roaming")
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def target_path() -> Path:
    """The file we create/remove to toggle auto-start on this OS."""
    plat = _platform()
    if plat == "windows":
        return _windows_startup_dir() / f"{APP_NAME}.lnk"
    if plat == "macos":
        return Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCH_LABEL}.plist"
    return Path.home() / ".config" / "autostart" / _LINUX_DESKTOP_NAME


# ---------------------------------------------------------------------------
# File contents
# ---------------------------------------------------------------------------
def _macos_plist() -> str:
    args = launch_command()
    program_args = "\n".join(f"      <string>{_xml_escape(a)}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{_LAUNCH_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{program_args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>ProcessType</key>
    <string>Interactive</string>
  </dict>
</plist>
"""


def _linux_desktop() -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        f"Exec={command_string()}\n"
        "X-GNOME-Autostart-enabled=true\n"
        "Terminal=false\n"
        "Comment=Keep a folder in sync with Blueforge One\n"
    )


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# Windows .lnk creation (best effort, no third-party deps)
# ---------------------------------------------------------------------------
def _write_windows_shortcut(path: Path) -> None:
    """Create a Startup shortcut. Uses Windows COM via a tiny PowerShell call so
    we don't need pywin32. Falls back to a .cmd launcher if PowerShell is
    unavailable."""
    argv = launch_command()
    exe = argv[0]
    args = " ".join(_quote(a) for a in argv[1:])
    workdir = str(Path(exe).parent)
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
        f"'{path}');"
        f"$s.TargetPath='{exe}';"
        f"$s.Arguments='{args}';"
        f"$s.WorkingDirectory='{workdir}';"
        "$s.WindowStyle=7;"
        "$s.Save()"
    )
    import subprocess
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return
    except Exception:
        # Fallback: a .cmd that launches the app, dropped next to the .lnk path.
        cmd_path = path.with_suffix(".cmd")
        cmd_path.write_text(f'start "" {command_string()}\r\n', encoding="utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def is_enabled() -> bool:
    tp = target_path()
    if tp.exists():
        return True
    # Windows fallback launcher
    if _platform() == "windows" and tp.with_suffix(".cmd").exists():
        return True
    return False


def enable() -> Path:
    """Turn on run-at-login for the current user. Returns the file written."""
    tp = target_path()
    tp.parent.mkdir(parents=True, exist_ok=True)
    plat = _platform()
    if plat == "windows":
        _write_windows_shortcut(tp)
    elif plat == "macos":
        tp.write_text(_macos_plist(), encoding="utf-8")
        _try_launchctl("load", tp)
    else:
        tp.write_text(_linux_desktop(), encoding="utf-8")
        try:
            os.chmod(tp, 0o755)
        except OSError:
            pass
    return tp


def disable() -> None:
    """Turn off run-at-login for the current user."""
    tp = target_path()
    if _platform() == "macos" and tp.exists():
        _try_launchctl("unload", tp)
    for p in (tp, tp.with_suffix(".cmd")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def _try_launchctl(action: str, plist: Path) -> None:
    import subprocess
    try:
        subprocess.run(["launchctl", action, str(plist)],
                       check=False, capture_output=True)
    except Exception:
        pass


def describe() -> str:
    """Human-readable one-liner about the current state (for the UI/log)."""
    where = target_path()
    state = "ON" if is_enabled() else "off"
    return f"Start at login: {state} ({where})"
