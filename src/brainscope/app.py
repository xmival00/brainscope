"""Desktop launcher: the local server plus a native window (pywebview); falls back to the default browser."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from .server import make_server

TITLE = "brainscope (research use only)"


def default_projects_root():
    return Path(os.environ.get("BRAINSCOPE_PROJECTS", Path.home() / "brainscope-projects"))


class DesktopApi:
    """Native dialogs exposed to the page as window.pywebview.api.*"""

    def __init__(self):
        self.window = None

    def _dialog(self, kind, file_types=()):
        import webview

        res = self.window.create_file_dialog(kind, allow_multiple=False, file_types=file_types)
        return (res[0] if isinstance(res, (list, tuple)) else res) if res else None

    def pick_nifti(self):
        import webview

        return self._dialog(webview.FileDialog.OPEN)  # no filter: macOS dialogs grey out .nii.gz with one

    def pick_folder(self):
        import webview

        return self._dialog(webview.FileDialog.FOLDER)

    def open_folder(self, path):
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


def run(projects_root=None, browser=False, port=0):
    root = Path(projects_root) if projects_root else default_projects_root()
    use_window = not browser
    if use_window:
        try:
            import webview
        except Exception:  # noqa: BLE001
            use_window = False
    server = make_server(root, port, desktop=use_window)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if use_window:
        api = DesktopApi()
        try:
            api.window = webview.create_window(TITLE, url, js_api=api, width=1500, height=950, min_size=(1100, 700))
            webview.start(private_mode=False)
            server.shutdown()
            return
        except Exception as e:  # noqa: BLE001
            print(f"Native window unavailable ({e}); opening the browser instead.")
            server.RequestHandlerClass.app.desktop = False
    print(f"brainscope is running at {url}  (Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
