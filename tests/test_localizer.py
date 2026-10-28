import numpy as np
import pytest
from scipy import ndimage

from brainscope import localizer as loc
from brainscope import phantom, presets, register
from brainscope.transforms import apply, rigid

# Leksell axes in a RAS scanner world: X to the left, Y anterior, Z inferior; frame centre at the world origin.
LEKSELL_FROM_RAS = np.array([[-1, 0, 0, 100], [0, 1, 0, 100], [0, 0, -1, 100], [0, 0, 0, 1.0]])


def _scene(truth, model, spacing=1.0, seed=0):
    """CT-like block: a skull, a DBS lead (distractor) and the localizer rods placed by `truth` (image -> frame)."""
    size = np.array([270.0, 270.0, 210.0])
    shape = tuple(int(x) for x in np.ceil(size / spacing))
    aff = phantom.grid_affine([spacing] * 3, -size / 2)
    data = np.full(shape, -1000.0, np.float32)
    ax = [np.arange(n, dtype=np.float32) * spacing - size[i] / 2 for i, n in enumerate(shape)]
    x, y, z = ax[0][:, None, None], ax[1][None, :, None], ax[2][None, None, :]
    r = np.sqrt((x / 70) ** 2 + (y / 85) ** 2 + (z / 70) ** 2)
    data[r < 1] = 40
    data[(r < 1) & (r > 0.9)] = 1200
    phantom.add_lead(data, aff, (-15, 5, -5), (-0.3, 0.3, 1), presets.load_all()["medtronic-3389"])
    inv = np.linalg.inv(truth)
    segs = [(n, apply(inv, a), apply(inv, b)) for n, a, b in loc.segments(model)]
    loc.render(data, aff, segs)
    data = ndimage.gaussian_filter(data, 0.6 / spacing)
    data += np.random.default_rng(seed).normal(0, 15, data.shape).astype(np.float32)
    return data, aff


@pytest.mark.parametrize("case", ["standard", "two_plates", "rotated_mount"])
def test_frame_is_found_and_fitted(case):
    model = loc.default_model()
    truth = LEKSELL_FROM_RAS @ rigid((3, -2, 4), (4, -6, 3))
    if case == "two_plates":
        model["plates"][2]["enabled"] = False         # no anterior plate in the scan
    if case == "rotated_mount":                        # e.g. on an animal: frame turned 90 degrees
        truth = LEKSELL_FROM_RAS @ rigid((0, 0, 90)) @ rigid((2, 3, -1), (3, 2, -4))
    data, aff = _scene(truth, model)
    lines, groups, fits = loc.detect(data, aff, loc.default_model(), 500)
    assert len(groups) == (2 if case == "two_plates" else 3)
    best = fits[0]
    assert best["rms"] < 0.5, best
    assert best["axes"] == ("X→L, Y→A, Z→I" if case != "rotated_mount" else best["axes"])
    err = register.mean_displacement(best["matrix"], truth, center=(0, 0, 0), radius=60)
    assert err < 0.5, err


def test_too_few_plates_is_reported():
    with pytest.raises(ValueError, match="at least 2"):
        loc.fit([], [], loc.default_model())


def test_frame_in_a_project(tmp_path):
    from brainscope import volume
    from brainscope.project import Project

    truth = LEKSELL_FROM_RAS @ rigid((2, -1, 3), (3, -4, 2))
    data, aff = _scene(truth, loc.default_model(), spacing=1.0)
    volume.save(tmp_path / "ct.nii.gz", data.astype(np.int16), aff)
    p = Project.create(tmp_path / "p")
    p.import_scan("ct", tmp_path / "ct.nii.gz")
    p.detect_frame(threshold=500)
    assert register.mean_displacement(p.frame_matrix(), truth, (0, 0, 0), 60) < 0.5
    assert volume.load(p.frame_file()).data.max() == 2           # detected rods and fitted model shown
    model = p.frame_model()
    model["mount"] = (np.array(model["mount"]) @ np.diag([-1, -1, 1])).tolist()  # only the expected mounting changes
    p.set_frame_model(model)
    assert p.frame_matrix() is not None                          # ... so the fit is kept


def _plate_ct(truth, model, spacing=1.0, seed=1):
    """Low-contrast CT: acrylic plates (120 HU, 10 mm thick) with fluid channels (10 HU) along the rods, and a head."""
    size = np.array([270.0, 270.0, 210.0])
    shape = tuple(int(x) for x in np.ceil(size / spacing))
    aff = phantom.grid_affine([spacing] * 3, -size / 2)
    data, _ = _scene(truth, model, spacing, seed)  # head + bright rods; the rods are replaced below
    data[data > 1300] = -1000.0
    data[:] = np.where(data < -500, -1000.0, data)
    ijk = np.stack(np.meshgrid(*[np.arange(n) for n in shape], indexing="ij"), -1).reshape(-1, 3)
    f = apply(truth @ aff, ijk).reshape(shape + (3,))
    for p in model["plates"]:
        a = 0 if p["axis"] == "x" else 1
        inside = (np.abs(f[..., a] - p["position"]) < 5) & (f[..., 1 - a] > 25) & (f[..., 1 - a] < 175) \
            & (f[..., 2] > 25) & (f[..., 2] < 175)
        data[inside] = 120.0
    for _, p0, p1 in loc.segments(model):
        d = p1 - p0
        t = np.clip((f - p0) @ d / (d @ d), 0, 1)
        dist = np.linalg.norm(f - (p0 + t[..., None] * d), axis=-1)
        data[dist < 1.5] = 10.0
    data = ndimage.gaussian_filter(data, 0.6 / spacing)
    return data + np.random.default_rng(seed).normal(0, 15, data.shape).astype(np.float32), aff


@pytest.mark.parametrize("case", ["bright_rods", "dark_channels_in_plates"])
def test_tracking_from_a_rough_pose(case):
    model = loc.default_model()
    truth = LEKSELL_FROM_RAS @ rigid((3, -2, 4), (4, -6, 3))
    if case == "bright_rods":       # e.g. fluid-filled tubes in the MRI: bright on a dark background
        data, aff = _scene(truth, model)
        band = (250, None)
    else:                           # e.g. the same localizer in the CT: rods barely differ from the plate
        data, aff = _plate_ct(truth, model)
        band = (-60, 60)
    rough = truth @ rigid((1.5, -1, 1), (2, -2, 1.5))
    pose, tracked, res, summary = loc.refine(data, aff, model, rough, band)
    assert register.mean_displacement(pose, truth, center=(0, 0, 0), radius=60) < 0.3
    assert summary["rms"] < 0.5, summary
    assert all(r["found"] > 0.8 * r["slices"] for r in summary["rods"].values()), summary


def test_manual_and_excluded_slices():
    model = loc.default_model()
    truth = LEKSELL_FROM_RAS @ rigid((3, -2, 4), (4, -6, 3))
    data, aff = _scene(truth, model)
    rod = loc.rod_list(model)[0]
    p = apply(np.linalg.inv(truth), rod["p0"] + 10 * (rod["p1"] - rod["p0"]) / np.linalg.norm(rod["p1"] - rod["p0"]))
    tracked = loc.track(data, aff, model, truth, (250, None), manual={rod["id"]: [p]}, excluded={rod["id"]: [20.0, 21.0]})
    rows = {round(t): (pt, s) for t, pt, s in tracked[rod["id"]]}
    assert rows[10][1] == "manual" and np.allclose(rows[10][0], p)
    assert rows[20][1] == rows[21][1] == "excluded" and rows[20][0] is None
    assert rows[30][1] == "auto"


def test_frame_per_scan_in_a_project(tmp_path):
    """Same localizer in a low-contrast CT and an MRI: rough alignment by hand, tracking, agreement, choice."""
    from brainscope import volume
    from brainscope.project import Project

    model = loc.default_model()
    truth = LEKSELL_FROM_RAS @ rigid((1, -1, 1), (3, -2, 2))
    ct, aff = _plate_ct(truth, model)
    mri, _ = _scene(truth, model)
    volume.save(tmp_path / "ct.nii.gz", ct.astype(np.int16), aff)
    volume.save(tmp_path / "mri.nii.gz", mri.astype(np.float32), aff)
    p = Project.create(tmp_path / "p")
    p.import_scan("ct", tmp_path / "ct.nii.gz")
    p.import_scan("mri", tmp_path / "mri.nii.gz")
    p.subject["coreg"] = {"ct_to_mri": np.eye(4).tolist()}       # same grid: identity coregistration
    centre = apply(np.linalg.inv(truth), np.mean([q for r in loc.rod_list(model) for q in (r["p0"], r["p1"])], 0))
    for role, band in (("ct", (-60, 60)), ("mri", (250, None))):
        p.set_frame_band(role, band)
        p.frame_pose(role, "reset")
        p.frame_pose(role, "place", point=centre + (1.5, -1.0, 1.0))  # the user's rough placement
        p.frame_refine(role)
        s = p.subject["frame"]["scans"][role]
        assert s["fitted"] and s["summary"]["rms"] < 0.5, s["summary"]
    assert p.subject["frame"]["use"] == "ct"                       # the first fitted scan
    assert p.frame_agreement() < 0.3
    assert register.mean_displacement(p.frame_from_mri(), truth, (0, 0, 0), 60) < 0.3
    p.frame_use("mri")
    assert register.mean_displacement(p.frame_matrix(), truth, (0, 0, 0), 60) < 0.3
    assert volume.load(p.frame_file("mri")).data.max() == 3       # model rods (2) and tracked centres (3)
    summary = p.summary()["frame"]
    assert summary["use"] == "mri" and summary["agreement"] < 0.3 and "tracked" not in summary["scans"]["ct"]
