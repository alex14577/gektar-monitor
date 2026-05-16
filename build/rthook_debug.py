"""PyInstaller runtime hook for the debug build variant.

Sets FIS_LOG_LEVEL_DEFAULT=DEBUG before any application code runs.
The env var can still be overridden by the operator at launch time.
"""
import os

os.environ.setdefault("FIS_LOG_LEVEL_DEFAULT", "DEBUG")
