import numpy as np
import pytest

from brainscope import detect, phantom, presets


def _preset(pid):
    return presets.load_all()[pid]


@pytest.mark.parametrize("pid,spacing,max_err", [
    ("dixi-microdeep-d08-10", (0.5, 0.5, 0.5), 0.25),     # contacts separate on the CT
    ("dixi-microdeep-d08-08", (0.45, 0.45, 0.8), 0.3),    # anisotropic voxels
    ("medtronic-3389", (0.5, 0.5, 0.5), 0.4),             # contacts merge: placed from geometry
])
def test_detect_oblique_lead(pid, spacing, max_err):
    p = _preset(pid)
    data, affine, truth, tip = phantom.lead_ct(p, spacing=spacing)
    u = (truth[-1] - truth[0]) / np.linalg.norm(truth[-1] - truth[0])
    rng = np.random.default_rng(1)
    rough_tip = tip + rng.uniform(-1.5, 1.5, 3)          # the user clicks roughly
    rough_entry = truth[-1] + 12 * u + rng.uniform(-1.5, 1.5, 3)
    lead = detect.detect(data, affine, rough_tip, rough_entry, p, lead="A")
    err = np.linalg.norm(detect.positions(lead) - truth, axis=1)
    assert err.max() < max_err, err
    assert len(lead["contacts"]) == p["n_contacts"]


def test_refit_with_anchor_and_axis_shift():
    p = _preset("dixi-microdeep-d08-05")
    data, affine, truth, tip = phantom.lead_ct(p)
    lead = detect.detect(data, affine, tip, truth[-1] + 10 * (truth[-1] - truth[0]) / 14, p)
    s2 = lead["contacts"][2]["s"]
    moved = detect.refit(data, affine, lead, anchor=(2, s2 + 0.5))
    assert moved["contacts"][2]["s"] == pytest.approx(s2 + 0.5)
    assert moved["contacts"][2]["mode"] == "manual"
    ax = detect.Axis.from_dict(lead["axis"])
    assert np.allclose(ax.shifted(3.0).point(1.0), ax.point(4.0))


def test_views_and_cross_section():
    p = _preset("dixi-microdeep-d08-05")
    data, affine, truth, tip = phantom.lead_ct(p)
    lead = detect.detect(data, affine, tip, truth[-1], p)
    v = detect.views(data, affine, lead)
    assert len(v["profile"]) == len(v["model"]) == len(v["strip1"])
    x = detect.cross_section(data, affine, lead, lead["contacts"][0]["s"])
    img = np.array(x["values"])
    peak = np.unravel_index(np.argmax(img), img.shape)  # the lead is at the center of the plane
    assert np.hypot(*(np.array(peak) - np.array(img.shape) // 2)) * x["h"] < 0.3
