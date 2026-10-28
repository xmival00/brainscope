import nibabel as nib
import numpy as np
import pytest
import SimpleITK as sitk

from brainscope import dicom, phantom, volume
from brainscope.project import Project
from brainscope.transforms import apply, rotation, to_sitk


def _bone_center(d, a):
    return apply(a, np.argwhere(d > 500)).mean(0)


@pytest.fixture
def series(tmp_path):
    data, aff = phantom.head(spacing=4.0, contrast="ct")
    oblique = np.eye(4)
    oblique[:3, :3] = rotation((0, 0, 20))  # in-plane rotation, as on a tilted scan
    aff = oblique @ aff
    dicom.write_test_series(data, aff, tmp_path / "dcm")
    return tmp_path / "dcm", data, aff


@pytest.mark.parametrize("converter", ["dcm2niix", "simpleitk"])
def test_dicom_series_converts_with_the_right_geometry(tmp_path, series, converter, monkeypatch):
    folder, data, aff = series
    if converter == "simpleitk":  # e.g. the OS refuses to run the bundled dcm2niix
        monkeypatch.setattr(dicom, "binary", lambda: str(tmp_path / "missing-dcm2niix"))
    out = dicom.convert(folder, tmp_path / "out")
    assert len(out) == 1 and out[0]["modality"] == "CT"
    v = volume.load(tmp_path / "out" / out[0]["file"])
    assert np.linalg.norm(_bone_center(v.data, v.affine) - _bone_center(data, aff)) < 0.5
    assert sorted(v.data.shape) == sorted(data.shape)
    for side in (tmp_path / "out").glob("*.json"):
        assert "Test" not in side.read_text()


@pytest.mark.parametrize("ext", [".mgz", ".nrrd", ".mha"])
def test_other_formats_are_converted_on_import(tmp_path, ext):
    data, aff = phantom.head(spacing=5.0, contrast="mri")
    aff = np.array([[0, -5.0, 0, 30], [5.0, 0, 0, -12], [0, 0, 5.0, 7], [0, 0, 0, 1]])
    src = tmp_path / f"scan{ext}"
    if ext == ".mgz":
        nib.save(nib.MGHImage(data, aff), str(src))
    else:
        sitk.WriteImage(to_sitk(data, aff), str(src))
    p = Project.create(tmp_path / "p")
    p.import_scan("mri", src)
    v = volume.load(p.path("mri"))
    assert p.path("mri").name.endswith(".nii.gz")
    assert np.allclose(v.affine, aff, atol=1e-4)
    assert np.allclose(v.data, data, atol=1e-3)
