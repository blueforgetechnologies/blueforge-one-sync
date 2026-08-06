"""Shared runtime state for the desktop app.

A single ``AppState`` object is the source of truth the tray icon, the settings
window, and the background worker all read from and write to. It is thread-safe
and notifies listeners (on the thread that made the change) so the UI can
refresh without polling.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional


# Status values the UI maps to an icon colour + label.
IDLE = "idle"
SYNCING = "syncing"
PAUSED = "paused"
ERROR = "error"
OFFLINE = "offline"
STARTING = "starting"

_LABELS = {
    IDLE: "Up to date",
    SYNCING: "Syncing…",
    PAUSED: "Paused",
    ERROR: "Attention needed",
    OFFLINE: "Waiting for connection",
    STARTING: "Starting…",
}


class AppState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.status: str = STARTING
        self.detail: str = "Starting…"
        self.last_sync_epoch: float = 0.0
        self.paused: bool = False
        self.workspace_name: str = ""
        self.local_folder: str = ""
        self.email: str = ""
        self.totals: Dict[str, int] = {
            "uploaded": 0, "downloaded": 0,
            "deleted_local": 0, "deleted_remote": 0,
            "conflicts": 0, "errors": 0,
        }
        self.log_lines: Deque[str] = deque(maxlen=300)
        self._listeners: List[Callable[["AppState"], None]] = []

    # ---- listeners ---------------------------------------------------------
    def on_change(self, fn: Callable[["AppState"], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def _emit(self) -> None:
        for fn in list(self._listeners):
            try:
                fn(self)
            except Exception:
                pass

    # ---- mutators ----------------------------------------------------------
    def set_config(self, workspace_name: str, local_folder: str, email: str) -> None:
        with self._lock:
            self.workspace_name = workspace_name or ""
            self.local_folder = local_folder or ""
            self.email = email or ""
        self._emit()

    def set_status(self, status: str, detail: Optional[str] = None) -> None:
        with self._lock:
            self.status = status
            self.detail = detail if detail is not None else _LABELS.get(status, status)
        self._emit()

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self.paused = paused
            self.status = PAUSED if paused else IDLE
            self.detail = _LABELS[self.status]
        self._emit()

    def record_sync(self, stats) -> None:
        """Fold a completed SyncStats into the running totals."""
        with self._lock:
            self.totals["uploaded"] += getattr(stats, "uploaded", 0)
            self.totals["downloaded"] += getattr(stats, "downloaded", 0)
            self.totals["deleted_local"] += getattr(stats, "deleted_local", 0)
            self.totals["deleted_remote"] += getattr(stats, "deleted_remote", 0)
            self.totals["conflicts"] += getattr(stats, "conflicts", 0)
            self.totals["errors"] += len(getattr(stats, "errors", []) or [])
            self.last_sync_epoch = time.time()
        self._emit()

    def add_log(self, line: str) -> None:
        with self._lock:
            self.log_lines.append(line)
        # Log spam should not force a full UI repaint every line.

    # ---- readers -----------------------------------------------------------
    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                "status": self.status,
                "detail": self.detail,
                "paused": self.paused,
                "last_sync_epoch": self.last_sync_epoch,
                "workspace_name": self.workspace_name,
                "local_folder": self.local_folder,
                "email": self.email,
                "totals": dict(self.totals),
            }

    def status_label(self) -> str:
        with self._lock:
            return _LABELS.get(self.status, self.status)

    def last_sync_text(self) -> str:
        with self._lock:
            ts = self.last_sync_epoch
        if not ts:
            return "not yet"
        delta = max(0, int(time.time() - ts))
        if delta < 5:
            return "just now"
        if delta < 60:
            return f"{delta}s ago"
        if delta < 3600:
            return f"{delta // 60} min ago"
        if delta < 86400:
            return f"{delta // 3600} hr ago"
        return f"{delta // 86400} d ago"
