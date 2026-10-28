"""A synthetic demo case: head MRI + CT with three leads, a CT marker on the left, and wrong ("pig-like")
orientation headers to fix. Ground truth is written to truth.json."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import orientation as ori
from . import phantom
from . import presets as pr
from .transforms import rigid
from .volume import save

LEADS = [("LA", "dixi-microdeep-d08-10", (-25, 10, -5), (-58, 28, 25)),
         ("RB", "dixi-microdeep-d08-08", (20, -15, 0), (56, -32, 20)),
         ("LD", "medtronic-3389", (-12, -5, -10), (-28, 12, 56))]
MRI_WRONG, CT_WRONG = ["rx+"], ["fx", "rz+"]  # what the "scanner" got wrong
MRI_FIX, CT_FIX = ["rx-"], ["rz-", "fx"]      # what the user has to press to undo it


def _wrong(ops, affine, shape):
    m = np.eye(3, dtype=int)
    for op in ops:
        m = ori.apply_op(m, op)
    return ori.corrected_affine(m, affine, shape)


def make(out_dir, ct_spacing=0.7):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    mri, aff = phantom.head(spacing=1.0, contrast="mri")
    ct, ct_aff = phantom.head(spacing=ct_spacing, contrast="ct", seed=1)
    presets = pr.load_all()
    truth = {}
    for name, pid, tip, entry in LEADS:
        p = presets[pid]
        d = np.subtract(entry, tip)
        centers = phantom.add_lead(ct, ct_aff, tip, d, p, length=float(np.linalg.norm(d)) + 15)
        for k, c in enumerate(centers):
            truth[pr.label(p, name, k)] = [round(float(x), 3) for x in c]
    phantom.add_lead(ct, ct_aff, (-61, 0, 10), (-1, 0, 0), dict(presets["generic-depth-8"], n_contacts=1,
                     contact_length_mm=3.0, diameter_mm=3.0), length=3.0, metal_hu=2500)
    ct = phantom.blur(ct, ct_aff, psf_mm=0.6, noise=8)
    true_ct_to_mri = rigid((4, -3, 6), (3, -5, 2))
    ct_aff = np.linalg.inv(true_ct_to_mri) @ ct_aff
    save(out / "mri.nii.gz", np.round(mri).astype(np.int16), _wrong(MRI_WRONG, aff, mri.shape))
    save(out / "ct.nii.gz", np.clip(np.round(ct), -1024, 32767).astype(np.int16), _wrong(CT_WRONG, ct_aff, ct.shape))
    (out / "truth.json").write_text(json.dumps({
        "fix_mri": MRI_FIX, "fix_ct": CT_FIX, "ct_to_mri": true_ct_to_mri.tolist(),
        "leads": [{"name": n, "preset": p, "tip": t, "entry": e} for n, p, t, e in LEADS],
        "contacts_mri": truth, "marker": "CT marker on the subject's left side"}, indent=1))
    return out
