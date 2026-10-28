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

import nibabel as nib
import re
import shutil
import threading
import zipfile
from pathlib import Path

import numpy as np
from scipy import ndimage

from . import __version__, acpc, brainmodel, detect, register, volume
from . import localizer as loc
from . import orientation as ori
from . import presets as pr
from .transforms import about, apply, rigid, rotation, translation, write_itk

FORMAT = "brainscope-project"
LEADS_FORMAT = "brainscope-leads"
ROLES = ("mri", "ct")
SPECIES = ("human", "pig", "other")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}")
DISPLAY_MAX_VOXELS = {"mri": 100e6, "ct": 40e6}  # larger volumes fail to load in the viewer (seen: 300M voxels)
DISPLAY_MARGIN_MM = 20.0
LEAD_COLORS = ["#ff5d5d", "#3fa7ff", "#3ecf8e", "#f0b429", "#c77dff", "#ff8fab", "#5ce1e6", "#ffd166", "#a3e635", "#fb923c"]
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

    # ---- stereotactic frame (N-bar localizer), per scan (PLAN D33-D36) ----
    def _frame(self):
        """The frame state; a 0.1.6 project (one CT fit in the corrected CT world) is converted on first use."""
        f = self.subject.setdefault("frame", {})
        if "fits" in f and "scans" not in f:
            c = self.correction("ct")
            ci = np.linalg.inv(c)
            fits = [dict(x, matrix=(np.array(x["matrix"]) @ c).tolist()) for x in f["fits"]]
            best = fits[f.get("chosen", 0)]
            f = {"model": f["model"], "use": "ct", "scans": {"ct": {
                "band": [f.get("threshold"), None], "pose": best["matrix"], "fitted": True, "method": "auto",
                "summary": {"rms": best["rms"], "axes": best["axes"], "plates": best.get("plates")},
                "auto": {"lines": [[apply(ci, a).tolist(), apply(ci, b).tolist()] for a, b in f.get("rods", [])],
                         "fits": fits, "chosen": f.get("chosen", 0)}}}}
            self.subject["frame"] = f
        f.setdefault("scans", {})
        f.setdefault("model", loc.default_model())  # stored, so a later edit can tell whether the rods changed
        return f

    def frame_model(self):
        return self._frame().get("model") or loc.default_model()

    def set_frame_model(self, model=None, reset=False):
        m = loc.default_model() if reset else loc.validate(model)
        f = self._frame()
        self.checkpoint()
        same = bool(f.get("model")) and f["model"]["plates"] == m["plates"] and not reset
        f["model"] = m
        if not same:  # new geometry: poses and rod points no longer apply (the intensity bands stay)
            f["scans"] = {k: {"band": s.get("band")} for k, s in f["scans"].items()}
            f.pop("use", None)
        self.save()

    def display_from_raw(self, role):
        """A scan's raw world -> the world the viewer shows it in (the CT: on the MRI once coregistered)."""
        m = self.correction(role)
        return self.coreg_matrix() @ m if role == "ct" and self.subject.get("coreg") else m

    def default_frame_band(self, role):
        if role == "ct":
            return [float(self.ct_levels()["overlay_bone"][0]) + 200, None]
        d = self.volume(role).data.ravel()
        d = d[:: max(1, d.size // 2_000_000)]
        return [float(np.percentile(d[d > 0], 99.5)), None]

    def frame_state(self, role):
        if role not in ("ct", "mri"):
            raise ValueError("the localizer is found in 'ct' or 'mri'")
        self.scan(role)
        s = self._frame()["scans"].setdefault(role, {})
        if not s.get("band") or s["band"][0] is None:
            s["band"] = self.default_frame_band(role)
        s.setdefault("manual", {})
        s.setdefault("excluded", {})
        return s

    def set_frame_band(self, role, band):
        s = self.frame_state(role)
        lo, hi = band
        self.checkpoint()
        s["band"] = [float(lo), None if hi in (None, "") else float(hi)]
        self.save()

    def frame_pose(self, role, op, point=None, axis=None, deg=90.0, move=(0, 0, 0), rot=(0, 0, 0), index=0):
        """Rough alignment of the expected localizer in a scan; points and axes in that scan's display world.
        op: 'reset' (expected mounting, centred on the scan), 'place' (model centre at `point`), 'turn' (`deg` about
        display axis `axis`), 'nudge' (`move` mm, `rot` degrees about the model centre), 'auto' (auto-find fit
        `index`)."""
        s = self.frame_state(role)
        model = self.frame_model()
        disp = self.display_from_raw(role)
        rods = loc.rod_list(model)
        cf = np.mean([q for r in rods for q in (r["p0"], r["p1"])], 0)
        if op == "auto":
            fits = (s.get("auto") or {}).get("fits") or []
            if not 0 <= int(index) < len(fits):
                raise ValueError("no such fit")
            pose, summary = np.array(fits[int(index)]["matrix"]), {k: fits[int(index)].get(k) for k in ("rms", "axes", "plates")}
            s["auto"]["chosen"] = int(index)
        else:
            if op == "reset" or s.get("pose") is None:
                n = (np.array(self.scan(role)["shape"]) - 1) / 2
                mount = np.asarray(model["mount"], float)
                f = np.eye(4)
                f[:3, :3], f[:3, 3] = mount, cf - mount @ apply(disp @ self.raw_affine(role), n)
            else:
                f = np.array(s["pose"]) @ np.linalg.inv(disp)  # display world -> frame
            x = apply(np.linalg.inv(f), cf)  # model centre, display world
            ax = {"x": 0, "y": 1, "z": 2}
            if op == "place":
                a = translation(np.asarray(point, float) - x)
            elif op == "turn":
                r = np.zeros(3)
                r[ax[axis]] = float(deg)
                a = about(rotation(r), x)
            elif op == "nudge":
                a = translation(move) @ about(rotation(rot), x)
            elif op == "reset":
                a = np.eye(4)
            else:
                raise ValueError(f"unknown alignment operation {op!r}")
            pose, summary = f @ np.linalg.inv(a) @ disp, None
        self.checkpoint()
        s.update(pose=pose.tolist(), fitted=op == "auto", method="auto" if op == "auto" else "by hand",
                 summary=summary, tracked=None)
        if op == "auto" and not self._frame().get("use"):
            self._frame()["use"] = role
        self.save()

    def frame_autofind(self, role):
        """Search the whole scan for N patterns (needs rods that stand out in the band); fits become poses to pick."""
        s = self.frame_state(role)
        c = self.correction(role)
        aff = c @ self.raw_affine(role)  # corrected world, so the expected mounting (RAS) can rank the fits
        lines = loc.merge_lines(loc.find_lines(self.volume(role).data, aff, s["band"]))
        groups = loc.group_n(lines)
        ci = np.linalg.inv(c)
        self.checkpoint()
        s["auto"] = {"lines": [[apply(ci, L["c"] + L["t0"] * L["u"]).tolist(), apply(ci, L["c"] + L["t1"] * L["u"]).tolist()]
                               for g in groups for L in (lines[i] for i in g)], "fits": [], "chosen": 0, "when": _now()}
        try:
            fits = loc.fit(lines, groups, self.frame_model())
        except ValueError:
            self.save()  # keep the rods that were found, for the viewer
            raise
        s["auto"]["fits"] = [dict(x, matrix=(x["matrix"] @ c).tolist(), rms=round(x["rms"], 3)) for x in fits]
        self.save()
        self.frame_pose(role, "auto", index=0)

    def frame_refine(self, role):
        """Track every rod from the current pose and fit the model to the centres (PLAN D35)."""
        s = self.frame_state(role)
        if s.get("pose") is None:
            raise ValueError("align the expected localizer first (auto-find or by hand)")
        pose, tracked, res, summ = loc.refine(
            self.volume(role).data, self.raw_affine(role), self.frame_model(), np.array(s["pose"]), s["band"],
            manual={k: [np.array(q) for q in v] for k, v in s["manual"].items()}, excluded=s["excluded"])
        summ["axes"] = loc.describe(pose @ np.linalg.inv(self.correction(role)))
        summ["rms"] = None if summ["rms"] is None else round(summ["rms"], 3)
        self.checkpoint()
        s.update(pose=pose.tolist(), fitted=True, method="tracked", summary=summ, when=_now(), tracked={
            rid: [[round(t, 2), None if q is None else np.round(q, 3).tolist(), src, None if d is None else round(d, 3)]
                  for (t, q, src), d in zip(rows, res[rid])] for rid, rows in tracked.items()})
        if not self._frame().get("use"):
            self._frame()["use"] = role
        self.save()

    def frame_use(self, role):
        if not self.frame_state(role).get("fitted"):
            raise ValueError(f"fit the localizer in the {role.upper()} first")
        self.checkpoint()
        self._frame()["use"] = role
        self.save()

    def _frame_used(self):
        f = self._frame()
        s = f["scans"].get(f.get("use") or "")
        return (f["use"], np.array(s["pose"], float)) if s and s.get("fitted") else (None, None)

    def frame_from_mri(self):
        """MRI (orientation-corrected) world -> frame coordinates, from the scan chosen for it; None if not fitted."""
        role, pose = self._frame_used()
        if role == "mri":
            return pose @ np.linalg.inv(self.correction("mri"))
        if role == "ct" and self.subject.get("coreg"):
            return pose @ np.linalg.inv(self.ct_raw_to_mri())
        return None

    def frame_matrix(self):
        """CT (orientation-corrected) world -> frame coordinates, or None."""
        role, pose = self._frame_used()
        if role == "ct":
            return pose @ np.linalg.inv(self.correction("ct"))
        m = self.frame_from_mri()
        return m @ self.coreg_matrix() if m is not None else None

    def frame_agreement(self):
        """Mean distance (mm) near the brain between the frame fitted in the CT and in the MRI, or None."""
        sc = self._frame()["scans"]
        if not all((sc.get(r) or {}).get("fitted") for r in ("ct", "mri")) or not self.subject.get("coreg"):
            return None
        a = np.array(sc["mri"]["pose"]) @ np.linalg.inv(self.correction("mri"))
        b = np.array(sc["ct"]["pose"]) @ np.linalg.inv(self.ct_raw_to_mri())
        roi = self.subject.get("roi")
        centre = roi["center"] if roi else apply(self.corrected_affine("mri"), (np.array(self.scan("mri")["shape"]) - 1) / 2)
        return round(float(register.mean_displacement(a, b, centre, 40.0)), 3)

    def detect_frame(self, threshold=None):  # the 0.1.7 Frame step: CT only
        if threshold not in (None, ""):
            self.set_frame_band("ct", [threshold, None])
        self.frame_autofind("ct")

    def choose_frame(self, index):
        self.frame_pose("ct", "auto", index=index)

    def frame_file(self, role="ct"):
        """Viewer overlay in the scan's display world: auto-found rods (1), the model at the current pose (2),
        tracked rod centres (3) and manual ones (4)."""
        s = self.frame_state(role)
        disp = self.display_from_raw(role)
        segs = [(1, np.array(a), np.array(b)) for a, b in (s.get("auto") or {}).get("lines", [])]
        if s.get("pose") is not None:
            ip = np.linalg.inv(np.array(s["pose"]))
            segs += [(2, apply(ip, r["p0"]), apply(ip, r["p1"])) for r in loc.rod_list(self.frame_model())]
        dots = [(4 if src == "manual" else 3, np.array(q)) for rows in (s.get("tracked") or {}).values()
                for _, q, src, _ in rows if q is not None]
        segs = [(v, apply(disp, a), apply(disp, b)) for v, a, b in segs]
        dots = [(v, apply(disp, q)) for v, q in dots]
        if not segs and not dots:
            raise ValueError("no localizer shown yet: align it or auto-find it")
        key = hashlib.sha1(json.dumps([[v, a.tolist(), b.tolist()] for v, a, b in segs]
                                      + [[v, q.tolist()] for v, q in dots]).encode()).hexdigest()[:12]
        out = self.root / "cache" / f"frame-{role}-{key}.nii.gz"
        if not out.exists():
            for old in (self.root / "cache").glob(f"frame-{role}-*.nii.gz"):
                old.unlink()
            pts = np.vstack([np.vstack([a, b]) for _, a, b in segs] + [q[None] for _, q in dots])
            lo, hi = pts.min(0) - 5, pts.max(0) + 5
            aff = np.eye(4)
            aff[:3, 3] = lo
            lab = np.zeros(tuple(np.ceil(hi - lo).astype(int) + 1), np.uint8)
            items = [(v, a + np.linspace(0, 1, int(np.linalg.norm(b - a) * 2) + 2)[:, None] * (b - a)) for v, a, b in segs]
            items += [(v, q[None]) for v, q in dots]
            for v, line in items:
                vox = np.round(line - lo).astype(int)
                for d in itertools.product((-1, 0, 1), repeat=3):  # ~3 mm thick for visibility
                    q = np.clip(vox + np.array(d), 0, np.array(lab.shape) - 1)
                    lab[q[:, 0], q[:, 1], q[:, 2]] = np.maximum(lab[q[:, 0], q[:, 1], q[:, 2]], v)
            volume.save(out, lab, aff)
        return out

    # ---- brain model (figures) ----
    def ct_on_mri(self):
        """The CT resampled onto the MRI grid (cached in memory for the current coregistration)."""
        key = (self.scan("ct")["sha256"], json.dumps(self.subject["coreg"]["ct_to_mri"]), json.dumps(self.scan("ct")["orient"]))
        if getattr(self, "_ct_on_mri", (None,))[0] != key:
            self._ct_on_mri = (key, self.ct_in_mri_grid())
        return self._ct_on_mri[1]

    def default_seed(self):
        a = self.subject.get("acpc") or {}
        if "ac" in a and "pc" in a:
            return ((np.array(a["ac"]) + np.array(a["pc"])) / 2).tolist()
        if self.subject.get("roi"):
            return self.subject["roi"]["center"]
        s = self.scan("mri")
        return apply(self.corrected_affine("mri"), (np.array(s["shape"]) - 1) / 2).tolist()

    def build_brain(self, seed=None, use_mri=True, threshold=None, smooth=20, detail=1, cuts=()):
        """Brain mask (CT inside the skull, refined by the MRI), cuts, surface and lead meshes, in cache/brain."""
        self.coreg_matrix()
        aff = self.corrected_affine("mri")
        seed = seed or self.default_seed()
        box = None
        roi = self.subject.get("roi")
        if roi:
            h, c = np.array(roi["size"]) / 2 + 10, np.array(roi["center"])
            box = brainmodel.apply_cuts(np.ones(tuple(self.scan("mri")["shape"]), bool), aff,
                                        [{"axis": ax, "keep": "above", "at": c[i] - h[i]} for i, ax in enumerate("xyz")]
                                        + [{"axis": ax, "keep": "below", "at": c[i] + h[i]} for i, ax in enumerate("xyz")])
        mask = brainmodel.intracranial(self.ct_on_mri(), aff, seed, self.ct_levels(), box=box)
        thr = None
        if use_mri:
            mask, thr = brainmodel.refine_with_mri(mask, self.volume("mri").data, threshold)
        mask = brainmodel.apply_cuts(mask, aff, list(cuts))
        d = self.root / "cache" / "brain"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        volume.save(d / "brain_mask.nii.gz", mask.astype(np.uint8), aff)
        v, f = brainmodel.surface(mask, aff, smooth=smooth, step=detail)
        brainmodel.write_gifti(d / "brain.gii", v, f)
        center = v.mean(0)
        leads = []
        if self.leads:
            m = self.ct_raw_to_mri()
            for i, L in enumerate(self.leads):
                ax = detect.Axis.from_dict(L["axis"]).transformed(m)
                s_end = max(c["s"] for c in L["contacts"]) + 20.0
                if L.get("entry_mri"):
                    s_end = max(s_end, float((np.array(L["entry_mri"]) - ax.origin) @ ax.u) + 10.0)
                (sv, sf), (cv, cf) = brainmodel.lead_meshes(ax.point, L["contacts"], L["preset"], s_end, center)
                name = re.sub(r"[^A-Za-z0-9_-]", "_", L["name"])
                brainmodel.write_gifti(d / f"lead-{name}-shaft.gii", sv, sf)
                brainmodel.write_gifti(d / f"lead-{name}-contacts.gii", cv, cf)
                leads.append({"name": L["name"], "file": name, "color": LEAD_COLORS[i % len(LEAD_COLORS)]})
        vox = float(np.prod(np.sqrt((aff[:3, :3] ** 2).sum(0))))
        self.checkpoint()
        self.subject["brain"] = {"params": {"seed": [float(x) for x in seed], "use_mri": bool(use_mri),
                                            "threshold": threshold, "smooth": int(smooth), "detail": int(detail),
                                            "cuts": list(cuts)},
                                 "threshold_used": thr, "volume_ml": round(float(mask.sum()) * vox / 1000, 2),
                                 "vertices": int(len(v)), "faces": int(len(f)), "leads": leads, "when": _now()}
        self.save()

    def export_brain(self):
        """Files for figures and for Python/MATLAB: masks (NIfTI), meshes (GIfTI, OBJ, STL), scene.json."""
        b = self.subject.get("brain")
        if not b:
            raise ValueError("build the brain model first")
        src, out = self.root / "cache" / "brain", self.root / "outputs" / "brain"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        shutil.copyfile(src / "brain_mask.nii.gz", out / "brain_mask.nii.gz")
        mask = volume.load(src / "brain_mask.nii.gz").data > 0
        volume.save(out / "brain_mri.nii.gz", np.where(mask, self.volume("mri").data, 0).astype(np.float32),
                    self.corrected_affine("mri"))
        items = []
        for g in sorted(src.glob("*.gii")):
            img = nib.load(str(g))
            v, f = img.darrays[0].data, img.darrays[1].data
            shutil.copyfile(g, out / g.name)
            brainmodel.write_obj(out / (g.stem + ".obj"), v, f)
            if g.stem == "brain":
                brainmodel.write_stl(out / "brain.stl", v, f)
            lead = next((L for L in b["leads"] if g.stem.startswith(f"lead-{L['file']}-")), None)
            items.append({"file": g.name, "obj": g.stem + ".obj", "kind": "brain" if g.stem == "brain" else
                          ("lead contacts" if g.stem.endswith("contacts") else "lead shaft"),
                          "lead": lead["name"] if lead else None,
                          "color": lead["color"] if lead and g.stem.endswith("contacts") else
                          ("#d9d9d9" if g.stem == "brain" else "#9aa3b2")})
        brainmodel.write_scene(out / "scene.json", items)
        return [str(f) for f in sorted(out.iterdir())]

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
                fm, fr = self.frame_model(), self._frame()
                used = fr["scans"][fr["use"]]
                cs = {"iEEGCoordinateSystem": "Other",
                      "iEEGCoordinateSystemDescription": f"Stereotactic frame coordinates ({fm['name']}): "
                      f"{fm.get('axes', '')}; mm. Localizer geometry verified: {bool(fm.get('verified'))}.",
                      "BrainscopeFrameFit": {"scan": fr["use"], "model": fm, "agreement_ct_mri_mm": self.frame_agreement()}
                      | {k: used.get(k) for k in ("band", "method", "summary", "pose")}}
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
        fr = self._frame()
        disp = self.frame_from_mri()
        if disp is None and fr.get("use") == "ct":  # not coregistered yet: the viewer shows the CT in its own world
            disp = self.frame_matrix()
        frame_scans = {}
        for r, s in fr["scans"].items():
            if not scans.get(r):
                continue
            frame_scans[r] = {k: s.get(k) for k in ("band", "fitted", "method", "summary", "when")} | {
                "auto": {"fits": (s.get("auto") or {}).get("fits", []), "chosen": (s.get("auto") or {}).get("chosen", 0),
                         "n_lines": len((s.get("auto") or {}).get("lines", []))},
                "manual": sum(len(v) for v in (s.get("manual") or {}).values()),
                "excluded": sum(len(v) for v in (s.get("excluded") or {}).values()),
                "display_to_frame": (np.array(s["pose"]) @ np.linalg.inv(self.display_from_raw(r))).tolist()
                if s.get("pose") is not None else None}
        return {"name": self.root.name, "species": sub["species"], "scans": scans, "coreg": sub.get("coreg"),
                "roi": sub.get("roi"), "acpc": sub.get("acpc"), "acpc_matrix": am.tolist() if am is not None else None,
                "acpc_distance": round(float(np.linalg.norm(np.subtract(a["ac"], a["pc"]))), 2)
                if "ac" in a and "pc" in a else None,
                "frame": {"model": self.frame_model(), "use": fr.get("use"), "scans": frame_scans,
                          "agreement": self.frame_agreement()},
                "frame_from_display": disp.tolist() if disp is not None else None, "brain": sub.get("brain"),
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
