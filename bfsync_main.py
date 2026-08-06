"""Single-file entry point used by PyInstaller to build the --onefile binary.

Building from a module inside a package can confuse PyInstaller's import
graph; this thin top-level script imports the package's main() and runs it.
"""

import sys

from bfsync.cli import main

if __name__ == "__main__":
    sys.exit(main())
