"""Entry point of the packaged app: no arguments starts the desktop app; `selftest` checks the bundle."""
import multiprocessing
import sys

from brainscope.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
