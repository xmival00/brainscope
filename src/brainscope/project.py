"""A project: one folder with `project.json`, the imported scans, a display cache and the outputs.

Spaces: each scan has its raw world (its file header) and its corrected world (after the user's orientation
fix). Leads are stored in the CT's raw world, so they survive re-orienting and re-coregistering; MRI
coordinates (the output space: the corrected MRI world) are always derived:
    CT raw world --correction(ct)--> CT corrected --coreg--> MRI corrected.
"""
from __future__ import annotations

import copy
import datetime
import itertools
import hashlib
import json
import os
import re
import shutil
import threading
import zipfile
from pathlib import Path

import numpy as np
from scipy import ndimage

from . import __version__, acpc, detect, register, volume
from . import localizer as loc
from . import orientation as ori
from . import presets as pr
from .transforms import apply, rigid, write_itk

FORMAT = "brainscope-project"
LEADS_FORMAT = "brainscope-leads"
ROLES = ("mri", "ct")
SPECIES = ("human", "pig", "other")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}")
DISPLAY_MAX_VOXELS = {"mri": 100e6, "ct": 40e6}  # larger volumes fail to load in the viewer (seen: 300M voxels)
DISPLAY_MARGIN_MM = 20.0
TSV_COLUMNS = ["name", "x", "y", "z", "size", "group", "type", "material", "manufacturer", "hemisphere",
               "lead", "contact_index", "mri_i", "mri_j", "mri_k", "ct_x", "ct_y", "ct_z", "fit_mode",
               "confidence", "acpc_x", "acpc_y", "acpc_z", "frame_x", "frame_y", "frame_z"]
PLAN_COLUMNS = ["lead", "point", "space", "planned_x", "planned_y", "planned_z", "delivered_x", "delivered_y",
                "delivered_z", "dx", "dy", "dz", "distance", "depth", "radial"]


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def jsonable(o):
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def _write_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=jsonable))
    os.replace(tmp, path)


def check_name(name, what="name"):
    name = str(name).strip()
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"{what} may use letters, digits, space, '.', '_' and '-' (max 64 characters)")
    return name


def list_projects(root):
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "project.json").is_file())


class Project:
    def __init__(self, root):
        self.root = Path(root)
        self.data = json.loads((self.root / "project.json").read_text())
        if self.data.get("format") != FORMAT:
            raise ValueError("this folder is not a brainscope project")
        self.lock = threading.RLock()
        self._vols = {}
        self.undo_stack, self.redo_stack = [], []

    @classmethod
    def create(cls, root):
        root = Path(root)
        if (root / "project.json").exists():
            raise ValueError("a project with this name already exists")
        for d in ("scans", "cache", "outputs"):
            (root / d).mkdir(parents=True, exist_ok=True)
        _write_json(root / "project.json", {
            "format": FORMAT, "version": 1, "created": _now(), "app_version": __version__,
            "subjects": [{"id": "sub-01", "species": "human", "scans": {}, "coreg": None, "leads": []}]})
        return cls(root)

    # ---- state, undo ----
    @property
    def subject(self):
        return self.data["subjects"][0]

    @property
    def leads(self):
        return self.subject["leads"]

    def save(self):
        self.data["modified"] = _now()
        _write_json(self.root / "project.json", self.data)

    def checkpoint(self):
        self.undo_stack.append(json.dumps(self.data, default=jsonable))
        del self.undo_stack[:-50]
        self.redo_stack.clear()

    def undo(self):
        if not self.undo_stack:
            raise ValueError("nothing to undo")
        self.redo_stack.append(json.dumps(self.data, default=jsonable))
        self.data = json.loads(self.undo_stack.pop())
        self.save()

    def redo(self):
        if not self.redo_stack:
            raise ValueError("nothing to redo")
        self.undo_stack.append(json.dumps(self.data, default=jsonable))
        self.data = json.loads(self.redo_stack.pop())
        self.save()

    # ---- scans and spaces ----
    def scan(self, role, required=True):
        s = self.subject["scans"].get(role)
        if s is None and required:
            raise ValueError(f"import the {role.upper()} first")
        return s

    def path(self, role):
        return self.root / self.scan(role)["file"]

    def import_scan(self, role, src, source_name=None):
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        src = Path(src)
        name = source_name or src.name
        if not volume.is_supported(name):
            raise ValueError("expected a NIfTI (.nii, .nii.gz), MGZ/MGH, NRRD or MHA/MHD file; "
                             "for DICOM use 'DICOM folder…'")
        sha = volume.sha256(src)
        if volume.is_nifti(name):
            dst = self.root / "scans" / f"{role}-{sha[:12]}{'.nii.gz' if name.lower().endswith('.gz') else '.nii'}"
            if not dst.exists():
                shutil.copyfile(src, dst)
        else:  # other formats are converted once, on import
            dst = self.root / "scans" / f"{role}-{sha[:12]}.nii.gz"
            if not dst.exists():
                volume.to_nifti(src, dst)
        _, shape = volume.raw_affine(dst)
        if len(shape) < 3 or min(shape[:3]) < 2:
            raise ValueError("expected a 3D volume")
        self.checkpoint()
        self.subject["scans"][role] = {"file": f"scans/{dst.name}", "source_name": Path(name).name, "sha256": sha,
                                       "shape": [int(x) for x in shape[:3]], "orient": np.eye(3, dtype=int).tolist(),
                                       "left_confirmed": False, "left_note": ""}
        self.subject["coreg"] = None
        if role == "ct":
            self.subject["leads"] = []  # contacts were measured on the previous CT
            self.subject["scans"]["ct"]["levels"] = register.ct_levels(self.volume("ct").data)
        self.save()

    def raw_affine(self, role):
        return volume.raw_affine(self.path(role))[0]

    def correction(self, role):
        s = self.scan(role)
        return ori.correction(s["orient"], self.raw_affine(role), s["shape"])

    def corrected_affine(self, role):
        return self.correction(role) @ self.raw_affine(role)

    def coreg_matrix(self):
        c = self.subject.get("coreg")
        if not c:
            raise ValueError("coregister the CT to the MRI first")
        return np.array(c["ct_to_mri"], float)

    def ct_raw_to_mri(self):
        return self.coreg_matrix() @ self.correction("ct")

    def volume(self, role):
        sha = self.scan(role)["sha256"]
        hit = self._vols.get(role)
        if hit is None or hit[0] != sha:
            self._vols[role] = (sha, volume.load(self.path(role)))
        return self._vols[role][1]

    def ct_levels(self):
        s = self.scan("ct")
        if "levels" not in s:  # projects made before 0.1.2
            s["levels"] = register.ct_levels(self.volume("ct").data)
            self.save()
        return s["levels"]

    def set_species(self, species):
        if species not in SPECIES:
            raise ValueError(f"species must be one of {SPECIES}")
        self.checkpoint()
        self.subject["species"] = species
        self.save()

    def orient(self, role, op=None, reset=False, left_confirmed=None, left_note=None):
        s = self.scan(role)
        self.checkpoint()
        if reset or op:
            before = self.correction(role)
            s["orient"] = (np.eye(3, dtype=int) if reset else ori.apply_op(s["orient"], op)).tolist()
            if role == "mri":  # points marked on the MRI stay on the same anatomy
                move = self.correction("mri") @ np.linalg.inv(before)
                a = self.subject.get("acpc") or {}
                for k in acpc.POINTS:
                    if k in a:
                        a[k] = apply(move, a[k]).tolist()
                if self.subject.get("roi"):
                    self.subject["roi"]["center"] = apply(move, self.subject["roi"]["center"]).tolist()
            s["left_confirmed"] = False
            self.subject["coreg"] = None  # the corrected spaces changed
        if left_confirmed is not None:
            s["left_confirmed"] = bool(left_confirmed)
        if left_note is not None:
            s["left_note"] = str(left_note)[:200]
        self.save()

    # ---- coregistration ----
    def set_roi(self, center=None, size=None, clear=False):
        """Box (MRI world axes, edge lengths in mm) that limits what coregistration compares, e.g. the brain."""
        self.scan("mri")
        self.checkpoint()
        if clear:
            self.subject["roi"] = None
        else:
            size = [float(x) for x in size]
            if len(size) != 3 or min(size) < 10:
                raise ValueError("the box must be at least 10 mm in each direction")
            self.subject["roi"] = {"center": [float(x) for x in center], "size": size}
        self.save()

    def roi_file(self):
        """The box as a thin outline on the MRI grid, for the viewer."""
        roi = self.subject.get("roi")
        if not roi:
            raise ValueError("no region of interest set")
        aff, shape = self.corrected_affine("mri"), np.array(self.scan("mri")["shape"])
        key = hashlib.sha1(json.dumps(roi).encode() + aff.tobytes()).hexdigest()[:12]
        f = self.root / "cache" / f"roi-{key}.nii.gz"
        if not f.exists():
            for old in (self.root / "cache").glob("roi-*.nii.gz"):
                old.unlink()
            h = np.array(roi["size"]) / 2
            c = np.array(roi["center"])
            corners = [c + h * np.array([sx, sy, sz]) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
            vox = apply(np.linalg.inv(aff), corners)
            lo = np.clip(np.round(vox.min(0)).astype(int), 0, shape - 1)
            hi = np.clip(np.round(vox.max(0)).astype(int), 0, shape - 1)
            lab = np.zeros(tuple(shape), np.uint8)
            box = tuple(slice(a, b + 1) for a, b in zip(lo, hi))
            lab[box] = 1
            t = np.maximum(1, np.round(1.0 / np.sqrt((aff[:3, :3] ** 2).sum(0))).astype(int))  # ~1 mm thick
            inner = tuple(slice(a + w, b + 1 - w) for a, b, w in zip(lo, hi, t))
            lab[inner] = 0
            volume.save(f, lab, aff)
        return f

    def coregister(self, crop_center=None, crop_radius=None, from_current=False):
        ct, mri = self.volume("ct"), self.volume("mri")
        roi = self.subject.get("roi")
        if roi and crop_center is None:
            crop_center, crop_radius = roi["center"], np.array(roi["size"]) / 2
        c = self.subject.get("coreg")
        init = np.array(c["ct_to_mri"]) if from_current and c else None
        m, metric = register.coregister(ct.data, self.corrected_affine("ct"), mri.data, self.corrected_affine("mri"),
                                        species=self.subject["species"], init=init, levels=self.ct_levels(),
                                        crop_center=crop_center, crop_radius=crop_radius)
        self.checkpoint()
        self.subject["coreg"] = {"ct_to_mri": m.tolist(), "metric": round(metric, 5), "accepted": False,
                                 "method": "rigid mutual information", "when": _now()}
        self.save()

    def nudge(self, rot=(0, 0, 0), trans=(0, 0, 0)):
        c = self.subject.get("coreg")
        m = np.array(c["ct_to_mri"]) if c else np.eye(4)
        s = self.scan("mri")
        center = apply(self.corrected_affine("mri"), (np.array(s["shape"]) - 1) / 2)
        self.checkpoint()
        self.subject["coreg"] = {"ct_to_mri": (rigid(rot, trans, center) @ m).tolist(), "metric": None,
                                 "accepted": False, "method": "manual adjustment", "when": _now()}
        self.save()

    def reset_coreg(self):
        self.checkpoint()
        self.subject["coreg"] = None
        self.save()

    def accept_coreg(self, accepted=True):
        self.coreg_matrix()
        self.checkpoint()
        self.subject["coreg"]["accepted"] = bool(accepted)
        self.save()

    # ---- leads ----
    def lead_index(self, name):
        for i, lead in enumerate(self.leads):
            if lead["name"] == name:
                return i
        raise ValueError(f"no lead named {name!r}")

    def _preset(self, preset):
        if isinstance(preset, dict):
            return pr.validate(preset)
        all_ = pr.load_all()
        if preset not in all_:
            raise ValueError(f"unknown preset {preset!r}")
        return all_[preset]

    def _detect(self, name, preset, tip_mri, entry_mri, curved):
        to_raw = np.linalg.inv(self.ct_raw_to_mri())
        ct = self.volume("ct")
        lead = detect.detect(ct.data, ct.affine, apply(to_raw, tip_mri), apply(to_raw, entry_mri), preset,
                             lead=name, curved=curved)
        lead.update(tip_mri=[float(x) for x in tip_mri], entry_mri=[float(x) for x in entry_mri], created=_now())
        return lead

    def add_lead(self, name, preset, tip_mri, entry_mri, curved=False, hemisphere="n/a"):
        name = check_name(name, "lead name")
        lead = self._detect(name, self._preset(preset), tip_mri, entry_mri, curved)
        lead["hemisphere"] = hemisphere
        self.checkpoint()
        try:
            self.leads[self.lead_index(name)] = lead
        except ValueError:
            self.leads.append(lead)
        self.save()
        return lead

    def edit_lead(self, name, action, **kw):
        i = self.lead_index(name)
        lead = copy.deepcopy(self.leads[i])
        ct = self.volume("ct") if action in ("refit", "redetect") else None
        if action == "shift":
            for c in lead["contacts"]:
                c["s"] = round(c["s"] + float(kw["ds"]), 3)
                c["mode"] = "manual"
        elif action == "move":
            c = lead["contacts"][int(kw["index"])]
            c["s"], c["mode"] = round(float(kw["s"]), 3), "manual"
        elif action == "refit":
            anchor = (int(kw["index"]), float(kw["s"])) if kw.get("index") is not None else None
            lead = detect.refit(ct.data, ct.affine, lead, anchor=anchor)
        elif action == "redetect":
            preset = self._preset(kw.get("preset") or lead["preset"])
            new = self._detect(lead["name"], preset, kw.get("tip_mri", lead["tip_mri"]),
                               kw.get("entry_mri", lead["entry_mri"]), bool(kw.get("curved", lead["curved"])))
            new["hemisphere"] = lead.get("hemisphere", "n/a")
            lead = new
        elif action == "rename":
            new = check_name(kw["new_name"], "lead name")
            if any(x["name"] == new for x in self.leads if x is not self.leads[i]):
                raise ValueError(f"a lead named {new!r} already exists")
            lead["name"] = new
        elif action == "hemisphere":
            lead["hemisphere"] = str(kw["hemisphere"])
        elif action == "plan":
            if kw.get("clear"):
                lead.pop("plan", None)
            else:
                space, point = kw.get("space", "acpc"), str(kw.get("point", "tip"))
                if space not in ("acpc", "mri", "frame"):
                    raise ValueError("space must be 'acpc', 'frame' or 'mri'")
                if point != "tip" and not (point[:1] == "c" and point[1:].isdigit()
                                           and int(point[1:]) < len(lead["contacts"])):
                    raise ValueError("point must be 'tip' or 'c<contact number>'")
                entry = kw.get("entry")
                lead["plan"] = {"target": [float(x) for x in kw["target"]], "space": space, "point": point,
                                "entry": [float(x) for x in entry] if entry else None}
        elif action != "delete":
            raise ValueError(f"unknown action {action!r}")
        self.checkpoint()
        if action == "delete":
            del self.leads[i]
        else:
            self.leads[i] = lead
        self.save()

    def contacts(self, lead=None):
        """One row per contact: everything the TSV export and the UI need."""
        m = self.ct_raw_to_mri()
        c_ct = self.correction("ct")
        inv_mri = np.linalg.inv(self.corrected_affine("mri"))
        to_acpc = self.acpc_matrix()
        to_frame = self.frame_matrix()
        rows = []
        for L in self.leads if lead is None else [self.leads[self.lead_index(lead)]]:
            raw = detect.positions(L)
            if not len(raw):
                continue
            p = L["preset"]
            xyz, ijk, ctc = apply(m, raw), apply(inv_mri, apply(m, raw)), apply(c_ct, raw)
            ap = apply(to_acpc, xyz) if to_acpc is not None else [[None] * 3] * len(xyz)
            fr = apply(to_frame, ctc) if to_frame is not None else [[None] * 3] * len(xyz)
            for c, a, b, d, e, f in zip(L["contacts"], xyz, ijk, ctc, ap, fr):
                rows.append({
                    "name": pr.label(p, L["name"], c["index"]), "x": a[0], "y": a[1], "z": a[2],
                    "size": np.pi * p["diameter_mm"] * p["contact_length_mm"], "group": L["name"],
                    "type": p.get("type", "depth"), "material": p.get("material", ""),
                    "manufacturer": p.get("manufacturer", ""), "hemisphere": L.get("hemisphere", "n/a"),
                    "lead": L["name"], "contact_index": c["index"], "mri_i": b[0], "mri_j": b[1], "mri_k": b[2],
                    "ct_x": d[0], "ct_y": d[1], "ct_z": d[2], "fit_mode": c["mode"], "confidence": c["confidence"],
                    "acpc_x": e[0], "acpc_y": e[1], "acpc_z": e[2], "frame_x": f[0], "frame_y": f[1],
                    "frame_z": f[2], "s": c["s"]})
        return rows

    # ---- AC/PC ----
    def set_acpc(self, point=None, xyz=None, origin=None, clear=False):
        a = dict(self.subject.get("acpc") or {"origin": "mcp"})
        if clear:
            a = None
        else:
            if point is not None:
                if point not in acpc.POINTS:
                    raise ValueError(f"point must be one of {acpc.POINTS}")
                a[point] = [float(x) for x in xyz]
            if origin is not None:
                if origin not in acpc.ORIGINS:
                    raise ValueError(f"origin must be one of {acpc.ORIGINS}")
                a["origin"] = origin
            if all(k in a for k in acpc.POINTS):
                acpc.matrix(a["ac"], a["pc"], a["msp"], a["origin"])  # validates the three points
        self.checkpoint()
        self.subject["acpc"] = a
        self.save()

    def acpc_matrix(self):
        """MRI world -> AC/PC coordinates, or None while AC, PC and the midline point are not all set."""
        a = self.subject.get("acpc") or {}
        if not all(k in a for k in acpc.POINTS):
            return None
        return acpc.matrix(a["ac"], a["pc"], a["msp"], a.get("origin", "mcp"))

    def acpc_file(self):
        """Small balls on the MRI grid at AC (1), PC (2) and the midline point (3), for the viewer."""
        a = self.subject.get("acpc") or {}
        aff = self.corrected_affine("mri")
        key = hashlib.sha1(json.dumps(a, sort_keys=True).encode() + aff.tobytes()).hexdigest()[:12]
        f = self.root / "cache" / f"acpc-{key}.nii.gz"
        if not f.exists():
            for old in (self.root / "cache").glob("acpc-*.nii.gz"):
                old.unlink()
            pts = [(a[k], v) for v, k in enumerate(acpc.POINTS, start=1) if k in a]
            volume.save(f, _balls(tuple(self.scan("mri")["shape"]), aff, pts, 1.5), aff)
        return f

    # ---- stereotactic frame (N-bar localizer) ----
    def frame_model(self):
        f = self.subject.get("frame") or {}
        return f.get("model") or loc.default_model()

    def set_frame_model(self, model=None, reset=False):
        m = loc.default_model() if reset else loc.validate(model)
        f = self.subject.get("frame") or {}
        self.checkpoint()
        if f.get("model") and f["model"]["plates"] == m["plates"] and not reset:
            f["model"] = m  # same rods (e.g. only the expected mounting or the notes changed): keep the fit
        else:
            self.subject["frame"] = {"model": m}  # new geometry: an earlier fit no longer applies
        self.save()

    def detect_frame(self, threshold=None):
        ct = self.volume("ct")
        thr = float(threshold) if threshold not in (None, "") else float(self.ct_levels()["overlay_bone"][0]) + 200
        model = self.frame_model()
        lines, groups, fits = loc.detect(ct.data, self.corrected_affine("ct"), model, thr)
        rods = [[(L["c"] + L["t0"] * L["u"]).tolist(), (L["c"] + L["t1"] * L["u"]).tolist()]
                for g in groups for L in (lines[i] for i in g)]
        self.checkpoint()
        self.subject["frame"] = {"model": model, "threshold": thr, "rods": rods, "chosen": 0, "when": _now(),
                                 "fits": [dict(f, matrix=f["matrix"].tolist(), rms=round(f["rms"], 3)) for f in fits]}
        self.save()

    def choose_frame(self, index):
        f = self.subject.get("frame") or {}
        if not 0 <= int(index) < len(f.get("fits", [])):
            raise ValueError("no such fit")
        self.checkpoint()
        f["chosen"] = int(index)
        self.save()

    def frame_matrix(self):
        """CT (orientation-corrected) world -> frame coordinates, or None without a fitted frame."""
        f = self.subject.get("frame") or {}
        if not f.get("fits"):
            return None
        return np.array(f["fits"][f.get("chosen", 0)]["matrix"], float)

    def frame_from_mri(self):
        m = self.frame_matrix()
        return None if m is None or not self.subject.get("coreg") else m @ np.linalg.inv(self.coreg_matrix())

    def frame_file(self):
        """Viewer overlay in the CT display world: detected rods (1) and the fitted model's rods (2)."""
        f = self.subject.get("frame") or {}
        to_disp = self.coreg_matrix() if self.subject.get("coreg") else np.eye(4)
        segs = [(1, np.array(a), np.array(b)) for a, b in f.get("rods", [])]
        m = self.frame_matrix()
        if m is not None:
            inv = np.linalg.inv(m)
            segs += [(2, apply(inv, a), apply(inv, b)) for _, a, b in loc.segments(f["model"])]
        segs = [(v, apply(to_disp, a), apply(to_disp, b)) for v, a, b in segs]
        if not segs:
            raise ValueError("no frame detected yet")
        key = hashlib.sha1(json.dumps([[v, a.tolist(), b.tolist()] for v, a, b in segs]).encode()).hexdigest()[:12]
        out = self.root / "cache" / f"frame-{key}.nii.gz"
        if not out.exists():
            for old in (self.root / "cache").glob("frame-*.nii.gz"):
                old.unlink()
            pts = np.vstack([np.vstack([a, b]) for _, a, b in segs])
            lo, hi = pts.min(0) - 5, pts.max(0) + 5
            aff = np.diag([1.0, 1.0, 1.0, 1.0])
            aff[:3, 3] = lo
            lab = np.zeros(tuple(np.ceil(hi - lo).astype(int) + 1), np.uint8)
            for v, a, b in segs:
                n = int(np.linalg.norm(b - a) * 2) + 2
                vox = np.round(a + np.linspace(0, 1, n)[:, None] * (b - a) - lo).astype(int)
                for d in itertools.product((-1, 0, 1), repeat=3):  # ~3 mm thick for visibility
                    q = np.clip(vox + np.array(d), 0, np.array(lab.shape) - 1)
                    lab[q[:, 0], q[:, 1], q[:, 2]] = np.maximum(lab[q[:, 0], q[:, 1], q[:, 2]], v)
            volume.save(out, lab, aff)
        return out

    # ---- planned versus delivered ----
    def delivered_point(self, L, point="tip"):
        """MRI-world position of the lead tip (\"tip\") or of contact k (\"c<k>\")."""
        axis = detect.Axis.from_dict(L["axis"])
        s = 0.0 if point == "tip" else L["contacts"][int(point[1:])]["s"]
        return apply(self.ct_raw_to_mri(), axis.point(s))

    def plan_result(self, L):
        pl = L.get("plan")
        if not pl:
            return None
        p = self.delivered_point(L, pl.get("point", "tip"))
        if pl["space"] == "acpc":
            m = self.acpc_matrix()
            if m is None:
                return {"error": "set AC, PC and the midline point first"}
            p = apply(m, p)
        elif pl["space"] == "frame":
            m = self.frame_from_mri()
            if m is None:
                return {"error": "detect the stereotactic frame first"}
            p = apply(m, p)
        res = acpc.compare(pl["target"], p, pl.get("entry"))
        res["delivered"] = [round(float(x), 3) for x in p]
        return res

    def lead_view(self, name):
        ct = self.volume("ct")
        return detect.views(ct.data, ct.affine, self.leads[self.lead_index(name)])

    def cross_section(self, name, s):
        ct = self.volume("ct")
        return detect.cross_section(ct.data, ct.affine, self.leads[self.lead_index(name)], s)

    # ---- files for the viewer ----
    def display_file(self, role, crop=True):
        """What the viewer loads: the scan with its corrected header (CT: also coregistered). A large CT is cropped
        to the MRI's field of view once coregistered, and anything still too large is averaged down; this is for
        display only (detection and registration always use the full scan)."""
        s = self.scan(role)
        shape = np.array(s["shape"])
        aff = self.correction(role) @ self.raw_affine(role)
        c = self.subject.get("coreg")
        if role == "ct" and c:
            aff = np.array(c["ct_to_mri"]) @ aff
        lo, hi = np.zeros(3, int), shape.copy()
        if role == "ct" and c and crop and self.scan("mri", required=False):
            roi = self.subject.get("roi")
            if roi:  # the region of interest (+ margin) is enough, and keeps the CT at full resolution
                h, ce = np.array(roi["size"]) / 2, np.array(roi["center"])
                corners = np.array([ce + h * np.array([i, j, k]) for i in (-1, 1) for j in (-1, 1) for k in (-1, 1)])
            else:
                n = np.array(self.scan("mri")["shape"]) - 1
                corners = apply(self.corrected_affine("mri"), [[i * n[0], j * n[1], k * n[2]]
                                                               for i in (0, 1) for j in (0, 1) for k in (0, 1)])
            vox = apply(np.linalg.inv(aff), corners)
            margin = DISPLAY_MARGIN_MM / np.sqrt((aff[:3, :3] ** 2).sum(0))
            lo = np.clip(np.floor(vox.min(0) - margin).astype(int), 0, shape)
            hi = np.clip(np.ceil(vox.max(0) + margin).astype(int) + 1, 0, shape)
            if np.any(hi - lo < 2):  # no overlap with the MRI: show all of it
                lo, hi = np.zeros(3, int), shape.copy()
        limit = DISPLAY_MAX_VOXELS[role]
        whole = not lo.any() and np.array_equal(hi, shape) and np.prod(shape.astype(float)) <= limit
        key = hashlib.sha1(np.round(aff, 5).tobytes() + s["sha256"].encode() + lo.tobytes() + hi.tobytes()
                           + str(limit).encode()).hexdigest()[:12]
        prefix = f"{role}-{'view' if crop else 'full'}"
        f = self.root / "cache" / f"{prefix}-{key}.nii"
        if not f.exists():
            for old in (self.root / "cache").glob(f"{prefix}-*.nii"):
                old.unlink()
            if whole:
                volume.save_reheadered(self.path(role), f, aff)
            else:
                shift = np.eye(4)
                shift[:3, 3] = lo
                data = self.volume(role).data[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]]
                data, sub = _reduce(data, aff @ shift, limit)
                volume.save(f, np.round(data).astype(np.int16) if role == "ct" else data.astype(np.float32), sub)
        return f

    def contacts_file(self):
        """Label volume on the MRI grid: a small ball per contact, value = lead number."""
        rows = self.contacts()
        aff = self.corrected_affine("mri")
        key = hashlib.sha1(json.dumps([[r["lead"], r["x"], r["y"], r["z"]] for r in rows]).encode()
                           + aff.tobytes()).hexdigest()[:12]
        f = self.root / "cache" / f"contacts-{key}.nii.gz"
        if not f.exists():
            for old in (self.root / "cache").glob("contacts-*.nii.gz"):
                old.unlink()
            volume.save(f, self.contact_labels(rows, by="lead"), aff)
        return f

    def contact_labels(self, rows, by="lead"):
        shape = tuple(self.scan("mri")["shape"])
        aff = self.corrected_affine("mri")
        inv = np.linalg.inv(aff)
        sp = np.sqrt((aff[:3, :3] ** 2).sum(0))
        lab = np.zeros(shape, np.uint16 if by == "contact" else np.uint8)
        leads = [L["name"] for L in self.leads]
        for n, r in enumerate(rows, start=1):
            L = self.leads[leads.index(r["lead"])]
            rad = max(1.0, L["preset"]["contact_length_mm"] / 2) / sp
            c = apply(inv, [r["x"], r["y"], r["z"]])
            lo = np.maximum(np.floor(c - rad).astype(int), 0)
            hi = np.minimum(np.ceil(c + rad).astype(int) + 1, shape)
            if np.any(hi <= lo):
                continue
            g = np.meshgrid(*[np.arange(a, b) for a, b in zip(lo, hi)], indexing="ij")
            ball = sum(((g[i] - c[i]) / rad[i]) ** 2 for i in range(3)) <= 1
            val = n if by == "contact" else leads.index(r["lead"]) % 254 + 1
            lab[tuple(slice(a, b) for a, b in zip(lo, hi))][ball] = val
        return lab

    # ---- results in our own format ----
    def leads_document(self):
        m = self.ct_raw_to_mri()
        s = self.scan("mri")
        rows = {(r["lead"], r["contact_index"]): r for r in self.contacts()}
        out = []
        for L in self.leads:
            out.append({k: L.get(k) for k in ("name", "hemisphere", "preset", "curved", "fit", "plan")} | {
                "axis": detect.Axis.from_dict(L["axis"]).transformed(m).to_dict(),
                "contacts": [dict(c, label=rows[L["name"], c["index"]]["name"],
                                  x=rows[L["name"], c["index"]]["x"], y=rows[L["name"], c["index"]]["y"],
                                  z=rows[L["name"], c["index"]]["z"]) for c in L["contacts"]]})
        return {"format": LEADS_FORMAT, "version": 1, "app_version": __version__, "created": _now(),
                "space": {"name": "MRI, orientation-corrected", "convention": "RAS+", "units": "mm",
                          "mri_source": s["source_name"], "mri_sha256": s["sha256"],
                          "mri_affine": self.corrected_affine("mri").tolist()},
                "acpc": self.subject.get("acpc"), "leads": out}

    def import_leads(self, doc, replace=False):
        if not isinstance(doc, dict) or doc.get("format") != LEADS_FORMAT:
            raise ValueError("not a brainscope leads file")
        to_raw = np.linalg.inv(self.ct_raw_to_mri())
        warnings = []
        if doc.get("space", {}).get("mri_sha256") != self.scan("mri")["sha256"]:
            warnings.append("These results were made with a different MRI file; their MRI coordinates were "
                            "applied as stored.")
        new = []
        for L in doc.get("leads", []):
            new.append({"name": check_name(L["name"], "lead name"), "hemisphere": L.get("hemisphere", "n/a"),
                        "preset": pr.validate(L["preset"]), "curved": bool(L.get("curved")), "fit": L.get("fit"),
                        "axis": detect.Axis.from_dict(L["axis"]).transformed(to_raw).to_dict(),
                        "contacts": [{"index": int(c["index"]), "s": float(c["s"]), "mode": c.get("mode", "manual"),
                                      "confidence": c.get("confidence", 0.0)} for c in L["contacts"]],
                        "plan": L.get("plan"), "imported": _now()})
        self.checkpoint()
        if replace:
            self.subject["leads"] = new
        else:
            names = [x["name"] for x in new]
            self.subject["leads"] = [x for x in self.leads if x["name"] not in names] + new
        self.save()
        return warnings

    # ---- export ----
    def export(self, folder=None, tsv=True, leads_file=True, transforms=True, mri=False, ct=False, labels=False,
               space="mri"):
        out = Path(folder) if folder else self.root / "outputs"
        out.mkdir(parents=True, exist_ok=True)
        sid = self.subject["id"]
        files = []
        if tsv:
            rows = self.contacts()
            s = self.scan("mri")
            if space == "acpc":  # AC/PC as the main x, y, z
                a = self.subject.get("acpc") or {}
                if self.acpc_matrix() is None:
                    raise ValueError("set AC, PC and the midline point first, or export in MRI space")
                for r in rows:
                    r["x"], r["y"], r["z"] = r["acpc_x"], r["acpc_y"], r["acpc_z"]
                origin = {"mcp": "the mid-commissural point", "ac": "AC", "pc": "PC"}[a.get("origin", "mcp")]
                cs = {"iEEGCoordinateSystem": "ACPC" if a.get("origin") == "ac" else "Other",
                      "iEEGCoordinateSystemDescription": f"AC/PC coordinates, origin at {origin}: +y from PC "
                      "towards AC, +z towards the midline point (superior), +x to the subject's right; mm.",
                      "BrainscopeACPC": a}
            elif space == "frame":
                if self.frame_matrix() is None:
                    raise ValueError("detect the stereotactic frame first, or export in MRI space")
                for r in rows:
                    r["x"], r["y"], r["z"] = r["frame_x"], r["frame_y"], r["frame_z"]
                fm = self.subject["frame"]["model"]
                cs = {"iEEGCoordinateSystem": "Other",
                      "iEEGCoordinateSystemDescription": f"Stereotactic frame coordinates ({fm['name']}): "
                      f"{fm.get('axes', '')}; mm. Localizer geometry verified: {bool(fm.get('verified'))}.",
                      "BrainscopeFrameFit": {k: v for k, v in self.subject["frame"].items() if k != "rods"}}
            else:
                cs = {"iEEGCoordinateSystem": "Other",
                      "iEEGCoordinateSystemDescription": "World coordinates (RAS+, mm) of the orientation-corrected "
                      f"MRI '{s['source_name']}' (sha256 {s['sha256']})."}
            fmt = lambda v: "n/a" if v in (None, "") else (f"{v:.3f}" if isinstance(v, (float, np.floating)) else str(v))
            lines = ["\t".join(TSV_COLUMNS)] + ["\t".join(fmt(r[c]) for c in TSV_COLUMNS) for r in rows]
            files.append(out / f"{sid}_electrodes.tsv")
            files[-1].write_text("\n".join(lines) + "\n")
            files.append(out / f"{sid}_coordsystem.json")
            _write_json(files[-1], {"iEEGCoordinateUnits": "mm", **cs, "BrainscopeACPCLandmarks":
                                    self.subject.get("acpc"), "BrainscopeMRIAffine": self.corrected_affine("mri").tolist(),
                                    "BrainscopeVersion": __version__})
            plans = [(L, self.plan_result(L)) for L in self.leads if L.get("plan")]
            if plans:
                prow = []
                for L, r in plans:
                    pl = L["plan"]
                    prow.append([L["name"], pl["point"], pl["space"], *pl["target"], *(r.get("delivered") or [None] * 3),
                                 *(r.get(k) for k in ("dx", "dy", "dz", "distance", "depth", "radial"))])
                files.append(out / f"{sid}_plan_vs_delivered.tsv")
                files[-1].write_text("\n".join(["\t".join(PLAN_COLUMNS)] + ["\t".join(fmt(v) for v in r) for r in prow])
                                     + "\n")
        if leads_file:
            files.append(out / f"{sid}_leads.brainscope.json")
            _write_json(files[-1], self.leads_document())
        if transforms:
            m = self.coreg_matrix()
            files.append(out / "ct_to_mri_ras.txt")
            np.savetxt(files[-1], m, fmt="%.8f", header="CT (orientation-corrected) world -> MRI (orientation-"
                       "corrected) world, RAS+ mm, 4x4")
            files.append(out / "ct_raw_to_mri_ras.txt")
            np.savetxt(files[-1], self.ct_raw_to_mri(), fmt="%.8f",
                       header="CT file world (raw header) -> MRI (orientation-corrected) world, RAS+ mm, 4x4")
            files.append(out / "mri_to_ct_itk.tfm")
            write_itk(files[-1], np.linalg.inv(m))  # ITK convention: maps fixed (MRI) points to moving (CT)
        if mri:
            files.append(out / f"{sid}_mri_corrected.nii.gz")
            volume.save_reheadered(self.path("mri"), files[-1], self.corrected_affine("mri"))
        if ct:
            files.append(out / f"{sid}_ct_in_mri.nii.gz")
            volume.save(files[-1], self.ct_in_mri_grid(), self.corrected_affine("mri"))
        if labels:
            rows = self.contacts()
            files.append(out / f"{sid}_contacts.nii.gz")
            volume.save(files[-1], self.contact_labels(rows, by="contact"), self.corrected_affine("mri"))
            files.append(out / f"{sid}_contacts_labels.tsv")
            files[-1].write_text("index\tname\n" + "".join(f"{n}\t{r['name']}\n" for n, r in enumerate(rows, 1)))
        return [str(f) for f in files]

    def ct_in_mri_grid(self):
        """The CT resampled onto the MRI grid (linear), as int16 HU."""
        ct = self.volume("ct")
        shape = tuple(self.scan("mri")["shape"])
        k = np.linalg.inv(ct.affine) @ np.linalg.inv(self.ct_raw_to_mri()) @ self.corrected_affine("mri")
        out = np.empty(shape, np.int16)
        ii, jj = np.meshgrid(np.arange(shape[0]), np.arange(shape[1]), indexing="ij")
        for z in range(shape[2]):
            vox = np.stack([ii.ravel(), jj.ravel(), np.full(ii.size, z)], 1)
            v = ndimage.map_coordinates(ct.data, apply(k, vox).T, order=1, cval=float(ct.data.min()))
            out[:, :, z] = np.clip(np.round(v), -32768, 32767).reshape(ii.shape)
        return out

    # ---- debug bundle (small enough to share; no names, no original headers) ----
    def debug_bundle(self, coarse_mm=2.0, margin_mm=8.0, max_side_mm=90.0):
        """Zip with both scans at about `coarse_mm` (raw headers), a full-resolution CT crop around each lead,
        and the project state, so a problem can be reproduced without the full scans."""
        out = self.root / "outputs" / "debug"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        files = []
        for role in ROLES:
            if self.scan(role, required=False):
                v = self.volume(role)
                data, aff = _block_mean(v.data, v.affine, coarse_mm)
                files.append(out / f"{role}_coarse.nii.gz")
                volume.save(files[-1], np.round(data).astype(np.int16 if role == "ct" else np.float32), aff)
        if self.scan("ct", required=False) and self.leads:
            ct = self.volume("ct")
            inv = np.linalg.inv(ct.affine)
            to_raw = np.linalg.inv(self.ct_raw_to_mri()) if self.subject.get("coreg") else None
            for L in self.leads:
                pts = [detect.positions(L)]
                if to_raw is not None and L.get("tip_mri"):
                    pts.append(apply(to_raw, [L["tip_mri"], L["entry_mri"]]))
                pts = np.vstack(pts)
                c = pts.mean(0)
                half = np.minimum((pts.max(0) - pts.min(0)) / 2 + margin_mm, max_side_mm / 2)
                corners = np.array([[c[0] + sx * half[0], c[1] + sy * half[1], c[2] + sz * half[2]]
                                    for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)])
                vox = apply(inv, corners)
                lo = np.clip(np.floor(vox.min(0)).astype(int), 0, np.array(ct.data.shape) - 1)
                hi = np.clip(np.ceil(vox.max(0)).astype(int) + 1, 1, ct.data.shape)
                crop = ct.data[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]]
                shift = np.eye(4)
                shift[:3, 3] = lo
                files.append(out / f"ct_crop_{re.sub(r'[^A-Za-z0-9_-]', '_', L['name'])}.nii.gz")
                volume.save(files[-1], np.clip(np.round(crop), -32768, 32767).astype(np.int16), ct.affine @ shift)
        state = json.loads(json.dumps(self.data, default=jsonable))
        for role, s in state["subjects"][0]["scans"].items():
            s["source_name"] = f"{role} (name removed)"
        files.append(out / "project.json")
        _write_json(files[-1], state)
        files.append(out / "README.txt")
        files[-1].write_text(
            "brainscope debug bundle\n"
            "- <role>_coarse.nii.gz: the scans block-averaged to about 2 mm, with their ORIGINAL (raw) headers\n"
            "- ct_crop_<lead>.nii.gz: full-resolution CT around each lead, raw CT world coordinates\n"
            "- project.json: orientation fixes, coregistration, leads (file names removed)\n")
        zpath = self.root / "outputs" / f"brainscope-debug-{self.root.name.replace(' ', '_')}.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.write(f, f.name)
        shutil.rmtree(out, ignore_errors=True)
        return str(zpath)

    # ---- for the UI ----
    def summary(self):
        sub = self.subject
        scans = {}
        for role in ROLES:
            s = self.scan(role, required=False)
            if s:
                aff = self.raw_affine(role)
                scans[role] = dict(s, spacing=[round(float(x), 3) for x in np.sqrt((aff[:3, :3] ** 2).sum(0))],
                                   axes=ori.axis_labels(s["orient"]),
                                   center=apply(self.corrected_affine(role), (np.array(s["shape"]) - 1) / 2).tolist())
        if "ct" in scans:
            scans["ct"]["levels"] = self.ct_levels()
        try:
            contacts = self.contacts() if sub.get("coreg") else []
        except ValueError:
            contacts = []
        leads = []
        for L in self.leads:
            try:
                pr_ = self.plan_result(L) if sub.get("coreg") else None
            except (ValueError, IndexError) as e:
                pr_ = {"error": str(e)}
            leads.append({k: L.get(k) for k in ("name", "hemisphere", "preset", "curved", "fit", "tip_mri",
                                                "entry_mri", "plan")}
                         | {"contacts": [r for r in contacts if r["lead"] == L["name"]], "plan_result": pr_})
        am = self.acpc_matrix()
        a = sub.get("acpc") or {}
        fr = sub.get("frame") or {}
        fm = self.frame_matrix() if scans.get("ct") else None
        disp = fm @ np.linalg.inv(self.coreg_matrix()) if fm is not None and sub.get("coreg") else fm
        return {"name": self.root.name, "species": sub["species"], "scans": scans, "coreg": sub.get("coreg"),
                "roi": sub.get("roi"), "acpc": sub.get("acpc"), "acpc_matrix": am.tolist() if am is not None else None,
                "acpc_distance": round(float(np.linalg.norm(np.subtract(a["ac"], a["pc"]))), 2)
                if "ac" in a and "pc" in a else None,
                "frame": {k: v for k, v in fr.items() if k != "rods"} | {"model": self.frame_model(),
                                                                         "n_rods": len(fr.get("rods", []))},
                "frame_from_display": disp.tolist() if disp is not None else None,
                "leads": leads, "can_undo": bool(self.undo_stack), "can_redo": bool(self.redo_stack),
                "folder": str(self.root)}


def _block_mean(data, affine, target_mm):
    """Average blocks of voxels to reach about `target_mm`; the affine keeps the same world positions."""
    sp = np.sqrt((affine[:3, :3] ** 2).sum(0))
    f = np.maximum(1, np.round(target_mm / sp).astype(int))
    n = np.array(data.shape[:3]) // f
    d = data[:n[0] * f[0], :n[1] * f[1], :n[2] * f[2]].reshape(n[0], f[0], n[1], f[1], n[2], f[2]).mean((1, 3, 5))
    scale = np.diag(list(f) + [1]).astype(float)
    scale[:3, 3] = (f - 1) / 2
    return d, affine @ scale


def _reduce(data, affine, limit):
    """Average voxel blocks until the volume has at most `limit` voxels (finest axes first), in slabs to save memory."""
    sp = np.sqrt((affine[:3, :3] ** 2).sum(0))
    f = np.ones(3, int)
    while np.prod(np.array(data.shape) // f, dtype=float) > limit:
        f[np.argmin(sp * f)] += 1
    if (f == 1).all():
        return np.asarray(data, np.float32), affine
    n = np.array(data.shape) // f
    out = np.empty(tuple(n), np.float32)
    step = max(1, 64 // f[2]) * f[2]
    for z0 in range(0, n[2] * f[2], step):
        z1 = min(z0 + step, n[2] * f[2])
        blk = np.asarray(data[:n[0] * f[0], :n[1] * f[1], z0:z1], np.float32)
        out[:, :, z0 // f[2]:z1 // f[2]] = blk.reshape(n[0], f[0], n[1], f[1], (z1 - z0) // f[2], f[2]).mean((1, 3, 5))
    scale = np.diag(list(f) + [1]).astype(float)
    scale[:3, 3] = (f - 1) / 2
    return out, affine @ scale


def _balls(shape, affine, points, radius_mm):
    """uint8 volume with a ball of `radius_mm` at each (world point, value)."""
    lab = np.zeros(shape, np.uint8)
    inv = np.linalg.inv(affine)
    rad = radius_mm / np.sqrt((affine[:3, :3] ** 2).sum(0))
    for p, val in points:
        c = apply(inv, p)
        lo = np.maximum(np.floor(c - rad).astype(int), 0)
        hi = np.minimum(np.ceil(c + rad).astype(int) + 1, shape)
        if np.any(hi <= lo):
            continue
        g = np.meshgrid(*[np.arange(a, b) for a, b in zip(lo, hi)], indexing="ij")
        ball = sum(((g[i] - c[i]) / rad[i]) ** 2 for i in range(3)) <= 1
        lab[tuple(slice(a, b) for a, b in zip(lo, hi))][ball] = val
    return lab
