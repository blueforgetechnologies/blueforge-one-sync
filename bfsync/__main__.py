"""Entry point so `python -m bfsync` and the PyInstaller binary both work."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
