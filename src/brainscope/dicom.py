"""DICOM to NIfTI with the bundled dcm2niix. Inputs are never modified; sidecars are anonymized."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import nibabel as nib
import numpy as np


def binary() -> str:
    try:
        import dcm2niix

        exe = Path(dcm2niix.__file__).parent / ("dcm2niix.exe" if sys.platform == "win32" else "dcm2niix")
        if exe.exists():
            return str(exe)
    except ImportError:
        pass
    found = shutil.which("dcm2niix")
    if not found:
        raise RuntimeError("dcm2niix was not found")
    return found


def convert(folder, out_dir) -> list[dict]:
    """Convert every series under `folder`; returns one entry per resulting 3D/4D NIfTI."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    flags = dict(creationflags=0x08000000) if sys.platform == "win32" else {}  # no console window on Windows
    res = subprocess.run([binary(), "-z", "y", "-b", "y", "-ba", "y", "-i", "y", "-f", "%s_%d", "-o", str(out_dir),
                          str(folder)], capture_output=True, text=True, **flags)
    series = []
    for f in sorted(out_dir.glob("*.nii.gz")):
        side = f.with_name(f.name[:-7] + ".json")
        meta = json.loads(side.read_text()) if side.exists() else {}
        hdr = nib.load(str(f)).header
        if len(hdr.get_data_shape()) < 3 or min(hdr.get_data_shape()[:3]) < 2:
            continue
        series.append({"file": f.name, "description": meta.get("SeriesDescription", f.name[:-7]),
                       "modality": meta.get("Modality", ""), "series_number": meta.get("SeriesNumber"),
                       "shape": [int(x) for x in hdr.get_data_shape()[:3]],
                       "spacing": [round(float(x), 3) for x in np.asarray(hdr.get_zooms()[:3])]})
    if not series:
        tail = (res.stdout + res.stderr).strip().splitlines()[-3:]
        raise RuntimeError("no image series found in this folder. " + " ".join(tail))
    return series
