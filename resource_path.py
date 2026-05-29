import os
import sys


def resource_path(relative_path: str) -> str:
    """Return the path to a resource, working for dev and PyInstaller onefile/folder builds.

    If running in a PyInstaller bundle, files are extracted to sys._MEIPASS; otherwise return the path as-is.
    """
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.abspath('.')
    return os.path.join(base_path, relative_path)
