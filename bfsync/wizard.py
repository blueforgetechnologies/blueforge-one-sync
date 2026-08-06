"""Tkinter UI for Blueforge One Sync — first-run wizard + status window.

Design constraints that keep it robust on every OS:
  - tkinter is only imported here, and only called from the MAIN thread by the
    desktop orchestrator. We never run a Tk mainloop at the same time as the
    tray's event loop (that is unsafe on macOS). Each function creates its own
    Tk root, runs a mainloop, then fully destroys it before returning.
  - Network calls (login, workspace lookup) run on a helper thread and post
    results back with ``root.after`` so the window never freezes.

Public entry points (all main-thread, blocking):
  - ``run_setup_wizard()``  -> bool   (True if configured & saved)
  - ``run_status_window()`` -> str    ('' | 'reconfigure' | 'signout' | 'quit')
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import List, Optional, Tuple

from .api import AuthError, SupabaseClient
from .config import Config

BRAND = "#2563eb"        # Blueforge blue
BRAND_DARK = "#1d4ed8"
INK = "#0f172a"
MUTE = "#64748b"
BG = "#f8fafc"
CARD = "#ffffff"
WEB_URL = "https://blueforge-one.vercel.app"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _center(win, w: int, h: int) -> None:
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 3)
    win.geometry(f"{w}x{h}+{x}+{y}")


def _style(root):
    from tkinter import ttk
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except Exception:
        pass
    st.configure("TFrame", background=BG)
    st.configure("Card.TFrame", background=CARD)
    st.configure("TLabel", background=BG, foreground=INK, font=("Segoe UI", 10))
    st.configure("Card.TLabel", background=CARD, foreground=INK, font=("Segoe UI", 10))
    st.configure("H1.TLabel", background=BG, foreground=INK, font=("Segoe UI", 17, "bold"))
    st.configure("H1c.TLabel", background=CARD, foreground=INK, font=("Segoe UI", 17, "bold"))
    st.configure("Sub.TLabel", background=BG, foreground=MUTE, font=("Segoe UI", 10))
    st.configure("Subc.TLabel", background=CARD, foreground=MUTE, font=("Segoe UI", 10))
    st.configure("Err.TLabel", background=CARD, foreground="#dc2626", font=("Segoe UI", 9))
    st.configure("Accent.TButton", font=("Segoe UI", 10, "bold"),
                 foreground="#ffffff", background=BRAND, borderwidth=0, padding=(16, 9))
    st.map("Accent.TButton",
           background=[("active", BRAND_DARK), ("disabled", "#93c5fd")])
    st.configure("Ghost.TButton", font=("Segoe UI", 10), padding=(12, 7))
    st.configure("TEntry", padding=6)
    st.configure("TRadiobutton", background=CARD, foreground=INK, font=("Segoe UI", 10))
    return st


def _header(parent):
    from tkinter import ttk
    import tkinter as tk
    bar = tk.Frame(parent, bg=BRAND, height=64)
    bar.pack(fill="x", side="top")
    bar.pack_propagate(False)
    tk.Label(bar, text="☁  Blueforge One Sync", bg=BRAND, fg="#ffffff",
             font=("Segoe UI", 14, "bold")).pack(side="left", padx=18)
    return bar


# ---------------------------------------------------------------------------
# background network calls (posted back to the UI thread)
# ---------------------------------------------------------------------------
def _run_async(root, fn, on_done):
    """Run fn() on a thread; call on_done(result, error) on the UI thread."""
    def worker():
        try:
            res = fn()
            root.after(0, lambda: on_done(res, None))
        except Exception as e:  # noqa: BLE001
            root.after(0, lambda: on_done(None, e))
    threading.Thread(target=worker, daemon=True).start()


def _candidate_folders(client, remote_prefix, max_depth=4):
    """Return the folders a user should choose between for Selective Sync.

    Descends through single-child folders (so a workspace whose only top folder
    is 'Dropbox' offers the folders *inside* Dropbox) until it reaches a level
    with a real choice, and returns those folder paths relative to
    remote_prefix — e.g. ['Dropbox/1- ADMIN', 'Dropbox/3- Driver Files', ...].
    Returns [] for a flat workspace (nothing meaningful to choose)."""
    from .api import SYSTEM_NAMES
    root = remote_prefix.rstrip("/")
    base = ""
    folders = []
    for _ in range(max_depth):
        prefix = root + "/" + (base + "/" if base else "")
        folders, files = [], 0
        for e in client.list_folder(prefix):
            name = e.get("name")
            if not name or name in SYSTEM_NAMES:
                continue
            if e.get("id") is None:
                folders.append(name)
            else:
                files += 1
        if not folders:
            return []
        if len(folders) == 1 and files == 0:
            base = (base + "/" if base else "") + folders[0]
            continue
        break
    prefix_base = (base + "/") if base else ""
    return [prefix_base + f for f in folders]


# ---------------------------------------------------------------------------
# The setup wizard
# ---------------------------------------------------------------------------
def run_setup_wizard() -> bool:
    import tkinter as tk
    from tkinter import ttk, filedialog

    root = tk.Tk()
    root.title("Set up Blueforge One Sync")
    root.configure(bg=BG)
    _style(root)
    _center(root, 460, 520)
    try:
        root.attributes("-topmost", True)
        root.after(400, lambda: root.attributes("-topmost", False))
    except Exception:
        pass

    state = {
        "client": None,       # SupabaseClient after login
        "cfg": Config.load(),
        "memberships": [],    # list of (tenant_id, name)
        "tenant_id": None,
        "tenant_name": "",
        "result": False,
    }

    _header(root)
    body = ttk.Frame(root, style="TFrame")
    body.pack(fill="both", expand=True, padx=22, pady=18)

    # ---- step 1: login -----------------------------------------------------
    def clear():
        for w in body.winfo_children():
            w.destroy()

    def show_login(prefill_email: str = ""):
        clear()
        ttk.Label(body, text="Sign in", style="H1.TLabel").pack(anchor="w")
        ttk.Label(body, text="Use your Blueforge One Cloud email and password.",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 16))

        ttk.Label(body, text="Email").pack(anchor="w")
        email = ttk.Entry(body, width=40)
        email.pack(fill="x", pady=(2, 12))
        email.insert(0, prefill_email or state["cfg"].email or "")

        ttk.Label(body, text="Password").pack(anchor="w")
        pw = ttk.Entry(body, width=40, show="•")
        pw.pack(fill="x", pady=(2, 6))

        err = ttk.Label(body, text="", style="Err.TLabel")
        err.pack(anchor="w", pady=(2, 8))

        btn = ttk.Button(body, text="Sign in", style="Accent.TButton")
        btn.pack(fill="x", pady=(8, 0))

        def do_login(_evt=None):
            e = email.get().strip()
            p = pw.get()
            if not e or not p:
                err.configure(text="Please enter your email and password.")
                return
            btn.configure(text="Signing in…", state="disabled")
            err.configure(text="")

            def login():
                c = SupabaseClient()
                c.login(e, p)
                c.user_id = c.user_id  # ensure set
                return c

            def done(client, error):
                if error is not None:
                    btn.configure(text="Sign in", state="normal")
                    msg = "That email or password didn't work."
                    if not isinstance(error, AuthError):
                        msg = "Couldn't reach the server. Check your internet."
                    err.configure(text=msg)
                    return
                state["client"] = client
                state["cfg"].email = e
                state["cfg"].refresh_token = client.refresh_token
                state["cfg"].save()
                load_workspaces()

            _run_async(root, login, done)

        btn.configure(command=do_login)
        root.bind("<Return>", do_login)
        email.focus_set()

    def load_workspaces():
        clear()
        ttk.Label(body, text="Loading your workspaces…", style="Sub.TLabel").pack(pady=40)

        def fetch():
            c = state["client"]
            members = c.list_memberships()
            ids = [m["tenant_id"] for m in members]
            names = c.tenant_names(ids)
            return [(tid, names.get(tid, "(workspace)")) for tid in ids]

        def done(rows, error):
            if error is not None:
                show_login()
                return
            state["memberships"] = rows
            show_workspace()

        _run_async(root, fetch, done)

    # ---- step 2: pick workspace -------------------------------------------
    def show_workspace():
        clear()
        rows = state["memberships"]
        if not rows:
            ttk.Label(body, text="No workspaces found", style="H1.TLabel").pack(anchor="w")
            ttk.Label(body, text="This account isn't a member of any workspace yet. "
                                 "Ask your admin to invite you, then try again.",
                      style="Sub.TLabel", wraplength=400).pack(anchor="w", pady=8)
            ttk.Button(body, text="Back", style="Ghost.TButton",
                       command=lambda: show_login(state["cfg"].email)).pack(anchor="w", pady=12)
            return

        ttk.Label(body, text="Choose a workspace", style="H1.TLabel").pack(anchor="w")
        ttk.Label(body, text="This is the workspace whose files will sync to your computer.",
                  style="Sub.TLabel", wraplength=400).pack(anchor="w", pady=(2, 14))

        sel = tk.StringVar(value=rows[0][0])
        card = ttk.Frame(body, style="Card.TFrame")
        card.pack(fill="x")
        for tid, name in rows:
            ttk.Radiobutton(card, text=f"  {name}", value=tid, variable=sel,
                            style="TRadiobutton").pack(anchor="w", padx=10, pady=6)

        nxt = ttk.Button(body, text="Continue", style="Accent.TButton")
        nxt.pack(fill="x", pady=(18, 0))

        def go():
            tid = sel.get()
            state["tenant_id"] = tid
            state["tenant_name"] = dict(rows).get(tid, "")
            show_folder()

        nxt.configure(command=go)

    # ---- step 3: choose folder --------------------------------------------
    def show_folder():
        clear()
        ttk.Label(body, text="Pick your sync folder", style="H1.TLabel").pack(anchor="w")
        ttk.Label(body, text="Files here stay in sync with your workspace — both ways, "
                             "automatically.", style="Sub.TLabel",
                  wraplength=400).pack(anchor="w", pady=(2, 14))

        default = str(Path.home() / "Blueforge One" / (state["tenant_name"] or "Workspace"))

        path_var = tk.StringVar(value=state["cfg"].local_folder or default)
        rowf = ttk.Frame(body, style="TFrame")
        rowf.pack(fill="x")
        entry = ttk.Entry(rowf, textvariable=path_var)
        entry.pack(side="left", fill="x", expand=True)

        def browse():
            chosen = filedialog.askdirectory(initialdir=str(Path.home()),
                                             title="Choose a sync folder")
            if chosen:
                path_var.set(chosen)

        ttk.Button(rowf, text="Browse…", style="Ghost.TButton",
                   command=browse).pack(side="left", padx=(8, 0))

        auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text="  Start Blueforge One Sync when I log in",
                        variable=auto_var).pack(anchor="w", pady=(16, 0))

        err = ttk.Label(body, text="", style="Err.TLabel")
        err.pack(anchor="w", pady=(6, 0))

        done_btn = ttk.Button(body, text="Continue", style="Accent.TButton")
        done_btn.pack(fill="x", pady=(18, 0))

        def proceed():
            folder = path_var.get().strip()
            if not folder:
                err.configure(text="Please choose a folder.")
                return
            try:
                Path(folder).expanduser().mkdir(parents=True, exist_ok=True)
            except Exception:
                err.configure(text="That folder can't be created. Pick another.")
                return
            state["local_folder"] = str(Path(folder).expanduser())
            state["auto"] = bool(auto_var.get())
            show_selective()

        done_btn.configure(command=proceed)

    # ---- step 4: choose what syncs to THIS computer (Selective Sync) --------
    def _save_and_finish(sync_all, candidates, selected):
        cfg = state["cfg"]
        cfg.tenant_id = state["tenant_id"]
        cfg.tenant_name = state["tenant_name"]
        cfg.local_folder = state["local_folder"]
        cfg.sync_all = bool(sync_all)
        cfg.sync_candidates = list(candidates)
        cfg.sync_folders = [] if sync_all else list(selected)
        cfg.save()
        if state.get("auto"):
            try:
                from . import autostart
                autostart.enable()
            except Exception:
                pass
        # Branded Blueforge One entry in the file-manager sidebar (like Dropbox).
        try:
            from . import sidebar
            sidebar.enable(cfg.local_folder)
        except Exception:
            pass
        state["result"] = True
        root.destroy()

    def show_selective():
        clear()
        ttk.Label(body, text="Choose what syncs here", style="H1.TLabel").pack(anchor="w")
        ttk.Label(body, text="Everything is on by default. Untick any folders you don't "
                             "want copied onto THIS computer — handy for a laptop with "
                             "limited space. You can change this anytime.",
                  style="Sub.TLabel", wraplength=400).pack(anchor="w", pady=(2, 12))
        loading = ttk.Label(body, text="Reading your workspace folders…", style="Sub.TLabel")
        loading.pack(anchor="w", pady=8)

        def fetch():
            client = state["client"]
            rr = (state["cfg"].remote_root or "").strip("/")
            remote_prefix = (state["tenant_id"] + "/" + rr) if rr else state["tenant_id"]
            return _candidate_folders(client, remote_prefix)

        def done(cands, error):
            try:
                loading.destroy()
            except Exception:
                pass
            if error is not None or not cands:
                ttk.Label(body, text="This workspace is one set of files — it will all "
                                     "sync. You're ready to go.", style="Sub.TLabel",
                          wraplength=400).pack(anchor="w", pady=6)
                ttk.Button(body, text="Finish & start syncing", style="Accent.TButton",
                           command=lambda: _save_and_finish(True, cands or [], [])
                           ).pack(fill="x", pady=(16, 0))
                return
            _render_folder_checklist(cands)

        _run_async(root, fetch, done)

    def _render_folder_checklist(cands):
        all_var = tk.BooleanVar(value=True)
        vars_by = {c: tk.BooleanVar(value=True) for c in cands}

        top = ttk.Frame(body, style="TFrame"); top.pack(fill="x", anchor="w")
        canvas = tk.Canvas(body, height=220, highlightthickness=0, bg=CARD)
        sbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, style="Card.TFrame")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sbar.set)

        checks = []
        for c in cands:
            cb = ttk.Checkbutton(inner, text="  " + c.split("/")[-1], variable=vars_by[c])
            cb.pack(anchor="w", padx=8, pady=3)
            checks.append(cb)

        def render_children():
            st = "disabled" if all_var.get() else "normal"
            for cb in checks:
                cb.configure(state=st)

        ttk.Checkbutton(top, text="  Sync everything (recommended)", variable=all_var,
                        command=render_children).pack(anchor="w", pady=(0, 8))
        canvas.pack(fill="x", side="top")
        sbar.place(in_=canvas, relx=1.0, rely=0, relheight=1.0, anchor="ne")
        render_children()

        def finish():
            if all_var.get():
                _save_and_finish(True, cands, [])
            else:
                selected = [c for c in cands if vars_by[c].get()]
                _save_and_finish(False, cands, selected)
        ttk.Button(body, text="Finish & start syncing", style="Accent.TButton",
                   command=finish).pack(fill="x", pady=(14, 0))

    # already logged in? skip to workspace step
    if state["cfg"].is_logged_in():
        try:
            c = SupabaseClient(refresh_token=state["cfg"].refresh_token)
            c.refresh()
            state["client"] = c
            state["cfg"].refresh_token = c.refresh_token
            state["cfg"].save()
            load_workspaces()
        except Exception:
            show_login(state["cfg"].email)
    else:
        show_login()

    root.mainloop()
    return bool(state["result"])


# ---------------------------------------------------------------------------
# The status / settings window
# ---------------------------------------------------------------------------
def run_status_window(state_obj) -> str:
    """Show current status + controls. Returns a follow-up action for the
    orchestrator: '' (just close), 'reconfigure', 'signout', or 'quit'."""
    import tkinter as tk
    from tkinter import ttk

    from . import autostart

    root = tk.Tk()
    root.title("Blueforge One Sync")
    root.configure(bg=BG)
    _style(root)
    _center(root, 440, 460)

    action = {"value": ""}
    _header(root)
    body = ttk.Frame(root, style="TFrame")
    body.pack(fill="both", expand=True, padx=22, pady=16)

    snap = state_obj.snapshot()
    ttk.Label(body, text=snap.get("workspace_name") or "Workspace",
              style="H1.TLabel").pack(anchor="w")
    status_lbl = ttk.Label(body, text="", style="Sub.TLabel")
    status_lbl.pack(anchor="w", pady=(2, 12))

    info = ttk.Frame(body, style="Card.TFrame")
    info.pack(fill="x", pady=(0, 12))
    rowvals = [
        ("Signed in as", snap.get("email") or "—"),
        ("Sync folder", snap.get("local_folder") or "—"),
    ]
    for k, v in rowvals:
        r = ttk.Frame(info, style="Card.TFrame")
        r.pack(fill="x", padx=12, pady=6)
        ttk.Label(r, text=k, style="Subc.TLabel", width=14).pack(side="left")
        ttk.Label(r, text=v, style="Card.TLabel", wraplength=260).pack(side="left")

    totals_lbl = ttk.Label(body, text="", style="Sub.TLabel")
    totals_lbl.pack(anchor="w", pady=(0, 10))

    # auto-start toggle
    auto_var = tk.BooleanVar(value=autostart.is_enabled())

    def toggle_auto():
        try:
            if auto_var.get():
                autostart.enable()
            else:
                autostart.disable()
        except Exception:
            pass

    ttk.Checkbutton(body, text="  Start when I log in", variable=auto_var,
                    command=toggle_auto).pack(anchor="w", pady=(0, 12))

    # buttons
    btns = ttk.Frame(body, style="TFrame")
    btns.pack(fill="x")

    def open_folder():
        _open_path(snap.get("local_folder") or "")

    def open_web():
        import webbrowser
        webbrowser.open(WEB_URL)

    def reconfigure():
        action["value"] = "reconfigure"
        root.destroy()

    def signout():
        action["value"] = "signout"
        root.destroy()

    ttk.Button(btns, text="Open folder", style="Ghost.TButton",
               command=open_folder).grid(row=0, column=0, padx=(0, 6), pady=4, sticky="ew")
    ttk.Button(btns, text="Open website", style="Ghost.TButton",
               command=open_web).grid(row=0, column=1, padx=6, pady=4, sticky="ew")
    ttk.Button(btns, text="Change folder / workspace", style="Ghost.TButton",
               command=reconfigure).grid(row=1, column=0, columnspan=2, pady=4, sticky="ew")
    ttk.Button(btns, text="Sign out", style="Ghost.TButton",
               command=signout).grid(row=2, column=0, columnspan=2, pady=4, sticky="ew")
    btns.columnconfigure(0, weight=1)
    btns.columnconfigure(1, weight=1)

    ttk.Button(body, text="Close", style="Accent.TButton",
               command=root.destroy).pack(fill="x", pady=(14, 0))

    def refresh():
        s = state_obj.snapshot()
        # While syncing, show the live "Syncing 1,200 of 11,042…" detail;
        # otherwise the plain status label. Always show when it last finished.
        headline = s["detail"] if s["status"] == "syncing" else state_obj.status_label()
        status_lbl.configure(text=f"{headline} · last sync {state_obj.last_sync_text()}")
        t = s["totals"]
        totals_lbl.configure(
            text=(f"This session: {t['uploaded']} uploaded · {t['downloaded']} downloaded"
                  f" · {t['conflicts']} conflicts kept"))
        root.after(1000, refresh)

    refresh()
    root.mainloop()
    return action["value"]


# ---------------------------------------------------------------------------
def _open_path(path: str) -> None:
    import subprocess
    import sys
    if not path:
        return
    try:
        if sys.platform.startswith("win"):
            import os
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass
