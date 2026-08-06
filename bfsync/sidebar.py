"""Show the sync folder in the file-manager sidebar — like Dropbox / OneDrive.

Every major cloud app puts a branded, always-visible entry in the OS file
manager's left pane. We do the same, the *native* way on each OS — no wrapper,
no shortcut-in-a-folder hack:

  - Windows : registers a **navigation-pane namespace extension** — a CLSID under
              HKCU that File Explorer shows as a top-level "Blueforge One" root
              in the left pane, with our cloud logo, pointing at the sync folder.
              This is the exact mechanism Dropbox and OneDrive use. No admin.
  - macOS   : sets a custom Blueforge folder icon and adds the folder to the
              Finder sidebar favourites (best-effort via the OS APIs / `mysides`).
  - Linux   : adds a GTK bookmark so the folder shows in the Files/Nautilus pane.

Everything here is best-effort and side-effect-free until you call ``enable()`` /
``disable()``. Nothing ever raises out of the public functions — a file manager
that can't be decorated must never break syncing.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional, Tuple

DISPLAY_NAME = "Blueforge One"

# A FIXED, stable identifier for our nav-pane root. Being constant means running
# enable() twice updates the same entry instead of creating duplicates.
BRAND_GUID = "{BF01C10D-5A7E-4B2C-9D31-0BF0E17C10D5}"

# The stock Windows shell handler that makes a CLSID behave like a real folder.
_DELEGATE_FOLDER_CLSID = "{0E5AAE11-A475-4c5b-AB00-C66DE400274E}"


# ---------------------------------------------------------------------------
# platform + asset helpers
# ---------------------------------------------------------------------------
def _platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _asset(name: str) -> Optional[Path]:
    """Locate a bundled asset both when frozen (PyInstaller) and from source."""
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "bfsync" / "assets" / name)
        candidates.append(Path(base) / "assets" / name)
    candidates.append(Path(__file__).resolve().parent / "assets" / name)
    for c in candidates:
        if c.exists():
            return c
    return None


def _app_dir() -> Path:
    from .config import app_dir
    return app_dir()


def _materialize_icon(filename: str) -> Optional[Path]:
    """Copy a bundled icon to a STABLE path in the app dir and return it.

    The registry / Finder must point at a file that won't move when the app
    updates, so we keep our own copy under ~/.blueforge-one-sync/.
    """
    src = _asset(filename)
    if not src:
        return None
    dst = _app_dir() / filename
    try:
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copyfile(src, dst)
        return dst
    except Exception:
        return src  # fall back to the bundled copy in place


# ---------------------------------------------------------------------------
# what folder + label to pin
# ---------------------------------------------------------------------------
def target_for(local_folder: str) -> Tuple[str, str]:
    """Decide which folder to surface as the "Blueforge One" root.

    The default layout is  ~/Blueforge One/<workspace>/ .  When that's the
    case we pin the *container* so every workspace groups under one branded root
    (exactly like Dropbox). For a custom folder we pin the folder itself.
    Returns (folder_path, display_label).
    """
    p = Path(local_folder).expanduser()
    if p.parent.name == DISPLAY_NAME:
        return str(p.parent), DISPLAY_NAME
    return str(p), DISPLAY_NAME


# ===========================================================================
# Windows — navigation-pane namespace extension (HKCU, no admin)
# ===========================================================================
def _win_set(root, subkey: str, name: str, value, is_dword: bool = False) -> None:
    import winreg
    k = winreg.CreateKeyEx(root, subkey, 0, winreg.KEY_WRITE)
    try:
        typ = winreg.REG_DWORD if is_dword else winreg.REG_SZ
        winreg.SetValueEx(k, name, 0, typ, value)
    finally:
        winreg.CloseKey(k)


def _win_write_clsid(classes_clsid_root: str, folder: str, label: str,
                     icon: Optional[Path]) -> None:
    """Write the full CLSID definition under a given ...\\CLSID root
    (called once for the native tree and once for the Wow6432Node mirror)."""
    import winreg
    HKCU = winreg.HKEY_CURRENT_USER
    base = rf"{classes_clsid_root}\{BRAND_GUID}"

    _win_set(HKCU, base, "", label)                       # display name
    _win_set(HKCU, base, "InfoTip", "Your Blueforge One files")
    _win_set(HKCU, base, "System.IsPinnedToNameSpaceTree", 1, is_dword=True)
    _win_set(HKCU, base, "SortOrderIndex", 0x42, is_dword=True)

    if icon:
        _win_set(HKCU, rf"{base}\DefaultIcon", "", f"{icon},0")

    _win_set(HKCU, rf"{base}\Instance", "CLSID", _DELEGATE_FOLDER_CLSID)
    _win_set(HKCU, rf"{base}\Instance\InitPropertyBag",
             "Attributes", 0x11, is_dword=True)
    _win_set(HKCU, rf"{base}\Instance\InitPropertyBag",
             "TargetFolderPath", folder)

    _win_set(HKCU, rf"{base}\ShellFolder", "Attributes", 0xF080004D, is_dword=True)
    _win_set(HKCU, rf"{base}\ShellFolder", "FolderValueFlags", 0x28, is_dword=True)


def _win_enable(folder: str, label: str) -> bool:
    import ctypes
    import winreg
    HKCU = winreg.HKEY_CURRENT_USER

    Path(folder).mkdir(parents=True, exist_ok=True)
    icon = _materialize_icon("blueforge.ico")

    # 1) The CLSID itself — native + 32-bit mirror so 64-bit Explorer sees it.
    _win_write_clsid(r"Software\Classes\CLSID", folder, label, icon)
    _win_write_clsid(r"Software\Classes\Wow6432Node\CLSID", folder, label, icon)

    # 2) Place it in the navigation pane.
    _win_set(HKCU,
             rf"Software\Microsoft\Windows\CurrentVersion\Explorer\Desktop\NameSpace\{BRAND_GUID}",
             "", label)

    # 3) Keep it OFF the desktop (nav pane only, like Dropbox).
    for panel in ("NewStartPanel", "ClassicStartMenu"):
        _win_set(HKCU,
                 rf"Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\{panel}",
                 BRAND_GUID, 1, is_dword=True)

    # 4) Tell Explorer to refresh so it appears without a reboot.
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:
        pass
    return True


def _win_delete_tree(root, subkey: str) -> None:
    import winreg
    try:
        k = winreg.OpenKey(root, subkey, 0, winreg.KEY_ALL_ACCESS)
    except FileNotFoundError:
        return
    try:
        while True:
            try:
                child = winreg.EnumKey(k, 0)
            except OSError:
                break
            _win_delete_tree(root, subkey + "\\" + child)
        winreg.CloseKey(k)
        winreg.DeleteKey(root, subkey)
    except Exception:
        try:
            winreg.CloseKey(k)
        except Exception:
            pass


def _win_disable() -> None:
    import ctypes
    import winreg
    HKCU = winreg.HKEY_CURRENT_USER
    _win_delete_tree(HKCU, rf"Software\Classes\CLSID\{BRAND_GUID}")
    _win_delete_tree(HKCU, rf"Software\Classes\Wow6432Node\CLSID\{BRAND_GUID}")
    _win_delete_tree(HKCU,
                     rf"Software\Microsoft\Windows\CurrentVersion\Explorer\Desktop\NameSpace\{BRAND_GUID}")
    for panel in ("NewStartPanel", "ClassicStartMenu"):
        try:
            k = winreg.OpenKey(
                HKCU,
                rf"Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\{panel}",
                0, winreg.KEY_ALL_ACCESS)
            winreg.DeleteValue(k, BRAND_GUID)
            winreg.CloseKey(k)
        except Exception:
            pass
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:
        pass


def _win_is_enabled() -> bool:
    import winreg
    try:
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Microsoft\Windows\CurrentVersion\Explorer\Desktop\NameSpace\{BRAND_GUID}")
        winreg.CloseKey(k)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


# ===========================================================================
# macOS — custom folder icon + Finder sidebar favourite (best-effort)
# ===========================================================================
def _mac_enable(folder: str, label: str) -> bool:
    Path(folder).expanduser().mkdir(parents=True, exist_ok=True)
    ok = False
    # (a) Give the folder our cloud icon.
    png = _materialize_icon("blueforge.png")
    if png:
        try:
            from AppKit import NSImage, NSWorkspace  # type: ignore
            img = NSImage.alloc().initWithContentsOfFile_(str(png))
            if img is not None:
                NSWorkspace.sharedWorkspace().setIcon_forFile_options_(
                    img, str(Path(folder).expanduser()), 0)
                ok = True
        except Exception:
            pass
    # (b) Add to the Finder sidebar favourites, if `mysides` is available.
    try:
        import subprocess
        if shutil.which("mysides"):
            uri = "file://" + str(Path(folder).expanduser()) + "/"
            subprocess.run(["mysides", "add", label, uri],
                           check=False, capture_output=True)
            ok = True
    except Exception:
        pass
    return ok


def _mac_disable() -> None:
    try:
        import subprocess
        if shutil.which("mysides"):
            subprocess.run(["mysides", "remove", DISPLAY_NAME],
                           check=False, capture_output=True)
    except Exception:
        pass


# ===========================================================================
# Linux — GTK bookmark (Nautilus / Files side pane)
# ===========================================================================
def _linux_bookmarks_file() -> Path:
    cfg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(cfg) if cfg else (Path.home() / ".config")
    return base / "gtk-3.0" / "bookmarks"


def _linux_enable(folder: str, label: str) -> bool:
    try:
        f = _linux_bookmarks_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        uri = "file://" + str(Path(folder).expanduser())
        line = f"{uri} {label}"
        existing = f.read_text(encoding="utf-8").splitlines() if f.exists() else []
        if any(l.split(" ")[0] == uri for l in existing):
            return True
        existing.append(line)
        f.write_text("\n".join(existing) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


def _linux_disable() -> None:
    try:
        f = _linux_bookmarks_file()
        if not f.exists():
            return
        keep = [l for l in f.read_text(encoding="utf-8").splitlines()
                if DISPLAY_NAME not in l and "Blueforge One" not in l]
        f.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    except Exception:
        pass


# ===========================================================================
# public API
# ===========================================================================
def enable(local_folder: str) -> bool:
    """Add the branded Blueforge One entry to this OS's file-manager sidebar."""
    folder, label = target_for(local_folder)
    try:
        plat = _platform()
        if plat == "windows":
            return _win_enable(folder, label)
        if plat == "macos":
            return _mac_enable(folder, label)
        return _linux_enable(folder, label)
    except Exception:
        return False


def disable() -> None:
    """Remove the Blueforge One sidebar entry."""
    try:
        plat = _platform()
        if plat == "windows":
            _win_disable()
        elif plat == "macos":
            _mac_disable()
        else:
            _linux_disable()
    except Exception:
        pass


def is_enabled() -> bool:
    try:
        if _platform() == "windows":
            return _win_is_enabled()
    except Exception:
        return False
    return False  # mac/linux favourites aren't reliably queryable; treat as unknown


def ensure(local_folder: Optional[str]) -> None:
    """Idempotently make sure the sidebar entry exists. Safe to call every launch;
    self-heals if the user cleared it. No-op without a configured folder."""
    if not local_folder:
        return
    try:
        if _platform() == "windows" and _win_is_enabled():
            return
        enable(local_folder)
    except Exception:
        pass


def describe() -> str:
    plat = _platform()
    where = {"windows": "File Explorer sidebar",
             "macos": "Finder sidebar",
             "linux": "Files side pane"}.get(plat, "sidebar")
    state = "ON" if is_enabled() else ("on" if plat != "windows" else "off")
    return f"Sidebar entry ({where}): {state}"
