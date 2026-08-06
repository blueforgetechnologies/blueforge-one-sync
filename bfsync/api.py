"""Direct HTTPS client for the Blueforge One Cloud backend.

Blueforge One stores each file as:
  - an object in the `files` STORAGE bucket under an opaque key
    "<account_id>/<uuid>-<filename>" (no folder tree in storage), and
  - a row in the `files` TABLE that carries the real tree position:
    name + parent ("Folder/Sub") + path (the storage key) + size + mime.
Folders are rows in the `folders` table (path/name/parent).

This client keeps the exact interface the sync engine already speaks
(walk / upload / download / delete / stat_remote / list_folder), but the
tree comes from the database — which means the server's permission rules
(who may see which company's files) apply automatically: each person's
sync folder only ever contains what their access allows.

No SDK — just `requests`. Handles:
  - email/password login and refresh_token grant
  - automatic access-token refresh on 401
  - retry-with-backoff on transient network / 5xx errors
  - streaming upload from disk and streaming download to disk
"""

from __future__ import annotations

import time
import urllib.parse
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests


# ---- Baked-in backend facts (anon key is public — same as the web app) -------
SUPABASE_URL = "https://hopclmsqndpjhojywxxq.supabase.co"
ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhvcGNsbXNxbmRwamhvanl3eHhxIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3ODU2MjY5MjQsImV4cCI6MjEwMTIwMjkyNH0."
    "8DIfT6jZ-UKByc9wqbDp14fCx4BgJCxzTa8NWXZQ8lc"
)
BUCKET = "files"

# Names we never sync (kept for engine compatibility; the DB tree has no
# system entries, but local scans still skip these).
SYSTEM_NAMES = {".keep", ".trash", ".versions"}

DEFAULT_TIMEOUT = 60
DOWNLOAD_CHUNK = 1024 * 1024  # 1 MiB
PAGE = 1000


class AuthError(Exception):
    pass


class ApiError(Exception):
    pass


def _encode_path(path: str) -> str:
    """URL-encode each segment of a storage path but keep the slashes."""
    return "/".join(urllib.parse.quote(seg, safe="") for seg in path.split("/"))


def _split_full(full_path: str):
    """'<account_id>/<rel...>' -> (account_id, rel)."""
    parts = full_path.split("/", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _rel_parent_name(rel: str):
    if "/" in rel:
        parent, name = rel.rsplit("/", 1)
    else:
        parent, name = "", rel
    return parent, name


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseClient:
    def __init__(self, access_token: Optional[str] = None,
                 refresh_token: Optional[str] = None):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.user_id: Optional[str] = None
        self._session = requests.Session()
        # rel -> files-table row, per walk()ed account. Lets download/delete/
        # upload find the storage key + row id for a tree path.
        self._index: Dict[str, Dict[str, Any]] = {}
        self._index_account: Optional[str] = None
        # folder paths that exist remotely (per indexed account)
        self._folders: set = set()

    # ---- auth ---------------------------------------------------------------
    def login(self, email: str, password: str) -> Dict[str, Any]:
        url = f"{SUPABASE_URL}/auth/v1/token?grant_type=password"
        r = self._session.post(
            url,
            headers={"apikey": ANON_KEY, "Content-Type": "application/json"},
            json={"email": email, "password": password},
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code != 200:
            raise AuthError(f"Login failed ({r.status_code}): {r.text}")
        data = r.json()
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        self.user_id = data.get("user", {}).get("id")
        return data

    def refresh(self) -> None:
        if not self.refresh_token:
            raise AuthError("No refresh token available; sign in again.")
        url = f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token"
        r = self._session.post(
            url,
            headers={"apikey": ANON_KEY, "Content-Type": "application/json"},
            json={"refresh_token": self.refresh_token},
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code != 200:
            raise AuthError(f"Token refresh failed ({r.status_code}): {r.text}")
        data = r.json()
        self.access_token = data["access_token"]
        # Supabase rotates the refresh token on each use.
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.user_id = data.get("user", {}).get("id") or self.user_id

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"apikey": ANON_KEY, "Authorization": f"Bearer {self.access_token}"}
        if extra:
            h.update(extra)
        return h

    # ---- request wrapper with auto-refresh + backoff ------------------------
    def _request(self, method: str, url: str, *, retries: int = 4,
                 stream: bool = False, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", {})
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            merged = self._headers(headers)
            try:
                r = self._session.request(
                    method, url, headers=merged, stream=stream,
                    timeout=kwargs.pop("timeout", DEFAULT_TIMEOUT), **kwargs
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                time.sleep(min(2 ** attempt, 30))
                continue

            if r.status_code == 401 and self.refresh_token:
                try:
                    self.refresh()
                    continue
                except AuthError:
                    return r
            if r.status_code in (429, 500, 502, 503, 504):
                last_exc = ApiError(f"{r.status_code}: {r.text[:200]}")
                time.sleep(min(2 ** attempt, 30))
                continue
            return r
        if last_exc:
            raise last_exc
        raise ApiError("Request failed after retries")

    # ---- small PostgREST helpers --------------------------------------------
    def _rest_get_all(self, table: str, query: str) -> List[Dict[str, Any]]:
        """GET all rows for a query, paginating with limit/offset."""
        out: List[Dict[str, Any]] = []
        offset = 0
        while True:
            url = (f"{SUPABASE_URL}/rest/v1/{table}?{query}"
                   f"&limit={PAGE}&offset={offset}")
            r = self._request("GET", url)
            if r.status_code != 200:
                raise ApiError(f"{table} query failed ({r.status_code}): {r.text[:200]}")
            batch = r.json()
            out.extend(batch)
            if len(batch) < PAGE:
                break
            offset += PAGE
        return out

    def _rest_insert(self, table: str, row: Dict[str, Any]) -> None:
        """INSERT with return=minimal.

        Important: we deliberately do NOT ask PostgREST to return the new row.
        The SELECT policy (can_see_file) runs as a STABLE function against the
        statement's starting snapshot, where the just-inserted row does not
        exist yet — so INSERT..RETURNING is denied even for owners. A follow-up
        GET in a fresh statement sees the row fine.
        """
        url = f"{SUPABASE_URL}/rest/v1/{table}"
        r = self._request(
            "POST", url,
            headers={"Content-Type": "application/json",
                     "Prefer": "return=minimal"},
            json=row,
        )
        if r.status_code not in (200, 201, 204):
            raise ApiError(f"{table} insert failed ({r.status_code}): {r.text[:200]}")

    def _rest_update(self, table: str, match: str, patch: Dict[str, Any]) -> None:
        url = f"{SUPABASE_URL}/rest/v1/{table}?{match}"
        r = self._request(
            "PATCH", url,
            headers={"Content-Type": "application/json"},
            json=patch,
        )
        if r.status_code not in (200, 204):
            raise ApiError(f"{table} update failed ({r.status_code}): {r.text[:200]}")

    # ---- workspace discovery -------------------------------------------------
    def list_memberships(self) -> List[Dict[str, Any]]:
        rows = self._rest_get_all(
            "account_members",
            f"select=account_id,role&user_id=eq.{self.user_id}",
        )
        # Keep the tenant_id key the rest of the app expects.
        return [{"tenant_id": r["account_id"], "role": r["role"]} for r in rows]

    def tenant_names(self, tenant_ids: List[str]) -> Dict[str, str]:
        if not tenant_ids:
            return {}
        ids = ",".join(tenant_ids)
        rows = self._rest_get_all("accounts", f"select=id,name&id=in.({ids})")
        return {row["id"]: row["name"] for row in rows}

    # ---- tree index (files + folders tables) ---------------------------------
    def _load_index(self, account_id: str) -> None:
        files = self._rest_get_all(
            "files",
            "select=id,name,parent,path,size,mime,created_at"
            f"&account_id=eq.{account_id}&deleted_at=is.null",
        )
        index: Dict[str, Dict[str, Any]] = {}
        for row in files:
            parent = row.get("parent") or ""
            rel = f"{parent}/{row['name']}" if parent else row["name"]
            index[rel] = row
        folders = self._rest_get_all(
            "folders",
            f"select=path&account_id=eq.{account_id}&deleted_at=is.null",
        )
        self._index = index
        self._index_account = account_id
        self._folders = {f["path"] for f in folders if f.get("path")}

    def _ensure_index(self, account_id: str) -> None:
        if self._index_account != account_id:
            self._load_index(account_id)

    def _ensure_folders(self, account_id: str, parent: str) -> None:
        """Create any missing folder rows for the ancestor chain of `parent`,
        so the web app's tree shows synced files in their real folders."""
        if not parent:
            return
        self._ensure_index(account_id)
        parts = parent.split("/")
        cur = ""
        for p in parts:
            prev = cur
            cur = f"{cur}/{p}" if cur else p
            if cur in self._folders:
                continue
            try:
                self._rest_insert("folders", {
                    "account_id": account_id,
                    "path": cur,
                    "name": p,
                    "parent": prev,
                })
            except ApiError as e:
                # A parallel device may have created it; duplicates are fine.
                if "duplicate" not in str(e).lower() and "23505" not in str(e):
                    raise
            self._folders.add(cur)

    # ---- engine interface ----------------------------------------------------
    def list_folder(self, prefix: str) -> List[Dict[str, Any]]:
        """One folder level, storage-listing shaped: folders have id=None.

        `prefix` is '<account_id>/' or '<account_id>/<sub>/'.
        """
        account_id, sub = _split_full(prefix.rstrip("/") + "/")
        sub = sub.rstrip("/")
        self._ensure_index(account_id)
        entries: List[Dict[str, Any]] = []
        seen_folders = set()
        for fpath in self._folders:
            parent, name = _rel_parent_name(fpath)
            if parent == sub and name not in seen_folders:
                seen_folders.add(name)
                entries.append({"name": name, "id": None})
        for rel, row in self._index.items():
            parent, name = _rel_parent_name(rel)
            if parent == sub:
                entries.append({
                    "name": name,
                    "id": row["id"],
                    "updated_at": row.get("created_at", ""),
                    "metadata": {"size": row.get("size") or 0},
                })
        entries.sort(key=lambda e: (e["id"] is not None, e["name"].lower()))
        return entries

    def walk(self, root_prefix: str) -> Dict[str, Dict[str, Any]]:
        """{relpath -> {"size","updated_at"}} for the whole visible tree.

        Fresh from the database each pass — so permission changes (a company
        granted or removed) show up on the next sync cycle.
        """
        account_id, sub = _split_full(root_prefix.rstrip("/"))
        self._load_index(account_id)
        result: Dict[str, Dict[str, Any]] = {}
        for rel, row in self._index.items():
            if sub:
                if not (rel == sub or rel.startswith(sub + "/")):
                    continue
                out_rel = rel[len(sub) + 1:] if rel != sub else rel.rsplit("/", 1)[-1]
            else:
                out_rel = rel
            if not out_rel:
                continue
            result[out_rel] = {
                "size": row.get("size") or 0,
                "updated_at": row.get("created_at", ""),
            }
        return result

    def _row_for_full(self, full_path: str) -> Optional[Dict[str, Any]]:
        account_id, rel = _split_full(full_path)
        self._ensure_index(account_id)
        return self._index.get(rel)

    def download(self, full_path: str, dest_path) -> None:
        """Stream a file to disk (looked up through the tree index)."""
        row = self._row_for_full(full_path)
        if not row:
            raise ApiError(f"download: no such remote file: {full_path}")
        key = row["path"]
        url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{_encode_path(key)}"
        r = self._request("GET", url, stream=True)
        if r.status_code != 200:
            raise ApiError(f"download failed ({r.status_code}) {full_path}: {r.text[:200]}")
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=DOWNLOAD_CHUNK):
                if chunk:
                    f.write(chunk)

    def _storage_put(self, src_path, key: str, content_type: str) -> None:
        import os as _os
        url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{_encode_path(key)}"
        size = _os.path.getsize(src_path)
        headers = {
            "x-upsert": "true",
            "Content-Type": content_type,
            "Content-Length": str(size),
        }
        last_exc: Optional[Exception] = None
        for attempt in range(4):
            try:
                with open(src_path, "rb") as body:
                    r = self._session.request(
                        "POST", url,
                        headers=self._headers(headers),
                        data=body,
                        timeout=max(DEFAULT_TIMEOUT, size // (256 * 1024) + DEFAULT_TIMEOUT),
                    )
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                time.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code == 401 and self.refresh_token:
                self.refresh()
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last_exc = ApiError(f"{r.status_code}: {r.text[:200]}")
                time.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code not in (200, 201):
                raise ApiError(f"upload failed ({r.status_code}) {key}: {r.text[:200]}")
            return
        if last_exc:
            raise last_exc
        raise ApiError(f"upload failed after retries: {key}")

    def _storage_delete(self, key: str) -> None:
        url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{_encode_path(key)}"
        r = self._request("DELETE", url)
        if r.status_code not in (200, 204, 404):
            raise ApiError(f"storage delete failed ({r.status_code}) {key}")

    def upload(self, src_path, full_path: str,
               content_type: str = "application/octet-stream") -> Dict[str, Any]:
        """Upload/replace one file: storage object + its tree row."""
        import os as _os
        account_id, rel = _split_full(full_path)
        parent, name = _rel_parent_name(rel)
        self._ensure_index(account_id)
        self._ensure_folders(account_id, parent)

        key = f"{account_id}/{_uuid.uuid4().hex}-{name}"
        self._storage_put(src_path, key, content_type)
        size = _os.path.getsize(src_path)

        existing = self._index.get(rel)
        if existing:
            old_key = existing.get("path")
            stamp = _now_iso()
            self._rest_update(
                "files", f"id=eq.{existing['id']}",
                {"path": key, "size": size, "mime": content_type,
                 "created_at": stamp},
            )
            existing.update({"path": key, "size": size, "created_at": stamp})
            if old_key and old_key != key:
                try:
                    self._storage_delete(old_key)
                except ApiError:
                    pass  # orphaned object is harmless; never fail the sync
        else:
            self._rest_insert("files", {
                "account_id": account_id,
                "name": name,
                "path": key,
                "size": size,
                "mime": content_type,
                "parent": parent,
            })
            # Fetch the new row in a fresh statement (see _rest_insert note).
            rows = self._rest_get_all(
                "files",
                "select=id,name,parent,path,size,mime,created_at"
                f"&account_id=eq.{account_id}"
                f"&parent=eq.{urllib.parse.quote(parent)}"
                f"&name=eq.{urllib.parse.quote(name)}&deleted_at=is.null"
                "&order=created_at.desc",
            )
            if rows:
                self._index[rel] = rows[0]
        return {}

    def delete(self, full_path: str) -> None:
        """Mirror a local delete by moving the remote file to the TRASH
        (deleted_at), exactly like deleting in the web app. Nothing is ever
        destroyed — it stays recoverable from the web app's trash."""
        account_id, rel = _split_full(full_path)
        self._ensure_index(account_id)
        row = self._index.get(rel)
        if not row:
            return
        self._rest_update("files", f"id=eq.{row['id']}",
                          {"deleted_at": _now_iso()})
        self._index.pop(rel, None)

    def stat_remote(self, root_prefix: str, relpath: str) -> Optional[Dict[str, Any]]:
        """{"size","updated_at"} for one remote file, or None."""
        account_id, sub = _split_full(root_prefix.rstrip("/"))
        rel = f"{sub}/{relpath}" if sub else relpath
        parent, name = _rel_parent_name(rel)
        rows = self._rest_get_all(
            "files",
            "select=id,size,created_at"
            f"&account_id=eq.{account_id}"
            f"&parent=eq.{urllib.parse.quote(parent)}"
            f"&name=eq.{urllib.parse.quote(name)}&deleted_at=is.null",
        )
        if not rows:
            return None
        row = rows[0]
        return {"size": row.get("size") or 0,
                "updated_at": row.get("created_at", "")}
