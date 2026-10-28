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
    lines, groups, fits = loc.detect(data, aff, loc.default_model(), threshold=500)
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
