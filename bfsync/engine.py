"""Two-way sync engine.

Reconciles a local folder against a remote Blueforge One Cloud workspace subtree using a
last-known-state manifest to classify each path as new / changed / deleted on
each side. Conservative and correct over clever.

Decision table per path (L=local present, R=remote present, M=in manifest):

  L  R  M   action
  1  0  0   new local          -> upload
  0  1  0   new remote         -> download
  1  1  0   appeared both      -> compare; if identical, adopt; else conflict
  1  0  1   deleted remotely   -> delete local (mirror remote delete)
  0  1  1   deleted locally    -> delete remote (mirror local delete)
  1  1  1   both present:
                - local changed & remote unchanged -> upload
                - remote changed & local unchanged -> download
                - both changed                     -> conflict (keep both)
                - neither changed                  -> skip
  0  0  1   gone both          -> drop from manifest
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .api import SupabaseClient
from .config import Config, StateManifest


LogFn = Callable[[str], None]


@dataclass
class SyncStats:
    uploaded: int = 0
    downloaded: int = 0
    deleted_local: int = 0
    deleted_remote: int = 0
    conflicts: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"up={self.uploaded} down={self.downloaded} "
                f"del_local={self.deleted_local} del_remote={self.deleted_remote} "
                f"conflicts={self.conflicts} skipped={self.skipped} "
                f"errors={len(self.errors)}")


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _content_type(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _iso_to_epoch(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        s = iso.replace("Z", "+00:00")
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() \
            if "+" not in s and "T" in s else datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


class SyncEngine:
    def __init__(self, cfg: Config, client: SupabaseClient,
                 log: Optional[LogFn] = None,
                 on_progress: Optional[Callable[[str, int, int], None]] = None):
        self.cfg = cfg
        self.client = client
        self.log: LogFn = log or (lambda m: print(m, flush=True))
        # on_progress(phase, done, total): called during a pass so the UI can
        # show "Syncing 1,200 of 11,042…" instead of a silent spinner.
        self.on_progress = on_progress or (lambda phase, done, total: None)
        self.local_root = Path(cfg.local_folder).expanduser().resolve()
        # Remote prefix = <tenant_id>[/<remote_root>]
        rr = (cfg.remote_root or "").strip("/")
        self.remote_prefix = f"{cfg.tenant_id}/{rr}" if rr else cfg.tenant_id
        self.manifest = StateManifest(cfg.tenant_id)

    # ---- local scan ---------------------------------------------------------
    def _scan_local(self) -> Dict[str, Dict[str, float]]:
        """Return {relpath -> {"size","mtime"}} for every file under local root.

        Skips system dot-entries so they never round-trip.
        """
        out: Dict[str, Dict[str, float]] = {}
        if not self.local_root.exists():
            self.local_root.mkdir(parents=True, exist_ok=True)
        for dirpath, dirnames, filenames in os.walk(self.local_root):
            # prune system dirs
            dirnames[:] = [d for d in dirnames
                           if d not in {".trash", ".versions"}]
            for fn in filenames:
                if fn in {".keep"}:
                    continue
                full = Path(dirpath) / fn
                rel = full.relative_to(self.local_root).as_posix()
                try:
                    st = full.stat()
                except OSError:
                    continue
                out[rel] = {"size": st.st_size, "mtime": st.st_mtime}
        return out

    def _remote_full(self, rel: str) -> str:
        return f"{self.remote_prefix}/{rel}"

    def _local_full(self, rel: str) -> Path:
        return self.local_root / rel

    # ---- one full pass ------------------------------------------------------
    def sync_once(self) -> SyncStats:
        stats = SyncStats()
        self.log(f"[scan] local={self.local_root} remote={self.remote_prefix}/")

        local = self._scan_local()
        remote = self.client.walk(self.remote_prefix)
        known = self.manifest.known_paths()

        all_paths = set(local) | set(remote) | known
        # Selective Sync: drop any path not selected for THIS computer BEFORE
        # reconciling, so excluded folders are neither downloaded nor ever
        # mistaken for a delete. Loose files always pass (see path_included).
        if not self.cfg.sync_all and self.cfg.sync_candidates:
            before = len(all_paths)
            all_paths = {p for p in all_paths if self.cfg.path_included(p)}
            self.log(f"[selective] {before - len(all_paths)} path(s) excluded "
                     f"by folder selection")

        self.log(f"[scan] local={len(local)} remote={len(remote)} "
                 f"manifest={len(known)} union={len(all_paths)}")

        paths = sorted(all_paths)
        total = len(paths)
        self.on_progress("start", 0, total)
        for i, rel in enumerate(paths, 1):
            L = rel in local
            R = rel in remote
            M = rel in known
            try:
                self._reconcile(rel, L, R, M, local.get(rel), remote.get(rel), stats)
            except Exception as e:  # keep going; one bad file must not stop sync
                msg = f"[error] {rel}: {e}"
                self.log(msg)
                stats.errors.append(msg)
            # Report progress in small batches, and checkpoint the manifest
            # periodically so a very large first sync never loses its place.
            if i % 20 == 0 or i == total:
                self.on_progress("sync", i, total)
            if i % 250 == 0:
                self.manifest.save()

        self.manifest.save()
        self.on_progress("done", total, total)
        self.log(f"[done] {stats.summary()}")
        return stats

    # ---- per-path decision --------------------------------------------------
    def _reconcile(self, rel, L, R, M, lmeta, rmeta, stats: SyncStats) -> None:
        if L and not R and not M:
            self._upload(rel, lmeta, stats, "new-local")
        elif R and not L and not M:
            self._download(rel, rmeta, stats, "new-remote")
        elif L and R and not M:
            # appeared on both since we last looked
            if self._same(rel, lmeta, rmeta):
                self._adopt(rel, lmeta, rmeta)
                stats.skipped += 1
                self.log(f"[skip] {rel} (identical, adopted)")
            else:
                self._conflict(rel, lmeta, rmeta, stats, "both-new-differ")
        elif L and not R and M:
            # was known, now gone remotely -> remote deleted it
            self._delete_local(rel, stats)
        elif R and not L and M:
            # was known, now gone locally -> local deleted it
            self._delete_remote(rel, stats)
        elif L and R and M:
            man = self.manifest.get(rel)
            local_changed = self._local_changed(lmeta, man)
            remote_changed = self._remote_changed(rmeta, man)
            if local_changed and not remote_changed:
                self._upload(rel, lmeta, stats, "local-changed")
            elif remote_changed and not local_changed:
                self._download(rel, rmeta, stats, "remote-changed")
            elif local_changed and remote_changed:
                if self._same(rel, lmeta, rmeta):
                    self._adopt(rel, lmeta, rmeta)
                    stats.skipped += 1
                    self.log(f"[skip] {rel} (both changed but identical)")
                else:
                    self._conflict(rel, lmeta, rmeta, stats, "both-changed")
            else:
                stats.skipped += 1
        elif not L and not R and M:
            self.manifest.remove(rel)

    # ---- change detection helpers ------------------------------------------
    @staticmethod
    def _local_changed(lmeta, man) -> bool:
        if not man:
            return True
        # mtime moved forward or size differs
        return (abs(lmeta["mtime"] - man.get("mtime", 0)) > 1e-6
                or lmeta["size"] != man.get("size"))

    @staticmethod
    def _remote_changed(rmeta, man) -> bool:
        if not man:
            return True
        return (rmeta.get("updated_at", "") != man.get("remote_updated_at", "")
                or rmeta.get("size") != man.get("size"))

    @staticmethod
    def _same(rel, lmeta, rmeta) -> bool:
        # Best-effort identity check without hashing: same size.
        # (Storage does not expose a content hash cheaply; size match plus the
        # adopt-into-manifest keeps us from re-uploading identical files.)
        return lmeta["size"] == rmeta.get("size")

    # ---- actions ------------------------------------------------------------
    def _upload(self, rel, lmeta, stats, why) -> None:
        full = self._remote_full(rel)
        self.log(f"[upload] {rel} ({lmeta['size']} bytes) [{why}]")
        self.client.upload(self._local_full(rel), full, _content_type(rel))
        rstat = self.client.stat_remote(self.remote_prefix, rel) or {}
        self.manifest.set(rel, lmeta["size"], lmeta["mtime"],
                          rstat.get("updated_at", ""))
        stats.uploaded += 1

    def _download(self, rel, rmeta, stats, why) -> None:
        dest = self._local_full(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.log(f"[download] {rel} ({rmeta.get('size')} bytes) [{why}]")
        self.client.download(self._remote_full(rel), dest)
        st = dest.stat()
        self.manifest.set(rel, st.st_size, st.st_mtime, rmeta.get("updated_at", ""))
        stats.downloaded += 1

    def _delete_local(self, rel, stats) -> None:
        dest = self._local_full(rel)
        self.log(f"[delete-local] {rel}")
        try:
            dest.unlink()
        except FileNotFoundError:
            pass
        self.manifest.remove(rel)
        stats.deleted_local += 1

    def _delete_remote(self, rel, stats) -> None:
        self.log(f"[delete-remote] {rel}")
        self.client.delete(self._remote_full(rel))
        self.manifest.remove(rel)
        stats.deleted_remote += 1

    def _adopt(self, rel, lmeta, rmeta) -> None:
        """Record an already-in-sync file into the manifest without transfer."""
        self.manifest.set(rel, lmeta["size"], lmeta["mtime"],
                          rmeta.get("updated_at", ""))

    def _conflict(self, rel, lmeta, rmeta, stats, why) -> None:
        """Keep both copies. Download the remote version alongside the local one
        as '<name> (conflict <ts>).<ext>', then upload the local copy so the
        remote also carries both. Manifest tracks the (unchanged) primary."""
        stem, dot, ext = rel.rpartition(".")
        base = stem if dot else rel
        suffix = f".{ext}" if dot else ""
        conflict_rel = f"{base} (conflict {_now_stamp()}){suffix}"
        self.log(f"[conflict] {rel} [{why}] -> keeping remote copy as "
                 f"{conflict_rel}")

        # Save remote version locally under the conflict name.
        cdest = self._local_full(conflict_rel)
        cdest.parent.mkdir(parents=True, exist_ok=True)
        self.client.download(self._remote_full(rel), cdest)

        # Upload local (primary) so remote matches local for the primary path.
        self.client.upload(self._local_full(rel), self._remote_full(rel),
                           _content_type(rel))
        rstat = self.client.stat_remote(self.remote_prefix, rel) or {}
        self.manifest.set(rel, lmeta["size"], lmeta["mtime"],
                          rstat.get("updated_at", ""))

        # Upload the conflict copy too, so it exists on both sides.
        cst = cdest.stat()
        self.client.upload(cdest, self._remote_full(conflict_rel),
                           _content_type(conflict_rel))
        crstat = self.client.stat_remote(self.remote_prefix, conflict_rel) or {}
        self.manifest.set(conflict_rel, cst.st_size, cst.st_mtime,
                          crstat.get("updated_at", ""))
        stats.conflicts += 1
