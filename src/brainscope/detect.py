"""Contact detection along a lead, in planes perpendicular to it.

Stages: the user gives the tip and the entry point; the axis is refined plane by plane from the metal
centroid (wide search first, then narrow, optionally curved); the intensity profile along the axis is fitted
with the preset's contact pattern (contact boxes blurred by the CT resolution); where contacts are clearly
separate on the CT each center is refined on its own, otherwise contacts are placed from the geometry.
All points are in one world space (the CT's); `s` is the distance along the lead in mm.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage
from scipy.special import erf

from . import presets as pr
from .transforms import apply

STEP = 0.1  # mm between profile samples along the lead


@dataclass
class Axis:
    origin: np.ndarray  # world point at s = 0
    u: np.ndarray  # reference direction (from tip towards entry)
    v: np.ndarray
    w: np.ndarray
    cv: np.ndarray  # polynomial (np.polyval order) of the lateral offset along v as a function of s
    cw: np.ndarray

    @classmethod
    def line(cls, a, b):
        a, b = np.asarray(a, float), np.asarray(b, float)
        if np.linalg.norm(b - a) < 1.0:
            raise ValueError("tip and entry must be at least 1 mm apart")
        u = (b - a) / np.linalg.norm(b - a)
        v = np.cross(u, np.eye(3)[np.argmin(np.abs(u))])
        v /= np.linalg.norm(v)
        return cls(a, u, v, np.cross(u, v), np.zeros(1), np.zeros(1))

    def point(self, s):
        s = np.asarray(s, float)
        return (self.origin + s[..., None] * self.u + np.polyval(self.cv, s)[..., None] * self.v
                + np.polyval(self.cw, s)[..., None] * self.w)

    def frame(self, s):
        s = np.asarray(s, float)
        t = (self.u + np.polyval(np.polyder(self.cv), s)[..., None] * self.v
             + np.polyval(np.polyder(self.cw), s)[..., None] * self.w)
        t = t / np.linalg.norm(t, axis=-1, keepdims=True)
        e1 = self.v - (t @ self.v)[..., None] * t
        e1 = e1 / np.linalg.norm(e1, axis=-1, keepdims=True)
        return t, e1, np.cross(t, e1)

    def transformed(self, m):
        """The same axis in another world space (`m` must be an isometry)."""
        r = m[:3, :3]
        return Axis(apply(m, self.origin), r @ self.u, r @ self.v, r @ self.w, self.cv.copy(), self.cw.copy())

    def shifted(self, s0):
        """Same curve, re-parametrised so that the new s = 0 is the old s = s0."""
        q = np.poly1d([1.0, s0])
        return Axis(self.origin + s0 * self.u, self.u, self.v, self.w,
                    np.poly1d(self.cv)(q).coeffs, np.poly1d(self.cw)(q).coeffs)

    def to_dict(self):
        return {k: np.asarray(getattr(self, k)).tolist() for k in ("origin", "u", "v", "w", "cv", "cw")}

    @classmethod
    def from_dict(cls, d):
        return cls(*(np.asarray(d[k], float) for k in ("origin", "u", "v", "w", "cv", "cw")))


def _sample(data, inv_affine, pts, cval):
    vox = apply(inv_affine, pts.reshape(-1, 3))
    return ndimage.map_coordinates(data, vox.T, order=1, mode="constant", cval=cval).reshape(pts.shape[:-1])


def _planes(axis, s, radius, h):
    g = np.arange(-radius, radius + h / 2, h)
    a, b = np.meshgrid(g, g, indexing="ij")
    c = axis.point(s)
    _, e1, e2 = axis.frame(s)
    pts = c[:, None, None] + a[None, ..., None] * e1[:, None, None] + b[None, ..., None] * e2[:, None, None]
    return pts, a, b


def _refine(data, inv, axis, s, radius, thr, cval, degree):
    """Fit the lateral offsets of the axis to the metal centroid found in each perpendicular plane."""
    pts, a, b = _planes(axis, s, radius, 0.25)
    img = _sample(data, inv, pts, cval)
    wgt = np.clip(img - thr, 0, None) * ((a ** 2 + b ** 2) <= radius ** 2)
    tot = wgt.sum((1, 2))
    ok = tot > 0
    if ok.sum() < degree + 3:
        return axis
    _, e1, e2 = axis.frame(s[ok])
    cen = axis.point(s[ok]) + ((wgt[ok] * a).sum((1, 2)) / tot[ok])[:, None] * e1 \
        + ((wgt[ok] * b).sum((1, 2)) / tot[ok])[:, None] * e2
    d = cen - axis.origin
    sp, pv, pw = d @ axis.u, d @ axis.v, d @ axis.w
    keep = np.ones(len(sp), bool)
    for _ in range(3):  # robust: drop planes that pull away (bone at the entry, a crossing lead)
        cv = np.polyfit(sp[keep], pv[keep], degree, w=np.sqrt(tot[ok][keep]))
        cw = np.polyfit(sp[keep], pw[keep], degree, w=np.sqrt(tot[ok][keep]))
        res = np.hypot(pv - np.polyval(cv, sp), pw - np.polyval(cw, sp))
        new = res <= max(0.5, 3 * 1.4826 * np.median(res[keep]))
        if new.sum() < degree + 3 or np.array_equal(new, keep):
            break
        keep = new
    out = Axis(axis.origin, axis.u, axis.v, axis.w, cv, cw)
    return _straight(out) if degree == 1 else out


def _straight(axis):
    """Turn a linear-offset axis into a plain line (new reference direction, zero offsets)."""
    p0, p1 = axis.point(0.0), axis.point(10.0)
    return Axis.line(p0, p1)


def profile(data, affine, axis, s, radius):
    """Mean intensity in a small disk perpendicular to the lead, for each s."""
    pts, a, b = _planes(axis, s, radius, 0.2)
    img = _sample(data, np.linalg.inv(affine), pts, float(data.min()))
    disk = (a ** 2 + b ** 2) <= radius ** 2 + 1e-9
    return img[:, disk].mean(1)


def _model(s, centers, length, sigma):
    """Unit-height contact boxes blurred by a Gaussian; rows = alternative center sets."""
    d = s[None, None, :] - np.atleast_2d(centers)[:, :, None]
    k = np.sqrt(2) * sigma
    return 0.5 * (erf((d + length / 2) / k) - erf((d - length / 2) / k)).sum(1)


def _corr(m, p):
    mc = m - m.mean(-1, keepdims=True)
    pc = p - p.mean()
    den = np.sqrt((mc ** 2).sum(-1) * (pc ** 2).sum()) + 1e-12
    return (mc @ pc) / den


def fit_contacts(s, p, preset, sigma_base, s_metal=None, anchor=None):
    """Fit contact centers (mm along the lead, deepest first) to the profile `p` sampled at `s`.

    `anchor` = (k, s_k) fixes contact k at s_k (a user correction); the others follow the geometry.
    """
    rel = pr.centers(preset)
    length = preset["contact_length_mm"]
    pitch = float(np.max(np.diff(rel))) if len(rel) > 1 else length
    sigmas = sigma_base * np.array([0.6, 0.9, 1.3, 1.8])
    stretches = np.arange(0.97, 1.0301, 0.01) if len(rel) > 1 else np.ones(1)
    if s_metal is None:
        s_metal = s[0] + 5.0
    best = None
    for sg in sigmas:
        for st in stretches:
            if anchor is not None:
                shifts = np.array([anchor[1] - rel[anchor[0]] * st])
            else:
                c1 = s_metal + length / 2
                shifts = np.arange(c1 - pitch - 1.0, c1 + pitch + 1.0, STEP)
            m = _model(s, shifts[:, None] + rel[None, :] * st, length, sg)
            r = _corr(m, p)
            i = int(np.argmax(r))
            if best is None or r[i] > best[0]:
                best = (float(r[i]), shifts[i] + rel * st, float(st), float(sg))
    r, cen, st, sg = best
    cen = cen.copy()

    # Separate contacts on the CT? Then refine each center locally; otherwise keep the geometry.
    gap = float(np.min(np.diff(rel))) - length if len(rel) > 1 else np.inf
    unit = _model(s, cen[None, :], length, sg)[0]
    bg = float(np.percentile(p, 10))
    at = np.interp(cen, s, p)
    mids = (cen[:-1] + cen[1:]) / 2
    contrast = (at.mean() - np.interp(mids, s, p).mean()) / max(at.mean() - bg, 1e-6) if len(cen) > 1 else 1.0
    resolved = bool(gap > 1.2 * sg and contrast > 0.15)
    modes = ["geometry"] * len(cen)
    half = (length + (gap if np.isfinite(gap) else length)) / 2
    if resolved:
        lim = min(1.0, gap / 2 if np.isfinite(gap) else 1.0)
        for k in range(len(cen)):
            if anchor is not None and k == anchor[0]:
                continue
            win = np.abs(s - cen[k]) <= half
            deltas = np.arange(-lim, lim + 1e-9, 0.05)
            m = _model(s[win], (cen[k] + deltas)[:, None], length, sg)
            cen[k] += deltas[int(np.argmax(_corr(m, p[win])))]
            modes[k] = "fitted"
    if anchor is not None:
        modes[anchor[0]] = "manual"
    unit = _model(s, cen[None, :], length, sg)[0]
    conf = []
    for c in cen:
        win = np.abs(s - c) <= half
        conf.append(round(float(np.clip(_corr(unit[win][None], p[win])[0], 0, 1)), 3) if win.sum() > 3 else 0.0)
    return cen, modes, conf, {"r": round(r, 4), "sigma_mm": round(sg, 3), "stretch": round(st, 3),
                              "resolved": resolved}


def _amplitude(m, p):
    mc = m - m.mean()
    b = float(mc @ (p - p.mean()) / max(mc @ mc, 1e-12))
    return float(p.mean() - b * m.mean()), b


def _metal_threshold(data, inv, axis, s, cval):
    pts, a, b = _planes(axis, s, 3.0, 0.5)
    img = _sample(data, inv, pts, cval)[:, (a ** 2 + b ** 2) <= 9.0]
    bg, hi = float(np.median(img)), float(np.percentile(img.max(1), 75))
    return bg + 0.5 * (hi - bg)


def _sigma_base(affine):
    return float(max(0.35, 0.5 * np.sqrt((affine[:3, :3] ** 2).sum(0)).max()))


def _profile_radius(preset):
    return float(np.clip(preset["diameter_mm"] / 2 + 0.25, 0.5, 1.0))


def detect(data, affine, tip, entry, preset, lead="", curved=False, extend_mm=6.0, threshold=None):
    """Find the contacts of one lead. `tip`/`entry` are rough world points; returns a lead dict.

    In the result the axis is re-parametrised so that s = 0 is the physical tip of the lead.
    """
    inv = np.linalg.inv(affine)
    cval = float(data.min())
    axis = Axis.line(tip, entry)
    length = float(np.linalg.norm(np.asarray(entry, float) - np.asarray(tip, float)))
    s = np.arange(-extend_mm, length + 0.25, 0.5)
    thr = _metal_threshold(data, inv, axis, s, cval) if threshold is None else float(threshold)
    for radius in (3.0, 2.0, 1.5):
        axis = _refine(data, inv, axis, s, radius, thr, cval, 1)
    if curved:
        axis = _refine(data, inv, axis, s, 1.5, thr, cval, 3)

    span = float(pr.centers(preset)[-1]) + preset["contact_length_mm"]
    s = np.arange(-extend_mm, max(length, span + 5.0) + STEP / 2, STEP)
    p = profile(data, affine, axis, s, _profile_radius(preset))
    bg, hi = np.percentile(p, 10), np.percentile(p, 95)
    above = np.flatnonzero(p > bg + 0.5 * (hi - bg))
    s_metal = float(s[above[0]]) if len(above) else 0.0
    cen, modes, conf, info = fit_contacts(s, p, preset, _sigma_base(affine), s_metal=s_metal)
    s_tip = float(cen[0] - preset["contact_length_mm"] / 2 - preset["tip_offset_mm"])
    info["threshold"] = round(thr, 1)
    out = {"name": lead, "preset": {k: v for k, v in preset.items() if k != "builtin"}, "curved": bool(curved),
           "axis": axis.shifted(s_tip).to_dict(), "fit": info,
           "contacts": [{"index": k, "s": round(float(c - s_tip), 3), "mode": m, "confidence": cf}
                        for k, (c, m, cf) in enumerate(zip(cen, modes, conf))]}
    return out


def refit(data, affine, lead, anchor=None):
    """Re-fit the contacts on the stored axis; `anchor` = (k, s_k) keeps contact k where the user put it."""
    axis = Axis.from_dict(lead["axis"])
    preset = lead["preset"]
    span = float(pr.centers(preset)[-1]) + preset["contact_length_mm"]
    s = np.arange(-6.0, span + preset["tip_offset_mm"] + 6.0, STEP)
    p = profile(data, affine, axis, s, _profile_radius(preset))
    c1 = preset["tip_offset_mm"]  # the distal edge of contact 1 sits here when the tip is s = 0
    cen, modes, conf, info = fit_contacts(s, p, preset, _sigma_base(affine), s_metal=c1, anchor=anchor)
    info["threshold"] = lead.get("fit", {}).get("threshold")
    lead = dict(lead, fit=info)
    lead["contacts"] = [{"index": k, "s": round(float(c), 3), "mode": m, "confidence": cf}
                        for k, (c, m, cf) in enumerate(zip(cen, modes, conf))]
    return lead


def positions(lead):
    """World positions (N, 3) of the contacts, in the space the lead's axis is stored in."""
    return Axis.from_dict(lead["axis"]).point(np.array([c["s"] for c in lead["contacts"]], float))


def views(data, affine, lead, lateral_mm=3.0):
    """What the UI shows for one lead: profile with the current model, and a lengthwise image strip."""
    axis = Axis.from_dict(lead["axis"])
    preset = lead["preset"]
    cs = np.array([c["s"] for c in lead["contacts"]], float)
    s = np.arange(min(-6.0, cs.min() - 6), cs.max() + 8.0, STEP)
    p = profile(data, affine, axis, s, _profile_radius(preset))
    sigma = (lead.get("fit") or {}).get("sigma_mm") or _sigma_base(affine)
    unit = _model(s, cs[None, :], preset["contact_length_mm"], sigma)[0]
    a0, b = _amplitude(unit, p)
    g = np.arange(-lateral_mm, lateral_mm + 1e-9, 0.2)
    c = axis.point(s)
    _, e1, e2 = axis.frame(s)
    inv = np.linalg.inv(affine)
    strips = [_sample(data, inv, c[:, None] + g[None, :, None] * e[:, None], float(data.min())) for e in (e1, e2)]
    r = lambda x: np.round(np.asarray(x, float), 1).tolist()
    return {"s0": float(s[0]), "ds": STEP, "profile": r(p), "model": r(a0 + b * unit),
            "lateral0": float(g[0]), "dlateral": 0.2, "strip1": r(strips[0]), "strip2": r(strips[1])}


def cross_section(data, affine, lead, s, radius=4.0, h=0.1):
    """Image of the plane perpendicular to the lead at distance s (rows: e1, columns: e2)."""
    axis = Axis.from_dict(lead["axis"])
    pts, _, _ = _planes(axis, np.array([float(s)]), radius, h)
    img = _sample(data, np.linalg.inv(affine), pts, float(data.min()))[0]
    return {"radius": radius, "h": h, "values": np.round(img, 1).tolist()}
