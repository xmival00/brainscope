"""Per-scan orientation fix: 90-degree rotations and flips of the world axes, header only.

The fix is a 3x3 signed permutation `m` applied about the volume center:
corrected_affine = about(m, center_raw) @ raw_affine. Voxels are never resampled.
"""
from __future__ import annotations

import itertools

import numpy as np

from .transforms import about, apply

# Rotations by +/-90 degrees about the world x (left-right), y (back-front), z (down-up) axes, and flips.
OPS = ("rx+", "rx-", "ry+", "ry-", "rz+", "rz-", "fx", "fy", "fz")


def op_matrix(op):
    if op not in OPS:
        raise ValueError(f"unknown orientation operation {op!r}; expected one of {OPS}")
    axis = "xyz".index(op[1])
    m = np.eye(3, dtype=int)
    if op[0] == "f":
        m[axis, axis] = -1
        return m
    i, j = [k for k in range(3) if k != axis]
    sign = 1 if op[2] == "+" else -1
    m[i, i] = m[j, j] = 0
    m[i, j], m[j, i] = -sign, sign
    return m


def apply_op(m, op):
    return op_matrix(op) @ np.asarray(m, int)


def correction(m, raw_affine, shape):
    """4x4 world transform raw -> corrected for a scan with this raw affine and shape."""
    center = apply(raw_affine, (np.array(shape[:3]) - 1) / 2)
    return about(np.asarray(m, float), center)


def corrected_affine(m, raw_affine, shape):
    return correction(m, raw_affine, shape) @ raw_affine


def is_signed_permutation(m):
    m = np.asarray(m)
    return m.shape == (3, 3) and np.all(np.abs(m).sum(0) == 1) and np.all(np.abs(m).sum(1) == 1) \
        and set(np.unique(m)) <= {-1, 0, 1}


def all_orientations():
    """The 48 signed permutation matrices (24 rotations + 24 mirrored)."""
    out = []
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product((1, -1), repeat=3):
            m = np.zeros((3, 3), int)
            for row, (col, s) in enumerate(zip(perm, signs)):
                m[row, col] = s
            out.append(m)
    return out


def axis_labels(m):
    """Which anatomical direction each raw world axis (+x, +y, +z) points to after the fix."""
    names = ("R", "A", "S")
    neg = ("L", "P", "I")
    m = np.asarray(m)
    labels = []
    for col in range(3):
        row = int(np.flatnonzero(m[:, col])[0])
        labels.append(names[row] if m[row, col] > 0 else neg[row])
    return "".join(labels)
