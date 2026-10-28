# PyInstaller spec: one-folder app (Windows brainscope.exe, macOS brainscope.app). Run from the repo root:
#   pyinstaller --noconfirm --clean packaging/brainscope.spec
import sys
from pathlib import Path

import dcm2niix
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

import brainscope

icons = Path(SPECPATH) / "icons"
dcm_bin = Path(dcm2niix.__file__).parent / ("dcm2niix.exe" if sys.platform == "win32" else "dcm2niix")

a = Analysis(
    ["launcher.py"],
    binaries=[(str(dcm_bin), "dcm2niix")],
    datas=collect_data_files("brainscope"),
    hiddenimports=collect_submodules("brainscope"),
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PyQt6", "PySide2", "PySide6"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="brainscope", console=False, upx=False,
          icon=str(icons / ("brainscope.ico" if sys.platform == "win32" else "brainscope.icns")))
coll = COLLECT(exe, a.binaries, a.datas, name="brainscope", upx=False)
if sys.platform == "darwin":
    app = BUNDLE(coll, name="brainscope.app", bundle_identifier="io.github.xmival00.brainscope",
                 icon=str(icons / "brainscope.icns"),
                 info_plist={"CFBundleShortVersionString": brainscope.__version__,
                             "CFBundleVersion": brainscope.__version__, "NSHighResolutionCapable": True})
