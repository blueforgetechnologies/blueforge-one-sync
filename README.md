# Blueforge One Sync

Keep a folder on your computer automatically in sync with your **Blueforge One Cloud**
workspace — like Dropbox, but for Blueforge One. It talks directly and securely
to the cloud over HTTPS. No third-party software, no browser needed.

It ships as a real **desktop app**: a small cloud icon lives in your menu bar
(Mac) / system tray (Windows), quietly keeping your folder in sync and starting
itself every time you log in.

---

## What it does

- **A finished desktop app.** Install it, sign in once in a friendly window, pick
  a folder — done. A cloud icon sits in your menu bar / tray and shows status at a
  glance (green = up to date, blue = syncing, amber = offline, red = needs you).
- **Two-way sync.** Add or change a file on your computer and it uploads. Add or
  change a file in the cloud and it downloads. Delete on one side and the delete
  is mirrored. Instant on local edits (file-watcher), plus a safety poll of the
  cloud.
- **Lives in your file-manager sidebar.** Setup adds a branded **Blueforge One**
  entry to the File Explorer navigation pane (Windows) / Finder sidebar (Mac),
  with the cloud logo — exactly like Dropbox and OneDrive. On Windows this is a
  real navigation-pane *namespace extension* (HKCU, no admin), not a shortcut.
  Re-add it any time from the tray menu, or `bfsync sidebar`.
- **Starts at login.** Turn on "Start at login" and it's always running, just like
  Dropbox — no admin rights needed.
- **Handles conflicts safely.** If the same file changes in both places at once,
  it keeps **both** copies (one renamed `… (conflict <date-time>)`) so nothing is
  ever lost.
- **Big files welcome.** Streams files up to 500 MB straight from disk without
  eating your memory.
- **Native notifications.** A toast when a batch syncs or a conflict is kept.
- **Secure.** Your password is never stored — only a refreshable login token in a
  protected file on your own machine.

---

## For the owner: install it (the easy way)

Once the automated build finishes on GitHub, you get a ready-to-install app.

### Windows

1. Repo's **Actions** tab → latest **"Build Blueforge One Sync"** run →
   **Artifacts** → download **`bfsync-windows-installer`**.
2. Unzip and run **`Blueforge One Sync Setup.exe`**. (Windows SmartScreen may
   warn about an unknown publisher — click *More info → Run anyway*. Code-signing
   removes this later; see Roadmap.)
3. The app opens. **Sign in**, **pick a workspace**, **choose a folder**, tick
   *Start when I log in*, and click **Finish**. That's it.

Drop files into that folder and they appear in the cloud; files added in the
cloud appear in your folder. The tray icon (bottom-right, may be under the "▲")
is your control panel.

### Mac

1. Download **`bfsync-macos-dmg`**, open **`Blueforge One Sync.dmg`**, and drag
   the app into **Applications**.
2. First launch: right-click the app → **Open** (macOS warns about an unsigned app
   once — Notarization arrives in the Roadmap).
3. Same friendly setup: sign in → workspace → folder → Finish. The cloud icon
   appears in your **menu bar** (top-right).

**Do I have to register anywhere?** No. Nothing to sign up for, nothing to wait
for approval on. The only optional extra later is a code-signing certificate,
which just removes the "unknown developer" warning — the app is 100% functional
without it, and adding it later needs **zero** rework.

---

## For developers / power users: run from source

```bash
python -m pip install -r requirements.txt

python -m bfsync app         # launch the desktop tray app
python -m bfsync selftest    # verify the GUI stack loads on this OS

# headless / scripting mode (no GUI):
python -m bfsync login
python -m bfsync setup
python -m bfsync sync        # one pass
python -m bfsync watch       # keep syncing (foreground)
python -m bfsync status
python -m bfsync logout
```

### Commands

| Command    | What it does |
|------------|--------------|
| `app`      | Launch the desktop tray app (wizard on first run, then the tray). |
| `selftest` | Confirm the tray + Pillow + tkinter + autostart stack loads on this OS. |
| `login`    | Sign in with email + password; stores only a refresh token. |
| `setup`    | Choose which workspace to sync and which local folder to use. |
| `sync`     | Run exactly one sync pass, then exit. |
| `watch`    | Keep syncing: instant local detection (watchdog) + cloud poll. |
| `status`   | Print the current configuration. |
| `logout`   | Delete the stored config, tokens, and local state manifest. |

### Where things live

- `~/.blueforge-one-sync/config.json` — your settings + refresh token
  (permissions locked to you; **never** committed).
- `~/.blueforge-one-sync/state-<workspace-id>.json` — the sync manifest.
- `~/.blueforge-one-sync/sync.log` — a rolling log of what synced (handy for
  support).
- Run-at-login entry:
  - Windows: a shortcut in the Startup folder.
  - macOS: `~/Library/LaunchAgents/com.blueforge.cloudsync.plist`.
  - Linux: `~/.config/autostart/blueforge-one-sync.desktop`.

---

## How the sync decides what to do

Each pass lists the cloud tree, scans your local folder, and compares both
against the last-known **manifest**:

- New locally, not in cloud → **upload**
- New in cloud, not local → **download**
- Changed only locally → **upload**
- Changed only in cloud → **download**
- Changed in **both** → **conflict**: keep both copies
- Deleted on one side (was previously synced) → mirror the **delete**

Conservative and correct over clever — it never guesses in a way that could lose
your data.

---

## Project layout

```
cloud-sync/
├── bfsync/                     # the app (a Python package)
│   ├── api.py                  # direct Supabase Auth + Storage HTTPS client
│   ├── config.py               # config + state manifest persistence
│   ├── engine.py               # two-way reconciliation logic
│   ├── runtime.py              # shared thread-safe app state
│   ├── worker.py               # background sync thread (pause/resume/sync-now)
│   ├── wizard.py               # tkinter first-run wizard + status window
│   ├── tray.py                 # menu-bar / system-tray icon (pystray)
│   ├── autostart.py            # run-at-login, per OS
│   ├── desktop.py              # the tray-app orchestrator (main thread)
│   └── cli.py                  # login / setup / sync / watch / app / selftest
├── bfsync_gui.py               # PyInstaller entry for the windowed app
├── bfsync_main.py              # PyInstaller entry for the CLI binary
├── requirements.txt
└── .github/workflows/build.yml # builds Win installer + macOS .dmg + CLI
```

The engine + worker are UI-free, so the tray and wizard sit cleanly on top.

---

## Roadmap

- **Phase 1:** two-way sync engine + cross-platform CLI builds. ✅
- **Phase 2 (this):** desktop tray/menu-bar app, first-run wizard, run-at-login,
  native notifications, one-click Windows installer + macOS .dmg. ✅
- **Phase 3 (optional polish):** code-signing + notarization (removes the
  "unknown developer" warning — needs an Apple Developer account + a Windows
  cert), and auto-update.
