"""Background sync worker thread for the desktop app.

Wraps the same ``SyncEngine`` the CLI uses, but runs it inside a controllable
thread that the tray icon drives: pause/resume, sync-now, and clean stop. It
keeps ``AppState`` current and fires a notification callback when a pass
actually moves files, so the user gets a native toast like Dropbox.

Robustness:
  - one bad cycle never kills the thread (caught + surfaced as ERROR/OFFLINE);
  - network/connection failures downgrade to OFFLINE and retry with backoff;
  - a watchdog observer (if available) makes local edits sync near-instantly,
    otherwise we fall back to interval polling.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .api import ApiError, AuthError, SupabaseClient
from .config import Config, app_dir
from .engine import SyncEngine
from . import runtime


NotifyFn = Callable[[str, str], None]

LOG_FILE = "sync.log"


class SyncWorker(threading.Thread):
    def __init__(self, state: runtime.AppState,
                 notify: Optional[NotifyFn] = None) -> None:
        super().__init__(name="bfsync-worker", daemon=True)
        self.state = state
        self._notify = notify or (lambda title, msg: None)
        self._stopev = threading.Event()
        self._wake = threading.Event()
        self._paused = threading.Event()  # set == paused
        self._engine: Optional[SyncEngine] = None
        self._logf = None

    # ---- external controls -------------------------------------------------
    def request_sync_now(self) -> None:
        self._wake.set()

    def pause(self) -> None:
        self._paused.set()
        self.state.set_paused(True)

    def resume(self) -> None:
        self._paused.clear()
        self.state.set_paused(False)
        self._wake.set()

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def stop(self) -> None:
        self._stopev.set()
        self._wake.set()

    # ---- logging -----------------------------------------------------------
    def _log(self, msg: str) -> None:
        self.state.add_log(msg)
        try:
            if self._logf:
                self._logf.write(msg + "\n")
                self._logf.flush()
        except Exception:
            pass

    def _open_log(self) -> None:
        try:
            self._logf = open(app_dir() / LOG_FILE, "a", encoding="utf-8")
        except Exception:
            self._logf = None

    # ---- engine construction (with fresh token) ----------------------------
    def _build_engine(self) -> Optional[SyncEngine]:
        cfg = Config.load()
        if not cfg.is_configured():
            self.state.set_status(runtime.ERROR, "Not set up yet")
            return None
        self.state.set_config(cfg.tenant_name or "", cfg.local_folder or "", cfg.email or "")
        client = SupabaseClient(refresh_token=cfg.refresh_token)
        client.refresh()
        if client.refresh_token and client.refresh_token != cfg.refresh_token:
            cfg.refresh_token = client.refresh_token
            cfg.save()
        return SyncEngine(cfg, client, log=self._log, on_progress=self._progress)

    def _progress(self, phase: str, done: int, total: int) -> None:
        if phase == "start":
            self.state.set_status(runtime.SYNCING, "Checking your files…")
        elif phase == "sync" and total:
            self.state.set_status(runtime.SYNCING,
                                  f"Syncing {done:,} of {total:,}…")

    # ---- the run loop ------------------------------------------------------
    def run(self) -> None:
        self._open_log()
        self.state.set_status(runtime.STARTING, "Starting…")

        observer = self._start_watchdog()
        interval = 60
        backoff = 5
        last_poll = 0.0
        self._wake.set()  # sync immediately on launch

        try:
            while not self._stopev.is_set():
                # Build/refresh the engine lazily until it succeeds. Retry
                # sleeps wait on STOP only, so backoff is actually honoured.
                if self._engine is None:
                    try:
                        self._engine = self._build_engine()
                    except AuthError as e:
                        self.state.set_status(runtime.ERROR, "Please sign in again")
                        self._log(f"[auth] {e}")
                        self._retry_sleep(30)
                        continue
                    except Exception as e:
                        self.state.set_status(runtime.OFFLINE, "Waiting for connection")
                        self._log(f"[offline] {e}")
                        self._retry_sleep(backoff)
                        backoff = min(backoff * 2, 300)
                        continue
                    if self._engine is None:
                        self._retry_sleep(30)
                        continue
                    interval = max(15, self._engine.cfg.interval_seconds)
                    self._rebind_watchdog(observer)
                    backoff = 5

                if self._paused.is_set():
                    self._wake.clear()
                    self._idle_wait(2)
                    continue

                now = time.time()
                if self._wake.is_set() or (now - last_poll) >= interval:
                    self._wake.clear()
                    last_poll = now
                    self._run_one_pass()

                # Steady idle: wake early on sync-now or stop.
                self._idle_wait(2)
        finally:
            if observer is not None:
                try:
                    observer.stop(); observer.join(timeout=3)
                except Exception:
                    pass
            if self._logf:
                try:
                    self._logf.close()
                except Exception:
                    pass

    def _run_one_pass(self) -> None:
        self.state.set_status(runtime.SYNCING, "Syncing…")
        try:
            stats = self._engine.sync_once()
        except (ApiError,) as e:
            self.state.set_status(runtime.OFFLINE, "Waiting for connection")
            self._log(f"[offline] {e}")
            self._engine = None  # rebuild (token/network) next loop
            return
        except AuthError as e:
            self.state.set_status(runtime.ERROR, "Please sign in again")
            self._log(f"[auth] {e}")
            self._engine = None
            return
        except Exception as e:
            self.state.set_status(runtime.ERROR, "Sync error — see log")
            self._log(f"[error] {e}")
            return

        self.state.record_sync(stats)
        self._maybe_notify(stats)
        if stats.errors:
            self.state.set_status(runtime.ERROR, f"{len(stats.errors)} file(s) had errors")
        else:
            self.state.set_status(runtime.IDLE, "Up to date")

    def _maybe_notify(self, stats) -> None:
        moved = (stats.uploaded + stats.downloaded
                 + stats.deleted_local + stats.deleted_remote)
        if stats.conflicts:
            self._notify(
                "Blueforge One Sync",
                f"Kept both copies of {stats.conflicts} file(s) that changed in "
                f"two places.",
            )
        if moved:
            bits = []
            if stats.uploaded:
                bits.append(f"{stats.uploaded} up")
            if stats.downloaded:
                bits.append(f"{stats.downloaded} down")
            if stats.deleted_local or stats.deleted_remote:
                bits.append(f"{stats.deleted_local + stats.deleted_remote} removed")
            self._notify("Blueforge One Sync", "Synced " + ", ".join(bits) + ".")

    # ---- watchdog ----------------------------------------------------------
    def _start_watchdog(self):
        try:
            from watchdog.observers import Observer
            return Observer()
        except Exception:
            return None

    def _rebind_watchdog(self, observer) -> None:
        if observer is None or self._engine is None:
            return
        try:
            from watchdog.events import FileSystemEventHandler

            worker = self

            class _H(FileSystemEventHandler):
                def on_any_event(self, event):
                    worker._wake.set()

            # (Re)schedule on the current local folder.
            try:
                observer.unschedule_all()
            except Exception:
                pass
            folder = self._engine.cfg.local_folder
            Path(folder).mkdir(parents=True, exist_ok=True)
            observer.schedule(_H(), folder, recursive=True)
            if not observer.is_alive():
                observer.start()
            self._log(f"[watch] instant sync active on {folder}")
        except Exception as e:
            self._log(f"[watch] watchdog unavailable, polling only: {e}")

    # ---- helpers -----------------------------------------------------------
    def _idle_wait(self, seconds: float) -> None:
        """Steady idle wait — returns early on sync-now (wake) or stop."""
        self._wake.wait(timeout=seconds)

    def _retry_sleep(self, seconds: float) -> None:
        """Backoff sleep — interrupted only by stop, so backoff is honoured."""
        self._stopev.wait(timeout=seconds)
