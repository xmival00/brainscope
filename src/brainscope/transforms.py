"""4x4 world transforms (RAS+ mm) and the only place where RAS and ITK's LPS meet."""
from __future__ import annotations

import numpy as np
import SimpleITK as sitk

RAS_LPS = np.diag([-1.0, -1.0, 1.0, 1.0])  # its own inverse


def apply(m, pts):
    """Apply a 4x4 transform to one point (3,) or many points (N, 3)."""
    pts = np.asarray(pts, float)
    return pts @ m[:3, :3].T + m[:3, 3]


def translation(t):
    m = np.eye(4)
    m[:3, 3] = t
    return m


def about(m3, center):
    """4x4 that applies the 3x3 matrix m3 about a world point."""
    m = np.eye(4)
    m[:3, :3] = m3
    return translation(center) @ m @ translation(-np.asarray(center, float))


def rotation(deg_xyz):
    """3x3 rotation: about x, then y, then z (degrees)."""
    out = np.eye(3)
    for axis, deg in enumerate(deg_xyz):
        a = np.deg2rad(deg)
        c, s = np.cos(a), np.sin(a)
        i, j = [k for k in range(3) if k != axis]
        r = np.eye(3)
        r[i, i], r[i, j], r[j, i], r[j, j] = c, -s, s, c
        out = r @ out
    return out


def rigid(rot_deg=(0, 0, 0), trans=(0, 0, 0), center=(0, 0, 0)):
    """Rotation about `center` followed by a translation."""
    return translation(trans) @ about(rotation(rot_deg), center)


def to_sitk(data, affine):
    """numpy volume indexed [i, j, k] with a RAS affine -> SimpleITK image (LPS)."""
    a = RAS_LPS @ affine
    spacing = np.sqrt((a[:3, :3] ** 2).sum(0))
    d = a[:3, :3] / spacing
    u, _, vt = np.linalg.svd(d)  # nearest orthonormal direction (ITK requires one)
    img = sitk.GetImageFromArray(np.ascontiguousarray(np.asarray(data, np.float32).transpose(2, 1, 0)))
    img.SetSpacing([float(x) for x in spacing])
    img.SetOrigin([float(x) for x in a[:3, 3]])
    img.SetDirection([float(x) for x in (u @ vt).ravel()])
    return img


def from_sitk(img):
    """SimpleITK image (LPS) -> (numpy volume indexed [i, j, k], RAS affine)."""
    a = np.eye(4)
    a[:3, :3] = np.array(img.GetDirection()).reshape(3, 3) * np.array(img.GetSpacing())
    a[:3, 3] = img.GetOrigin()
    return sitk.GetArrayFromImage(img).transpose(2, 1, 0), RAS_LPS @ a


def sitk_to_ras(tx):
    """ITK transform (fixed -> moving points, LPS) -> 4x4 in RAS mapping fixed world to moving world."""
    r = np.array(tx.GetMatrix()).reshape(3, 3)
    c = np.array(tx.GetCenter())
    t = np.array(tx.GetTranslation())
    a = np.eye(4)
    a[:3, :3] = r
    a[:3, 3] = t + c - r @ c
    return RAS_LPS @ a @ RAS_LPS


def ras_to_sitk(m, center=(0, 0, 0)):
    """4x4 RAS (fixed world -> moving world) -> ITK Euler3DTransform (LPS). `m` must be rigid."""
    a = RAS_LPS @ m @ RAS_LPS
    c = np.asarray(center, float)
    tx = sitk.Euler3DTransform()
    tx.SetCenter([float(x) for x in c])
    tx.SetMatrix([float(x) for x in a[:3, :3].ravel()])
    tx.SetTranslation([float(x) for x in a[:3, 3] - c + a[:3, :3] @ c])
    return tx


def write_itk(path, m_fixed_to_moving):
    """Write an ITK .tfm (as used to resample the moving image onto the fixed grid)."""
    a = RAS_LPS @ m_fixed_to_moving @ RAS_LPS
    tx = sitk.AffineTransform(3)
    tx.SetMatrix([float(x) for x in a[:3, :3].ravel()])
    tx.SetTranslation([float(x) for x in a[:3, 3]])
    sitk.WriteTransform(tx, str(path))
