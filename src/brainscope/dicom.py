"""DICOM to NIfTI. Inputs are never modified; sidecars are anonymized.

The bundled dcm2niix is used first. If it cannot run (e.g. the OS blocks the helper program) or finds nothing,
SimpleITK's DICOM reader is used instead.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import SimpleITK as sitk

from .transforms import from_sitk, to_sitk
from .volume import save


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


def _info(f, meta):
    hdr = nib.load(str(f)).header
    shape = hdr.get_data_shape()
    if len(shape) < 3 or min(shape[:3]) < 2:
        return None
    return {"file": f.name, "description": meta.get("SeriesDescription", f.name.split(".")[0]),
            "modality": meta.get("Modality", ""), "series_number": meta.get("SeriesNumber"),
            "shape": [int(x) for x in shape[:3]], "spacing": [round(float(x), 3) for x in np.asarray(hdr.get_zooms()[:3])]}


def convert_dcm2niix(folder, out_dir):
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    flags = dict(creationflags=0x08000000) if sys.platform == "win32" else {}  # no console window on Windows
    res = subprocess.run([binary(), "-z", "y", "-b", "y", "-ba", "y", "-i", "y", "-f", "%s_%d", "-o", str(out_dir),
                          str(folder)], capture_output=True, text=True, **flags)
    series = []
    for f in sorted(Path(out_dir).glob("*.nii.gz")):
        side = f.with_name(f.name[:-7] + ".json")
        s = _info(f, json.loads(side.read_text()) if side.exists() else {})
        if s:
            series.append(s)
    if not series:
        tail = " ".join((res.stdout + res.stderr).strip().splitlines()[-3:])
        raise RuntimeError(f"dcm2niix (exit {res.returncode}) found no image series. {tail}")
    return series


def convert_sitk(folder, out_dir):
    """Fallback: one NIfTI per DICOM series found in `folder` and its subfolders."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    series = []
    for root, _, _ in os.walk(folder):
        for sid in sitk.ImageSeriesReader.GetGDCMSeriesIDs(str(root)) or ():
            names = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(str(root), sid)
            if len(names) < 2:
                continue
            reader = sitk.ImageSeriesReader()
            reader.SetFileNames(names)
            reader.MetaDataDictionaryArrayUpdateOn()
            img = reader.Execute()
            tag = lambda k: reader.GetMetaData(0, k).strip() if reader.HasMetaDataKey(0, k) else ""
            meta = {"SeriesDescription": tag("0008|103e"), "Modality": tag("0008|0060"),
                    "SeriesNumber": int(tag("0020|0011")) if tag("0020|0011").isdigit() else None}
            data, affine = from_sitk(img)
            name = f"{meta['SeriesNumber'] or len(series) + 1}_{re.sub(r'[^A-Za-z0-9]+', '_', meta['SeriesDescription'])}"
            f = Path(out_dir) / f"{name.strip('_')}_{sid[-6:]}.nii.gz"
            save(f, data, affine)
            s = _info(f, meta)
            if s:
                series.append(s)
    if not series:
        raise RuntimeError("no DICOM image series with at least 2 slices found")
    return series


def convert(folder, out_dir) -> list[dict]:
    """Convert every series under `folder`; returns one entry per resulting volume."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        return convert_dcm2niix(folder, out_dir)
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        first = f"{type(e).__name__}: {e}"
    for f in out_dir.iterdir():
        f.unlink()
    try:
        return convert_sitk(folder, out_dir)
    except RuntimeError as e:
        raise RuntimeError(f"DICOM conversion failed. dcm2niix: {first}. SimpleITK: {e}") from None


def write_test_series(data, affine, folder, description="test head"):
    """A minimal CT DICOM series (one file per slice), as a scanner would write it. For tests and the self-test."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    img = sitk.Cast(to_sitk(data, affine), sitk.sitkInt16)
    d = img.GetDirection()
    w = sitk.ImageFileWriter()
    w.KeepOriginalImageUIDOn()
    common = {"0008|0060": "CT", "0008|103e": description, "0020|000d": "1.2.826.0.1.3680043.2.1125.1",
              "0020|000e": "1.2.826.0.1.3680043.2.1125.2", "0020|0011": "3", "0010|0010": "Test^Subject",
              "0020|0037": "\\".join(f"{x:.6f}" for x in (d[0], d[3], d[6], d[1], d[4], d[7])),
              "0028|0030": f"{img.GetSpacing()[1]:.4f}\\{img.GetSpacing()[0]:.4f}",
              "0018|0050": f"{img.GetSpacing()[2]:.4f}", "0028|1052": "0", "0028|1053": "1",
              "0020|0052": "1.2.826.0.1.3680043.2.1125.4", "0018|5100": "HFS", "0008|0070": "TEST",
              "0008|0008": "ORIGINAL\\PRIMARY\\AXIAL", "0008|0021": "20260101", "0008|0031": "120000",
              "0008|0020": "20260101", "0008|0030": "120000", "0020|0012": "1"}
    for k in range(img.GetDepth()):
        sl = img[:, :, k]
        for t, val in common.items():
            sl.SetMetaData(t, val)
        sl.SetMetaData("0020|0032", "\\".join(f"{x:.4f}" for x in img.TransformIndexToPhysicalPoint((0, 0, k))))
        sl.SetMetaData("0020|0013", str(k + 1))
        sl.SetMetaData("0008|0018", f"1.2.826.0.1.3680043.2.1125.3.{k + 1}")
        w.SetFileName(str(folder / f"{k:04d}.dcm"))
        w.Execute(sl)
