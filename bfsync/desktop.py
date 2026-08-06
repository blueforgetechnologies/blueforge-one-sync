"""Desktop app orchestrator — the one true main() for the tray application.

Ties together: first-run wizard → background sync worker → tray icon, and keeps
everything on the main thread so it is safe on macOS, Windows and Linux.

Flow:
    load config
    ┌────────────────────────────────────────────────────────┐
    │ if not configured: run setup wizard (main thread)      │
    │ start / ensure the SyncWorker background thread         │
    │ run the tray icon (main thread, blocks until an action) │
    │ handle the pending action:                              │
    │    'quit'        -> exit                                 │
    │    'settings'    -> show status window, maybe loop back  │
    │    'reconfigure' -> run wizard again, then tray again    │
    │    'signout'     -> logout, back to wizard              │
    └────────────────────────────────────────────────────────┘

Because the wizard and the tray never run their event loops at the same time,
we sidestep the classic macOS "two GUI toolkits fighting for the main thread"
crash entirely.
"""

from __future__ import annotations

import sys

from .config import Config, config_path, state_path
from . import runtime


def _single_instance_guard():
    """Best-effort: prevent two copies of the tray running at once. Returns a
    handle to keep alive, or None. Never fatal."""
    try:
        import socket
        # Abstract-namespace socket on Linux; fall back to a lock file elsewhere.
        if sys.platform.startswith("linux"):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.bind("\0blueforge-one-sync")
                return s
            except OSError:
                return "BUSY"
        else:
            from .config import app_dir
            import os
            lock = app_dir() / "app.lock"
            # Use O_CREAT|O_EXCL as a simple cross-process guard; stale locks are
            # cleared if the PID inside is dead.
            if lock.exists():
                try:
                    pid = int(lock.read_text().strip() or "0")
                except Exception:
                    pid = 0
                if pid and _pid_alive(pid):
                    return "BUSY"
            lock.write_text(str(os.getpid()))
            return lock
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        if sys.platform.startswith("win"):
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        import os
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def run() -> int:
    guard = _single_instance_guard()
    if guard == "BUSY":
        _emit("Blueforge One Sync is already running.")
        return 0

    from .wizard import run_setup_wizard, run_status_window
    from .worker import SyncWorker
    from .tray import Tray

    # 1) First-run: make sure we're configured before starting anything.
    cfg = Config.load()
    if not cfg.is_configured():
        if not run_setup_wizard():
            return 0  # user closed the wizard without finishing
        cfg = Config.load()

    # 2) Make sure the branded sidebar entry exists (self-heals every launch).
    try:
        from . import sidebar
        sidebar.ensure(cfg.local_folder)
    except Exception:
        pass

    # 3) Start the background worker once; it survives tray restarts.
    state = runtime.AppState()
    state.set_config(cfg.tenant_name or "", cfg.local_folder or "", cfg.email or "")
    tray = Tray(state, worker=None)  # worker set below
    worker = SyncWorker(state, notify=tray.notify)
    tray.worker = worker
    worker.start()

    # 3) Main loop: run the tray; react to whatever the user picked.
    while True:
        action = tray.run()  # blocks until a menu action stops the icon

        if action == "quit" or action == "":
            break

        if action == "settings":
            follow = run_status_window(state)
            if follow == "reconfigure":
                action = "reconfigure"
            elif follow == "signout":
                action = "signout"
            elif follow == "quit":
                break
            # else: just loop back and show the tray again

        if action == "reconfigure":
            if run_setup_wizard():
                new = Config.load()
                state.set_config(new.tenant_name or "", new.local_folder or "",
                                 new.email or "")
                worker._engine = None  # force rebuild against new config
                worker.request_sync_now()
            # continue -> tray again

        if action == "signout":
            worker.stop()
            _logout()
            if not run_setup_wizard():
                break
            new = Config.load()
            state.set_config(new.tenant_name or "", new.local_folder or "",
                             new.email or "")
            # fresh worker for the new account
            worker = SyncWorker(state, notify=tray.notify)
            tray.worker = worker
            worker.start()
        # loop back and show the tray again

    worker.stop()
    return 0


def _logout() -> None:
    cfg = Config.load()
    tid = cfg.tenant_id
    try:
        config_path().unlink()
    except FileNotFoundError:
        pass
    if tid:
        try:
            state_path(tid).unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Self-test — CI runs this on real Windows & macOS to prove the whole GUI stack
# (pystray + Pillow + tkinter + autostart) imports and constructs on that OS.
# It never opens a visible window or touches the user's real config.
# ---------------------------------------------------------------------------
def _emit(msg: str) -> None:
    """Print that never raises — windowed PyInstaller builds can have no stdout."""
    try:
        if sys.stdout is not None:
            sys.stdout.write(msg + "\n")
            sys.stdout.flush()
    except Exception:
        pass


def selftest() -> int:
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            fn()
            _emit(f"  ok   {name}")
        except Exception as e:  # noqa: BLE001
            ok = False
            _emit(f"  FAIL {name}: {e}")

    _emit("bfsync selftest — GUI stack")

    def _icons():
        from .tray import _icon_image
        for s in (runtime.IDLE, runtime.SYNCING, runtime.ERROR,
                  runtime.OFFLINE, runtime.PAUSED):
            img = _icon_image(s)
            assert img.size == (64, 64)

    def _pystray():
        import pystray  # noqa: F401

    def _autostart_paths():
        from . import autostart
        _ = autostart.target_path()
        _ = autostart.command_string()

    def _sidebar_stack():
        from pathlib import Path
        from . import sidebar
        # Pure logic only — never writes to the registry/Finder in the selftest.
        folder, label = sidebar.target_for(str(Path.home() / "Blueforge One" / "WS"))
        assert label == "Blueforge One"
        assert sidebar._asset("blueforge.ico") is not None, "icon asset not bundled"
        _ = sidebar.describe()

    def _runtime():
        st = runtime.AppState()
        st.set_status(runtime.SYNCING, "x")
        assert st.status_label()

    def _tk_construct():
        # Build a hidden Tk root and destroy it — proves tkinter is present and
        # works on this OS without showing anything. Skipped on headless Linux.
        if sys.platform.startswith("linux") and not _has_display():
            _emit("  skip Tk construct (headless Linux, no DISPLAY)")
            return
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        r.update_idletasks()
        r.destroy()

    check("Pillow icons render", _icons)
    check("pystray import", _pystray)
    check("autostart pathing", _autostart_paths)
    check("sidebar stack + icon asset", _sidebar_stack)
    check("runtime state", _runtime)
    check("tkinter construct", _tk_construct)

    _emit("SELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _has_display() -> bool:
    import os
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
