"""AC/PC coordinates and planned-versus-delivered comparison.

The AC/PC frame (all points in the orientation-corrected MRI world, RAS+ mm):
- y: from PC towards AC (anterior; rostral in animals);
- z: towards the midline point, perpendicular to y (superior; dorsal), so the y-z plane is the midsagittal plane;
- x = y cross z: towards the subject's right;
- origin: the mid-commissural point (default), AC or PC.
"""
from __future__ import annotations

import numpy as np

from .transforms import apply

POINTS = ("ac", "pc", "msp")
ORIGINS = ("mcp", "ac", "pc")


def matrix(ac, pc, msp, origin="mcp"):
    """4x4 from MRI world to AC/PC coordinates."""
    ac, pc, msp = (np.asarray(p, float) for p in (ac, pc, msp))
    y = ac - pc
    if np.linalg.norm(y) < 1.0:
        raise ValueError("AC and PC must be at least 1 mm apart")
    y /= np.linalg.norm(y)
    m = msp - ac
    z = m - (m @ y) * y
    if np.linalg.norm(z) < 1.0:
        raise ValueError("the midline point must lie off the AC-PC line (e.g. high on the midline)")
    z /= np.linalg.norm(z)
    x = np.cross(y, z)
    o = {"mcp": (ac + pc) / 2, "ac": ac, "pc": pc}[origin]
    out = np.eye(4)
    out[:3, :3] = np.stack([x, y, z])
    out[:3, 3] = -out[:3, :3] @ o
    return out


def compare(planned, delivered, entry=None):
    """Planned versus delivered point, both in the same coordinates (e.g. AC/PC).

    Returns the error vector (delivered - planned), its length and, when the planned entry is given, the distance
    of the delivered point from the planned trajectory (radial) and along it (depth; positive = deeper)."""
    planned, delivered = np.asarray(planned, float), np.asarray(delivered, float)
    d = delivered - planned
    out = {"dx": d[0], "dy": d[1], "dz": d[2], "distance": float(np.linalg.norm(d))}
    if entry is not None:
        u = planned - np.asarray(entry, float)
        if np.linalg.norm(u) > 1e-6:
            u /= np.linalg.norm(u)
            out["depth"] = float(d @ u)
            out["radial"] = float(np.linalg.norm(d - (d @ u) * u))
    return {k: round(float(v), 3) for k, v in out.items()}


def to_acpc(m, pts):
    return apply(m, pts)
