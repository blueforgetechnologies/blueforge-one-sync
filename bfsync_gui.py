"""Top-level entry point PyInstaller bundles into the windowed desktop app.

Building from a module inside a package can confuse PyInstaller's import graph,
so this thin top-level script imports the package and launches the tray app.

It still honours a couple of command-line args so CI (and power users) can poke
the frozen binary without a screen:
    <binary> --version     print version and exit
    <binary> selftest      verify the GUI stack loads on this OS, then exit
    <binary> [app]         launch the desktop tray app (default)
"""

import sys


def main() -> int:
    from bfsync import __version__

    argv = sys.argv[1:]
    if argv and argv[0] in ("--version", "-V", "version"):
        try:
            if sys.stdout is not None:
                sys.stdout.write(f"Blueforge One Sync {__version__}\n")
                sys.stdout.flush()
        except Exception:
            pass
        return 0
    if argv and argv[0] == "selftest":
        from bfsync.desktop import selftest
        return selftest()
    from bfsync.desktop import run
    return run()


if __name__ == "__main__":
    sys.exit(main())
