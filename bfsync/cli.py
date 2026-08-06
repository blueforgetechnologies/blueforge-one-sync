"""Command-line interface for Blueforge One Sync.

Commands:
  bfsync login              authenticate (email + password), store refresh token
  bfsync setup              pick a workspace + local folder
  bfsync sync               run one sync pass and exit
  bfsync watch              loop forever (watchdog if available, else polling)
  bfsync status             show current configuration
  bfsync logout             remove stored credentials + config

Designed so a future tray-icon GUI (Phase 2) can import `run_login`,
`run_setup`, `build_engine`, and `run_watch` directly instead of shelling out.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path
from typing import Optional

from . import __version__
from .api import AuthError, SupabaseClient
from .config import Config, config_path, state_path
from .engine import SyncEngine


def _log(msg: str) -> None:
    print(msg, flush=True)


def _client_from_config(cfg: Config) -> SupabaseClient:
    """Build a client with a fresh access token from the stored refresh token."""
    client = SupabaseClient(refresh_token=cfg.refresh_token)
    client.refresh()
    # Persist the possibly-rotated refresh token.
    if client.refresh_token and client.refresh_token != cfg.refresh_token:
        cfg.refresh_token = client.refresh_token
        cfg.save()
    return client


# ---- commands ---------------------------------------------------------------
def run_login(email: Optional[str] = None, password: Optional[str] = None) -> int:
    cfg = Config.load()
    if not email:
        email = input("Email: ").strip()
    if not password:
        password = getpass.getpass("Password: ")
    client = SupabaseClient()
    try:
        client.login(email, password)
    except AuthError as e:
        _log(f"Login failed: {e}")
        return 1
    cfg.email = email
    cfg.refresh_token = client.refresh_token  # store ONLY the refresh token
    cfg.save()
    _log(f"Logged in as {email}. Credentials saved to {config_path()}")
    _log("Next: run `bfsync setup` to choose a workspace and local folder.")
    return 0


def run_setup(local_folder: Optional[str] = None,
              tenant_id: Optional[str] = None,
              remote_root: Optional[str] = None,
              interval: Optional[int] = None,
              non_interactive: bool = False) -> int:
    cfg = Config.load()
    if not cfg.is_logged_in():
        _log("Not logged in. Run `bfsync login` first.")
        return 1
    client = _client_from_config(cfg)

    members = client.list_memberships()
    if not members:
        _log("No workspaces found for this account.")
        return 1
    ids = [m["tenant_id"] for m in members]
    names = client.tenant_names(ids)

    chosen = tenant_id
    if not chosen:
        if non_interactive:
            _log("tenant_id required in non-interactive mode.")
            return 1
        _log("Your workspaces:")
        for i, tid in enumerate(ids):
            _log(f"  [{i}] {names.get(tid, '(unknown)')}  ({tid})")
        pick = input("Pick a workspace number: ").strip()
        try:
            chosen = ids[int(pick)]
        except (ValueError, IndexError):
            _log("Invalid selection.")
            return 1

    cfg.tenant_id = chosen
    cfg.tenant_name = names.get(chosen, "")

    if not local_folder:
        if non_interactive:
            _log("local_folder required in non-interactive mode.")
            return 1
        default = str(Path.home() / "Blueforge One" / (cfg.tenant_name or "workspace"))
        entered = input(f"Local folder to sync [{default}]: ").strip()
        local_folder = entered or default
    cfg.local_folder = str(Path(local_folder).expanduser())
    Path(cfg.local_folder).mkdir(parents=True, exist_ok=True)

    if remote_root is not None:
        cfg.remote_root = remote_root.strip("/")
    if interval:
        cfg.interval_seconds = int(interval)
    cfg.save()

    _log(f"Configured: workspace '{cfg.tenant_name}' ({cfg.tenant_id})")
    _log(f"  Local folder : {cfg.local_folder}")
    _log(f"  Remote root  : {cfg.tenant_id}/{cfg.remote_root}")
    _log(f"  Interval     : {cfg.interval_seconds}s")
    _log("Run `bfsync sync` for a one-time sync, or `bfsync watch` to keep syncing.")
    return 0


def build_engine(cfg: Optional[Config] = None) -> Optional[SyncEngine]:
    cfg = cfg or Config.load()
    if not cfg.is_configured():
        _log("Not configured. Run `bfsync login` then `bfsync setup`.")
        return None
    client = _client_from_config(cfg)
    return SyncEngine(cfg, client, log=_log)


def run_sync() -> int:
    engine = build_engine()
    if not engine:
        return 1
    stats = engine.sync_once()
    return 0 if not stats.errors else 2


def run_watch() -> int:
    cfg = Config.load()
    engine = build_engine(cfg)
    if not engine:
        return 1
    interval = cfg.interval_seconds

    # Try watchdog for instant local change detection; fall back to polling.
    observer = None
    dirty = {"flag": True}  # start dirty so we sync immediately
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                dirty["flag"] = True

        observer = Observer()
        observer.schedule(_Handler(), engine.cfg.local_folder, recursive=True)
        observer.start()
        _log(f"[watch] watchdog active on {engine.cfg.local_folder}; "
             f"remote poll every {interval}s")
    except Exception:
        _log(f"[watch] watchdog unavailable; polling every {interval}s")

    try:
        last_remote_poll = 0.0
        while True:
            now = time.time()
            remote_due = (now - last_remote_poll) >= interval
            if dirty["flag"] or remote_due:
                dirty["flag"] = False
                last_remote_poll = now
                try:
                    engine.sync_once()
                except Exception as e:
                    _log(f"[watch] cycle error: {e}")
            time.sleep(2)
    except KeyboardInterrupt:
        _log("\n[watch] stopped.")
    finally:
        if observer:
            observer.stop()
            observer.join()
    return 0


def run_sidebar(remove: bool = False) -> int:
    """Add (or remove) the branded Blueforge One entry in the file-manager
    sidebar — the Dropbox/OneDrive-style left-pane shortcut."""
    from . import sidebar
    cfg = Config.load()
    if remove:
        sidebar.disable()
        _log("Removed the Blueforge One sidebar entry.")
        return 0
    if not cfg.local_folder:
        _log("Not configured yet. Run `bfsync setup` first.")
        return 1
    ok = sidebar.enable(cfg.local_folder)
    folder, label = sidebar.target_for(cfg.local_folder)
    if ok:
        _log(f"Added '{label}' to your file-manager sidebar → {folder}")
        _log("Open a new File Explorer / Finder window to see it on the left.")
        return 0
    _log("Could not add the sidebar entry on this system.")
    return 1


def run_status() -> int:
    cfg = Config.load()
    _log(f"Blueforge One Sync v{__version__}")
    _log(f"Config file : {config_path()}")
    _log(f"Logged in   : {cfg.is_logged_in()} ({cfg.email or '-'})")
    _log(f"Workspace   : {cfg.tenant_name or '-'} ({cfg.tenant_id or '-'})")
    _log(f"Local folder: {cfg.local_folder or '-'}")
    _log(f"Remote root : {cfg.tenant_id or '-'}/{cfg.remote_root}")
    _log(f"Interval    : {cfg.interval_seconds}s")
    return 0


def run_logout() -> int:
    cfg = Config.load()
    tid = cfg.tenant_id
    for p in (config_path(),):
        try:
            Path(p).unlink()
        except FileNotFoundError:
            pass
    if tid:
        try:
            Path(state_path(tid)).unlink()
        except FileNotFoundError:
            pass
    _log("Logged out. Stored credentials and config removed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bfsync",
        description="Blueforge One Sync — two-way folder sync for Blueforge One Cloud.",
    )
    p.add_argument("--version", action="version", version=f"bfsync {__version__}")
    sub = p.add_subparsers(dest="command")

    lp = sub.add_parser("login", help="authenticate and store refresh token")
    lp.add_argument("--email")
    lp.add_argument("--password")

    sp = sub.add_parser("setup", help="pick workspace + local folder")
    sp.add_argument("--tenant-id")
    sp.add_argument("--local-folder")
    sp.add_argument("--remote-root", help="subpath under the tenant to sync")
    sp.add_argument("--interval", type=int)
    sp.add_argument("--non-interactive", action="store_true")

    sub.add_parser("sync", help="run one sync pass")
    sub.add_parser("watch", help="keep syncing on an interval")
    sub.add_parser("status", help="show current configuration")
    sub.add_parser("logout", help="remove stored credentials + config")
    sbp = sub.add_parser("sidebar",
                         help="add/remove the Blueforge One file-manager sidebar entry")
    sbp.add_argument("--remove", action="store_true", help="remove it instead of adding")

    sub.add_parser("app", help="launch the desktop tray app (GUI)")
    sub.add_parser("selftest", help="verify the desktop GUI stack loads on this OS")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.command
    if cmd == "login":
        return run_login(args.email, args.password)
    if cmd == "setup":
        return run_setup(
            local_folder=args.local_folder,
            tenant_id=args.tenant_id,
            remote_root=args.remote_root,
            interval=args.interval,
            non_interactive=args.non_interactive,
        )
    if cmd == "sync":
        return run_sync()
    if cmd == "watch":
        return run_watch()
    if cmd == "status":
        return run_status()
    if cmd == "logout":
        return run_logout()
    if cmd == "sidebar":
        return run_sidebar(remove=args.remove)
    if cmd == "app":
        from .desktop import run as run_app
        return run_app()
    if cmd == "selftest":
        from .desktop import selftest
        return selftest()
    if cmd is None:
        # No subcommand: if this is the frozen GUI binary, boot the tray;
        # otherwise show CLI help.
        if getattr(sys, "frozen", False):
            from .desktop import run as run_app
            return run_app()
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
