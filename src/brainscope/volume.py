"""NIfTI volumes: reading, writing and header-only changes."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import nibabel as nib
import numpy as np

from .transforms import apply


@dataclass
class Volume:
    data: np.ndarray  # 3D float32, indexed [i, j, k] as stored in the file
    affine: np.ndarray  # voxel -> world, RAS+ mm (sform if set, else qform)

    @property
    def spacing(self):
        return np.sqrt((self.affine[:3, :3] ** 2).sum(0))

    @property
    def center(self):
        return apply(self.affine, (np.array(self.data.shape[:3]) - 1) / 2)


def load(path) -> Volume:
    img = nib.load(str(path))
    data = img.get_fdata(dtype=np.float32)
    if data.ndim == 4:
        data = data[..., 0]
    if data.ndim != 3:
        raise ValueError(f"expected a 3D volume, got shape {data.shape}")
    return Volume(np.asarray(data), img.affine.astype(float))


def raw_affine(path):
    img = nib.load(str(path))
    return img.affine.astype(float), img.shape[:3]


def _set_affine(img, affine):
    img.set_sform(affine, code=1)
    img.set_qform(affine, code=1)
    return img


def save(path, data, affine):
    nib.save(_set_affine(nib.Nifti1Image(np.asarray(data), affine), affine), str(path))


def save_reheadered(src, dst, affine):
    """Copy a NIfTI with a new affine; voxel values and data type unchanged."""
    img = nib.load(str(src))
    out = nib.Nifti1Image(img.dataobj, affine, header=img.header)
    if out.ndim == 4:
        out = out.slicer[..., 0]
    nib.save(_set_affine(out, affine), str(dst))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_nifti(path):
    name = Path(path).name.lower()
    return name.endswith(".nii") or name.endswith(".nii.gz")


NIBABEL_EXT = (".mgz", ".mgh")
SITK_EXT = (".nrrd", ".nhdr", ".mha", ".mhd")


def is_supported(path):
    name = Path(path).name.lower()
    return is_nifti(name) or name.endswith(NIBABEL_EXT + SITK_EXT)


def to_nifti(src, dst):
    """Write `src` (MGZ/MGH or NRRD/MHA/MHD) as a NIfTI with the same world geometry."""
    name = Path(src).name.lower()
    if name.endswith(NIBABEL_EXT):
        img = nib.load(str(src))
        data, affine = np.asanyarray(img.dataobj), img.affine
    else:
        import SimpleITK as sitk

        from .transforms import from_sitk

        data, affine = from_sitk(sitk.ReadImage(str(src)))
    if data.ndim == 4:
        data = data[..., 0]
    save(dst, data, affine)
