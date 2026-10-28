import numpy as np
import pytest

from brainscope import acpc
from brainscope.transforms import apply


def test_acpc_frame_axes_and_origins():
    ac, pc, msp = np.array([1.0, 12, 0]), np.array([1.0, -13, 0]), np.array([1.0, 0, 40])
    m = acpc.matrix(ac, pc, msp)                       # origin at the mid-commissural point
    assert np.allclose(apply(m, ac), [0, 12.5, 0]) and np.allclose(apply(m, pc), [0, -12.5, 0])
    assert apply(m, msp)[2] > 0                         # midline point is superior
    assert apply(m, [11.0, -0.5, 0])[0] == pytest.approx(10)  # +x is the subject's right (RAS world)
    assert np.allclose(apply(acpc.matrix(ac, pc, msp, "ac"), ac), 0)
    assert np.allclose(m[:3, :3] @ m[:3, :3].T, np.eye(3))  # a rigid change of axes
    with pytest.raises(ValueError):
        acpc.matrix(ac, pc, ac + (ac - pc))              # midline point on the AC-PC line


def test_compare_with_planned_trajectory():
    r = acpc.compare(planned=[10, 0, -5], delivered=[11, 0, -7], entry=[10, 0, 45])
    assert r["distance"] == pytest.approx(np.sqrt(5), abs=1e-3)
    assert r["depth"] == pytest.approx(2)               # 2 mm deeper than planned
    assert r["radial"] == pytest.approx(1)
