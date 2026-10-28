import numpy as np
import pytest

from brainscope import orientation as ori
from brainscope import phantom, register, volume
from brainscope.transforms import apply, ras_to_sitk, rigid, sitk_to_ras, to_sitk


def test_ops_reach_all_48_orientations():
    seen, frontier = {ori.op_matrix("fx").tobytes()}, [np.eye(3, dtype=int)]
    seen = {frontier[0].tobytes()}
    while frontier:
        m = frontier.pop()
        for op in ori.OPS:
            n = ori.apply_op(m, op)
            if n.tobytes() not in seen:
                seen.add(n.tobytes())
                frontier.append(n)
    assert len(seen) == 48 == len(ori.all_orientations())


def test_correction_is_header_only_and_keeps_center(tmp_path):
    data = np.random.default_rng(0).random((5, 6, 7)).astype(np.float32)
    aff = np.diag([0.5, 0.6, 0.7, 1])
    aff[:3, 3] = [10, -4, 3]
    src = tmp_path / "a.nii.gz"
    volume.save(src, data, aff)
    m = ori.apply_op(ori.apply_op(np.eye(3, dtype=int), "rx+"), "fz")
    new = ori.corrected_affine(m, aff, data.shape)
    volume.save_reheadered(src, tmp_path / "b.nii", new)
    v = volume.load(tmp_path / "b.nii")
    assert np.array_equal(v.data, data)
    assert np.allclose(v.affine, new)
    assert np.allclose(v.center, volume.load(src).center)
    assert np.linalg.det(new[:3, :3]) == pytest.approx(-np.linalg.det(aff[:3, :3]))


def test_axis_labels():
    assert ori.axis_labels(np.eye(3)) == "RAS"
    assert ori.axis_labels(ori.op_matrix("fx")) == "LAS"


def test_ras_lps_round_trip():
    m = rigid((5, -7, 12), (3, -2, 8), (10, 20, 30))
    assert np.allclose(sitk_to_ras(ras_to_sitk(m, center=(1, 2, 3))), m)
    data = np.zeros((4, 5, 6), np.float32)
    aff = np.array([[0, -1.5, 0, 20], [0.8, 0, 0, -10], [0, 0, 2, 5], [0, 0, 0, 1]])
    img = to_sitk(data, aff)
    p_ras = apply(aff, [1, 2, 3])
    p_lps = np.array(img.TransformIndexToPhysicalPoint((1, 2, 3)))
    assert np.allclose(p_lps * [-1, -1, 1], p_ras)


@pytest.mark.parametrize("offset", [0, 1024])  # CT in HU, or stored as HU + 1024 (e.g. 0-4095)
def test_coregistration_recovers_known_rigid_transform(offset):
    mri, aff = phantom.head(spacing=2.0, contrast="mri")
    ct, _ = phantom.head(spacing=2.0, contrast="ct", seed=1)
    ct = np.clip(ct + offset, -1024 if offset == 0 else 0, None)
    assert register.ct_levels(ct)["kind"] == ("hu" if offset == 0 else "hu+offset")
    truth = rigid((6, -4, 8), (5, -3, 4), (0, 0, 0))  # CT world -> MRI world
    ct_aff = np.linalg.inv(truth) @ aff
    m, _ = register.coregister(ct, ct_aff, mri, aff, species="human")
    assert register.mean_displacement(m, truth, center=(0, 0, 0), radius=50) < 1.0
