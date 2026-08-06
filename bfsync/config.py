"""Configuration + local state persistence for Blueforge One Sync.

All state lives under ~/.blueforge-one-sync/ :
  - config.json                 : {email, refresh_token, tenant_id, tenant_name,
                                    local_folder, interval_seconds, remote_root}
  - state-<tenant_id>.json      : manifest mapping relpath -> {size, mtime,
                                    remote_updated_at}

We NEVER store the plaintext password. Only the Supabase refresh_token is kept,
which lets us mint fresh access tokens without re-prompting.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


APP_DIR_NAME = ".blueforge-one-sync"


def app_dir() -> Path:
    """Return the per-user application directory, creating it if needed."""
    d = Path.home() / APP_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return app_dir() / "config.json"


def state_path(tenant_id: str) -> Path:
    return app_dir() / f"state-{tenant_id}.json"


def _atomic_write(path: Path, data: str) -> None:
    """Write to a temp file in the same dir then rename, so a crash mid-write
    never corrupts the file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


class Config:
    """Small typed wrapper over config.json."""

    FIELDS = (
        "email",
        "refresh_token",
        "tenant_id",
        "tenant_name",
        "local_folder",
        "interval_seconds",
        "remote_root",
        "sync_all",
        "sync_folders",
        "sync_candidates",
    )

    def __init__(self, data: Optional[Dict[str, Any]] = None):
        data = data or {}
        self.email: Optional[str] = data.get("email")
        self.refresh_token: Optional[str] = data.get("refresh_token")
        self.tenant_id: Optional[str] = data.get("tenant_id")
        self.tenant_name: Optional[str] = data.get("tenant_name")
        self.local_folder: Optional[str] = data.get("local_folder")
        self.interval_seconds: int = int(data.get("interval_seconds", 60))
        # remote_root is the path prefix under the tenant that we sync.
        # Empty string = the whole workspace. Used by the test harness to
        # scope sync to a ".synctest" subfolder.
        self.remote_root: str = data.get("remote_root", "")
        # --- Selective Sync ---
        # sync_all True  -> mirror the whole workspace (default).
        # sync_all False -> only paths under one of `sync_folders` download;
        #                   `sync_candidates` is the full set the user chose
        #                   from, so files that live OUTSIDE every candidate
        #                   folder (loose files) always sync and are never
        #                   mistaken for deletions.
        self.sync_all: bool = bool(data.get("sync_all", True))
        self.sync_folders: list = list(data.get("sync_folders") or [])
        self.sync_candidates: list = list(data.get("sync_candidates") or [])

    @classmethod
    def load(cls) -> "Config":
        p = config_path()
        if not p.exists():
            return cls()
        return cls(json.loads(p.read_text(encoding="utf-8")))

    def save(self) -> None:
        out = {k: getattr(self, k) for k in self.FIELDS}
        _atomic_write(config_path(), json.dumps(out, indent=2))
        # Restrict permissions where the OS supports it (tokens live here).
        try:
            os.chmod(config_path(), 0o600)
        except (OSError, NotImplementedError):
            pass

    def is_logged_in(self) -> bool:
        return bool(self.refresh_token and self.email)

    def is_configured(self) -> bool:
        return bool(self.is_logged_in() and self.tenant_id and self.local_folder)

    def path_included(self, rel: str) -> bool:
        """Whether a workspace-relative path should sync on THIS computer.

        - Selective Sync off (sync_all) -> everything syncs.
        - On -> a path under a candidate folder syncs only if that candidate is
          selected. A loose file (under no candidate folder) always syncs, so a
          deselected folder never causes those files to be treated as deleted.
        """
        if self.sync_all or not self.sync_candidates:
            return True
        under_candidate = None
        for cand in self.sync_candidates:
            if rel == cand or rel.startswith(cand + "/"):
                # deepest (longest) matching candidate wins
                if under_candidate is None or len(cand) > len(under_candidate):
                    under_candidate = cand
        if under_candidate is None:
            return True  # loose file, not inside any offered folder
        return under_candidate in self.sync_folders


class StateManifest:
    """Tracks what we last knew about each path, so a later cycle can tell
    new / changed / deleted apart on both sides.

    Maps relpath (POSIX-style, relative to remote_root / local_folder) to:
        {"size": int, "mtime": float, "remote_updated_at": str}
    """

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id
        self.entries: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        p = state_path(self.tenant_id)
        if p.exists():
            try:
                self.entries = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self.entries = {}

    def save(self) -> None:
        _atomic_write(
            state_path(self.tenant_id),
            json.dumps(self.entries, indent=2, sort_keys=True),
        )

    def get(self, relpath: str) -> Optional[Dict[str, Any]]:
        return self.entries.get(relpath)

    def set(self, relpath: str, size: int, mtime: float, remote_updated_at: str) -> None:
        self.entries[relpath] = {
            "size": size,
            "mtime": mtime,
            "remote_updated_at": remote_updated_at,
        }

    def remove(self, relpath: str) -> None:
        self.entries.pop(relpath, None)

    def known_paths(self) -> set:
        return set(self.entries.keys())
