"""Brain model for figures: brain mask, cuts, smooth surface, lead meshes, and files for Python/MATLAB.

Mask: the space inside the skull is grown from a seed in the CT (soft tissue, not crossing bone; thin connections such
as the orbits are cut by an erosion first), then refined with the MRI (brain brighter than CSF on T1) so that gyri
and sulci show. Cuts remove everything on one side of a plane (e.g. brainstem/cerebellum, or one hemisphere).
All arrays are on the MRI grid; all coordinates are MRI world (RAS+, mm).
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import nibabel as nib
import numpy as np
from scipy import ndimage, sparse

from .transforms import apply

AXES = {"x": 0, "y": 1, "z": 2}


def intracranial(ct, affine, seed, levels, box=None, open_mm=2.0):
    """Inside-skull mask from a CT on the MRI grid. `seed`: MRI world point inside the brain."""
    off = levels.get("offset") or 0.0
    if levels.get("kind") == "percentile":
        bone_thr, soft_lo, soft_hi = levels["overlay_bone"][0], levels["reg"][0], levels["overlay_bone"][0]
    else:
        bone_thr, soft_lo, soft_hi = 300 + off, -30 + off, 150 + off
    sp = np.sqrt((affine[:3, :3] ** 2).sum(0))
    bone = ndimage.binary_dilation(ct > bone_thr)
    soft = (ct > soft_lo) & (ct < soft_hi) & ~bone
    if box is not None:
        soft &= box
    r = max(1, int(round(open_mm / sp.min())))
    lab, n = ndimage.label(ndimage.binary_erosion(soft, iterations=r))
    if n == 0:
        raise ValueError("no soft tissue found inside the skull; check the CT and the seed")
    s = np.round(apply(np.linalg.inv(affine), seed)).astype(int)
    s = np.clip(s, 0, np.array(ct.shape) - 1)
    k = lab[tuple(s)]
    if k == 0:  # seed just outside the eroded core: the nearest component within 10 mm
        rad = np.ceil(10 / sp).astype(int)
        win = tuple(slice(max(0, a - b), a + b + 1) for a, b in zip(s, rad))
        vals = lab[win][lab[win] > 0]
        if not len(vals):
            raise ValueError("the seed is not inside the brain; put the crosshair in the brain and try again")
        k = np.bincount(vals).argmax()
    mask = ndimage.binary_dilation(lab == k, iterations=r) & soft
    return ndimage.binary_fill_holes(mask)


def refine_with_mri(mask, mri, threshold=None):
    """Keep brain tissue (brighter than CSF on T1) inside `mask`; returns (mask, threshold used)."""
    if threshold is None:
        from skimage.filters import threshold_otsu

        threshold = float(threshold_otsu(mri[mask][:: max(1, mask.sum() // 2_000_000)]))
    brain = mask & (mri > threshold)
    lab, n = ndimage.label(brain)
    if n:
        brain = lab == (np.bincount(lab.ravel())[1:].argmax() + 1)
    brain = ndimage.binary_closing(brain, iterations=1)
    return ndimage.binary_fill_holes(brain), threshold


def apply_cuts(mask, affine, cuts):
    """Each cut {"axis": x|y|z, "keep": "above"|"below", "at": mm} keeps one side of a world plane."""
    if not cuts:
        return mask
    idx = [np.arange(n) for n in mask.shape]
    out = mask.copy()
    for c in cuts:
        a = AXES[c["axis"]]
        w = (affine[a, 0] * idx[0][:, None, None] + affine[a, 1] * idx[1][None, :, None]
             + affine[a, 2] * idx[2][None, None, :] + affine[a, 3])
        out &= (w >= c["at"]) if c["keep"] == "above" else (w <= c["at"])
    return out


def _taubin(v, f, iterations, lam=0.5, mu=-0.53):
    if iterations <= 0:
        return v
    e = np.vstack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    a = sparse.coo_matrix((np.ones(len(e)), (e[:, 0], e[:, 1])), shape=(len(v), len(v))).tocsr()
    a = ((a + a.T) > 0).astype(float)
    deg = np.asarray(a.sum(1)).ravel()
    lap = sparse.diags(1 / np.maximum(deg, 1)) @ a
    for _ in range(iterations):
        v = v + lam * (lap @ v - v)
        v = v + mu * (lap @ v - v)
    return v


def surface(mask, affine, smooth=20, step=1):
    """Smooth triangle surface (world coordinates) of a mask."""
    from skimage.measure import marching_cubes

    if not mask.any():
        raise ValueError("the mask is empty")
    sl = ndimage.find_objects(mask.astype(np.uint8))[0]
    sl = tuple(slice(max(0, s.start - 2), s.stop + 2) for s in sl)
    m = ndimage.gaussian_filter(mask[sl].astype(np.float32), 0.7)
    v, f, _, _ = marching_cubes(m, level=0.5, step_size=int(step))
    v = apply(affine, v + np.array([s.start for s in sl]))
    return _taubin(v, f.astype(np.int32), int(smooth)), f.astype(np.int32)


def _tube(points, radius, sides=16, caps=True):
    """Triangle tube along a polyline (N, 3)."""
    points = np.asarray(points, float)
    t = np.gradient(points, axis=0)
    t /= np.linalg.norm(t, axis=1, keepdims=True)
    ref = np.eye(3)[np.argmin(np.abs(t[0]))]
    n1 = np.cross(t[0], ref)
    n1 /= np.linalg.norm(n1)
    ang = np.linspace(0, 2 * np.pi, sides, endpoint=False)
    verts, frames = [], []
    for i, p in enumerate(points):  # parallel transport of the frame along the curve
        if i:
            n1 = n1 - (n1 @ t[i]) * t[i]
            n1 /= np.linalg.norm(n1)
        n2 = np.cross(t[i], n1)
        verts.append(p + radius * (np.cos(ang)[:, None] * n1 + np.sin(ang)[:, None] * n2))
    v = np.vstack(verts)
    faces = []
    for i in range(len(points) - 1):
        for k in range(sides):
            a, b = i * sides + k, i * sides + (k + 1) % sides
            c, d = a + sides, b + sides
            faces += [[a, c, b], [b, c, d]]
    if caps:
        v = np.vstack([v, points[0], points[-1]])
        c0, c1 = len(v) - 2, len(v) - 1
        last = (len(points) - 1) * sides
        for k in range(sides):
            faces += [[c0, (k + 1) % sides, k], [c1, last + k, last + (k + 1) % sides]]
    return v, np.array(faces, np.int32)


def _merge(parts):
    vs, fs, off = [], [], 0
    for v, f in parts:
        vs.append(v)
        fs.append(f + off)
        off += len(v)
    return np.vstack(vs), np.vstack(fs)


def lead_meshes(axis_point, contacts, preset, s_end, center=None):
    """(shaft, contacts) meshes for one lead. `axis_point(s)` gives MRI world points along the lead (s from the tip).

    Depth/DBS leads: a tube and a slightly wider cylinder per contact. Strips (preset type "strip"): a thin band and a
    disc per contact (disc diameter = contact length), facing away from `center` (e.g. the brain centre)."""
    d = preset["diameter_mm"]
    s = np.linspace(0.0, max(s_end, max(c["s"] for c in contacts) + preset["contact_length_mm"]), 60)
    pts = axis_point(s)
    if preset.get("type") == "strip":
        shaft = _tube(pts, 0.3, sides=8)
        discs = []
        for c in contacts:
            p = axis_point(np.array([c["s"]]))[0]
            t = axis_point(np.array([c["s"] + 0.5]))[0] - p
            nrm = p - (np.asarray(center, float) if center is not None else p + np.cross(t, [0, 0, 1]))
            nrm = nrm - (nrm @ t) / (t @ t) * t
            nrm /= np.linalg.norm(nrm) + 1e-9
            discs.append(_tube(np.array([p - 0.25 * nrm, p + 0.25 * nrm]), preset["contact_length_mm"] / 2, sides=20))
        return shaft, _merge(discs)
    shaft = _tube(pts, d / 2, sides=16)
    L = preset["contact_length_mm"]
    rings = [_tube(axis_point(np.linspace(c["s"] - L / 2, c["s"] + L / 2, 4)), d / 2 + 0.08, sides=16) for c in contacts]
    return shaft, _merge(rings)


# ---- mesh files ----
def write_gifti(path, v, f):
    img = nib.gifti.GiftiImage(darrays=[
        nib.gifti.GiftiDataArray(np.asarray(v, np.float32), intent="NIFTI_INTENT_POINTSET", datatype="NIFTI_TYPE_FLOAT32"),
        nib.gifti.GiftiDataArray(np.asarray(f, np.int32), intent="NIFTI_INTENT_TRIANGLE", datatype="NIFTI_TYPE_INT32")])
    nib.save(img, str(path))


def write_obj(path, v, f):
    with open(path, "w") as fh:
        fh.write("# brainscope mesh, MRI world coordinates (RAS+, mm)\n")
        fh.writelines(f"v {a:.4f} {b:.4f} {c:.4f}\n" for a, b, c in v)
        fh.writelines(f"f {a + 1} {b + 1} {c + 1}\n" for a, b, c in f)


def write_stl(path, v, f):
    tri = np.asarray(v, np.float32)[f]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    with open(path, "wb") as fh:
        fh.write(b"brainscope mesh, MRI world (RAS+, mm)".ljust(80, b" "))
        fh.write(struct.pack("<I", len(f)))
        rec = np.zeros(len(f), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
        rec["n"], rec["v"] = n, tri
        fh.write(rec.tobytes())


def write_scene(path, items):
    Path(path).write_text(json.dumps({"coordinates": "MRI world, RAS+, mm", "meshes": items}, indent=1))
