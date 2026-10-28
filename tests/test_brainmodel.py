import nibabel as nib
import numpy as np

from brainscope import brainmodel as bm
from brainscope import phantom, presets
from brainscope.register import ct_levels


def test_brain_mask_cuts_and_surface(tmp_path):
    ct, aff = phantom.head(spacing=2.0, contrast="ct")
    mri, _ = phantom.head(spacing=2.0, contrast="mri")
    cavity = bm.intracranial(ct, aff, seed=(0, 0, 5), levels=ct_levels(ct))
    true_ml = 4 / 3 * np.pi * 52 * 66 * 52 / 1000           # the phantom's brain ellipsoid
    ml = cavity.sum() * 8 / 1000
    assert abs(ml - true_ml) / true_ml < 0.12, ml
    brain, thr = bm.refine_with_mri(cavity, mri)
    assert brain.sum() < cavity.sum()                       # CSF (ventricles) removed
    half = bm.apply_cuts(brain, aff, [{"axis": "x", "keep": "below", "at": 0.0}])  # keep the left hemisphere
    assert 0.4 < half.sum() / brain.sum() < 0.6
    v, f = bm.surface(half, aff, smooth=10)
    assert len(f) > 1000 and v[:, 0].max() < 3.0
    for writer, ext in ((bm.write_gifti, ".gii"), (bm.write_obj, ".obj"), (bm.write_stl, ".stl")):
        writer(tmp_path / f"b{ext}", v, f)
    g = nib.load(str(tmp_path / "b.gii"))
    assert np.allclose(g.darrays[0].data, v, atol=1e-3)
    assert (tmp_path / "b.stl").stat().st_size == 84 + 50 * len(f)


def test_lead_meshes_depth_and_strip():
    p = presets.load_all()["dixi-microdeep-d08-05"]
    axis = lambda s: np.stack([np.zeros_like(s), np.zeros_like(s), np.asarray(s, float)], -1)
    contacts = [{"s": 1.0 + 3.5 * k} for k in range(5)]
    (sv, sf), (cv, cf) = bm.lead_meshes(axis, contacts, p, s_end=40)
    assert sv[:, 2].max() >= 39 and len(cf) > 0
    strip = dict(p, type="strip", contact_length_mm=4.0, spacing_mm=10.0)
    (_, _), (dv, _) = bm.lead_meshes(axis, contacts, strip, s_end=40, center=(-30, 0, 0))
    assert np.ptp(dv[:, 0]) < 1.0                            # discs face the centre: thin along x
