"""Stereotactic frame coordinates from an N-bar localizer box (e.g. Leksell-compatible) in the CT.

Model: plates, each with two parallel ("vertical") rods and one diagonal rod in the plate's plane, given in frame
coordinates (mm). On every slice an N-plate shows three dots; the diagonal fixes the height, which is why two plates
in different planes already determine all six degrees of freedom.

Steps: thin bright structures are found in the CT (at about 1 mm), straight segments are extracted (RANSAC), grouped
into N patterns (two parallel lines and a coplanar oblique one), and the model is fitted by rigid point-to-rod
matching (ICP), trying all 24 axis-aligned mountings and all usable plate subsets. Result: a rigid 4x4 from the
image world to frame coordinates, with the residual per plate.

Tracking (low-contrast rods, CT or MRI): from a rough pose, each expected rod is followed in planes perpendicular to it
(`track`), the model is fitted to those centres with fixed rod correspondence (`fit_tracked`); `refine` alternates
both. Voxels count as rod when their value is inside an intensity band (from, to).
"""
from __future__ import annotations

import copy
import itertools

import numpy as np
from scipy import ndimage

from . import orientation as ori
from .detect import Axis, _planes, _sample
from .transforms import apply

DEFAULT_MODEL = {
    "name": "Leksell-compatible N-bar localizer",
    "axes": "Leksell convention: +X towards the subject's left, +Y anterior, +Z inferior; frame centre (100, 100, 100)",
    "verified": False,
    "note": "Default geometry: check plate positions, rod positions and diagonal direction against your localizer.",
    # Expected mounting: frame axes in the subject's (RAS) world, rows = frame X, Y, Z. Used to choose between fits
    # that match the rods equally well (with only the two lateral plates, up/down and front/back are ambiguous).
    "mount": [[-1, 0, 0], [0, 1, 0], [0, 0, -1]],
    "plates": [
        {"name": "right", "axis": "x", "position": 0.0, "rods": [40.0, 160.0], "z": [40.0, 160.0],
         "diagonal": [[40.0, 40.0], [160.0, 160.0]], "enabled": True},
        {"name": "left", "axis": "x", "position": 200.0, "rods": [40.0, 160.0], "z": [40.0, 160.0],
         "diagonal": [[40.0, 40.0], [160.0, 160.0]], "enabled": True},
        {"name": "anterior", "axis": "y", "position": 200.0, "rods": [40.0, 160.0], "z": [40.0, 160.0],
         "diagonal": [[40.0, 40.0], [160.0, 160.0]], "enabled": True},
    ],
}


def default_model():
    return copy.deepcopy(DEFAULT_MODEL)


def validate(model):
    m = copy.deepcopy(model)
    if not m.get("plates"):
        raise ValueError("the localizer model needs plates")
    for p in m["plates"]:
        if p.get("axis") not in ("x", "y"):
            raise ValueError("plate axis must be 'x' (lateral plate) or 'y' (anterior/posterior plate)")
        p["position"] = float(p["position"])
        p["rods"] = [float(v) for v in p["rods"]]
        p["z"] = [float(v) for v in p["z"]]
        p["diagonal"] = [[float(v) for v in q] for q in p["diagonal"]]
        p["enabled"] = bool(p.get("enabled", True))
        if len(p["rods"]) != 2 or len(p["z"]) != 2 or np.shape(p["diagonal"]) != (2, 2):
            raise ValueError(f"plate {p.get('name')}: two rods, a z range and two diagonal end points are needed")
    m["verified"] = bool(m.get("verified"))
    mount = np.asarray(m.get("mount", DEFAULT_MODEL["mount"]), float)
    if mount.shape != (3, 3) or not np.allclose(mount @ mount.T, np.eye(3), atol=1e-6):
        raise ValueError("mount must be a 3x3 rotation (rows: frame X, Y, Z in RAS)")
    m["mount"] = mount.tolist()
    return m


def describe(r):
    """Where each frame axis points in the subject (RAS) world, e.g. 'X→L, Y→A, Z→I'."""
    names = (("R", "L"), ("A", "P"), ("S", "I"))
    out = []
    for axis, row in zip("XYZ", np.asarray(r)[:3, :3]):  # row i of image->frame rotation = frame axis i in RAS
        k = int(np.argmax(np.abs(row)))
        out.append(f"{axis}→{names[k][0] if row[k] > 0 else names[k][1]}")
    return ", ".join(out)


def segments(model, plates=None):
    """[(plate name, p0, p1)] in frame coordinates for the enabled plates (or the named ones)."""
    out = []
    for p in model["plates"]:
        if not p.get("enabled", True) or (plates is not None and p["name"] not in plates):
            continue
        pos, (z0, z1) = p["position"], p["z"]

        def pt(h, z):
            return np.array([pos, h, z]) if p["axis"] == "x" else np.array([h, pos, z])

        for a in p["rods"]:
            out.append((p["name"], pt(a, z0), pt(a, z1)))
        (h0, zz0), (h1, zz1) = p["diagonal"]
        out.append((p["name"], pt(h0, zz0), pt(h1, zz1)))
    return out


def rod_list(model):
    """[{id, plate, p0, p1}] for the enabled plates (frame mm); ids '<plate>-1', '<plate>-2' (parallel), '<plate>-d'."""
    out = []
    for p in model["plates"]:
        if p.get("enabled", True):
            for (name, a, b), k in zip(segments(model, [p["name"]]), ("1", "2", "d")):
                out.append({"id": f"{name}-{k}", "plate": name, "p0": a, "p1": b})
    return out


def band_of(band):
    """(from, to) from a band [from, to] (to may be None: no upper limit) or a single threshold."""
    if band is None or np.isscalar(band):
        return float(band if band is not None else -np.inf), np.inf
    lo, hi = band
    return float(lo), (np.inf if hi is None else float(hi))


# ---- detection ----
def _maxpool(data, affine, target_mm, band=None):
    sp = np.sqrt((affine[:3, :3] ** 2).sum(0))
    f = np.maximum(1, np.round(target_mm / sp).astype(int))
    n = np.array(data.shape) // f
    out = np.empty(tuple(n), np.float32)
    step = max(1, 32 // f[2]) * f[2]
    for z0 in range(0, n[2] * f[2], step):
        z1 = min(z0 + step, n[2] * f[2])
        blk = np.asarray(data[:n[0] * f[0], :n[1] * f[1], z0:z1], np.float32)
        if band is not None:  # voxels outside the band do not count
            blk = np.where((blk >= band[0]) & (blk <= band[1]), blk, -np.inf)
        out[:, :, z0 // f[2]:z1 // f[2]] = blk.reshape(n[0], f[0], n[1], f[1], (z1 - z0) // f[2], f[2]).max((1, 3, 5))
    scale = np.diag(list(f) + [1]).astype(float)
    scale[:3, 3] = (f - 1) / 2
    return out, affine @ scale


def _ransac_lines(pts, rng, tol=1.5, min_len=30.0, max_lines=4, iters=150):
    lines, rest = [], pts
    for _ in range(max_lines):
        if len(rest) < 15:
            break
        best_n, best = 0, None
        for _ in range(iters):
            a, b = rest[rng.integers(len(rest), size=2)]
            d = b - a
            if np.linalg.norm(d) < 8:
                continue
            d = d / np.linalg.norm(d)
            r = rest - a
            inl = np.linalg.norm(r - np.outer(r @ d, d), axis=1) < tol
            if inl.sum() > best_n:
                best_n, best = int(inl.sum()), inl
        if best is None or best_n < 15:
            break
        p = rest[best]
        c = p.mean(0)
        u = np.linalg.svd(p - c, full_matrices=False)[2][0]
        t = (p - c) @ u
        if t.max() - t.min() >= min_len:
            lines.append({"c": c, "u": u, "t0": float(t.min()), "t1": float(t.max()), "n": best_n})
        rest = rest[~best]
    return lines


def find_lines(data, affine, band, voxel_mm=1.0, seed=0):
    """Straight thin segments (rods) of voxels inside the intensity band; world coordinates of `affine`."""
    band = band_of(band)
    d, aff = _maxpool(data, affine, voxel_mm, band)
    lab, n = ndimage.label(d >= band[0], structure=np.ones((3, 3, 3)))
    if n == 0:
        return []
    sizes = np.bincount(lab.ravel())
    rng = np.random.default_rng(seed)
    lines = []
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        if sl is None or not 20 <= sizes[i] <= 60000:
            continue
        vox = np.argwhere(lab[sl] == i) + np.array([s.start for s in sl])
        pts = apply(aff, vox)
        ev = np.linalg.eigvalsh(np.cov((pts - pts.mean(0)).T))
        if np.sqrt(max(ev[0], 0)) > 4.0:  # thick in every direction: bone, not rods
            continue
        lines += _ransac_lines(pts, rng)
    return lines


def _line_from_points(p, n):
    c = p.mean(0)
    u = np.linalg.svd(p - c, full_matrices=False)[2][0]
    t = (p - c) @ u
    return {"c": c, "u": u, "t0": float(t.min()), "t1": float(t.max()), "n": n}


def merge_lines(lines, max_angle=3.0, max_offset=2.0, max_gap=25.0):
    """Join collinear pieces of one rod (rods often break up where they touch other rods or fade)."""
    lines = sorted(lines, key=lambda L: -(L["t1"] - L["t0"]))
    cos_a = np.cos(np.deg2rad(max_angle))
    changed = True
    while changed:
        changed = False
        for i, j in itertools.combinations(range(len(lines)), 2):
            a, b = lines[i], lines[j]
            if abs(a["u"] @ b["u"]) < cos_a:
                continue
            ends = np.array([b["c"] + b["t0"] * b["u"], b["c"] + b["t1"] * b["u"]]) - a["c"]
            along = ends @ a["u"]
            if np.linalg.norm(ends - np.outer(along, a["u"]), axis=1).max() > max_offset:
                continue
            if along.min() > a["t1"] + max_gap or along.max() < a["t0"] - max_gap:
                continue
            lines[i] = _line_from_points(sample([a, b], [0, 1]), a["n"] + b["n"])
            del lines[j]
            changed = True
            break
    return lines


def group_n(lines, max_angle=5.0):
    """N patterns: two parallel lines and an oblique one in their plane. Returns [(i, j, k)] (k = diagonal)."""
    cands = []
    cos_par = np.cos(np.deg2rad(max_angle))
    for i, j in itertools.combinations(range(len(lines)), 2):
        a, b = lines[i], lines[j]
        if abs(a["u"] @ b["u"]) < cos_par:
            continue
        w = b["c"] - a["c"]
        perp = w - (w @ a["u"]) * a["u"]
        sep = np.linalg.norm(perp)
        if not 30 < sep < 300:
            continue
        n = np.cross(a["u"], perp / sep)
        for k, c in enumerate(lines):
            if k in (i, j):
                continue
            ang = np.degrees(np.arccos(min(1.0, abs(c["u"] @ a["u"]))))
            if not 15 < ang < 75 or abs(c["u"] @ n) > 0.08:
                continue
            off = abs((c["c"] - a["c"]) @ n)
            h = ((c["c"] - a["c"]) @ (perp / sep)) / sep  # diagonal centre between the rods: 0..1
            if off < 3.0 and -0.1 < h < 1.1:
                cands.append((off, i, j, k))
    groups, used = [], set()
    for off, i, j, k in sorted(cands):
        if not {i, j, k} & used:
            groups.append((i, j, k))
            used |= {i, j, k}
    return groups


# ---- fit ----
def _closest_on_segments(q, segs):
    a = np.array([s[1] for s in segs])
    d = np.array([s[2] for s in segs]) - a
    t = np.clip(np.einsum("nsk,sk->ns", q[:, None] - a[None], d) / (d ** 2).sum(1), 0, 1)
    c = a[None] + t[..., None] * d[None]
    dist = np.linalg.norm(q[:, None] - c, axis=2)
    k = dist.argmin(1)
    return c[np.arange(len(q)), k], dist[np.arange(len(q)), k], k


def _kabsch(p, q):
    cp, cq = p.mean(0), q.mean(0)
    u, _, vt = np.linalg.svd((p - cp).T @ (q - cq))
    s = np.eye(3)
    s[2, 2] = np.sign(np.linalg.det(vt.T @ u.T))
    r = vt.T @ s @ u.T
    return r, cq - r @ cp


def _icp(pts, segs, r, t, iters=60):
    for _ in range(iters):
        q = pts @ r.T + t
        c, dist, _ = _closest_on_segments(q, segs)
        keep = dist < max(3.0, np.percentile(dist, 90))
        r2, t2 = _kabsch(pts[keep], c[keep])
        if np.allclose(r2, r, atol=1e-7) and np.allclose(t2, t, atol=1e-5):
            break
        r, t = r2, t2
    q = pts @ r.T + t
    _, dist, k = _closest_on_segments(q, segs)
    return r, t, dist, k


def sample(lines, idx, step=1.0):
    pts = []
    for i in idx:
        L = lines[i]
        ts = np.arange(L["t0"], L["t1"] + 1e-9, step)
        pts.append(L["c"] + ts[:, None] * L["u"])
    return np.vstack(pts) if pts else np.zeros((0, 3))


def fit(lines, groups, model, keep=3):
    """Best rigid image->frame fits (sorted by RMS) over 24 mountings and the usable plate subsets."""
    pts = sample(lines, [i for g in groups for i in g])
    if len(groups) < 2 or len(pts) < 30:
        raise ValueError(f"found {len(groups)} N-plate(s) in the CT; at least 2 are needed")
    enabled = [p["name"] for p in model["plates"] if p.get("enabled", True)]
    mount = np.asarray(model.get("mount", DEFAULT_MODEL["mount"]), float)
    subsets = [s for k in range(2, len(enabled) + 1) for s in itertools.combinations(enabled, k) if k <= len(groups)]
    rotations = [m for m in ori.all_orientations() if np.linalg.det(m) > 0]
    results = []
    for sub in subsets:
        segs = segments(model, sub)
        mpts = np.vstack([s[1] + np.linspace(0, 1, 60)[:, None] * (s[2] - s[1]) for s in segs])
        for rot in rotations:
            r = rot.astype(float)
            t = mpts.mean(0) - r @ pts.mean(0)
            r, t, dist, k = _icp(pts, segs, r, t)
            rms = float(np.sqrt(np.mean(dist ** 2)))
            per = {}
            for name in sub:
                sel = np.array([segs[j][0] == name for j in k])
                if sel.any():
                    per[name] = {"rms": round(float(np.sqrt(np.mean(dist[sel] ** 2))), 3), "points": int(sel.sum())}
            m = np.eye(4)
            m[:3, :3], m[:3, 3] = r, t
            ang = float(np.degrees(np.arccos(np.clip((np.trace(r @ mount.T) - 1) / 2, -1, 1))))
            results.append({"matrix": m, "rms": rms, "plates": per, "subset": list(sub), "mount_deg": round(ang, 1),
                            "axes": describe(r)})
    # fit quality first; between equally good fits, the one closest to the expected mounting
    results.sort(key=lambda x: x["rms"] / np.sqrt(len(x["subset"])) + 0.002 * x["mount_deg"])
    distinct = []
    for res in results:  # keep clearly different solutions only
        if all(np.abs(res["matrix"][:3, :3] - d["matrix"][:3, :3]).max() > 0.2 or
               np.linalg.norm(res["matrix"][:3, 3] - d["matrix"][:3, 3]) > 5 for d in distinct):
            distinct.append(res)
        if len(distinct) == keep:
            break
    return distinct


def detect(data, affine, model, band):
    """Lines, N groups and the best fits (image world of `affine` -> frame coordinates)."""
    lines = merge_lines(find_lines(data, affine, band))
    groups = group_n(lines)
    return lines, groups, fit(lines, groups, model)


# ---- tracking from a rough pose ----
def track(data, affine, model, pose, band, step=1.0, search_mm=5.0, max_diam_mm=10.0, manual=None, excluded=None):
    """Centre of each rod in planes perpendicular to where the model at `pose` (image world -> frame) puts it.

    Returns {rod id: [[tau, point or None, source]]}: tau = mm along the model rod (frame), point in image world,
    source 'auto', 'manual', 'missing' or 'excluded'. `manual` {rod id: [image world points]} replaces the automatic
    centre of the nearest slice; `excluded` {rod id: [tau]} drops slices."""
    lo, hi = band_of(band)
    inv, ipose = np.linalg.inv(affine), np.linalg.inv(pose)
    h = float(np.clip(np.sqrt((affine[:3, :3] ** 2).sum(0)).min() / 2, 0.2, 0.5))
    radius = search_mm + max_diam_mm / 2
    cval = lo - 1.0 if np.isfinite(lo) else float(np.min(data)) - 1.0
    out = {}
    for rod in rod_list(model):
        length = float(np.linalg.norm(rod["p1"] - rod["p0"]))
        axis = Axis.line(apply(ipose, rod["p0"]), apply(ipose, rod["p1"]))
        taus = np.arange(0.0, length + 1e-9, step)
        pts, ga, gb = _planes(axis, taus, radius, h)
        img = _sample(data, inv, pts, cval)
        r = np.hypot(ga, gb)
        edge = r > radius - 1.5 * h
        _, e1, e2 = axis.frame(taus)
        skip = {int(round(t / step)) for t in (excluded or {}).get(rod["id"], [])}
        rows = []
        for k, t in enumerate(taus):
            if k in skip:
                rows.append([float(t), None, "excluded"])
                continue
            lab, n = ndimage.label((img[k] >= lo) & (img[k] <= hi) & (r <= radius))
            best = None
            for j, sl in enumerate(ndimage.find_objects(lab), start=1):
                sel = lab[sl] == j
                a_, b_ = ga[sl][sel], gb[sl][sel]
                if edge[sl][sel].any() or len(a_) < 2:  # reaches the window edge: a plate or bone, not a rod
                    continue
                if np.sqrt(12 * np.linalg.eigvalsh(np.cov(np.stack([a_, b_])))[-1]) > max_diam_mm:
                    continue  # too long across: not a rod section
                ca, cb = a_.mean(), b_.mean()
                d = np.hypot(ca, cb)
                if d <= search_mm and (best is None or d < best[0]):
                    best = (d, ca, cb)
            if best is None:
                rows.append([float(t), None, "missing"])
            else:
                rows.append([float(t), axis.point(t) + best[1] * e1[k] + best[2] * e2[k], "auto"])
        d = (rod["p1"] - rod["p0"]) / length
        for p in (manual or {}).get(rod["id"], []):
            t = float((apply(pose, p) - rod["p0"]) @ d)
            k = int(round(t / step))
            if 0 <= k < len(rows) and rows[k][2] != "excluded":
                rows[k] = [t, np.asarray(p, float), "manual"]
            elif not 0 <= k < len(rows):
                rows.append([t, np.asarray(p, float), "manual"])
        out[rod["id"]] = rows
    return out


def residuals(tracked, model, pose):
    """Distance (mm) of every tracked centre from its rod at `pose`, as {rod id: [distance or None]}."""
    rods = {r["id"]: r for r in rod_list(model)}
    out = {}
    for rid, rows in tracked.items():
        a, b = rods[rid]["p0"], rods[rid]["p1"]
        d = (b - a) / np.linalg.norm(b - a)
        out[rid] = [None if p is None else float(np.linalg.norm((q := apply(pose, p) - a) - (q @ d) * d))
                    for _, p, _ in rows]
    return out


def fit_tracked(tracked, model, pose, iters=200):
    """Rigid image->frame fit of tracked centres to their own rods (fixed correspondence), from `pose`."""
    rods = {r["id"]: r for r in rod_list(model)}
    pts, a, d, plates = [], [], [], set()
    for rid, rows in tracked.items():
        for _, p, _ in rows:
            if p is not None:
                r = rods[rid]
                pts.append(p)
                a.append(r["p0"])
                d.append((r["p1"] - r["p0"]) / np.linalg.norm(r["p1"] - r["p0"]))
                plates.add(r["plate"])
    if len(plates) < 2 or len(pts) < 20:
        raise ValueError(f"rod centres found on {len(plates)} plate(s) ({len(pts)} points); at least 2 plates needed: "
                         "check the rough alignment and the intensity band")
    pts, a, d = np.array(pts, float), np.array(a), np.array(d)
    r, t = pose[:3, :3].copy(), pose[:3, 3].copy()
    for _ in range(iters):
        q = pts @ r.T + t
        c = a + ((q - a) * d).sum(1)[:, None] * d
        dist = np.linalg.norm(q - c, axis=1)
        keep = dist <= max(2.0, 3 * np.median(dist))  # outliers (a wrong blob) do not pull the fit
        r2, t2 = _kabsch(pts[keep], c[keep])
        done = np.allclose(r2, r, atol=1e-8) and np.allclose(t2, t, atol=1e-6)
        r, t = r2, t2
        if done:
            break
    m = np.eye(4)
    m[:3, :3], m[:3, 3] = r, t
    return m


def refine(data, affine, model, pose, band, rounds=3, **kw):
    """Alternate tracking and fitting from a rough pose. Returns (pose, tracked, residuals, summary)."""
    pose = np.asarray(pose, float)
    for _ in range(rounds):
        pose = fit_tracked(track(data, affine, model, pose, band, **kw), model, pose)
    tracked = track(data, affine, model, pose, band, **kw)
    res = residuals(tracked, model, pose)
    per, alld = {}, []
    for rid, rows in tracked.items():
        ds = [x for x in res[rid] if x is not None]
        alld += ds
        per[rid] = {"rms": round(float(np.sqrt(np.mean(np.square(ds)))), 3) if ds else None, "found": len(ds),
                    "slices": len(rows)}
    rms = float(np.sqrt(np.mean(np.square(alld)))) if alld else None
    return pose, tracked, res, {"rms": rms, "rods": per, "axes": describe(pose)}


def render(data, affine, segs_world, radius=1.0, value=1500.0):
    """Draw rods (world segments) into `data` in place: synthetic test data."""
    inv = np.linalg.inv(affine)
    sp = np.sqrt((affine[:3, :3] ** 2).sum(0))
    for _, p0, p1 in segs_world:
        v = apply(inv, np.array([p0, p1]))
        lo = np.clip(np.floor(v.min(0) - (radius + 2) / sp).astype(int), 0, np.array(data.shape) - 1)
        hi = np.clip(np.ceil(v.max(0) + (radius + 2) / sp).astype(int) + 1, 1, data.shape)
        g = np.stack(np.meshgrid(*[np.arange(a, b) for a, b in zip(lo, hi)], indexing="ij"), -1)
        q = apply(affine, g.reshape(-1, 3))
        d = p1 - p0
        t = np.clip((q - p0) @ d / (d @ d), 0, 1)
        dist = np.linalg.norm(q - (p0 + t[:, None] * d), axis=1).reshape(g.shape[:3])
        w = np.clip(radius + 0.5 - dist, 0, 1)
        region = data[tuple(slice(a, b) for a, b in zip(lo, hi))]
        region[:] = np.maximum(region, region * (1 - w) + value * w)
