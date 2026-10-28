"""Synthetic test data with known ground truth: a head (MRI and CT contrast) and leads rendered into a CT."""
from __future__ import annotations

import numpy as np
from scipy import ndimage

from . import presets as pr
from .transforms import apply


def grid_affine(spacing, origin):
    a = np.diag(list(spacing) + [1.0])
    a[:3, 3] = origin
    return a


def world_grid(shape, affine):
    ijk = np.stack(np.meshgrid(*[np.arange(n) for n in shape], indexing="ij"), -1).reshape(-1, 3)
    return apply(affine, ijk).reshape(tuple(shape) + (3,))


def _soft(x, width):
    """1 inside (x < 0), 0 outside, smooth over `width` mm: a cheap partial-volume model."""
    return 1.0 / (1.0 + np.exp(np.clip(x / max(width, 1e-3) * 4.0, -50, 50)))


def head(spacing=2.0, contrast="mri", seed=0):
    """A head-like volume: scalp, skull, brain, asymmetric ventricles and a nose (front = +y, up = +z)."""
    size = np.array([140.0, 170.0, 150.0])
    shape = tuple(int(x) for x in np.ceil(size / spacing))
    affine = grid_affine([spacing] * 3, -size / 2)
    ax = [np.arange(n, dtype=np.float32) * spacing - size[i] / 2 for i, n in enumerate(shape)]
    x, y, z = ax[0][:, None, None], ax[1][None, :, None], ax[2][None, None, :]

    def rad(c, r):
        return np.sqrt(((x - c[0]) / r[0]) ** 2 + ((y - c[1]) / r[1]) ** 2 + ((z - c[2]) / r[2]) ** 2)

    w = spacing / 2
    head_ = _soft((rad((0, 0, 0), (60, 75, 62)) - 1) * 60, w)
    nose = _soft((rad((0, 72, -20), (14, 18, 14)) - 1) * 14, w)
    brain = _soft((rad((0, 0, 5), (52, 66, 52)) - 1) * 52, w)
    skull = _soft((rad((0, 0, 3), (56, 70, 57)) - 1) * 56, w) - brain
    vents = _soft((rad((-9, 5, 10), (5, 18, 7)) - 1) * 6, w) + _soft((rad((11, 8, 8), (4, 14, 6)) - 1) * 5, w)
    wm = _soft((rad((0, 0, 8), (38, 50, 36)) - 1) * 38, w)
    rng = np.random.default_rng(seed)
    tex = ndimage.gaussian_filter(rng.standard_normal(shape, dtype=np.float32), 3.0 / spacing)
    tex /= tex.std() + 1e-9
    noise = rng.standard_normal(shape, dtype=np.float32)
    if contrast == "mri":
        soft = np.clip(head_ + nose - skull - brain, 0, 1)
        img = 550 * soft + 120 * skull + brain * (650 + 250 * wm) - 500 * vents * brain + 25 * tex * brain + 12 * noise
        img = np.clip(img, 0, None)
    else:
        img = -1000 + 1040 * np.clip(head_ + nose, 0, 1) + 1150 * skull - 5 * brain - 30 * vents * brain
        img += 4 * tex * brain + 8 * noise
    return img.astype(np.float32), affine


def add_lead(data, affine, tip, direction, preset, length=None, body_hu=400.0, metal_hu=12000.0):
    """Render a lead into `data` (in place, before blurring). Returns true contact centers (world)."""
    u = np.asarray(direction, float)
    u /= np.linalg.norm(u)
    tip = np.asarray(tip, float)
    rel = pr.centers(preset)
    L, d = preset["contact_length_mm"], preset["diameter_mm"]
    c1 = preset["tip_offset_mm"] + L / 2
    length = length or float(c1 + rel[-1] + L / 2 + 20)
    inv = np.linalg.inv(affine)
    ends = np.array([tip - 3 * u, tip + (length + 3) * u])
    lo = np.floor(apply(inv, ends).min(0) - 4 / np.sqrt((affine[:3, :3] ** 2).sum(0))).astype(int)
    hi = np.ceil(apply(inv, ends).max(0) + 4 / np.sqrt((affine[:3, :3] ** 2).sum(0))).astype(int)
    lo = np.clip(lo, 0, np.array(data.shape) - 1)
    hi = np.clip(hi, 0, np.array(data.shape) - 1)
    sub = tuple(slice(a, b + 1) for a, b in zip(lo, hi))
    p = apply(affine, np.stack(np.meshgrid(*[np.arange(a, b + 1) for a, b in zip(lo, hi)], indexing="ij"), -1)
              .reshape(-1, 3)).reshape(tuple(hi - lo + 1) + (3,))
    t = (p - tip) @ u
    r = np.linalg.norm(p - tip - t[..., None] * u, axis=-1)
    w = 0.5 * float(np.sqrt((affine[:3, :3] ** 2).sum(0)).min())
    radial = _soft(r - d / 2, w)
    body = radial * _soft(-t, w) * _soft(t - length, w)
    metal = np.zeros_like(t)
    for c in c1 + rel:
        metal = np.maximum(metal, radial * _soft(np.abs(t - c) - L / 2, w))
    region = data[sub]
    region[:] = np.maximum(region, region * (1 - body) + body_hu * body)
    region[:] = np.maximum(region, region * (1 - metal) + metal_hu * metal)
    return tip + (c1 + rel)[:, None] * u


def blur(data, affine, psf_mm=0.6, noise=10.0, seed=0):
    sp = np.sqrt((affine[:3, :3] ** 2).sum(0))
    out = ndimage.gaussian_filter(data, psf_mm / sp)
    return (out + np.random.default_rng(seed).normal(0, noise, data.shape)).astype(np.float32)


def lead_ct(preset, spacing=(0.5, 0.5, 0.5), direction=(0.3, 0.5, 0.8), psf_mm=0.6, size=60.0, seed=0):
    """A small CT-like block with one oblique lead. Returns data, affine, true centers, true tip."""
    shape = np.ceil(size / np.asarray(spacing)).astype(int)
    affine = grid_affine(spacing, [-size / 2] * 3)
    data = np.full(shape, 35.0, np.float32)
    u = np.asarray(direction, float) / np.linalg.norm(direction)
    tip = -0.35 * size * u + np.array([1.3, -0.7, 0.4])
    truth = add_lead(data, affine, tip, u, preset)
    return blur(data, affine, psf_mm, seed=seed), affine, truth, tip
