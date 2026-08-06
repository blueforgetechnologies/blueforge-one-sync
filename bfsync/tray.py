"""System-tray / menu-bar presence for Blueforge One Sync (pystray).

The tray icon is the app's day-to-day face: a small cloud in the menu bar whose
colour reflects sync status, with a menu to sync now, pause, open the folder or
website, toggle start-at-login, open settings, and quit.

MAIN-THREAD RULE: pystray's macOS backend must run on the main thread, and so
must tkinter. We never run them at once. Menu items that need a window (Settings,
Change folder, Sign out) don't open tkinter here — they record a *pending action*
and stop the icon. The desktop orchestrator (desktop.py) then handles that action
on the main thread and, if appropriate, starts the tray again. Everything stays
serialized and crash-free on macOS, Windows and Linux.
"""

from __future__ import annotations

from typing import Optional

from . import runtime

WEB_URL = "https://blueforge-one.vercel.app"

# status -> accent colour for the little dot on the cloud
_DOT = {
    runtime.IDLE: (34, 197, 94),      # green  = up to date
    runtime.SYNCING: (37, 99, 235),   # blue   = syncing
    runtime.PAUSED: (148, 163, 184),  # grey   = paused
    runtime.ERROR: (220, 38, 38),     # red    = attention
    runtime.OFFLINE: (234, 179, 8),   # amber  = offline
    runtime.STARTING: (37, 99, 235),
}


def _icon_image(status: str):
    """Draw a 64x64 cloud with a status dot. Returns a PIL image."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cloud = (71, 85, 105, 255)  # slate — reads on light or dark menu bars
    # cloud = three overlapping circles + a base rectangle
    d.ellipse((8, 26, 34, 52), fill=cloud)
    d.ellipse((24, 18, 52, 46), fill=cloud)
    d.ellipse((34, 28, 58, 52), fill=cloud)
    d.rectangle((16, 40, 52, 52), fill=cloud)
    # status dot
    r, g, b = _DOT.get(status, _DOT[runtime.IDLE])
    d.ellipse((40, 8, 60, 28), fill=(255, 255, 255, 255))
    d.ellipse((42, 10, 58, 26), fill=(r, g, b, 255))
    return img


class Tray:
    def __init__(self, state: runtime.AppState, worker) -> None:
        self.state = state
        self.worker = worker
        self.icon = None
        self.pending: Optional[str] = None  # 'quit'|'settings'|'reconfigure'|'signout'

    # ---- notifications (used by the worker) --------------------------------
    def notify(self, title: str, message: str) -> None:
        try:
            if self.icon is not None:
                self.icon.notify(message, title)
        except Exception:
            pass

    # ---- menu --------------------------------------------------------------
    def _menu(self):
        import pystray
        from pystray import MenuItem as Item, Menu

        def status_text(_item):
            s = self.state.snapshot()
            # During a sync show the live "Syncing X of Y…" detail; otherwise the
            # generic status label. Always append when the last sync finished.
            label = s["detail"] if s["status"] == runtime.SYNCING else self.state.status_label()
            return f"{label} · last sync {self.state.last_sync_text()}"

        def workspace_text(_item):
            s = self.state.snapshot()
            return s.get("workspace_name") or "Blueforge One Sync"

        def do_sync(icon, item):
            self.worker.request_sync_now()

        def do_pause(icon, item):
            if self.worker.is_paused():
                self.worker.resume()
            else:
                self.worker.pause()
            icon.update_menu()

        def is_paused(item):
            return self.worker.is_paused()

        def do_open_folder(icon, item):
            self._open_folder()

        def do_open_web(icon, item):
            import webbrowser
            webbrowser.open(WEB_URL)

        def do_pin_sidebar(icon, item):
            from . import sidebar
            s = self.state.snapshot()
            ok = sidebar.enable(s.get("local_folder") or "")
            self.notify("Blueforge One",
                        "Added to your File Explorer sidebar." if ok
                        else "Couldn't add the sidebar entry.")

        def autostart_checked(item):
            try:
                from . import autostart
                return autostart.is_enabled()
            except Exception:
                return False

        def do_autostart(icon, item):
            from . import autostart
            try:
                if autostart.is_enabled():
                    autostart.disable()
                else:
                    autostart.enable()
            except Exception:
                pass
            icon.update_menu()

        def do_settings(icon, item):
            self.pending = "settings"
            icon.stop()

        def do_quit(icon, item):
            self.pending = "quit"
            self.worker.stop()
            icon.stop()

        return Menu(
            Item(workspace_text, None, enabled=False),
            Item(status_text, None, enabled=False),
            Menu.SEPARATOR,
            Item("Sync now", do_sync),
            Item("Pause syncing", do_pause, checked=is_paused),
            Menu.SEPARATOR,
            Item("Status & settings…", do_settings, default=True),
            Item("Open sync folder", do_open_folder),
            Item("Show in File Explorer sidebar", do_pin_sidebar),
            Item("Open Blueforge One Cloud website", do_open_web),
            Menu.SEPARATOR,
            Item("Start at login", do_autostart, checked=autostart_checked),
            Menu.SEPARATOR,
            Item("Quit", do_quit),
        )

    def _open_folder(self):
        from .wizard import _open_path
        s = self.state.snapshot()
        _open_path(s.get("local_folder") or "")

    # ---- lifecycle ---------------------------------------------------------
    def run(self) -> str:
        """Create the icon and run its loop on the CURRENT (main) thread.
        Blocks until a menu action calls icon.stop(). Returns the pending
        action so the orchestrator can react."""
        import pystray

        self.pending = None
        self.icon = pystray.Icon(
            "blueforge-one-sync",
            icon=_icon_image(self.state.snapshot()["status"]),
            title="Blueforge One Sync",
            menu=self._menu(),
        )

        # Live-update the icon + tooltip whenever state changes.
        def on_change(state: runtime.AppState):
            try:
                snap = state.snapshot()
                self.icon.icon = _icon_image(snap["status"])
                self.icon.title = f"Blueforge One Sync — {state.status_label()}"
                self.icon.update_menu()
            except Exception:
                pass

        self.state.on_change(on_change)
        self.icon.run()
        return self.pending or ""
